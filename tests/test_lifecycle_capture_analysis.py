"""Capture-fact classification tests for issue #111 (v2 probe facts)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from llm_vs_zombies import lifecycle_events  # noqa: E402
from llm_vs_zombies.lifecycle_report import analyze_capture_facts  # noqa: E402

ENTITY = 0x00020001
OTHER = 0x00030001
LIVE_INITIAL = {ENTITY: {"state": 0, "disappeared": False}, OTHER: {"state": 0, "disappeared": False}}
FULL_COVERAGE = {"receipt_valid": True, "probe_capability": True, "initialization_capture": True,
                 "full_window": True, "health_clean": True, "initial_entities": LIVE_INITIAL}


def init_event(sequence, *, entity=ENTITY, engine_call_id=42, invocation=1):
    return {
        "schema": lifecycle_events.EVENT_SCHEMA, "kind": lifecycle_events.KIND_INITIALIZATION,
        "capture_sequence": sequence, "version": {"epoch": 1, "tick": 0, "revision": 0},
        "version_phase": "controlled_boundary", "engine_call_id": engine_call_id,
        "invocation": {"invocation_id": invocation, "depth": 0, "parent_invocation_id": None},
        "entity": {"id": entity, "slot": entity & 0xFFFF, "generation": entity >> 16},
        "before_after": {"before": None, "after": {"id": entity, "slot": entity & 0xFFFF,
                                                   "generation": entity >> 16, "row0": 0,
                                                   "type": 16, "game_clock": 0}},
        "classification": {"class": "initialization", "cause": "unknown"},
        "probe": {"name": "zombie-initialize-exit", "schema": "lvz.spawn.v1",
                  "sequence_domain": lifecycle_events.SEQUENCE_DOMAIN},
        "complete": True,
    }


def event(kind, sequence, *, entity=ENTITY, site="phase-mowdown", before=0, after=3, on_board=True,
          engine_call_id=None):
    base = {
        "schema": lifecycle_events.PROBE_EVENT_SCHEMA, "kind": kind, "capture_sequence": sequence,
        "version": None, "version_phase": "uncontrolled_update", "engine_call_id": engine_call_id,
        "entity": {"id": entity, "slot": entity & 0xFFFF, "generation": entity >> 16},
        "object": {"class": "zombie", "on_board": on_board, "wave": 0 if on_board else -2,
                   "board": 0x123456},
        "probe": {"name": "zombie-lifecycle-store", "schema": lifecycle_events.PROBE_EVENT_SCHEMA,
                  "sequence_domain": lifecycle_events.SEQUENCE_DOMAIN},
        "complete": True,
    }
    if kind == "zombie_phase_transition":
        base["phase"] = {"site": site, "before": before, "after": after}
    elif kind == "zombie_removal_marked":
        base["removal"] = {"source": "dienoloot_mdead_store", "before": 0, "after": 1,
                           "frame": {"status": "missing", "return_into_diewithloot": None,
                                     "return_into_applyburn": None, "callsite_bytes_match": False}}
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
        self.assertTrue(report["entities"][ENTITY]["confirmed_death_stage"])
        self.assertEqual(report["facts"][1]["class"], "removal_after_death")
        self.assertTrue(report["first_kill"]["proven"], report["first_kill"])
        self.assertEqual(report["summary"]["recycle_candidates"], 1)

    def test_missing_evidence_never_proves_a_kill(self):
        report = analyze_capture_facts([event("zombie_phase_transition", 1)])
        self.assertFalse(report["first_kill"]["proven"])
        self.assertFalse(report["first_kill"]["prerequisites"]["receipt_valid"])
        self.assertTrue(any("receipt" in reason for reason in report["first_kill"]["reasons"]))

    def test_droploot_wrong_after_and_already_dying_are_not_evidence(self):
        report = analyze_capture_facts([
            event("zombie_phase_transition", 1, site="phase-drop-loot", after=1),
            event("zombie_phase_transition", 2, site="phase-mowdown", after=99),
            event("zombie_phase_transition", 3, site="phase-mowdown", before=2, after=3),
            event("zombie_removal_marked", 4),
        ], coverage=FULL_COVERAGE)
        self.assertFalse(report["first_kill"]["proven"])
        self.assertEqual(report["facts"][0]["class"], "nondeath")
        self.assertEqual(report["facts"][1]["class"], "phase_unclassified")
        self.assertEqual(report["facts"][2]["class"], "already_dying")
        self.assertEqual(report["facts"][2]["onset"], "unknown")
        self.assertEqual(report["facts"][3]["class"], "removal_after_death")
        self.assertEqual(report["summary"]["unknown_onsets"], 1)

    def test_already_dying_then_validated_chain_is_not_a_new_first_kill(self):
        already = event("zombie_phase_transition", 1, site="phase-mowdown", before=2, after=3)
        removal = event("zombie_removal_marked", 2)
        removal["removal"]["frame"] = {"status": "validated",
                                       "return_into_diewithloot": 0x5302FF,
                                       "return_into_applyburn": 0x532FC7,
                                       "callsite_bytes_match": True}
        report = analyze_capture_facts([already, removal], coverage=FULL_COVERAGE)
        self.assertFalse(report["first_kill"]["proven"], report["first_kill"])
        self.assertEqual(report["facts"][1]["class"], "removal_after_death")
        self.assertTrue(any("onset" in reason for reason in report["first_kill"]["reasons"]))

    def test_reviewer_repro_is_not_proven(self):
        report = analyze_capture_facts([{
            "schema": "lvz.lifecycle-event.v2", "kind": "zombie_phase_transition", "capture_sequence": 1,
            "entity": {"id": 65536}, "phase": {"site": "phase-dienoloot", "before": 0, "after": 99}}])
        self.assertFalse(report["first_kill"]["proven"])

    def test_preview_facts_are_scoped_out_of_gameplay_proof(self):
        report = analyze_capture_facts([
            event("zombie_phase_transition", 1, on_board=False),
        ], coverage=FULL_COVERAGE)
        self.assertFalse(report["first_kill"]["proven"])
        self.assertEqual(report["summary"]["preview_facts"], 1)
        self.assertEqual(report["preview_facts"][0]["scope"], "preview")

    def test_preview_cleanup_then_gameplay_death_proves(self):
        report = analyze_capture_facts([
            event("zombie_phase_transition", 1, on_board=False),
            event("zombie_phase_transition", 2),
        ], coverage=FULL_COVERAGE)
        self.assertTrue(report["first_kill"]["proven"], report["first_kill"])
        self.assertEqual(report["summary"]["preview_facts"], 1)

    def test_earlier_unknown_removal_blocks_later_kill(self):
        report = analyze_capture_facts([
            event("zombie_removal_marked", 1, entity=OTHER),
            event("zombie_phase_transition", 2, entity=ENTITY),
        ], coverage=FULL_COVERAGE)
        self.assertFalse(report["first_kill"]["proven"])
        self.assertEqual(report["first_kill"]["blocking_facts"][0]["capture_sequence"], 1)

    def test_future_initializer_is_not_a_prior_predecessor(self):
        report = analyze_capture_facts([
            event("zombie_phase_transition", 1),
            init_event(2),
        ], coverage={**FULL_COVERAGE, "initial_entities": {}})
        self.assertFalse(report["first_kill"]["proven"])
        self.assertEqual(report["facts"][0]["predecessor"], "unknown_predecessor")

    def test_initial_residue_uses_the_raw_live_state(self):
        dead = {ENTITY: {"state": 3, "disappeared": False}}
        report = analyze_capture_facts([event("zombie_phase_transition", 1)],
                                       coverage={**FULL_COVERAGE, "initial_entities": dead})
        self.assertFalse(report["first_kill"]["proven"])
        self.assertEqual(report["facts"][0]["predecessor"], "initial_residue_doomed")
        live = {ENTITY: {"state": 0, "disappeared": False}}
        report = analyze_capture_facts([event("zombie_phase_transition", 1)],
                                       coverage={**FULL_COVERAGE, "initial_entities": live})
        self.assertTrue(report["first_kill"]["proven"], report["first_kill"])
        self.assertEqual(report["facts"][0]["predecessor"], "initial_residue")

    def test_same_call_lifetime_refers_to_the_removal(self):
        report = analyze_capture_facts([
            init_event(1),
            event("zombie_phase_transition", 2, engine_call_id=42),
            event("zombie_removal_marked", 3, engine_call_id=42),
        ], coverage={**FULL_COVERAGE, "initial_entities": {}})
        self.assertNotIn("same_call_lifetime", report["facts"][0])
        self.assertTrue(report["facts"][1]["same_call_lifetime"])
        self.assertEqual(report["summary"]["same_call_lifetimes"], 1)

    def test_validated_applyburn_chain_is_a_direct_death(self):
        removal = event("zombie_removal_marked", 1)
        removal["removal"]["frame"] = {"status": "validated",
                                       "return_into_diewithloot": 0x5302FF,
                                       "return_into_applyburn": 0x532FC7,
                                       "callsite_bytes_match": True}
        report = analyze_capture_facts([removal], coverage=FULL_COVERAGE)
        self.assertEqual(report["facts"][0]["class"], "confirmed_death_path")
        self.assertEqual(report["facts"][0]["death_path"], "applyburn_diewithloot_dienoloot")
        self.assertTrue(report["first_kill"]["proven"], report["first_kill"])

    def test_unvalidated_removal_is_never_a_death(self):
        cases = [
            ("no frame", None),
            ("foreign returns", {"status": "foreign_return", "return_into_diewithloot": 0x1111,
                                 "return_into_applyburn": None, "callsite_bytes_match": True}),
            ("bytes mismatch", {"status": "validated", "return_into_diewithloot": 0x5302FF,
                                "return_into_applyburn": 0x532FC7, "callsite_bytes_match": False}),
        ]
        for label, frame in cases:
            with self.subTest(case=label):
                removal = event("zombie_removal_marked", 1)
                if frame is not None:
                    removal["removal"]["frame"] = frame
                report = analyze_capture_facts([removal], coverage=FULL_COVERAGE)
                self.assertEqual(report["facts"][0]["class"], "removal_unclassified")
                self.assertFalse(report["first_kill"]["proven"])
        repeated = event("zombie_removal_marked", 1)
        repeated["removal"]["before"] = 1
        repeated["removal"]["frame"] = {"return_into_diewithloot": 0x5302FF,
                                        "return_into_applyburn": 0x532FC7,
                                        "callsite_bytes_match": True}
        report = analyze_capture_facts([repeated], coverage=FULL_COVERAGE)
        self.assertEqual(report["facts"][0]["class"], "removal_unclassified")

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
        self.assertTrue(benign["first_kill"]["proven"], benign["first_kill"])

    def test_envelope_records_are_unwrapped(self):
        report = analyze_capture_facts([{"event": event("zombie_phase_transition", 1)}],
                                       coverage=FULL_COVERAGE)
        self.assertEqual(report["summary"]["fact_count"], 1)


if __name__ == "__main__":
    unittest.main()
