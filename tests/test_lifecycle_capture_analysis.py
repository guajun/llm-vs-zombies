"""Capture-fact classification tests for issue #111 (v2 probe facts)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from llm_vs_zombies import lifecycle_events  # noqa: E402
from llm_vs_zombies.lifecycle_report import analyze_capture_facts  # noqa: E402

FULL_COVERAGE = {"receipt_valid": True, "probe_capability": True, "initialization_capture": True,
                 "full_window": True, "health_clean": True,
                 "initial_entity_ids": frozenset({0x00020001, 0x00030001})}


def event(kind, sequence, *, entity=0x00020001, site="phase-mowdown", before=0, after=3):
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
        base["removal"] = {"source": "dienoloot_mdead_store", "before": 0, "after": 1}
    else:
        base["recycle"] = {"state": "candidate", "slot": 0, "free_head_before": 0, "count_before": 1}
    return base


class CaptureAnalysisTests(unittest.TestCase):
    def test_confirmed_death_then_removal_and_recycle(self):
        report = analyze_capture_facts([
            event("zombie_phase_transition", 1),
            event("zombie_removal_marked", 2),
            event("zombie_slot_recycle_candidate", 3),
        ], coverage=FULL_COVERAGE)
        self.assertTrue(report["entities"][0x00020001]["confirmed_death_stage"])
        self.assertEqual(report["facts"][1]["class"], "removal_after_death")
        self.assertTrue(report["first_kill"]["proven"])
        self.assertEqual(report["summary"]["recycle_candidates"], 1)

    def test_missing_evidence_never_proves_a_kill(self):
        report = analyze_capture_facts([event("zombie_phase_transition", 1)])
        self.assertFalse(report["first_kill"]["proven"])
        self.assertFalse(report["first_kill"]["prerequisites"]["receipt_valid"])
        self.assertTrue(any("receipt" in reason for reason in report["first_kill"]["reasons"]))

    def test_droploot_and_wrong_after_are_not_death_evidence(self):
        report = analyze_capture_facts([
            event("zombie_phase_transition", 1, site="phase-drop-loot", after=1),
            event("zombie_phase_transition", 2, site="phase-mowdown", after=99),
            event("zombie_removal_marked", 3),
        ], coverage=FULL_COVERAGE)
        self.assertFalse(report["first_kill"]["proven"])
        self.assertIn(report["facts"][0]["class"], ("nondeath", "phase_unclassified"))
        self.assertEqual(report["facts"][1]["class"], "phase_unclassified")
        self.assertEqual(report["facts"][2]["class"], "removal_unclassified")

    def test_reviewer_repro_is_not_proven(self):
        report = analyze_capture_facts([{
            "schema": "lvz.lifecycle-event.v2", "kind": "zombie_phase_transition", "capture_sequence": 1,
            "entity": {"id": 65536}, "phase": {"site": "phase-dienoloot", "before": 0, "after": 99}}])
        self.assertFalse(report["first_kill"]["proven"])

    def test_earlier_unknown_removal_blocks_later_kill(self):
        report = analyze_capture_facts([
            event("zombie_removal_marked", 1, entity=0x00030001),
            event("zombie_phase_transition", 2, entity=0x00020001),
        ], coverage=FULL_COVERAGE)
        self.assertFalse(report["first_kill"]["proven"])
        self.assertEqual(report["first_kill"]["blocking_facts"][0]["capture_sequence"], 1)

    def test_repeated_phase_facts_are_preserved_in_order(self):
        report = analyze_capture_facts([
            event("zombie_phase_transition", 1),
            event("zombie_phase_transition", 2),
        ], coverage=FULL_COVERAGE)
        self.assertEqual([fact["capture_sequence"] for fact in report["facts"]], [1, 2])

    def test_unhealthy_counters_block_proof(self):
        report = analyze_capture_facts([event("zombie_phase_transition", 1)],
                                       counters={"overflow": 1}, coverage=FULL_COVERAGE)
        self.assertFalse(report["first_kill"]["proven"])
        self.assertIn("overflow", " ".join(report["first_kill"]["reasons"]))
        for counter in ("read_failed", "classify_refused", "inactive_suppressed"):
            with self.subTest(counter=counter):
                report = analyze_capture_facts([event("zombie_phase_transition", 1)],
                                               counters={counter: 1}, coverage=FULL_COVERAGE)
                self.assertFalse(report["first_kill"]["proven"])
                self.assertIn(counter, " ".join(report["first_kill"]["reasons"]))
        benign = analyze_capture_facts([event("zombie_phase_transition", 1)],
                                       counters={"live_skips": 3}, coverage=FULL_COVERAGE)
        self.assertTrue(benign["first_kill"]["proven"], benign["first_kill"]["reasons"])

    def test_envelope_records_are_unwrapped(self):
        report = analyze_capture_facts([{"event": event("zombie_phase_transition", 1)}],
                                       coverage=FULL_COVERAGE)
        self.assertEqual(report["summary"]["fact_count"], 1)


if __name__ == "__main__":
    unittest.main()
