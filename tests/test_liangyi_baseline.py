"""Regressions identified by the complete-cycle static strategy review."""
import contextlib
import copy
import io
import json
from pathlib import Path
import unittest

from test_baseline_strategy import baseline, observation, zombie


class LiangyiCycleTests(unittest.TestCase):
    def decide(self, policy, state):
        with contextlib.redirect_stdout(io.StringIO()):
            return policy.decide(state, {"remaining_ticks": 200000})

    def test_spent_card_is_confirmed_if_plant_dies_before_next_observation(self):
        state = observation()
        for seed in state["seeds"]:
            seed["cd_raw"] = seed["initial_cd"] = 0
        policy = baseline.LiangyiBaseline()
        self.assertEqual(self.decide(policy, state)["actions"][-1]["type"], baseline.SQUASH)
        after = copy.deepcopy(state)
        after["game_clock"] += 1
        seed = next(s for s in after["seeds"] if s["type"] == baseline.SQUASH)
        seed.update(cd_raw=1, initial_cd=3000, usable=False)
        self.decide(policy, after)
        self.assertTrue(policy.opening_done)
        self.assertEqual(policy.confirmed[baseline.SQUASH], 1)
        self.assertEqual(policy.failures, 0)

    def test_loss_of_sun_without_cooldown_does_not_confirm_cast(self):
        state = observation()
        for seed in state["seeds"]:
            seed["cd_raw"] = 0
        policy = baseline.LiangyiBaseline()
        self.decide(policy, state)
        after = copy.deepcopy(state)
        after["sun"] = 0
        next(s for s in after["seeds"] if s["type"] == baseline.SQUASH)["usable"] = False
        self.decide(policy, after)
        self.assertFalse(policy.opening_done)
        self.assertEqual(policy.failures, 1)

    def test_fallback_doom_never_consumes_primary_white_ice_station(self):
        policy = baseline.LiangyiBaseline()
        view = baseline.View(observation(wave=10))
        for wave in baseline.DOOM_WAVES | {10}:
            self.assertNotIn((4, 4), policy._doom_grids(view, wave))
        self.assertEqual(policy._doom_grids(view, 10)[0], (3, 4))

    def test_emergency_blover_avoids_active_giant_hammer(self):
        state = observation()
        giant = zombie(32, 2, 250, 6000)
        giant["state"] = 70
        state["zombies"] = [giant, zombie(16, 1, 120, 270)]
        policy = baseline.LiangyiBaseline()
        result = self.decide(policy, state)
        action = result["actions"][-1]
        self.assertEqual(action["type"], baseline.BLOVER)
        self.assertNotEqual((action["row"], action["col"]), (2, 3))
        self.assertFalse(baseline.View(state).imminent_hazard((action["row"], action["col"]), fodder=True))

    def test_grounded_balloon_is_not_treated_as_blowable(self):
        state = observation()
        walker = zombie(16, 1, 80, 270)
        walker["state"] = 75
        state["zombies"] = [walker]
        policy = baseline.LiangyiBaseline()
        policy.opening_done = True
        actions = self.decide(policy, state)["actions"]
        self.assertTrue(actions)
        self.assertEqual(actions[-1]["type"], baseline.JALAPENO)
        self.assertFalse(any(a.get("type") == baseline.BLOVER for a in actions))

    def test_tunneling_miners_do_not_spend_emergency_ash(self):
        # Recorded 017's sole emergency triggers at ticks 1420 and 1640.
        for row, x in ((5, 243.95721435546875), (6, 119.64140319824219)):
            with self.subTest(row=row):
                state = observation()
                miner = zombie(17, row, x, 270)
                miner["state"] = 32
                state["zombies"] = [miner]
                policy = baseline.LiangyiBaseline()
                policy.opening_done = True
                actions = self.decide(policy, state)["actions"]
                self.assertFalse(any(a.get("type") in (baseline.JALAPENO, baseline.CHERRY) for a in actions))

    def test_left_walking_miner_without_pickaxe_retains_emergency_response(self):
        # Original phase 38 is NOT IsWalkingBackwards; it can enter the house.
        state = observation()
        miner = zombie(17, 5, 120, 270)
        miner["state"] = 38
        state["zombies"] = [miner]
        policy = baseline.LiangyiBaseline()
        policy.opening_done = True
        action = self.decide(policy, state)["actions"][-1]
        self.assertEqual((action["type"], action["row"]), (baseline.JALAPENO, 5))

    def test_emerging_stunned_and_right_walking_miners_are_not_incoming(self):
        # Recorded 023's miner at ticks 1881, 1942 and 2301. It then moves
        # right to x56.077 at tick2979 while the existing formation kills it.
        for phase, x in ((33, 9.424354553222656), (36, 9.424354553222656),
                         (37, 10.207267761230469)):
            with self.subTest(phase=phase):
                state = observation()
                miner = zombie(17, 6, x, 270)
                miner.update(state=phase, y=475, armor1=100)
                state["zombies"] = [miner]
                policy = baseline.LiangyiBaseline()
                policy.opening_done = True
                actions = self.decide(policy, state)["actions"]
                self.assertFalse(any(a.get("type") in (baseline.JALAPENO, baseline.CHERRY) for a in actions))

    def test_right_walking_miner_does_not_disable_real_core_pumpkin_repair(self):
        state = observation()
        miner = zombie(17, 2, 9.77683162689209, 270)
        miner.update(state=37, y=135)
        state["zombies"] = [miner]
        next(p for p in state["plants"] if p["type"] == baseline.PUMPKIN and (p["row"], p["col"]) == (2, 1))["hp"] = 100
        policy = baseline.LiangyiBaseline()
        policy.opening_done = True
        action = self.decide(policy, state)["actions"][-1]
        self.assertEqual((action["type"], action["row"], action["col"]), (baseline.PUMPKIN, 2, 1))

    def test_tunneling_miner_does_not_mask_other_ground_emergency(self):
        state = observation()
        miner = zombie(17, 5, 50, 270)
        miner["state"] = 32
        threat = zombie(7, 6, 100, 270)
        threat["id"] = 101
        state["zombies"] = [miner, threat]
        policy = baseline.LiangyiBaseline()
        policy.opening_done = True
        action = self.decide(policy, state)["actions"][-1]
        self.assertEqual((action["type"], action["row"]), (baseline.JALAPENO, 6))

    @staticmethod
    def recorded_cherry_state():
        fixture = json.loads((Path(__file__).parent / "fixtures/liangyi_023_emergency_cherry.json").read_text(encoding="utf8"))
        policy = baseline.LiangyiBaseline()
        policy.wave = 1
        policy.opening_done = True
        policy.hold_until = 5032
        return fixture, policy

    def test_recorded_cherry_placement_guard_covers_target_instead_of_far_crowd(self):
        fixture, policy = self.recorded_cherry_state()
        state = fixture["observation"]
        # The original, unfiltered crowd score really selects the recorded miss.
        self.assertEqual(policy._cherry_grids(baseline.View(state), 5)[0], (5, 8))
        # This old miner trigger is now separately excluded as non-incoming.
        # Exercise the placement guard itself against the real recorded miss.
        view = baseline.View(state)
        target = next(z for z in view.zombies if z["id"] == fixture["source"]["trigger_id"])
        action = policy._cast(view, baseline.CHERRY, policy._emergency_cherry_grids(view, target, 5))[-1]
        self.assertEqual((action["type"], action["row"], action["col"]), (baseline.CHERRY, 6, 2))
        # Independently check actual 1.0.0.1051 circle/rectangle geometry at
        # the recorded blast time: phase 36, zero altitude, mirrored digger
        # hurtbox x+42..70 and y..y+115. The old blast left hp/armor unchanged.
        target = fixture["source"]["post_blast"]
        def overlaps(candidate):
            cx, cy = candidate["col"]*80, (candidate["row"]-1)*85+120
            dx = max(int(target["x"])+42-cx, cx-int(target["x"])-70, 0)
            dy = max(int(target["y"])-cy, cy-int(target["y"])-115, 0)
            return dx*dx+dy*dy <= 115**2
        self.assertFalse(overlaps(fixture["source"]["recorded_action"]))
        self.assertTrue(overlaps(action))

    def test_emergency_cherry_does_not_fall_back_to_unrelated_legal_square(self):
        fixture, policy = self.recorded_cherry_state()
        state = fixture["observation"]
        # All near-target squares are unavailable, while the old distant
        # (5,8) remains legal. Saving the card is preferable to that known miss.
        for row in (5, 6):
            for col in (1, 2, 3):
                state["plantable"][str(baseline.CHERRY)][row-1][col-1] = False
        view = baseline.View(state)
        target = next(z for z in view.zombies if z["id"] == fixture["source"]["trigger_id"])
        self.assertIsNone(policy._cast(view, baseline.CHERRY, policy._emergency_cherry_grids(view, target, 5)))

    def test_full_recorded_023_rising_miner_no_longer_spends_emergency_ash(self):
        fixture, policy = self.recorded_cherry_state()
        actions = self.decide(policy, fixture["observation"])["actions"]
        self.assertFalse(any(a.get("type") in (baseline.JALAPENO, baseline.CHERRY) for a in actions))

    def test_planned_cherry_still_uses_original_crowd_ranking(self):
        fixture, policy = self.recorded_cherry_state()
        state = fixture["observation"]
        policy.events = [{"id": 1, "kind": "cherry", "due": state["game_clock"], "wave": 3}]
        action = self.decide(policy, state)["actions"][-1]
        self.assertEqual((action["type"], action["row"], action["col"]), (baseline.CHERRY, 5, 8))

    def test_giant_emergency_also_covers_its_critical_target(self):
        state = observation()
        target = zombie(32, 2, 240, 6000)
        target.update(y=135, id=200)
        state["zombies"] = [target]
        for index in range(12):
            remote = zombie(32, 2, 600+index, 6000)
            remote.update(y=135, id=300+index)
            state["zombies"].append(remote)
        for seed in state["seeds"]:
            if seed["type"] in (baseline.SQUASH, baseline.ICE, baseline.JALAPENO):
                seed["usable"] = False
        policy = baseline.LiangyiBaseline()
        policy.opening_done = True
        action = self.decide(policy, state)["actions"][-1]
        self.assertEqual(action["type"], baseline.CHERRY)
        # The real grounded giant's hurtbox is x-17..108, y-38..116.
        cx, cy = action["col"]*80, (action["row"]-1)*85+120
        dx = max(223-cx, cx-348, 0)
        dy = max(97-cy, cy-251, 0)
        self.assertLessEqual(dx*dx+dy*dy, 115**2)


if __name__ == "__main__":
    unittest.main()
