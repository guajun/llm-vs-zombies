"""Offline tests for the #111 lifecycle recording contract and validator.

No game, no AvZ, no native runtime: every fixture below is a synthetic audit
directory built with the same JSON shapes the native recorder writes. The
native side proves the real write/close/receipt behavior in
``tests/determinism_lifecycle.cpp``; these tests prove the file contract and
the fail-closed reader, including old-trajectory ``unavailable`` semantics,
malformed-input robustness, LIFO invocation nesting and probe faults.
"""
import copy
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
ENTITY_ID = (1 << 16) | 1
# Valid LIFO nesting: invocation 1 enters, invocation 2 nests, 2 exits first
# (capture 1), 1 exits next (capture 3), then root 3 exits (capture 4). The
# gap between 1 and 3 is legitimate.
DEFAULT_SEQUENCES = ((1, 2, 1, 1), (3, 1, 0, None), (4, 3, 0, None))


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
        "entity": {"id": ENTITY_ID, "slot": 1, "generation": 1},
        "before_after": {"before": None,
                         "after": {"id": ENTITY_ID, "slot": 1, "generation": 1,
                                   "row0": 0, "type": 16, "game_clock": 42}},
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


def _record_sequence(record):
    if not isinstance(record, dict):
        return None
    value = record.get("event")
    if not isinstance(value, dict):
        return None
    sequence = value.get("capture_sequence")
    return sequence if type(sequence) is int else None


def receipt_for(events_bytes, *, manifest_sha256=None, records=None, session=SESSION,
                run_id=RUN_ID, branch=BRANCH_ID, build=None, **overrides):
    if records is None:
        records = [json.loads(line) for line in events_bytes.splitlines() if line.strip()]
    sequences = [sequence for sequence in (_record_sequence(record) for record in records)
                 if sequence is not None]
    value = {
        "schema": lifecycle_events.RECEIPT_SCHEMA,
        "run_id": run_id,
        "branch_id": branch,
        "session_id": session,
        "sequence_domain": lifecycle_events.SEQUENCE_DOMAIN,
        "envelope_schema": lifecycle_events.ENVELOPE_SCHEMA,
        "event_schema": lifecycle_events.EVENT_SCHEMA,
        "probe": {"name": lifecycle_events.PROBE_NAME, "schema": lifecycle_events.PROBE_SCHEMA},
        "build": build if build is not None else {"module": "recorder.dll", "sha256": BUILD_SHA},
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

    def __init__(self, root, *, cap=..., sequences=DEFAULT_SEQUENCES, run_manifest=None, launcher=None,
                 write_receipt=True, run_id=RUN_ID, branch_id=BRANCH_ID):
        self.root = Path(root)
        self.run_id = run_id
        self.branch_id = branch_id
        self.audit = self.root / "audit"
        self.audit.mkdir(parents=True, exist_ok=True)
        self.cap = capability() if cap is ... else cap
        manifest = {"schema": "lvz.audit.v1", "target": "fixture", "loaded_signatures_match": True}
        if self.cap is not None:
            manifest["lifecycle_recording"] = self.cap
        self.manifest_bytes = (json.dumps(manifest, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        (self.audit / "manifest.json").write_bytes(self.manifest_bytes)
        self.items = [envelope(index, event(*sequence), run_id=self.run_id, branch=self.branch_id)
                      for index, sequence in enumerate(sequences)]
        self.events_bytes = envelopes_bytes(self.items)
        self.receipt = self.receipt_for(self.events_bytes)
        self.write_events(self.events_bytes)
        if write_receipt:
            self.write_receipt(self.receipt)

    def receipt_for(self, events_bytes, **overrides):
        build = None
        if isinstance(self.cap, dict) and isinstance(self.cap.get("build"), dict):
            build = self.cap["build"]
        overrides.setdefault("run_id", self.run_id)
        overrides.setdefault("branch", self.branch_id)
        return receipt_for(events_bytes, manifest_sha256=hashlib.sha256(self.manifest_bytes).hexdigest(),
                           build=build, **overrides)

    def write_events(self, data: bytes):
        (self.audit / lifecycle_events.EVENTS_FILE).write_bytes(data)

    def write_receipt(self, value: dict):
        (self.audit / lifecycle_events.RECEIPT_FILE).write_text(
            json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")

    def refresh(self, items=None, **receipt_overrides):
        if items is not None:
            self.items = items
            self.events_bytes = envelopes_bytes(items)
        self.receipt = self.receipt_for(self.events_bytes, **receipt_overrides)
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

    def test_declared_but_malformed_is_not_unavailable(self):
        for block in (None, [], "enabled", 1,
                      {"mode": "lvz.something-else.v1", "enabled": True},
                      {"mode": lifecycle_events.MODE, "enabled": "yes"},
                      {"mode": lifecycle_events.MODE, "enabled": None},
                      {"mode": lifecycle_events.MODE, "enabled": True},
                      {"mode": lifecycle_events.MODE, "enabled": False, "extra": 1},
                      capability(sequence_domain="other-domain"),
                      capability(session_id=0),
                      capability(session_id=True),
                      capability(build={"module": "recorder.dll", "sha256": "xyz"}),
                      capability(build={"module": "", "sha256": BUILD_SHA}),
                      capability(live_validated="no")):
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

    # --- classification and basic protocol ---------------------------------

    def test_valid_fixture_with_sequence_gap_and_child_first_nesting(self):
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

    def test_valid_controlled_boundary_event(self):
        item = envelope(0, event(1, 1))
        item["event"]["version"] = {"epoch": 1, "tick": 900, "revision": 0}
        item["event"]["version_phase"] = "controlled_boundary"
        item["event"]["engine_call_id"] = None
        fixture = self.fixture()
        fixture.refresh(items=[item])
        self.assertEqual(fixture.validate()["status"], "valid")

    def test_empty_enabled_run_is_a_measured_zero(self):
        fixture = self.fixture()
        fixture.refresh(items=[])
        report = fixture.validate()
        self.assertEqual(report["status"], "valid")
        self.assertEqual(report["records"]["count"], 0)
        self.assertIsNone(report["records"]["first_capture_sequence"])
        self.assertEqual(report["close_receipt"]["records"], 0)

    def test_old_trajectory_is_unavailable_not_zero(self):
        fixture = self.fixture(cap=None, write_receipt=False)
        (fixture.audit / lifecycle_events.EVENTS_FILE).unlink()
        report = fixture.validate()
        self.assertEqual(report["status"], "unavailable")
        self.assertIsNone(report["records"]["count"])
        self.assertFalse(report["capability"]["declared"])
        self.assertIn("unavailable", report["claims"]["note"])

    def test_unavailable_with_stray_evidence_fails(self):
        report = self.fixture(cap=None).validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "does not declare the capability")

    def test_disabled_capability_is_classified(self):
        fixture = self.fixture(cap={"mode": lifecycle_events.MODE, "enabled": False}, write_receipt=False)
        (fixture.audit / lifecycle_events.EVENTS_FILE).unlink()
        self.assertEqual(fixture.validate()["status"], "disabled")

    def test_disabled_capability_with_evidence_fails(self):
        report = self.fixture(cap={"mode": lifecycle_events.MODE, "enabled": False}).validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "disabled but lifecycle evidence exists")

    def test_missing_close_receipt_fails_closed(self):
        fixture = self.fixture()
        (fixture.audit / lifecycle_events.RECEIPT_FILE).unlink()
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "close receipt is missing")

    def test_missing_receipt_with_require_close_false_is_open(self):
        fixture = self.fixture()
        (fixture.audit / lifecycle_events.RECEIPT_FILE).unlink()
        report = fixture.validate(require_close=False)
        self.assertEqual(report["status"], "open")
        self.assertEqual(report["problems"], [])
        # A live partial tail is tolerated, but a closed read refuses it.
        fixture.write_events(fixture.events_bytes + b'{"schema":"lvz.lifecycle-record.v1"')
        self.assertEqual(fixture.validate(require_close=False)["status"], "open")
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "truncated")

    def test_receipt_present_remains_strict_even_when_called_live(self):
        fixture = self.fixture()
        data = fixture.events_bytes.splitlines(keepends=True)
        fixture.write_events(b"".join(data[:-1])[:-1])  # truncated last record, receipt unchanged
        report = fixture.validate(require_close=False)
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "truncated")

    def test_live_child_first_prefix_then_parent_then_close(self):
        fixture = self.fixture(sequences=((1, 2, 1, 1), (3, 1, 0, None)))
        full = fixture.items
        # Only the nested child exited so far: invocation 1 is still open on
        # disk and must not be treated as a contradiction.
        fixture.refresh(items=full[:1])
        (fixture.audit / lifecycle_events.RECEIPT_FILE).unlink()
        report = fixture.validate(require_close=False)
        self.assertEqual(report["status"], "open")
        self.assertEqual(report["problems"], [])
        # The parent reaches disk later; the prefix is still open, not closed.
        fixture.refresh(items=full)
        (fixture.audit / lifecycle_events.RECEIPT_FILE).unlink()
        self.assertEqual(fixture.validate(require_close=False)["status"], "open")
        # Only the receipt closes the session and re-enables strict nesting.
        fixture.write_receipt(fixture.receipt)
        self.assertEqual(fixture.validate()["status"], "valid")
        self.assertEqual(fixture.validate(require_close=False)["status"], "valid")

    def test_live_prefix_still_rejects_contradictions(self):
        cases = {
            "duplicate id": [envelope(0, event(1, 2, 1, 1)), envelope(1, event(2, 2, 1, 1))],
            "parent already exited": [envelope(0, event(1, 1)), envelope(1, event(2, 2, 1, 1))],
            "depth mismatch": [envelope(0, event(1, 2, 2, 1))],
            "wrong sibling parent": [envelope(0, event(1, 2, 1, 1)), envelope(1, event(2, 3, 1, 2))],
        }
        for label, items in cases.items():
            with self.subTest(case=label):
                fixture = self.fixture()
                fixture.refresh(items=items)
                (fixture.audit / lifecycle_events.RECEIPT_FILE).unlink()
                report = fixture.validate(require_close=False)
                self.assertEqual(report["status"], "failed", report["problems"])

    def test_live_nesting_reconstruction_is_bounded(self):
        for identifier in (33, 2**64 - 1):
            with self.subTest(identifier=identifier):
                fixture = self.fixture()
                fixture.refresh(items=[envelope(0, event(1, identifier, identifier - 1, identifier - 1))])
                (fixture.audit / lifecycle_events.RECEIPT_FILE).unlink()
                report = fixture.validate(require_close=False)
                self.assertEqual(report["status"], "failed")
                self.assertProblem(report, "native nesting bound")
        fixture = self.fixture()
        fixture.refresh(items=[envelope(0, event(1, 32, 31, 31))])
        (fixture.audit / lifecycle_events.RECEIPT_FILE).unlink()
        self.assertEqual(fixture.validate(require_close=False)["status"], "open")

    def test_live_prefix_tolerates_a_partial_audit_stream_tail(self):
        fixture = self.fixture()
        (fixture.audit / "events.jsonl").write_text(
            json.dumps({"schema": "lvz.audit.v1", "seq": 0, "kind": "pre_step", "payload": {}}) + "\n"
            + '{"schema":"lvz.audit.v1"', encoding="utf-8")
        (fixture.audit / lifecycle_events.RECEIPT_FILE).unlink()
        self.assertEqual(fixture.validate(require_close=False)["status"], "open")
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "not a complete JSONL stream")

    def test_storage_and_decode_failures_are_contract_errors(self):
        with self.subTest(case="invalid utf8 manifest"):
            fixture = self.fixture(name="invalid-utf8")
            (fixture.audit / "manifest.json").write_bytes(b"\xff\xfe\x00")
            with self.assertRaises(lifecycle_events.LifecycleError):
                fixture.validate()
        with self.subTest(case="corrupt codec receipt"):
            fixture = self.fixture(name="codec-receipt")
            evidence_codec.compress_evidence(fixture.audit)
            (fixture.audit / "evidence-codec.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(lifecycle_events.LifecycleError):
                fixture.validate()
        with self.subTest(case="truncated gzip"):
            fixture = self.fixture(name="truncated-gzip")
            evidence_codec.compress_evidence(fixture.audit)
            container = fixture.audit / (lifecycle_events.EVENTS_FILE + ".gz")
            container.write_bytes(container.read_bytes()[:-5])
            with self.assertRaises(lifecycle_events.LifecycleError):
                fixture.validate()
        with self.subTest(case="corrupt gzip payload"):
            fixture = self.fixture(name="corrupt-gzip")
            evidence_codec.compress_evidence(fixture.audit)
            container = fixture.audit / (lifecycle_events.EVENTS_FILE + ".gz")
            raw = bytearray(container.read_bytes())
            raw[15] ^= 0xFF
            container.write_bytes(bytes(raw))
            with self.assertRaises(lifecycle_events.LifecycleError):
                fixture.validate()

    def test_malformed_present_identity_files_report_binding_failure(self):
        fixture = self.fixture()
        (fixture.root / "manifest.json").write_bytes(b"\xff")
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "run manifest is present but unreadable")
        fixture = self.fixture(name="run2")
        (fixture.root / "launcher.json").write_text("{not json", encoding="utf-8")
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "launcher evidence is present but unreadable")
        fixture = self.fixture(name="run3")
        (fixture.root / "manifest.json").write_text("[1, 2]", encoding="utf-8")
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "run manifest is present but is not an object")

    def test_null_identity_files_are_not_absent(self):
        for name in ("manifest.json", "launcher.json"):
            with self.subTest(name=name):
                fixture = self.fixture(name=name)
                (fixture.root / name).write_text("null", encoding="utf-8")
                report = fixture.validate()
                self.assertEqual(report["status"], "failed")
                self.assertProblem(report, "not an object")

    def test_missing_events_file_fails(self):
        fixture = self.fixture()
        (fixture.audit / lifecycle_events.EVENTS_FILE).unlink()
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "no lifecycle-events.jsonl")

    def test_truncated_events_fail(self):
        fixture = self.fixture()
        data = fixture.events_bytes.splitlines(keepends=True)
        fixture.write_events(b"".join(data[:-1])[:-1])
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "truncated")
        self.assertProblem(report, "byte count")

    # --- sequences -----------------------------------------------------------

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

    # --- invocation nesting --------------------------------------------------

    def test_duplicate_invocation_id_fails(self):
        fixture = self.fixture()
        fixture.refresh(items=[envelope(0, event(1, 2, 1, 1)), envelope(1, event(3, 2)),
                               envelope(2, event(4, 3))])
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "duplicate invocation_id 2")

    def test_depth_mismatch_fails(self):
        fixture = self.fixture()
        child = event(1, 2, 2, 1)
        parent = event(3, 1)
        fixture.refresh(items=[envelope(0, child), envelope(1, parent)])
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "does not match its nesting depth")

    def test_parent_exit_before_child_fails(self):
        fixture = self.fixture()
        fixture.refresh(items=[envelope(0, event(1, 1)), envelope(1, event(3, 2, 1, 1))])
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "does not match the LIFO parent")

    def test_crossed_nesting_fails(self):
        # invocation 1 opens; 2 nests and exits; 3 claims to be a new root while
        # 1 is still open; 1 then exits. LIFO says 3's parent must be 1.
        fixture = self.fixture()
        fixture.refresh(items=[envelope(0, event(1, 2, 1, 1)), envelope(1, event(2, 3)),
                               envelope(2, event(3, 1))])
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "parent None does not match the LIFO parent 1")

    def test_missing_invocation_counter_slot_fails(self):
        fixture = self.fixture()
        fixture.refresh(items=[envelope(0, event(1, 1)), envelope(1, event(2, 3))])
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "contiguous session counter")

    def test_bad_parent_reference_fails(self):
        fixture = self.fixture()
        fixture.refresh(items=[envelope(0, event(1, 2, 1, 1)), envelope(1, event(3, 1))])
        fixture.items[0]["event"]["invocation"]["parent_invocation_id"] = 99
        fixture.refresh(items=fixture.items)
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "LIFO parent")

    # --- semantic event contract --------------------------------------------

    def test_semantic_mutations_fail_with_a_consistent_receipt(self):
        mutations = {
            "wrong kind": lambda e: e.__setitem__("kind", "confirmed_death_stage"),
            "missing before_after": lambda e: e.pop("before_after"),
            "null before_after": lambda e: e.__setitem__("before_after", None),
            "non-null before": lambda e: e["before_after"].__setitem__("before", {"id": 1}),
            "missing after field": lambda e: e["before_after"]["after"].pop("game_clock"),
            "before snapshot identity drift": lambda e: e["before_after"]["after"].__setitem__("slot", 2),
            "entity identity drift": lambda e: e["entity"].__setitem__("id", 2),
            "known cause": lambda e: e["classification"].__setitem__("cause", "known"),
            "initialization version": lambda e: e.__setitem__("version", {"epoch": 1, "tick": 0, "revision": 0}),
            "initialization engine_call_id": lambda e: e.__setitem__("engine_call_id", 7),
            "unknown phase": lambda e: e.__setitem__("version_phase", "unknown"),
            "incomplete event": lambda e: e.__setitem__("complete", False),
            "wrong probe schema": lambda e: e["probe"].__setitem__("schema", "lvz.other.v1"),
            "extra key": lambda e: e.__setitem__("extra", 1),
            "float sequence": lambda e: e.__setitem__("capture_sequence", 1.0),
            "bool sequence": lambda e: e.__setitem__("capture_sequence", True),
            "string sequence": lambda e: e.__setitem__("capture_sequence", "1"),
            "list sequence": lambda e: e.__setitem__("capture_sequence", []),
            "wrong entity generation": lambda e: e["entity"].__setitem__("generation", 9),
        }
        for label, mutate in mutations.items():
            with self.subTest(mutation=label):
                fixture = self.fixture()
                item = copy.deepcopy(fixture.items[0])
                mutate(item["event"])
                fixture.refresh(items=[item])
                report = fixture.validate()
                self.assertEqual(report["status"], "failed", report["problems"])

    # --- malformed JSON types must never crash -------------------------------

    def test_malformed_value_table_never_crashes(self):
        tables = {
            "envelope run_id list": lambda f: f.items[0].__setitem__("run_id", []),
            "envelope branch dict": lambda f: f.items[0].__setitem__("branch_id", {}),
            "envelope session bool": lambda f: f.items[0].__setitem__("session_id", True),
            "envelope file_seq float": lambda f: f.items[0].__setitem__("file_seq", 0.5),
            "envelope event null": lambda f: f.items[0].__setitem__("event", None),
            "envelope event list": lambda f: f.items[0].__setitem__("event", []),
            "envelope schema list": lambda f: f.items[0].__setitem__("schema", []),
            "invocation null": lambda f: f.items[0]["event"].__setitem__("invocation", None),
            "invocation id bool": lambda f: f.items[0]["event"]["invocation"].__setitem__("invocation_id", True),
            "invocation parent list": lambda f: f.items[0]["event"]["invocation"].__setitem__(
                "parent_invocation_id", []),
            "entity list": lambda f: f.items[0]["event"].__setitem__("entity", []),
            "entity id null": lambda f: f.items[0]["event"]["entity"].__setitem__("id", None),
            "classification list": lambda f: f.items[0]["event"].__setitem__("classification", []),
            "probe null": lambda f: f.items[0]["event"].__setitem__("probe", None),
            "version list on initialization": lambda f: f.items[0]["event"].__setitem__("version", []),
            "version_phase dict": lambda f: f.items[0]["event"].__setitem__("version_phase", {}),
            "capture_sequence dict": lambda f: f.items[0]["event"].__setitem__("capture_sequence", {}),
            "capture_sequence null": lambda f: f.items[0]["event"].__setitem__("capture_sequence", None),
            "kind list": lambda f: f.items[0]["event"].__setitem__("kind", []),
            "record as list": lambda f: f.items.__setitem__(0, []),
        }
        for label, mutate in tables.items():
            with self.subTest(mutation=label):
                fixture = self.fixture()
                mutate(fixture)
                fixture.refresh(items=fixture.items)
                try:
                    report = fixture.validate()
                except lifecycle_events.LifecycleError:
                    continue
                self.assertEqual(report["status"], "failed", report["problems"])
                self.assertIsInstance(report["records"]["identities"], list)
        # Invalid JSON and non-object lines must also be structured failures.
        for data in (b"{not json}\n", b"[1,2,3]\n", b"null\n", b'"text"\n', b"{}\n"):
            with self.subTest(data=data):
                fixture = self.fixture()
                fixture.write_events(data)
                try:
                    report = fixture.validate()
                except lifecycle_events.LifecycleError:
                    continue
                self.assertEqual(report["status"], "failed")

    def test_capability_null_is_a_contract_error_not_unavailable(self):
        fixture = self.fixture()
        manifest = json.loads((fixture.audit / "manifest.json").read_text(encoding="utf-8"))
        manifest["lifecycle_recording"] = None
        (fixture.audit / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(lifecycle_events.LifecycleError):
            fixture.validate()
        (fixture.audit / lifecycle_events.EVENTS_FILE).unlink()
        (fixture.audit / lifecycle_events.RECEIPT_FILE).unlink()
        with self.assertRaises(lifecycle_events.LifecycleError):
            fixture.validate()

    # --- receipt contract ----------------------------------------------------

    def test_receipt_mutations_fail(self):
        mutations = {
            "count": lambda r: r.__setitem__("records", r["records"] + 1),
            "digest": lambda r: r.__setitem__("sha256", "0" * 64),
            "manifest digest": lambda r: r.__setitem__("manifest_sha256", "1" * 64),
            "empty run id": lambda r: r.__setitem__("run_id", ""),
            "empty branch id": lambda r: r.__setitem__("branch_id", ""),
            "session bool": lambda r: r.__setitem__("session_id", True),
            "persistence method": lambda r: r["persistence"].__setitem__("method", "pre_written_flag"),
            "persistence order": lambda r: r["persistence"].__setitem__("receipt_written_after_close", False),
            "probe": lambda r: r["probe"].__setitem__("name", "other"),
            "build": lambda r: r["build"].__setitem__("sha256", "c" * 64),
            "counter mismatch": lambda r: r["counters"].__setitem__("persisted", 2),
            "counter fault": lambda r: r["counters"].__setitem__("overflow", 1),
            "health fault": lambda r: r["probe_health"].__setitem__("faults", 1),
            "health unhealthy": lambda r: r["probe_health"].__setitem__("healthy", False),
            "health queued": lambda r: r["probe_health"].__setitem__("queued", 1),
            "health captured": lambda r: r["probe_health"].__setitem__("captured", 99),
            "first bound": lambda r: r.__setitem__("first_capture_sequence", 2),
            "null first bound": lambda r: r.__setitem__("first_capture_sequence", None),
            "extra key": lambda r: r.__setitem__("extra", 1),
        }
        for label, mutate in mutations.items():
            with self.subTest(mutation=label):
                fixture = self.fixture()
                mutate(fixture.receipt)
                fixture.write_receipt(fixture.receipt)
                report = fixture.validate()
                self.assertEqual(report["status"], "failed", report["problems"])

    def test_zero_record_receipt_requires_non_null_identity(self):
        fixture = self.fixture()
        fixture.refresh(items=[], run_id="")
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "run_id must be a non-empty string")

    def test_zero_record_receipt_rejects_non_null_bounds(self):
        fixture = self.fixture()
        fixture.refresh(items=[])
        fixture.receipt["first_capture_sequence"] = 1
        fixture.write_receipt(fixture.receipt)
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "zero-record receipt")

    # --- probe faults and audit-stream corruption ----------------------------

    def test_explicit_probe_fault_disqualifies_success(self):
        fixture = self.fixture()
        (fixture.audit / "events.jsonl").write_text(
            json.dumps({"schema": "lvz.audit.v1", "seq": 0, "kind": "spawn_hook_fault",
                        "payload": {"healthy": False}}) + "\n", encoding="utf-8")
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["probe_events"]["fault_events"], 1)
        self.assertProblem(report, "explicit probe fault")

    def test_unhealthy_probe_close_disqualifies_success(self):
        fixture = self.fixture()
        (fixture.audit / "events.jsonl").write_text(
            json.dumps({"schema": "lvz.audit.v1", "seq": 0, "kind": "spawn_hook_closed",
                        "payload": {"healthy": False}}) + "\n", encoding="utf-8")
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["probe_events"]["closed_unhealthy"])
        self.assertProblem(report, "unhealthy probe close")

    def test_unreadable_audit_events_disqualify_a_closed_read(self):
        fixture = self.fixture()
        (fixture.audit / "events.jsonl").write_text("{not json}\n", encoding="utf-8")
        report = fixture.validate()
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "unreadable")
        # A live reader may race a partial audit record; it still needs explicit faults.
        report = fixture.validate(require_close=False)
        self.assertEqual(report["status"], "failed")

    # --- identity bindings ---------------------------------------------------

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
        recorder = Path(self._temp.name) / "recorder.dll"
        recorder.write_bytes(b"recorder-bytes")
        digest = hashlib.sha256(recorder.read_bytes()).hexdigest()
        fixture = self.fixture(cap=capability(build={"module": "recorder.dll", "sha256": digest}))
        self.assertEqual(fixture.validate(recorder=recorder)["status"], "valid")
        other = Path(self._temp.name) / "other.dll"
        other.write_bytes(b"other-bytes")
        report = fixture.validate(recorder=other)
        self.assertEqual(report["status"], "failed")
        self.assertProblem(report, "recorder binary does not match")

    # --- archive integration -------------------------------------------------

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
        self.assertEqual(record_schema["$defs"]["event"]["properties"]["kind"]["const"],
                         lifecycle_events.KIND_INITIALIZATION)
        self.assertEqual(record_schema["$defs"]["event"]["properties"]["classification"]["properties"]["class"]["const"],
                         "initialization")
        self.assertEqual(receipt_schema["$defs"]["receipt_v1"]["properties"]["schema"]["const"],
                         lifecycle_events.RECEIPT_SCHEMA)
        self.assertEqual(receipt_schema["$defs"]["receipt_v2"]["properties"]["schema"]["const"],
                         lifecycle_events.PROBE_RECEIPT_SCHEMA)
        self.assertEqual(receipt_schema["oneOf"],
                         [{"$ref": "#/$defs/receipt_v1"}, {"$ref": "#/$defs/receipt_v2"}])
        self.assertFalse(record_schema["additionalProperties"])
        self.assertFalse(receipt_schema["$defs"]["receipt_v1"]["additionalProperties"])
        self.assertFalse(receipt_schema["$defs"]["receipt_v2"]["additionalProperties"])
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp) / "run")
            envelope = fixture.items[0]
            self.assertEqual(set(envelope), set(record_schema["properties"]))
            self.assertEqual(set(record_schema["required"]), set(record_schema["properties"]))
            event = envelope["event"]
            event_schema = record_schema["$defs"]["event"]
            self.assertEqual(set(event), set(event_schema["properties"]))
            self.assertEqual(set(event_schema["required"]), set(event_schema["properties"]))
            self.assertEqual(set(event["before_after"]["after"]), set(record_schema["$defs"]["snapshot"]["properties"]))
            receipt_v1 = receipt_schema["$defs"]["receipt_v1"]
            self.assertEqual(set(fixture.receipt), set(receipt_v1["properties"]))
            self.assertEqual(set(receipt_v1["required"]), set(receipt_v1["properties"]))


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

    def test_cli_malformed_capability_is_a_contract_error(self):
        fixture = Fixture(Path(self._temp.name) / "run")
        manifest = json.loads((fixture.audit / "manifest.json").read_text(encoding="utf-8"))
        manifest["lifecycle_recording"] = None
        (fixture.audit / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(self.run_tool(fixture.root).returncode, 2)

    def test_cli_storage_error_is_exit_2_with_valid_json(self):
        fixture = Fixture(Path(self._temp.name) / "run")
        (fixture.audit / "manifest.json").write_bytes(b"\xff\xfe\x00")
        result = self.run_tool(fixture.root)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "invalid")

    def test_cli_missing_path_is_contract_error(self):
        self.assertEqual(self.run_tool(Path(self._temp.name) / "absent").returncode, 2)

    def test_resolve_audit_accepts_run_and_audit_directories(self):
        fixture = Fixture(Path(self._temp.name) / "run")
        self.assertEqual(checker.resolve_audit(fixture.root), fixture.audit)
        self.assertEqual(checker.resolve_audit(fixture.audit), fixture.audit)


if __name__ == "__main__":
    unittest.main()
