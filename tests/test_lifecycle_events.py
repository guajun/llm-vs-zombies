"""Offline tests for the #111 lifecycle recording contract and validator.

No game, no AvZ, no native runtime: every fixture below is a synthetic audit
directory built with the same JSON shapes the native recorder writes. The
native side proves the real write/close/receipt behavior in
``tests/determinism_lifecycle.cpp``; these tests prove the file contract and
the fail-closed reader, including old-trajectory ``unavailable`` semantics.
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

from llm_vs_zombies import audit_compare, evidence_codec, lifecycle_events  # noqa: E402
from llm_vs_zombies.audit_compare import EvidenceError  # noqa: E402
import issue111_lifecycle_check as checker  # noqa: E402

RUN_ID = "run-lifecycle-fixture"
BRANCH_ID = "branch-lifecycle-fixture"
SESSION = 3
BUILD_SHA = "b" * 64
DEFAULT_SEQUENCES = ((1, 1, 0, None), (3, 2, 0, None), (4, 3, 1, 2))


def capability(**overrides):
    value = {
        "mode": lifecycle_events.MODE,
        "enabled": True,
        "event_schema": lifecycle_events.EVENT_SCHEMA,
        "envelope_schema": lifecycle_events.ENVELOPE_SCHEMA,
        "receipt_schema": lifecycle_events.RECEIPT_SCHEMA,
        "sequence_domain": lifecycle_events.SEQUENCE_DOMAIN,
        "session_id": SESSION,
        "probe": {"name": lifecycle_events.PROBE_NAME, "schema": lifecycle_events.PROBE_SCHEMA,
                  "event_kind": lifecycle_events.KIND_INITIALIZATION},
        "build": {"module": "recorder.dll", "sha256": BUILD_SHA},
        "files": {"events": lifecycle_events.EVENTS_FILE, "close_receipt": lifecycle_events.RECEIPT_FILE},
        "live_validated": False,
    }
    value.update(overrides)
    return value


def event(sequence, invocation, depth=0, parent=None):
    return {
        "schema": lifecycle_events.EVENT_SCHEMA,
        "kind": lifecycle_events.KIND_INITIALIZATION,
        "capture_sequence": sequence,
        "version": None,
        "version_phase": "initialization",
        "engine_call_id": None,
        "invocation": {"invocation_id": invocation, "depth": depth, "parent_invocation_id": parent},
        "entity": {"id": (1 << 16) | 1, "slot": 1, "generation": 1},
        "before_after": {"before": None, "after": {"id": (1 << 16) | 1, "slot": 1, "generation": 1}},
        "classification": {"class": "initialization", "cause": "unknown"},
        "probe": {"name": lifecycle_events.PROBE_NAME, "schema": lifecycle_events.PROBE_SCHEMA,
                  "sequence_domain": lifecycle_events.SEQUENCE_DOMAIN},
        "complete": True,
    }


def envelope(index, item, *, session=SESSION, run_id=RUN_ID, branch=BRANCH_ID, **overrides):
    value = {"schema": lifecycle_events.ENVELOPE_SCHEMA, "file_seq": index, "run_id": run_id,
             "branch_id": branch, "session_id": session,
             "sequence_domain": lifecycle_events.SEQUENCE_DOMAIN, "event": item}
    value.update(overrides)
    return value


def envelopes_bytes(items):
    return b"".join((json.dumps(item, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
                    for item in items)


def receipt_for(events_bytes, *, session=SESSION, run_id=RUN_ID, branch=BRANCH_ID,
                manifest_sha256=None, records=None, sequences=None, **overrides):
    if records is None:
        records = [json.loads(line) for line in events_bytes.splitlines() if line.strip()]
    if sequences is None:
        sequences = [record["event"]["capture_sequence"] for record in records]
    value = {
        "schema": lifecycle_events.RECEIPT_SCHEMA,
        "run_id": run_id,
        "branch_id": branch,
        "session_id": session,
        "sequence_domain": lifecycle_events.SEQUENCE_DOMAIN,
        "envelope_schema": lifecycle_events.ENVELOPE_SCHEMA,
        "event_schema": lifecycle_events.EVENT_SCHEMA,
        "probe": {"name": lifecycle_events.PROBE_NAME, "schema": lifecycle_events.PROBE_SCHEMA},
        "build": {"module": "recorder.dll", "sha256": BUILD_SHA},
        "manifest_sha256": manifest_sha256 or hashlib.sha256(b"{}").hexdigest(),
        "records": len(records),
        "first_capture_sequence": sequences[0] if sequences else None,
        "last_capture_sequence": sequences[-1] if sequences else None,
        "bytes": len(events_bytes),
        "sha256": hashlib.sha256(events_bytes).hexdigest(),
        "counters": {"captured": len(records), "delivered": len(records), "persisted": len(records),
                     "overflow": 0, "wrong_thread": 0, "nesting_mismatch": 0, "incomplete_events": 0},
        "probe_health": {"captured": len(records), "queued": 0, "wrong_thread_calls": 0, "faults": 0,
                         "overflow": 0, "active_initializers": 0, "healthy": True},
        "completed": True,
        "persistence": {"method": "flush_close_then_atomic_receipt", "receipt_written_after_close": True},
    }
    value.update(overrides)
    return value


class Fixture:
    """One synthetic run directory: <root>/audit plus an optional run manifest."""

    def __init__(self, root, *, cap=..., sequences=DEFAULT_SEQUENCES, run_manifest=None, launcher=None):
        self.root = Path(root)
        self.audit = self.root / "audit"
        self.audit.mkdir(parents=True, exist_ok=True)
        self.cap = capability() if cap is ... else cap
        manifest = {"schema": "lvz.audit.v1", "target": "fixture", "loaded_signatures_match": True}
        if self.cap is not None:
            manifest["lifecycle_recording"] = self.cap
        self.manifest_bytes = (json.dumps(manifest, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        (self.audit / "manifest.json").write_bytes(self.manifest_bytes)
        self.items = [envelope(index, event(*sequence)) for index, sequence in enumerate(sequences)]
        self.events_bytes = envelopes_bytes(self.items)
        self.receipt = receipt_for(self.events_bytes, manifest_sha256=hashlib.sha256(self.manifest_bytes).hexdigest())
        self.write_events(self.events_bytes)
        self.write_receipt(self.receipt)
        if run_manifest is not None:
            (self.root / "manifest.json").write_text(json.dumps(run_manifest), encoding="utf-8")
        if launcher is not None:
            (self.root / "launcher.json").write_text(json.dumps(launcher), encoding="utf-8")

    def write_events(self, data: bytes):
        (self.audit / lifecycle_events.EVENTS_FILE).write_bytes(data)

    def write_receipt(self, value: dict):
        (self.audit / lifecycle_events.RECEIPT_FILE).write_text(
            json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")

    def refresh(self, items=None, **receipt_overrides):
        if items is not None:
            self.items = items
            self.events_bytes = envelopes_bytes(items)
        self.receipt = receipt_for(self.events_bytes,
                                   manifest_sha256=hashlib.sha256(self.manifest_bytes).hexdigest(),
                                   **receipt_overrides)
        self.write_events(self.events_bytes)
        self.write_receipt(self.receipt)

    def validate(self, **kwargs):
        return lifecycle_events.validate(self.audit, **kwargs)


class CapabilityTests(unittest.TestCase):
    def test_unavailable_without_declaration(self):
        self.assertEqual(lifecycle_events.mode({"schema": "lvz.audit.v1"}), "unavailable")

    def test_disabled_declaration(self):
        block = {"mode": lifecycle_events.MODE, "enabled": False}
        self.assertEqual(lifecycle_events.mode({"lifecycle_recording": block}), "disabled")

    def test_enabled_declaration(self):
        self.assertEqual(lifecycle_events.mode({"lifecycle_recording": capability()}), "enabled")

    def test_malformed_declarations_are_contract_errors(self):
        for block in ({"mode": "lvz.something-else.v1", "enabled": True},
                      {"mode": lifecycle_events.MODE, "enabled": "yes"},
                      {"mode": lifecycle_events.MODE, "enabled": True},
                      capability(sequence_domain="other-domain"),
                      capability(session_id=0),
                      capability(build={"module": "recorder.dll", "sha256": "xyz"})):
            with self.subTest(block=block):
                with self.assertRaises(lifecycle_events.LifecycleError):
                    lifecycle_events.mode({"lifecycle_recording": block})


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)

    def fixture(self, name="run", **kwargs):
        return Fixture(Path(self._temp.name) / name, **kwargs)

    def assertProblem(self, report, needle):
        self.assertTrue(any(needle in problem for problem in report["problems"]),
                        f"{needle!r} not in {report['problems']!r}")

    def test_valid_fixture_with_sequence_gap(self):
        report = self.fixture().validate()
        self.assertEqual(report["status"], "valid")
        self.assertEqual(report["records"]["count"], 3)
        self.assertEqual(report["records"]["gaps"], 1)
        self.assertEqual(report["records"]["first_capture_sequence"], 1)
        self.assertEqual(report["records"]["last_capture_sequence"], 4)
        self.assertTrue(report["close_receipt"]["present"])
        self.assertEqual(report["problems"], [])
        self.assertFalse(report["claims"]["first_kill_proven"])
        self.assertEqual(report["claims"]["first_kill_gate"], "unverified")
        self.assertIn("confirmed_death_stage", report["claims"]["unimplemented_fact_classes"])

    def test_empty_enabled_run_is_a_measured_zero(self):
        fixture = self.fixture()
        fixture.refresh(items=[])
        report = fixture.validate()
        self.assertEqual(report["status"], "valid")
        self.assertEqual(report["records"]["count"], 0)
        self.assertIsNone(report["records"]["first_capture_sequence"])
        self.assertEqual(report["close_receipt"]["records"], 0)

    def test_old_trajectory_is_unavailable_not_zero(self):
        fixture = self.fixture(cap=None)
        (fixture.audit / lifecycle_events.EVENTS_FILE).unlink()
        (fixture.audit / lifecycle_events.RECEIPT_FILE).unlink()
        report = fixture.validate()
        self.assertEqual(report["status"], "unavailable")
        self.assertIsNone(report["records"]["count"])
        self.assertFalse(report["capability"]["declared"])
        self.assertIn("unavailable", report["claims"]["note"])

    def test_unavailable_with_stray_evidence_fails(self):
        fixture = self.fixture(cap=None)
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "does not declare the capability")

    def test_disabled_capability_is_classified(self):
        fixture = self.fixture(cap={"mode": lifecycle_events.MODE, "enabled": False})
        (fixture.audit / lifecycle_events.EVENTS_FILE).unlink()
        (fixture.audit / lifecycle_events.RECEIPT_FILE).unlink()
        report = fixture.validate()
        self.assertEqual(report["status"], "disabled")

    def test_disabled_capability_with_evidence_fails(self):
        fixture = self.fixture(cap={"mode": lifecycle_events.MODE, "enabled": False})
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "disabled but lifecycle evidence exists")

    def test_missing_close_receipt_fails(self):
        fixture = self.fixture()
        (fixture.audit / lifecycle_events.RECEIPT_FILE).unlink()
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "close receipt is missing")

    def test_missing_events_file_fails(self):
        fixture = self.fixture()
        (fixture.audit / lifecycle_events.EVENTS_FILE).unlink()
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "no lifecycle-events.jsonl")

    def test_truncated_events_fail(self):
        fixture = self.fixture()
        data = fixture.events_bytes.splitlines(keepends=True)
        fixture.write_events(b"".join(data[:-1])[:-1])  # drop one record and the final newline
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "truncated")
        self.assertProblem(report, "byte count")

    def test_duplicate_sequence_fails(self):
        fixture = self.fixture()
        fixture.refresh(items=[envelope(0, event(1, 1)), envelope(1, event(3, 2)), envelope(2, event(3, 3))])
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "duplicated")

    def test_out_of_order_sequence_fails(self):
        fixture = self.fixture()
        fixture.refresh(items=[envelope(0, event(1, 1)), envelope(1, event(4, 2)), envelope(2, event(3, 3))])
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "out of order")

    def test_bad_parent_reference_fails(self):
        fixture = self.fixture()
        fixture.refresh(items=[envelope(0, event(1, 1)), envelope(1, event(3, 2, 1, 99))])
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "parent invocation 99 has no event")

    def test_wrong_session_identity_fails(self):
        fixture = self.fixture()
        broken = [envelope(0, event(1, 1)), envelope(1, event(3, 2), session=SESSION + 1)]
        fixture.refresh(items=broken)
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "session identity")

    def test_wrong_run_and_branch_identity_fails(self):
        fixture = self.fixture()
        broken = [envelope(0, event(1, 1), run_id="other-run", branch="other-branch")]
        fixture.refresh(items=broken, run_id="other-run", branch="other-branch")
        (fixture.audit.parent / "manifest.json").write_text(json.dumps(
            {"run_id": RUN_ID, "implementation": {"recorder_sha256": BUILD_SHA}}), encoding="utf-8")
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "run manifest run_id does not match")

    def test_receipt_count_mismatch_fails(self):
        fixture = self.fixture()
        fixture.receipt["records"] += 1
        fixture.write_receipt(fixture.receipt)
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "records=")

    def test_receipt_digest_mismatch_fails(self):
        fixture = self.fixture()
        fixture.receipt["sha256"] = "0" * 64
        fixture.write_receipt(fixture.receipt)
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "SHA-256 does not match")

    def test_receipt_manifest_digest_mismatch_fails(self):
        fixture = self.fixture()
        fixture.receipt["manifest_sha256"] = "1" * 64
        fixture.write_receipt(fixture.receipt)
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "manifest_sha256")

    def test_receipt_counter_mismatch_fails(self):
        fixture = self.fixture()
        fixture.receipt["counters"]["persisted"] = 2
        fixture.write_receipt(fixture.receipt)
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "persisted")

    def test_probe_fault_events_are_reported(self):
        fixture = self.fixture()
        (fixture.audit / "events.jsonl").write_text(
            json.dumps({"schema": "lvz.audit.v1", "seq": 0, "kind": "spawn_hook_fault", "payload": {}}) + "\n",
            encoding="utf-8")
        (fixture.audit / lifecycle_events.RECEIPT_FILE).unlink()
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["probe_events"]["scanned"])
        self.assertEqual(report["probe_events"]["fault_events"], 1)
        self.assertProblem(report, "close receipt is missing")

    def test_wrong_classification_fails(self):
        fixture = self.fixture()
        item = envelope(0, event(1, 1))
        item["event"]["classification"]["cause"] = "known"
        fixture.refresh(items=[item])
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "cause=unknown")

    def test_run_manifest_build_binding(self):
        fixture = self.fixture(run_manifest={"run_id": RUN_ID, "implementation": {"recorder_sha256": BUILD_SHA}})
        self.assertEqual(fixture.validate()["status"], "valid")
        fixture.root.joinpath("manifest.json").write_text(json.dumps(
            {"run_id": RUN_ID, "implementation": {"recorder_sha256": "c" * 64}}), encoding="utf-8")
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "recorder_sha256")

    def test_launcher_branch_binding(self):
        fixture = self.fixture(launcher={"branch_id": BRANCH_ID})
        self.assertEqual(fixture.validate()["status"], "valid")
        fixture.root.joinpath("launcher.json").write_text(json.dumps({"branch_id": "other"}), encoding="utf-8")
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "launcher branch identity")

    def test_recorder_binary_binding(self):
        fixture = self.fixture()
        recorder = Path(self._temp.name) / "recorder.dll"
        recorder.write_bytes(b"recorder-bytes")
        digest = hashlib.sha256(recorder.read_bytes()).hexdigest()
        fixture.cap["build"]["sha256"] = digest
        manifest = json.loads((fixture.audit / "manifest.json").read_text(encoding="utf-8"))
        manifest["lifecycle_recording"] = fixture.cap
        fixture.manifest_bytes = (json.dumps(manifest, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        (fixture.audit / "manifest.json").write_bytes(fixture.manifest_bytes)
        fixture.refresh(build={"module": "recorder.dll", "sha256": digest})
        self.assertEqual(fixture.validate(recorder=recorder)["status"], "valid")
        other = Path(self._temp.name) / "other.dll"
        other.write_bytes(b"other-bytes")
        report = fixture.validate(recorder=other)
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "recorder binary does not match")

    def test_compressed_archive_round_trip(self):
        fixture = self.fixture()
        evidence_codec.compress_evidence(fixture.audit)
        report = fixture.validate()
        self.assertEqual(report["status"], "valid")
        self.assertEqual(report["records"]["count"], 3)

    def test_audit_files_gates_on_the_declared_capability(self):
        fixture = self.fixture()
        manifest = json.loads((fixture.audit / "manifest.json").read_text(encoding="utf-8"))
        expected = audit_compare.audit_files(fixture.audit, manifest)
        self.assertIn(lifecycle_events.EVENTS_FILE, expected)
        self.assertIn(lifecycle_events.RECEIPT_FILE, expected)
        # A live tail sees the events stream before the receipt exists.
        (fixture.audit / lifecycle_events.RECEIPT_FILE).unlink()
        live = audit_compare.audit_files(fixture.audit, manifest)
        self.assertIn(lifecycle_events.EVENTS_FILE, live)
        self.assertNotIn(lifecycle_events.RECEIPT_FILE, live)
        (fixture.audit / lifecycle_events.EVENTS_FILE).unlink()
        with self.assertRaises(EvidenceError):
            audit_compare.audit_files(fixture.audit, manifest)

    def test_audit_files_refuses_undeclared_lifecycle_evidence(self):
        fixture = self.fixture(cap=None)
        manifest = json.loads((fixture.audit / "manifest.json").read_text(encoding="utf-8"))
        with self.assertRaises(EvidenceError):
            audit_compare.audit_files(fixture.audit, manifest)
        (fixture.audit / lifecycle_events.EVENTS_FILE).unlink()
        (fixture.audit / lifecycle_events.RECEIPT_FILE).unlink()
        self.assertEqual(audit_compare.audit_files(fixture.audit, manifest),
                         ("manifest.json", "events.jsonl", "checksums.jsonl", "state-deltas.jsonl"))

    def test_validate_reports_missing_manifest_as_contract_error(self):
        with self.assertRaises(lifecycle_events.LifecycleError):
            lifecycle_events.validate(Path(self._temp.name) / "absent")


class SchemaDocumentTests(unittest.TestCase):
    def test_schema_documents_match_the_native_record_shape(self):
        record_schema = json.loads((ROOT / "logger/schemas/lifecycle-record.schema.json").read_text(encoding="utf-8"))
        receipt_schema = json.loads((ROOT / "logger/schemas/lifecycle-close-receipt.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(record_schema["properties"]["schema"]["const"], lifecycle_events.ENVELOPE_SCHEMA)
        self.assertEqual(record_schema["$defs"]["event"]["properties"]["schema"]["const"], lifecycle_events.EVENT_SCHEMA)
        self.assertEqual(receipt_schema["properties"]["schema"]["const"], lifecycle_events.RECEIPT_SCHEMA)
        self.assertFalse(record_schema["additionalProperties"])
        self.assertFalse(receipt_schema["additionalProperties"])
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp) / "run")
            envelope = fixture.items[0]
            self.assertEqual(set(envelope), set(record_schema["properties"]))
            self.assertEqual(set(record_schema["required"]), set(record_schema["properties"]))
            event = envelope["event"]
            event_schema = record_schema["$defs"]["event"]
            self.assertEqual(set(event), set(event_schema["properties"]))
            self.assertEqual(set(event_schema["required"]), set(event_schema["properties"]))
            self.assertEqual(set(fixture.receipt), set(receipt_schema["properties"]))
            self.assertEqual(set(receipt_schema["required"]), set(receipt_schema["properties"]))


class CliTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)

    def run_tool(self, path):
        return subprocess.run([sys.executable, str(ROOT / "tools" / "issue111_lifecycle_check.py"),
                               str(path), "--json"], capture_output=True, text=True, timeout=120)

    def test_cli_valid_and_failed_exit_codes(self):
        fixture = Fixture(Path(self._temp.name) / "run")
        result = self.run_tool(fixture.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "valid")
        (fixture.audit / lifecycle_events.RECEIPT_FILE).unlink()
        result = self.run_tool(fixture.root)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["status"], "failed")

    def test_cli_missing_path_is_contract_error(self):
        result = self.run_tool(Path(self._temp.name) / "absent")
        self.assertEqual(result.returncode, 2)

    def test_resolve_audit_accepts_run_and_audit_directories(self):
        fixture = Fixture(Path(self._temp.name) / "run")
        self.assertEqual(checker.resolve_audit(fixture.root), fixture.audit)
        self.assertEqual(checker.resolve_audit(fixture.audit), fixture.audit)


if __name__ == "__main__":
    unittest.main()
