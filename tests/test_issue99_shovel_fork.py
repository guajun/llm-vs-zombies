"""Issue #99 offline contracts: the declared plan pair, the comparator and wiring.

None of these tests start a game or read a live run. They pin what the frozen
plans declare, what the comparison must and must not conclude, and that the
runtime, the protocol document and the evaluation entry keep naming the same
action.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from issue99_shovel_fork import GROUPS, compare_pair, markdown_report, verdict  # noqa: E402
from llm_vs_zombies.evaluation import (INTERVENTION_OPS, Plan, intervention_relation,  # noqa: E402
                                       intervention_requirement_failure, intervention_spec,
                                       stop_condition_spec)

CONTROL_PLAN = ROOT / "experiments/plans/issue99-shovel-control.json"
INTERVENTION_PLAN = ROOT / "experiments/plans/issue99-shovel-intervene.json"


def checksums(rows) -> str:
    return "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)


def boundary(tick, revision=0, **digests) -> dict:
    values = {"all": "same", "board": "board", "plants": "plants", "rng": "rng",
              "sound_effects": "sound", "zombies": "zombies"}
    values.update(digests)
    return {"schema": "lvz.audit.v1", "seq": tick, "kind": "pre_step",
            "version": {"epoch": 3, "tick": tick, "revision": revision}, "digests": values,
            "payload": {}}


class PlanPairTests(unittest.TestCase):
    def test_shipped_plans_are_identical_apart_from_the_declared_action(self):
        control = Plan.load(CONTROL_PLAN)
        intervention = Plan.load(INTERVENTION_PLAN)
        self.assertEqual(control.scenario, "jingdian12")
        self.assertEqual(control.seeds, (42,))
        self.assertIsNone(control.intervention["action"])
        self.assertEqual(intervention.intervention["action"], {"op": "shovel_hold_cancel"})
        self.assertEqual(control.stop_when, intervention.stop_when)
        self.assertEqual(control.stop_when, {"wave_at_least": 2})
        left, right = control.__dict__, intervention.__dict__
        differing = sorted(key for key in set(left) | set(right)
                           if json.dumps(left.get(key), sort_keys=True) != json.dumps(right.get(key), sort_keys=True))
        self.assertEqual(differing, ["intervention"])
        # Two fields carry the whole difference: the action itself and the
        # request budget that only exists because there is an action.
        self.assertEqual(control.intervention["advance_ticks"], 0)
        self.assertEqual(intervention.intervention["advance_ticks"], 100)
        for key in control.intervention:
            if key not in ("action", "advance_ticks"):
                self.assertEqual(control.intervention[key], intervention.intervention[key])

    def test_the_frozen_point_is_before_the_first_wave_with_a_hard_cap(self):
        plan = Plan.load(INTERVENTION_PLAN)
        self.assertEqual(plan.intervention["at"], {"epoch": 3, "tick": 101, "revision": 0})
        self.assertEqual(plan.intervention["require"]["wave"], 0)
        self.assertGreater(plan.intervention["require"]["refresh_countdown"], 0)
        self.assertEqual(plan.intervention["require"]["zombies"], 0)
        # The cap is the frozen hard limit: the event end may be reached earlier
        # but the run is never extended to reach a difference.
        self.assertLessEqual(plan.intervention["at"]["tick"], plan.tick_budget)
        self.assertLessEqual(plan.intervention["advance_ticks"], plan.chunk_ticks)


class InterventionSpecTests(unittest.TestCase):
    def spec(self, **change):
        value = {"schema": "lvz.intervention.v1", "id": "shovel-hold-cancel-01",
                 "at": {"epoch": 3, "tick": 101, "revision": 0}, "require": {"wave": 0},
                 "action": {"op": "shovel_hold_cancel"}, "advance_ticks": 100}
        value.update(change)
        return value

    def test_defaults_and_normalization(self):
        self.assertEqual(intervention_spec(None), None)
        spec = intervention_spec(self.spec())
        self.assertEqual(spec["action"], {"op": "shovel_hold_cancel"})
        self.assertEqual(spec["require"], {"wave": 0})
        control = intervention_spec(self.spec(action=None, advance_ticks=0))
        self.assertIsNone(control["action"])
        self.assertEqual(intervention_spec(self.spec(action={"op": "shovel_hold_cancel", "cancel": "left"}))["action"],
                         {"op": "shovel_hold_cancel", "cancel": "left"})

    def test_declaration_errors_fail_before_any_process_exists(self):
        cases = {
            "unknown key": self.spec(target_type=14),
            "missing id": {key: value for key, value in self.spec().items() if key != "id"},
            "bad id": self.spec(id="bad id"),
            "bad schema": self.spec(schema="lvz.intervention.v2"),
            "partial at": self.spec(at={"epoch": 3, "tick": 101}),
            "boolean tick": self.spec(at={"epoch": 3, "tick": True, "revision": 0}),
            "zero tick": self.spec(at={"epoch": 3, "tick": 0, "revision": 0}),
            "unknown require": self.spec(require={"wave": 0, "sun": 50}),
            "unknown op": self.spec(action={"op": "shovel", "row": 3, "col": 4}),
            "unknown cancel": self.spec(action={"op": "shovel_hold_cancel", "cancel": "middle"}),
            "coordinates in action": self.spec(action={"op": "shovel_hold_cancel", "x": 0}),
            "negative advance": self.spec(advance_ticks=-1),
        }
        for label, value in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                intervention_spec(value)
        self.assertEqual(intervention_spec(self.spec())["action"]["op"], INTERVENTION_OPS[0])
        with self.assertRaises(ValueError):
            stop_condition_spec({"wave_at_least": 0})
        with self.assertRaises(ValueError):
            stop_condition_spec({"wave_at_least": 2, "extra": 1})
        self.assertEqual(stop_condition_spec(None), None)

    def test_plan_validation_binds_the_boundary_to_the_frozen_budget(self):
        raw = json.loads(INTERVENTION_PLAN.read_text(encoding="utf-8"))
        for change in ({"tick_budget": 100}, {"chunk_ticks": 10}, {"intervention": None}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                Plan(**{**raw, **change}).validate()
        # An event end without a declared boundary has nothing to stop after.
        self.assertIsNotNone(Plan(**raw).validate().stop_when)
        without_event_end = Plan(**{**raw, "stop_when": None}).validate()
        self.assertIsNone(without_event_end.stop_when)
        Plan(**raw).validate()

    def test_boundary_relation_and_preconditions(self):
        at = {"epoch": 3, "tick": 101, "revision": 0}
        self.assertEqual(intervention_relation({"epoch": 3, "tick": 101, "revision": 0}, at), "at")
        self.assertEqual(intervention_relation({"epoch": 3, "tick": 101, "revision": 1}, at), "past")
        self.assertEqual(intervention_relation({"epoch": 3, "tick": 1, "revision": 0}, at), "before")
        self.assertEqual(intervention_relation({"epoch": 4, "tick": 1, "revision": 0}, at), "past")
        with self.assertRaises(ValueError):
            intervention_relation({"tick": 101}, at)
        require = {"wave": 0, "refresh_countdown": 498, "zombies": 0, "plants": 52}
        observation = {"wave": 0, "refresh_countdown": 498, "zombies": [], "plants": [0] * 52}
        self.assertIsNone(intervention_requirement_failure(observation, require))
        self.assertIn("refresh_countdown", intervention_requirement_failure(
            {**observation, "refresh_countdown": 497}, require))
        self.assertIn("zombies", intervention_requirement_failure(
            {**observation, "zombies": [1]}, require))
        self.assertIsNone(intervention_requirement_failure({"wave": 0}, {}))


class ComparatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def run_with(self, name, rows):
        directory = self.root / name / "audit"
        directory.mkdir(parents=True)
        (directory / "checksums.jsonl").write_text(checksums(rows), encoding="utf-8", newline="\n")
        return self.root / name

    def test_identical_prefix_then_grouped_difference(self):
        left = self.run_with("left", [boundary(1), boundary(2), boundary(3),
                                     boundary(4, rng="rng2", sound_effects="sound2")])
        right = self.run_with("right", [boundary(1), boundary(2), boundary(3),
                                       boundary(4, revision=1, rng="rng3", sound_effects="sound3")])
        report = compare_pair(left, right, left_name="left", right_name="right")
        self.assertEqual(report["common_prefix_ticks"], 3)
        self.assertEqual(report["first_difference"]["tick"], 4)
        self.assertEqual(report["first_difference"]["components"], ["rng", "sound_effects"])
        self.assertEqual(report["first_difference"]["groups"], ["audio", "rng"])
        self.assertEqual(report["first_difference"]["revision"], {"a": [0], "b": [1]})
        self.assertEqual(report["first_difference_by_group"]["rng"]["tick"], 4)
        self.assertNotIn("simulation", report["first_difference_by_group"])
        self.assertFalse(report["identical"])

    def test_simulation_difference_is_reported_separately_from_rng(self):
        left = self.run_with("left", [boundary(1), boundary(2)])
        right = self.run_with("right", [boundary(1, rng="rng2"), boundary(2, board="board2")])
        report = compare_pair(left, right, left_name="left", right_name="right")
        self.assertEqual(report["common_prefix_ticks"], 0)
        self.assertEqual(report["first_difference_by_group"]["rng"]["tick"], 1)
        self.assertEqual(report["first_difference_by_group"]["simulation"]["tick"], 2)
        self.assertEqual(report["first_difference"]["components"], ["rng"])
        self.assertIn("simulation", GROUPS)

    def test_identical_runs_report_no_difference(self):
        rows = [boundary(1), boundary(2)]
        left = self.run_with("left", rows)
        right = self.run_with("right", rows)
        report = compare_pair(left, right, left_name="left", right_name="right")
        self.assertTrue(report["identical"])
        self.assertIsNone(report["first_difference"])
        self.assertEqual(report["common_prefix_ticks"], 2)

    def test_a_missing_boundary_is_named_not_scored_as_agreement(self):
        left = self.run_with("left", [boundary(1), boundary(2)])
        right = self.run_with("right", [boundary(1)])
        report = compare_pair(left, right, left_name="left", right_name="right")
        self.assertEqual(report["first_difference"]["tick"], 2)
        self.assertIn("missing", report["first_difference"]["reason"])
        self.assertFalse(report["identical"])

    def test_markdown_and_verdict_read_a_report_without_a_game(self):
        control = self.run_with("control", [boundary(1)])
        intervention = self.run_with("intervention", [boundary(1, rng="rng2")])
        record = {"declared_at": {"epoch": 3, "tick": 101, "revision": 0},
                  "fired_at": {"epoch": 3, "tick": 101, "revision": 0}, "fired": True,
                  "action": {"op": "shovel_hold_cancel"},
                  "summary": {"ok": True, "cursor_type": [0, 6, 0], "rng_cursor": [575, 577, 578],
                              "rng_words_equal": True, "plants_changed": False}}

        def summary(path, intervention_record):
            return {"path": str(path), "outcome": "stop_condition_reached",
                    "final_version": {"epoch": 3, "tick": 1, "revision": 0}, "maximum_wave": 2,
                    "hosted_fire_count": 1, "first_hosted_fire_tick": 1, "first_removal": None,
                    "intervention": intervention_record}

        report = {"schema": "lvz.issue99-shovel-fork.v1", "plans": {}, "plan_differences": {}, "pairs": {},
                  "runs": {"control": summary(control, None), "control-rerun": summary(control, None),
                           "intervention": summary(intervention, record),
                           "intervention-rerun": summary(intervention, record)}}
        report["pairs"]["control-rerun"] = compare_pair(control, control, left_name="control",
                                                        right_name="control-rerun")
        report["pairs"]["intervention-rerun"] = compare_pair(intervention, intervention,
                                                             left_name="intervention",
                                                             right_name="intervention-rerun")
        report["pairs"]["control-vs-intervention"] = compare_pair(control, intervention,
                                                                  left_name="control", right_name="intervention")
        report["verdict"] = verdict(report)
        self.assertTrue(report["verdict"]["branches_reproduce"])
        self.assertTrue(report["verdict"]["intervention_action_verified"])
        self.assertTrue(report["verdict"]["intervention_fired_as_declared"])
        self.assertTrue(report["verdict"]["random_state_fork_demonstrated"])
        self.assertFalse(report["verdict"]["gameplay_fork_demonstrated"])
        text = markdown_report(report)
        self.assertIn("结论摘要", text)
        self.assertIn("shovel_hold_cancel", text)


class WiringTests(unittest.TestCase):
    def test_runtime_capability_protocol_and_entry_agree_on_one_name(self):
        runtime = (ROOT / "runtime/runtime.cpp").read_text(encoding="utf-8")
        header = (ROOT / "runtime/shovel_action.hpp").read_text(encoding="utf-8")
        protocol = (ROOT / "docs/runtime-protocol.md").read_text(encoding="utf-8")
        self.assertIn('{"shovel_hold_cancel",true}', runtime)
        self.assertIn("RunShovelHoldCancel(a)", runtime)
        self.assertIn('ShovelHoldCancelOp[] = "shovel_hold_cancel"', header)
        self.assertIn("## Controlled shovel action (issue #99)", protocol)
        self.assertEqual(INTERVENTION_OPS, ("shovel_hold_cancel",))
        self.assertIn("shovel_hold_cancel", protocol)

    def test_default_build_keeps_the_action_where_it_belongs(self):
        cmake = (ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
        self.assertIn("runtime_shovel_action_tests", cmake)
        # The live path is header-only and only runtime.cpp includes it, so no
        # new source file can leak into an unrelated target.
        self.assertNotIn("shovel_hold.cpp", cmake)
        live = (ROOT / "runtime/shovel_hold.hpp").read_text(encoding="utf-8")
        self.assertIn("AAsm::ClickScene", live)
        self.assertNotIn("RestoreRng", live)
        self.assertNotIn("SeedRng", live)
        self.assertNotIn("AShovel(", live)


if __name__ == "__main__":
    unittest.main()
