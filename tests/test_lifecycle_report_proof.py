"""End-to-end report/CLI proof tests for the capture-level first-kill gate.

Each synthetic run is a strict audit (built from the repository's animation
fixture) plus a valid mixed v1/v2 lifecycle stream and executed-endpoint
evidence; the tests exercise ``report_for_run`` and the real CLI wrapper.
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
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))

from llm_vs_zombies import lifecycle_events, lifecycle_report  # noqa: E402
from tests.test_lifecycle_probes_contract import (  # noqa: E402
    SITES, envelope, probe_counters, probe_event, v1_event,
)


def build_run(base: Path, *, with_receipt: bool = True, truncate: bool = False,
              endpoint_version: dict | None = None) -> tuple[Path, Path, Path]:
    from tests.test_audit_compare import animation_audit
    runs = base / "experiments" / "runs"
    run = runs / "issue111-d-probe-on-a-s42-c0"
    audit = run / "audit"
    run.mkdir(parents=True)
    animation_audit(audit)

    v1 = v1_event(1)
    v1["version"] = {"epoch": 1, "tick": 0, "revision": 0}
    v1["version_phase"] = "controlled_boundary"
    v1["engine_call_id"] = 42
    v1["classification"] = {"class": "initialization", "cause": "unknown"}
    events = [
        v1,
        probe_event("zombie_phase_transition", 2, site="phase-mowdown"),
        probe_event("zombie_removal_marked", 3),
        probe_event("zombie_slot_recycle_candidate", 4),
        probe_event("zombie_slot_recycle_commit", 5,
                    recycle={"state": "committed", "slot": 1, "candidate_capture_sequence": 4,
                             "free_head_after": 1, "count_after": 1}),
    ]
    for event in events[1:]:
        event["version"] = {"epoch": 1, "tick": 1, "revision": 0}
        event["version_phase"] = "controlled_boundary"
        event["engine_call_id"] = 42

    manifest = json.loads((audit / "manifest.json").read_text(encoding="utf-8"))
    manifest["lifecycle_recording"] = {
        "mode": lifecycle_events.MODE, "enabled": True,
        "event_schema": lifecycle_events.EVENT_SCHEMA,
        "envelope_schema": lifecycle_events.ENVELOPE_SCHEMA,
        "receipt_schema": lifecycle_events.RECEIPT_SCHEMA,
        "sequence_domain": lifecycle_events.SEQUENCE_DOMAIN, "session_id": 7,
        "probe": {"name": lifecycle_events.PROBE_NAME, "schema": lifecycle_events.PROBE_SCHEMA,
                  "event_kind": lifecycle_events.KIND_INITIALIZATION},
        "build": {"module": "recorder.dll", "sha256": "b" * 64},
        "files": {"events": lifecycle_events.EVENTS_FILE, "close_receipt": lifecycle_events.RECEIPT_FILE},
        "live_validated": False,
    }
    v2_count = len(events) - 1
    final_counters = probe_counters()
    final_counters.update({"captured": v2_count, "delivered": v2_count})
    manifest["lifecycle_probes"] = {
        "mode": lifecycle_events.PROBE_MODE, "enabled": True,
        "record_schema": lifecycle_events.PROBE_EVENT_SCHEMA,
        "event_schemas": [lifecycle_events.PROBE_EVENT_SCHEMA],
        "probe_set": list(lifecycle_events.PROBE_SET), "session_id": 7,
        "build": {"module": "recorder.dll", "sha256": "b" * 64},
        "sites": SITES, "patch_windows_evidence": "docs/issue111-patch-windows.json",
        "probe_counters": final_counters, "healthy": True, "pending_candidate": False,
        "active": False, "installed": False, "live_validated": False,
    }
    manifest_bytes = (json.dumps(manifest, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    (audit / "manifest.json").write_bytes(manifest_bytes)

    def child_envelope(index, event):
        value = envelope(index, event)
        value["run_id"] = run.name
        return value
    data = b"".join((json.dumps(child_envelope(index, event), separators=(",", ":")) + "\n").encode("utf-8")
                    for index, event in enumerate(events))
    if truncate:
        data = data[:-1]
    (audit / lifecycle_events.EVENTS_FILE).write_bytes(data)

    with_persisted = dict(final_counters, persisted=v2_count)
    health = {"installed": False, "active": False, "healthy": True, "pending_candidate": False,
              "patched_sites": [], "counters": with_persisted, "sites": SITES,
              "reader_protected": False, "pending_callbacks": 0}
    if with_receipt:
        receipt = {
            "schema": lifecycle_events.PROBE_RECEIPT_SCHEMA, "run_id": run.name,
            "branch_id": "branch-probe", "session_id": 7,
            "sequence_domain": lifecycle_events.SEQUENCE_DOMAIN,
            "envelope_schema": lifecycle_events.ENVELOPE_SCHEMA,
            "event_schemas": [lifecycle_events.EVENT_SCHEMA, lifecycle_events.PROBE_EVENT_SCHEMA],
            "probe": {"name": lifecycle_events.PROBE_NAME, "schema": lifecycle_events.PROBE_SCHEMA},
            "build": {"module": "recorder.dll", "sha256": "b" * 64},
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "records": len(events), "first_capture_sequence": events[0]["capture_sequence"],
            "last_capture_sequence": events[-1]["capture_sequence"], "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "counters": {
                "initialization": {"captured": 1, "delivered": 1, "persisted": 1, "overflow": 0,
                                   "wrong_thread": 0, "nesting_mismatch": 0, "incomplete_events": 0},
                "probes": with_persisted,
                "total": {"persisted": len(events), "records": len(events), "bytes": len(data)},
            },
            "probe_health": health, "completed": True,
            "persistence": {"method": "flush_close_then_atomic_receipt",
                            "receipt_written_after_close": True},
        }
        (audit / lifecycle_events.RECEIPT_FILE).write_text(
            json.dumps(receipt, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")

    # Insert the probe-close audit event before recording_closed, as the real
    # cleanup order does.
    lines = [json.loads(line) for line in (audit / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    closing = lines.pop()
    closed = {"schema": "lvz.audit.v1", "seq": closing["seq"], "kind": "lifecycle_probes_closed",
              "version": closing["version"], "payload": health}
    closing["seq"] += 1
    lines += [closed, closing]
    (audit / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in lines), encoding="utf-8")

    endpoint = endpoint_version or {"epoch": 1, "tick": 1, "revision": 0}
    (run / "experiment-end.json").write_text(json.dumps({
        "final_observation": {"version": endpoint}, "maximum_wave": 2, "full_cycle": True}),
        encoding="utf-8")
    # The captured initial boundary and the run/build identity that the frozen
    # plan and its raw identity binding must agree with.
    initial_frame = json.loads((audit / "state-deltas.jsonl").read_text(encoding="utf-8").splitlines()[0])
    (run / "replay-initial.json").write_text(json.dumps({
        "schema": "lvz.replay-initial.v1",
        "observation": {"version": {"epoch": 1, "tick": 0, "revision": 0},
                        "game_clock": 0},
        "state": initial_frame["initial"]}), encoding="utf-8")
    (run / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "run_id": run.name, "status": "recording",
        "implementation": {"recorder_sha256": "b" * 64}}), encoding="utf-8")
    # The real frozen issue99 plan and the serialization run_suite writes.
    from dataclasses import asdict

    from llm_vs_zombies import evaluation
    plan_path = ROOT / "experiments" / "plans" / "issue99-shovel-control.json"
    normalized = asdict(evaluation.Plan.load(plan_path))
    suite = runs / "issue111-d-probe-on-a"
    suite.mkdir()
    (suite / "plan.json").write_text(json.dumps(normalized, sort_keys=True) + "\n", encoding="utf-8")
    (suite / "lifecycle-plan-binding.json").write_text(json.dumps({
        "schema": "lvz.lifecycle-plan-binding.v1",
        "raw_plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        "plan": "experiments/plans/issue99-shovel-control.json",
        "mode": "on", "probes": "on", "single_cold": True,
        "recorder_sha256": "b" * 64}) + "\n", encoding="utf-8")
    return run, suite, plan_path


class ReportProofTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.base = Path(self._temp.name)

    def test_complete_evidence_proves_first_kill(self):
        run, _, plan_path = build_run(self.base)
        report = lifecycle_report.report_for_run(run, plan=plan_path)
        self.assertTrue(report["first_kill"]["proven"], report["first_kill"])
        capture = report["capture_facts"]
        self.assertTrue(capture["first_kill"]["proven"], capture["first_kill"])
        self.assertTrue(all(capture["first_kill"]["prerequisites"].values()),
                        capture["first_kill"]["prerequisites"])
        self.assertEqual(capture["summary"]["same_call_lifetimes"], 1)
        self.assertEqual(capture["summary"]["initialization_facts"], 1)

    def test_missing_receipt_and_truncated_stream_do_not_prove(self):
        run, _, plan_path = build_run(self.base / "missing", with_receipt=False)
        report = lifecycle_report.report_for_run(run, plan=plan_path, require_closed=False)
        self.assertFalse(report["first_kill"]["proven"])
        self.assertIn("receipt", " ".join(report["first_kill"]["reasons"]))
        run2, _, plan2 = build_run(self.base / "truncated", truncate=True)
        with self.assertRaises(lifecycle_report.ReportError):
            lifecycle_report.report_for_run(run2, plan=plan2)

    def test_plan_and_endpoint_mismatch_do_not_prove(self):
        run, suite, plan_path = build_run(self.base)
        suite_plan = json.loads((suite / "plan.json").read_text(encoding="utf-8"))
        suite_plan["tick_budget"] = 9999
        (suite / "plan.json").write_text(json.dumps(suite_plan, sort_keys=True) + "\n", encoding="utf-8")
        report = lifecycle_report.report_for_run(run, plan=plan_path)
        self.assertFalse(report["first_kill"]["proven"])
        self.assertTrue(any("does not match" in problem
                            for problem in report["coverage"]["full_window_problems"]))
        run2, _, plan2 = build_run(self.base / "endpoint",
                                   endpoint_version={"epoch": 1, "tick": 99, "revision": 0})
        report2 = lifecycle_report.report_for_run(run2, plan=plan2)
        self.assertFalse(report2["first_kill"]["proven"])
        self.assertTrue(any("does not end" in problem
                            for problem in report2["coverage"]["full_window_problems"]))

    def test_prepared_mode_and_probe_arms_must_match_the_audit(self):
        run, suite, plan_path = build_run(self.base)
        binding_path = suite / "lifecycle-plan-binding.json"
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        binding["probes"] = "off"
        binding_path.write_text(json.dumps(binding) + "\n", encoding="utf-8")
        report = lifecycle_report.report_for_run(run, plan=plan_path)
        self.assertFalse(report["first_kill"]["proven"])
        self.assertTrue(any("probe arm" in problem
                            for problem in report["coverage"]["binding_problems"]))

    def test_legitimate_preceding_boundary_is_located_by_version_and_state(self):
        run, _, plan_path = build_run(self.base)
        from llm_vs_zombies import audit_compare
        initial_path = run / "replay-initial.json"
        initial = json.loads(initial_path.read_text(encoding="utf-8"))
        frames = audit_compare.AuditLog(run / "audit", require_closed=True).frames
        initial["observation"]["version"] = dict(frames[1].version)
        initial["state"] = frames[1].state
        initial_path.write_text(json.dumps(initial), encoding="utf-8")
        report = lifecycle_report.report_for_run(run, plan=plan_path)
        self.assertTrue(report["first_kill"]["proven"], report["coverage"]["full_window_problems"])
        self.assertEqual(report["coverage"]["window_start_index"], 1)

    def test_audit_directory_input_and_sealed_evidence_keep_the_proof(self):
        from llm_vs_zombies import evidence_codec
        run, _, plan_path = build_run(self.base)
        as_run = lifecycle_report.report_for_run(run, plan=plan_path)
        as_audit = lifecycle_report.report_for_run(run / "audit", plan=plan_path)
        self.assertTrue(as_audit["first_kill"]["proven"], as_audit["first_kill"])
        self.assertEqual(as_run["capture_facts"], as_audit["capture_facts"])
        evidence_codec.compress_evidence(run / "audit")
        sealed = lifecycle_report.report_for_run(run, plan=plan_path)
        self.assertTrue(sealed["first_kill"]["proven"], sealed["first_kill"])
        self.assertEqual(as_run["capture_facts"], sealed["capture_facts"])
        self.assertEqual(as_run["first_kill"], sealed["first_kill"])

    def test_cli_reports_proof_and_refuses_broken_coverage(self):
        script = ROOT / "tools" / "issue111_lifecycle_report.py"
        run, _, plan_path = build_run(self.base)
        result = subprocess.run([sys.executable, str(script), str(run), "--plan", str(plan_path)],
                                capture_output=True, text=True, timeout=300)
        report = json.loads(result.stdout)
        self.assertTrue(report["first_kill"]["proven"], report["first_kill"])
        broken, _, broken_plan = build_run(self.base / "cli-broken", truncate=True)
        result = subprocess.run([sys.executable, str(script), str(broken), "--plan", str(broken_plan)],
                                capture_output=True, text=True, timeout=300)
        report = json.loads(result.stdout)
        self.assertFalse(report["ok"])
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
