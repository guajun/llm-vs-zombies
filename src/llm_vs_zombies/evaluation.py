"""Provider-neutral live experiment runner and evidence-based readiness gates.

No model provider or paid service is called by this module. A trusted local
Python strategy may supply decisions; its source, results and explicit model
exchanges are recorded. Offline tests never establish live readiness.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import threading
import time
import uuid
from typing import Any

from .records import finish, read_json, sha256, write_json
from .initialization import apply_recipe, clock_anchor
from . import sound_effects
from .app_update_anchor import target_from_recipe

SCHEMA = "lvz.evaluation.v1"
PLAN_SCHEMA = "lvz.evaluation-plan.v1"
LIVE_GATES = ("private_launch", "scenario", "fixed_rng", "initial_state", "single_step",
              "pause_invariance", "disconnect_recovery", "failure_recovery", "recording",
              "full_cycle", "engine_replay", "ten_cold_starts", "shared_profile_unchanged")


@dataclass(frozen=True)
class Plan:
    schema: str = PLAN_SCHEMA
    tier: str = "smoke"
    seeds: tuple[int, ...] = (0, 1, 42)
    cold_starts: int = 1
    tick_budget: int = 1000
    chunk_ticks: int = 100
    pause_seconds: float = 1.0
    timeout_seconds: float = 90.0
    wall_budget_seconds: float = 3600.0
    strategy: str | None = None
    audio_mode: str = "original"

    def validate(self) -> "Plan":
        sound_effects.configured(self.audio_mode)
        if self.schema != PLAN_SCHEMA or self.tier not in {"smoke", "strict"}:
            raise ValueError("unsupported evaluation schema or tier")
        if not isinstance(self.seeds, (tuple, list)) or not self.seeds or len(set(self.seeds)) != len(self.seeds):
            raise ValueError("seeds must be a nonempty set of explicitly ordered unique uint32 values")
        if any(type(seed) is not int or not 0 <= seed <= 0xFFFFFFFF for seed in self.seeds):
            raise ValueError("seeds must be uint32 integers")
        for name, maximum in (("cold_starts", 100), ("tick_budget", 1000000), ("chunk_ticks", 100000)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be an integer in 1..{maximum}")
        if self.chunk_ticks > self.tick_budget:
            raise ValueError("chunk_ticks exceeds tick_budget")
        for name, lower, upper in (("pause_seconds", 0.05, 10), ("timeout_seconds", 1, 600), ("wall_budget_seconds", 1, 86400)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not lower <= value <= upper:
                raise ValueError(f"{name} must be in {lower}..{upper}")
        if self.tier == "strict" and (self.cold_starts < 10 or not self.strategy):
            raise ValueError("strict requires at least 10 cold starts per seed and an explicit strategy file")
        return self

    @classmethod
    def load(cls, path: Path) -> "Plan":
        value = read_json(path)
        if "seeds" in value:
            value["seeds"] = tuple(value["seeds"])
        if value.get("strategy") and not Path(value["strategy"]).is_absolute():
            value["strategy"] = str((path.parent / value["strategy"]).resolve())
        return cls(**value).validate()


def evidence(status: str, *, source: str, detail: Any, artifacts: list[Path] = ()) -> dict:
    if status not in {"pass", "fail", "unverified"}:
        raise ValueError("invalid evidence status")
    return {"status": status, "source": source, "detail": detail,
            "artifacts": [{"path": str(path.resolve()), "sha256": sha256(path)} for path in artifacts if path.is_file()]}


def readiness(plan: Plan, checks: dict[str, dict]) -> dict:
    """Only real live evidence can satisfy a simulation gate.

    Readiness certifies this experiment suite and captured-state replay scope;
    it never certifies completeness of the original game's internal state.
    """
    plan.validate()
    missing = []
    for name in ("build_and_tests", *LIVE_GATES):
        check = checks.get(name, {})
        allowed = {"local_build", "ci_build"} if name == "build_and_tests" else {"live_engine"}
        intact = bool(check.get("artifacts"))
        for artifact in check.get("artifacts", []):
            try:
                intact = intact and sha256(Path(artifact["path"])) == artifact["sha256"]
            except (OSError, KeyError, TypeError):
                intact = False
        if check.get("status") != "pass" or check.get("source") not in allowed or not intact:
            missing.append(name)
    if plan.tier != "strict":
        missing.insert(0, "strict_suite_not_requested")
    if plan.cold_starts < 10:
        missing.append("at_least_ten_cold_starts_per_seed")
    return {"experiment_ready": not missing, "unmet_gates": missing,
            "strict_engine_determinism_proven": False,
            "scope": "tested game/runtime/scenario/seed set/trajectory; audit schema has documented uncovered fields"}


def full_cycle_completed(initial: dict, final: dict, maximum_wave: int) -> bool:
    before, after = initial.get("completed_rounds"), final.get("completed_rounds")
    return (type(before) is int and type(after) is int and after > before
            and maximum_wave >= 20 and final.get("scene") == 3)


def profile_fingerprint() -> dict:
    """Hash shared original state without copying personal save contents."""
    if os.name != "nt":
        raise OSError("live profile isolation checks require Windows")
    directory = Path(os.environ.get("ProgramData", "C:/ProgramData")) / "PopCap Games/PlantsVsZombies/userdata"
    files = {str(p.relative_to(directory)): sha256(p) for p in directory.rglob("*") if p.is_file()} if directory.exists() else None
    import winreg
    def tree(key):
        values, children, index = {}, {}, 0
        while True:
            try:
                name, value, kind = winreg.EnumValue(key, index)
                encoded = json.dumps([kind, value.hex() if isinstance(value, bytes) else value], ensure_ascii=False).encode()
                values[name] = hashlib.sha256(encoded).hexdigest()
                index += 1
            except OSError as error:
                if error.winerror != 259:
                    raise
                break
        index = 0
        while True:
            try:
                name = winreg.EnumKey(key, index)
                with winreg.OpenKey(key, name) as child:
                    children[name] = tree(child)
                index += 1
            except OSError as error:
                if error.winerror != 259:
                    raise
                break
        return {"values": values, "children": children}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\PopCap\PlantsVsZombies") as key:
            registry = tree(key)
    except FileNotFoundError:
        registry = None
    return {"userdata": files, "registry_hkcu": registry}


def window_probe(pid: int | None = None) -> dict:
    """Read foreground ownership and owned visibility; never activate or send input."""
    import ctypes
    from ctypes import wintypes
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetForegroundWindow.restype = wintypes.HWND
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    # A foreground switch or destroyed HWND can race the PID lookup. Keep an
    # unresolved sample rather than attributing it to either the user or game.
    foreground, foreground_pid, resolved = 0, None, False
    for _ in range(3):
        foreground = int(user.GetForegroundWindow() or 0)
        owner = wintypes.DWORD()
        thread_id = user.GetWindowThreadProcessId(foreground, ctypes.byref(owner)) if foreground else 0
        if foreground == int(user.GetForegroundWindow() or 0):
            resolved = not foreground or bool(thread_id and owner.value)
            foreground_pid = owner.value if foreground and resolved else None
            break
    windows = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    @callback_type
    def visit(window, _):
        owner = wintypes.DWORD()
        user.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value == pid:
            windows.append({"handle": window, "visible": bool(user.IsWindowVisible(window))})
        return True
    if pid is not None:
        user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        if not user.EnumWindows(visit, 0):
            raise ctypes.WinError(ctypes.get_last_error())
    return {"foreground": foreground, "foreground_pid": foreground_pid,
            "foreground_resolved": resolved, "owned_windows": windows}


class LaunchWindowMonitor:
    """Finite read-only sampling covering the complete blocking launcher call.

    PID is resolved after start returns, so foreground transitions are retained
    even while the helper has not yet written its process receipt.
    """
    interval_seconds = 0.025
    max_gap_seconds = 0.250

    def __init__(self):
        self.samples = []
        self.errors = []
        self.finished = threading.Event()
        self.thread = None

    def sample(self, pid=None):
        timestamp = time.monotonic()
        try:
            result = window_probe(pid)
        except Exception as error:
            message = f"{type(error).__name__}: {error}"
            self.errors.append(message)
            result = {"foreground": None, "foreground_pid": None,
                      "foreground_resolved": False, "owned_windows": [], "error": message}
        self.samples.append({"monotonic_seconds": timestamp, **result})
        return self.samples[-1]

    def _poll(self):
        while not self.finished.wait(self.interval_seconds):
            self.sample()

    def __enter__(self):
        self.sample()
        self.thread = threading.Thread(target=self._poll, name="lvz-foreground-observer", daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.finished.set()
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            self.errors.append("foreground observer did not stop within two seconds")

    def result(self, pid):
        # Called after stopping the worker; collect the final owned-window list.
        self.sample(pid)
        return launch_window_evidence(self.samples, pid, errors=self.errors,
                                      interval_seconds=self.interval_seconds, max_gap_seconds=self.max_gap_seconds)


def launch_window_evidence(samples: list[dict], pid: int | None, *, errors=(),
                           interval_seconds: float = 0.025, max_gap_seconds: float = 0.250) -> dict:
    """Classify sampled game ownership, never infer a cause from HWND inequality."""
    samples = sorted(samples, key=lambda sample: sample["monotonic_seconds"])
    before, after = (samples[0], samples[-1]) if samples else ({}, {})
    observed = [sample for sample in samples if pid is not None and sample.get("foreground_pid") == pid]
    unresolved = [index for index, sample in enumerate(samples) if sample.get("foreground_resolved") is not True]
    gaps = [right["monotonic_seconds"]-left["monotonic_seconds"] for left, right in zip(samples, samples[1:])]
    maximum_gap = max(gaps, default=0)
    owned = after.get("owned_windows", [])
    hidden = bool(owned) and not any(window["visible"] for window in owned)
    complete = (type(pid) is int and pid > 0 and len(samples) >= 2 and not errors and not unresolved
                and maximum_gap <= max_gap_seconds)
    # A sampled ownership match is positive evidence even if other samples are
    # missing. A negative conclusion requires complete bounded sampling.
    status = ("fail" if observed or any(window["visible"] for window in owned)
              else "pass" if complete and hidden else "unverified")
    return {"schema": "lvz.launch-windows.v2", "pid": pid, "before": before, "after": after,
            "hidden": hidden, "foreground_unchanged": before.get("foreground") == after.get("foreground"),
            "foreground_change_cause": "not_inferred",
            "game_foreground_observed": bool(observed), "game_foreground_samples": observed,
            "foreground_check_status": status, "samples": samples,
            "sampling": {"method": "read_only_foreground_hwnd_and_pid_polling",
                         "interval_seconds": interval_seconds, "maximum_gap_seconds": maximum_gap,
                         "allowed_maximum_gap_seconds": max_gap_seconds, "sample_count": len(samples),
                         "duration_seconds": after.get("monotonic_seconds", 0)-before.get("monotonic_seconds", 0),
                         "unresolved_sample_indices": unresolved, "errors": list(errors), "complete": complete,
                         "scope": "launch start through initialized return; final game-window visibility",
                         "limitation": "finite samples can miss activation between reads; not a continuous-focus guarantee"}}


def private_launch_passed(state: dict) -> bool | None:
    if state.get("isolation_ready") is not True:
        return False
    status = state.get("evaluation_windows", {}).get("foreground_check_status")
    return True if status == "pass" else False if status == "fail" else None


def _write_report(output: Path, report: dict, plan: Plan) -> None:
    report["readiness"] = readiness(plan, report["checks"])
    write_json(output / "evaluation.json", report)
    lines = ["# 实验验收报告", "", f"实验就绪：{'是' if report['readiness']['experiment_ready'] else '否'}",
             f"等级：{plan.tier}；固定种子：{list(plan.seeds)}；每种子要求冷启动：{plan.cold_starts}", "",
             "| 检查 | 状态 | 证据来源 |", "|---|---|---|"]
    for name in ("build_and_tests", *LIVE_GATES):
        check = report["checks"].get(name, {})
        lines.append(f"| {name} | {check.get('status', 'unverified')} | {check.get('source', 'none')} |")
    lines += ["", "此结论限于实际版本、场景、种子集与已捕获状态；不代表已覆盖原版全部内部状态。",
              "", "完整证据、失败原因和文件 SHA256 见同目录 evaluation.json。"]
    (output / "evaluation.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_checks(root: Path, output: Path) -> dict:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        return evidence("unverified", source="local_build", detail="PowerShell unavailable")
    commands = [[powershell, "-NoProfile", "-File", str(root / "tools/build-avz.ps1"), "-Jobs", "2"],
                [powershell, "-NoProfile", "-File", str(root / "launcher/build.ps1")],
                [shutil.which("python") or "python", "-m", "unittest", "discover", "-s", "tests", "-v"],
                ["ctest", "--test-dir", "build/cmake", "--output-on-failure"]]
    log = output / "build-and-tests.log"
    results = []
    env = dict(os.environ, PYTHONPATH=str(root / "src"))
    with log.open("w", encoding="utf-8") as stream:
        for command in commands:
            stream.write(json.dumps(command) + "\n")
            try:
                result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", timeout=900)
                stream.write(result.stdout + result.stderr + "\n")
                results.append({"command": command, "exit_code": result.returncode})
                if result.returncode:
                    break
            except Exception as error:
                stream.write(f"{type(error).__name__}: {error}\n")
                results.append({"command": command, "error": str(error)})
                break
    passed = len(results) == len(commands) and all(item.get("exit_code") == 0 for item in results)
    return evidence("pass" if passed else "fail", source="local_build", detail=results, artifacts=[log])


class ScriptStrategy:
    def __init__(self, client, trace, path: Path | None, chunk_ticks: int):
        from .repl import RecordedConsole
        self.console, self.chunk_ticks = RecordedConsole(client, trace), chunk_ticks
        self.path = path
        if path is not None:
            source = path.read_text(encoding="utf-8")
            trace.emit("strategy_source", {"path": str(path), "sha256": hashlib.sha256(source.encode()).hexdigest(), "source": source})
            self.console.locals["record_exchange"] = lambda provider, request, response: trace.emit(
                "model_exchange", {"provider": provider, "request": request, "response": response})
            if not self.console.execute_cell(source, str(path)) or not callable(self.console.locals.get("decide")):
                raise ValueError("strategy must execute successfully and define decide(observation, context)")

    def decide(self, observation: dict, context: dict) -> dict:
        if self.path is None:
            result = {"actions": [], "advance_ticks": min(self.chunk_ticks, context["remaining_ticks"])}
        else:
            self.console.locals.update(observation=observation, context=context)
            if not self.console.execute_cell("decision = decide(observation, context)", "<strategy-decision>"):
                raise RuntimeError("strategy decision raised an exception")
            result = self.console.locals["decision"]
        if not isinstance(result, dict) or set(result) != {"actions", "advance_ticks"} or not isinstance(result["actions"], list):
            raise ValueError("strategy decision must have actions:list and advance_ticks:int")
        ticks = result["advance_ticks"]
        if type(ticks) is not int or not 0 <= ticks <= min(100000, context["remaining_ticks"]):
            raise ValueError("strategy tick budget exceeds the remaining experiment budget")
        self.console.trace.emit("strategy_decision", {"context": context, "decision": result})
        return result


@contextmanager
def live_session(root: Path, name: str, plan: Plan, seed: int = 0):
    from .cli import create_run
    from .launcher import start, stop
    from .client import connect
    from .session import SessionTrace
    run = create_run(root, root / "experiments/configs/liangyi.json", name)
    state, client = None, None
    trace = SessionTrace(run / "decisions/evaluation.jsonl")
    try:
        monitor = LaunchWindowMonitor()
        try:
            with monitor:
                state = start(root, run, timeout=plan.timeout_seconds, seed=seed, defer_preparation=True,
                              audio_mode=plan.audio_mode)
        finally:
            windows = monitor.result(state["pid"] if state is not None else None)
            write_json(run / "evaluation-windows.json", windows)
        state["evaluation_windows"] = windows
        client = connect(pid=state["pid"], trace=trace, timeout=plan.timeout_seconds)
        client._initialization_run = run
        client._initialization_scenario_verified = state.get("scenario_verified") is True
        yield run, state, client, trace
    finally:
        cleanup = {}
        if client is not None:
            try:
                observation = client.observe()
                client.request("stop_recording", expect=observation["version"])
                cleanup["recording_closed"] = True
            except Exception as error:
                cleanup["close_error"] = str(error)
            finally:
                try:
                    client.close()
                except Exception as error:
                    cleanup["client_close_error"] = str(error)
        try:
            trace.close()
        except Exception as error:
            cleanup["trace_close_error"] = str(error)
        if state is not None:
            try:
                stop(run)
                cleanup["owned_process_stopped"] = True
            except Exception as error:
                cleanup["stop_error"] = str(error)
        write_json(run / "evaluation-cleanup.json", cleanup)


def _pause_probe(client, seconds: float) -> dict:
    if client.request("status").get("state") != "paused_at_boundary":
        raise RuntimeError("pause probe requires an already completed, paused boundary")
    before = client.request("audit_snapshot")
    observation = client.observe()
    time.sleep(seconds)
    after = client.request("audit_snapshot")
    if before != after or observation != client.observe():
        raise RuntimeError("paused wall time changed the captured simulation state")
    return {"wall_seconds": seconds, "version": observation["version"],
            "state_sha256": hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest()}


def _recovery_probe(root: Path, name: str, plan: Plan, seed: int) -> tuple[Path, dict]:
    """Disconnect after a fully written mutation, then resolve its original ID."""
    from .client import WindowsNamedPipeStream, connect
    with live_session(root, name, plan, seed) as (run, launcher, client, trace):
        recipe = apply_recipe(client, seed)
        before = client.observe()
        request_id = f"disconnect-{uuid.uuid4().hex}"
        request = {"protocol": 1, "request_id": request_id, "method": "advance",
                   "expect": before["version"], "params": {"max_ticks": 100}}
        raw = json.dumps(request, separators=(",", ":")).encode()
        frame = struct.pack("<I", len(raw)) + raw
        stream = WindowsNamedPipeStream(launcher["endpoint"], timeout=plan.timeout_seconds)
        try:
            offset = 0
            while offset < len(frame):
                count = stream.write(frame[offset:], plan.timeout_seconds)
                if count <= 0:
                    raise ConnectionError("disconnect probe failed to send full request")
                offset += count
            # A completed OS write is not evidence that the game thread has
            # accepted the request. Establish that through another connection
            # before deliberately discarding the original response.
            deadline = time.monotonic() + plan.timeout_seconds
            while True:
                accepted = client.request("status", {"request_id": request_id})
                if accepted.get("request_state") in {"pending", "completed"}:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("disconnect probe request was not accepted")
                time.sleep(0.02)
        finally:
            stream.close()
        trace.emit("intentional_disconnect", {"request": request, "response_received": False})
        deadline = time.monotonic() + plan.timeout_seconds
        with connect(pid=launcher["pid"], trace=trace, timeout=plan.timeout_seconds) as resumed:
            while True:
                status = resumed.request("status", {"request_id": request_id})
                if status.get("request_state") == "completed":
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("disconnected request did not resolve; no automatic retry was issued")
                time.sleep(0.02)
            response = status["response"]
            executed = response.get("result", {}).get("executed_ticks")
            reason = response.get("result", {}).get("stop_reason")
            if (response.get("ok") is not True or type(executed) is not int or not 0 <= executed <= 100
                    or reason not in {"client_disconnected", "budget_exhausted"}
                    or reason == "budget_exhausted" and executed != 100):
                raise RuntimeError("disconnected advancement did not resolve its actual bounded execution")
            observation = resumed.observe()
            if observation["version"]["tick"] != before["version"]["tick"] + executed:
                raise RuntimeError("disconnect recovery has an unexpected tick count")
            if resumed.request("status", {"request_id": request_id})["response"] != response:
                raise RuntimeError("resolved disconnected request did not preserve its immutable result")
            failure = resumed.commit([{"op": "unsupported_probe", "row": 1, "col": 1}], advance_ticks=1)
            if failure["stop_reason"] != "action_failed" or failure["executed_ticks"] != 0:
                raise RuntimeError("invalid action did not stop without advancing")
            recovery = resumed.advance(1)
            if recovery["executed_ticks"] != 1:
                raise RuntimeError("runtime did not recover after the failed action")
        result = {"recipe": recipe, "request_id": request_id, "accepted_status": accepted, "resolved_status": status,
                  "after_disconnect": observation, "invalid_action": failure, "recovery": recovery}
        write_json(run / "recovery-probe.json", result)
    finish(run, "recovery_probe_completed")
    return run, result


def run_suite(root: Path, plan: Plan, output: Path, *, run_builds: bool = True) -> dict:
    """Run real owned processes. Unit tests should exercise pure gates instead."""
    from .audit_compare import first_difference
    from .engine_replay import (ReplaySession, build_trajectory, capture_initial,
                                identity_from_launcher, replay)
    root, output = root.resolve(), output.resolve()
    plan.validate()
    if os.name != "nt":
        raise OSError("live suite requires Windows and local, hash-verified game files")
    if not output.is_relative_to(root / "experiments/runs") or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in output.name):
        raise ValueError("suite output must be a safe name under experiments/runs")
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "plan.json", asdict(plan))
    strategy_path = Path(plan.strategy).resolve() if plan.strategy else None
    if strategy_path:
        shutil.copy2(strategy_path, output / "strategy.py")
        strategy_path = output / "strategy.py"
    report = {"schema": SCHEMA, "created_at": datetime.now(timezone.utc).isoformat(),
              "source": "live_engine", "plan": asdict(plan), "cases": [], "checks": {}}
    checks = report["checks"]
    checks["build_and_tests"] = build_checks(root, output) if run_builds else evidence("unverified", source="none", detail="build checks explicitly skipped")
    if checks["build_and_tests"]["status"] == "fail":
        report["statistics"] = {"seed_cases": 0, "completed_cases": 0, "failed_cases": len(plan.seeds)}
        report["failure"] = "build or tests failed; no game process was started"
        _write_report(output, report, plan)
        return report
    original_profile = profile_fingerprint()
    write_json(output / "shared-profile-before.json", original_profile)
    started = time.monotonic()
    gate_results = {name: [] for name in LIVE_GATES}

    def add(name, passed, detail, artifact):
        item = evidence("unverified" if passed is None else "pass" if passed else "fail",
                        source="live_engine", detail=detail, artifacts=[artifact])
        if name != "shared_profile_unchanged":
            item["seed"] = seed
        gate_results[name].append(item)

    try:
        for seed in plan.seeds:
            case_started = time.monotonic()
            case = {"seed": seed, "source": "live_engine", "cold_starts": [], "status": "running",
                    "expected_run": str(root / "experiments/runs" / f"{output.name}-s{seed}-c0")}
            report["cases"].append(case)
            run = None
            try:
                with live_session(root, f"{output.name}-s{seed}-c0", plan, seed) as (run, launcher, client, trace):
                    artifact = run / "observations/initial.json"
                    windows = launcher["evaluation_windows"]
                    add("private_launch", private_launch_passed(launcher),
                        {"isolation_ready": launcher.get("isolation_ready"), "windows": windows}, run / "evaluation-windows.json")
                    add("scenario", launcher.get("scenario_verified") is True, "actual Scene 3/layout/card verification", artifact)
                    recipe = apply_recipe(client, seed)
                    write_json(run / "initialization-recipe.json", recipe)
                    add("fixed_rng", True, recipe, run / "initialization-recipe.json")
                    pause = _pause_probe(client, plan.pause_seconds)
                    write_json(run / "pause-probe.json", pause)
                    add("pause_invariance", True, pause, run / "pause-probe.json")
                    initial = capture_initial(client, identity=identity_from_launcher(client.hello_result, launcher), initialization=recipe)
                    write_json(run / "replay-initial.json", initial)
                    initial_observation = initial["observation"]
                    first = client.advance(1)
                    single = first["executed_ticks"] == 1 and first["observation"]["game_clock"] == initial_observation["game_clock"] + 1
                    write_json(run / "single-step.json", first)
                    add("single_step", single, first, run / "single-step.json")
                    if not single:
                        raise RuntimeError("one requested tick did not execute exactly one game tick")
                    observation = first["observation"]
                    maximum_wave = max(initial_observation["wave"], observation["wave"])
                    strategy = ScriptStrategy(client, trace, strategy_path, plan.chunk_ticks)
                    stalled, decisions = 0, 0
                    deadline = time.monotonic() + plan.wall_budget_seconds
                    while observation["version"]["tick"] < plan.tick_budget and not full_cycle_completed(initial_observation, observation, maximum_wave):
                        if time.monotonic() >= deadline:
                            raise TimeoutError("strategy wall-time budget exhausted")
                        if observation["game_ui"] != 3:
                            break
                        context = {"seed": seed, "decision_index": decisions, "remaining_ticks": plan.tick_budget-observation["version"]["tick"],
                                   "tier": plan.tier, "target": "complete_two_flags" if plan.tier == "strict" else "tick_budget"}
                        decision = strategy.decide(observation, context)
                        before_tick = observation["version"]["tick"]
                        result = client.commit(decision["actions"], advance_ticks=decision["advance_ticks"])
                        observation = result["observation"]
                        maximum_wave = max(maximum_wave, observation.get("wave", 0))
                        decisions += 1
                        stalled = stalled + 1 if observation["version"]["tick"] == before_tick else 0
                        if stalled >= 16:
                            raise RuntimeError("16 consecutive decisions made no simulation progress")
                        if result["stop_reason"] not in {"budget_exhausted", "action_failed", "wave_changed"}:
                            break
                    case.update(run=str(run), final_observation=observation, maximum_wave=maximum_wave, decisions=decisions,
                                full_cycle=full_cycle_completed(initial_observation, observation, maximum_wave))
                    case["outcome"] = ("full_cycle_completed" if case["full_cycle"] else "tick_budget_exhausted"
                                       if observation["version"]["tick"] >= plan.tick_budget else "terminal_before_complete")
                    if observation["game_ui"] == 3:
                        late_pause = _pause_probe(client, plan.pause_seconds)
                        write_json(run / "pause-probe-after-play.json", late_pause)
                        add("pause_invariance", True, late_pause, run / "pause-probe-after-play.json")
                    write_json(run / "experiment-end.json", case)
                    add("full_cycle", case["full_cycle"], {"maximum_wave": maximum_wave, "initial_rounds": initial_observation.get("completed_rounds"),
                        "final_rounds": observation.get("completed_rounds")}, run / "experiment-end.json")
                # live_session closed the recorder before packaging; startup-only
                # failures never get synthetic observations or finalize calls.
                trajectory = build_trajectory(run / "decisions/evaluation.jsonl", run / "audit", run / "trajectory")
                finish(run, case["outcome"])
                add("recording", True, {"trajectory_id": trajectory.manifest["trajectory_id"]}, run / "trajectory/trajectory.json")
                case["cold_starts"].append({"run": str(run), "kind": "source", "passed": True})
                anchor = trajectory.initial["initialization"]["clock_anchor"]
                # Smoke still performs one genuine cold-start replay, independent
                # of the stricter ten-start minimum.
                for repeat in range(1, max(2, plan.cold_starts)):
                    @contextmanager
                    def initializer(expected, destination, repeat=repeat):
                        with live_session(root, f"{output.name}-s{seed}-c{repeat}", plan, seed) as (replay_run, state, replay_client, replay_trace):
                            windows = state["evaluation_windows"]
                            add("private_launch", private_launch_passed(state),
                                {"repeat": repeat, "windows": windows}, replay_run / "evaluation-windows.json")
                            add("scenario", state.get("scenario_verified") is True, {"repeat": repeat}, replay_run / "observations/initial.json")
                            apply_recipe(replay_client, seed, anchor,
                                         app_update_count=target_from_recipe(expected.initial["initialization"]))
                            actual_initial = replay_client.request("audit_snapshot")["state"]
                            difference = first_difference(expected.initial["state"], actual_initial)
                            write_json(replay_run / "initial-comparison.json", {"equal": difference is None, "difference": difference})
                            add("initial_state", difference is None, difference, replay_run / "initial-comparison.json")
                            yield ReplaySession(replay_client, identity_from_launcher(replay_client.hello_result, state), replay_run / "audit")
                        finish(replay_run, "engine_replay_candidate_completed")
                    destination = output / f"seed-{seed}-replay-{repeat}"
                    replay_result = replay(trajectory, initializer, destination)
                    passed = replay_result.get("equal") is True
                    case["cold_starts"].append({"kind": "replay", "report": str(destination / "replay-report.json"), "passed": passed})
                    add("engine_replay", passed, {"seed": seed, "repeat": repeat}, destination / "replay-report.json")
                probe_run, recovery = _recovery_probe(root, f"{output.name}-s{seed}-recovery", plan, seed)
                add("disconnect_recovery", True, "full mutation sent, connection closed, original ID resolved without reissuing", probe_run / "recovery-probe.json")
                add("failure_recovery", True, "invalid action advanced zero ticks; next valid step advanced one", probe_run / "recovery-probe.json")
                case["status"] = "completed"
            except Exception as error:
                case.update(status="failed" if run is not None else "startup_failed", error={"type": type(error).__name__, "message": str(error)})
            finally:
                case["wall_seconds"] = time.monotonic() - case_started
                case_file = output / f"seed-{seed}-case.json"
                write_json(case_file, case)
                add("ten_cold_starts", len(case["cold_starts"]) >= 10 and all(c["passed"] for c in case["cold_starts"]),
                    {"seed": seed, "successful_cold_starts": len(case["cold_starts"])}, case_file)
                _write_report(output, report, plan)
    finally:
        after = profile_fingerprint()
        write_json(output / "shared-profile-after.json", after)
        add("shared_profile_unchanged", original_profile == after, "hash-only before/after user files and original registry",
            output / "shared-profile-after.json")
        for name, items in gate_results.items():
            if not items:
                checks[name] = evidence("unverified", source="live_engine", detail="stage was not reached")
            else:
                covered = name == "shared_profile_unchanged" or {item.get("seed") for item in items} == set(plan.seeds)
                status = ("fail" if any(i["status"] == "fail" for i in items)
                          else "pass" if covered and all(i["status"] == "pass" for i in items) else "unverified")
                checks[name] = {"status": status, "source": "live_engine", "detail": items,
                                "artifacts": [artifact for item in items for artifact in item["artifacts"]]}
        report["wall_seconds"] = time.monotonic() - started
        report["statistics"] = {"seed_cases": len(report["cases"]), "completed_cases": sum(c["status"] == "completed" for c in report["cases"]),
                                "failed_cases": sum(c["status"] != "completed" for c in report["cases"]),
                                "full_cycle_successes": sum(c.get("full_cycle") is True for c in report["cases"]),
                                "cold_starts_verified": sum(len(c["cold_starts"]) for c in report["cases"])}
        _write_report(output, report, plan)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    sample = commands.add_parser("plan", help="write a reviewable plan; does not launch a game")
    sample.add_argument("output", type=Path)
    sample.add_argument("--tier", choices=("smoke", "strict"), default="smoke")
    sample.add_argument("--strategy")
    sample.add_argument("--audio-mode", choices=("original", sound_effects.MODE), default="original")
    run = commands.add_parser("run", help="execute the plan against owned original-game processes")
    run.add_argument("plan", type=Path)
    run.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--skip-build", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            if args.output.exists():
                raise FileExistsError(args.output)
            plan = Plan(tier=args.tier, cold_starts=10 if args.tier == "strict" else 1,
                        tick_budget=200000 if args.tier == "strict" else 1000,
                        strategy=str(Path(args.strategy).resolve()) if args.strategy else None,
                        audio_mode=args.audio_mode).validate()
            write_json(args.output, asdict(plan))
            print(args.output)
            return 0
        report = run_suite(args.root, Plan.load(args.plan), args.output, run_builds=not args.skip_build)
        print(json.dumps(report["readiness"], ensure_ascii=False, indent=2))
        # Smoke success is not strict readiness. Its required stages must still
        # complete, including an actual cold-start replay and recovery probe.
        if report["plan"]["tier"] == "strict":
            return 0 if report["readiness"]["experiment_ready"] else 2
        return 0 if report["statistics"]["failed_cases"] == 0 else 2
    except Exception as error:
        parser.exit(1, f"evaluation: {type(error).__name__}: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
