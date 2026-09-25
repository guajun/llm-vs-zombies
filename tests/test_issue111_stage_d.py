"""Offline tests for the #111 stage-D freeze plan checker/resource doctor.

No game and no execution: the repository plan is checked for internal and
#99-anchor consistency, negative mutations must fail, and the doctor is
exercised with injected resource facts and a temporary checkout.
"""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import issue111_stage_d as stage_d  # noqa: E402


class PlanCheckTests(unittest.TestCase):
    def setUp(self):
        self.doc = stage_d.load()

    def test_repository_plan_passes_and_stays_frozen(self):
        self.assertEqual(stage_d.check(self.doc, ROOT), [])
        self.assertEqual(self.doc["status"], "frozen_pending_maintainer_review")
        self.assertEqual([run["mode"] for run in self.doc["runs"]], ["off", "off", "on", "on"])

    def test_tick_cap_must_match_frozen_issue99(self):
        doc = copy.deepcopy(self.doc)
        doc["resource_limits"]["tick_cap"] = 4000
        self.assertTrue(any("tick_cap" in problem for problem in stage_d.check(doc, ROOT)))

    def test_stop_rule_must_match_frozen_issue99(self):
        doc = copy.deepcopy(self.doc)
        doc["based_on"]["stop_when"] = {"wave_at_least": 3}
        self.assertTrue(any("stop_when" in problem for problem in stage_d.check(doc, ROOT)))

    def test_duplicate_or_unnamespaced_runs_fail(self):
        doc = copy.deepcopy(self.doc)
        doc["runs"][1]["name"] = doc["runs"][0]["name"]
        doc["runs"][2]["name"] = "plain-run"
        problems = stage_d.check(doc, ROOT)
        self.assertTrue(any("duplicate run name" in problem for problem in problems))
        self.assertTrue(any("issue111-d-" in problem for problem in problems))

    def test_comparison_must_reference_declared_runs(self):
        doc = copy.deepcopy(self.doc)
        doc["comparisons"][0]["right"] = "missing-run"
        self.assertTrue(any("not a declared run" in problem for problem in stage_d.check(doc, ROOT)))

    def test_status_and_mode_switch_are_locked(self):
        doc = copy.deepcopy(self.doc)
        doc["status"] = "executed"
        doc["mode_switch"]["env_var"] = "OTHER"
        problems = stage_d.check(doc, ROOT)
        self.assertTrue(any("frozen_pending_maintainer_review" in problem for problem in problems))
        self.assertTrue(any("LVZ_LIFECYCLE_RECORDING" in problem for problem in problems))


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.doc = stage_d.load()
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        (self.root / "build").mkdir()
        (self.root / "build" / "recorder.dll").write_bytes(b"dll")
        frozen = {"schema": "lvz.evaluation-plan.v2",
                  "tick_budget": self.doc["based_on"]["tick_budget"],
                  "stop_when": self.doc["based_on"]["stop_when"],
                  "scenario": self.doc["based_on"]["scenario"],
                  "seeds": [self.doc["based_on"]["seed"]]}
        plan_path = self.root / self.doc["based_on"]["plan"]
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(json.dumps(frozen), encoding="utf-8")

    def doctor(self, **kwargs):
        kwargs.setdefault("disk_free", 1 << 40)
        kwargs.setdefault("game_processes", [])
        kwargs.setdefault("require_game", False)
        return stage_d.doctor(self.doc, self.root, **kwargs)

    def test_ready_checkout_passes(self):
        report = self.doctor()
        self.assertEqual(report["problems"], [])
        self.assertTrue(report["ok"])
        self.assertTrue(report["recorder"]["present"])

    def test_missing_recorder_and_existing_runs_fail(self):
        (self.root / "build" / "recorder.dll").unlink()
        (self.root / "experiments" / "runs" / self.doc["runs"][0]["name"]).mkdir(parents=True)
        problems = self.doctor()["problems"]
        self.assertTrue(any("recorder.dll is missing" in problem for problem in problems))
        self.assertTrue(any("already exist" in problem for problem in problems))

    def test_low_disk_and_running_game_fail(self):
        problems = self.doctor(disk_free=1, game_processes=["PlantsVsZombies.exe"])["problems"]
        self.assertTrue(any("below the plan reserve" in problem for problem in problems))
        self.assertTrue(any("already running" in problem for problem in problems))

    def test_require_game_reports_missing_locked_files(self):
        lock = {"files": [{"path": "game/original/PlantsVsZombies.exe", "sha256": "0" * 64}]}
        (self.root / "dependencies.lock.json").write_text(json.dumps(lock), encoding="utf-8")
        problems = self.doctor(require_game=True)["problems"]
        self.assertTrue(any("locked game file is missing" in problem for problem in problems))


if __name__ == "__main__":
    unittest.main()
