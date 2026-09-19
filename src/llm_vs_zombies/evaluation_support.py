"""Bounded host checks and truthful retention for live evaluation sessions.

These helpers do not alter game requests, validate synthetic evidence as live,
or remove writer locks. A retained failed archive is not a verified trajectory.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import sys
import time
import zipfile

from .records import finish, read_json, sha256, validate, write_json


def error_detail(error: BaseException, *, prefer_report=False) -> dict:
    chain, seen, current = [], set(), error
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        item = {"type": type(current).__name__, "message": str(current)}
        if isinstance(getattr(current, "report", None), dict):
            item["report"] = current.report
        if isinstance(getattr(current, "detail", None), dict):
            item["detail"] = current.detail
        chain.append(item)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    # replay can encounter an I/O failure while writing its divergence report.
    # Retain the actual divergence as primary and the write failure separately.
    primary = next((item for item in reversed(chain) if "report" in item), chain[0]) if prefer_report else chain[0]
    return {**primary, **({"exception_chain": chain} if len(chain) > 1 else {})}


class BoundaryStop(RuntimeError):
    def __init__(self, reason: str, detail: dict):
        self.reason, self.detail = reason, detail
        super().__init__(reason)


class BoundaryBudget:
    """Check actual resources only at already completed request boundaries."""
    def __init__(self, root, plan, *, cold=False, clock=None, disk_usage=None, report_path=None):
        self.root, self.plan = Path(root), plan
        self.clock = clock or time.monotonic
        self.disk_usage = disk_usage or shutil.disk_usage
        self.started = self.clock()
        self.wall_limit = plan.cold_wall_budget_seconds if cold else plan.wall_budget_seconds
        self.bucket = None
        self.samples = []
        self.stop = None
        self.report_path = report_path
        self.last_version = None

    def check(self, version, *, force=False, enforce=True):
        self.last_version = dict(version)
        if self.stop is not None:
            if enforce:
                raise BoundaryStop(self.stop["reason"], self.stop)
            return
        elapsed = self.clock() - self.started
        bucket = version["tick"] // self.plan.disk_check_ticks
        sampled = force or self.bucket != bucket
        if sampled:
            usage = self.disk_usage(self.root)
            sample = {"version": dict(version), "free_bytes": usage.free,
                      "min_free_bytes": self.plan.min_free_bytes, "elapsed_seconds": elapsed}
            self.samples.append(sample)
            self.bucket = bucket
            if usage.free < self.plan.min_free_bytes:
                self.stop = {"reason": "disk_reserve_stop", **sample}
        if self.stop is None and elapsed >= self.wall_limit:
            self.stop = {"reason": "wall_budget_exhausted", "version": dict(version),
                         "elapsed_seconds": elapsed, "limit_seconds": self.wall_limit}
        if self.report_path is not None and (sampled or self.stop is not None):
            write_json(self.report_path, self.report())
        if enforce and self.stop is not None:
            raise BoundaryStop(self.stop["reason"], self.stop)

    def report(self):
        return {"wall_budget_seconds": self.wall_limit, "elapsed_seconds": self.clock()-self.started,
                "disk_check_ticks": self.plan.disk_check_ticks, "min_free_bytes": self.plan.min_free_bytes,
                "samples": self.samples, "stop": self.stop,
                "scope": "completed request boundaries; no filesystem capacity reservation"}


class ReplayBoundaryClient:
    """Delegate exact replay RPCs; never split or retry a mutation.

    replay uses request() for controlled calls. Cleanup owns the original Client,
    so a resource stop cannot prevent stop_recording or replace a failed request.
    """
    def __init__(self, client, budget, after_step, *, on_rpc=None):
        self.client, self.budget, self.after_step = client, budget, after_step
        self.on_rpc = on_rpc

    def __getattr__(self, name):
        return getattr(self.client, name)

    def request(self, method, *args, **kwargs):
        controlled = method in {"advance", "commit"}
        if controlled:
            self.budget.check(self.client.version)
        started = time.monotonic_ns() if controlled and self.on_rpc else None
        result = self.client.request(method, *args, **kwargs)
        ended = time.monotonic_ns() if started is not None else None
        if started is not None:
            self.on_rpc({"request_id": kwargs.get("request_id"), "method": method,
                "start_ns": started, "end_ns": ended, "version": result["observation"]["version"],
                "executed_ticks": result.get("executed_ticks"), "executed_engine_calls": result.get("executed_engine_calls")})
        if controlled:
            self.budget.check(result["observation"]["version"], enforce=False)
            self.after_step(result["observation"])
        return result


def packaging_space(run: Path, plan, *, disk_usage=None) -> dict:
    audit, trace = run / "audit", run / "decisions/evaluation.jsonl"
    if not audit.is_dir() or not trace.is_file():
        raise ValueError("closed audit and decision trace are required for packaging")
    files = [p for p in audit.rglob("*") if p.is_file()] + [trace]
    sizes = {p.relative_to(run).as_posix(): p.stat().st_size for p in files}
    free = (disk_usage or shutil.disk_usage)(run).free
    required = sum(sizes.values()) + plan.packaging_reserve_bytes
    return {"files": sizes, "actual_copy_bytes": sum(sizes.values()), "free_bytes": free,
            "reserve_bytes": plan.packaging_reserve_bytes, "required_free_bytes": required,
            "sufficient": free >= required}


def writers_closed(cleanup: dict) -> bool:
    return all(cleanup.get(key) is True for key in
               ("recording_closed", "client_closed", "trace_closed", "owned_process_stopped"))


def cleanup_passed(cleanup: dict) -> bool:
    return writers_closed(cleanup) and not any(key.endswith("_error") for key in cleanup)


def finalize_run(run: Path, plan, outcome: str, *, package=False, primary_error=None) -> tuple[dict, object]:
    """Retain only real closed evidence. Report every secondary failure separately.

    The caller writes this returned result outside the sealed run; no changes to
    run files are made after finish(). Strict replay verifies its own full tail
    before its caller invokes this helper.
    """
    result = {"run": str(run), "outcome": outcome, "primary_error": primary_error,
              "secondary_errors": [], "archive_sealed": False, "trajectory_verified": False,
              "passed_recording": False}
    trajectory = None
    if (run / "manifest.json").is_file() and read_json(run / "manifest.json").get("status") == "finalized":
        raise ValueError("refusing to change an already finalized run")
    try:
        result["cleanup"] = read_json(run / "evaluation-cleanup.json")
    except Exception as error:
        result["secondary_errors"].append({"stage": "read_cleanup", **error_detail(error)})
    result["cleanup_passed"] = cleanup_passed(result.get("cleanup", {}))
    closed = writers_closed(result.get("cleanup", {}))
    if package and closed:
        try:
            space = packaging_space(run, plan)
            result["packaging_space"] = space
            if not space["sufficient"]:
                result["packaging_skipped"] = "packaging_skipped_insufficient_space"
            else:
                from .engine_replay import build_trajectory
                trajectory = build_trajectory(run / "decisions/evaluation.jsonl", run / "audit", run / "trajectory")
                result["trajectory_verified"] = True
                result["trajectory_id"] = trajectory.manifest["trajectory_id"]
        except Exception as error:
            result["secondary_errors"].append({"stage": "strict_packaging", **error_detail(error)})
    elif package:
        result["packaging_skipped"] = "cleanup_not_verified"
    # This file intentionally describes the pre-seal attempt. The external suite
    # receipt below is the authority for actual finish()/validate() success.
    try:
        write_json(run / "evaluation-finalization.json", result)
    except Exception as error:
        result["secondary_errors"].append({"stage": "write_finalization", **error_detail(error)})
    try:
        if not closed:
            raise ValueError("cleanup receipt does not prove recorder, client, trace and owned process closed")
        manifest = finish(run, outcome)
        result["archive_sealed"] = True
        result["manifest_sha256"] = sha256(run / "manifest.json")
        result["archive_validation"] = validate(run)
        result["archive_outcome"] = manifest["outcome"]
    except Exception as error:
        result["secondary_errors"].append({"stage": "seal_archive", **error_detail(error)})
    result["passed_recording"] = (result["cleanup_passed"] and result["archive_sealed"]
        and not result["secondary_errors"] and (not package or result["trajectory_verified"]))
    return result, trajectory


def host_sources(run: Path, previous: dict | None = None) -> dict:
    """Bind actual import paths to the run's existing implementation ZIP.

    This is filesystem source provenance, not a claim about arbitrary trusted
    strategy code or a byte-for-byte Python interpreter memory snapshot.
    """
    package = Path(__file__).resolve().parent
    current = {p.relative_to(package).as_posix(): sha256(p) for p in package.rglob("*.py")}
    archive = run / "inputs/implementation.zip"
    with zipfile.ZipFile(archive) as zipped:
        matches = all(hashlib.sha256(zipped.read("src/llm_vs_zombies/" + name)).hexdigest() == digest
                      for name, digest in current.items())
    imported = {}
    for name, module in tuple(sys.modules.items()):
        spec_name = getattr(getattr(module, "__spec__", None), "name", "") or ""
        if name == "llm_vs_zombies" or name.startswith("llm_vs_zombies.") or spec_name in {"llm_vs_zombies.evaluation", "llm_vs_zombies.cold_workers"}:
            filename = getattr(module, "__file__", None)
            if filename:
                path = Path(filename).resolve()
                imported[name] = {"path": str(path), "sha256": sha256(path)}
                matches = matches and path.is_relative_to(package) and path.suffix == ".py"
    stable = previous is None or (previous["source_files"] == current
                                  and previous["source_archive_sha256"] == sha256(archive))
    return {"package_directory": str(package), "source_files": current, "imported_modules": imported,
            "source_archive": "inputs/implementation.zip", "source_archive_sha256": sha256(archive),
            "matches_archive": matches, "unchanged": stable,
            "scope": "actual imported module paths and on-disk sources, checked against archived implementation"}
