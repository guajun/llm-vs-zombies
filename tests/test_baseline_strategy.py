import contextlib
import copy
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest

from llm_vs_zombies.evaluation import ScriptStrategy
from llm_vs_zombies.session import SessionTrace


spec = importlib.util.spec_from_file_location("liangyi_baseline_under_test", Path(__file__).resolve().parents[1] / "examples/liangyi_baseline.py")
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)


def observation(wave=0, clock=100, wave_time=0):
    types = {(2, 2): 42, (5, 2): 42, (3, 6): 42, (4, 6): 42,
             (3, 2): 37, (4, 2): 37}
    plants = []
    for index, (row, col) in enumerate(sorted(baseline.CORE)):
        plants.append({"id": index+1, "type": types.get((row, col), 41), "row": row, "col": col, "hp": 300})
    for row, col in baseline.PUMPKINS:
        plants.append({"id": len(plants)+1, "type": 30, "row": row, "col": col, "hp": 4000})
    for row, col in sorted(baseline.CORE):
        if row in (3, 4):
            plants.append({"id": len(plants)+1, "type": 16, "row": row, "col": col, "hp": 300})
    return {"version": {"epoch": 1, "tick": 0, "revision": 0}, "game_ui": 3, "scene": 3,
            "wave": wave, "game_clock": clock, "wave_time": wave_time, "sun": 8000,
            "plants": plants, "zombies": [], "refresh_countdown": 500,
            "seeds": [{"type": 48 if kind == 63 else kind, "imitator_type": 14 if kind == 63 else -1,
                       "usable": True, "cd_raw": 5000, "initial_cd": 5000} for kind in baseline.COST],
            "plantable": {str(kind): [[True]*9 for _ in range(6)] for kind in baseline.COST}}


def zombie(kind, row, x, hp=3000, **other):
    return dict(id=100, type=kind, row=row, x=x, y=(row-1)*85+80, hp=hp, state=0,
                speed=0.2, freeze=0, slow=0, **other)


class BaselineStrategyTests(unittest.TestCase):
    def decide(self, policy, obs, remaining=10000):
        with contextlib.redirect_stdout(io.StringIO()):
            return policy.decide(obs, {"remaining_ticks": remaining, "decision_index": 0, "seed": 42})

    def test_balloon_emergency_precedes_opening_squash(self):
        obs = observation()
        obs["zombies"] = [zombie(16, 1, 120, 270)]
        result = self.decide(baseline.LiangyiBaseline(), obs)
        self.assertEqual(result["actions"][-1]["type"], baseline.BLOVER)
        self.assertNotIn((result["actions"][-1]["row"], result["actions"][-1]["col"]), baseline.CORE)

    def test_resource_budget_and_remaining_ticks_are_respected(self):
        obs = observation()
        obs["sun"] = 0
        result = self.decide(baseline.LiangyiBaseline(), obs, remaining=1)
        self.assertLessEqual(result["advance_ticks"], 1)
        self.assertTrue(all(a.get("type") == baseline.PUFF for a in result["actions"]))
        self.assertEqual(self.decide(baseline.LiangyiBaseline(), obs, remaining=0), {"actions": [], "advance_ticks": 0})

    def test_never_shovels_core_and_repairs_only_pumpkin_layer(self):
        obs = observation()
        view = baseline.View(obs)
        policy = baseline.LiangyiBaseline()
        self.assertIsNone(policy._actions(view, baseline.CHERRY, (2, 2)))
        obs["plantable"]["30"][1][1] = False
        damaged = next(p for p in obs["plants"] if p["type"] == 30 and (p["row"], p["col"]) == (2, 2))
        damaged["hp"] = 100
        result = self.decide(policy, obs)
        self.assertEqual(result["actions"], [{"op": "shovel", "target_type": 30, "row": 2, "col": 2},
                                             {"op": "plant", "type": 30, "row": 2, "col": 2}])

    def test_real_twin_sunflower_type_is_preserved_and_can_receive_pumpkin(self):
        obs = observation()
        policy = baseline.LiangyiBaseline()
        with contextlib.redirect_stdout(io.StringIO()):
            policy._observe(baseline.View(obs))
        self.assertEqual(policy.missing_core, set())
        obs["plantable"]["30"][1][0] = False
        self.assertEqual(policy._actions(baseline.View(obs), baseline.PUMPKIN, (2, 1)),
                         [{"op": "shovel", "target_type": 30, "row": 2, "col": 1},
                          {"op": "plant", "type": 30, "row": 2, "col": 1}])

    def test_rejected_attempt_is_not_marked_success_and_gets_a_progress_tick(self):
        obs = observation()
        policy = baseline.LiangyiBaseline()
        first = self.decide(policy, obs)
        self.assertEqual(first["actions"][-1]["type"], baseline.SQUASH)
        # The executor rejected the action, so no new plant, cooldown or clock.
        second = self.decide(policy, copy.deepcopy(obs))
        self.assertEqual(second, {"actions": [], "advance_ticks": 1})
        self.assertFalse(policy.opening_done)
        self.assertEqual(policy.failures, 1)

    def test_imitator_transforming_plant_confirms_cast_without_guessing_usable(self):
        obs = observation(wave=1, clock=1000, wave_time=480)
        policy = baseline.LiangyiBaseline()
        policy.opening_done = True
        result = self.decide(policy, obs)
        self.assertEqual([a["type"] for a in result["actions"]], [baseline.LILY, baseline.WHITE_ICE])
        target = result["actions"][-1]
        updated = copy.deepcopy(obs)
        updated["game_clock"] += 1
        updated["wave_time"] += 1
        updated["plants"].append({"id": 9999, "type": 48, "row": target["row"], "col": target["col"], "hp": 300})
        self.decide(policy, updated)
        self.assertEqual(policy.confirmed.get(baseline.WHITE_ICE), 1)
        self.assertFalse(any(e["kind"] == "white_ice" for e in policy.events))

    def test_water_doom_skips_a_known_crater_and_checks_combined_cost(self):
        obs = observation(wave=2, clock=2200, wave_time=2200)
        obs["plantable"]["16"][3][7] = False
        obs["plantable"]["15"][3][7] = False
        policy = baseline.LiangyiBaseline()
        result = policy._cast(baseline.View(obs), baseline.DOOM, [(4, 8), (3, 7)], allow_lily=True)
        self.assertEqual(result, [{"op": "plant", "type": 16, "row": 3, "col": 7},
                                  {"op": "plant", "type": 15, "row": 3, "col": 7}])
        obs["sun"] = 140  # enough for doom, insufficient for lily + doom.
        self.assertIsNone(policy._cast(baseline.View(obs), baseline.DOOM, [(3, 7)], allow_lily=True))

    def test_extra_ice_call_changes_next_wave_alternation(self):
        policy = baseline.LiangyiBaseline()
        obs = observation(wave=9, clock=3000, wave_time=2500)
        with contextlib.redirect_stdout(io.StringIO()):
            policy._observe(baseline.View(obs))
        self.assertEqual([e["kind"] for e in policy.events if e["kind"] in ("ice", "white_ice")], ["white_ice", "ice"])
        obs = observation(wave=10, clock=4000, wave_time=480)
        with contextlib.redirect_stdout(io.StringIO()):
            policy._observe(baseline.View(obs))
        current = [e for e in policy.events if e["wave"] == 10 and e["kind"] == "white_ice"]
        self.assertEqual(len(current), 1)

    def test_calm_fodder_is_limited_to_four_shore_slots(self):
        obs = observation()
        for row, col in ((2, 4), (5, 4), (2, 3), (5, 3)):
            obs["plants"].append({"id": len(obs["plants"])+1, "type": 8, "row": row, "col": col, "hp": 300})
        policy = baseline.LiangyiBaseline()
        policy.opening_done = True
        self.assertEqual(self.decide(policy, obs)["actions"], [])

    def test_actual_recorded_console_loads_strategy_and_keeps_its_state(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            with SessionTrace(Path(temporary) / "session.jsonl") as trace:
                strategy = ScriptStrategy(None, trace, Path(baseline.__file__), 100)
                context = {"remaining_ticks": 1000, "decision_index": 0, "seed": 42}
                first = strategy.decide(observation(), context)
                self.assertEqual(first["actions"][-1]["type"], baseline.SQUASH)
                context["decision_index"] += 1
                # An unchanged next observation signifies a failed action.
                self.assertEqual(strategy.decide(observation(), context),
                                 {"actions": [], "advance_ticks": 1})


if __name__ == "__main__":
    unittest.main()
