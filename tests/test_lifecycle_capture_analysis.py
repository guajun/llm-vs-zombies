"""Capture-fact classification tests for issue #111 (v2 probe facts)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from llm_vs_zombies import lifecycle_events  # noqa: E402
from llm_vs_zombies.lifecycle_report import analyze_capture_facts  # noqa: E402


def event(kind, sequence, *, entity=0x20001, site="phase-dienoloot", before=0, after=3):
    base = {
        "schema": lifecycle_events.PROBE_EVENT_SCHEMA, "kind": kind, "capture_sequence": sequence,
        "version": None, "version_phase": "uncontrolled_update", "engine_call_id": None,
        "entity": {"id": entity, "slot": entity & 0xFFFF, "generation": entity >> 16},
        "object": {"class": "zombie", "on_board": True, "wave": 0, "board": 0x123456},
        "probe": {"name": "zombie-lifecycle-store", "schema": lifecycle_events.PROBE_EVENT_SCHEMA,
                  "sequence_domain": lifecycle_events.SEQUENCE_DOMAIN},
        "complete": True,
    }
    if kind == "zombie_phase_transition":
        base["phase"] = {"site": site, "before": before, "after": after}
    elif kind == "zombie_removal_marked":
        base["removal"] = {"source": "phase-lifetime", "before": 0, "after": 1}
    else:
        base["recycle"] = {"state": "candidate", "slot": 0, "free_head_before": 0, "count_before": 1}
    return base


class CaptureAnalysisTests(unittest.TestCase):
    def test_confirmed_death_then_removal_and_recycle(self):
        report = analyze_capture_facts([
            event("zombie_phase_transition", 1),
            event("zombie_removal_marked", 2),
            event("zombie_slot_recycle_candidate", 3),
        ])
        self.assertTrue(report["entities"][0x20001]["confirmed_death_stage"])
        self.assertEqual(report["facts"][1]["class"], "removal_after_death")
        self.assertTrue(report["first_kill"]["proven"])
        self.assertEqual(report["summary"]["recycle_candidates"], 1)

    def test_droploot_is_not_death_evidence(self):
        report = analyze_capture_facts([
            event("zombie_phase_transition", 1, site="phase-drop-loot", after=1),
            event("zombie_removal_marked", 2),
        ])
        self.assertFalse(report["first_kill"]["proven"])
        self.assertEqual(report["facts"][0]["class"], "nondeath")
        self.assertEqual(report["facts"][1]["class"], "removal_unclassified")

    def test_earlier_unknown_removal_blocks_later_kill(self):
        report = analyze_capture_facts([
            event("zombie_removal_marked", 1, entity=0x30001),
            event("zombie_phase_transition", 2, entity=0x20001),
        ])
        self.assertFalse(report["first_kill"]["proven"])
        self.assertEqual(report["first_kill"]["blocking_facts"][0]["capture_sequence"], 1)

    def test_repeated_phase_facts_are_preserved_in_order(self):
        report = analyze_capture_facts([
            event("zombie_phase_transition", 1, site="phase-mowdown"),
            event("zombie_phase_transition", 2, site="phase-mowdown"),
        ])
        self.assertEqual([fact["capture_sequence"] for fact in report["facts"]], [1, 2])

    def test_unhealthy_counters_block_proof(self):
        report = analyze_capture_facts([event("zombie_phase_transition", 1)], counters={"overflow": 1})
        self.assertFalse(report["first_kill"]["proven"])
        self.assertIn("overflow", " ".join(report["first_kill"]["reasons"]))

    def test_envelope_records_are_unwrapped(self):
        report = analyze_capture_facts([{"event": event("zombie_phase_transition", 1)}])
        self.assertEqual(report["summary"]["fact_count"], 1)


if __name__ == "__main__":
    unittest.main()
