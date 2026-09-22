"""Parallel branch workers: one isolated worker process per training branch.

N10 of the branch-data contract (``docs/分支数据合同与实施计划.md``, issue
#38). One branch worker executes one node of a tree from that tree's root. The
scheduler starts several branches at the same time, gives every branch its own
process, user profile, log, artifact and evidence directory, and keeps a
failure local: the failing branch is sealed where it happened while the other
branches keep running.

Boundary against the scoring pipeline (#19, ``cold_workers`` / ``evaluation``):
this module never records a source, never applies the cold-replay gates, never
retries an attempt on behalf of a score, and never claims cold-start
determinism. Its plans reject scoring-pipeline keys outright
(:data:`SCORING_KEYS`) and every report states
``scoring_cold_replay: not_applicable``. Same-root fork determinism (P1) is
only ever compared between branches that the plan declares comparable - one
root, one action sequence. Couplings that are *not* isolated are listed in the
report (:data:`SHARED_RESOURCES`); the audio device stays ``待定`` until N9
lands, so a plan has to either serialize the device window with a lease or
accept the shared device explicitly.
"""
from __future__ import annotations

import argparse
import copy
import ctypes
import hashlib
import importlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .audit_compare import EvidenceError, canonical, first_difference
from .records import read_json, sha256, write_json

SCHEMA = "lvz.branch-workers.v1"
TASK_SCHEMA = "lvz.branch-worker-task.v1"
RECEIPT_SCHEMA = "lvz.branch-worker-receipt.v1"
REPORT_SCHEMA = "lvz.branch-workers-report.v1"
VERIFY_SCHEMA = "lvz.branch-workers-verify.v1"
FRAMES_SCHEMA = "lvz.branch-worker-frames.v1"
PLAN_FILE = "plan.json"
REPORT_FILE = "report.json"
BRANCHES_DIRECTORY = "branches"
LEASE_JOURNAL = "device-lease.jsonl"
TASK_FILE = "task.json"
ARM_FILE = "armed.json"
RECEIPT_FILE = "receipt.json"
FAILURE_FILE = "failure.json"
FRAMES_FILE = "frames.json"
BRANCH_DIRECTORIES = ("profile", "logs", "artifacts", "evidence")
DEVICE_POLICIES = ("shared_declared", "exclusive_lease")
MAX_WORKERS_LIMIT = 4
MONOTONIC_CLOCK = "same-host system-wide monotonic (comparable across worker processes)"
BRANCH_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PLAN_KEYS = frozenset({"schema", "plan_id", "executor", "max_workers", "timeout_seconds", "root_identity", "tree",
                       "device", "env", "branches", "comparisons"})
BRANCH_KEYS = frozenset({"branch_id", "node", "steps", "env"})
DEVICE_KEYS = frozenset({"policy", "accept_shared", "reason", "lease_timeout_seconds"})
COMPARISON_KEYS = frozenset({"name", "branches"})
# Keys that belong to the scoring pipeline (#19). A branch plan may not use
# them: the training pipeline forks, the scoring pipeline re-runs cold.
SCORING_KEYS = frozenset({"cold_starts", "cold_workers", "cold_wall_budget_seconds", "strict", "full_cycle",
                          "ten_cold_starts", "official_replay", "recovery_probes", "experiment_ready",
                          "live_gates", "source_run", "replay_report"})
PIPELINE = {"name": "training-branch", "fork": "required",
            "scoring_cold_replay": "not_applicable",
            "scope": "one sealed root, one worker process per branch",
            "excluded": ["source recording", "cold-replay gates", "retry/rerun for scoring",
                         "original-engine determinism certification"]}
# Still-shared couplings, taken from ``docs/跨界耦合清单.md`` §3.5 (状态词表 in
# its §1.2). Only entries this module actually changes are listed as fixed.
SHARED_RESOURCES = (
    {"id": "C28", "resource": "single-instance mutex",
     "status": "已固定", "isolation": "the launcher names the mutex per target process, so parallel branches "
                                       "never collide with each other or with a user-opened original",
     "evidence": "docs/跨界耦合清单.md §3.5 C28; docs/launcher.md §5"},
    {"id": "C29", "resource": "user profile and PopCap registry key",
     "status": "已固定", "isolation": "every branch owns <branch>/profile as its appdata sandbox and its own "
                                       "per-run registry seed, injected by the launcher before the engine starts",
     "evidence": "docs/跨界耦合清单.md §3.5 C29; docs/launcher.md §3/§5"},
    {"id": "C30", "resource": "working directory",
     "status": "已固定", "isolation": "each worker process starts with its branch directory as cwd; a branch can "
                                       "only write relative paths inside itself",
     "evidence": "docs/跨界耦合清单.md §3.5 C30; docs/launcher.md §2"},
    {"id": "C31", "resource": "process and job ownership",
     "status": "已固定", "isolation": "one owned Windows Job Object (or POSIX session) per branch; an isolated "
                                       "branch failure terminates that branch tree only, after the worker is reaped",
     "evidence": "docs/parallel-cold.md; docs/跨界耦合清单.md §3.5 C31"},
    {"id": "C32", "resource": "physical audio device, DirectSound objects and BASS music channels",
     "status": "待定", "isolation": "not virtualized (N9 has not landed). exclusive_lease serializes the device "
                                     "window and journals it; shared_declared accepts the shared device on purpose; "
                                     "sound_effects_allocation_none_v1 only cuts Foley allocation",
     "evidence": "docs/跨界耦合清单.md §3.2/§3.5 C32; docs/silent-audio.md"},
    {"id": "C33", "resource": "disk, CPU and memory contention",
     "status": "待定", "isolation": "no capacity is reserved: the parent enforces a per-branch wall deadline and "
                                     "keeps real failures instead of retrying them",
     "evidence": "docs/跨界耦合清单.md §3.5 C33; docs/parallel-cold.md"},
    {"id": "C34", "resource": "display device and window",
     "status": "已固定", "isolation": "launcher-side hidden window (SW_HIDE, WS_EX_NOACTIVATE); N10 does not "
                                       "change display handling and does not claim headless removal",
     "evidence": "docs/跨界耦合清单.md §3.5 C34; docs/launcher.md §5"},
    {"id": "N10-1", "resource": "device lease journal (control plane)",
     "status": "已固定", "isolation": "append-only, written only while holding the device lease; the report "
                                       "verifies pair closure and non-overlap",
     "evidence": "this module, ``verify_lease_journal``"},
    {"id": "N10-2", "resource": "worker host source (the Python package on disk)",
     "status": "已固定", "isolation": "every worker recomputes the package digest it was dispatched with and "
                                       "fails its own branch if the source changed",
     "evidence": "this module, ``package_identity``"},
)


def require(value, message):
    if not value:
        raise EvidenceError(message)


def digest_bytes(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def atomic(path, value) -> Path:
    path = Path(path)
    pending = path.with_suffix(path.suffix + ".pending")
    write_json(pending, value)
    os.replace(pending, path)
    return path


def append_line(path, value) -> None:
    """One JSON record per line, flushed and synced while the caller owns the lease."""
    path = Path(path)
    line = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line)
        stream.flush()
        os.fsync(stream.fileno())


def error_detail(error, *, prefer_report=False) -> dict:
    detail = {"type": type(error).__name__, "message": str(error)}
    if getattr(error, "__notes__", None):
        detail["exception_notes"] = list(error.__notes__)
    if prefer_report and getattr(error, "report", None) is not None:
        detail["report"] = str(error.report)
    return detail


def package_identity() -> dict:
    """The host source a worker must still see when it starts."""
    package = Path(__file__).resolve().parent
    return {"directory": str(package), "files": {path.relative_to(package).as_posix(): sha256(path)
                                                 for path in sorted(package.rglob("*.py"))}}


def file_manifest(directory) -> dict:
    """Every file below ``directory`` with its digest; the immutability evidence."""
    directory = Path(directory)
    files = {}
    if directory.is_dir():
        for path in sorted(item for item in directory.rglob("*") if item.is_file()):
            files[path.relative_to(directory).as_posix()] = {"sha256": sha256(path),
                                                             "size": path.stat().st_size}
    return {"files": files, "count": len(files), "digest": digest_bytes(files)}


PARENT_FILES = ("stdout.log", "stderr.log", RECEIPT_FILE, FAILURE_FILE)


def evidence_manifest(directory) -> dict:
    """Branch-owned evidence only: no parent streams, no seals, no writers in flight."""
    manifest = file_manifest(directory)
    files = {name: value for name, value in manifest["files"].items()
             if name not in PARENT_FILES and not name.endswith(".pending")}
    return {"files": files, "count": len(files), "digest": digest_bytes(files)}


def manifest_difference(before: dict, after: dict) -> dict:
    left, right = before.get("files", {}), after.get("files", {})
    return {"added": sorted(set(right) - set(left)),
            "removed": sorted(set(left) - set(right)),
            "changed": sorted(name for name in set(left) & set(right) if left[name] != right[name])}


def action_sequence(steps) -> list:
    """The semantic action sequence of a recorded node.

    P1 talks about *one action sequence*, so the digest keeps the request
    method, its parameters and the pre-state expectation, and drops the
    per-run request id: two replicas of the same actions must compare equal
    even though every recording names its own requests.
    """
    return [{"method": step["request"]["method"], "params": copy.deepcopy(step["request"].get("params", {})),
             "expect": copy.deepcopy(step["request"].get("expect"))} for step in steps]


def steps_digest(steps) -> str:
    return digest_bytes(action_sequence(steps))


def validate_steps(steps, label: str) -> list:
    require(isinstance(steps, list) and steps, f"{label}: an action sequence must be a non-empty list")
    for index, step in enumerate(steps):
        request = step.get("request") if isinstance(step, dict) else None
        require(isinstance(request, dict) and isinstance(request.get("method"), str) and request["method"],
                f"{label}: step {index} must name a request method")
    return copy.deepcopy(steps)


def parse_executor(spec) -> tuple[str, str]:
    require(isinstance(spec, str) and spec, "an executor must be named module:function or path/to/executor.py:function")
    # Split on the last colon: a Windows drive letter must not be read as the separator.
    target, separator, name = spec.rpartition(":")
    require(separator and target and name, f"executor {spec!r} must be module:function or path/to/executor.py:function")
    return target, name


def absolutize_executor(spec: str) -> str:
    """Resolve a file executor against the scheduler's cwd, not a branch's cwd."""
    target, name = parse_executor(spec)
    return f"{Path(target).resolve()}:{name}" if target.endswith(".py") else spec


def load_executor(spec: str):
    """Resolve ``module:function`` or ``path/to/executor.py:function``."""
    target, name = parse_executor(spec)
    if target.endswith(".py"):
        path = Path(target).resolve()
        require(path.is_file(), f"executor module is missing: {path}")
        module_spec = importlib.util.spec_from_file_location(f"lvz_branch_executor_{path.stem}", path)
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
    else:
        module = importlib.import_module(target)
    function = getattr(module, name, None)
    require(callable(function), f"executor {spec} is not callable")
    return function, f"{getattr(module, '__name__', target)}:{name}"


@dataclass(frozen=True)
class Branch:
    """One branch of one plan: a label, an action sequence, an optional tree node."""

    branch_id: str
    steps: tuple
    steps_sha256: str
    node: str | None = None
    tree: dict | None = None
    end: dict | None = None
    env: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Plan:
    """A validated branch plan: one root, N branches, declared comparisons."""

    plan_id: str
    executor: str | None
    max_workers: int
    timeout_seconds: float
    root_identity: dict
    branches: tuple
    comparisons: tuple
    device: dict
    env: dict
    tree: dict | None

    def branch(self, branch_id: str) -> Branch:
        for branch in self.branches:
            if branch.branch_id == branch_id:
                return branch
        raise EvidenceError(f"plan has no branch {branch_id}")

    def to_dict(self) -> dict:
        # ``tree`` stays the tree directory so a written plan can be loaded again.
        document = {"schema": SCHEMA, "plan_id": self.plan_id, "executor": self.executor,
                    "max_workers": self.max_workers, "timeout_seconds": self.timeout_seconds,
                    "root_identity": copy.deepcopy(self.root_identity),
                    "tree": None if self.tree is None else self.tree["directory"],
                    "device": copy.deepcopy(self.device), "env": dict(self.env),
                    "branches": [{"branch_id": branch.branch_id, "node": branch.node,
                                  "steps": copy.deepcopy(list(branch.steps)), "env": dict(branch.env)}
                                 for branch in self.branches],
                    "comparisons": copy.deepcopy(list(self.comparisons))}
        return document

    @classmethod
    def load(cls, path) -> "Plan":
        return cls.from_dict(read_json(Path(path)))

    @classmethod
    def from_dict(cls, value) -> "Plan":
        require(isinstance(value, dict), "a branch plan must be a JSON object")
        require(value.get("schema") == SCHEMA, f"branch plan schema must be {SCHEMA}")
        scoring = sorted(set(value) & SCORING_KEYS)
        require(not scoring, f"scoring-pipeline keys are not branch-worker inputs: {scoring}")
        unknown = sorted(set(value) - PLAN_KEYS)
        require(not unknown, f"branch plan has unknown keys: {unknown}")
        executor = value.get("executor")
        if executor is not None:
            parse_executor(executor)
        timeout = value.get("timeout_seconds", 900)
        require(isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and 0 < timeout <= 86400,
                "timeout_seconds must be a positive number of seconds up to 86400")
        env = _environment(value.get("env"), "plan")
        device = _device(value.get("device"))
        branches, tree = _branches(value)
        max_workers = value.get("max_workers", min(2, len(branches)))
        require(isinstance(max_workers, int) and not isinstance(max_workers, bool)
                and 1 <= max_workers <= MAX_WORKERS_LIMIT, f"max_workers must be an integer in 1..{MAX_WORKERS_LIMIT}")
        require(max_workers <= len(branches), "max_workers cannot exceed the number of branches")
        comparisons = _comparisons(value.get("comparisons"), branches)
        identity = tree["root_identity"] if tree is not None else _identity(value.get("root_identity"))
        if tree is None:
            require("root_identity" in value, "an inline plan requires the root identity of its branches")
        elif value.get("root_identity") is not None:
            require(_identity(value["root_identity"]) == identity,
                    "the plan's root identity does not match the sealed tree root identity")
        content = {"root_identity": identity, "tree": None if tree is None else tree["tree_id"],
                   "branches": [{"branch_id": branch.branch_id, "node": branch.node,
                                 "steps_sha256": branch.steps_sha256} for branch in branches],
                   "device": device}
        plan_id = digest_bytes(content)
        if value.get("plan_id") is not None:
            require(value["plan_id"] == plan_id,
                    "the plan_id does not match the plan contents; drop it and let the plan derive its own")
        return cls(plan_id=plan_id, executor=executor, max_workers=max_workers,
                   timeout_seconds=float(timeout), root_identity=identity, branches=tuple(branches),
                   comparisons=tuple(comparisons), device=device, env=env,
                   tree=None if tree is None else {"tree_id": tree["tree_id"], "directory": tree["directory"]})


def _environment(value, label: str) -> dict:
    if value is None:
        return {}
    require(isinstance(value, dict), f"{label} env must be an object of strings")
    for key, item in value.items():
        require(isinstance(key, str) and key and isinstance(item, str), f"{label} env must map strings to strings")
        require(not key.startswith("LVZ_"), f"{label} env may not set the reserved worker variables (LVZ_*)")
    return dict(value)


def _identity(value) -> dict:
    from .evidence_tree import normalize_identity

    require(isinstance(value, dict), "root_identity must be an object with game, artifacts and init_recipe")
    return normalize_identity(value)


def _device(value) -> dict:
    value = {} if value is None else value
    require(isinstance(value, dict), "device must be an object")
    unknown = sorted(set(value) - DEVICE_KEYS)
    require(not unknown, f"device has unknown keys: {unknown}")
    policy = value.get("policy")
    require(policy in DEVICE_POLICIES, f"device policy must be one of {list(DEVICE_POLICIES)}")
    lease_timeout = value.get("lease_timeout_seconds")
    if lease_timeout is not None:
        require(isinstance(lease_timeout, (int, float)) and not isinstance(lease_timeout, bool) and lease_timeout > 0,
                "lease_timeout_seconds must be a positive number")
    if policy == "shared_declared":
        require(value.get("accept_shared") is True,
                "device policy shared_declared requires accept_shared: true")
        require(isinstance(value.get("reason"), str) and value["reason"].strip(),
                "device policy shared_declared requires a reason")
    else:
        require(value.get("accept_shared") in (None, False),
                "accept_shared is only meaningful for device policy shared_declared")
    return {"policy": policy, "accept_shared": bool(value.get("accept_shared")),
            "reason": value.get("reason"), "lease_timeout_seconds": lease_timeout}


def _branches(value) -> tuple[list, dict | None]:
    from .evidence_tree import EvidenceTree, end_boundary
    from .engine_replay import Trajectory

    tree_path = value.get("tree")
    nodes = {}
    tree = None
    if tree_path is not None:
        require(isinstance(tree_path, str) and tree_path, "tree must be a path to a sealed evidence tree")
        packaged = EvidenceTree.load(Path(tree_path).resolve())
        summary = packaged.validate()
        for entry in packaged.nodes:
            trajectory = Trajectory.load(packaged.directory / entry["path"])
            placement = trajectory.manifest["tree"]
            require(placement["branch_id"] == entry["branch_id"] and placement["trunk"] == entry["trunk"]
                    and trajectory.manifest["trajectory_id"] == entry["trajectory_id"],
                    f"tree node placement disagrees with the tree index: {entry['key']}")
            nodes[entry["key"]] = {"entry": entry, "trajectory": trajectory, "placement": placement}
        root = nodes[summary["root"]]
        tree = {"tree_id": summary["tree_id"], "directory": str(packaged.directory), "summary": summary,
                "root_identity": copy.deepcopy(root["placement"]["root"]["identity"])}
    raw = value.get("branches")
    require(isinstance(raw, list) and raw, "a branch plan needs at least one branch")
    branches, labels, used_nodes = [], set(), set()
    for index, item in enumerate(raw):
        label = f"branch {index}"
        require(isinstance(item, dict), f"{label} must be an object")
        scoring = sorted(set(item) & SCORING_KEYS)
        require(not scoring, f"{label} uses scoring-pipeline keys: {scoring}")
        unknown = sorted(set(item) - BRANCH_KEYS)
        require(not unknown, f"{label} has unknown keys: {unknown}")
        branch_id = item.get("branch_id")
        require(isinstance(branch_id, str) and BRANCH_LABEL.match(branch_id),
                f"{label} needs a filesystem-safe branch_id ([A-Za-z0-9][A-Za-z0-9._-]*, at most 64 characters)")
        require(branch_id not in labels, f"branch_id {branch_id} is used twice")
        labels.add(branch_id)
        env = _environment(item.get("env"), f"branch {branch_id}")
        node, binding, end = item.get("node"), None, None
        if tree is not None:
            require(isinstance(node, str) and node, f"branch {branch_id} must name a tree node")
            require(node in nodes, f"branch {branch_id} names a node that is not in the tree: {node}")
            require(node not in used_nodes, f"tree node {node} is claimed by two branches")
            used_nodes.add(node)
            record = nodes[node]
            steps = validate_steps(copy.deepcopy(list(record["trajectory"].steps)),
                                   f"branch {branch_id} (node {node})")
            placement = record["placement"]
            binding = {"tree_id": tree["tree_id"], "node": node, "branch_id": placement["branch_id"],
                       "trunk": placement["trunk"], "trajectory_id": record["trajectory"].manifest["trajectory_id"],
                       "chain_sha256": placement["chain"]["sha256"],
                       "parent_trajectory_id": (placement["parent"] or {}).get("trajectory_id"),
                       "fork": copy.deepcopy(placement["parent"]["boundary"] if placement["parent"] else None)}
            end = end_boundary(record["trajectory"])
        else:
            require(node is None, f"branch {branch_id} names tree node {node} without a plan tree")
            require("steps" in item, f"branch {branch_id} needs either steps or a tree node")
            steps = validate_steps(item.get("steps"), f"branch {branch_id}")
        branches.append(Branch(branch_id=branch_id, steps=tuple(steps), steps_sha256=steps_digest(steps),
                               node=node, tree=binding, end=end, env=env))
    return branches, tree


def _comparisons(value, branches) -> list:
    value = [] if value is None else value
    require(isinstance(value, list), "comparisons must be a list")
    labels = {branch.branch_id: branch for branch in branches}
    names, output = set(), []
    for index, item in enumerate(value):
        label = f"comparison {index}"
        require(isinstance(item, dict), f"{label} must be an object")
        unknown = sorted(set(item) - COMPARISON_KEYS)
        require(not unknown, f"{label} has unknown keys: {unknown}")
        name = item.get("name")
        require(isinstance(name, str) and name and name not in names, f"{label} needs a unique name")
        names.add(name)
        members = item.get("branches")
        require(isinstance(members, list) and len(set(members)) >= 2,
                f"{label} needs at least two distinct branch ids")
        for member in members:
            require(member in labels, f"{label} references an unknown branch: {member}")
        digests = {labels[member].steps_sha256 for member in members}
        require(len(digests) == 1,
                f"{label} compares different action sequences; only the same root with the same actions "
                "is comparable (P1)")
        output.append({"name": name, "branches": list(dict.fromkeys(members))})
    return output


SOURCE_ROOT = Path(__file__).resolve().parents[1]
MANIFESTS_DIRECTORY = "manifests"


class ProcessBinding:
    """PID plus the strongest creation stamp the host offers.

    Windows binds PID to creation time (``window_observer.ProcessIdentity``);
    POSIX binds to the procfs start tick when ``/proc`` is readable and falls
    back to a PID-only binding, which the receipt states explicitly.
    """

    def __init__(self, pid):
        if type(pid) is not int or pid <= 0:
            raise ValueError("process PID must be positive")
        self.pid = pid
        self._handle = None
        if os.name == "nt":
            from .window_observer import ProcessIdentity

            self._handle = ProcessIdentity(pid)
            self.value = self._handle.value
            self.kind = "pid+creation_time_100ns"
        else:
            start = self._start_tick()
            self.value = {"pid": pid} if start is None else {"pid": pid, "start_time_ticks": start}
            self.kind = "pid+procfs_start_time" if start is not None else "pid_only"

    def _start_tick(self):
        try:
            return int(Path(f"/proc/{self.pid}/stat").read_text(encoding="ascii", errors="replace").split()[21])
        except (OSError, ValueError, IndexError):
            return None

    def alive(self) -> bool:
        if self._handle is not None:
            return self._handle.alive()
        start = self._start_tick()
        if start is not None:
            return start == self.value.get("start_time_ticks")
        try:
            os.kill(self.pid, 0)
            return True
        except OSError:
            return False

    def close(self):
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def read_journal(path) -> list:
    path = Path(path)
    if not path.is_file():
        return []
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise EvidenceError(f"lease journal line {number} is not JSON: {error}")
        require(isinstance(value, dict), f"lease journal line {number} is not an object")
        records.append(value)
    return records


def lease_open_records(journal) -> list:
    """Acquire records that no release or abandonment closed yet."""
    records = read_journal(journal)
    closed = {(item.get("branch_id"), item.get("token")) for item in records
              if item.get("event") in ("release", "abandoned")}
    return [item for item in records if item.get("event") == "acquire"
            and (item.get("branch_id"), item.get("token")) not in closed]


def close_open_lease(journal, branch_id, reason) -> list:
    """The parent seals a lease window whose owner exited without releasing it.

    Only the parent may do this, only after the owner process is reaped, and
    only for the window it opened: the entry records that the release instant
    is unknown and why.
    """
    sealed = []
    for record in lease_open_records(journal):
        if record.get("branch_id") != branch_id:
            continue
        entry = {"event": "abandoned", "branch_id": branch_id, "token": record.get("token"),
                 "by": "parent", "reason": reason, "monotonic_ns": time.monotonic_ns()}
        append_line(journal, entry)
        sealed.append(entry)
    return sealed


def verify_lease_journal(path, *, expect=None) -> dict:
    """Pair closure and non-overlap of the device lease windows.

    The lock itself is the mechanism (a branch cannot hold two windows at
    once); this check is the evidence. A window closed by ``abandoned`` has an
    unknown release instant, so it is listed separately instead of being read
    as an overlap.
    """
    records = read_journal(path)
    open_tokens, intervals, unbounded, problems = {}, [], [], []
    acquires = releases = abandoned = 0
    for index, item in enumerate(records, 1):
        event = item.get("event")
        key = (item.get("branch_id"), item.get("token"))
        if event == "acquire":
            acquires += 1
            require(isinstance(item.get("branch_id"), str) and isinstance(item.get("token"), str),
                    f"lease journal line {index} needs a branch and a token")
            if key in open_tokens:
                problems.append({"line": index, "reason": "acquire reuses an open token", "token": item.get("token")})
                continue
            open_tokens[key] = item
        elif event in ("release", "abandoned"):
            record = open_tokens.pop(key, None)
            if event == "release":
                releases += 1
                if record is None:
                    problems.append({"line": index, "reason": "release without an open acquire", "token": item.get("token")})
                else:
                    intervals.append({"branch_id": key[0], "token": key[1],
                                      "start_ns": record.get("monotonic_ns"), "end_ns": item.get("monotonic_ns"),
                                      "wait_seconds": record.get("wait_seconds")})
            else:
                abandoned += 1
                if record is None:
                    problems.append({"line": index, "reason": "abandonment without an open acquire",
                                     "token": item.get("token")})
                else:
                    unbounded.append({"branch_id": key[0], "token": key[1], "reason": item.get("reason")})
        else:
            problems.append({"line": index, "reason": "unknown lease event", "event": event})
    for (branch, token), record in sorted(open_tokens.items(), key=lambda item: str(item[0])):
        problems.append({"reason": "unclosed lease window", "branch_id": branch, "token": token,
                         "wait_seconds": record.get("wait_seconds")})
    timed = sorted([item for item in intervals if type(item["start_ns"]) is int and type(item["end_ns"]) is int],
                   key=lambda item: item["start_ns"])
    overlaps = [{"left": [left["branch_id"], left["token"]], "right": [right["branch_id"], right["token"]],
                 "overlap_ns": left["end_ns"] - right["start_ns"]}
                for left, right in zip(timed, timed[1:]) if right["start_ns"] < left["end_ns"]]
    if expect is not None and acquires != expect:
        problems.append({"reason": f"expected {expect} lease windows, found {acquires}"})
    return {"journal": None if path is None else str(path), "acquires": acquires, "releases": releases,
            "abandoned": abandoned, "intervals": intervals, "unbounded": unbounded, "overlaps": overlaps,
            "problems": problems, "clock": MONOTONIC_CLOCK,
            "status": "passed" if not problems and not overlaps else "failed"}


class DeviceLease:
    """Exclusive device window: a named mutex (Windows) or lock file (POSIX).

    A worker appends its release record *before* dropping the lock, so the
    journal order is the ownership order. The lock is the isolation mechanism;
    the journal is only the evidence, and it is a shared control-plane file.
    """

    def __init__(self, task, binding):
        device = task["device"]
        self.policy = device["policy"]
        self.name = device["lease_name"]
        self.journal = Path(device["lease_journal"])
        self.timeout_seconds = device["lease_timeout_seconds"]
        self.branch_id, self.token = task["branch_id"], task["token"]
        self.plan_id, self.owner = task["plan_id"], binding.value
        self.handle = self.api = None
        self.acquired = False
        self.wait_seconds = None

    def acquire(self) -> dict:
        started, note = time.monotonic(), None
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            api.CreateMutexW.restype = wintypes.HANDLE
            api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            api.WaitForSingleObject.restype = wintypes.DWORD
            api.ReleaseMutex.argtypes = [wintypes.HANDLE]
            api.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = api.CreateMutexW(None, False, self.name)
            require(bool(handle), f"cannot create the device mutex: {ctypes.WinError(ctypes.get_last_error())}")
            self.handle, self.api = handle, api
            result = api.WaitForSingleObject(handle, max(1, int(self.timeout_seconds * 1000)))
            if result not in (0, 0x80):
                api.CloseHandle(handle)
                self.handle = None
                require(result != 0x102, f"the device lease was not granted within {self.timeout_seconds}s")
                raise EvidenceError(f"device lease wait failed with status 0x{result:x}")
            note = "the previous holder exited without releasing" if result == 0x80 else None
        else:
            import fcntl

            self.handle = os.open(self.journal.with_name(self.journal.name + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
            deadline = started + self.timeout_seconds
            while True:
                try:
                    fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        os.close(self.handle)
                        self.handle = None
                        raise EvidenceError(f"the device lease was not granted within {self.timeout_seconds}s")
                    time.sleep(0.02)
        self.acquired = True
        self.wait_seconds = round(time.monotonic() - started, 6)
        append_line(self.journal, {"event": "acquire", "branch_id": self.branch_id, "token": self.token,
                                   "plan_id": self.plan_id, "owner": self.owner, "policy": self.policy,
                                   "name": self.name, "wait_seconds": self.wait_seconds, "note": note,
                                   "monotonic_ns": time.monotonic_ns()})
        return {"policy": self.policy, "name": self.name, "acquired": True,
                "wait_seconds": self.wait_seconds, "note": note}

    def release(self, outcome: str) -> dict:
        if self.acquired:
            append_line(self.journal, {"event": "release", "branch_id": self.branch_id, "token": self.token,
                                       "owner": self.owner, "outcome": outcome,
                                       "monotonic_ns": time.monotonic_ns()})
            self.acquired = False
        self._unlock()
        return {"policy": self.policy, "name": self.name, "acquired": True, "released": True, "outcome": outcome}

    def _unlock(self):
        if self.handle is None:
            return
        if os.name == "nt":
            import ctypes

            if not self.api.ReleaseMutex(self.handle):
                error = ctypes.WinError(ctypes.get_last_error())
                self.api.CloseHandle(self.handle)
                self.handle = None
                raise error
            self.api.CloseHandle(self.handle)
        else:
            import fcntl

            fcntl.flock(self.handle, fcntl.LOCK_UN)
            os.close(self.handle)
        self.handle = None


def worker_environment(task, *, base=None) -> dict:
    """The environment one branch worker is allowed to start with."""
    environment = dict(os.environ if base is None else base)
    extra = [entry for entry in (task["environment"].get("PYTHONPATH") or "").split(os.pathsep) if entry]
    inherited = [entry for entry in str(environment.get("PYTHONPATH") or "").split(os.pathsep) if entry]
    environment["PYTHONPATH"] = os.pathsep.join(dict.fromkeys([str(SOURCE_ROOT), *extra, *inherited]))
    for key, value in task["environment"].items():
        if key != "PYTHONPATH":
            environment[key] = value
    return environment


def wait_for_arm(task, directory, binding, *, clock=time.monotonic, wait=time.sleep) -> dict:
    """No branch work at all before the parent armed this exact worker."""
    armed_path = directory / ARM_FILE
    parent = ProcessBinding(task["parent"]["pid"])
    try:
        require(parent.value == task["parent"], "the parent PID was reused before this worker was armed")
        deadline = clock() + task["arm_timeout_seconds"]
        while not armed_path.is_file():
            require(parent.alive(), "the parent exited before arming this worker")
            require(clock() < deadline, "the parent did not arm this worker in time")
            wait(0.01)
    finally:
        parent.close()
    armed = read_json(armed_path)
    require(isinstance(armed, dict), "the arm receipt must be an object")
    require(armed.get("token") == task["token"], "the arm receipt token differs")
    require(armed.get("owner") == binding.value, "the arm receipt belongs to another worker")
    require(type(armed.get("job_assigned")) is bool, "the arm receipt must state its job assignment")
    if os.name == "nt":
        require(armed["job_assigned"] is True, "a Windows branch worker must be assigned to an owned job")
    return armed


def collect_frames(record, task, directory) -> dict:
    """Validate one executor result, write ``frames.json`` and seal its digest."""
    require(isinstance(record, dict), "the executor must return a branch record object")
    mode = record.get("mode")
    require(isinstance(mode, str) and mode, "the branch record must name its mode")
    require(type(record.get("real_game")) is bool, "the branch record must state whether a real game process ran")
    raw = record.get("frames")
    require(isinstance(raw, list) and raw, "the branch record needs per-tick frames")
    frames = []
    for index, frame in enumerate(raw):
        tick, state = (frame.get("tick"), frame.get("state")) if isinstance(frame, dict) else (None, None)
        require(type(tick) is int and tick >= 0, f"frame {index} needs a nonnegative integer tick")
        require(isinstance(state, dict) and state, f"frame {index} needs a state object")
        frames.append({"tick": tick, "state": copy.deepcopy(state)})
    ticks = [frame["tick"] for frame in frames]
    require(ticks[0] == 0, "the first frame must be the tick-zero restore boundary")
    require(ticks == sorted(ticks), "frames must be in non-decreasing tick order")
    if task.get("end") is not None:
        require(ticks[-1] >= task["end"]["tick"],
                f"frames stop at tick {ticks[-1]}, before the branch end boundary {task['end']['tick']}")
    identity, binding = record.get("identity"), "not_declared"
    if identity is not None:
        require(isinstance(identity, dict) and identity.get("game") == task["root_identity"]["game"]
                and identity.get("artifacts") == task["root_identity"]["artifacts"],
                "the run identity does not match the sealed root identity")
        binding = "matched"
    path = Path(directory) / FRAMES_FILE
    path.write_text(json.dumps({"schema": FRAMES_SCHEMA, "branch_id": task["branch_id"], "node": task.get("node"),
                                "steps_sha256": task["steps_sha256"], "tick_range": [ticks[0], ticks[-1]],
                                "frames": frames}, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n",
                    encoding="utf-8", newline="\n")
    return {"status": "pass", "mode": mode, "real_game": record["real_game"], "note": record.get("note"),
            "identity": copy.deepcopy(identity), "identity_binding": binding, "root_binding": binding,
            "restore": copy.deepcopy(record.get("restore")),
            "frames": {"path": FRAMES_FILE, "sha256": sha256(path), "count": len(frames),
                       "ticks": [ticks[0], ticks[-1]]}}


def worker(task_path, *, clock=time.monotonic, wait=time.sleep) -> int:
    """One branch worker: prepare, arm, execute, seal. Never deletes evidence."""
    task_path = Path(task_path).resolve()
    directory = task_path.parent
    task = read_json(task_path)
    require(isinstance(task, dict) and task.get("schema") == TASK_SCHEMA, "unknown branch worker protocol")
    require(Path(task["directory"]).resolve() == directory, "the task file must live in its branch directory")
    binding = ProcessBinding(os.getpid())
    receipt = {"schema": RECEIPT_SCHEMA, "token": task["token"], "plan_id": task["plan_id"],
               "branch_id": task["branch_id"], "node": task.get("node"), "executor": task["executor"],
               "owner": binding.value, "owner_binding": binding.kind, "parent": task["parent"],
               "host": task["host"], "status": "fail", "result": None, "error": None, "lease": None,
               "environment": {key: os.environ.get(key) for key in sorted(os.environ) if key.startswith("LVZ_")},
               "started_monotonic": clock(), "clock": MONOTONIC_CLOCK}
    lease, stage = None, "prepare"
    try:
        require(directory.is_dir(), "the branch directory is missing")
        require(steps_digest(task["steps"]) == task["steps_sha256"], "the dispatched action sequence changed")
        require(package_identity() == task["host"], "the worker host source differs from the dispatched plan")
        stage = "arm"
        receipt["arm"] = wait_for_arm(task, directory, binding, clock=clock, wait=wait)
        stage = "execute"
        executor, resolved = load_executor(task["executor"])
        receipt["resolved_executor"] = resolved
        if task["device"]["policy"] == "exclusive_lease":
            lease = DeviceLease(task, binding)
            receipt["lease"] = lease.acquire()
        receipt["result"] = collect_frames(executor(copy.deepcopy(task), directory), task, directory)
        receipt["status"] = "pass"
    except BaseException as error:
        receipt["error"] = {"stage": stage, **error_detail(error)}
    finally:
        if lease is not None:
            try:
                receipt["lease"] = {**(receipt["lease"] or {}),
                                    **lease.release("released" if receipt["status"] == "pass" else "failed")}
            except BaseException as error:
                receipt["status"] = "fail"
                receipt["error"] = receipt["error"] or {"stage": "lease", **error_detail(error)}
                receipt.setdefault("secondary_errors", []).append({"stage": "lease_release", **error_detail(error)})
        receipt["evidence"] = evidence_manifest(directory)
        receipt["finished_monotonic"] = clock()
        atomic(directory / RECEIPT_FILE, receipt)
        binding.close()
    return 0 if receipt["status"] == "pass" else 1


class BranchJob:
    """An unnamed, non-inheritable Windows job owning exactly one branch tree.

    Closing the handle kills that branch tree only: a sibling branch lives in
    its own job, so one branch's failure can never terminate another's work.
    POSIX hosts fall back to a fresh session per worker (see
    :meth:`WorkerProcess.terminate_tree`).
    """

    def __init__(self):
        require(os.name == "nt", "Windows Job Objects are required")
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                        ("active_limit", wintypes.DWORD), ("affinity", ctypes.c_size_t),
                        ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", ctypes.c_ulonglong * 6), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                        ("peak_job", ctypes.c_size_t)]

        class Accounting(ctypes.Structure):
            _fields_ = [("user", ctypes.c_longlong), ("kernel", ctypes.c_longlong),
                        ("period_user", ctypes.c_longlong), ("period_kernel", ctypes.c_longlong),
                        ("faults", wintypes.DWORD), ("total", wintypes.DWORD), ("active", wintypes.DWORD),
                        ("terminated", wintypes.DWORD)]

        self.Accounting = Accounting
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                      wintypes.DWORD, ctypes.c_void_p]
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
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

    def active(self) -> int:
        value = self.Accounting()
        if not self.api.QueryInformationJobObject(self.handle, 1, ctypes.byref(value), ctypes.sizeof(value), None):
            raise OSError("cannot query the branch job")
        return int(value.active)

    def wait_empty(self, seconds=5.0) -> int:
        """Process exit can precede descendant rundown; still require a true zero."""
        deadline, active = time.monotonic() + seconds, self.active()
        while active and time.monotonic() < deadline:
            time.sleep(0.025)
            active = self.active()
        return active

    def close(self):
        if self.handle:
            handle, self.handle = self.handle, None
            if not self.api.CloseHandle(handle):
                raise OSError("cannot close the branch job")


class WorkerProcess:
    """One branch worker host: fresh directory, owned job, arm receipt, receipt."""

    def __init__(self, task, workspace, *, clock=time.monotonic, environ=None):
        self.task, self.clock = task, clock
        self.directory = Path(workspace) / BRANCHES_DIRECTORY / task["branch_id"]
        self.directory.mkdir(parents=True, exist_ok=False)
        for name in BRANCH_DIRECTORIES:
            (self.directory / name).mkdir()
        atomic(self.directory / TASK_FILE, task)
        self.started = clock()
        self.deadline = self.started + task["timeout_seconds"]
        self.job = BranchJob() if os.name == "nt" else None
        self.process = self.stdout = self.stderr = None
        self.owner = None
        try:
            self.stdout = (self.directory / "stdout.log").open("xb")
            self.stderr = (self.directory / "stderr.log").open("xb")
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
            self.process = subprocess.Popen(
                [sys.executable, "-u", "-m", "llm_vs_zombies.branch_workers", "--worker",
                 str(self.directory / TASK_FILE)],
                cwd=str(self.directory), env=worker_environment(task, base=environ), stdin=subprocess.DEVNULL,
                stdout=self.stdout, stderr=self.stderr, close_fds=True, **options)
            binding = ProcessBinding(self.process.pid)
            try:
                self.owner = binding.value
                if self.job is not None:
                    self.job.assign(self.process)
                atomic(self.directory / ARM_FILE, {"token": task["token"], "owner": self.owner,
                                                   "job_assigned": self.job is not None})
            finally:
                binding.close()
        except BaseException as primary:
            try:
                self.terminate_tree()
            except BaseException as cleanup_error:
                primary.add_note(f"unarmed branch termination failed: {cleanup_error!r}")
            try:
                self.close()
            except BaseException as cleanup_error:
                primary.add_note(f"unarmed branch cleanup failed: {error_detail(cleanup_error)!r}")
            raise

    def poll(self):
        return None if self.process is None else self.process.poll()

    def terminate_tree(self):
        """Kill this branch tree only; never touch a sibling branch."""
        if self.process is None:
            return
        if self.job is not None:
            self.job.close()
            return
        import signal

        try:
            os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            self.process.kill()

    def _host_process(self, *, active, killed) -> dict:
        return {"owner": self.owner, "returncode": self.process.returncode, "killed": killed,
                "active_descendants_after_exit": active, "dispatch_monotonic": self.started,
                "exit_monotonic": self.clock(), "deadline_monotonic": self.deadline}

    def _read_receipt(self) -> dict:
        path = self.directory / RECEIPT_FILE
        require(path.is_file(), "the branch worker left no receipt")
        receipt = read_json(path)
        require(isinstance(receipt, dict) and receipt.get("schema") == RECEIPT_SCHEMA, "unknown branch worker receipt")
        for key in ("token", "plan_id", "branch_id", "executor", "host", "parent"):
            require(receipt.get(key) == self.task[key], f"the receipt disagrees with its dispatched task: {key}")
        require(receipt.get("owner") == self.owner, "the receipt belongs to another worker process")
        environment = receipt.get("environment")
        require(isinstance(environment, dict), "the receipt must record the worker environment")
        require(environment.get("LVZ_BRANCH_ID") == self.task["branch_id"],
                "the worker saw a foreign branch identity")
        require(environment.get("LVZ_BRANCH_DIR")
                and Path(environment["LVZ_BRANCH_DIR"]).resolve() == self.directory.resolve(),
                "the worker saw a foreign branch directory")
        require(receipt.get("status") in ("pass", "fail"), "the receipt must state a pass or fail status")
        require(isinstance(receipt.get("evidence"), dict), "the receipt must seal the branch evidence manifest")
        require((self.process.returncode == 0) == (receipt["status"] == "pass"),
                "the worker exit code disagrees with its receipt")
        if receipt["status"] == "pass":
            result = receipt.get("result")
            require(isinstance(result, dict) and result.get("status") == "pass",
                    "a passing receipt needs a passing result")
            frames = result.get("frames")
            require(isinstance(frames, dict) and frames.get("count", 0) > 0, "a passing receipt needs frame evidence")
            evidence = (self.directory / str(frames.get("path"))).resolve()
            require(evidence.is_relative_to(self.directory.resolve()) and evidence.is_file(),
                    "frame evidence escaped the branch directory")
            require(sha256(evidence) == frames.get("sha256"), "frame evidence changed after the worker sealed it")
            document = read_json(evidence)
            require(document.get("schema") == FRAMES_SCHEMA and len(document.get("frames", [])) == frames["count"]
                    and document.get("steps_sha256") == self.task["steps_sha256"],
                    "frame evidence does not match the dispatched branch")
        else:
            require(receipt.get("error") is not None, "a failing receipt must keep its error detail")
        return {"path": RECEIPT_FILE, "sha256": sha256(path), "document": receipt}

    def _seal_failure(self, detail) -> dict | None:
        """Keep the failure where it happened; the parent never rewrites evidence."""
        document = {"schema": RECEIPT_SCHEMA + ".failure", "branch_id": self.task["branch_id"],
                    "plan_id": self.task["plan_id"], "token": self.task["token"], "detail": detail,
                    "monotonic_ns": time.monotonic_ns()}
        try:
            atomic(self.directory / FAILURE_FILE, document)
        except BaseException as error:
            return {"path": None, "error": error_detail(error)}
        return {"path": FAILURE_FILE, "sha256": sha256(self.directory / FAILURE_FILE)}

    def collect(self, *, reason=None, killed=False) -> dict:
        """Collect one finished branch worker; a failure stays this branch's own."""
        active, mutation, receipt = None, None, None
        try:
            require(self.process.poll() is not None, "a branch worker must really exit before its result is collected")
            self.process.wait(timeout=0)
            if self.job is not None:
                active = self.job.wait_empty()
                require(active == 0, "a descendant of this branch survived its worker exit")
            receipt = self._read_receipt()
            document = receipt["document"]
            mutation = manifest_difference(document["evidence"], evidence_manifest(self.directory))
            result = {"branch_id": self.task["branch_id"],
                      "status": "passed" if document["status"] == "pass" else "failed",
                      "receipt": receipt, "error": copy.deepcopy(document.get("error"))}
            require(not any(mutation.values()),
                    f"branch evidence changed after its worker sealed it: {mutation}")
        except BaseException as error:
            result = {"branch_id": self.task["branch_id"], "status": "failed", "receipt": receipt,
                      "error": {"stage": "collect", **error_detail(error)}}
        if mutation is not None and any(mutation.values()):
            result["evidence_mutation"] = mutation
        if reason is not None:
            result["error"] = {"stage": "deadline", "type": "EvidenceError", "message": reason,
                               "collection": result["error"]}
            result["status"] = "failed"
            result["reason"] = reason
        if result["status"] == "failed" and (result["receipt"] is None or reason is not None):
            # A receipted failure already seals itself; the parent only seals the
            # failures it observed on its own (deadline, unreadable receipt).
            result["failure_seal"] = self._seal_failure(result["error"])
        result["host_process"] = self._host_process(active=active, killed=killed)
        result["manifest"] = file_manifest(self.directory)
        result["close_error"] = self.close()
        return result

    def emergency_finish(self, reason) -> dict:
        """Deadline path: kill this branch tree, then collect whatever it kept."""
        killed = False
        try:
            if self.process.poll() is None:
                killed = True
                self.terminate_tree()
            self.process.wait(timeout=30)
        except BaseException as error:
            reason = f"{reason}; termination failed: {error!r}"
        return self.collect(reason=reason, killed=killed)

    def close(self):
        """Close owned handles; returns an error detail instead of raising."""
        errors = []
        for label, operation in (("job close", None if self.job is None else self.job.close),
                                 ("worker wait", None if self.process is None
                                  else lambda: self.process.wait(timeout=30)),
                                 ("stdout close", None if self.stdout is None else self.stdout.close),
                                 ("stderr close", None if self.stderr is None else self.stderr.close)):
            if operation is None:
                continue
            try:
                operation()
            except BaseException as error:
                errors.append({"stage": label, **error_detail(error)})
        if errors:
            return {"errors": errors}
        return None


def task_for(plan, branch, workspace, spec, parent_value, *, workers) -> dict:
    """The exact dispatched unit of work: identity, action sequence, isolation."""
    directory = (Path(workspace) / BRANCHES_DIRECTORY / branch.branch_id).resolve()
    device = {"policy": plan.device["policy"], "lease_name": None,
              "lease_journal": str((Path(workspace) / LEASE_JOURNAL).resolve()),
              "lease_timeout_seconds": plan.device["lease_timeout_seconds"] or plan.timeout_seconds}
    if plan.device["policy"] == "exclusive_lease":
        device["lease_name"] = f"Local\\lvz-branch-device-{plan.plan_id[:40]}"
    environment = {"LVZ_BRANCH_ID": branch.branch_id, "LVZ_PLAN_ID": plan.plan_id,
                   "LVZ_BRANCH_DIR": str(directory),
                   "LVZ_BRANCH_PROFILE": str(directory / "profile"),
                   "LVZ_BRANCH_LOGS": str(directory / "logs"),
                   "LVZ_BRANCH_ARTIFACTS": str(directory / "artifacts"),
                   "LVZ_BRANCH_EVIDENCE": str(directory / "evidence"),
                   "LVZ_DEVICE_POLICY": plan.device["policy"], "LVZ_MAX_WORKERS": str(workers),
                   **plan.env, **branch.env}
    return {"schema": TASK_SCHEMA, "plan_id": plan.plan_id, "token": uuid.uuid4().hex,
            "branch_id": branch.branch_id, "node": branch.node, "tree": copy.deepcopy(branch.tree),
            "directory": str(directory), "executor": spec, "steps": copy.deepcopy(list(branch.steps)),
            "steps_sha256": branch.steps_sha256, "root_identity": copy.deepcopy(plan.root_identity),
            "end": copy.deepcopy(branch.end), "device": device, "environment": environment,
            "timeout_seconds": plan.timeout_seconds,
            "arm_timeout_seconds": min(120.0, plan.timeout_seconds), "parent": copy.deepcopy(parent_value),
            "host": package_identity()}


def compare_branch_frames(workspace, left_id, right_id) -> dict:
    """Frame-by-frame P1 comparison of two branches that ran the same actions."""

    def load(branch_id):
        path = Path(workspace) / BRANCHES_DIRECTORY / branch_id / FRAMES_FILE
        require(path.is_file(), f"branch {branch_id} has no frame evidence")
        document = read_json(path)
        require(document.get("schema") == FRAMES_SCHEMA and isinstance(document.get("frames"), list),
                f"branch {branch_id} frame evidence is malformed")
        return document["frames"], sha256(path)

    left, left_sha = load(left_id)
    right, right_sha = load(right_id)
    base = {"left": left_id, "right": right_id, "left_sha256": left_sha, "right_sha256": right_sha,
            "scope": "frame order, tick and canonical audit state"}
    left_ticks = [frame["tick"] for frame in left]
    right_ticks = [frame["tick"] for frame in right]
    if len(left) != len(right):
        return {**base, "equal": False, "reason": "frame_count", "left_frames": len(left),
                "right_frames": len(right)}
    if left_ticks != right_ticks:
        return {**base, "equal": False, "reason": "tick_sequence", "left_ticks": left_ticks,
                "right_ticks": right_ticks}
    for index, (first, second) in enumerate(zip(left, right)):
        difference = first_difference(first["state"], second["state"])
        if difference:
            return {**base, "equal": False, "reason": "state", "index": index, "tick": first["tick"],
                    "difference": difference}
    return {**base, "equal": True, "frames": len(left), "ticks": [left_ticks[0], left_ticks[-1]]}


def _limitations(real_game_run: bool) -> dict:
    return {"real_game_run": real_game_run, "original_engine_verified": False,
            "statement": ("every branch reports real_game=true; this report still certifies no original-engine "
                          "determinism claim" if real_game_run else
                          "no real game process was launched: every executed branch reports real_game=false"),
            "prerequisites": [
                "an a2 snapshot/restore implementation (N5, tools/process_snapshot.py) so a branch can start from "
                "the sealed tree root image at a pause boundary",
                "one executor that launches the controlled target (launcher + pinned runtime build + private "
                "profile), restores the root image, executes the branch steps and returns per-tick frames",
                "a sealed tree whose root identity equals the executor's run identity (N3/N7), otherwise the "
                "branches are not the same root and are not comparable",
                "N9 (virtual audio device) or device policy exclusive_lease for real runs; shared_declared only "
                "records that the shared device was accepted",
                "R4-style restore-interval, host-load and parallelism variation (N6 / N10) before any claim about "
                "parallel behaviour beyond this harness",
                "a real long-game run is out of scope here and needs the scoring pipeline's own gates, not these",
            ],
            "excluded": list(PIPELINE["excluded"])}


def _report_branch(result, branch, workspace) -> tuple[dict, dict]:
    directory = Path(workspace) / BRANCHES_DIRECTORY / branch.branch_id
    final = file_manifest(directory)
    mutation = manifest_difference(result["manifest"], final)
    for key, values in (result.get("evidence_mutation") or {}).items():
        mutation[key] = sorted(set(mutation[key]) | set(values))
    interfered = any(mutation.values())
    status, error = result["status"], result["error"]
    if interfered:
        status = "failed"
        error = {"stage": "immutability", "type": "EvidenceError",
                 "message": "branch evidence changed after its worker sealed it",
                 "interference": mutation, "cause": error}
    manifest_path = Path(workspace) / MANIFESTS_DIRECTORY / f"{branch.branch_id}.json"
    atomic(manifest_path, {"schema": RECEIPT_SCHEMA + ".manifest", "branch_id": branch.branch_id,
                           "completion": result["manifest"], "final": final, "mutation": mutation})
    receipt = result["receipt"]
    frames = None
    if receipt is not None and (receipt["document"].get("result") or {}).get("frames"):
        evidence = receipt["document"]["result"]["frames"]
        frames = {"path": f"{BRANCHES_DIRECTORY}/{branch.branch_id}/{evidence['path']}",
                  "sha256": evidence["sha256"], "count": evidence["count"], "ticks": evidence["ticks"]}
    record = {"branch_id": branch.branch_id, "node": branch.node, "tree": copy.deepcopy(branch.tree),
              "steps": len(branch.steps), "steps_sha256": branch.steps_sha256,
              "end": copy.deepcopy(branch.end), "status": status,
              "receipt": None if receipt is None else {"path": f"{BRANCHES_DIRECTORY}/{branch.branch_id}/"
                                                                 f"{receipt['path']}", "sha256": receipt["sha256"],
                                                        "document": receipt["document"]},
              "frames": frames, "failure_seal": result.get("failure_seal"), "error": error,
              "reason": result.get("reason"), "host_process": result["host_process"],
              "close_error": result.get("close_error"),
              "manifest": {"path": f"{MANIFESTS_DIRECTORY}/{branch.branch_id}.json", "sha256": sha256(manifest_path),
                           "completion": {"count": result["manifest"]["count"],
                                          "digest": result["manifest"]["digest"]},
                           "final": {"count": final["count"], "digest": final["digest"]},
                           "mutation": mutation}}
    return record, mutation


def run_branches(plan, workspace, *, executor_spec=None, max_workers=None, base_environment=None,
                 clock=time.monotonic, wait=time.sleep) -> tuple[dict, Path]:
    """Start one worker per branch, keep failures local, write ``report.json``."""
    if isinstance(plan, dict):
        plan = Plan.from_dict(plan)
    require(isinstance(plan, Plan), "run_branches needs a Plan or a plan mapping")
    spec = executor_spec or plan.executor
    require(isinstance(spec, str) and spec, "no executor: pass executor_spec or name one in the plan")
    spec = absolutize_executor(spec)
    workers = plan.max_workers if max_workers is None else max_workers
    require(isinstance(workers, int) and not isinstance(workers, bool) and 1 <= workers <= MAX_WORKERS_LIMIT,
            f"max_workers must be an integer in 1..{MAX_WORKERS_LIMIT}")
    require(workers <= len(plan.branches), "max_workers cannot exceed the number of branches")
    workspace = Path(workspace).resolve()
    try:
        workspace.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise EvidenceError(f"branch evidence is never overwritten; the workspace already exists: {workspace}")
    document = plan.to_dict()
    document.update(executor=spec, max_workers=workers)
    atomic(workspace / PLAN_FILE, document)
    (workspace / MANIFESTS_DIRECTORY).mkdir()
    parent = ProcessBinding(os.getpid())
    started, primary = clock(), None
    pending, running, results = list(plan.branches), {}, []
    try:
        while pending or running:
            for branch_id in list(running):
                handle = running[branch_id]
                if handle.poll() is not None:
                    over = clock() >= handle.deadline
                    results.append(handle.collect(reason=None if not over else
                                                  f"the branch finished after its {handle.task['timeout_seconds']}s "
                                                  "wall deadline"))
                    del running[branch_id]
                elif clock() >= handle.deadline:
                    results.append(handle.emergency_finish(
                        f"the branch exceeded its {handle.task['timeout_seconds']}s wall deadline"))
                    del running[branch_id]
            while pending and len(running) < workers:
                branch = pending.pop(0)
                task = task_for(plan, branch, workspace, spec, parent.value, workers=workers)
                try:
                    running[branch.branch_id] = WorkerProcess(task, workspace, clock=clock,
                                                              environ=base_environment)
                except BaseException as error:
                    results.append({"branch_id": branch.branch_id, "status": "failed", "receipt": None,
                                    "error": {"stage": "start", **error_detail(error)},
                                    "manifest": file_manifest(workspace / BRANCHES_DIRECTORY / branch.branch_id),
                                    "host_process": {"owner": None, "returncode": None, "killed": False,
                                                     "active_descendants_after_exit": None,
                                                     "dispatch_monotonic": clock(), "exit_monotonic": clock(),
                                                     "deadline_monotonic": None}})
            if running:
                wait(0.02)
    except BaseException as error:
        primary = error_detail(error)
    finally:
        while running:
            _, handle = running.popitem()
            try:
                results.append(handle.emergency_finish("the scheduler stopped; this branch was terminated"))
            except BaseException as error:
                results.append({"branch_id": handle.task["branch_id"], "status": "failed", "receipt": None,
                                "error": {"stage": "scheduler_cleanup", **error_detail(error)},
                                "manifest": file_manifest(handle.directory),
                                "host_process": {"owner": handle.owner, "returncode": None, "killed": True,
                                                 "active_descendants_after_exit": None,
                                                 "dispatch_monotonic": handle.started,
                                                 "exit_monotonic": clock(), "deadline_monotonic": handle.deadline}})
        parent.close()
    order = [branch.branch_id for branch in plan.branches]
    results.sort(key=lambda item: order.index(item["branch_id"]))
    lease_path = workspace / LEASE_JOURNAL
    if plan.device["policy"] == "exclusive_lease":
        for result in results:
            if result["status"] != "passed":
                result.setdefault("lease_seals", close_open_lease(
                    lease_path, result["branch_id"], "the branch did not release its device window"))
    branches, mutations = [], []
    for result, branch in zip(results, plan.branches):
        require(result["branch_id"] == branch.branch_id, "branch results lost their plan order")
        record, mutation = _report_branch(result, branch, workspace)
        if any(mutation.values()):
            mutations.append({"branch_id": branch.branch_id, **mutation})
        branches.append(record)
    by_id = {record["branch_id"]: record for record in branches}
    comparisons = []
    for group in plan.comparisons:
        members = group["branches"]
        missing = [member for member in members if by_id[member]["status"] != "passed"
                   or by_id[member]["frames"] is None]
        entry = {"name": group["name"], "branches": members,
                 "scope": "P1: one root and one action sequence, compared frame by frame; the action digest "
                          "keeps method, params and pre-state expect and drops volatile request ids"}
        if missing:
            entry.update(status="not_available", comparisons=[],
                         reason=f"branches without usable frames: {missing}")
        else:
            pairs = [compare_branch_frames(workspace, members[0], other) for other in members[1:]]
            entry.update(status="passed" if all(pair["equal"] for pair in pairs) else "failed", comparisons=pairs)
        comparisons.append(entry)
    lease = (verify_lease_journal(lease_path) if plan.device["policy"] == "exclusive_lease"
             else {"journal": None, "status": "not_applicable", "acquires": 0, "releases": 0, "abandoned": 0,
                   "intervals": [], "unbounded": [], "overlaps": [], "problems": [],
                   "reason": "the plan declares the shared device instead of a lease"})
    isolation = {"status": "failed" if mutations or lease["status"] == "failed" else "passed",
                 "interference": {"status": "failed" if mutations else "passed", "mutations": mutations,
                                  "rule": "a branch directory must be quiescent and unchanged once its worker "
                                          "exits; a foreign write invalidates that branch's evidence"},
                 "device": {"policy": plan.device["policy"], "accept_shared": plan.device["accept_shared"],
                            "reason": plan.device["reason"], "lease": lease},
                 "shared_resources": [copy.deepcopy(item) for item in SHARED_RESOURCES],
                 "layout": {"plan": PLAN_FILE, "branches": BRANCHES_DIRECTORY, "manifests": MANIFESTS_DIRECTORY,
                            "branch_directories": list(BRANCH_DIRECTORIES),
                            "lease_journal": None if plan.device["policy"] != "exclusive_lease" else LEASE_JOURNAL},
                 "process": {"model": "one owned worker process per branch",
                             "job_objects": os.name == "nt", "identity_binding": parent.kind,
                             "max_workers": workers,
                             "branch_processes": len({str(record["host_process"].get("owner", {}).get("pid"))
                                                      for record in branches
                                                      if record["host_process"].get("owner")})}}
    failed_branches = [record["branch_id"] for record in branches if record["status"] != "passed"]
    failed_comparisons = [entry["name"] for entry in comparisons if entry["status"] != "passed"]
    verdict = {"status": "passed" if not failed_branches and not failed_comparisons
               and isolation["status"] == "passed" and primary is None else "failed",
               "failed_branches": failed_branches, "failed_comparisons": failed_comparisons,
               "branch_status": {record["branch_id"]: record["status"] for record in branches}}
    real_game = any(((record["receipt"] or {}).get("document", {}).get("result") or {}).get("real_game")
                    for record in branches)
    report = {"schema": REPORT_SCHEMA, "plan_id": plan.plan_id,
              "plan": {"path": PLAN_FILE, "sha256": sha256(workspace / PLAN_FILE), "executor": spec,
                       "max_workers": workers, "timeout_seconds": plan.timeout_seconds,
                       "branch_order": order},
              "pipeline": copy.deepcopy(PIPELINE), "root_identity": copy.deepcopy(plan.root_identity),
              "isolation": {"workspace": str(workspace), "started_monotonic": started,
                            "finished_monotonic": clock(), **isolation},
              "branches": branches, "comparisons": comparisons, "verdict": verdict,
              "primary_error": primary, "limitations": _limitations(bool(real_game))}
    report["report_id"] = digest_bytes(report)
    write_json(workspace / REPORT_FILE, report)
    return report, workspace / REPORT_FILE


def verify_workspace(workspace) -> dict:
    """Re-check a finished workspace from disk, without re-running any branch."""
    workspace = Path(workspace).resolve()
    report = read_json(workspace / REPORT_FILE)
    require(isinstance(report, dict) and report.get("schema") == REPORT_SCHEMA,
            "not a branch-worker report")
    problems = []
    plan_sha = sha256(workspace / PLAN_FILE)
    if plan_sha != report["plan"]["sha256"]:
        problems.append({"reason": "plan changed", "expected": report["plan"]["sha256"], "actual": plan_sha})
    require(report.get("report_id") == digest_bytes({key: value for key, value in report.items()
                                                     if key != "report_id"}),
            "the report id does not match its contents")
    for record in report["branches"]:
        directory = workspace / BRANCHES_DIRECTORY / record["branch_id"]
        current = file_manifest(directory)
        manifest_path = workspace / record["manifest"]["path"]
        if not manifest_path.is_file() or sha256(manifest_path) != record["manifest"]["sha256"]:
            problems.append({"reason": "manifest evidence changed", "branch_id": record["branch_id"]})
            continue
        sealed = read_json(manifest_path)
        if sealed.get("final") != current or current["digest"] != record["manifest"]["final"]["digest"]:
            problems.append({"reason": "branch evidence changed after the report",
                             "branch_id": record["branch_id"],
                             "difference": manifest_difference(sealed.get("final", {}), current)})
    lease = (verify_lease_journal(workspace / LEASE_JOURNAL)
             if report["isolation"]["device"]["policy"] == "exclusive_lease"
             else {"status": "not_applicable", "problems": [], "overlaps": [], "unbounded": [], "acquires": 0})
    if lease["status"] == "failed":
        problems.append({"reason": "device lease journal no longer verifies", "lease": lease})
    comparisons = []
    for group in report["comparisons"]:
        if group["status"] == "not_available":
            comparisons.append({"name": group["name"], "status": "not_available", "reason": group["reason"]})
            continue
        members = group["branches"]
        pairs = [compare_branch_frames(workspace, members[0], other) for other in members[1:]]
        status = "passed" if all(pair["equal"] for pair in pairs) else "failed"
        expected = group["status"] == "passed"
        if (status == "passed") != expected:
            problems.append({"reason": "recomputed comparison disagrees with the report", "name": group["name"],
                             "report": group["status"], "recomputed": status})
        comparisons.append({"name": group["name"], "status": status, "comparisons": pairs})
    verdict = {"status": "passed" if not problems and all(item["status"] in ("passed", "not_available")
                                                          for item in comparisons) else "failed",
               "problems": problems, "comparisons": comparisons, "lease": lease}
    return {"schema": VERIFY_SCHEMA, "report_id": report["report_id"], "verdict": verdict,
            "branches": {record["branch_id"]: record["status"] for record in report["branches"]}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--worker", type=Path, required=True,
                        help="internal: run one dispatched branch worker task")
    arguments = parser.parse_args(argv)
    return worker(arguments.worker)


if __name__ == "__main__":
    raise SystemExit(main())
