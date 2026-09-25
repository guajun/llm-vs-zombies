"""Explicit lifecycle-recording experiment entry for issue #111.

One coherent identity flow:

* ``prepare`` validates the real evaluation plan (``Plan.load``), locks its
  exact bytes and normalized hash, records the mode/root and the child run
  names ``run_suite`` will create, and writes a sidecar **next to** (not
  inside) the suite directory. It never starts a game and never creates the
  suite output directory, because ``evaluation.run_suite`` requires
  ``--output`` to not exist.
* ``run`` re-verifies the locked plan bytes/root/mode, sets the explicit
  ``LVZ_LIFECYCLE_RECORDING`` environment and invokes the real
  ``evaluation.run_suite`` backend (the same function the public CLI calls).
  Build execution is taken from the prepared metadata; skipping builds is an
  explicit choice, never unconditional.
* ``check`` proves completion and mode correctness: the suite report must be
  complete, every expected child run must exist, every child audit must pass
  the strict closed ``AuditLog`` reader, and every child must declare the mode
  capability (enabled + valid lifecycle receipt, or explicitly disabled with
  no lifecycle files).
* ``seal`` re-checks that proof, compresses each child audit with the existing
  evidence codec and writes ``<suite>.lifecycle-seal.json`` binding the plan,
  suite report, build and per-child run/audit/receipt hashes.

The mode switch gates lifecycle **persistence only**; the spawn probe is
installed by the audit host in both modes. The off/on comparison therefore
measures the incremental recording adapter, not instrumented-vs-uninstrumented
gameplay.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from llm_vs_zombies import evidence_codec, lifecycle_events  # noqa: E402

MODE_SCHEMA = "lvz.lifecycle-experiment.v1"
REPORT_SCHEMA = "lvz.lifecycle-experiment-report.v1"
SEAL_SCHEMA = "lvz.lifecycle-seal.v1"
MODE_ENV = "LVZ_LIFECYCLE_RECORDING"
MODE_VALUES = {"off": "0", "on": "1"}
PROBES_ENV = "LVZ_LIFECYCLE_PROBES"
PROBES_VALUES = {"off": "0", "on": "1"}
_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


class ExperimentError(ValueError):
    """The experiment request or run directory is invalid."""


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def run_directory(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ExperimentError("run name must contain only letters, digits, '-' or '_'")
    return Path(root) / "experiments" / "runs" / name


def mode_path(root: Path, name: str) -> Path:
    return run_directory(root, name).with_suffix(".lifecycle.json")


def seal_path(root: Path, name: str) -> Path:
    return run_directory(root, name).with_suffix(".lifecycle-seal.json")


def expected_children(name: str, plan, single_cold: bool = False) -> list[str]:
    """The child directories ``evaluation.run_suite`` creates for this plan."""
    children = []
    for seed in plan.seeds:
        indexes = [0] if single_cold else list(range(max(2, plan.cold_starts)))
        for index in indexes:
            children.append(f"{name}-s{seed}-c{index}")
        if not single_cold:
            children.append(f"{name}-s{seed}-recovery")
    return children


def launch_command(root: Path, name: str, plan_path: Path, *, run_builds: bool,
                   single_cold: bool = False) -> list[str]:
    command = [sys.executable, "-m", "llm_vs_zombies.evaluation", "run", str(plan_path),
               "--root", str(root), "--output", str(run_directory(root, name))]
    if not run_builds:
        command.append("--skip-build")
    if single_cold:
        command.append("--single-cold")
    return command


def _load_plan(plan_path: Path):
    from llm_vs_zombies.evaluation import Plan
    try:
        return Plan.load(Path(plan_path))
    except Exception as exc:
        raise ExperimentError(f"evaluation plan is invalid: {exc}") from exc


def _plan_identity(plan) -> str:
    return sha256_bytes(canonical(asdict(plan)))


def plan_abspath(root: Path, metadata: dict) -> Path:
    path = Path(metadata["plan"]["path"])
    return path if path.is_absolute() else Path(root) / path


def prepare(root: Path, name: str, plan: Path, mode: str, *, run_builds: bool = True,
            probes: str | None = None, single_cold: bool = False,
            build_sha256: str | None = None) -> dict:
    """Lock plan/root/mode/probe/child identities without creating the suite output."""
    root = Path(root).resolve()
    if mode not in MODE_VALUES:
        raise ExperimentError("mode must be off or on")
    if probes is not None and probes not in PROBES_VALUES:
        raise ExperimentError("probes must be off, on or omitted")
    if probes == "on" and mode != "on":
        raise ExperimentError("the probe arm requires lifecycle recording to stay on")
    if build_sha256 is not None and not _valid_sha256(build_sha256):
        raise ExperimentError("expected recorder build must be a lowercase sha256")
    plan_path = Path(plan).resolve()
    parsed = _load_plan(plan_path)
    suite = run_directory(root, name)
    sidecar = mode_path(root, name)
    if suite.exists():
        raise ExperimentError(f"suite output already exists; create a fresh run: {suite}")
    for child in expected_children(name, parsed, single_cold):
        candidate = root / "experiments" / "runs" / child
        if candidate.exists():
            raise ExperimentError(f"expected child run already exists: {candidate}")
    if sidecar.exists():
        raise ExperimentError(f"run is already prepared: {sidecar}")
    metadata = {
        "schema": MODE_SCHEMA,
        "run": name,
        "root": str(root),
        "mode": mode,
        "env": {"name": MODE_ENV, "value": MODE_VALUES[mode]},
        "probes": None if probes is None else {"env": {"name": PROBES_ENV, "value": PROBES_VALUES[probes]},
                                               "mode": probes},
        "plan": {
            "path": str(plan_path.relative_to(root)) if plan_path.is_relative_to(root) else str(plan_path),
            "sha256": sha256_file(plan_path),
            "normalized_sha256": _plan_identity(parsed),
        },
        "run_builds": bool(run_builds),
        "single_cold": bool(single_cold),
        "expected_recorder_sha256": build_sha256,
        "suite": str(suite),
        "expected_children": expected_children(name, parsed, single_cold),
        "launch_command": launch_command(root, name, plan_path, run_builds=run_builds,
                                         single_cold=single_cold),
    }
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return metadata


def _read_prepared(root: Path, name: str) -> dict:
    root = Path(root).resolve()
    sidecar = mode_path(root, name)
    if not sidecar.is_file():
        raise ExperimentError(f"run is not prepared for lifecycle recording: {sidecar}")
    try:
        metadata = json.loads(sidecar.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExperimentError(f"lifecycle mode file unreadable: {exc}") from exc
    if metadata.get("schema") != MODE_SCHEMA or metadata.get("mode") not in MODE_VALUES:
        raise ExperimentError("unsupported lifecycle mode file")
    if metadata.get("env", {}).get("name") != MODE_ENV \
            or metadata["env"].get("value") != MODE_VALUES[metadata["mode"]]:
        raise ExperimentError("lifecycle mode file does not bind the explicit environment switch")
    probes = metadata.get("probes")
    if probes is not None:
        if not isinstance(probes, dict) or probes.get("mode") not in PROBES_VALUES:
            raise ExperimentError("lifecycle mode file has a malformed probe arm")
        if probes.get("env", {}).get("name") != PROBES_ENV \
                or probes["env"].get("value") != PROBES_VALUES[probes["mode"]]:
            raise ExperimentError("lifecycle mode file does not bind the probe environment switch")
        if probes["mode"] == "on" and metadata.get("mode") != "on":
            raise ExperimentError("a probe-on arm requires lifecycle recording on")
    if Path(metadata.get("root", "")).resolve() != root:
        raise ExperimentError("prepared root differs from the requested root")
    plan_path = Path(metadata["plan"]["path"])
    if not plan_path.is_absolute():
        plan_path = root / plan_path
    if not plan_path.is_file() or sha256_file(plan_path) != metadata["plan"]["sha256"]:
        raise ExperimentError("evaluation plan bytes changed since prepare")
    parsed = _load_plan(plan_path)
    if _plan_identity(parsed) != metadata["plan"]["normalized_sha256"]:
        raise ExperimentError("evaluation plan normalized identity changed since prepare")
    expected = expected_children(name, parsed)
    pin = metadata.get("expected_recorder_sha256")
    if pin is not None and not _valid_sha256(pin):
        raise ExperimentError("lifecycle mode file has a malformed recorder build pin")
    single_cold = metadata.get("single_cold", False)
    if type(single_cold) is not bool:
        raise ExperimentError("lifecycle mode file has a malformed single_cold flag")
    expected = expected_children(name, parsed, single_cold)
    if metadata.get("expected_children") != expected:
        raise ExperimentError("prepared child-run list does not match the validated plan")
    if Path(metadata.get("suite", "")).resolve() != run_directory(root, name).resolve():
        raise ExperimentError("prepared suite path does not match the run name")
    return metadata


def _running_game_processes() -> list[str]:
    if sys.platform != "win32":
        return []
    try:
        output = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                                timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return ["tasklist_unavailable"]
    return [line.split(",")[0].strip('"') for line in output.stdout.splitlines()
            if "plantsvszombies" in line.lower()]


def _stage_d_contract(root: Path, metadata: dict, plan) -> tuple[dict, list[str]]:
    """Cross-check the prepared plan against the frozen stage-D resource contract."""
    problems: list[str] = []
    contract: dict = {}
    candidate = root / "docs" / "issue111-阶段D计划.json"
    if not candidate.is_file():
        candidate = ROOT / "docs" / "issue111-阶段D计划.json"
    if candidate.is_file():
        try:
            contract = json.loads(candidate.read_bytes())
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            problems.append(f"stage-D plan is unreadable: {exc}")
            contract = {}
    based_on = contract.get("based_on") if isinstance(contract, dict) else None
    prepared_relative = metadata["plan"]["path"]
    if isinstance(based_on, dict):
        frozen_plan = str(based_on.get("plan", ""))
        if Path(prepared_relative).as_posix().endswith(frozen_plan) or prepared_relative == frozen_plan:
            if plan.tick_budget != based_on.get("tick_budget"):
                problems.append("evaluation plan tick_budget does not match the frozen stage-D cap")
            if plan.stop_when != based_on.get("stop_when"):
                problems.append("evaluation plan stop_when does not match the frozen stage-D endpoint")
            if plan.scenario != based_on.get("scenario"):
                problems.append("evaluation plan scenario does not match the frozen stage-D plan")
            limits = contract.get("resource_limits") or {}
            if plan.tick_budget > (limits.get("tick_cap") or plan.tick_budget):
                problems.append("evaluation plan tick_budget exceeds the stage-D tick cap")
            contract["applies"] = True
            contract["min_free_bytes"] = max(plan.min_free_bytes, limits.get("min_free_bytes") or 0)
    if not contract.get("applies"):
        contract["min_free_bytes"] = plan.min_free_bytes
    return contract, problems


def run_experiment(root: Path, name: str, *, suite_runner=None,
                   disk_free: int | None = None, game_processes: list[str] | None = None) -> dict:
    """Invoke the real evaluation suite with the explicit mode environment.

    The frozen plan/resource contract is enforced here, not left to an optional
    doctor command: plan identities, stage-D tick cap/reserve, disk reserve and
    a single-game-process rule are checked before the backend starts.
    """
    root = Path(root).resolve()
    metadata = _read_prepared(root, name)
    suite = Path(metadata["suite"])
    if suite.exists():
        raise ExperimentError(f"suite output already exists; create a fresh run: {suite}")
    for child in metadata["expected_children"]:
        candidate = root / "experiments" / "runs" / child
        if candidate.exists():
            raise ExperimentError(f"expected child run already exists: {candidate}")
    plan = _load_plan(plan_abspath(root, metadata))
    contract, problems = _stage_d_contract(root, metadata, plan)
    free = shutil.disk_usage(root).free if disk_free is None else disk_free
    if free < contract.get("min_free_bytes", 0):
        problems.append(f"disk free {free} is below the required reserve {contract.get('min_free_bytes')}")
    processes = _running_game_processes() if game_processes is None else game_processes
    if processes:
        problems.append(f"a game process is already running: {processes}")
    if problems:
        raise ExperimentError("resource/contract gate failed: " + "; ".join(problems))
    from llm_vs_zombies import evaluation
    previous = os.environ.get(MODE_ENV)
    os.environ[MODE_ENV] = metadata["env"]["value"]
    probes = metadata.get("probes")
    previous_probes = os.environ.get(PROBES_ENV)
    if probes is not None:
        os.environ[PROBES_ENV] = probes["env"]["value"]
    try:
        runner = suite_runner
        if runner is None:
            def runner():
                return evaluation.run_suite(root, plan, suite, run_builds=metadata["run_builds"],
                                            single_cold=metadata.get("single_cold", False))
        return runner()
    finally:
        if previous is None:
            os.environ.pop(MODE_ENV, None)
        else:
            os.environ[MODE_ENV] = previous
        if probes is not None:
            if previous_probes is None:
                os.environ.pop(PROBES_ENV, None)
            else:
                os.environ[PROBES_ENV] = previous_probes


def _valid_sha256(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _child_facts(root: Path, name: str, mode: str, child: str,
                 probe_mode: str | None = None,
                 expected_build: str | None = None) -> tuple[dict | None, list[str]]:
    from llm_vs_zombies.audit_compare import AuditLog, EvidenceError
    directory = root / "experiments" / "runs" / child
    problems: list[str] = []
    facts = {"child": child, "audit": {}, "run_manifest_sha256": None, "build": None}
    if not directory.is_dir():
        return None, [f"child run directory is missing: {directory}"]
    run_manifest = directory / "manifest.json"
    if not run_manifest.is_file():
        return facts, [f"{child}: run manifest.json is missing"]
    facts["run_manifest_sha256"] = sha256_file(run_manifest)
    try:
        manifest = json.loads(run_manifest.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return facts, [f"{child}: run manifest is unreadable: {exc}"]
    if not isinstance(manifest, dict):
        return facts, [f"{child}: run manifest is not an object"]
    if manifest.get("run_id") != child:
        problems.append(f"{child}: run manifest identity does not match its directory")
    implementation = manifest.get("implementation")
    build = implementation.get("recorder_sha256") if isinstance(implementation, dict) else None
    if not _valid_sha256(build):
        problems.append(f"{child}: run manifest has no well-formed recorder_sha256")
    else:
        facts["build"] = build
        if expected_build is not None and build != expected_build:
            problems.append(f"{child}: recorder build does not match the pinned build")
    audit = directory / "audit"
    if not (audit / "manifest.json").is_file():
        return facts, problems + [f"{child}: audit/manifest.json is missing"]
    try:
        strict = AuditLog(audit, require_closed=True)
        strict.verify_files()
    except (EvidenceError, OSError, UnicodeError, ValueError) as exc:
        return facts, problems + [f"{child}: strict audit verification failed: {exc}"]
    facts["audit"] = {"manifest_sha256": sha256_file(audit / "manifest.json"),
                      "frames": len(strict.frames),
                      "target": strict.manifest.get("target")}
    lifecycle_mode = lifecycle_events.mode(strict.manifest)
    if probe_mode is not None:
        probe_block = strict.manifest.get("lifecycle_probes")
        if not isinstance(probe_block, dict):
            problems.append(f"{child}: prepared probe arm has no lifecycle_probes capability")
        else:
            expected_enabled = probe_mode == "on"
            if probe_block.get("enabled") is not expected_enabled:
                problems.append(f"{child}: lifecycle_probes.enabled does not match the prepared probe arm")
            elif expected_enabled:
                if probe_block.get("healthy") is not True:
                    problems.append(f"{child}: lifecycle_probes capability is not healthy")
                if probe_block.get("pending_candidate") is not False:
                    problems.append(f"{child}: lifecycle_probes still has a pending candidate")
                if probe_block.get("installed") is not False:
                    problems.append(f"{child}: lifecycle_probes was not cleanly unloaded")
    if mode == "on":
        if lifecycle_mode != "enabled":
            problems.append(f"{child}: expected lifecycle capability enabled, found {lifecycle_mode}")
        else:
            declared = strict.manifest.get(lifecycle_events.CAPABILITY_KEY) or {}
            declared_build = (declared.get("build") or {}).get("sha256") if isinstance(declared, dict) else None
            if not _valid_sha256(declared_build):
                problems.append(f"{child}: capability has no well-formed build identity")
            elif facts["build"] is not None and declared_build != facts["build"]:
                problems.append(f"{child}: capability build does not match the run manifest recorder_sha256")
            try:
                report = lifecycle_events.validate(audit, manifest=strict.manifest, require_close=True)
            except lifecycle_events.LifecycleError as exc:
                problems.append(f"{child}: lifecycle evidence unreadable: {exc}")
            else:
                if report["status"] != "valid":
                    problems.append(f"{child}: lifecycle evidence invalid: " + "; ".join(report["problems"]))
                facts["lifecycle"] = {"status": report["status"],
                                      "records": report["records"]["count"],
                                      "session_id": report["capability"]["session_id"]}
    else:
        if lifecycle_mode != "disabled":
            problems.append(f"{child}: expected explicitly disabled lifecycle capability, found {lifecycle_mode}")
        for evidence in (lifecycle_events.EVENTS_FILE, lifecycle_events.RECEIPT_FILE):
            if (audit / evidence).exists() or (audit / (evidence + ".gz")).exists():
                problems.append(f"{child}: disabled mode contains lifecycle evidence {evidence}")
    return facts, problems


def check(root: Path, name: str) -> dict:
    """Prove suite completion, mode correctness and strict child audit health."""
    root = Path(root).resolve()
    problems: list[str] = []
    report = {"schema": REPORT_SCHEMA, "run": name, "mode": None, "plan": None,
              "suite": None, "children": [], "problems": problems}
    try:
        metadata = _read_prepared(root, name)
    except ExperimentError as exc:
        report["ok"] = False
        problems.append(str(exc))
        return report
    try:
        plan = _load_plan(plan_abspath(root, metadata))
    except ExperimentError as exc:
        report["ok"] = False
        problems.append(str(exc))
        return report
    report["mode"] = metadata["mode"]
    report["plan"] = metadata["plan"]
    suite = Path(metadata["suite"])
    report["suite"] = str(suite)
    if not suite.is_dir():
        report["ok"] = False
        problems.append(f"suite output was never created: {suite}")
        return report
    report_file = suite / "evaluation.json"
    if not report_file.is_file():
        report["ok"] = False
        problems.append("suite evaluation.json is missing")
        return report
    report["suite_report_sha256"] = sha256_file(report_file)
    try:
        suite_report = json.loads(report_file.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        report["ok"] = False
        problems.append(f"suite report is unreadable: {exc}")
        return report
    plan_path = plan_abspath(root, metadata)
    try:
        plan = _load_plan(plan_path)
    except ExperimentError as exc:
        report["ok"] = False
        problems.append(str(exc))
        return report
    if not isinstance(suite_report, dict) or suite_report.get("schema") != "lvz.evaluation.v1":
        problems.append("suite report schema is not lvz.evaluation.v1")
    else:
        if _plan_identity(_load_plan_from_report(suite_report)) != metadata["plan"]["normalized_sha256"]:
            problems.append("suite report plan does not match the prepared plan")
        statistics = suite_report.get("statistics") or {}
        if statistics.get("failed_cases") != 0:
            problems.append(f"suite has failed cases: {statistics.get('failed_cases')!r}")
        if statistics.get("completed_cases") != len(plan.seeds):
            problems.append(f"suite completed_cases={statistics.get('completed_cases')!r} "
                            f"does not match {len(plan.seeds)} seed(s)")
        cases = suite_report.get("cases")
        if not isinstance(cases, list) or {case.get("seed") for case in cases if isinstance(case, dict)} != set(plan.seeds):
            problems.append("suite cases do not cover exactly the prepared seeds")
        else:
            for case in cases:
                seed = case.get("seed")
                if case.get("status") != "completed":
                    problems.append(f"suite case seed {seed!r} status is {case.get('status')!r}")
                if case.get("error") is not None:
                    problems.append(f"suite case seed {seed!r} retains an error")
                attempts = case.get("cold_starts")
                if not isinstance(attempts, list) or not attempts:
                    problems.append(f"suite case seed {seed!r} has no cold-start evidence")
                elif not all(attempt.get("passed") is True for attempt in attempts):
                    problems.append(f"suite case seed {seed!r} has an unpassed cold-start attempt")
                sessions = case.get("sessions")
                if not isinstance(sessions, list) or not sessions:
                    problems.append(f"suite case seed {seed!r} has no retained child sessions")
    children: list[dict] = []
    builds: set[str] = set()
    for child in metadata["expected_children"]:
        facts, child_problems = _child_facts(root, name, metadata["mode"], child,
                                             (metadata.get("probes") or {}).get("mode"),
                                             metadata.get("expected_recorder_sha256"))
        problems.extend(child_problems)
        if facts is not None:
            if facts.get("build"):
                builds.add(facts["build"])
            children.append(facts)
    if len(builds) > 1:
        problems.append(f"child runs do not share one recorder build: {sorted(builds)}")
    if not builds:
        problems.append("no child run exposes a recorder build identity")
    report["children"] = children
    report["build"] = next(iter(builds)) if len(builds) == 1 else None
    report["ok"] = not problems
    return report


def _load_plan_from_report(suite_report: dict):
    from llm_vs_zombies.evaluation import Plan
    value = suite_report.get("plan")
    if not isinstance(value, dict):
        raise ExperimentError("suite report has no plan object")
    return Plan(**{key: (tuple(item) if key in ("seeds", "pause_points", "pause_perturbations",
                                               "b0_normalization") and isinstance(item, list) else item)
                   for key, item in value.items()}).validate()


def _seal_document(root: Path, name: str, *, allow_compress: bool = False) -> dict:
    report = seal_check(root, name)
    if not report.get("ok"):
        raise ExperimentError("run is not sealable: " + "; ".join(report["problems"]))
    metadata = _read_prepared(root, name)
    for child in metadata["expected_children"]:
        audit = root / "experiments" / "runs" / child / "audit"
        if not (audit / evidence_codec.RECEIPT).is_file():
            if not allow_compress:
                raise ExperimentError(f"sealed codec receipt is missing: {audit}")
            evidence_codec.compress_evidence(audit)
    # Re-run the proof against the compressed evidence (strict read + digest).
    sealed = check(root, name)
    if not sealed.get("ok"):
        raise ExperimentError("sealed evidence does not re-verify: " + "; ".join(sealed["problems"]))
    for child in sealed["children"]:
        audit = root / "experiments" / "runs" / child["child"] / "audit"
        receipt = audit / evidence_codec.RECEIPT
        child["codec_receipt_sha256"] = sha256_file(receipt) if receipt.is_file() else None
    document = {
        "schema": SEAL_SCHEMA,
        "run": name,
        "mode": sealed["mode"],
        "root": str(root),
        "plan": sealed["plan"],
        "suite_report_sha256": sealed["suite_report_sha256"],
        "build": sealed["build"],
        "children": sealed["children"],
    }
    document["seal_id"] = sha256_bytes(canonical(document))
    return document


def seal_check(root: Path, name: str) -> dict:
    """``check`` as used by the seal path (kept for call-site clarity)."""
    return check(root, name)


def seal(root: Path, name: str) -> dict:
    """Check, compress child audits and bind all identities in one seal.

    If a seal already exists it must reproduce the current evidence exactly;
    the stored seal is never silently replaced by a different document.
    """
    root = Path(root).resolve()
    target = seal_path(root, name)
    document = _seal_document(root, name, allow_compress=not target.exists())
    if target.is_file():
        try:
            existing = json.loads(target.read_bytes())
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ExperimentError(f"existing seal is unreadable: {exc}") from exc
        if existing != document:
            raise ExperimentError("existing seal does not match the current evidence; refusing to replace it")
        return existing
    target.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return document


def verify_seal(root: Path, name: str) -> dict:
    """Read-only: verify an existing seal against the current evidence."""
    root = Path(root).resolve()
    target = seal_path(root, name)
    if not target.is_file():
        raise ExperimentError(f"seal is missing: {target}")
    try:
        existing = json.loads(target.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExperimentError(f"seal is unreadable: {exc}") from exc
    document = _seal_document(root, name)
    if existing != document:
        raise ExperimentError("existing seal does not match the current evidence")
    return {"schema": SEAL_SCHEMA, "run": name, "ok": True, "seal_id": existing["seal_id"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Explicit #111 lifecycle-recording experiment entry")
    parser.add_argument("--root", type=Path, default=ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare", help="lock plan/root/mode and child identities (no launch)")
    prepare_parser.add_argument("--name", required=True)
    prepare_parser.add_argument("--plan", type=Path, required=True)
    prepare_parser.add_argument("--mode", choices=sorted(MODE_VALUES), required=True)
    prepare_parser.add_argument("--probes", choices=sorted(PROBES_VALUES), default=None,
                                help="explicit same-build probe arm (off/on); omitted keeps the old adapter-only arm")
    prepare_parser.add_argument("--single-cold", action="store_true",
                                help="one source cold start per arm; no extra replay or recovery child")
    prepare_parser.add_argument("--expected-recorder-sha256", default=None,
                                help="pin the recorder build every child audit must declare")
    prepare_parser.add_argument("--skip-build", action="store_true",
                                help="record that the existing build is reused; never implicit")
    run_parser = sub.add_parser("run", help="run the real evaluation suite with the mode environment")
    run_parser.add_argument("--name", required=True)
    check_parser = sub.add_parser("check", help="offline completion/mode/strict-audit check")
    check_parser.add_argument("--run", required=True)
    seal_parser = sub.add_parser("seal", help="check, compress child audits and write the seal")
    seal_parser.add_argument("--run", required=True)
    verify_parser = sub.add_parser("verify", help="read-only verification of an existing seal")
    verify_parser.add_argument("--run", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            report = prepare(args.root, args.name, args.plan, args.mode, run_builds=not args.skip_build,
                             probes=args.probes, single_cold=args.single_cold,
                             build_sha256=args.expected_recorder_sha256)
        elif args.command == "run":
            report = run_experiment(args.root, args.name)
        elif args.command == "check":
            report = check(args.root, args.run)
        elif args.command == "seal":
            report = seal(args.root, args.run)
        else:
            report = verify_seal(args.root, args.run)
    except ExperimentError as exc:
        print(json.dumps({"schema": REPORT_SCHEMA, "ok": False, "problems": [str(exc)]},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
