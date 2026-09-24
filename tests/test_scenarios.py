"""Scenario registry, plan scenario/flags fields and the hosted script's half.

The default scenario (``liangyi``) has to stay exactly the behaviour the suite
had before the registry existed: the same fixed inputs, the same card order,
the same Scene 3/layout check, and a plan without the new fields that reads as
today's plan does.
"""
from __future__ import annotations

import hashlib
import contextlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from llm_vs_zombies import evaluation as ev
from llm_vs_zombies import launcher as lv

ROOT = Path(__file__).resolve().parents[1]

# avz/framework/inc/avz_types.h: AICE_SHROOM=14 ... ACOFFEE_BEAN=35, APUFF_SHROOM=8,
# ACOB_CANNON=47; an imitater card is 49 + the copied seed.
JINGDIAN12_CARDS = [14, 63, 35, 15, 16, 17, 2, 27, 30, 8]
JINGDIAN12_SAVE = "experiments/scenarios/jingdian12/game1_13.dat"
JINGDIAN12_SHA256 = "5d5165dc46cf30c69358bb55e85d8b94d7b114ce251cf410019f7f1c3d2a7b25"
JINGDIAN12_SIZE = 275364


class ScenarioRegistryTests(unittest.TestCase):
    def test_default_scenario_is_the_liangyi_registry_entry(self):
        spec = lv.scenario_spec()
        self.assertEqual(spec.name, "liangyi")
        self.assertEqual(spec.save, "experiments/scenarios/liangyi/game1_13.dat")
        self.assertEqual(spec.config, "experiments/configs/liangyi.json")
        self.assertEqual(list(spec.cards), lv.DEFAULT_CARDS)
        self.assertEqual((spec.game_mode, spec.expected_scene, spec.scene_label), (13, 3, "fog"))
        self.assertEqual(spec.layout, lv.REFERENCE_LAYOUT)
        self.assertEqual(spec.min_plants, {})
        # The fixed inputs are derived from the scenario, in the historic order.
        self.assertEqual(lv.REQUIRED_INPUTS, (*lv.COMMON_INPUTS, spec.save))
        self.assertEqual(spec.inputs(), lv.REQUIRED_INPUTS)
        self.assertEqual(lv.verify_inputs.__defaults__, ("liangyi",))

    def test_jingdian12_cards_come_from_the_tutorial_enums(self):
        spec = lv.scenario_spec("jingdian12")
        self.assertEqual(list(spec.cards), JINGDIAN12_CARDS)
        self.assertEqual(spec.cards[1], 49 + 14)          # AM_ICE_SHROOM
        self.assertEqual(spec.min_plants, {lv.COB_CANNON: 12})
        self.assertEqual(lv.COB_CANNON, 47)               # ACOB_CANNON
        self.assertEqual((spec.expected_scene, spec.scene_label), (2, "pool"))
        self.assertEqual(spec.save, JINGDIAN12_SAVE)
        self.assertEqual(spec.config, "experiments/configs/jingdian12.json")
        self.assertEqual(spec.inputs(), (*lv.COMMON_INPUTS, JINGDIAN12_SAVE))
        self.assertTrue((ROOT / spec.config).is_file())

    def test_jingdian12_save_is_locked_and_the_checkout_copy_matches(self):
        lock = {item["path"]: item for item in
                json.loads((ROOT / "dependencies.lock.json").read_text(encoding="utf-8"))["files"]}
        entry = lock[JINGDIAN12_SAVE]
        self.assertEqual((entry["sha256"], entry["size"]), (JINGDIAN12_SHA256, JINGDIAN12_SIZE))
        copy = ROOT / JINGDIAN12_SAVE
        if copy.is_file():
            # experiments/scenarios/**/*.dat is git-ignored: a checkout without
            # the local copy (or without the AvZ submodule) still passes.
            self.assertEqual(hashlib.sha256(copy.read_bytes()).hexdigest(), JINGDIAN12_SHA256)
            self.assertEqual(copy.stat().st_size, JINGDIAN12_SIZE)
        tutorial = ROOT / "avz/framework/tutorial/scripts/jing_dian_12/game1_13.dat"
        if tutorial.is_file():
            self.assertEqual(hashlib.sha256(tutorial.read_bytes()).hexdigest(), JINGDIAN12_SHA256)

    def test_unknown_scenario_fails_closed_before_anything_starts(self):
        for name in ("pool-endless", "", None, 13):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "unknown scenario"):
                lv.scenario_spec(name)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaisesRegex(ValueError, "unknown scenario"):
                lv.verify_inputs(root, "pool-endless")
            with self.assertRaisesRegex(ValueError, "unknown scenario"):
                lv.prepare(root, root / "run", scenario="pool-endless")

    def test_verify_scenario_uses_the_scenario_scene_cards_and_minimum(self):
        spec = lv.scenario_spec("jingdian12")
        observation = {"game_ui": 3, "scene": 2,
                       "plants": [{"type": lv.COB_CANNON, "row": 1, "col": col + 1} for col in range(12)],
                       "seeds": [{"type": t} if t < 49 else {"type": 48, "imitator_type": t - 49}
                                 for t in spec.cards]}
        lv.verify_scenario(observation, "jingdian12")
        with self.assertRaisesRegex(ValueError, "Scene 2 pool"):
            lv.verify_scenario({**observation, "scene": 3}, "jingdian12")
        with self.assertRaisesRegex(ValueError, "at least 12"):
            lv.verify_scenario({**observation, "plants": observation["plants"][:-1]}, "jingdian12")
        with self.assertRaisesRegex(ValueError, "card order"):
            lv.verify_scenario({**observation, "seeds": observation["seeds"][:-1]}, "jingdian12")
        with self.assertRaisesRegex(ValueError, "Scene 2 pool"):
            lv.verify_scenario({**observation, "game_ui": 2}, "jingdian12")
        # The same observation is not a liangyi board: the default is still fog.
        with self.assertRaisesRegex(ValueError, "Scene 3 fog"):
            lv.verify_scenario(observation)

    def test_prepare_refuses_a_run_created_for_another_scenario(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = root / "experiments/runs/mismatch"
            run.mkdir(parents=True)
            (run / "manifest.json").write_text(json.dumps({"status": "recording"}), encoding="utf-8")
            (run / "config.json").write_text(
                json.dumps({"scenario_save": "experiments/scenarios/liangyi/game1_13.dat"}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "was created for scenario save"):
                lv.prepare(root, run, scenario="jingdian12")


class PlanScenarioTests(unittest.TestCase):
    def write(self, directory, document) -> Path:
        path = Path(directory) / "plan.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_default_plan_and_old_plans_keep_today_behaviour(self):
        plan = ev.Plan().validate()
        self.assertEqual((plan.scenario, plan.rounds_to_complete), ("liangyi", 1))
        self.assertEqual(plan.expected_scene, 3)
        with tempfile.TemporaryDirectory() as temp:
            # a v2 document written before these fields existed
            plan = ev.Plan.load(self.write(temp, {"schema": "lvz.evaluation-plan.v2", "tier": "smoke"}))
            self.assertEqual((plan.scenario, plan.rounds_to_complete, plan.expected_scene), ("liangyi", 1, 3))
            # a v1 document
            plan = ev.Plan.load(self.write(temp, {"schema": "lvz.evaluation-plan.v1"}))
            self.assertEqual((plan.scenario, plan.rounds_to_complete, plan.expected_scene), ("liangyi", 1, 3))
            # the fields survive the JSON round trip the runner and cold workers use
            again = ev.Plan.load(self.write(temp, ev.asdict(plan)))
            self.assertEqual((again.scenario, again.rounds_to_complete, again.expected_scene),
                             (plan.scenario, plan.rounds_to_complete, plan.expected_scene))
            self.assertEqual((again.seeds, again.tick_budget, again.audio_mode, again.b0_normalization),
                             (plan.seeds, plan.tick_budget, plan.audio_mode, plan.b0_normalization))
            self.assertNotIn("flags_to_complete", ev.asdict(plan))

    def test_the_pre_rename_field_is_a_read_only_alias(self):
        with tempfile.TemporaryDirectory() as temp:
            # #97: the old name always counted rounds, so it still names the same
            # run; only the new key is ever written back.
            plan = ev.Plan.load(self.write(temp, {"schema": "lvz.evaluation-plan.v2",
                                                  "scenario": "jingdian12", "flags_to_complete": 2}))
            self.assertEqual((plan.scenario, plan.rounds_to_complete), ("jingdian12", 2))
            self.assertNotIn("flags_to_complete", ev.asdict(plan))
            mixed = self.write(temp, {"schema": "lvz.evaluation-plan.v2",
                                      "rounds_to_complete": 1, "flags_to_complete": 2})
            with self.assertRaisesRegex(ValueError, "read-only alias flags_to_complete"):
                ev.Plan.load(mixed)

    def test_plan_cli_round_trips_scenario_and_rounds(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "jingdian12.json"
            with patch("builtins.print"):
                self.assertEqual(ev.main(["plan", str(path), "--scenario", "jingdian12",
                                          "--rounds-to-complete", "2", "--tick-budget", "200000"]), 0)
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual((document["scenario"], document["rounds_to_complete"]), ("jingdian12", 2))
            self.assertNotIn("flags_to_complete", document)
            plan = ev.Plan.load(path)
            self.assertEqual((plan.scenario, plan.rounds_to_complete, plan.expected_scene), ("jingdian12", 2, 2))
            self.assertEqual(plan.scenario_spec(), lv.scenario_spec("jingdian12"))
            # the transitional alias still selects the same run and writes the new key
            alias = Path(temp) / "alias.json"
            with patch("builtins.print"):
                self.assertEqual(ev.main(["plan", str(alias), "--flags-to-complete", "2"]), 0)
            aliased = json.loads(alias.read_text(encoding="utf-8"))
            self.assertEqual(aliased["rounds_to_complete"], 2)
            self.assertNotIn("flags_to_complete", aliased)
            mixed = Path(temp) / "mixed.json"
            with self.assertRaises(SystemExit), patch("builtins.print"), contextlib.redirect_stderr(io.StringIO()):
                ev.main(["plan", str(mixed), "--rounds-to-complete", "1", "--flags-to-complete", "2"])
            self.assertFalse(mixed.exists())

    def test_unknown_scenario_plan_is_rejected_before_it_is_written(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "unknown.json"
            with self.assertRaises(SystemExit), patch("builtins.print"), contextlib.redirect_stderr(io.StringIO()):
                ev.main(["plan", str(path), "--scenario", "pool-endless"])
            self.assertFalse(path.exists())
            plan = self.write(temp, {"schema": "lvz.evaluation-plan.v2", "scenario": "pool-endless"})
            with self.assertRaisesRegex(ValueError, "unknown scenario"):
                ev.Plan.load(plan)
            with self.assertRaisesRegex(ValueError, "unknown scenario"):
                ev.Plan(scenario="pool-endless").validate()

    def test_rounds_to_complete_bounds_and_type_are_enforced(self):
        for value in (0, 101, -1, True, 1.0, "2", None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "rounds_to_complete"):
                ev.Plan(rounds_to_complete=value).validate()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "plan.json"
            with self.assertRaises(SystemExit), patch("builtins.print"), contextlib.redirect_stderr(io.StringIO()):
                ev.main(["plan", str(path), "--rounds-to-complete", "101"])
            self.assertFalse(path.exists())
            # the alias is validated under the new name too
            with self.assertRaisesRegex(ValueError, "rounds_to_complete"):
                ev.Plan.load(self.write(temp, {"schema": "lvz.evaluation-plan.v2", "flags_to_complete": 101}))

    def test_full_cycle_gate_uses_the_scenario_scene_not_a_fixed_three(self):
        pool_initial = {"completed_rounds": 4, "scene": 2}
        self.assertTrue(ev.full_cycle_completed(pool_initial, {"completed_rounds": 5, "scene": 2}, 20, 1, 2))
        self.assertFalse(ev.full_cycle_completed(pool_initial, {"completed_rounds": 5, "scene": 3}, 20, 1, 2))
        # ... and the default is still fog, so older calls keep their meaning.
        self.assertTrue(ev.full_cycle_completed(pool_initial, {"completed_rounds": 5, "scene": 3}, 20))
        self.assertFalse(ev.full_cycle_completed(pool_initial, {"completed_rounds": 5, "scene": 2}, 20))
        # one round of completed_rounds growth is the two flags the gate always meant
        self.assertEqual(ev.rounds_completed(pool_initial, {"completed_rounds": 5}), 1)
        self.assertEqual(ev.rounds_completed(pool_initial, {"completed_rounds": 6}), 2)
        self.assertIsNone(ev.rounds_completed(pool_initial, {}))
        self.assertFalse(ev.full_cycle_completed(pool_initial, {"completed_rounds": 5, "scene": 2}, 20, 2, 2))
        self.assertTrue(ev.full_cycle_completed(pool_initial, {"completed_rounds": 6, "scene": 2}, 20, 2, 2))


class RoundCapabilityTests(unittest.TestCase):
    """A plan may only ask for rounds the runtime can actually play out (#97)."""

    def test_a_multi_round_plan_is_refused_when_the_runtime_cannot_resubmit_cards(self):
        client = Mock()
        client.hello_result = {"capabilities": {"initialize": True}}
        ev._require_round_capability(client, ev.Plan())  # one round never needs it
        with self.assertRaisesRegex(ValueError, r"rounds_to_complete=2.*card_resubmit_mid_run.*#97"):
            ev._require_round_capability(client, ev.Plan(rounds_to_complete=2))
        client.hello_result = {"capabilities": {"card_resubmit_mid_run": True}}
        ev._require_round_capability(client, ev.Plan(rounds_to_complete=2))


class PlaySourceRoundTests(unittest.TestCase):
    """The source loop stops after ``rounds_to_complete`` rounds, not after one."""

    @staticmethod
    def observation(tick, *, rounds, scene=2, wave=20, ui=3):
        return {"version": {"epoch": 1, "tick": tick, "revision": tick}, "game_clock": tick,
                "wave": wave, "completed_rounds": rounds, "game_ui": ui, "scene": scene}

    def play(self, plan, commits, *, first_rounds=0, first_tick=1):
        """``commits`` are (tick, rounds) or (tick, rounds, game_ui) steps."""
        steps = [item if isinstance(item, dict) else
                 {"tick": item[0], "rounds": item[1], **({"ui": item[2]} if len(item) > 2 else {})}
                 for item in commits]
        client = Mock()
        client.commit.side_effect = [{"observation": self.observation(**step),
                                      "action_results": [], "stop_reason": "budget_exhausted"}
                                     for step in steps]
        strategy = Mock()
        strategy.decide.return_value = {"actions": [], "advance_ticks": 1}
        budget = Mock(stop=None)
        budget.report.return_value = {}
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        with patch.object(ev, "ScriptStrategy", return_value=strategy):
            return ev._play_source(client, Mock(), None, plan, 42,
                                   self.observation(0, rounds=first_rounds),
                                   {"observation": self.observation(first_tick, rounds=first_rounds)},
                                   Path(directory), budget)

    def test_one_round_still_stops_the_default_run(self):
        plan = ev.Plan(tick_budget=100, chunk_ticks=1, pause_points=(), scenario="jingdian12")
        result = self.play(plan, [(2, 1), (3, 2)])
        self.assertEqual((result["outcome"], result["rounds_completed"]), ("full_cycle_completed", 1))
        self.assertEqual(result["rounds_to_complete"], 1)
        self.assertTrue(result["full_cycle"])

    def test_two_rounds_do_not_stop_at_the_first_round(self):
        plan = ev.Plan(tick_budget=100, chunk_ticks=1, pause_points=(), scenario="jingdian12",
                       rounds_to_complete=2)
        result = self.play(plan, [(2, 1), (3, 2)])
        self.assertEqual((result["outcome"], result["decisions"]), ("full_cycle_completed", 2))
        self.assertEqual((result["rounds_completed"], result["rounds_to_complete"]), (2, 2))

    def test_the_gate_is_unchanged_when_the_declared_run_is_longer(self):
        plan = ev.Plan(tick_budget=2, chunk_ticks=1, pause_points=(), scenario="jingdian12",
                       rounds_to_complete=2)
        result = self.play(plan, [(2, 1)])
        self.assertEqual(result["outcome"], "tick_budget_exhausted")
        self.assertEqual(result["rounds_completed"], 1)
        self.assertTrue(result["full_cycle"], "one completed round must still satisfy the gate")

    def test_the_round_boundary_is_named_instead_of_a_generic_early_stop(self):
        # #97: one round is done and the board is back at the next round's
        # card-select screen while two were declared. The run must say why it
        # stopped instead of reporting a silent terminal_before_complete.
        plan = ev.Plan(tick_budget=100, chunk_ticks=1, pause_points=(), scenario="jingdian12",
                       rounds_to_complete=2)
        result = self.play(plan, [(2, 1), (3, 1, 2)])
        self.assertEqual(result["outcome"], "round_handoff_required")
        self.assertEqual((result["rounds_completed"], result["rounds_to_complete"]), (1, 2))
        self.assertTrue(result["full_cycle"], "the at-least-one-round gate is unchanged")

    def test_a_short_default_run_still_reports_the_historic_outcome(self):
        plan = ev.Plan(tick_budget=100, chunk_ticks=1, pause_points=(), scenario="jingdian12")
        result = self.play(plan, [(2, 0, 2)])
        self.assertEqual(result["outcome"], "terminal_before_complete")
        self.assertFalse(result["full_cycle"])


class HostedScriptTests(unittest.TestCase):
    def test_hosted_copy_keeps_as_set_zombies_and_drops_as_select_cards(self):
        source = (ROOT / "logger/avz/hosted/jing_dian_12.cpp").read_text(encoding="utf-8")
        self.assertIn("ACoroutine Script()", source)
        self.assertIn("ASetZombies({", source)
        self.assertNotIn("ASelectCards(", source)
        card_line = ", ".join(str(card) for card in JINGDIAN12_CARDS)
        self.assertIn(card_line, (ROOT / "experiments/scenarios/jingdian12/README.md").read_text(encoding="utf-8"))
        self.assertIn(card_line, (ROOT / "docs/avz-script-hosting.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
