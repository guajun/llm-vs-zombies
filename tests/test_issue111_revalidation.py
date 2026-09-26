"""Strict Stage-D revalidation/seal/verify tests (pure computation vs writes)."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))

import issue111_lifecycle_experiment as experiment  # noqa: E402
from tests.test_issue111_lifecycle_experiment import build_root  # noqa: E402


def copy_sources(root: Path) -> None:
    for relative in experiment._VALIDATOR_SOURCES:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)


def prepare_arm(root: Path, *, probes="on"):
    copy_sources(root)
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "--allow-empty", "-m", "init"], check=True, capture_output=True)
    plan = root / "experiments" / "plans" / "d.json"
    name = f"issue111-d-hosted-v2-{probes}-a"
    metadata = experiment.prepare(root, name, plan, "on", run_builds=False, probes=probes,
                                  single_cold=True)
    suite = Path(metadata["suite"])
    suite.mkdir(parents=True, exist_ok=True)
    (suite / "lifecycle-plan-binding.json").write_text(json.dumps(
        {"schema": "lvz.lifecycle-plan-binding.v1",
         "raw_plan_sha256": metadata["plan"]["sha256"], "plan": metadata["plan"]["path"],
         "mode": "on", "probes": probes, "single_cold": True}), encoding="utf-8")
    plan_object = experiment._load_plan(plan)
    child = metadata["expected_children"][0]
    (suite / "evaluation.json").write_text(json.dumps(suite_report(metadata, plan_object, child,
                                                                   on=probes == "on")),
                                           encoding="utf-8")
    return metadata, suite, plan_object, child


def suite_report(metadata, plan, child, *, on=True, checks_override=None, status=None,
                 infra=None, secondary=None, cleanup_passed=True, cleanup=None, wave=2,
                 child_name=None, seed=None, cold=None, session_run=None):
    checks = {name: {"status": "pass"} for name in experiment._EXPECTED_GATES}
    for name in experiment._UNVERIFIED_GATES:
        checks[name] = {"status": "unverified"}
    checks["full_cycle"] = {"status": "fail"}
    checks["ten_cold_starts"] = {"status": "fail"}
    expected_status = "completed"
    expected_infra = []
    expected_secondary = []
    if on:
        checks["recording"] = {"status": "fail"}
        checks["archive_integrity"] = {"status": "fail"}
        expected_status = "failed"
        expected_infra = ["recording"]
        expected_secondary = [dict(experiment._OLD_READER_SECONDARY)]
    checks.update(checks_override or {})
    run_path = str(Path(metadata["suite"]).parent / (child if child_name is None else child_name))
    session = {"run": session_run if session_run is not None else run_path,
               "infrastructure_failures": expected_infra if infra is None else infra,
               "primary_error": None,
               "secondary_errors": expected_secondary if secondary is None else secondary,
               "cleanup_passed": cleanup_passed,
               "cleanup": {"recording_closed": True, "client_closed": True, "trace_closed": True,
                           "owned_process_stopped": True} if cleanup is None else cleanup}
    case = {"seed": plan.seeds[0] if seed is None else seed, "source": "live_engine",
            "expected_run": run_path, "outcome": "stop_condition_reached", "maximum_wave": wave,
            "status": expected_status if status is None else status,
            "cold_starts": [{"passed": True}] if cold is None else cold,
            "sessions": [session]}
    return {"schema": "lvz.evaluation.v1", "plan": asdict(plan), "checks": checks, "cases": [case],
            "source": "live_engine"}


class GatePolicyTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = build_root(Path(self._temp.name))

    def test_on_and_off_inventory_are_accepted(self):
        for probes in ("on", "off"):
            with self.subTest(arm=probes):
                arm_root = build_root(Path(self._temp.name) / ("root-" + probes))
                metadata, _, plan, child = prepare_arm(arm_root, probes=probes)
                report = suite_report(metadata, plan, child, on=probes == "on")
                self.assertEqual(experiment._gate_problems(report, metadata, plan,
                                                           metadata["expected_children"]), [])

    def test_gate_mutations_are_rejected(self):
        metadata, _, plan, child = prepare_arm(self.root, probes="on")
        base = suite_report(metadata, plan, child, on=True)
        cases = []
        missing_checks = dict(base)
        missing_checks.pop("checks")
        cases.append(("missing inventory", missing_checks))
        missing_gate = json.loads(json.dumps(base))
        missing_gate["checks"].pop("scenario")
        cases.append(("missing gate", missing_gate))
        extra_gate = json.loads(json.dumps(base))
        extra_gate["checks"]["unexpected"] = {"status": "pass"}
        cases.append(("extra gate", extra_gate))
        untyped = json.loads(json.dumps(base))
        untyped["checks"]["scenario"] = {"pass": True}
        cases.append(("untyped status", untyped))
        unrelated_fail = json.loads(json.dumps(base))
        unrelated_fail["checks"]["session_cleanup"] = {"status": "fail", "detail": "cleanup failed"}
        cases.append(("unrelated failure", unrelated_fail))
        off_recording = json.loads(json.dumps(base))
        off_recording["cases"][0]["status"] = "completed"
        off_recording["cases"][0]["sessions"][0]["infrastructure_failures"] = []
        off_recording["cases"][0]["sessions"][0]["secondary_errors"] = []
        off_recording["checks"]["recording"] = {"status": "pass"}
        off_recording["checks"]["archive_integrity"] = {"status": "pass"}
        cases.append(("on-off mismatch with off shape", off_recording))
        mixed = json.loads(json.dumps(base))
        mixed["cases"][0]["sessions"][0]["secondary_errors"].append(
            {"stage": "packaging", "type": "EvidenceError", "message": "other"})
        cases.append(("mixed secondary error", mixed))
        wrong_child = json.loads(json.dumps(base))
        wrong_child["cases"][0]["expected_run"] = "somewhere/else-s42-c0"
        cases.append(("wrong child", wrong_child))
        bad_cleanup = json.loads(json.dumps(base))
        bad_cleanup["cases"][0]["sessions"][0]["cleanup"]["trace_closed"] = False
        cases.append(("cleanup incomplete", bad_cleanup))
        no_cleanup_pass = json.loads(json.dumps(base))
        no_cleanup_pass["cases"][0]["sessions"][0]["cleanup_passed"] = False
        cases.append(("cleanup not passed", no_cleanup_pass))
        primary = json.loads(json.dumps(base))
        primary["cases"][0]["sessions"][0]["primary_error"] = {"type": "RuntimeError"}
        cases.append(("primary error", primary))
        missing_endpoint = json.loads(json.dumps(base))
        missing_endpoint["cases"][0]["outcome"] = "tick_budget_exhausted"
        cases.append(("endpoint not reached", missing_endpoint))
        low_wave = json.loads(json.dumps(base))
        low_wave["cases"][0]["maximum_wave"] = 1
        cases.append(("wave below frozen stop", low_wave))
        two_colds = json.loads(json.dumps(base))
        two_colds["cases"][0]["cold_starts"].append({"passed": True})
        cases.append(("extra cold start", two_colds))
        bad_plan = json.loads(json.dumps(base))
        bad_plan["plan"]["tick_budget"] = 99999
        cases.append(("plan mismatch", bad_plan))
        wrong_seed = json.loads(json.dumps(base))
        wrong_seed["cases"][0]["seed"] = 7
        cases.append(("wrong seed", wrong_seed))
        for label, report in cases:
            with self.subTest(case=label):
                self.assertTrue(experiment._gate_problems(report, metadata, plan,
                                                          metadata["expected_children"]), label)


class RevalidationSealTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = build_root(Path(self._temp.name))
        self.metadata, self.suite, self.plan, self.child = prepare_arm(self.root, probes="on")
        self.name = self.metadata["run"]

    def fake(self, ok=True):
        return (lambda root, name, metadata, child:
                {"child": child, "ok": ok, "checks": {}, "inputs": {},
                 "problems": [] if ok else ["bad evidence"]})

    def test_strict_seal_and_read_only_verify(self):
        with mock.patch.object(experiment, "_child_revalidation", self.fake(True)):
            document = experiment.revalidate(self.root, self.name)
            self.assertTrue(document["ok"], document["problems"])
            self.assertNotIn("git_head", document["tool"])
            self.assertTrue(all(len(value) == 64 for value in document["tool"]["sources"].values()))
            seal = experiment.seal_revalidation(self.root, self.name)
            self.assertIn("implementation_commit", seal)
            self.assertEqual(seal["implementation_sources"], document["tool"]["sources"])
            revalidation_path = self.suite / "revalidation.json"
            seal_path = self.suite / "revalidation-seal.json"
            before = {path: path.stat().st_mtime_ns for path in (revalidation_path, seal_path)}
            with mock.patch.object(Path, "write_text", side_effect=AssertionError("verify must not write")):
                verified = experiment.verify_revalidation(self.root, self.name)
            self.assertTrue(verified["ok"])
            after = {path: path.stat().st_mtime_ns for path in (revalidation_path, seal_path)}
            self.assertEqual(before, after)

    def test_report_only_commit_does_not_break_verification_and_source_change_does(self):
        with mock.patch.object(experiment, "_child_revalidation", self.fake(True)):
            experiment.revalidate(self.root, self.name)
            experiment.seal_revalidation(self.root, self.name)
            (self.root / "docs").mkdir(exist_ok=True)
            (self.root / "docs" / "report.md").write_text("report-only change", encoding="utf-8")
            self.assertTrue(experiment.verify_revalidation(self.root, self.name)["ok"])
            source = self.root / "src" / "llm_vs_zombies" / "action_compare.py"
            source.write_text(source.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
            with self.assertRaises(experiment.ExperimentError):
                experiment.verify_revalidation(self.root, self.name)

    def test_missing_or_tampered_evidence_is_rejected_without_regeneration(self):
        with mock.patch.object(experiment, "_child_revalidation", self.fake(True)):
            experiment.revalidate(self.root, self.name)
            experiment.seal_revalidation(self.root, self.name)
            revalidation_path = self.suite / "revalidation.json"
            original = revalidation_path.read_bytes()
            revalidation_path.unlink()
            with self.assertRaises(experiment.ExperimentError):
                experiment.verify_revalidation(self.root, self.name)
            self.assertFalse(revalidation_path.exists(), "verify must not regenerate evidence")
            revalidation_path.write_bytes(original)
            tampered = json.loads(original)
            tampered["ok"] = False
            revalidation_path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaises(experiment.ExperimentError):
                experiment.verify_revalidation(self.root, self.name)

    def test_invalid_revalidation_cannot_be_sealed_or_verified(self):
        with mock.patch.object(experiment, "_child_revalidation", self.fake(False)):
            document = experiment.revalidate(self.root, self.name)
            self.assertFalse(document["ok"])
            with self.assertRaises(experiment.ExperimentError):
                experiment.seal_revalidation(self.root, self.name)
            with self.assertRaises(experiment.ExperimentError):
                experiment.verify_revalidation(self.root, self.name)

    def test_normalize_step_preserves_pixel_evidence(self):
        from llm_vs_zombies import action_compare
        left = [{"request": {"protocol": 1, "request_id": "a", "branch": "left", "method": "capture_frame"},
                 "capture_response": {"ok": True, "result": {"capture_ok": True}},
                 "pixels_evidence": {"sha256": "a" * 64, "byte_length": 4}}]
        right = json.loads(json.dumps(left))
        right[0]["request"]["branch"] = "right"
        self.assertTrue(action_compare.compare_action_steps(left, right)["equal"])
        right[0]["pixels_evidence"]["sha256"] = "b" * 64
        self.assertFalse(action_compare.compare_action_steps(left, right)["equal"])


if __name__ == "__main__":
    unittest.main()
