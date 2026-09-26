"""Strict Stage-D revalidation/seal/verify tests (pure computation vs writes)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))

import issue111_lifecycle_experiment as experiment  # noqa: E402
from tests.test_issue111_lifecycle_experiment import build_root  # noqa: E402


def prepare_arm(root: Path):
    plan = root / "experiments" / "plans" / "d.json"
    metadata = experiment.prepare(root, "issue111-d-hosted-v2-on-a", plan, "on",
                                  run_builds=False, probes="on", single_cold=True)
    suite = Path(metadata["suite"])
    suite.mkdir(parents=True, exist_ok=True)
    (suite / "evaluation.json").write_text(json.dumps(
        {"schema": "lvz.evaluation.v1",
         "checks": {"private_launch": {"status": "pass"}, "recording": {"status": "pass"}}}),
        encoding="utf-8")
    (suite / "lifecycle-plan-binding.json").write_text(json.dumps(
        {"schema": "lvz.lifecycle-plan-binding.v1",
         "raw_plan_sha256": metadata["plan"]["sha256"], "plan": metadata["plan"]["path"],
         "mode": "on", "probes": "on", "single_cold": True}), encoding="utf-8")
    return metadata, suite


def ok_record(child):
    return {"child": child, "ok": True, "checks": {}, "inputs": {}, "problems": []}


class RevalidationSealTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = build_root(Path(self._temp.name))
        self.metadata, self.suite = prepare_arm(self.root)
        self.name = "issue111-d-hosted-v2-on-a"

    def test_strict_seal_and_read_only_verify(self):
        with mock.patch.object(experiment, "_child_revalidation",
                               lambda root, name, metadata, child: ok_record(child)):
            document = experiment.revalidate(self.root, self.name)
            self.assertTrue(document["ok"], document["problems"])
            seal = experiment.seal_revalidation(self.root, self.name)
            self.assertTrue(seal["seal_id"])
            revalidation_path = self.suite / "revalidation.json"
            seal_path = self.suite / "revalidation-seal.json"
            before = {path: path.stat().st_mtime_ns for path in (revalidation_path, seal_path)}
            with mock.patch.object(Path, "write_text", side_effect=AssertionError("verify must not write")):
                verified = experiment.verify_revalidation(self.root, self.name)
            self.assertTrue(verified["ok"])
            after = {path: path.stat().st_mtime_ns for path in (revalidation_path, seal_path)}
            self.assertEqual(before, after)

    def test_missing_or_tampered_evidence_is_rejected_without_regeneration(self):
        with mock.patch.object(experiment, "_child_revalidation",
                               lambda root, name, metadata, child: ok_record(child)):
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
            revalidation_path.write_bytes(original)
            seal_path = self.suite / "revalidation-seal.json"
            seal = json.loads(seal_path.read_bytes())
            seal["seal_id"] = "0" * 64
            seal_path.write_text(json.dumps(seal), encoding="utf-8")
            with self.assertRaises(experiment.ExperimentError):
                experiment.verify_revalidation(self.root, self.name)

    def test_invalid_revalidation_cannot_be_sealed_or_verified(self):
        with mock.patch.object(experiment, "_child_revalidation",
                               lambda root, name, metadata, child: {
                                   "child": child, "ok": False, "checks": {}, "inputs": {},
                                   "problems": ["bad evidence"]}):
            document = experiment.revalidate(self.root, self.name)
            self.assertFalse(document["ok"])
            with self.assertRaises(experiment.ExperimentError):
                experiment.seal_revalidation(self.root, self.name)
            with self.assertRaises(experiment.ExperimentError):
                experiment.verify_revalidation(self.root, self.name)

    def test_verify_detects_a_changed_child_check(self):
        result = {"ok": True}
        with mock.patch.object(experiment, "_child_revalidation",
                               lambda root, name, metadata, child:
                               ok_record(child) if result["ok"] else {
                                   "child": child, "ok": False, "checks": {}, "inputs": {},
                                   "problems": ["now failing"]}):
            experiment.revalidate(self.root, self.name)
            experiment.seal_revalidation(self.root, self.name)
            result["ok"] = False
            with self.assertRaises(experiment.ExperimentError):
                experiment.verify_revalidation(self.root, self.name)

    def test_gate_policy_permits_only_retained_old_reader_failures(self):
        permitted = {"recording": {"status": "fail",
                                   "detail": "strict_packaging: native event after recording close"},
                     "archive_integrity": {"status": "fail",
                                           "detail": "native audit target/coverage identity differs from hello"},
                     "full_cycle": {"status": "fail"}, "ten_cold_starts": {"status": "fail"},
                     "build_and_tests": {"status": "unverified"},
                     "engine_replay": {"status": "unverified"}}
        self.assertEqual(experiment._gate_problems({"checks": permitted}), [])
        unrelated = dict(permitted, session_cleanup={"status": "fail", "detail": "cleanup failed"})
        self.assertTrue(any("session_cleanup" in problem
                            for problem in experiment._gate_problems({"checks": unrelated})))
        other_reader = dict(permitted, recording={"status": "fail", "detail": "some other error"})
        self.assertTrue(any("recording" in problem
                            for problem in experiment._gate_problems({"checks": other_reader})))

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
