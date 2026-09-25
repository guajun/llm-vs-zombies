"""Scope-aware Stage-D comparison tests.

An OFF arm and an ON arm legitimately differ in their exact-store probe stream
and manifest; the scoped common comparison must pass on shared gameplay
evidence while the lifecycle comparator stays strict. A real state/RNG/action
mismatch and an ON/ON event mismatch must still fail.
"""
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))

from llm_vs_zombies import lifecycle_compare, lifecycle_events  # noqa: E402
from tests.test_lifecycle_report_proof import build_run  # noqa: E402


def make_off_run(source: Path, target: Path) -> Path:
    """Copy the ON child and turn it into a valid probe-off arm."""
    shutil.copytree(source, target)
    audit = target / "audit"
    manifest = json.loads((audit / "manifest.json").read_text(encoding="utf-8"))
    manifest["lifecycle_recording"]["session_id"] = 11
    manifest["lifecycle_probes"] = {"mode": lifecycle_events.PROBE_MODE, "enabled": False}
    manifest_bytes = (json.dumps(manifest, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    (audit / "manifest.json").write_bytes(manifest_bytes)
    (audit / lifecycle_events.EVENTS_FILE).write_bytes(b"")
    receipt = {
        "schema": lifecycle_events.RECEIPT_SCHEMA,
        "run_id": target.name, "branch_id": "branch-probe", "session_id": 11,
        "sequence_domain": lifecycle_events.SEQUENCE_DOMAIN,
        "envelope_schema": lifecycle_events.ENVELOPE_SCHEMA,
        "event_schema": lifecycle_events.EVENT_SCHEMA,
        "probe": {"name": lifecycle_events.PROBE_NAME, "schema": lifecycle_events.PROBE_SCHEMA},
        "build": {"module": "recorder.dll", "sha256": "b" * 64},
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "records": 0, "first_capture_sequence": None, "last_capture_sequence": None,
        "bytes": 0, "sha256": hashlib.sha256(b"").hexdigest(),
        "counters": {"captured": 0, "delivered": 0, "persisted": 0, "overflow": 0,
                     "wrong_thread": 0, "nesting_mismatch": 0, "incomplete_events": 0},
        "probe_health": {"captured": 0, "queued": 0, "wrong_thread_calls": 0, "faults": 0,
                         "overflow": 0, "active_initializers": 0, "healthy": True},
        "completed": True,
        "persistence": {"method": "flush_close_then_atomic_receipt",
                        "receipt_written_after_close": True},
    }
    (audit / lifecycle_events.RECEIPT_FILE).write_text(
        json.dumps(receipt, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
    return target


class ScopedCompareTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.base = Path(self._temp.name)
        self.on_run, _, _ = build_run(self.base / "on")
        self.off_run = make_off_run(self.on_run, self.base / "off" / self.on_run.name)
        self.on_b = self.base / "on-b" / self.on_run.name
        shutil.copytree(self.on_run, self.on_b)
        self.audit_on_b = self.on_b / "audit"

    def test_off_on_common_evidence_matches_while_streams_differ(self):
        scoped = lifecycle_compare.compare_scoped(self.off_run, self.on_run, scope="common")
        self.assertTrue(scoped["equal"], scoped)
        self.assertIn("lifecycle_probes manifest", scoped["normalized"])
        # The lifecycle semantic comparator is intentionally not used here.
        streams = lifecycle_compare.compare_scoped(self.off_run, self.on_run, scope="lifecycle")
        self.assertFalse(streams["equal"])

    def test_on_on_event_mismatch_fails_lifecycle_but_not_common(self):
        events_path = self.audit_on_b / lifecycle_events.EVENTS_FILE
        records = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
        for record in records:
            event = record.get("event") if isinstance(record.get("event"), dict) else None
            if event and event.get("kind") == "zombie_phase_transition":
                event["phase"]["before"] = 5
                break
        data = b"".join((json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8") for record in records)
        events_path.write_bytes(data)
        receipt_path = self.audit_on_b / lifecycle_events.RECEIPT_FILE
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["bytes"] = len(data)
        receipt["sha256"] = hashlib.sha256(data).hexdigest()
        receipt_path.write_text(json.dumps(receipt, separators=(",", ":"), sort_keys=True) + "\n",
                                encoding="utf-8")
        common = lifecycle_compare.compare_scoped(self.on_run, self.on_b, scope="common")
        self.assertTrue(common["equal"], common)
        streams = lifecycle_compare.compare_scoped(self.on_run, self.on_b, scope="lifecycle")
        self.assertFalse(streams["equal"])
        self.assertNotEqual(streams["lifecycle"].get("equal"), True)

    def test_real_particle_identity_mismatch_is_reported(self):
        from tests.test_audit_compare import particle_audit
        left = self.base / "p-left" / "audit"
        right = self.base / "p-right" / "audit"
        left.parent.mkdir(parents=True)
        right.parent.mkdir(parents=True)
        particle_audit(left)
        particle_audit(right, identity=131074)
        common = lifecycle_compare.compare_scoped(left, right, scope="common")
        self.assertFalse(common["equal"])
        self.assertEqual(common["common"]["reason"], "particle_shake")


if __name__ == "__main__":
    unittest.main()
