"""Probe A of the cross-boundary coupling list (issue #34): boundary-independent pauses.

A0 and A1 ask one question at two cost levels: while the controller holds a
*completed* boundary in a hidden-window run, does advancing the host wall clock
— optionally while desktop focus or the cursor is perturbed — change the
captured simulation state, or the trajectory that continues afterwards?

The rungs, the judgement and the evidence shape follow ``docs/跨界耦合清单.md``
§4 (A0 墙钟诊断, A1 短/中挂起) and the report conventions of
``docs/headless-validation.md``. This module owns the criteria and the offline
assessor; the live part is started by an operator against a process that is
already paused at a boundary:

    python -m llm_vs_zombies.suspend_probes probe --pid <PID> --seconds 300 \
        --perturb wall,focus,cursor --trace <run>/decisions/a0-trace.jsonl \
        --output <run>/suspend-probe-a0.json
    python -m llm_vs_zombies.suspend_probes assess --run <run> \
        --replay-report <output>/seed-42-replay-0/replay-report.json

Nothing here relaxes a gate: the assessor only re-derives the §4.6 criteria from
evidence that already exists, and reports ``unverified`` instead of ``pass``
whenever a piece of evidence is missing.
"""
from __future__ import annotations

import argparse
import ctypes
import gzip
import hashlib
import json
import sys
import time
from pathlib import Path

from .records import read_json, write_json

SCHEMA = "lvz.suspend-probe.v1"
ASSESSMENT_SCHEMA = "lvz.suspend-probe-assessment.v1"
PERTURBATIONS = ("wall", "focus", "cursor")
BOUNDARY_KINDS = ("pre_step", "post_step", "scene_changed", "terminal_transition")
CHECKSUMS_EVIDENCE = "checksums.jsonl"


class PerturbationError(RuntimeError):
    pass


def normalize_kinds(perturbations) -> tuple[str, ...]:
    """Validate the requested kinds; the wall delay is the baseline of every probe."""
    if isinstance(perturbations, str):
        perturbations = [item for item in perturbations.replace(",", " ").split() if item]
    kinds = []
    for item in perturbations or ():
        if item not in PERTURBATIONS:
            raise ValueError(f"unknown perturbation {item!r}; known kinds: {', '.join(PERTURBATIONS)}")
        if item not in kinds:
            kinds.append(item)
    return ("wall", *(item for item in kinds if item != "wall"))


class HostPerturbations:
    """Wall clock and desktop perturbations applied while a run is paused.

    ``simulated`` records the intent without touching the desktop, for runs
    where the operator must keep the machine usable. The wall clock still
    advances: that delay *is* the A0 subject, and a simulated cursor move whose
    wall time never passed would not exercise the same question.
    """

    simulated = False

    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def screen(self) -> dict:
        raise PerturbationError("this host cannot report screen metrics")

    def cursor_position(self) -> dict:
        raise PerturbationError("this host cannot read the cursor position")

    def move_cursor(self, x: int, y: int) -> None:
        raise PerturbationError("this host cannot move the cursor")

    def foreground(self) -> dict:
        raise PerturbationError("this host cannot read the foreground window")

    def steal_foreground(self) -> dict:
        raise PerturbationError("this host cannot change the foreground window")

    def restore_foreground(self, hwnd) -> bool:
        raise PerturbationError("this host cannot restore the foreground window")


class DesktopHost(HostPerturbations):
    """Real Win32 perturbations. Only constructed for a live run on Windows."""

    def __init__(self, *, simulated: bool = False):
        if sys.platform != "win32":
            raise PerturbationError("desktop perturbations require Windows")
        self.simulated = simulated

    @staticmethod
    def _user32():
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.GetCursorPos.restype = wintypes.BOOL
        user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        user32.SetCursorPos.restype = wintypes.BOOL
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetShellWindow.restype = wintypes.HWND
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.GetSystemMetrics.restype = ctypes.c_int
        return user32

    def screen(self) -> dict:
        user32 = self._user32()
        return {"width": user32.GetSystemMetrics(0), "height": user32.GetSystemMetrics(1)}

    def cursor_position(self) -> dict:
        from ctypes import wintypes
        point = wintypes.POINT()
        if not self._user32().GetCursorPos(ctypes.byref(point)):
            raise PerturbationError(f"GetCursorPos failed with winerror {ctypes.get_last_error()}")
        return {"x": point.x, "y": point.y}

    def move_cursor(self, x: int, y: int) -> None:
        if not self._user32().SetCursorPos(int(x), int(y)):
            raise PerturbationError(f"SetCursorPos failed with winerror {ctypes.get_last_error()}")

    def foreground(self) -> dict:
        from ctypes import wintypes
        user32 = self._user32()
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return {"hwnd": 0, "pid": None, "title": None}
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, len(title))
        return {"hwnd": int(hwnd), "pid": int(pid.value), "title": title.value or None}

    def steal_foreground(self) -> dict:
        user32 = self._user32()
        target = user32.GetShellWindow() or user32.FindWindowW("Shell_TrayWnd", None)
        if not target:
            raise PerturbationError("no desktop window is available to take the foreground")
        if not user32.SetForegroundWindow(target):
            raise PerturbationError(f"SetForegroundWindow failed with winerror {ctypes.get_last_error()}")
        return {"target": "shell", **self.foreground()}

    def restore_foreground(self, hwnd) -> bool:
        if not hwnd:
            return False
        return bool(self._user32().SetForegroundWindow(hwnd))


def apply_perturbations(host: HostPerturbations, kinds, seconds: float) -> list[dict]:
    """Apply every kind in order and return one record per kind.

    A failure is recorded, never raised: a perturbation that could not be
    applied is evidence of a boundary, and the judgement then reports
    ``unverified`` instead of silently claiming invariance under it.
    """
    records = []
    for kind in normalize_kinds(kinds):
        try:
            if kind == "wall":
                started = host.now()
                host.sleep(seconds)
                records.append({"kind": "wall", "applied": True, "requested_seconds": seconds,
                                "wall_seconds": host.now() - started, "simulated": False})
            elif kind == "focus":
                records.append(_focus_record(host))
            elif kind == "cursor":
                records.append(_cursor_record(host))
        except Exception as error:  # noqa: BLE001 - the boundary is the evidence
            records.append({"kind": kind, "applied": False,
                            "error": f"{type(error).__name__}: {error}"})
    return records


def _focus_record(host: HostPerturbations) -> dict:
    before = host.foreground()
    if host.simulated:
        return {"kind": "focus", "applied": True, "simulated": True, "before": before,
                "target": None, "after": None, "restored": None}
    target = host.steal_foreground()
    after = host.foreground()
    restored = host.restore_foreground(before.get("hwnd"))
    return {"kind": "focus", "applied": True, "simulated": False, "before": before,
            "target": target, "after": after, "restored": bool(restored)}


def _cursor_record(host: HostPerturbations) -> dict:
    before = host.cursor_position()
    screen = host.screen()
    target = {"x": max(0, screen["width"] - 2), "y": max(0, screen["height"] - 2)}
    if host.simulated:
        return {"kind": "cursor", "applied": True, "simulated": True, "before": before,
                "target": target, "after": None, "restored": None}
    host.move_cursor(target["x"], target["y"])
    after = host.cursor_position()
    host.move_cursor(before["x"], before["y"])
    restored = host.cursor_position()
    return {"kind": "cursor", "applied": True, "simulated": False, "before": before,
            "target": target, "after": after, "restored": restored == before}


def compare_snapshots(before: dict, after: dict, observation: dict, observation_after: dict,
                      *, hello: dict | None = None) -> dict:
    """The A0/A1 field comparison. Only declared, captured fields are compared."""
    checks = {"version_unchanged": before.get("version") == after.get("version"),
              "state_unchanged": before.get("state") == after.get("state"),
              "observation_unchanged": observation == observation_after}
    if "engine_call" in before or "engine_call" in after:
        # One controlled call writes one pre_step/post_step pair, so identical
        # counters across the pause prove no boundary executed inside it.
        checks["engine_call_counters_unchanged"] = before.get("engine_call") == after.get("engine_call")
    from . import fp_environment
    game = (hello or {}).get("game", {})
    if fp_environment.mode(game):
        checks["fixed_fp_activation_unchanged"] = (
            before.get("fixed_fp", {}).get("activation") == after.get("fixed_fp", {}).get("activation"))
    return checks


def judge(record: dict) -> dict:
    """Re-derive the verdict of one probe record from its own evidence.

    ``fail`` means a compared field moved (or the wall clock never advanced);
    ``unverified`` means a requested perturbation was not actually applied, so
    invariance under it is simply not established.
    """
    failures, missing = [], []
    if record.get("schema") != SCHEMA:
        failures.append(f"not a {SCHEMA} record")
    checks = record.get("checks")
    if not isinstance(checks, dict) or not checks:
        failures.append("probe carries no field comparison")
    else:
        failures.extend(f"{name} is not true" for name, value in sorted(checks.items()) if value is not True)
    wall, requested = record.get("wall_seconds"), record.get("requested_seconds")
    if isinstance(wall, bool) or not isinstance(wall, (int, float)) or wall < 0:
        failures.append("wall_seconds is missing or invalid")
    elif not isinstance(requested, bool) and isinstance(requested, (int, float)) and wall + 1e-6 < requested:
        failures.append(f"pause lasted {wall:.3f}s of the requested {requested:.3f}s")
    declared = record.get("requested_perturbations")
    if declared is not None:
        applied = {item.get("kind") for item in record.get("perturbations") or [] if item.get("applied") is True}
        for kind in normalize_kinds(declared):
            if kind not in applied:
                detail = next((item.get("error") for item in record.get("perturbations") or []
                               if item.get("kind") == kind and item.get("error")), None)
                missing.append(f"{kind} perturbation was not applied" + (f": {detail}" if detail else ""))
    verdict = "fail" if failures else "unverified" if missing else "pass"
    return {"verdict": verdict, "failures": failures, "missing": missing, "checks": checks or {}}


def perturbation_record(host: HostPerturbations, kinds, seconds: float, *, applied: list[dict],
                        wall_seconds: float, before: dict, after: dict, observation: dict,
                        observation_after: dict, hello: dict | None = None) -> dict:
    """The declared-perturbation part of a plan-driven (A1) pause probe record.

    The runner keeps its own long-standing record shape; this block is added
    only when a plan actually declares perturbations, so an undeclared run's
    evidence stays byte-identical to what it was before this channel existed.
    """
    record = {"schema": SCHEMA, "requested_seconds": float(seconds), "wall_seconds": wall_seconds,
              "requested_perturbations": list(normalize_kinds(kinds)), "perturbations": applied,
              "simulated": bool(getattr(host, "simulated", False)),
              "checks": compare_snapshots(before, after, observation, observation_after, hello=hello)}
    record["judgement"] = judge(record)
    record["verdict"] = record["judgement"]["verdict"]
    return record


def runner_probe_checks(record: dict, *, expected_seconds=None) -> dict:
    """The fields a plan-driven pause probe must record to be usable as evidence."""
    digest = record.get("state_sha256")
    checks = {"version_recorded": isinstance(record.get("version"), dict),
              "state_sha256_recorded": isinstance(digest, str) and len(digest) == 64}
    if expected_seconds is not None:
        wall = record.get("wall_seconds")
        checks["pause_lasted_long_enough"] = (not isinstance(wall, bool) and isinstance(wall, (int, float))
                                              and wall + 1e-6 >= expected_seconds)
    return checks


def probe(client, *, seconds: float, perturbations=(), host: HostPerturbations | None = None,
          label: str | None = None, pause: bool = True) -> dict:
    """Run one A0/A1 probe against a live client that is paused at a boundary."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
        raise ValueError("seconds must be a positive number")
    kinds = normalize_kinds(perturbations)
    host = host or DesktopHost()
    pause_evidence = None
    if client.request("status").get("state") != "paused_at_boundary":
        if not pause:
            raise RuntimeError("probe requires an already completed, paused boundary")
        observation = client.observe()
        paused = client.request("pause", {}, expect=observation["version"])
        pause_evidence = {"state": paused.get("state"), "observation_version": observation["version"],
                          "observation_unchanged": paused.get("observation") == observation}
        if paused.get("request_id") is not None:
            pause_evidence["request_id"] = paused["request_id"]
        state = client.request("status").get("state")
        if state != "paused_at_boundary":
            raise RuntimeError(f"pause did not reach a stable boundary: state={state}")
    before = client.request("audit_snapshot")
    observation = client.observe()
    records = apply_perturbations(host, kinds, seconds)
    wall = next((item["wall_seconds"] for item in records if item["kind"] == "wall"), None)
    after = client.request("audit_snapshot")
    observation_after = client.observe()
    record = {"schema": SCHEMA, "probe": "A0", "label": label,
              "requested_seconds": float(seconds), "wall_seconds": wall,
              "requested_perturbations": list(kinds), "perturbations": records,
              "simulated": bool(getattr(host, "simulated", False)),
              "pause": pause_evidence,
              "version": before.get("version"), "state_sha256": hashlib.sha256(
                  json.dumps(before.get("state"), sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
              "checks": compare_snapshots(before, after, observation, observation_after,
                                          hello=getattr(client, "hello_result", None)),
              "compared_fields": ["state", "version", "observation"]}
    record["judgement"] = judge(record)
    record["verdict"] = record["judgement"]["verdict"]
    return record


def boundary_records(audit_directory) -> list[dict]:
    """Ordered boundary coordinates from a closed native audit (checksums stream).

    The checksum stream carries ``seq``/``kind``/``version`` and the engine-call
    counter per boundary without the captured state, so the scan stays cheap
    even for tens of thousands of ticks.
    """
    from . import evidence_codec
    from .audit_compare import SCHEMA as AUDIT_SCHEMA
    directory = Path(audit_directory)
    manifest = read_json(directory / "manifest.json")
    if manifest.get("schema") != AUDIT_SCHEMA:
        raise ValueError("native audit manifest is missing or malformed")
    store = evidence_codec.EvidenceStore(directory, error=ValueError)
    path = store.stored_path(CHECKSUMS_EVIDENCE)
    if not path.is_file():
        raise ValueError(f"native audit has no {CHECKSUMS_EVIDENCE} stream")
    opener = gzip.open if path.suffix == ".gz" else open
    records = []
    with opener(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            item = json.loads(line)
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            call = payload.get("engine_call") if isinstance(payload.get("engine_call"), dict) else {}
            records.append({"seq": item.get("seq"), "kind": item.get("kind"), "version": item.get("version"),
                            "engine_call_id": call.get("engine_call_id")})
    if not records:
        raise ValueError("native audit has no boundary records")
    return records


def boundary_checks(records: list[dict], probes) -> dict:
    """§4.6 criterion 2: no boundary, scene or epoch change inside any pause."""
    checks, details = {}, {}
    steps = [item for item in records if item["kind"] == "post_step"]
    coordinates = [(item["version"]["epoch"], item["version"]["tick"], item["version"]["revision"])
                   for item in steps if isinstance(item.get("version"), dict)]
    checks["boundary_sequence_strictly_increasing"] = all(
        left < right for left, right in zip(coordinates, coordinates[1:]))
    ids = [item["engine_call_id"] for item in records if item["engine_call_id"] is not None]
    ordered = [value for index, value in enumerate(ids) if index == 0 or value != ids[index - 1]]
    checks["engine_call_ids_contiguous"] = bool(ordered) and all(
        right == left + 1 for left, right in zip(ordered, ordered[1:]))
    transitions = [item for item in records if item["kind"] in ("scene_changed", "terminal_transition")]
    details["scene_or_terminal_transitions"] = len(transitions)
    for index, item in enumerate(probes):
        version = item.get("version")
        if not isinstance(version, dict):
            continue
        key = (version.get("epoch"), version.get("tick"), version.get("revision"))
        matches = [coordinate for coordinate in coordinates if coordinate == key]
        if item.get("requested_tick") is None:
            # The pre-play probe pauses at B(0) before any controlled step; no
            # pre/post boundary exists there by construction.
            details[f"pause_{index}_boundary"] = "not_applicable_before_first_step" if not matches else "extra"
            continue
        checks[f"pause_{index}_paused_boundary_recorded_once"] = len(matches) == 1
        details[f"pause_{index}_boundary_matches"] = len(matches)
    return {"checks": checks, "details": details, "boundary_count": len(steps),
            "controlled_calls": len(ordered), "scene_or_terminal_transitions": len(transitions)}


def replay_checks(report: dict) -> dict:
    """§4.6 criterion 3, as far as its own report can prove it.

    ``pause_controls`` only exists when the *trajectory* carries `pause`
    requests. The runner's plan-driven pauses are client-side wall time and
    leave no such request, so ``recorded == 0`` is not a failure: the cold leg
    then proves equality frame by frame, not a re-executed pause. That boundary
    is reported in ``cold_leg_pauses`` and documented in
    ``docs/挂起扰动探针A0A1.md`` §6.
    """
    checks = {"replay_equal": report.get("equal") is True,
              "replay_requests_recorded": bool(report.get("requests"))}
    controls = report.get("pause_controls")
    if isinstance(controls, dict):
        recorded, executed, verified = (controls.get("recorded"), controls.get("executed"),
                                        controls.get("verified_noop"))
        checks["replay_pauses_executed"] = (type(recorded) is int and recorded >= 0
                                            and executed == recorded and verified == recorded)
    return checks


def read_probe_files(run: Path) -> dict:
    run = Path(run)
    files = {}
    for name in ("pause-probe.json", "pause-probes-during-play.json"):
        path = run / name
        files[name] = read_json(path) if path.is_file() else None
    return files


def assess(run, *, replay_report=None) -> dict:
    """Re-derive the A1 judgement from a session directory and its side evidence.

    A hard criterion that is contradicted yields ``fail``; a criterion whose
    evidence is simply absent (no audit, no replay report, an unapplied
    perturbation) yields ``unverified``. Neither ever upgrades an old archive.
    """
    run = Path(run)
    files = read_probe_files(run)
    schedule = files["pause-probes-during-play.json"]
    failed, unverified, checks = [], [], {}
    configured, completed = [], []
    if schedule is None:
        unverified.append("run has no pause-probes-during-play.json")
    elif not isinstance(schedule, dict):
        failed.append("pause-probes-during-play.json is malformed")
    else:
        configured = list(schedule.get("configured") or [])
        completed = [item for item in schedule.get("completed") or [] if isinstance(item, dict)]
        # A run that ends before a configured tick reports that point as
        # "not_applicable_beyond_actual_endpoint" and still passes its own
        # coverage gate; the assessor keeps the same meaning instead of
        # inventing a stricter rule.
        not_reached = {item.get("requested_tick"): item for item in schedule.get("not_reached") or []
                       if isinstance(item, dict)}
        checks["pause_coverage_pass"] = schedule.get("coverage_status") == "pass"
        if checks["pause_coverage_pass"] is not True:
            failed.append(f"pause coverage is {schedule.get('coverage_status')!r}")
        if not completed:
            failed.append("no completed pause probe is recorded for this run")
        for point in configured:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                failed.append(f"malformed configured pause point {point!r}")
                continue
            tick, seconds = point
            matches = [item for item in completed if item.get("requested_tick") == tick]
            if matches:
                verdict = runner_probe_checks(matches[-1], expected_seconds=seconds)
            elif tick in not_reached:
                beyond = not_reached[tick]
                verdict = {"beyond_actual_endpoint":
                           beyond.get("status") == "not_applicable_beyond_actual_endpoint"
                           and beyond.get("wall_seconds") == seconds}
            else:
                verdict = {"completed_probe_recorded": False}
            checks.update({f"pause_at_{tick}.{name}": value for name, value in verdict.items()})
            failed.extend(f"pause at tick {tick}: {name} is not satisfied"
                          for name, value in verdict.items() if value is not True)
    for index, item in enumerate([files["pause-probe.json"], *completed]):
        if not isinstance(item, dict) or item.get("schema") != SCHEMA:
            continue
        verdict = judge(item)
        checks[f"probe_{index}_verdict"] = verdict["verdict"]
        if verdict["verdict"] == "fail":
            failed.append(f"probe {index}: " + "; ".join(verdict["failures"]))
        elif verdict["verdict"] == "unverified":
            unverified.append(f"probe {index}: " + "; ".join(verdict["missing"]))
    audit, replay = None, None
    try:
        records = boundary_records(run / "audit")
    except Exception as error:  # noqa: BLE001 - absent audit evidence is a boundary
        unverified.append(f"native audit could not be scanned: {type(error).__name__}: {error}")
    else:
        audit = boundary_checks(records, completed)
        checks.update({f"audit.{name}": value for name, value in audit["checks"].items()})
        failed.extend(f"native audit check failed: {name}"
                      for name, value in audit["checks"].items() if value is not True)
    if replay_report is not None:
        report = read_json(Path(replay_report))
        replay = replay_checks(report)
        checks.update({f"replay.{name}": value for name, value in replay.items()})
        failed.extend(f"replay check failed: {name}" for name, value in replay.items() if value is not True)
        controls = report.get("pause_controls")
        if isinstance(controls, dict) and controls.get("recorded") == 0 and completed:
            # Documented boundary: the pause this run measured was client-side
            # wall time, so the trajectory carries no pause request for the cold
            # leg to re-execute. Cold-leg evidence is then frame equality only.
            replay["cold_leg_pauses"] = 0
            replay["cold_leg_note"] = ("trajectory carries no pause request; the cold leg proves frame equality, "
                                       "not a re-executed pause")
        if report.get("equal") is not True:
            failed.append("replay is not equal: "
                          + json.dumps(report.get("failure"), ensure_ascii=False)[:400])
    verdict = "fail" if failed else "unverified" if unverified else "pass"
    return {"schema": ASSESSMENT_SCHEMA, "probe": "A1", "run": str(run),
            "replay_report": None if replay_report is None else str(replay_report),
            "checks": checks, "failures": failed, "unverified": unverified, "reasons": failed + unverified,
            "verdict": verdict,
            "pause_configured": configured or None, "pause_completed": completed,
            "audit": audit, "replay": replay,
            "scope": "captured state, recorded boundaries and the declared pause schedule only; "
                     "not a bitwise proof of the original engine"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    live = sub.add_parser("probe", help="run one A0/A1 probe against a live paused run")
    live.add_argument("--pid", type=int, required=True)
    live.add_argument("--seconds", type=float, required=True, help="wall clock held inside the pause")
    live.add_argument("--perturb", default="wall",
                      help=f"comma separated subset of {', '.join(PERTURBATIONS)}; wall is always applied")
    live.add_argument("--simulate", action="store_true",
                      help="record the desktop perturbations without touching the desktop")
    live.add_argument("--no-pause", action="store_true", help="require an already paused boundary")
    live.add_argument("--label", default=None)
    live.add_argument("--branch-id", default=None,
                      help="bind the connection to this runtime branch scope (fails on a mismatch)")
    live.add_argument("--timeout", type=float, default=90.0)
    live.add_argument("--trace", type=Path, required=True,
                      help="client audit JSONL, e.g. <run>/decisions/a0-trace.jsonl")
    live.add_argument("--output", type=Path, required=True)
    offline = sub.add_parser("assess", help="re-derive the A1 judgement from recorded evidence")
    offline.add_argument("--run", type=Path, required=True)
    offline.add_argument("--replay-report", type=Path, default=None)
    offline.add_argument("--output", type=Path, default=None)
    arguments = parser.parse_args(argv)
    if arguments.command == "assess":
        report = assess(arguments.run, replay_report=arguments.replay_report)
        if arguments.output is not None:
            write_json(arguments.output, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["verdict"] == "pass" else 1
    from .client import connect
    from .session import SessionTrace
    with SessionTrace(arguments.trace) as trace:
        client = connect(pid=arguments.pid, trace=trace, timeout=arguments.timeout,
                         expected_branch=arguments.branch_id)
        try:
            record = probe(client, seconds=arguments.seconds, perturbations=arguments.perturb,
                           host=DesktopHost(simulated=arguments.simulate), label=arguments.label,
                           pause=not arguments.no_pause)
        finally:
            client.close()
    write_json(arguments.output, record)
    print(json.dumps({"verdict": record["verdict"], "wall_seconds": record["wall_seconds"],
                      "version": record["version"], "output": str(arguments.output)}, ensure_ascii=False, indent=2))
    return 0 if record["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
