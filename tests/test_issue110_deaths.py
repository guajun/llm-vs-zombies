"""Offline behavioral fixtures: no game, no AvZ installation required."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import issue110_deaths as death


def zombie(state=0, hp=270, disappeared=0, generation=1, wave=0):
    values = dict(type=0, state=state, hp=hp, armor1=0, armor2=0, disappeared=disappeared, at_wave=wave)
    return {"id_or_free_next": generation << 16,
            "fields": {offset: values[name] for name, offset in death.FIELDS.items()}}


def stream(*entities):
    items = []
    for index, entity in enumerate(entities):
        call_id = index // 2 + 1
        pre = index % 2 == 0
        version = {"epoch": 3, "tick": index // 2 + (not pre), "revision": 0}
        call = {"engine_call_id": call_id, "pre_version": {"epoch": 3, "tick": call_id - 1, "revision": 0},
                "engine_call_completed": not pre, "board_identity_preserved": True, "native_tick_delta": 1}
        items.append({"index": index, "row": {"seq": index * 3, "kind": "pre_step" if pre else "post_step",
                        "version": version, "payload": {"engine_call": call}},
                      "state": {"zombies": {"slots": {"0": entity} if entity else {}}}})
    return items


def analyze(items):
    return death.scan(items, {"epoch": 3, "tick": 0, "revision": 0},
                      {"epoch": 3, "tick": len(items) // 2, "revision": 0})


class DeathBehaviorTests(unittest.TestCase):
    def test_death_positive_hp_then_delayed_release(self):
        result = analyze(stream(zombie(), zombie(2), zombie(2), None))
        self.assertEqual(len(result["confirmed_entries"]), 1)
        self.assertEqual(result["confirmed_entries"][0]["after"]["raw"]["hp"], 270)
        self.assertEqual(result["removals"][0]["classification"], "release_after_confirmed_death")
        self.assertEqual(result["uncertain_events"], [])
        self.assertEqual(result["first_window_kill"], "unverified")

    def test_hp_zero_does_not_confirm_death(self):
        result = analyze(stream(zombie(), zombie(hp=0)))
        self.assertEqual(result["death_observations"], [])

    def test_direct_removal_is_unknown(self):
        result = analyze(stream(zombie(), None))
        self.assertEqual(result["confirmed_entries"], [])
        self.assertEqual(result["uncertain_events"][0]["reason"], "removed_without_confirmed_death")

    def test_disappeared_without_death_then_release(self):
        result = analyze(stream(zombie(), zombie(disappeared=1), zombie(disappeared=1), None))
        self.assertEqual(result["confirmed_entries"], [])
        self.assertEqual(len(result["uncertain_events"]), 2)

    def test_slot_reuse_does_not_inherit_live_predecessor(self):
        result = analyze(stream(zombie(generation=1), zombie(2, generation=2)))
        self.assertEqual(result["confirmed_entries"], [])
        self.assertEqual(result["removals"][0]["replacement"]["generation"], 2)

    def test_initial_dead_is_observation_not_entry(self):
        result = analyze(stream(zombie(2), None))
        self.assertEqual(result["initial_objects"][0]["classification"], "preexisting_death")
        self.assertEqual(len(result["death_observations"]), 1)
        self.assertEqual(result["confirmed_entries"], [])

    def test_initial_disappeared_is_not_new_kill(self):
        result = analyze(stream(zombie(disappeared=1, wave=0xfffffffe), None))
        self.assertEqual(result["removals"][0]["classification"], "release_of_preexisting_disappeared_object")
        self.assertEqual(result["uncertain_events"], [])

    def test_same_tick_pre_post_revision_not_merged(self):
        items = stream(zombie(), zombie(), zombie(), zombie(2))
        items[2]["row"]["version"]["revision"] = 1
        items[3]["row"]["payload"]["engine_call"]["pre_version"]["revision"] = 1
        result = analyze(items)
        event = result["confirmed_entries"][0]
        self.assertEqual(result["boundaries"], 4)
        self.assertEqual(event["before"]["coordinate"]["version"]["revision"], 1)
        self.assertEqual(event["after"]["coordinate"]["engine_call_id"], 2)

    def test_missing_field_fails_closed(self):
        items = stream(zombie(), zombie(2))
        del items[0]["state"]["zombies"]["slots"]["0"]["fields"][death.FIELDS["hp"]]
        result = analyze(items)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["confirmed_entries"], [])

    def test_unknown_state_is_unclassified_and_cannot_confirm_entry(self):
        result = analyze(stream(zombie(999), zombie(2)))
        self.assertIn(999, result["unclassified_non_death_codes"])
        self.assertEqual(result["confirmed_entries"], [])
        self.assertEqual(result["uncertain_events"][0]["reason"], "death_observed_without_verified_live_predecessor")

    def test_truncation_and_call_gap_fail(self):
        for items in (stream(zombie()), stream(zombie(), zombie(2))[1:]):
            self.assertEqual(analyze(items)["status"], "failed")
        items = stream(zombie(), zombie(), zombie(), zombie(2))
        items[2]["row"]["payload"]["engine_call"]["engine_call_id"] = 9
        self.assertEqual(analyze(items)["status"], "failed")

    def test_same_count_different_identity_does_not_match(self):
        a = analyze(stream(zombie(), zombie(2)))
        b = analyze(stream(zombie(generation=2), zombie(2, generation=2)))
        self.assertNotEqual(death.event_signature(a), death.event_signature(b))

    def test_immediate_disappearance_at_first_death_boundary_is_retained(self):
        items = stream(zombie(), zombie(2))
        for i, item in enumerate(items):
            second = zombie(disappeared=i)
            second["id_or_free_next"] += 1
            item["state"]["zombies"]["slots"]["1"] = second
        result = analyze(items)
        self.assertEqual(len(result["first_confirmed_entries"]), 1)
        self.assertEqual(len(result["earlier_or_same_boundary_uncertainty"]), 1)

    def test_all_tied_first_deaths_retained(self):
        items = stream(zombie(), zombie(2))
        for item in items:
            second = copy.deepcopy(item["state"]["zombies"]["slots"]["0"])
            second["id_or_free_next"] += 1
            item["state"]["zombies"]["slots"]["1"] = second
        self.assertEqual(len(analyze(items)["first_confirmed_entries"]), 2)

    def test_exclusive_output_and_missing_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            out = root / "output"
            self.assertEqual(death.main(["--root", str(root), "--output", str(out)]), 1)
            self.assertEqual(json.loads((out / "failure.json").read_text())["first_kill_gate"], "unverified")
            with self.assertRaises(FileExistsError):
                death.main(["--root", str(root), "--output", str(out)])

    def test_signed_hp_preserves_bits(self):
        self.assertEqual(death.signed(0xffffffff), -1)
        self.assertEqual(death.signed(270), 270)

    def test_report_cannot_be_written_into_source_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            out = root / "experiments/trees/derived"
            with self.assertRaises(SystemExit):
                death.main(["--root", str(root), "--output", str(out)])
            self.assertFalse(out.exists())

    def test_missing_identity_is_not_an_empty_slot(self):
        items = stream(zombie(), zombie(2))
        items[0]["state"]["zombies"]["slots"]["0"] = {}
        self.assertEqual(analyze(items)["status"], "failed")


if __name__ == "__main__":
    unittest.main()
