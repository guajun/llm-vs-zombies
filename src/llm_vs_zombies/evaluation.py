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
import time
import uuid
from typing import Any

from .records import finish, read_json, sha256, write_json
from .initialization import apply_recipe, clock_anchor
from . import sound_effects
from .app_update_anchor import target_from_recipe
from .window_observer import LaunchWindowMonitor, launch_window_evidence, window_probe
from .evaluation_support import (BoundaryBudget, BoundaryStop, ReplayBoundaryClient,
    cleanup_passed, error_detail, finalize_run, host_sources)

SCHEMA = "lvz.evaluation.v1"
PLAN_SCHEMA = "lvz.evaluation-plan.v1"
LIVE_GATES = ("private_launch", "runtime_windows", "session_cleanup", "archive_integrity", "host_identity", "resource_limits",
              "scenario", "fixed_rng", "initial_state", "single_step",
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
    cold_wall_budget_seconds: float = 86400.0
    min_free_bytes: int = 2 * 1024**3
    packaging_reserve_bytes: int = 1024**3
    disk_check_ticks: int = 500
    pause_points: tuple = ((1000, 1.0), (2500, 5.0))
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
        for name, lower, upper in (("pause_seconds", 0.05, 10), ("timeout_seconds", 1, 600),
                                  ("wall_budget_seconds", 1, 86400), ("cold_wall_budget_seconds", 1, 86400)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not lower <= value <= upper:
                raise ValueError(f"{name} must be in {lower}..{upper}")
        for name, maximum in (("min_free_bytes", 2**63-1), ("packaging_reserve_bytes", 2**63-1), ("disk_check_ticks", 100000)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be an integer in 1..{maximum}")
        if not isinstance(self.pause_points, (tuple, list)):
            raise ValueError("pause_points must be an ordered sequence of [tick, seconds]")
        last = 0
        for point in self.pause_points:
            if (not isinstance(point, (tuple, list)) or len(point) != 2 or type(point[0]) is not int
                    or not last < point[0] <= 1000000 or isinstance(point[1], bool)
                    or not isinstance(point[1], (int, float)) or not 0.05 <= point[1] <= 10):
                raise ValueError("pause_points require increasing positive ticks and durations in .05..10")
            last = point[0]
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




def private_launch_passed(state: dict) -> bool | None:
    if state.get("isolation_ready") is not True:
        return False
    status = (state.get("evaluation_windows") or {}).get("foreground_check_status")
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
def live_session(root: Path, name: str, plan: Plan, seed: int = 0, *, lifecycle: dict | None = None):
    from .cli import create_run
    from .launcher import start, stop
    from .client import connect
    from .session import SessionTrace
    free = shutil.disk_usage(root).free
    if free < plan.min_free_bytes:
        raise BoundaryStop("disk_reserve_stop", {"phase": "before_launch", "free_bytes": free,
                                                 "min_free_bytes": plan.min_free_bytes})
    run = create_run(root, root / "experiments/configs/liangyi.json", name)
    if lifecycle is not None:
        lifecycle["created_run"] = str(run)
    state, client, trace, runtime_monitor = None, None, None, None
    cleanup, primary, provenance = {}, None, None

    def retain(stage, operation):
        try:
            return operation()
        except Exception as error:
            cleanup[stage + "_error"] = error_detail(error)
            return None

    try:
        trace = SessionTrace(run / "decisions/evaluation.jsonl")
        provenance = host_sources(run)
        write_json(run / "inputs/evaluation-host.json", provenance)
        if not provenance["matches_archive"]:
            raise RuntimeError("actual imported host sources differ from the run implementation archive")
        monitor = LaunchWindowMonitor(evidence_directory=run / "decisions/window-observer-launch")
        try:
            with monitor:
                state = start(root, run, timeout=plan.timeout_seconds, seed=seed, defer_preparation=True,
                              audio_mode=plan.audio_mode)
                monitor.bind_pid(state["pid"])
        finally:
            windows = retain("launch_observer", lambda: monitor.result(state["pid"] if state is not None else None))
            retain("launch_window_write", lambda: write_json(run / "evaluation-windows.json", windows))
        state["evaluation_windows"] = windows
        runtime_monitor = LaunchWindowMonitor(pid=state["pid"],
            evidence_directory=run / "decisions/window-observer-runtime")
        runtime_monitor.__enter__()
        client = connect(pid=state["pid"], trace=trace, timeout=plan.timeout_seconds)
        client._initialization_run = run
        client._initialization_scenario_verified = state.get("scenario_verified") is True
        yield run, state, client, trace
    except BaseException as error:
        primary = error_detail(error, prefer_report=True)
        raise
    finally:
        if primary is not None:
            retain("primary_write", lambda: write_json(run / "evaluation-error.json", primary))
        if client is not None:
            try:
                observation = client.observe()
                closed = client.request("stop_recording", expect=observation["version"])
                if closed.get("closed") is not True:
                    raise RuntimeError("stop_recording did not acknowledge closed=true")
                cleanup["recording_closed"] = True
            except Exception as error:
                cleanup["close_error"] = error_detail(error)
            finally:
                try:
                    client.close()
                    cleanup["client_closed"] = True
                except Exception as error:
                    cleanup["client_close_error"] = error_detail(error)
        # Target is still alive while the independent observer seals its final
        # sample. A window failure never interrupts replay's native tail checks.
        if runtime_monitor is not None:
            retain("runtime_observer_close", lambda: runtime_monitor.__exit__(None, None, None))
            windows = retain("runtime_observer", lambda: runtime_monitor.result(state["pid"]))
            state["evaluation_runtime_windows"] = windows
            retain("runtime_window_write", lambda: write_json(run / "evaluation-runtime-windows.json", windows))
        if trace is not None:
            try:
                trace.close()
                cleanup["trace_closed"] = True
            except Exception as error:
                cleanup["trace_close_error"] = error_detail(error)
        if state is not None:
            try:
                stop(run)
                cleanup["owned_process_stopped"] = True
            except Exception as error:
                cleanup["stop_error"] = error_detail(error)
        if provenance is not None:
            final_provenance = retain("host_identity", lambda: host_sources(run, provenance))
            if state is not None:
                state["evaluation_host"] = final_provenance
            retain("host_write", lambda: write_json(run / "evaluation-host-final.json", final_provenance))
        if state is not None:
            state["evaluation_cleanup"] = cleanup
        if lifecycle is not None:
            lifecycle["cleanup"] = cleanup
        # If this last write fails the caller retains the error from the shared
        # dictionary; the absent cleanup receipt prevents pack/seal.
        retain("cleanup_write", lambda: write_json(run / "evaluation-cleanup.json", cleanup))


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


class PauseSchedule:
    def __init__(self, client, run, points):
        self.client, self.run, self.points = client, run, list(points)
        self.configured = list(points)
        self.completed = []
        self.unexecuted = []
        self.final_observation = None

    def after_step(self, observation, *, skip_reason=None):
        changed = self.final_observation is None
        self.final_observation = observation
        while self.points and observation["version"]["tick"] >= self.points[0][0]:
            target, seconds = self.points.pop(0)
            changed = True
            if skip_reason is not None or observation.get("game_ui") != 3:
                self.unexecuted.append({"requested_tick": target, "wall_seconds": seconds,
                    "version": observation["version"], "reason": skip_reason or "no_completed_in_play_boundary"})
                continue
            try:
                probe = {"requested_tick": target, **_pause_probe(self.client, seconds)}
            except Exception as error:
                self.unexecuted.append({"requested_tick": target, "wall_seconds": seconds,
                    "version": observation["version"], "reason": "probe_failed", "error": error_detail(error)})
                try:
                    self.finish()
                except Exception:
                    pass  # Preserve the actual probe failure; outer cleanup retains its trace.
                raise
            self.completed.append(probe)
            self.client.trace.emit("evaluation_pause_probe", probe)
        if changed:
            self.finish()

    def finish(self):
        status = "fail" if any(p["reason"] == "probe_failed" for p in self.unexecuted) else "unverified" if self.unexecuted else "pass"
        report = {"configured": self.configured, "completed": self.completed, "unexecuted_reached": self.unexecuted,
            "not_reached": [{"requested_tick": tick, "wall_seconds": seconds, "status": "not_applicable_beyond_actual_endpoint"}
                            for tick, seconds in self.points],
            "final_version": self.final_observation["version"] if self.final_observation else None,
            "coverage_status": status}
        write_json(self.run / "pause-probes-during-play.json", report)
        return report


def _coverage_passed(report):
    return True if report.get("coverage_status") == "pass" else False if report.get("coverage_status") == "fail" else None


def _play_source(client, trace, strategy_path, plan, seed, initial_observation, first, run, budget):
    observation = first["observation"]
    maximum_wave = max(initial_observation["wave"], observation["wave"])
    strategy = ScriptStrategy(client, trace, strategy_path, plan.chunk_ticks)
    pauses = PauseSchedule(client, run, plan.pause_points)
    pauses.after_step(observation, skip_reason=budget.stop["reason"] if budget.stop else None)
    stalled = decisions = failed_actions = 0
    while True:
        if full_cycle_completed(initial_observation, observation, maximum_wave):
            outcome = "full_cycle_completed"
            break
        if observation["game_ui"] != 3:
            outcome = "game_over" if observation["game_ui"] == 4 else "terminal_before_complete"
            break
        if observation["version"]["tick"] >= plan.tick_budget:
            outcome = "tick_budget_exhausted"
            break
        if stalled >= 16:
            outcome = "strategy_no_progress"
            break
        try:
            budget.check(observation["version"])
        except BoundaryStop as stop:
            outcome = stop.reason
            break
        context = {"seed": seed, "decision_index": decisions,
                   "remaining_ticks": plan.tick_budget-observation["version"]["tick"],
                   "tier": plan.tier, "target": "complete_two_flags" if plan.tier == "strict" else "tick_budget"}
        decision = strategy.decide(observation, context)
        # Trusted policy/model execution may take time; do not send a mutation
        # after it has already used the source wall budget.
        try:
            budget.check(observation["version"])
        except BoundaryStop as stop:
            outcome = stop.reason
            break
        before_tick = observation["version"]["tick"]
        result = client.commit(decision["actions"], advance_ticks=decision["advance_ticks"])
        observation = result["observation"]
        maximum_wave = max(maximum_wave, observation.get("wave", 0))
        decisions += 1
        failed_actions += sum(item.get("ok") is False for item in result["action_results"])
        stalled = stalled + 1 if observation["version"]["tick"] == before_tick else 0
        budget.check(observation["version"], enforce=False)
        pauses.after_step(observation, skip_reason=budget.stop["reason"] if budget.stop else None)
        budget.check(observation["version"], enforce=False)
        if result["stop_reason"] not in {"budget_exhausted", "action_failed", "wave_changed", "scene_changed"}:
            outcome = result["stop_reason"]
            break
    return {"final_observation": observation, "maximum_wave": maximum_wave, "decisions": decisions,
            "failed_actions": failed_actions, "outcome": outcome,
            "full_cycle": full_cycle_completed(initial_observation, observation, maximum_wave),
            "resources": budget.report(), "pause_probes": pauses.finish()}


def _require_production_runtime(client):
    """A lifecycle test DLL can be recorded/replayed, never certify readiness."""
    game = client.hello_result.get("game", {})
    if "test_fixture" in game:
        raise ValueError("test_fixture runtime cannot certify production experiment readiness")


def _recovery_probe(root: Path, name: str, plan: Plan, seed: int, *, lifecycle=None) -> tuple[Path, dict]:
    """Disconnect after a fully written mutation, then resolve its original ID."""
    from .client import WindowsNamedPipeStream, connect
    with live_session(root, name, plan, seed, lifecycle=lifecycle) as (run, launcher, client, trace):
        _require_production_runtime(client)
        recipe = apply_recipe(client, seed)
        before = client.observe()
        budget = BoundaryBudget(root, plan, report_path=run / "evaluation-resources.json")
        budget.check(before["version"], force=True)
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
            budget.check(observation["version"])
            if observation["version"]["tick"] != before["version"]["tick"] + executed:
                raise RuntimeError("disconnect recovery has an unexpected tick count")
            if resumed.request("status", {"request_id": request_id})["response"] != response:
                raise RuntimeError("resolved disconnected request did not preserve its immutable result")
            failure = resumed.commit([{"op": "unsupported_probe", "row": 1, "col": 1}], advance_ticks=1)
            if failure["stop_reason"] != "action_failed" or failure["executed_ticks"] != 0:
                raise RuntimeError("invalid action did not stop without advancing")
            budget.check(failure["observation"]["version"])
            recovery = resumed.advance(1)
            if recovery["executed_ticks"] != 1:
                raise RuntimeError("runtime did not recover after the failed action")
            budget.check(recovery["observation"]["version"], enforce=False)
        result = {"recipe": recipe, "request_id": request_id, "accepted_status": accepted, "resolved_status": status,
                  "after_disconnect": observation, "invalid_action": failure, "recovery": recovery,
                  "resources": budget.report()}
        write_json(run / "recovery-probe.json", result)
    return run, result


def run_suite(root: Path, plan: Plan, output: Path, *, run_builds: bool = True) -> dict:
    """Run owned processes; each session is retained before evaluating its gates."""
    from .audit_compare import AuditLog, first_difference
    from .engine_replay import ReplaySession, capture_initial, identity_from_launcher, replay
    root, output = root.resolve(), output.resolve()
    plan.validate()
    if os.name != "nt":
        raise OSError("live suite requires Windows and local, hash-verified game files")
    if not output.is_relative_to(root / "experiments/runs") or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in output.name):
        raise ValueError("suite output must be a safe name under experiments/runs")
    for seed in plan.seeds:
        suffixes = [f"c{index}" for index in range(max(2, plan.cold_starts))] + ["recovery"]
        for suffix in suffixes:
            candidate = root / "experiments/runs" / f"{output.name}-s{seed}-{suffix}"
            if candidate.exists():
                raise FileExistsError(candidate)
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

    def add(name, passed, detail, *artifacts):
        item = evidence("unverified" if passed is None else "pass" if passed else "fail",
                        source="live_engine", detail=detail, artifacts=list(artifacts))
        if name != "shared_profile_unchanged":
            item["seed"] = seed
        gate_results[name].append(item)

    def read_optional(path):
        try:
            return read_json(path)
        except Exception:
            return None

    def retain_session(run, role, lifecycle, *, primary=None, package=False, outcome="failed"):
        item = {"run": str(run), "role": role, "primary_error": primary}
        trajectory = None
        if lifecycle.get("created_run") == str(run):
            try:
                final, trajectory = finalize_run(run, plan, outcome, package=package, primary_error=primary)
                item.update(final)
            except Exception as error:
                item.update(passed_recording=False, archive_sealed=False,
                            secondary_errors=[{"stage": "finalize", **error_detail(error)}])
        else:
            item.update(passed_recording=False, archive_sealed=False, secondary_errors=[],
                        not_created=True)
        runtime_cleanup = lifecycle.get("cleanup")
        if isinstance(runtime_cleanup, dict):
            item["cleanup_runtime_receipt"] = runtime_cleanup
            runtime_errors = {key: value for key, value in runtime_cleanup.items() if key.endswith("_error")}
            if runtime_errors:
                item.setdefault("secondary_errors", []).append({"stage": "runtime_cleanup", "errors": runtime_errors})
                item["cleanup_passed"] = item["passed_recording"] = False
        receipt_path = output / (run.name + "-retention.json")
        write_json(receipt_path, item)
        case["sessions"].append(item)
        launcher = read_optional(run / "launcher.json") or {}
        windows = read_optional(run / "evaluation-windows.json") or {}
        launcher["evaluation_windows"] = windows
        add("private_launch", private_launch_passed(launcher), {"role": role, "windows": windows},
            run / "evaluation-windows.json", *sorted((run / "decisions/window-observer-launch").glob("*")))
        runtime = read_optional(run / "evaluation-runtime-windows.json") or {}
        status = runtime.get("foreground_check_status") if runtime.get("schema") == "lvz.launch-windows.v3" else None
        passed = True if status == "pass" else False if status == "fail" else None
        add("runtime_windows", passed, {"role": role, "windows": runtime},
            run / "evaluation-runtime-windows.json", *sorted((run / "decisions/window-observer-runtime").glob("*")))
        add("session_cleanup", item.get("cleanup_passed") is True, {"role": role, "cleanup": item.get("cleanup")}, receipt_path)
        add("archive_integrity", item.get("archive_sealed") is True and not item.get("secondary_errors"),
            {"role": role, "retention": item}, receipt_path, run / "manifest.json")
        host = read_optional(run / "evaluation-host-final.json") or {}
        add("host_identity", host.get("matches_archive") is True and host.get("unchanged") is True,
            {"role": role, "host": host}, run / "evaluation-host-final.json", run / "inputs/implementation.zip")
        resource = read_optional(run / "evaluation-resources.json")
        add("resource_limits", isinstance(resource, dict) and resource.get("stop") is None,
            {"role": role, "resources": resource}, run / "evaluation-resources.json")
        item["infrastructure_passed"] = (item.get("passed_recording") is True and passed is True
            and private_launch_passed(launcher) is True and host.get("matches_archive") is True
            and host.get("unchanged") is True and isinstance(resource, dict) and resource.get("stop") is None)
        # The external receipt and sealed run stay immutable after their hashes
        # are attached to gates. The aggregate also appears in the seed case.
        return item, trajectory

    try:
        for seed in plan.seeds:
            case_started = time.monotonic()
            source_run = root / "experiments/runs" / f"{output.name}-s{seed}-c0"
            case = {"seed": seed, "source": "live_engine", "cold_starts": [], "sessions": [],
                    "status": "running", "expected_run": str(source_run)}
            report["cases"].append(case)
            primary = None
            source_completed = False
            source_lifecycle = {}
            try:
                try:
                    with live_session(root, source_run.name, plan, seed, lifecycle=source_lifecycle) as (run, launcher, client, trace):
                        _require_production_runtime(client)
                        case["run"] = str(run)
                        add("scenario", launcher.get("scenario_verified") is True, "actual Scene 3/layout/card verification", run / "observations/initial.json")
                        recipe = apply_recipe(client, seed)
                        write_json(run / "initialization-recipe.json", recipe)
                        add("fixed_rng", True, recipe, run / "initialization-recipe.json")
                        budget = BoundaryBudget(root, plan, report_path=run / "evaluation-resources.json")
                        budget.check(client.version, force=True)
                        pause = _pause_probe(client, plan.pause_seconds)
                        write_json(run / "pause-probe.json", pause)
                        add("pause_invariance", True, pause, run / "pause-probe.json")
                        initial = capture_initial(client, identity=identity_from_launcher(client.hello_result, launcher), initialization=recipe)
                        write_json(run / "replay-initial.json", initial)
                        initial_observation = initial["observation"]
                        budget.check(client.version)
                        first = client.advance(1)
                        budget.check(first["observation"]["version"], enforce=False)
                        single = first["executed_ticks"] == 1 and first["observation"]["game_clock"] == initial_observation["game_clock"] + 1
                        write_json(run / "single-step.json", first)
                        add("single_step", single, first, run / "single-step.json")
                        if not single:
                            raise RuntimeError("one requested tick did not execute exactly one game tick")
                        ending = _play_source(client, trace, strategy_path, plan, seed, initial_observation, first, run, budget)
                        case.update(ending)
                        write_json(run / "experiment-end.json", ending)
                        add("pause_invariance", _coverage_passed(ending["pause_probes"]), ending["pause_probes"], run / "pause-probes-during-play.json")
                        add("full_cycle", ending["full_cycle"], {"maximum_wave": ending["maximum_wave"],
                            "initial_rounds": initial_observation.get("completed_rounds"),
                            "final_rounds": ending["final_observation"].get("completed_rounds")}, run / "experiment-end.json")
                        source_completed = True
                except Exception as error:
                    primary = error_detail(error, prefer_report=True)
                    case["error"] = primary
                    if isinstance(error, BoundaryStop):
                        case["outcome"] = error.reason
                finally:
                    retained, trajectory = retain_session(source_run, "source", source_lifecycle, primary=primary,
                        package=source_run.exists(), outcome=case.get("outcome", "source_failed"))
                    add("recording", retained.get("passed_recording") is True and retained.get("trajectory_verified") is True,
                        retained, output / (source_run.name + "-retention.json"))
                source_passed = source_completed and retained["infrastructure_passed"] and trajectory is not None
                case["cold_starts"].append({"run": str(source_run), "kind": "source", "passed": source_passed})
                if not source_passed:
                    case["status"] = "failed" if source_run.exists() else "startup_failed"
                    case["replays_skipped"] = "source infrastructure or recording did not pass"
                    continue
                if plan.tier == "strict" and case.get("full_cycle") is not True:
                    case.update(status="incomplete", replays_skipped="strict source did not complete two flags")
                    continue
                anchor = trajectory.initial["initialization"]["clock_anchor"]
                all_cold = True
                for repeat in range(1, max(2, plan.cold_starts)):
                    cold_run = root / "experiments/runs" / f"{output.name}-s{seed}-c{repeat}"
                    destination = output / f"seed-{seed}-replay-{repeat}"
                    cold_error, replay_result, cold_budget = None, None, None
                    cold_lifecycle = {}

                    @contextmanager
                    def initializer(expected, destination):
                        nonlocal cold_budget
                        with live_session(root, cold_run.name, plan, seed, lifecycle=cold_lifecycle) as (replay_run, state, replay_client, replay_trace):
                            _require_production_runtime(replay_client)
                            add("scenario", state.get("scenario_verified") is True, {"repeat": repeat}, replay_run / "observations/initial.json")
                            apply_recipe(replay_client, seed, anchor,
                                         app_update_count=target_from_recipe(expected.initial["initialization"]))
                            actual_initial = replay_client.request("audit_snapshot")["state"]
                            difference = first_difference(expected.initial["state"], actual_initial)
                            write_json(replay_run / "initial-comparison.json", {"equal": difference is None, "difference": difference})
                            add("initial_state", difference is None, difference, replay_run / "initial-comparison.json")
                            cold_budget = BoundaryBudget(root, plan, cold=True, report_path=replay_run / "evaluation-resources.json")
                            cold_budget.check(replay_client.version, force=True)
                            probe = _pause_probe(replay_client, plan.pause_seconds)
                            write_json(replay_run / "pause-probe.json", probe)
                            add("pause_invariance", True, probe, replay_run / "pause-probe.json")
                            schedule = PauseSchedule(replay_client, replay_run, plan.pause_points)
                            wrapped = ReplayBoundaryClient(replay_client, cold_budget,
                                lambda obs: schedule.after_step(obs, skip_reason=cold_budget.stop["reason"] if cold_budget.stop else None))
                            yield ReplaySession(wrapped, identity_from_launcher(replay_client.hello_result, state), replay_run / "audit")
                            pause_coverage = schedule.finish()
                            add("pause_invariance", _coverage_passed(pause_coverage), pause_coverage, replay_run / "pause-probes-during-play.json")
                            # This records a final over-budget boundary without throwing
                            # before replay performs its authoritative close-health checks.
                            cold_budget.check(replay_client.version, enforce=False)

                    try:
                        replay_result = replay(trajectory, initializer, destination)
                        if cold_budget is not None:
                            # Include completed native tail verification in the
                            # cold wall bound, without interrupting that evidence.
                            cold_budget.check(cold_budget.last_version, enforce=False)
                    except Exception as error:
                        cold_error = error_detail(error, prefer_report=True)
                        case.setdefault("error", cold_error)
                    finally:
                        cold_retained, _ = retain_session(cold_run, "cold", cold_lifecycle, primary=cold_error,
                            outcome="engine_replay_completed" if replay_result and replay_result.get("equal") else "engine_replay_failed")
                    equal = replay_result is not None and replay_result.get("equal") is True
                    passed = equal and cold_retained["infrastructure_passed"] and cold_error is None
                    case["cold_starts"].append({"kind": "replay", "run": str(cold_run),
                        "report": str(destination / "replay-report.json"), "passed": passed, "error": cold_error})
                    add("engine_replay", equal, {"seed": seed, "repeat": repeat}, destination / "replay-report.json")
                    if not passed:
                        all_cold = False
                        case["replays_skipped"] = "stopped after first failed cold; existing attempts retained"
                        break
                if not all_cold:
                    case["status"] = "failed"
                    continue
                probe_run = root / "experiments/runs" / f"{output.name}-s{seed}-recovery"
                recovery_error, recovery = None, None
                recovery_lifecycle = {}
                try:
                    _, recovery = _recovery_probe(root, probe_run.name, plan, seed, lifecycle=recovery_lifecycle)
                    # The intentionally discarded response is not a replayable
                    # full client trace; validate native frames/sidecars/footer directly.
                    audit = AuditLog(probe_run / "audit", require_closed=True)
                    audit.verify_files()
                    write_json(probe_run / "recovery-audit-health.json", {"verified": True,
                        "frames": len(audit.frames), "engine_calls": audit.engine_call_health,
                        "sound_effects": audit.sound_effects_health, "draw": audit.draw_health})
                except Exception as error:
                    recovery_error = error_detail(error, prefer_report=True)
                    case.setdefault("error", recovery_error)
                finally:
                    probe_retained, _ = retain_session(probe_run, "recovery", recovery_lifecycle, primary=recovery_error,
                        outcome="recovery_probe_completed" if recovery_error is None else "recovery_probe_failed")
                recovered = recovery is not None and recovery_error is None and probe_retained["infrastructure_passed"]
                add("disconnect_recovery", recovered, "original accepted request ID resolved without reissuing",
                    probe_run / "recovery-probe.json", probe_run / "recovery-audit-health.json")
                add("failure_recovery", recovered, "invalid action advanced zero ticks; next valid step advanced one",
                    probe_run / "recovery-probe.json", probe_run / "recovery-audit-health.json")
                case["status"] = "completed" if recovered else "failed"
            except Exception as error:
                detail = error_detail(error, prefer_report=True)
                if "error" in case:
                    case.setdefault("secondary_errors", []).append(detail)
                else:
                    case["error"] = detail
                case["status"] = "failed"
            finally:
                case["wall_seconds"] = time.monotonic() - case_started
                case_file = output / f"seed-{seed}-case.json"
                write_json(case_file, case)
                success_count = sum(c["passed"] is True for c in case["cold_starts"])
                add("ten_cold_starts", success_count >= 10 and all(c["passed"] for c in case["cold_starts"]),
                    {"seed": seed, "requested": plan.cold_starts, "attempted": len(case["cold_starts"]),
                     "successful_cold_starts": success_count}, case_file)
    finally:
        try:
            after = profile_fingerprint()
            write_json(output / "shared-profile-after.json", after)
            add("shared_profile_unchanged", original_profile == after, "hash-only before/after user files and original registry",
                output / "shared-profile-before.json", output / "shared-profile-after.json")
        except Exception as error:
            report.setdefault("secondary_errors", []).append({"stage": "profile_after", **error_detail(error)})
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
        report["statistics"] = {"seed_cases": len(report["cases"]),
            "completed_cases": sum(c["status"] == "completed" for c in report["cases"]),
            "failed_cases": sum(c["status"] != "completed" for c in report["cases"]),
            "full_cycle_successes": sum(c.get("full_cycle") is True for c in report["cases"]),
            "cold_starts_attempted": sum(len(c["cold_starts"]) for c in report["cases"]),
            "cold_starts_verified": sum(s["passed"] is True for c in report["cases"] for s in c["cold_starts"])}
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
    sample.add_argument("--seeds", default="0,1,42", help="ordered comma-separated uint32 seeds")
    sample.add_argument("--tick-budget", type=int)
    sample.add_argument("--cold-starts", type=int)
    sample.add_argument("--timeout-seconds", type=float)
    sample.add_argument("--wall-budget-seconds", type=float)
    sample.add_argument("--cold-wall-budget-seconds", type=float, default=86400)
    sample.add_argument("--min-free-bytes", type=int, default=2*1024**3)
    sample.add_argument("--packaging-reserve-bytes", type=int, default=1024**3)
    sample.add_argument("--disk-check-ticks", type=int, default=500)
    sample.add_argument("--pause-points", type=json.loads, default=[[1000, 1.0], [2500, 5.0]],
                        help='JSON [[minimum_tick, wall_seconds], ...]; executed at completed boundaries')
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
            strict = args.tier == "strict"
            plan = Plan(tier=args.tier, seeds=tuple(int(seed) for seed in args.seeds.split(',')),
                        cold_starts=args.cold_starts if args.cold_starts is not None else 10 if strict else 1,
                        tick_budget=args.tick_budget if args.tick_budget is not None else 200000 if strict else 1000,
                        timeout_seconds=args.timeout_seconds if args.timeout_seconds is not None else 600 if strict else 90,
                        wall_budget_seconds=args.wall_budget_seconds if args.wall_budget_seconds is not None else 86400 if strict else 3600,
                        cold_wall_budget_seconds=args.cold_wall_budget_seconds,
                        min_free_bytes=args.min_free_bytes, packaging_reserve_bytes=args.packaging_reserve_bytes,
                        disk_check_ticks=args.disk_check_ticks, pause_points=args.pause_points,
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
        smoke_missing = set(report["readiness"]["unmet_gates"]) - {
            "strict_suite_not_requested", "at_least_ten_cold_starts_per_seed", "full_cycle", "ten_cold_starts"}
        # --skip-build is an explicit development option, never a strict pass.
        if args.skip_build:
            smoke_missing.discard("build_and_tests")
        return 0 if report["statistics"]["failed_cases"] == 0 and not smoke_missing else 2
    except Exception as error:
        parser.exit(1, f"evaluation: {type(error).__name__}: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
