"""Contract tests for the v2 exact-store probe records (issue #111).

The native probe stream lives in the same lifecycle-events.jsonl, discriminated
by schema. These tests cover the versioned contract, the fail-closed
counterexamples and a real artifact produced by the native recorder fixture
when the build is present.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from llm_vs_zombies import lifecycle_events  # noqa: E402

SITES = [
    {"id": "phase-playdeathanim", "va": 0x533377, "window_bytes": 7,
     "bytes": [0xc7, 0x47, 0x28, 1, 0, 0, 0], "continuation_va": 0x53337e},
    {"id": "phase-applyburn", "va": 0x532f5f, "window_bytes": 7,
     "bytes": [0xc7, 0x46, 0x28, 2, 0, 0, 0], "continuation_va": 0x532f66},
    {"id": "phase-mowdown", "va": 0x532a62, "window_bytes": 7,
     "bytes": [0xc7, 0x47, 0x28, 3, 0, 0, 0], "continuation_va": 0x532a69},
    {"id": "phase-catapult", "va": 0x52ec92, "window_bytes": 7,
     "bytes": [0xc7, 0x47, 0x28, 1, 0, 0, 0], "continuation_va": 0x52ec99},
    {"id": "phase-zamboni", "va": 0x52ea38, "window_bytes": 6,
     "bytes": [0x89, 0x5f, 0x28, 0xd9, 0x47, 0x2c], "continuation_va": 0x52ea3e},
    {"id": "removal-mdead", "va": 0x530602, "window_bytes": 7,
     "bytes": [0xc6, 0x87, 0xec, 0, 0, 0, 1], "continuation_va": 0x530609},
    {"id": "recycle-guard", "va": 0x41bba9, "window_bytes": 6,
     "bytes": [0x38, 0x9f, 0xec, 0, 0, 0], "continuation_va": 0x41bbaf},
    {"id": "recycle-commit", "va": 0x41bc23, "window_bytes": 5,
     "bytes": [0xe9, 0x30, 0xff, 0xff, 0xff], "continuation_va": 0x41bb58},
]
SHA = "b" * 64
SESSION = 7


def probe_counters(persisted=None):
    counters = {"captured": 0, "queued": 0, "delivered": 0, "overflow": 0, "wrong_thread": 0,
                "inactive_suppressed": 0, "classify_refused": 0, "read_failed": 0, "live_skips": 0,
                "unmatched_commits": 0, "pair_mismatch": 0, "overwritten_pending": 0, "faults": 0}
    if persisted is not None:
        counters["persisted"] = persisted
    return counters


def v1_event(sequence=1):
    return {
        "schema": "lvz.lifecycle-event.v1", "kind": "zombie_initialized", "capture_sequence": sequence,
        "version": None, "version_phase": "initialization", "engine_call_id": None,
        "invocation": {"invocation_id": 1, "depth": 0, "parent_invocation_id": None},
        "entity": {"id": 0x00020001, "slot": 1, "generation": 2},
        "before_after": {"before": None, "after": {"id": 0x00020001, "slot": 1, "generation": 2,
                                                   "row0": 0, "type": 16, "game_clock": 42}},
        "classification": {"class": "initialization", "cause": "unknown"},
        "probe": {"name": "zombie-initialize-exit", "schema": "lvz.spawn.v1",
                  "sequence_domain": lifecycle_events.SEQUENCE_DOMAIN},
        "complete": True,
    }


def probe_event(kind, sequence, *, entity=0x00020001, wave=0, on_board=True, site=None, phase=None,
                recycle=None):
    event = {
        "schema": lifecycle_events.PROBE_EVENT_SCHEMA, "kind": kind, "capture_sequence": sequence,
        "version": None, "version_phase": "uncontrolled_update", "engine_call_id": None,
        "entity": {"id": entity, "slot": entity & 0xFFFF, "generation": entity >> 16},
        "object": {"class": "zombie", "on_board": on_board, "wave": wave, "board": 0x123456},
        "probe": {"name": "zombie-lifecycle-store", "schema": lifecycle_events.PROBE_EVENT_SCHEMA,
                  "sequence_domain": lifecycle_events.SEQUENCE_DOMAIN},
        "complete": True,
    }
    if kind == "zombie_phase_transition":
        event["phase"] = phase or {"site": site or "phase-mowdown", "before": 0, "after": 3}
    elif kind == "zombie_removal_marked":
        event["removal"] = {"source": "dienoloot_mdead_store", "before": 0, "after": 1,
                            "frame": {"return_into_diewithloot": None, "return_into_applyburn": None,
                                      "callsite_bytes_match": False}}
    elif kind == "zombie_slot_recycle_candidate":
        event["recycle"] = recycle or {"state": "candidate", "slot": entity & 0xFFFF,
                                       "free_head_before": 0, "count_before": 2}
    else:
        event["recycle"] = recycle or {"state": "committed", "slot": entity & 0xFFFF,
                                       "candidate_capture_sequence": 3, "free_head_after": entity & 0xFFFF,
                                       "count_after": 1}
    return event


def envelope(index, event):
    return {"schema": lifecycle_events.ENVELOPE_SCHEMA, "file_seq": index, "run_id": "run-probe",
            "branch_id": "branch-probe", "session_id": SESSION,
            "sequence_domain": lifecycle_events.SEQUENCE_DOMAIN, "event": event}


class ProbeContractTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.audit = Path(self._temp.name) / "audit"
        self.audit.mkdir(parents=True)

    def write(self, events, *, probes=True, v2=True, manifest_extra=None, receipt_v2=True,
              capability_probe_set=None, receipt=None, events_extra=None, live=False):
        v1_count = sum(1 for event in events if event.get("schema") == lifecycle_events.EVENT_SCHEMA)
        v2_count = len(events) - v1_count
        manifest = {"schema": "lvz.audit.v1", "target": "probe-fixture"}
        manifest["lifecycle_recording"] = {
            "mode": lifecycle_events.MODE, "enabled": True,
            "event_schema": lifecycle_events.EVENT_SCHEMA,
            "envelope_schema": lifecycle_events.ENVELOPE_SCHEMA,
            "receipt_schema": lifecycle_events.RECEIPT_SCHEMA,
            "sequence_domain": lifecycle_events.SEQUENCE_DOMAIN, "session_id": SESSION,
            "probe": {"name": lifecycle_events.PROBE_NAME, "schema": lifecycle_events.PROBE_SCHEMA,
                      "event_kind": lifecycle_events.KIND_INITIALIZATION},
            "build": {"module": "recorder.dll", "sha256": SHA},
            "files": {"events": lifecycle_events.EVENTS_FILE, "close_receipt": lifecycle_events.RECEIPT_FILE},
            "live_validated": False,
        }
        probe_counters_final = probe_counters()
        probe_counters_final["captured"] = v2_count
        probe_counters_final["delivered"] = v2_count
        manifest["lifecycle_probes"] = {
            "mode": lifecycle_events.PROBE_MODE, "enabled": probes,
            "record_schema": lifecycle_events.PROBE_EVENT_SCHEMA,
            "event_schemas": [lifecycle_events.PROBE_EVENT_SCHEMA],
            "probe_set": capability_probe_set or list(lifecycle_events.PROBE_SET),
            "session_id": SESSION, "build": {"module": "recorder.dll", "sha256": SHA},
            "sites": SITES, "patch_windows_evidence": "docs/issue111-patch-windows.json",
            "probe_counters": probe_counters_final, "healthy": True, "pending_candidate": False,
            "active": False, "installed": False, "live_validated": False,
        }
        if manifest_extra:
            manifest.update(manifest_extra)
        manifest_bytes = (json.dumps(manifest, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        (self.audit / "manifest.json").write_bytes(manifest_bytes)
        data = b"".join((json.dumps(envelope(index, event), separators=(",", ":")) + "\n").encode("utf-8")
                        for index, event in enumerate(events))
        (self.audit / lifecycle_events.EVENTS_FILE).write_bytes(data)
        if events_extra is None and receipt_v2 and not live:
            closed_counters = dict(probe_counters_final, persisted=v2_count, captured=v2_count,
                                   delivered=v2_count)
            events_extra = [{"kind": "lifecycle_probes_closed",
                             "payload": {"installed": False, "active": False, "healthy": True,
                                         "pending_candidate": False, "patched_sites": [],
                                         "counters": closed_counters, "sites": SITES,
                                         "reader_protected": False, "pending_callbacks": 0}}]
        if events_extra is not None:
            lines = [json.dumps(value, separators=(",", ":")) for value in events_extra]
            (self.audit / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        if receipt is None and receipt_v2:
            with_persisted = dict(probe_counters_final, persisted=v2_count)
            health = {"installed": False, "active": False, "healthy": True, "pending_candidate": False,
                      "patched_sites": [], "counters": with_persisted, "sites": SITES,
                      "reader_protected": False, "pending_callbacks": 0}
            receipt = {
                "schema": lifecycle_events.PROBE_RECEIPT_SCHEMA, "run_id": "run-probe",
                "branch_id": "branch-probe", "session_id": SESSION,
                "sequence_domain": lifecycle_events.SEQUENCE_DOMAIN,
                "envelope_schema": lifecycle_events.ENVELOPE_SCHEMA,
                "event_schemas": [lifecycle_events.EVENT_SCHEMA, lifecycle_events.PROBE_EVENT_SCHEMA],
                "probe": {"name": lifecycle_events.PROBE_NAME, "schema": lifecycle_events.PROBE_SCHEMA},
                "build": {"module": "recorder.dll", "sha256": SHA},
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "records": len(events), "first_capture_sequence": events[0]["capture_sequence"],
                "last_capture_sequence": events[-1]["capture_sequence"], "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "counters": {
                    "initialization": {"captured": max(v1_count, 1), "delivered": max(v1_count, 1),
                                       "persisted": v1_count, "overflow": 0, "wrong_thread": 0,
                                       "nesting_mismatch": 0, "incomplete_events": 0},
                    "probes": with_persisted,
                    "total": {"persisted": len(events), "records": len(events), "bytes": len(data)},
                },
                "probe_health": health, "completed": True,
                "persistence": {"method": "flush_close_then_atomic_receipt",
                                "receipt_written_after_close": True},
            }
        if receipt is not None and not live:
            (self.audit / lifecycle_events.RECEIPT_FILE).write_text(
                json.dumps(receipt, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")

    def problem(self, report, needle):
        self.assertTrue(any(needle in problem for problem in report["problems"]), report["problems"])

    def valid_events(self):
        return [
            v1_event(1),
            probe_event("zombie_phase_transition", 2, site="phase-mowdown"),
            probe_event("zombie_removal_marked", 3),
            probe_event("zombie_slot_recycle_candidate", 4),
            probe_event("zombie_slot_recycle_commit", 5,
                        recycle={"state": "committed", "slot": 1, "candidate_capture_sequence": 4,
                                 "free_head_after": 1, "count_after": 1}),
        ]

    def test_valid_mixed_stream(self):
        self.write(self.valid_events(),
                   events_extra=[{"kind": "lifecycle_probes_closed", "payload": {
                       "installed": False, "active": False, "healthy": True, "pending_candidate": False,
                       "patched_sites": [], "counters": dict(probe_counters(), captured=4, delivered=4),
                       "sites": SITES, "reader_protected": False, "pending_callbacks": 0}}])
        report = lifecycle_events.validate(self.audit, require_close=True)
        self.assertEqual(report["status"], "valid", report["problems"])

    def test_native_fixture_artifact(self):
        executable = ROOT / "build" / "determinism_lifecycle_probes.exe"
        if not executable.is_file():
            self.skipTest("native probe fixture is not built")
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "artifact"
            result = subprocess.run([str(executable), "--emit-audit", str(target)], capture_output=True,
                                    text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = lifecycle_events.validate(target, require_close=True)
            self.assertEqual(report["status"], "valid", report["problems"])
            self.assertGreater(report["records"]["count"], 2)

    def test_repeated_phase_facts_are_not_deduplicated(self):
        events = [v1_event(1), probe_event("zombie_phase_transition", 2, site="phase-mowdown"),
                  probe_event("zombie_phase_transition", 3, site="phase-mowdown")]
        self.write(events)
        report = lifecycle_events.validate(self.audit, require_close=True)
        self.assertEqual(report["status"], "valid", report["problems"])
        self.assertEqual(report["records"]["kinds"]["zombie_phase_transition"], 2)

    def test_wrong_entity_slot_or_board_pair_fails(self):
        for label, commit in [
            ("entity", {"state": "committed", "slot": 1, "candidate_capture_sequence": 4,
                        "free_head_after": 1, "count_after": 1}),
            ("slot", {"state": "committed", "slot": 0, "candidate_capture_sequence": 4,
                      "free_head_after": 0, "count_after": 1}),
        ]:
            with self.subTest(case=label):
                events = self.valid_events()
                events[4] = probe_event("zombie_slot_recycle_commit", 5,
                                        entity=0x00030001 if label == "entity" else 0x00020001,
                                        recycle=commit)
                self.write(events)
                report = lifecycle_events.validate(self.audit, require_close=True)
                self.assertEqual(report["status"], "failed")
                self.problem(report, "does not match its candidate")

    def test_duplicate_commit_and_missing_commit_fail(self):
        events = [v1_event(1), probe_event("zombie_slot_recycle_candidate", 2),
                  probe_event("zombie_slot_recycle_commit", 3,
                              recycle={"state": "committed", "slot": 1, "candidate_capture_sequence": 2,
                                       "free_head_after": 1, "count_after": 1}),
                  probe_event("zombie_slot_recycle_commit", 4,
                              recycle={"state": "committed", "slot": 1, "candidate_capture_sequence": 2,
                                       "free_head_after": 1, "count_after": 1})]
        self.write(events)
        report = lifecycle_events.validate(self.audit, require_close=True)
        self.assertEqual(report["status"], "failed")
        self.problem(report, "reuses candidate")
        self.write([v1_event(1), probe_event("zombie_slot_recycle_candidate", 2)])
        report = lifecycle_events.validate(self.audit, require_close=True)
        self.assertEqual(report["status"], "failed")
        self.problem(report, "has no commit")

    def test_unknown_site_and_changed_capability_fail(self):
        self.write([v1_event(1), probe_event("zombie_phase_transition", 2, site="phase-dienoloot")])
        report = lifecycle_events.validate(self.audit, require_close=True)
        self.assertEqual(report["status"], "failed")
        self.problem(report, "not a frozen phase store")
        self.write(self.valid_events(), capability_probe_set=["zombie-phase-store"])
        report = lifecycle_events.validate(self.audit, require_close=True)
        self.assertEqual(report["status"], "failed")
        self.problem(report, "probe_set")

    def test_unhashable_sequence_does_not_crash(self):
        events = [v1_event(1)]
        candidate = probe_event("zombie_slot_recycle_candidate", 2)
        candidate["capture_sequence"] = ["not", "an", "int"]
        events.append(candidate)
        self.write(events)
        report = lifecycle_events.validate(self.audit, require_close=True)
        self.assertEqual(report["status"], "failed")
        self.assertFalse(any("Traceback" in problem for problem in report["problems"]))

    def test_wrong_phase_value_fails(self):
        self.write([v1_event(1), probe_event("zombie_phase_transition", 2,
                                             phase={"site": "phase-mowdown", "before": 0, "after": 99})])
        report = lifecycle_events.validate(self.audit, require_close=True)
        self.assertEqual(report["status"], "failed")
        self.problem(report, "does not match the locked store")

    def test_generation_zero_and_bad_probe_name_fail(self):
        broken = probe_event("zombie_phase_transition", 2, entity=0x00000000, site="phase-mowdown")
        self.write([v1_event(1), broken])
        report = lifecycle_events.validate(self.audit, require_close=True)
        self.assertEqual(report["status"], "failed")
        self.problem(report, "positive integer")
        bad_probe = probe_event("zombie_phase_transition", 2, site="phase-mowdown")
        bad_probe["probe"]["name"] = "other"
        self.write([v1_event(1), bad_probe])
        report = lifecycle_events.validate(self.audit, require_close=True)
        self.assertEqual(report["status"], "failed")
        self.problem(report, "probe declaration")

    def test_probe_records_without_capability_fail(self):
        events = self.valid_events()
        self.write(events)
        manifest = json.loads((self.audit / "manifest.json").read_bytes())
        manifest.pop("lifecycle_probes")
        (self.audit / "manifest.json").write_bytes(
            (json.dumps(manifest, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8"))
        report = lifecycle_events.validate(self.audit, require_close=True)
        self.assertEqual(report["status"], "failed")
        self.problem(report, "without an enabled lifecycle_probes capability")

    def test_lost_observation_counters_fail_the_receipt(self):
        for counter in ("read_failed", "classify_refused", "inactive_suppressed"):
            with self.subTest(counter=counter):
                self.write(self.valid_events())
                receipt = json.loads((self.audit / lifecycle_events.RECEIPT_FILE).read_text(encoding="utf-8"))
                receipt["counters"]["probes"][counter] = 1
                receipt["probe_health"]["counters"][counter] = 1
                (self.audit / lifecycle_events.RECEIPT_FILE).write_text(
                    json.dumps(receipt, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
                report = lifecycle_events.validate(self.audit, require_close=True)
                self.assertEqual(report["status"], "failed")
                self.problem(report, counter)

    def test_removal_frame_contract(self):
        events = self.valid_events()
        events[2]["removal"]["frame"] = {"return_into_diewithloot": 0x5302FF,
                                         "return_into_applyburn": 0x532FC7,
                                         "callsite_bytes_match": True}
        self.write(events)
        report = lifecycle_events.validate(self.audit, require_close=True)
        self.assertEqual(report["status"], "valid", report["problems"])
        events = self.valid_events()
        events[2]["removal"]["frame"] = {"return_into_diewithloot": "no", "return_into_applyburn": None,
                                          "callsite_bytes_match": "yes"}
        self.write(events)
        report = lifecycle_events.validate(self.audit, require_close=True)
        self.assertEqual(report["status"], "failed")
        self.problem(report, "frame")

    def test_offboard_fact_is_allowed(self):
        events = [v1_event(1), probe_event("zombie_phase_transition", 2, site="phase-mowdown",
                                           wave=-2, on_board=False)]
        self.write(events)
        report = lifecycle_events.validate(self.audit, require_close=True)
        self.assertEqual(report["status"], "valid", report["problems"])

    def test_live_prefix_allows_open_candidate(self):
        self.write([v1_event(1), probe_event("zombie_slot_recycle_candidate", 2)], live=True)
        report = lifecycle_events.validate(self.audit, require_close=False)
        self.assertEqual(report["status"], "open", report["problems"])


if __name__ == "__main__":
    unittest.main()
