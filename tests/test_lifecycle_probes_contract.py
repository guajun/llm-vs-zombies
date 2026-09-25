"""Contract tests for the v2 exact-store probe records (issue #111).

The native probe stream lives in the same lifecycle-events.jsonl, discriminated
by schema. These tests cover the versioned contract and the counterexamples
required by the implementation authorization: repeated facts are kept, a
candidate without a commit fails, a commit without a candidate fails, missing
or disabled capabilities fail, and malformed payloads fail.
"""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from llm_vs_zombies import lifecycle_events  # noqa: E402


def probe_capability(*, enabled=True, session=1, build_sha="b" * 64):
    if not enabled:
        return {"mode": lifecycle_events.PROBE_MODE, "enabled": False}
    return {
        "mode": lifecycle_events.PROBE_MODE,
        "enabled": True,
        "record_schema": lifecycle_events.PROBE_EVENT_SCHEMA,
        "probe_set": ["zombie-phase-store", "zombie-removal-marked", "zombie-slot-recycle"],
        "session_id": session,
        "build": {"module": "recorder.dll", "sha256": build_sha},
        "sites": [{"id": "phase-mowdown", "va": 0x532A62, "window_bytes": 7, "bytes": [0xC7]}],
        "live_validated": False,
    }


def v1_capability(session=1, build_sha="b" * 64):
    return {
        "mode": lifecycle_events.MODE, "enabled": True,
        "event_schema": lifecycle_events.EVENT_SCHEMA,
        "envelope_schema": lifecycle_events.ENVELOPE_SCHEMA,
        "receipt_schema": lifecycle_events.RECEIPT_SCHEMA,
        "sequence_domain": lifecycle_events.SEQUENCE_DOMAIN,
        "session_id": session,
        "probe": {"name": lifecycle_events.PROBE_NAME, "schema": lifecycle_events.PROBE_SCHEMA,
                  "event_kind": lifecycle_events.KIND_INITIALIZATION},
        "build": {"module": "recorder.dll", "sha256": build_sha},
        "files": {"events": lifecycle_events.EVENTS_FILE, "close_receipt": lifecycle_events.RECEIPT_FILE},
        "live_validated": False,
    }


def probe_event(kind, sequence, *, entity=0x20001, wave=0, on_board=True, phase=None, recycle=None,
                removal=None, schema=None):
    event = {
        "schema": schema or lifecycle_events.PROBE_EVENT_SCHEMA,
        "kind": kind,
        "capture_sequence": sequence,
        "version": None,
        "version_phase": "uncontrolled_update",
        "engine_call_id": None,
        "entity": {"id": entity, "slot": entity & 0xFFFF, "generation": entity >> 16},
        "object": {"class": "zombie", "on_board": on_board, "wave": wave, "board": 0x123456},
        "probe": {"name": "zombie-lifecycle-store", "schema": lifecycle_events.PROBE_EVENT_SCHEMA,
                  "sequence_domain": lifecycle_events.SEQUENCE_DOMAIN},
        "complete": True,
    }
    if kind == "zombie_phase_transition":
        event["phase"] = phase or {"site": "phase-mowdown", "before": 0, "after": 3}
    elif kind == "zombie_removal_marked":
        event["removal"] = removal or {"source": "dienoloot_mdead_store", "before": 0, "after": 1}
    elif kind == "zombie_slot_recycle_candidate":
        event["recycle"] = recycle or {"state": "candidate", "slot": 0, "free_head_before": 0, "count_before": 1}
    else:
        event["recycle"] = recycle or {"state": "committed", "slot": 0, "candidate_capture_sequence": 1,
                                       "free_head_after": 0, "count_after": 0}
    return event


class ProbeContractTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.audit = Path(self._temp.name) / "audit"
        self.audit.mkdir(parents=True)

    def write(self, events, *, probes=..., v1=True, records=None, run_id="run-probe"):
        from test_lifecycle_events import envelope, envelopes_bytes, receipt_for
        manifest = {"schema": "lvz.audit.v1", "target": "probe-fixture", "loaded_signatures_match": True}
        if v1:
            manifest["lifecycle_recording"] = v1_capability()
        if probes is not ...:
            manifest["lifecycle_probes"] = probes
        manifest_bytes = (json.dumps(manifest, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        (self.audit / "manifest.json").write_bytes(manifest_bytes)
        items = [envelope(index, event, run_id=run_id, session=1) for index, event in enumerate(events)]
        data = envelopes_bytes(items)
        (self.audit / lifecycle_events.EVENTS_FILE).write_bytes(data)
        receipt = receipt_for(data, manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(), run_id=run_id, session=1)
        (self.audit / lifecycle_events.RECEIPT_FILE).write_text(
            json.dumps(receipt, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")

    def assertProblem(self, report, needle):
        self.assertTrue(any(needle in problem for problem in report["problems"]), report["problems"])

    def test_valid_probe_stream_with_paired_recycle(self):
        self.write([
            probe_event("zombie_phase_transition", 1),
            probe_event("zombie_removal_marked", 2),
            probe_event("zombie_slot_recycle_candidate", 3),
            probe_event("zombie_slot_recycle_commit", 4,
                        recycle={"state": "committed", "slot": 0, "candidate_capture_sequence": 3,
                                 "free_head_after": 0, "count_after": 0}),
        ], probes=probe_capability())
        report = lifecycle_events.validate(self.audit, require_close=True)
        self.assertEqual(report["status"], "valid", report["problems"])
        self.assertEqual(report["records"]["count"], 4)

    def test_repeated_phase_facts_are_not_deduplicated(self):
        self.write([
            probe_event("zombie_phase_transition", 1),
            probe_event("zombie_phase_transition", 2),
        ], probes=probe_capability())
        report = lifecycle_events.validate(self.audit, require_close=True)
        self.assertEqual(report["status"], "valid", report["problems"])
        self.assertEqual(report["records"]["kinds"], {"zombie_phase_transition": 2})

    def test_candidate_without_commit_fails(self):
        self.write([probe_event("zombie_slot_recycle_candidate", 1)], probes=probe_capability())
        report = lifecycle_events.validate(self.audit, require_close=True)
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "has no commit")

    def test_commit_without_candidate_fails(self):
        self.write([probe_event("zombie_slot_recycle_commit", 1)], probes=probe_capability())
        report = lifecycle_events.validate(self.audit, require_close=True)
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "references unknown candidate")

    def test_probe_records_without_capability_fail(self):
        self.write([probe_event("zombie_phase_transition", 1)], probes=...)
        report = lifecycle_events.validate(self.audit, require_close=True)
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "without an enabled lifecycle_probes capability")

    def test_disabled_probe_capability_with_records_fails(self):
        self.write([probe_event("zombie_phase_transition", 1)], probes=probe_capability(enabled=False))
        report = lifecycle_events.validate(self.audit, require_close=True)
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "capability is disabled")

    def test_malformed_probe_payloads_fail(self):
        cases = [
            ("raw phase type", probe_event("zombie_phase_transition", 1, phase={"site": "x", "before": "0", "after": 1})),
            ("missing removal", {**probe_event("zombie_removal_marked", 1), "removal": {"before": 0, "after": 1}}),
            ("on_board false", probe_event("zombie_phase_transition", 1, on_board=False)),
            ("unknown kind", probe_event("zombie_removed", 1)),
        ]
        for label, event in cases:
            with self.subTest(case=label):
                self.write([event], probes=probe_capability())
                report = lifecycle_events.validate(self.audit, require_close=True)
                self.assertEqual(report["status"], "failed", report["problems"])


if __name__ == "__main__":
    unittest.main()
