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
    events_path = audit / "events.jsonl"
    if events_path.is_file():
        kept = [line for line in events_path.read_text(encoding="utf-8").splitlines()
                if json.loads(line).get("kind") != "lifecycle_probes_closed"]
        events_path.write_text("".join(line + chr(10) for line in kept), encoding="utf-8")
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


def write_trace(run: Path, *, executed_ticks=1, method="advance") -> None:
    """One minimal strict trace accepted by engine_replay._trace_steps."""
    marker = {
        "schema": "lvz.engine-replay.v1",
        "identity": {"build": {"runtime_protocol": 1, "avz_commit": "a" * 40},
                     "game": {"schema": "lvz.audit.v1", "loaded_signatures_match": True,
                              "target": "synthetic"},
                     "artifacts": {"runtime": "a" * 64}},
        "observation": {"version": {"epoch": 0, "tick": 0, "revision": 0}, "game_ui": 3, "scene": 1},
        "initialization": {"execution_mode": "headless"},
        "state": {"schema": "lvz.audit.v1", "rng": {"target": "synthetic"}, "board": {"tick": 0}},
    }
    declared = run / "replay-initial.json"
    if declared.is_file():
        try:
            declared_version = (json.loads(declared.read_bytes()).get("observation") or {}).get("version")
        except (OSError, UnicodeError, json.JSONDecodeError):
            declared_version = None
        if isinstance(declared_version, dict):
            marker["observation"]["version"] = declared_version
    records = [
        {"schema": 1, "seq": 0, "kind": "replay_initial", "data": marker},
        {"schema": 1, "seq": 1, "kind": "request",
         "data": {"protocol": 1, "request_id": "a", "branch": "run-branch-identity",
                  "method": method, "params": {"ticks": 1}}},
        {"schema": 1, "seq": 2, "kind": "response",
         "data": {"protocol": 1, "request_id": "a", "ok": True,
                  "result": {"executed_ticks": executed_ticks,
                             "observation": {"version": {"epoch": 0, "tick": 1, "revision": 0}}}}},
        {"schema": 1, "seq": 3, "kind": "request",
         "data": {"protocol": 1, "request_id": "b", "method": "stop_recording", "params": {}}},
        {"schema": 1, "seq": 4, "kind": "response",
         "data": {"protocol": 1, "request_id": "b", "ok": True, "result": {"closed": True}}},
    ]
    path = run / "decisions" / "evaluation.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record, separators=(",", ":")) + chr(10) for record in records),
                    encoding="utf-8")

class ScopedCompareTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.base = Path(self._temp.name)
        self.on_run, _, _ = build_run(self.base / "on")
        write_trace(self.on_run)
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

    def test_action_or_result_mismatch_is_detected_even_with_equal_audits(self):
        write_trace(self.on_b, executed_ticks=0)
        scoped = lifecycle_compare.compare_scoped(self.on_run, self.on_b, scope="common")
        self.assertFalse(scoped["equal"])
        self.assertTrue(scoped["common"]["equal"], scoped["common"])
        self.assertEqual(scoped["actions"]["reason"], "action_or_result")
        self.assertEqual(scoped["actions"]["index"], 0)

    def test_action_method_difference_is_detected(self):
        write_trace(self.on_b, method="commit")
        scoped = lifecycle_compare.compare_scoped(self.on_run, self.on_b, scope="common")
        self.assertFalse(scoped["equal"])
        self.assertEqual(scoped["actions"]["reason"], "action_method")

    def test_resource_telemetry_is_not_a_gameplay_divergence_but_stop_is(self):
        from llm_vs_zombies import action_compare
        for run, elapsed, free in ((self.on_run, 10.0, 9000000000),
                                   (self.on_b, 12.4, 8999999000)):
            path = run / "experiment-end.json"
            endpoint = json.loads(path.read_bytes())
            endpoint["resources"] = {
                "wall_budget_seconds": 21600, "elapsed_seconds": elapsed,
                "samples": [{"version": {"epoch": 0, "tick": 0, "revision": 0},
                             "elapsed_seconds": elapsed, "free_bytes": free,
                             "min_free_bytes": 2147483648}], "stop": None}
            path.write_text(json.dumps(endpoint), encoding="utf-8")
        result = action_compare.compare_actions(self.on_run, self.on_b)
        self.assertTrue(result["equal"], result)
        self.assertIn("experiment_end.resources.elapsed_seconds", result["normalized_resource_telemetry"])
        path = self.on_b / "experiment-end.json"
        endpoint = json.loads(path.read_bytes())
        endpoint["resources"]["stop"] = {"reason": "disk_reserve_stop"}
        path.write_text(json.dumps(endpoint), encoding="utf-8")
        self.assertFalse(action_compare.compare_actions(self.on_run, self.on_b)["equal"])

    def test_missing_endpoint_is_not_an_equal_outcome(self):
        from llm_vs_zombies import action_compare
        (self.on_run / "experiment-end.json").unlink()
        (self.on_b / "experiment-end.json").unlink()
        with self.assertRaises(action_compare.ActionCompareError):
            action_compare.compare_actions(self.on_run, self.on_b)

    def test_action_envelope_branch_identity_is_normalized_but_params_are_not(self):
        path = self.on_b / "decisions" / "evaluation.jsonl"
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        records[1]["data"]["branch"] = "different-run-branch"
        path.write_text("".join(json.dumps(r, separators=(",", ":")) + chr(10) for r in records),
                        encoding="utf-8")
        scoped = lifecycle_compare.compare_scoped(self.on_run, self.on_b, scope="common")
        self.assertTrue(scoped["equal"], scoped)
        records[1]["data"]["params"]["branch"] = "semantic-parameter"
        path.write_text("".join(json.dumps(r, separators=(",", ":")) + chr(10) for r in records),
                        encoding="utf-8")
        scoped = lifecycle_compare.compare_scoped(self.on_run, self.on_b, scope="common")
        self.assertFalse(scoped["equal"])
        self.assertEqual(scoped["actions"]["reason"], "action_or_result")

    def test_outcome_difference_is_reported(self):
        end = self.on_b / "experiment-end.json"
        value = json.loads(end.read_text(encoding="utf-8"))
        value["maximum_wave"] = value.get("maximum_wave", 2) + 1
        end.write_text(json.dumps(value) + chr(10), encoding="utf-8")
        scoped = lifecycle_compare.compare_scoped(self.on_run, self.on_b, scope="common")
        self.assertFalse(scoped["equal"])
        self.assertEqual(scoped["actions"]["reason"], "outcome")

    def test_real_particle_identity_mismatch_is_reported(self):
        from tests.test_audit_compare import particle_audit
        left = self.base / "p-left" / "audit"
        right = self.base / "p-right" / "audit"
        left.parent.mkdir(parents=True)
        right.parent.mkdir(parents=True)
        particle_audit(left)
        particle_audit(right, identity=131074)
        from llm_vs_zombies import audit_compare
        common = audit_compare.compare_common_audits(
            audit_compare.AuditLog(left, require_closed=True),
            audit_compare.AuditLog(right, require_closed=True))
        self.assertFalse(common["equal"])
        self.assertEqual(common["reason"], "particle_shake")


if __name__ == "__main__":
    unittest.main()
