"""Run at most two independently verified cold replay hosts on Windows.

Only the scheduler starts workers. Worker entry waits for an owned Job arm
receipt before any game or observer launch. No messages or OS input are sent.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import uuid

from .records import read_json, sha256, validate, write_json
from .evaluation_support import error_detail as _error_detail
from .window_observer import ProcessIdentity

SCHEMA = "lvz.cold-worker.v1"
REQUIRED_GATES = {"private_launch", "runtime_windows", "session_cleanup", "archive_integrity",
                  "host_identity", "resource_limits", "scenario", "initial_state", "pause_invariance", "engine_replay"}


def error_detail(error, **kwargs):
    detail = _error_detail(error, **kwargs)
    if getattr(error, "__notes__", None):
        detail["exception_notes"] = list(error.__notes__)
    return detail


def require(value, message):
    if not value:
        raise RuntimeError(message)


def atomic(path, value):
    path = Path(path)
    pending = path.with_suffix(path.suffix + ".pending")
    write_json(pending, value)
    os.replace(pending, path)


def package_identity():
    package = Path(__file__).resolve().parent
    return {"directory": str(package), "files": {p.relative_to(package).as_posix(): sha256(p)
            for p in sorted(package.rglob("*.py"))}}


def prepare_cache():
    """One serial warm-up before any worker. Children can never compile."""
    from . import audit_compare as ac
    require(ac.hash_backend() == "native_c_fnv1a", "parallel replay requires the prewarmed native FNV helper")
    root = Path(ac.__file__).resolve().parents[2]
    key = hashlib.sha256((ac._FNV_SOURCE + platform.machine() + str(ctypes.sizeof(ctypes.c_void_p))).encode()).hexdigest()[:20]
    path = root / "work/audit-hash" / key / "fnv.dll"
    require(path.is_file(), "prewarmed FNV is not at its verified cache path")
    return {"path": str(path), "sha256": sha256(path), "backend": "native_c_fnv1a"}


def load_cache(expected):
    from . import audit_compare as ac
    actual = Path(ac.__file__).resolve().parents[2]
    key = hashlib.sha256((ac._FNV_SOURCE + platform.machine() + str(ctypes.sizeof(ctypes.c_void_p))).encode()).hexdigest()[:20]
    library = actual / "work/audit-hash" / key / "fnv.dll"
    require(str(library) == expected["path"] and library.is_file() and sha256(library) == expected["sha256"],
            "worker FNV cache differs; concurrent compilation forbidden")
    run = ac.subprocess.run
    def forbidden(*args, **kwargs):
        raise RuntimeError("worker attempted to compile FNV cache")
    ac.subprocess.run = forbidden
    try:
        require(ac.hash_backend() == expected["backend"] == "native_c_fnv1a", "worker cache load failed")
    finally:
        ac.subprocess.run = run


class OwnedJob:
    """An unnamed, non-inheritable job containing only one armed host tree."""
    def __init__(self):
        if os.name != 'nt':
            raise OSError('Windows Job Objects are required')
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                        ('flags', w.DWORD), ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t),
                        ('active_limit', w.DWORD), ('affinity', ctypes.c_size_t),
                        ('priority', w.DWORD), ('scheduling', w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', IO), ('process_memory', ctypes.c_size_t),
                        ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t),
                        ('peak_job', ctypes.c_size_t)]
        class Accounting(ctypes.Structure):
            _fields_ = [('user', ctypes.c_longlong), ('kernel', ctypes.c_longlong),
                        ('period_user', ctypes.c_longlong), ('period_kernel', ctypes.c_longlong),
                        ('faults', w.DWORD), ('total', w.DWORD), ('active', w.DWORD), ('terminated', w.DWORD)]
        self.Accounting = Accounting
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.api.CreateJobObjectW.restype = w.HANDLE
        self.api.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.api.QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p]
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            failure = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise failure

    def assign(self, process):
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def active(self):
        value = self.Accounting()
        if not self.api.QueryInformationJobObject(self.handle, 1, ctypes.byref(value), ctypes.sizeof(value), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(value.active)

    def wait_empty(self, seconds=5.):
        """Process exit can precede descendant rundown; still require true zero."""
        deadline = time.monotonic() + seconds
        active = self.active()
        while active and time.monotonic() < deadline:
            time.sleep(.025)
            active = self.active()
        return active

    def close(self):
        if self.handle:
            if not self.api.CloseHandle(self.handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self.handle = None


def binding(task):
    return {key: task[key] for key in ("token", "seed", "repeat", "run", "destination", "source", "source_manifest_sha256",
                                      "source_trajectory_id", "host", "cache")}


def failure_result(task, stage, error):
    from .evaluation import LIVE_GATES
    detail = {"stage": stage, **error_detail(error, prefer_report=True)}
    gates = {name: [] for name in LIVE_GATES}
    gates["engine_replay"].append({"seed": task["seed"], "source": "live_engine", "status": "fail",
                                    "detail": detail, "artifacts": []})
    return {"seed": task["seed"], "repeat": task["repeat"],
            "attempt": {"kind": "replay", "run": task["run"], "report": str(Path(task["destination"]) / "replay-report.json"),
                        "passed": False, "error": detail},
            "sessions": [], "gates": gates, "error": detail}


def verify_result(task, value, owner, exit_code, *, check_files=True):
    from .evaluation import LIVE_GATES
    require(value.get("schema") == SCHEMA and value.get("binding") == binding(task)
            and value.get("owner") == owner, "worker receipt identity differs")
    result = value["result"]
    require(result.get("seed") == task["seed"] and result.get("repeat") == task["repeat"]
            and result["attempt"].get("kind") == "replay" and result["attempt"].get("run") == task["run"]
            and result["attempt"].get("report") == str(Path(task["destination"]) / "replay-report.json"),
            "worker result belongs to another attempt")
    require(set(result["gates"]) == set(LIVE_GATES), "worker omitted gate collections")
    require(type(result["attempt"].get("passed")) is bool, "worker success is not explicit")
    for name, items in result["gates"].items():
        require(isinstance(items, list), "malformed worker gate")
        for item in items:
            require(item.get("seed") == task["seed"] and item.get("source") == "live_engine"
                    and item.get("status") in {"pass", "fail", "unverified"}, "worker gate identity/status differs")
            if check_files:
                for artifact in item["artifacts"]:
                    path = Path(artifact["path"]).resolve()
                    allowed = (path.is_relative_to(Path(task["run"]).resolve())
                               or path.is_relative_to(Path(task["destination"]).resolve())
                               or path == (Path(task["output"]) / (Path(task["run"]).name + "-retention.json")).resolve())
                    require(allowed and path.is_file() and sha256(path) == artifact["sha256"], "worker gate artifact changed/foreign")
    if result["attempt"]["passed"]:
        require(exit_code == 0 and result.get("error") is None and value.get("source_unchanged") is True
                and value.get("host_unchanged") is True, "successful worker lacks normal exit/immutable evidence")
        require(all(result["gates"][name] and all(item["status"] == "pass" and (not check_files or item["artifacts"])
                                               for item in result["gates"][name])
                    for name in REQUIRED_GATES), "successful worker omitted/failed a required per-cold gate")
        require(len(result["sessions"]) == 1 and result["sessions"][0].get("run") == task["run"]
                and result["sessions"][0].get("infrastructure_passed") is True
                and result["sessions"][0].get("archive_sealed") is True
                and result["sessions"][0].get("cleanup_passed") is True, "worker session retention incomplete")
        if check_files:
            source, run = Path(task["source"]), Path(task["run"])
            require(sha256(source / "manifest.json") == task["source_manifest_sha256"], "source manifest changed")
            require(validate(run)["status"] == "finalized"
                    and sha256(run / "manifest.json") == result["sessions"][0]["manifest_sha256"], "worker archive changed")
            official = read_json(Path(task["destination"]) / "replay-report.json")
            require(official.get("equal") is True and official.get("parent_trajectory_id") == task["source_trajectory_id"]
                    and official.get("requested_target_tick") is None, "worker did not fully replay its source")
            verify_intervals(result.get("rpc_intervals"), official)
    else:
        require(exit_code != 0, "failed worker incorrectly exited zero")
    return result


def verify_intervals(intervals, official):
    controlled = [request for request in official["requests"] if "executed_ticks" in request["result"]
                  and request.get("method") not in {"pause", "capture_frame"}]
    require(isinstance(intervals, list) and len(intervals) == len(controlled), "missing actual controlled-RPC timing evidence")
    previous = -1
    for interval, request in zip(intervals, controlled):
        result = request["result"]
        require(interval.get("request_id") == request["actual_request_id"]
                and interval.get("method") in {"advance", "commit"}
                and interval.get("version") == result["observation"]["version"]
                and interval.get("executed_ticks") == result["executed_ticks"]
                and interval.get("executed_engine_calls") == result.get("executed_engine_calls")
                and type(interval.get("start_ns")) is int and type(interval.get("end_ns")) is int
                and previous < interval["start_ns"] < interval["end_ns"], "timing row differs from verified original RPC")
        previous = interval["end_ns"]


def measured_overlaps(results):
    """Original positive-step RPC overlap, excluding all subsequent holds/IO."""
    output = []
    for index, left in enumerate(results):
        for right in results[index+1:]:
            # Failed receipts cannot certify measured engine execution.
            if not left["attempt"]["passed"] or not right["attempt"]["passed"]:
                continue
            a = [item for item in left.get("rpc_intervals", []) if (item.get("executed_ticks") or 0) > 0]
            b = [item for item in right.get("rpc_intervals", []) if (item.get("executed_ticks") or 0) > 0]
            i = j = 0
            while i < len(a) and j < len(b):
                start, end = max(a[i]["start_ns"], b[j]["start_ns"]), min(a[i]["end_ns"], b[j]["end_ns"])
                if end > start:
                    output.append({"left_repeat": left["repeat"], "right_repeat": right["repeat"],
                        "left_request_id": a[i]["request_id"], "right_request_id": b[j]["request_id"], "overlap_ns": end-start})
                    break
                if a[i]["end_ns"] <= b[j]["end_ns"]:
                    i += 1
                else:
                    j += 1
    return output


def worker(path):
    from .evaluation import Plan, run_cold_attempt
    path = Path(path).resolve()
    task = read_json(path)
    require(task.get("schema") == SCHEMA, "unknown worker protocol")
    root, output, source = (Path(task[key]).resolve() for key in ("root", "output", "source"))
    run = root / "experiments/runs" / f"{output.name}-s{task['seed']}-c{task['repeat']}"
    destination = output / f"seed-{task['seed']}-replay-{task['repeat']}"
    require(str(run) == task["run"] and str(destination) == task["destination"]
            and source.is_relative_to(root / "experiments/runs") and output.is_relative_to(root / "experiments/runs"),
            "worker paths do not match their exact attempt")
    owner, parent = ProcessIdentity(os.getpid()), ProcessIdentity(task["parent"]["pid"])
    require(parent.value == task["parent"], "parent PID reused")
    value = {"schema": SCHEMA, "binding": binding(task), "owner": owner.value,
             "source_unchanged": False, "host_unchanged": False}
    try:
        # No observer/game before assignment to the parent's exact owned Job.
        while not path.with_name("armed.json").exists():
            require(parent.alive() and time.monotonic() < task["arm_deadline"], "parent exited or arm timed out")
            time.sleep(.025)
        arm = read_json(path.with_name("armed.json"))
        require(arm == {"token": task["token"], "owner": owner.value, "job_assigned": True}, "worker was not armed for its creation identity")
        require(package_identity() == task["host"], "worker host source differs")
        load_cache(task["cache"])
        require(validate(source)["status"] == "finalized"
                and sha256(source / "manifest.json") == task["source_manifest_sha256"], "source archive identity differs")
        require(read_json(source / "trajectory/trajectory.json")["trajectory_id"] == task["source_trajectory_id"], "source trajectory differs")
        plan = Plan(**task["plan"]).validate()
        value["result"] = run_cold_attempt(root, plan, output, task["seed"], task["repeat"], source / "trajectory")
    except BaseException as error:
        value["result"] = failure_result(task, "worker", error)
    finally:
        for key, check in (
            ("source_unchanged", lambda: validate(source)["status"] == "finalized" and sha256(source / "manifest.json") == task["source_manifest_sha256"]),
            ("host_unchanged", lambda: package_identity() == task["host"])):
            try:
                require(check(), key + " failed")
                value[key] = True
            except Exception as error:
                result = value["result"]
                result["attempt"]["passed"] = False
                result.setdefault("secondary_errors", []).append({"stage": key, **error_detail(error)})
                result["error"] = result.get("error") or error_detail(error)
        value["finished_monotonic"] = time.monotonic()
        atomic(path.with_name("receipt.json"), value)
        owner.close()
        parent.close()
    return 0 if value["result"]["attempt"]["passed"] else 1


class WorkerProcess:
    """Only holds its own Popen and Job; never discovers/terminates other games."""
    def __init__(self, task, directory):
        if os.name != "nt":
            # Only the scheduler starts workers, and a worker owns one Windows
            # Job and one hidden host process. Fail explicitly here instead of
            # reaching a platform-specific creation flag that does not exist.
            raise RuntimeError("parallel cold worker hosts require Windows Job Objects")
        self.task, self.directory = task, Path(directory)
        self.directory.mkdir()
        self.process = self.job = None
        self.stdout = self.stderr = None
        self.started = time.monotonic()
        # Finite IO/reader/cleanup allowance beyond the configured simulation
        # wall bound and startup. Expiration is failure, never shortened replay.
        self.deadline = self.started + task["plan"]["cold_wall_budget_seconds"] + 2*max(600, task["plan"]["timeout_seconds"]) + 3600
        task["arm_deadline"] = self.started + 600
        atomic(self.directory / "task.json", task)
        try:
            self.job = OwnedJob()
            self.stdout = (self.directory / "stdout.log").open("xb")
            self.stderr = (self.directory / "stderr.log").open("xb")
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
            self.process = subprocess.Popen([sys.executable, "-u", "-m", "llm_vs_zombies.cold_workers",
                                             "--worker", str(self.directory / "task.json")],
                cwd=task["root"], env=environment, stdin=subprocess.DEVNULL, stdout=self.stdout, stderr=self.stderr,
                close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW)
            identity = ProcessIdentity(self.process.pid)
            try:
                self.owner = identity.value
                self.job.assign(self.process)
                atomic(self.directory / "armed.json", {"token": task["token"], "owner": self.owner, "job_assigned": True})
            finally:
                identity.close()
        except BaseException as primary:
            try:
                if self.process is not None and self.process.poll() is None:
                    self.process.terminate()  # Own unarmed child; no game can launch.
                    self.process.wait(timeout=10)
            except BaseException as cleanup_error:
                primary.add_note(f"unarmed host termination failed: {cleanup_error!r}")
            try:
                self.close()
            except BaseException as cleanup_error:
                primary.add_note(f"owned worker cleanup failed: {error_detail(cleanup_error)!r}")
            raise

    def poll(self):
        return self.process.poll()

    def finish(self):
        require(self.process.poll() is not None, "worker must really exit before result collection")
        self.process.wait(timeout=0)
        drain_started = time.monotonic()
        active = self.job.wait_empty()
        try:
            require(active == 0, "owned descendant survived worker exit")
            result = verify_result(self.task, read_json(self.directory / "receipt.json"), self.owner, self.process.returncode)
        except Exception as error:
            result = failure_result(self.task, "worker_result", error)
            if (self.directory / "receipt.json").is_file():
                result["rejected_receipt"] = {"path": str(self.directory / "receipt.json"), "sha256": sha256(self.directory / "receipt.json")}
        result["host_process"] = {"owner": self.owner, "returncode": self.process.returncode,
            "active_descendants_after_exit": active, "descendant_drain_seconds": time.monotonic()-drain_started,
            "dispatch_monotonic": self.started, "exit_monotonic": time.monotonic()}
        self.close()
        return result

    def emergency_finish(self, reason):
        # Deadline/fatal parent paths retain raw files and locks. Never finalize.
        result = failure_result(self.task, "owned_worker_deadline", RuntimeError(reason))
        active = None
        try:
            active = self.job.active() if self.job and self.job.handle else None
        except BaseException as error:
            result.setdefault("secondary_errors", []).append({"stage": "job_count", **error_detail(error)})
        finally:
            try:
                self.close()
            except BaseException as error:
                result.setdefault("secondary_errors", []).append({"stage": "owned_job_close_join", **error_detail(error)})
        result["host_process"] = {"owner": getattr(self, "owner", None),
            "emergency_job_closed": self.job is None or self.job.handle is None,
            "owned_host_exited": self.process is None or self.process.poll() is not None,
            "active_before_emergency_close": active, "returncode": self.process.returncode if self.process else None}
        return result

    def close(self):
        errors = []
        def attempt(label, operation):
            try:
                operation()
            except BaseException as error:
                errors.append((label, error))
        if self.job:
            attempt("owned Job close", self.job.close)
        if self.process is not None:
            attempt("owned host wait", lambda: self.process.wait(timeout=15))
        for label, stream in (("stdout close", self.stdout), ("stderr close", self.stderr)):
            if stream is not None:
                attempt(label, stream.close)
        if errors:
            primary_label, primary = errors[0]
            primary.add_note(primary_label)
            for label, error in errors[1:]:
                primary.add_note(f"{label}: {error!r}")
            raise primary


def schedule(tasks, start, *, clock=time.monotonic, wait=time.sleep):
    """Two at most; once failure is observed, queued tasks never start."""
    pending, running, results = list(tasks), {}, []
    started_handles = []
    failed = False
    primary = None
    def collect(handle, reason=None):
        try:
            return handle.emergency_finish(reason) if reason else handle.finish()
        except BaseException as error:
            result = failure_result(handle.task, "worker_collection", error)
            try:
                result["emergency_cleanup"] = handle.emergency_finish("result collection failed")
            except BaseException as cleanup_error:
                result["cleanup_error"] = error_detail(cleanup_error)
            return result
    def collect_if_finished(handle, deadline_reason):
        try:
            exited = handle.poll() is not None
        except BaseException as error:
            result = collect(handle, "owned host status query failed")
            result["poll_error"] = error_detail(error)
            return result
        if exited:
            return collect(handle)
        if clock() >= handle.deadline:
            return collect(handle, deadline_reason)
        return None
    try:
        while pending or running:
            # Poll all current completions BEFORE refilling a vacancy. A peer
            # which already failed must prevent dispatch even if another passed.
            for repeat, handle in list(running.items()):
                result = collect_if_finished(handle, "finite worker wall/startup/reader/closure bound exceeded")
                if result is None:
                    continue
                del running[repeat]
                results.append(result)
                failed = failed or not result["attempt"]["passed"]
            while not failed and pending and len(running) < 2:
                task = pending.pop(0)
                try:
                    handle = start(task)
                    started_handles.append(handle)
                    running[task["repeat"]] = handle
                except BaseException as error:
                    results.append(failure_result(task, "worker_start", error))
                    failed = True
            if failed and not running:
                break
            if running:
                wait(.05)
    except BaseException as error:
        primary = error_detail(error)
        failed = True
    finally:
        # Parent-level exceptions stop scheduling but do not discard an in-flight
        # normal result. Each has its own already finite deadline.
        while running:
            for repeat, handle in list(running.items()):
                result = collect_if_finished(handle, "parent failed; owned worker deadline reached")
                if result is None:
                    continue
                del running[repeat]
                results.append(result)
            if running:
                try:
                    wait(.05)
                except BaseException as error:
                    primary = primary or error_detail(error)
    exit_query_errors = []
    exited = True
    for handle in started_handles:
        try:
            exited = (handle.poll() is not None) and exited
        except BaseException as error:
            exited = False
            exit_query_errors.append({"repeat": handle.task["repeat"], **error_detail(error)})
    if not exited:
        primary = primary or {"type": "RuntimeError", "message": "owned worker join did not complete after emergency cleanup"}
    return {"results": sorted(results, key=lambda item: item["repeat"]),
            "not_started": [task["repeat"] for task in pending], "parent_error": primary,
            "all_started_exited": exited, "owned_exit_query_errors": exit_query_errors,
            "stopped_scheduling_after_failure": failed}


def run_parallel_colds(root, plan, output, seed, source_run, trajectory):
    require(os.name == "nt" and plan.cold_workers == 2, "two cold hosts require Windows and explicit cold_workers=2")
    root, output, source_run = Path(root).resolve(), Path(output).resolve(), Path(source_run).resolve()
    require(validate(source_run)["status"] == "finalized", "source must seal before cold scheduling")
    cache, host = prepare_cache(), package_identity()
    identity = ProcessIdentity(os.getpid())
    directory = output / f"seed-{seed}-cold-workers"
    directory.mkdir()
    tasks = []
    for repeat in range(1, max(2, plan.cold_starts)):
        tasks.append({"schema": SCHEMA, "token": uuid.uuid4().hex, "root": str(root), "output": str(output),
            "seed": seed, "repeat": repeat, "plan": asdict(plan), "parent": identity.value,
            "run": str(root / "experiments/runs" / f"{output.name}-s{seed}-c{repeat}"),
            "destination": str(output / f"seed-{seed}-replay-{repeat}"), "source": str(source_run),
            "source_manifest_sha256": sha256(source_run / "manifest.json"),
            "source_trajectory_id": trajectory.manifest["trajectory_id"], "host": host, "cache": cache})
    try:
        summary = schedule(tasks, lambda task: WorkerProcess(task, directory / f"repeat-{task['repeat']}"))
        summary["schema"] = SCHEMA
        summary["seed"] = seed
        summary["source"] = str(source_run)
        summary["cold_workers"] = 2
        summary["disk_reservation"] = False
        summary["measured_positive_rpc_overlaps"] = measured_overlaps(summary["results"])
        summary["overlap_scope"] = "verified positive RPC intervals, not CPU instruction simultaneity"
        try:
            write_json(directory / "summary.json", summary)
        except Exception as error:
            summary["parent_error"] = summary["parent_error"] or error_detail(error)
        results = summary["results"]
        if summary["parent_error"] and results and all(item["attempt"]["passed"] for item in results):
            # A host can finish cleanly despite a scheduling failure. Preserve its
            # genuine receipt, while making the suite unable to claim full success.
            results[-1]["scheduler_error"] = summary["parent_error"]
            results[-1]["attempt"]["passed"] = False
            results[-1]["error"] = summary["parent_error"]
        require(results, "parallel scheduler produced no actual attempt receipts")
        for result in results:
            result["scheduling"] = {key: value for key, value in summary.items() if key != "results"}
        return results
    finally:
        identity.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    args = parser.parse_args(argv)
    return worker(args.worker)


if __name__ == "__main__":
    raise SystemExit(main())
