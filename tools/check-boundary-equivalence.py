"""Offline acceptance of two separately recorded original-engine experiments.

This tool never starts a game. It consumes closed, hash-bound trajectories and
compares every captured boundary plus exact initializer-exit spawn evidence.
"""
from __future__ import annotations

import argparse
import copy
import json
from itertools import zip_longest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from llm_vs_zombies.audit_compare import EvidenceError, first_difference
from llm_vs_zombies.engine_replay import Trajectory


def _spawns(trajectory):
    """Controlled initializer exits belong to the pre-update boundary's tick."""
    epoch = trajectory.initial["observation"]["version"]["epoch"]
    indexed = {}
    for event in trajectory.audit.events:
        if event["kind"] != "zombie_initialized" or event.get("phase") != "controlled_boundary":
            continue
        if event["version"]["epoch"] != epoch:
            raise EvidenceError("spawn outside the compared epoch")
        indexed.setdefault(event["version"]["tick"], []).append(event)
    return indexed


def _spawn_semantics(event, post_frame):
    """Keep initializer scalars/RNG; resolve only verified animation references.

    Initializer-exit handles can be changed by the caller before the post-frame.
    If that happens, there is insufficient evidence to normalize that handle:
    reject the comparison instead of deleting or guessing the field.
    """
    payload = copy.deepcopy(event["payload"])
    payload.pop("boundary", None)
    payload.pop("ordinal", None)  # Relative controlled event order is checked separately.
    initial = payload.get("initial", {})
    fields = initial.get("raw_scalar_fields")
    if not isinstance(fields, dict) or type(payload.get("slot")) is not int:
        raise EvidenceError("spawn lacks exact initializer raw fields/slot")
    raw = post_frame.raw_animations
    if not isinstance(raw, dict):
        raise EvidenceError("spawn normalization requires checked raw animation evidence")
    by_path = {link["path"]: link for link in raw["links"]}
    # Native reanimation_audit.cpp zombieRoles, also covered by manifest.
    for offset in ("00000118", "00000140", "00000144", "00000150"):
        if offset not in fields:
            raise EvidenceError("spawn lacks an audited zombie animation role")
        handle = fields[offset]
        if type(handle) is not int or not 0 <= handle <= 0xffffffff:
            raise EvidenceError("spawn animation handle is not uint32")
        if handle == 0:
            fields[offset] = {"status": "null"}
            continue
        link = by_path.get(f"/zombies/slots/{payload['slot']}/fields/{offset}")
        if link is None or link["raw_handle"] != handle or not link["lookup_matches"]:
            raise EvidenceError("initializer animation handle cannot be proven at the following boundary")
        fields[offset] = copy.deepcopy(link["normalized_reference"])
    return payload


def _actions(trajectory):
    result = []
    for event in trajectory.audit.events:
        if event["kind"] == "action":
            payload = copy.deepcopy(event["payload"])
            payload.pop("request_id", None)
            payload.pop("ordinal", None)
            payload.get("result", {}).pop("ordinal", None)
            result.append({"tick": event["version"]["tick"], "payload": payload})
    return result


def compare(left_path, right_path, *, purpose, ticks=100):
    if purpose not in {"render", "batch"} or type(ticks) is not int or ticks <= 0:
        raise ValueError("purpose must be render/batch and ticks must be positive")
    left, right = Trajectory.load(left_path), Trajectory.load(right_path)
    report = {"schema": "lvz.boundary-equivalence.v1", "purpose": purpose, "ticks": ticks,
              "left": left.manifest["trajectory_id"], "right": right.manifest["trajectory_id"],
              "passed": False, "frames_compared": 0, "spawn_events_compared": 0,
              "spawn_exercised": False, "original_engine_replay_verified": False,
              "scope": "all captured semantic state including RNG, actions, and controlled initializer-exit spawns",
              "excluded": ["request_id", "request grouping/budget", "wall time", "pixel bytes",
                           "raw animation allocation identities after verified semantic mapping",
                           "pre-marker initialization spawn history (initial state is compared)"]}

    def equal(expected, actual, stage, **where):
        difference = first_difference(expected, actual)
        if difference:
            report.update(stage=stage, difference=difference, **where)
            return False
        return True

    for key in ("identity", "initialization", "state"):
        if not equal(left.initial[key], right.initial[key], "initial_" + key):
            return report
    seed = left.initial["initialization"].get("seed")
    if type(seed) is not int or not 0 <= seed <= 0xffffffff:
        raise EvidenceError("experiment initialization must identify its uint32 seed")
    report["seed"] = seed
    observations = [copy.deepcopy(item.initial["observation"]) for item in (left, right)]
    for observation in observations:
        if observation["version"]["tick"] != 0:
            raise EvidenceError("experiment must begin at the recorded tick-zero boundary")
        observation.pop("version")  # Each fresh runtime has its own epoch/revision origin.
    if not equal(*observations, "initial_observation") or not equal(_actions(left), _actions(right), "action_attempts"):
        return report
    commands, captures = [], []
    for trajectory in (left, right):
        commands.append([step for step in trajectory.steps if step["request"]["method"] in {"commit", "advance"}])
        captures.append([step for step in trajectory.steps if step["request"]["method"] == "capture_frame"])
    report["requests"] = [len(steps) for steps in commands]
    report["captures"] = [len(steps) for steps in captures]
    if any(sum(step["result"]["executed_ticks"] for step in steps) != ticks for steps in commands):
        raise EvidenceError("recording length does not equal the requested experiment duration")
    if purpose == "batch":
        if ([step["result"]["executed_ticks"] for step in commands[0]] != [1] * ticks
                or [step["result"]["executed_ticks"] for step in commands[1]] != [ticks]
                or any(captures) or _actions(left) or _actions(right)):
            raise EvidenceError("batch experiment requires N single advances vs one N-tick advance, without captures/actions")
    else:
        if captures[0] or not captures[1]:
            raise EvidenceError("render experiment requires left capture-off and right capture-on")
        for step in captures[1]:
            response = step["capture_response"]
            result = response.get("result", {})
            if (response.get("ok") is not True or result.get("capture_ok") is not True
                    or result.get("forced_render") is not True or result.get("known_rng_unchanged") is not True
                    or result.get("game_clock_before") != result.get("game_clock_after")):
                raise EvidenceError("render experiment lacks successful state-guarded forced rendering")
    # An end-of-run capture without a following audited step cannot prove absence
    # of animation changes. Require every intervention to precede a sampled step.
    if any(step["request"]["expect"]["tick"] >= ticks for steps in captures for step in steps):
        raise EvidenceError("capture at the final boundary requires a further audited step")
    births = [_spawns(item) for item in (left, right)]
    if not equal(sorted(births[0]), sorted(births[1]), "spawn_ticks"):
        return report
    streams = [trajectory.audit._frames(reuse_state=True) for trajectory in (left, right)]
    for index, pair in enumerate(zip_longest(*streams)):
        a, b = pair
        if a is None or b is None:
            raise EvidenceError("audit frame counts differ")
        expected_tick, expected_kind = (index + 1) // 2, "post_step" if index % 2 else "pre_step"
        for frame in pair:
            if frame.kind != expected_kind or frame.version["tick"] != expected_tick:
                raise EvidenceError("experiment does not contain consecutive pre/post boundaries from tick zero")
        if a.canonical_state != b.canonical_state and not equal(a.state, b.state, "state",
                tick=expected_tick, phase=expected_kind):
            return report
        report["frames_compared"] += 1
        if expected_kind == "post_step":
            source_tick = expected_tick - 1
            source_births = [collection.get(source_tick, []) for collection in births]
            if len(source_births[0]) != len(source_births[1]):
                report.update(stage="spawn_count", tick=source_tick,
                              difference={"expected": len(source_births[0]), "actual": len(source_births[1])})
                return report
            for ordinal, events in enumerate(zip(*source_births)):
                if not equal(_spawn_semantics(events[0], a), _spawn_semantics(events[1], b),
                             "spawn", tick=source_tick, controlled_ordinal=ordinal):
                    return report
                report["spawn_events_compared"] += 1
    if report["frames_compared"] != ticks * 2:
        raise EvidenceError("wrong number of audited boundaries")
    if any(tick < 0 or tick >= ticks for collection in births for tick in collection):
        raise EvidenceError("controlled spawn lies outside sampled update boundaries")
    report.update(passed=True, spawn_exercised=report["spawn_events_compared"] > 0)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--purpose", required=True, choices=("render", "batch"))
    parser.add_argument("--ticks", type=int, default=100)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = compare(args.left, args.right, purpose=args.purpose, ticks=args.ticks)
    except (EvidenceError, OSError, ValueError) as error:
        report = {"schema": "lvz.boundary-equivalence.v1", "passed": False,
                  "stage": "evidence", "error": str(error), "original_engine_replay_verified": False}
    if args.output:
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
