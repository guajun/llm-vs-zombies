"""Offline tests for semantic lifecycle comparison with identity normalization.

Two synthetic cold runs carry distinct real identities (run_id, branch_id,
session_id); only those identity fields are normalized, while every recorded
fact, counter and sequence value stays in the comparison.
"""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from llm_vs_zombies import audit_compare, lifecycle_compare, lifecycle_events  # noqa: E402


def cold_run(directory: Path, *, run_id: str, branch_id: str, session: int):
    """One strict, lifecycle-enabled synthetic cold run."""
    from tests.test_audit_compare import animation_audit
    from tests.test_lifecycle_events import capability, envelope, event, envelopes_bytes, receipt_for
    directory.parent.mkdir(parents=True, exist_ok=True)
    animation_audit(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    manifest["lifecycle_recording"] = capability(session_id=session)
    manifest_bytes = (json.dumps(manifest, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    (directory / "manifest.json").write_bytes(manifest_bytes)
    items = [
        envelope(0, event(1, 2, 1, 1), run_id=run_id, branch=branch_id, session=session),
        envelope(1, event(3, 1, 0, None), run_id=run_id, branch=branch_id, session=session),
        envelope(2, event(4, 3, 0, None), run_id=run_id, branch=branch_id, session=session),
    ]
    events_bytes = envelopes_bytes(items)
    (directory / lifecycle_events.EVENTS_FILE).write_bytes(events_bytes)
    receipt = receipt_for(events_bytes, manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                          run_id=run_id, branch=branch_id, session=session)
    (directory / lifecycle_events.RECEIPT_FILE).write_text(
        json.dumps(receipt, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
    return manifest_bytes


class LifecycleCompareTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.left = Path(self._temp.name) / "left" / "audit"
        self.right = Path(self._temp.name) / "right" / "audit"
        cold_run(self.left, run_id="run-left", branch_id="branch-left", session=1)
        cold_run(self.right, run_id="run-right", branch_id="branch-right", session=7)

    def test_distinct_identities_compare_equal_on_facts(self):
        report = lifecycle_compare.compare_lifecycle(self.left, self.right)
        self.assertTrue(report["equal"], report)
        self.assertEqual(report["normalized"], ["run_id", "branch_id", "session_id"])
        self.assertEqual(report["records"], 3)
        self.assertEqual(report["counters"]["persisted"], 3)

    def test_strict_boundary_comparison_ignores_only_lifecycle_session_identity(self):
        left = audit_compare.AuditLog(self.left, require_closed=True)
        right = audit_compare.AuditLog(self.right, require_closed=True)
        compared = audit_compare.compare_audits(left, right)
        self.assertTrue(compared["equal"], compared)

    def test_fact_difference_is_reported_semantically(self):
        from tests.test_lifecycle_events import envelope, envelopes_bytes, receipt_for
        items = [json.loads(line) for line in (self.right / lifecycle_events.EVENTS_FILE).read_bytes().splitlines()]
        items[2]["event"]["capture_sequence"] = 5
        events_bytes = envelopes_bytes(items)
        manifest_bytes = (self.right / "manifest.json").read_bytes()
        (self.right / lifecycle_events.EVENTS_FILE).write_bytes(events_bytes)
        receipt = receipt_for(events_bytes, manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                              run_id="run-right", branch="branch-right", session=7)
        (self.right / lifecycle_events.RECEIPT_FILE).write_text(
            json.dumps(receipt, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
        report = lifecycle_compare.compare_lifecycle(self.left, self.right)
        self.assertFalse(report["equal"])
        self.assertEqual(report["reason"], "record")
        self.assertEqual(report["index"], 2)
        self.assertIn("capture_sequence", json.dumps(report["difference"]))

    def test_record_count_difference_is_reported(self):
        from tests.test_lifecycle_events import envelope, event, envelopes_bytes, receipt_for
        items = [json.loads(line) for line in (self.right / lifecycle_events.EVENTS_FILE).read_bytes().splitlines()]
        items = items[:2]
        events_bytes = envelopes_bytes(items)
        manifest_bytes = (self.right / "manifest.json").read_bytes()
        (self.right / lifecycle_events.EVENTS_FILE).write_bytes(events_bytes)
        receipt = receipt_for(events_bytes, manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                              run_id="run-right", branch="branch-right", session=7)
        (self.right / lifecycle_events.RECEIPT_FILE).write_text(
            json.dumps(receipt, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
        report = lifecycle_compare.compare_lifecycle(self.left, self.right)
        self.assertFalse(report["equal"])
        self.assertEqual(report["reason"], "record_count")

    def test_normalize_removes_only_identity_fields(self):
        record = {"run_id": "r", "branch_id": "b", "session_id": 1,
                  "file_seq": 0, "capture_sequence_probe": {"counters": {"captured": 2}}}
        normalized = lifecycle_compare.normalize(record)
        self.assertNotIn("run_id", normalized)
        self.assertNotIn("branch_id", normalized)
        self.assertNotIn("session_id", normalized)
        self.assertEqual(normalized["file_seq"], 0)
        self.assertEqual(normalized["capture_sequence_probe"]["counters"]["captured"], 2)

    def test_normalize_replaces_only_the_local_board_pointer(self):
        left = {"event": {"object": {"board": 0x1111, "wave": 0, "on_board": True},
                          "entity": {"id": 0x00020001, "slot": 1}}}
        right = {"event": {"object": {"board": 0x2222, "wave": 0, "on_board": True},
                           "entity": {"id": 0x00020001, "slot": 1}}}
        self.assertEqual(lifecycle_compare.normalize(left), lifecycle_compare.normalize(right))
        self.assertEqual(lifecycle_compare.normalize(left)["event"]["object"]["board"], "<board-scope>")
        self.assertEqual(lifecycle_compare.normalize(left)["event"]["entity"]["id"], 0x00020001)

    def test_missing_receipt_is_a_compare_error(self):
        (self.right / lifecycle_events.RECEIPT_FILE).unlink()
        with self.assertRaises(lifecycle_compare.CompareError):
            lifecycle_compare.compare_lifecycle(self.left, self.right)

    def test_cli_exit_codes(self):
        equal = subprocess.run([sys.executable, str(ROOT / "tools" / "issue111_lifecycle_compare.py"),
                                str(self.left), str(self.right)], capture_output=True, text=True, timeout=300)
        self.assertEqual(equal.returncode, 0, equal.stderr)
        self.assertTrue(json.loads(equal.stdout)["equal"])
        (self.right / lifecycle_events.EVENTS_FILE).write_bytes(b"")
        different = subprocess.run([sys.executable, str(ROOT / "tools" / "issue111_lifecycle_compare.py"),
                                    str(self.left), str(self.right)], capture_output=True, text=True, timeout=300)
        self.assertEqual(different.returncode, 2)


if __name__ == "__main__":
    unittest.main()
