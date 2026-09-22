"""Same-root branch self-check: re-run sampled tree nodes twice and compare frames.

N8 of the branch-data contract (``docs/分支数据合同与实施计划.md``, issue #36).
This tool never launches a game by itself. It consumes a sealed evidence tree
(``evidence_tree`` / ``package_tree`` output), samples nodes from it, and asks a
caller-supplied *executor* to re-execute each sampled node's recorded action
sequence twice from the tree root. Both runs are compared frame by frame, and
the rungs of the contract's recovery ladder are reported separately, so a later
failure never overwrites an earlier conclusion:

* R0  capture -> restore -> recapture is byte-identical (the a2 self-check);
* R1  the tick-zero boundary after the restore, without advancing;
* R2  one tick past the restore boundary;
* R3  the whole recorded run (N ticks) including the sampled branch edge.

R4 (restore interval, host load, parallelism) and every real-game claim stay
out of scope; the report marks both explicitly.

Evidence layout: the tool creates one immutable directory per run under
``<output>/runs/<node>/<run>/`` and writes the normalized run record next to
the executor's own raw evidence; the report goes to
``<output>/selfcheck-report.json``. An existing output directory is never
overwritten, so failure evidence from an earlier attempt cannot be lost.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from llm_vs_zombies.audit_compare import EvidenceError, canonical, first_difference
from llm_vs_zombies.engine_replay import Trajectory
from llm_vs_zombies.evidence_tree import EvidenceTree, end_boundary

SCHEMA = "lvz.branch-selfcheck.v1"
RUN_SCHEMA = "lvz.branch-selfcheck-run.v1"
REPORT_FILE = "selfcheck-report.json"
RUNS_DIRECTORY = "runs"
RUNS_RECORD = "record.json"
RUN_LABELS = ("run-a", "run-b")
RUNG_ORDER = ("r0", "r1", "r2", "r3")
RUNGS = {
    "r0": {"ladder": "capture -> restore -> recapture is byte-identical",
           "points_to": "restore is not the identity transform"},
    "r1": {"ladder": "post-restore audit state without advancing equals the recorded root boundary",
           "points_to": "capture / restore scope"},
    "r2": {"ladder": "one tick past the restore boundary",
           "points_to": "an environment difference is read for the first time"},
    "r3": {"ladder": "N ticks past the restore boundary (the whole recorded run)",
           "points_to": "slow coupling"},
    "r4": {"ladder": "restore interval, host load and parallelism varied",
           "points_to": "time and shared resources"},
}
COMPARISON_SCOPE = ("frame order, tick and canonical audit state; epoch and revision are mapped by the "
                    "executor and are not compared")
SAMPLE_LIMIT_DEFAULT = 4


@dataclass(frozen=True)
class Sample:
    """One planned self-check unit: a node's recorded action sequence from the root.

    The executor receives this object plus a fresh directory. It must restore or
    rebuild the tree root, execute ``steps`` in order, and return per-tick
    frames. Sealed frames are deliberately not part of this structure: the
    comparison stage reads them from the tree, not the executor.
    """

    key: str
    path: str
    branch_id: str
    trunk: bool
    tree_id: str
    trajectory_id: str
    chain_sha256: str
    parent_key: str | None
    parent_trajectory_id: str | None
    fork: dict | None
    end: dict
    root_identity: dict
    trajectory: str
    steps: tuple

    @property
    def edge_ticks(self) -> int:
        return self.end["tick"] - (self.fork["tick"] if self.fork is not None else 0)

    def binding(self) -> dict:
        """Everything the report uses to bind this sample to a tree and a branch."""
        return {"tree_id": self.tree_id, "node": self.key, "path": self.path, "branch_id": self.branch_id,
                "trunk": self.trunk, "trajectory_id": self.trajectory_id, "chain_sha256": self.chain_sha256,
                "parent_key": self.parent_key, "parent_trajectory_id": self.parent_trajectory_id,
                "fork": copy.deepcopy(self.fork), "end": copy.deepcopy(self.end), "edge_ticks": self.edge_ticks}

    def request(self) -> dict:
        """What was handed to the executor, without repeating the action sequence."""
        return {"node": self.key, "trajectory": self.trajectory, "fork": copy.deepcopy(self.fork),
                "end": copy.deepcopy(self.end), "steps": len(self.steps), "edge_ticks": self.edge_ticks}


def node_records(tree: EvidenceTree) -> dict[str, dict]:
    """Load every placed node bundle after the tree itself has been re-derived."""
    records = {}
    for entry in tree.record["nodes"]:
        trajectory = Trajectory.load(tree.directory / entry["path"])
        records[entry["key"]] = {"entry": entry, "trajectory": trajectory,
                                 "placement": trajectory.manifest["tree"]}
    return records


def _even_pick(items: list, count: int) -> list:
    if count <= 0 or not items:
        return []
    if count >= len(items):
        return list(items)
    if count == 1:
        return [items[len(items) // 2]]
    indices = sorted({round(index * (len(items) - 1) / (count - 1)) for index in range(count)})
    return [items[index] for index in indices]


def plan_samples(tree: EvidenceTree, nodes: dict[str, dict], *, limit=SAMPLE_LIMIT_DEFAULT,
                 requested=None) -> tuple[list[str], dict]:
    """Choose the sampled nodes and describe the coverage of that choice.

    The root and every branch origin are mandatory: a self-check that skips the
    only place where a branch departs from its parent proves nothing about that
    branch. Remaining budget is spread evenly over the tree index, so the choice
    is deterministic and reproducible from the report alone.
    """
    order = [entry["key"] for entry in tree.record["nodes"]]
    root = tree.record["root"]
    origins = [key for key in order if nodes[key]["entry"]["parent_key"] is None
               or nodes[nodes[key]["entry"]["parent_key"]]["entry"]["branch_id"] != nodes[key]["entry"]["branch_id"]]
    mandatory = [root] + [key for key in origins if key != root]
    if requested:
        unknown = [key for key in requested if key not in nodes]
        if unknown:
            raise EvidenceError(f"unknown sampled node keys: {', '.join(sorted(unknown))}")
        if len(set(requested)) != len(requested):
            raise EvidenceError("sampled node keys must be unique")
        selected, strategy = [key for key in order if key in set(requested)], "requested_nodes"
    elif limit == "all":
        selected, strategy = list(order), "all_nodes"
    else:
        if type(limit) is not int or limit < 1:
            raise EvidenceError("sample limit must be a positive integer or 'all'")
        if len(mandatory) > limit:
            raise EvidenceError(
                f"sample limit {limit} is below the mandatory root + branch origin set ({', '.join(mandatory)}); "
                f"raise --sample-limit to at least {len(mandatory)} or use 'all'")
        extra = _even_pick([key for key in order if key not in mandatory], limit - len(mandatory))
        chosen = set(mandatory) | set(extra)
        selected = [key for key in order if key in chosen]
        strategy = f"root+branch_origins+even_spacing(limit={limit})"
    branch_members: dict[str, list[str]] = {}
    for key in order:
        branch_members.setdefault(nodes[key]["entry"]["branch_id"], []).append(key)
    total_ticks = sum(_node_ticks(nodes[key]) for key in order)
    sampled_ticks = sum(_node_ticks(nodes[key]) for key in selected)
    branches_sampled = [branch for branch, members in branch_members.items()
                        if any(key in selected for key in members)]
    coverage = {
        "strategy": strategy,
        "limit": limit,
        "nodes_total": len(order), "nodes_sampled": len(selected),
        "node_fraction": round(len(selected) / len(order), 6),
        "branches_total": len(branch_members), "branches_sampled": len(branches_sampled),
        "branch_fraction": round(len(branches_sampled) / len(branch_members), 6),
        "edge_ticks_total": total_ticks, "edge_ticks_sampled": sampled_ticks,
        "edge_tick_fraction": round(sampled_ticks / total_ticks, 6) if total_ticks else None,
        "branches": {branch: {"nodes": members, "sampled": [key for key in members if key in selected]}
                     for branch, members in sorted(branch_members.items())},
        "selected": selected, "skipped": [key for key in order if key not in selected],
        "scope": ("one sample per node; a sampled node's edge ticks are counted once even when another sampled "
                  "node shares its prefix"),
    }
    return selected, coverage


def _node_ticks(node: dict) -> int:
    placement = node["placement"]
    fork = placement["parent"]["boundary"] if placement["parent"] is not None else None
    return end_boundary(node["trajectory"])["tick"] - (fork["tick"] if fork is not None else 0)


def build_sample(tree: EvidenceTree, nodes: dict[str, dict], key: str) -> Sample:
    node = nodes[key]
    entry, placement = node["entry"], node["placement"]
    root = nodes[tree.record["root"]]
    return Sample(
        key=key, path=entry["path"], branch_id=placement["branch_id"], trunk=placement["trunk"],
        tree_id=placement["tree_id"], trajectory_id=entry["trajectory_id"],
        chain_sha256=placement["chain"]["sha256"], parent_key=entry["parent_key"],
        parent_trajectory_id=None if placement["parent"] is None else placement["parent"]["trajectory_id"],
        fork=None if placement["parent"] is None else copy.deepcopy(placement["parent"]["boundary"]),
        end=end_boundary(node["trajectory"]), root_identity=copy.deepcopy(root["placement"]["root"]["identity"]),
        trajectory=str(tree.directory / entry["path"]), steps=tuple(copy.deepcopy(node["trajectory"].steps)))


def sealed_frames(trajectory: Trajectory) -> list[dict]:
    """The node's recorded boundary states: the tick-zero marker plus every post_step frame."""
    frames = [{"tick": 0, "kind": "initial", "seq": None, "version": copy.deepcopy(trajectory.initial["observation"]["version"]),
               "state": copy.deepcopy(trajectory.initial["state"]), "source": "initial marker"}]
    for frame in trajectory.audit.frames:
        if frame.kind == "post_step":
            frames.append({"tick": frame.version["tick"], "kind": frame.kind, "seq": frame.seq,
                           "version": copy.deepcopy(frame.version), "state": frame.state, "source": "audit post_step"})
    ticks = [frame["tick"] for frame in frames]
    if ticks[0] != 0 or ticks != sorted(ticks):
        raise EvidenceError(f"sealed boundary timeline is not ordered: {ticks}")
    return frames


def _boundary_frame(frames: list[dict], boundary: dict) -> dict | None:
    exact = [frame for frame in frames if frame["version"] == boundary]
    if exact:
        return {"frame": exact[-1], "match": "version"}
    same_tick = [frame for frame in frames if frame["tick"] == boundary["tick"]]
    if same_tick:
        return {"frame": same_tick[-1], "match": "tick"}
    return None


def compare_frames(left: list[dict], right: list[dict], *, left_label: str, right_label: str) -> dict:
    """Frame-by-frame comparison; the first mismatch is the reported divergence."""
    for index, (a, b) in enumerate(zip(left, right)):
        if a["tick"] != b["tick"]:
            return {"equal": False, "left": left_label, "right": right_label, "reason": "boundary",
                    "index": index, "left_tick": a["tick"], "right_tick": b["tick"]}
        if canonical(a["state"]) != canonical(b["state"]):
            return {"equal": False, "left": left_label, "right": right_label, "reason": "state", "index": index,
                    "tick": a["tick"], "difference": first_difference(a["state"], b["state"]),
                    "left_sha256": hashlib.sha256(canonical(a["state"])).hexdigest(),
                    "right_sha256": hashlib.sha256(canonical(b["state"])).hexdigest()}
    if len(left) != len(right):
        return {"equal": False, "left": left_label, "right": right_label, "reason": "frame_count",
                "left_frames": len(left), "right_frames": len(right)}
    return {"equal": True, "left": left_label, "right": right_label, "frames": len(left)}


def _at_tick(frames: list[dict], tick: int) -> list[dict]:
    return [frame for frame in frames if frame["tick"] == tick]


def _slice(frames: list[dict], rung: str) -> list[dict]:
    if rung == "r1":
        return _at_tick(frames, 0)
    if rung == "r2":
        return _at_tick(frames, 1)
    return [frame for frame in frames if frame["tick"] >= 1]


def _finish_rung(rung: str, status: str, comparisons: list[dict], *, ticks=None, note=None, blocked_by=None) -> dict:
    result = {"rung": rung, "status": status, "ladder": RUNGS[rung]["ladder"],
              "points_to": RUNGS[rung]["points_to"], "comparisons": comparisons}
    if ticks is not None:
        result["ticks"] = ticks
    failure = next((item for item in comparisons if item.get("equal") is False), None)
    if failure is not None:
        result["first_difference"] = copy.deepcopy(failure)
    if note is not None:
        result["note"] = note
    if blocked_by is not None:
        result["blocked_by"] = blocked_by
    return result


def _not_reached(rung: str, blocked_by: str) -> dict:
    return _finish_rung(rung, "not_reached", [], note=f"{blocked_by} failed; later rungs are not evaluated",
                        blocked_by=blocked_by)


def check_restore(runs: list[dict], root_identity: dict) -> dict:
    """R0: every run must prove that restoring the root image was the identity transform."""
    entries = []
    for label, run in zip(RUN_LABELS, runs):
        restore = run.get("restore")
        if not isinstance(restore, dict) or restore.get("attempted") is not True:
            entries.append({"run": label, "attempted": False,
                            "note": "this run reports no restore step; R0 does not apply to a rebuild-from-scratch executor"})
            continue
        equal = restore.get("equal", restore.get("r0"))
        entry = {"run": label, "attempted": True, "equal": None if equal is None else bool(equal),
                 "method": restore.get("method"), "restore": copy.deepcopy(restore)}
        image = run.get("root_image_sha256") or restore.get("image_sha256")
        recorded = root_identity.get("image_sha256")
        if image is None or recorded is None:
            entry["image_binding"] = "unavailable"
        elif image == recorded:
            entry["image_binding"] = "matched"
        else:
            entry["image_binding"] = "mismatch"
            entry["restored_image_sha256"] = image
            entry["recorded_image_sha256"] = recorded
        entries.append(entry)
    if not any(entry["attempted"] for entry in entries):
        return _finish_rung("r0", "not_applicable", [], note="no run reports a restore step; R0 needs a restore-based executor")
    comparisons = []
    for entry in entries:
        if not entry["attempted"]:
            comparisons.append({"equal": False, "left": entry["run"], "right": "restore",
                                "reason": "restore_not_attempted"})
        elif entry["equal"] is not True:
            comparisons.append({"equal": False, "left": entry["run"], "right": "restore", "reason": "restore_not_equal",
                                "restore": entry.get("restore")})
        elif entry["image_binding"] == "mismatch":
            comparisons.append({"equal": False, "left": entry["run"], "right": "root_image", "reason": "image_digest",
                                "restored_image_sha256": entry["restored_image_sha256"],
                                "recorded_image_sha256": entry["recorded_image_sha256"]})
        else:
            comparisons.append({"equal": True, "left": entry["run"], "right": "restore",
                                "image_binding": entry["image_binding"]})
    status = "passed" if all(item["equal"] for item in comparisons) else "failed"
    result = _finish_rung("r0", status, comparisons)
    result["runs"] = entries
    if any(entry.get("image_binding") == "unavailable" for entry in entries if entry["attempted"]):
        result["note"] = "the root identity carries no image digest or the run reported none; the image binding is unavailable"
    return result


def check_rung(rung: str, runs: list[dict], reference: dict | None, *, use_reference: bool) -> dict:
    left = _slice(runs[0]["frames"], rung)
    right = _slice(runs[1]["frames"], rung)
    if not left and not right:
        return _finish_rung(rung, "not_available", [],
                            note=f"the executed runs contain no boundary for {RUNGS[rung]['ladder']}")
    comparisons = [compare_frames(left, right, left_label=RUN_LABELS[0], right_label=RUN_LABELS[1])]
    note = None
    if use_reference:
        sealed = _slice(reference["frames"], rung) if reference else []
        if not sealed:
            return _finish_rung(rung, "not_available", comparisons,
                                note="the sealed reference has no boundary for this rung")
        comparisons.append(compare_frames(left, sealed, left_label=RUN_LABELS[0], right_label="sealed"))
        comparisons.append(compare_frames(right, sealed, left_label=RUN_LABELS[1], right_label="sealed"))
    else:
        note = "no sealed reference was requested; only the two fresh runs were compared"
    status = "passed" if all(item["equal"] for item in comparisons) else "failed"
    return _finish_rung(rung, status, comparisons, ticks=sorted({frame["tick"] for frame in left + right}), note=note)


def check_fork(sample: Sample, runs: list[dict], reference: dict | None, *, use_reference: bool) -> dict:
    """Diagnostic (not a rung): the boundary where this node leaves its parent."""
    ladder = "the sealed parent boundary is reproduced at the fork"
    if sample.fork is None:
        return {"check": "fork", "status": "not_applicable", "ladder": ladder,
                "note": "the sampled node is the tree root; every other node is checked against it"}
    left, right = _at_tick(runs[0]["frames"], sample.fork["tick"]), _at_tick(runs[1]["frames"], sample.fork["tick"])
    if not left or not right:
        return {"check": "fork", "status": "not_available", "ladder": ladder,
                "note": f"a run has no frame at the fork boundary tick {sample.fork['tick']}"}
    comparisons = [compare_frames(left, right, left_label=RUN_LABELS[0], right_label=RUN_LABELS[1])]
    result = {"check": "fork", "ladder": ladder, "comparisons": comparisons, "boundary": copy.deepcopy(sample.fork)}
    if use_reference:
        parent = (reference or {}).get("parent_boundary")
        if parent is None:
            result.update(status="not_available",
                          note=f"the sealed parent {sample.parent_key} has no frame at the fork boundary")
        else:
            sealed = [parent["frame"]]
            comparisons.append(compare_frames(left, sealed, left_label=RUN_LABELS[0], right_label="sealed_parent"))
            comparisons.append(compare_frames(right, sealed, left_label=RUN_LABELS[1], right_label="sealed_parent"))
            result["reference"] = {"node": sample.parent_key, "match": parent["match"], "seq": parent["frame"]["seq"],
                                   "tick": parent["frame"]["tick"]}
    if "status" not in result:
        result["status"] = "passed" if all(item["equal"] for item in comparisons) else "failed"
    failure = next((item for item in comparisons if item.get("equal") is False), None)
    if failure is not None:
        result["first_difference"] = copy.deepcopy(failure)
    return result


def check_sample(sample: Sample, runs: list[dict], reference: dict | None, *, use_reference: bool) -> dict:
    """Evaluate the R0-R3 ladder for one sample, keeping every reached conclusion."""
    rungs = {"r0": check_restore(runs, sample.root_identity)}
    blocked = "r0" if rungs["r0"]["status"] == "failed" else None
    for rung in ("r1", "r2", "r3"):
        if blocked is not None:
            rungs[rung] = _not_reached(rung, blocked)
            continue
        rungs[rung] = check_rung(rung, runs, reference, use_reference=use_reference)
        if rungs[rung]["status"] == "failed":
            blocked = rung
    fork = (check_fork(sample, runs, reference, use_reference=use_reference) if blocked is None
            else {"check": "fork", "status": "not_reached", "blocked_by": blocked})
    divergence = _first_divergence(sample, rungs, fork)
    report = {"node": sample.key, "branch_id": sample.branch_id,
              "binding": sample.binding(), "request": sample.request(),
              "rungs": rungs, "fork": fork, "first_divergence": divergence,
              "evidence": {label: f"{RUNS_DIRECTORY}/{sample.key}/{label}/{RUNS_RECORD}" for label in RUN_LABELS}}
    report["status"] = ("failed" if any(rung["status"] == "failed" for rung in rungs.values())
                        else "passed" if all(rung["status"] in {"passed", "not_applicable"} for rung in rungs.values())
                        else "incomplete")
    return report


def _first_divergence(sample: Sample, rungs: dict, fork: dict) -> dict | None:
    for rung in RUNG_ORDER:
        failure = rungs[rung].get("first_difference")
        if failure is not None:
            return {"node": sample.key, "rung": rung, "ladder": RUNGS[rung]["ladder"],
                    "points_to": RUNGS[rung]["points_to"], "comparison": copy.deepcopy(failure),
                    "evidence": {label: f"{RUNS_DIRECTORY}/{sample.key}/{label}/{RUNS_RECORD}" for label in RUN_LABELS}}
    if fork.get("first_difference") is not None:
        return {"node": sample.key, "rung": "fork", "ladder": fork.get("ladder"),
                "comparison": copy.deepcopy(fork["first_difference"]),
                "evidence": {label: f"{RUNS_DIRECTORY}/{sample.key}/{label}/{RUNS_RECORD}" for label in RUN_LABELS}}
    return None


def normalize_run(label: str, record: dict, sample: Sample) -> dict:
    """Validate one executor result and reduce it to the comparable evidence."""
    if not isinstance(record, dict):
        raise EvidenceError(f"{label}: executor must return a run record object")
    if not isinstance(record.get("mode"), str) or not record["mode"]:
        raise EvidenceError(f"{label}: the run record must name its mode")
    if type(record.get("real_game")) is not bool:
        raise EvidenceError(f"{label}: the run record must state whether a real game process ran")
    if record.get("restore") is not None and not isinstance(record["restore"], dict):
        raise EvidenceError(f"{label}: restore evidence must be an object or null")
    root_binding = "unavailable"
    identity = record.get("identity")
    if identity is not None:
        if not isinstance(identity, dict):
            raise EvidenceError(f"{label}: run identity must be an object")
        if (identity.get("game") != sample.root_identity["game"]
                or identity.get("artifacts") != sample.root_identity["artifacts"]):
            raise EvidenceError(f"{label}: the run identity does not match the sealed root identity")
        root_binding = "matched"
    frames = []
    raw = record.get("frames")
    if not isinstance(raw, list) or not raw:
        raise EvidenceError(f"{label}: the run record needs per-tick frames")
    for index, frame in enumerate(raw):
        tick, state = (frame.get("tick"), frame.get("state")) if isinstance(frame, dict) else (None, None)
        if type(tick) is not int or tick < 0:
            raise EvidenceError(f"{label}: frame {index} needs a nonnegative integer tick")
        if not isinstance(state, dict) or not state:
            raise EvidenceError(f"{label}: frame {index} needs a state object")
        frames.append({"tick": tick, "state": copy.deepcopy(state), "kind": frame.get("kind", "post_step")})
    ticks = [frame["tick"] for frame in frames]
    if ticks[0] != 0:
        raise EvidenceError(f"{label}: the first frame must be the tick-zero restore boundary")
    if ticks != sorted(ticks):
        raise EvidenceError(f"{label}: frames must be in non-decreasing tick order")
    if ticks[-1] < sample.end["tick"]:
        raise EvidenceError(f"{label}: frames stop at tick {ticks[-1]}, before the sampled end boundary "
                            f"{sample.end['tick']}")
    return {"schema": RUN_SCHEMA, "label": label, "node": sample.key, "mode": record["mode"],
            "real_game": record["real_game"], "root_binding": root_binding,
            "restore": copy.deepcopy(record.get("restore")),
            "root_image_sha256": record.get("root_image_sha256"), "note": record.get("note"),
            "tick_range": [ticks[0], ticks[-1]], "frames": frames}


def execute_sample(sample: Sample, executor, output: Path) -> list[dict]:
    """Run one sample twice; every run keeps its own directory and record."""
    directory = output / RUNS_DIRECTORY / sample.key
    directory.mkdir(parents=True, exist_ok=False)
    runs = []
    for label in RUN_LABELS:
        run_directory = directory / label
        run_directory.mkdir(exist_ok=False)
        record = normalize_run(label, executor(copy.deepcopy(sample), run_directory), sample)
        path = run_directory / RUNS_RECORD
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                        encoding="utf-8", newline="\n")
        runs.append({"label": label, "directory": f"{RUNS_DIRECTORY}/{sample.key}/{label}",
                     "record": f"{RUNS_DIRECTORY}/{sample.key}/{label}/{RUNS_RECORD}",
                     "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), **record})
    return runs


def _reference_for(sample: Sample, nodes: dict[str, dict], cache: dict) -> dict:
    if sample.key not in cache:
        cache[sample.key] = sealed_frames(nodes[sample.key]["trajectory"])
    reference = {"frames": cache[sample.key], "source": f"sealed node {sample.key}"}
    if sample.parent_key is not None:
        if sample.parent_key not in cache:
            cache[sample.parent_key] = sealed_frames(nodes[sample.parent_key]["trajectory"])
        reference["parent_boundary"] = _boundary_frame(cache[sample.parent_key], sample.fork)
    else:
        reference["parent_boundary"] = None
    return reference


def _report_id(report: dict) -> str:
    return hashlib.sha256(canonical({key: value for key, value in report.items() if key != "report_id"})).hexdigest()


def _limitations(real_game_run: bool) -> dict:
    return {
        "real_game_run": real_game_run,
        "original_engine_verified": False,
        "statement": ("every executed run reports real_game=true; this report still certifies no original-engine "
                      "determinism claim" if real_game_run else
                      "no real game process was launched: every executed run reports real_game=false"),
        "prerequisites": [
            "an a2 snapshot/restore implementation (N5, tools/process_snapshot.py): capture the sealed tree root "
            "image at a pause boundary and pass that image back to a live target",
            "an executor named by --executor that launches the controlled target (launcher + pinned runtime build + "
            "private profile), restores the root image, executes the sampled action sequence and returns per-tick "
            "audit states; pass the verify_r0 report verbatim as the run's restore evidence",
            "run identity equal to the sealed root identity: game, artifacts and runtime build must match, otherwise "
            "the sealed comparison is not the same root",
            "R4 additionally needs restore-interval, host-load and parallelism variation (N6 / N10)",
            "a real long-game run is out of scope for this tool and for issue #36",
        ],
        "excluded": ["R4", "real game execution", "original-engine determinism certification"],
    }


def run_selfcheck(tree_path, *, executor, output, limit=SAMPLE_LIMIT_DEFAULT, samples=None,
                  reference="sealed", stop_on_failure=False, executor_name=None) -> tuple[dict, Path]:
    """Sample a sealed tree, re-run every sample twice and write the report."""
    if reference not in {"sealed", "none"}:
        raise EvidenceError("reference must be 'sealed' or 'none'")
    tree = EvidenceTree.load(tree_path)
    summary = tree.validate()
    nodes = node_records(tree)
    selected, coverage = plan_samples(tree, nodes, limit=limit, requested=samples)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output / RUNS_DIRECTORY).mkdir()
    use_reference = reference == "sealed"
    cache: dict = {}
    sample_reports, stopped = [], False
    for key in selected:
        if stopped:
            sample_reports.append({"node": key, "status": "not_run",
                                   "note": "stop-on-failure was requested and an earlier sample failed"})
            continue
        sample = build_sample(tree, nodes, key)
        runs = execute_sample(sample, executor, output)
        report = check_sample(sample, runs, _reference_for(sample, nodes, cache) if use_reference else None,
                              use_reference=use_reference)
        report["runs"] = [{"label": run["label"], "record": run["record"], "sha256": run["sha256"],
                           "mode": run["mode"], "real_game": run["real_game"],
                           "root_binding": run["root_binding"]} for run in runs]
        report["reference"] = ({"mode": "sealed", "frames": f"sealed node {key} recording",
                                "fork_reference": report["fork"].get("reference")} if use_reference else
                               {"mode": "none", "note": "only the two fresh runs were compared"})
        sample_reports.append({name: report[name] for name in
                               ("node", "branch_id", "status", "binding", "request", "runs", "reference",
                                "rungs", "fork", "first_divergence", "evidence")})
        if stop_on_failure and report["status"] == "failed":
            stopped = True
    executed = [sample for sample in sample_reports if sample["status"] != "not_run"]
    rungs = {rung: {status: sum(1 for sample in executed if sample.get("rungs", {}).get(rung, {}).get("status") == status)
                    for status in ("passed", "failed", "not_reached", "not_available", "not_applicable")}
             for rung in RUNG_ORDER}
    divergence = next((sample["first_divergence"] for sample in executed if sample["first_divergence"] is not None), None)
    real_game_run = bool(executed) and all(run["real_game"] for sample in executed for run in sample["runs"])
    report = {
        "schema": SCHEMA,
        "tool": "tools/branch_selfcheck.py",
        "scope": ("same-root, same-action re-execution of sampled tree nodes compared frame by frame; "
                  "R0-R3 only, never R4 and never an original-engine claim"),
        "comparison": COMPARISON_SCOPE,
        "executor": executor_name,
        "reference_mode": reference,
        "tree": {"path": str(tree.directory), "tree_id": summary["tree_id"], "root": summary["root"],
                 "nodes": summary["nodes"], "branches": summary["branches"], "trunk_nodes": summary["trunk_nodes"],
                 "branch_points": summary["branch_points"], "scope": summary["scope"]},
        "root_identity": copy.deepcopy(nodes[tree.record["root"]]["placement"]["root"]["identity"]),
        "run_labels": list(RUN_LABELS),
        "sampling": coverage,
        "samples": sample_reports,
        "rungs": rungs,
        "first_divergence": divergence,
        "limitations": _limitations(real_game_run),
    }
    report["verdict"] = _verdict(report)
    report["report_id"] = _report_id(report)
    path = output / REPORT_FILE
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8", newline="\n")
    return report, path


def _verdict(report: dict) -> dict:
    failed = sorted({sample["node"] for sample in report["samples"]
                     if sample.get("status") == "failed"})
    unreached = sorted({sample["node"] for sample in report["samples"]
                        if any(rung.get("status") == "not_available" for rung in sample.get("rungs", {}).values())})
    notes = []
    if report["rungs"]["r0"]["not_applicable"]:
        notes.append("R0 did not apply: no executed run reported a restore step")
    if report["reference_mode"] == "none":
        notes.append("no sealed reference was compared; only P1 between the two fresh runs was checked")
    if failed:
        status = "failed"
        reason = "at least one rung failed; the sealed tree was not reproduced under this executor"
    elif unreached:
        status = "incomplete"
        reason = "some sampled boundaries could not be evaluated"
    else:
        status = "passed"
        reason = "every evaluated rung matched"
    if report["limitations"]["real_game_run"] is False:
        notes.append("no real game process was launched; see limitations.prerequisites")
    return {"status": status, "reason": reason, "failed_nodes": failed,
            "unevaluated_nodes": unreached, "notes": notes}


def load_executor(spec: str):
    """Resolve ``module:function`` or ``path/to/executor.py:function``."""
    target, separator, name = spec.partition(":")
    if not separator or not target or not name:
        raise EvidenceError("executor must be named module:function or path/to/executor.py:function")
    if target.endswith(".py"):
        path = Path(target).resolve()
        if not path.is_file():
            raise EvidenceError(f"executor module is missing: {path}")
        module_spec = importlib.util.spec_from_file_location(f"lvz_branch_executor_{path.stem}", path)
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
    else:
        module = importlib.import_module(target)
    function = getattr(module, name, None)
    if not callable(function):
        raise EvidenceError(f"executor {spec} is not callable")
    return function, f"{getattr(module, '__name__', target)}:{name}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tree", type=Path, help="sealed evidence tree directory (package_tree output)")
    parser.add_argument("output", type=Path, help="fresh directory for the report and the run evidence")
    parser.add_argument("--executor", required=True,
                        help="module:function or path/to/executor.py:function that executes one sampled node")
    parser.add_argument("--sample-limit", default=str(SAMPLE_LIMIT_DEFAULT),
                        help=f"maximum number of sampled nodes (default {SAMPLE_LIMIT_DEFAULT}) or 'all'")
    parser.add_argument("--sample-nodes", help="explicit comma-separated node keys instead of the sampling policy")
    parser.add_argument("--reference", choices=("sealed", "none"), default="sealed",
                        help="compare the fresh runs against the sealed node frames (default) or only against each other")
    parser.add_argument("--stop-on-failure", action="store_true",
                        help="stop after the first failing sample instead of covering the whole selection")
    arguments = parser.parse_args(argv)
    limit: object = arguments.sample_limit
    if isinstance(limit, str) and limit != "all":
        try:
            limit = int(limit)
        except ValueError:
            parser.exit(2, "sample limit must be a positive integer or 'all'\n")
    samples = None
    if arguments.sample_nodes:
        samples = [key.strip() for key in arguments.sample_nodes.split(",") if key.strip()]
        if not samples:
            parser.exit(2, "sample nodes must be a nonempty comma-separated list\n")
    try:
        executor, name = load_executor(arguments.executor)
        report, path = run_selfcheck(arguments.tree, executor=executor, output=arguments.output, limit=limit,
                                     samples=samples, reference=arguments.reference,
                                     stop_on_failure=arguments.stop_on_failure, executor_name=name)
    except (EvidenceError, OSError, KeyError, TypeError, ValueError) as error:
        parser.exit(1, f"branch self-check rejected: {error}\n")
    print(json.dumps({"report": str(path), "report_id": report["report_id"], "verdict": report["verdict"]["status"],
                      "samples": [sample["node"] for sample in report["samples"]],
                      "coverage": {key: report["sampling"][key] for key in
                                   ("nodes_total", "nodes_sampled", "node_fraction", "branches_sampled",
                                    "edge_ticks_sampled", "edge_ticks_total")},
                      "first_divergence": report["first_divergence"],
                      "real_game_run": report["limitations"]["real_game_run"]}, indent=2, ensure_ascii=False))
    return 1 if report["verdict"]["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
