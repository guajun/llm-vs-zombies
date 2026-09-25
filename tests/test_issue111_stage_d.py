"""Offline tests for the #111 stage-D freeze plan checker/resource doctor.

No game and no execution: the repository plan is checked for internal and
#99-anchor consistency, negative mutations must fail, and the doctor is
exercised with injected resource facts and a temporary checkout.
"""
import copy
import json
import shutil
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
        self.assertTrue(any("LVZ_LIFECYCLE_PROBES" in problem for problem in problems))

    def test_persistence_switch_must_state_what_it_does_not_gate(self):
        doc = copy.deepcopy(self.doc)
        doc["mode_switch"]["not_gated"] = "nothing"
        doc["mode_switch"].pop("limitations")
        problems = stage_d.check(doc, ROOT)
        self.assertTrue(any("not_gated" in problem for problem in problems))
        self.assertTrue(any("limitations" in problem for problem in problems))

    def test_child_mapping_must_match_the_frozen_plan(self):
        doc = copy.deepcopy(self.doc)
        doc["evaluation_child_mapping"]["per_suite"] = ["<suite>-s42-c0"]
        self.assertTrue(any("per_suite" in problem for problem in stage_d.check(doc, ROOT)))

    def test_comparisons_must_not_claim_instrumentation_off_on(self):
        doc = copy.deepcopy(self.doc)
        doc["comparisons"][2]["name"] = "instrumentation_non_perturbation"
        problems = stage_d.check(doc, ROOT)
        self.assertTrue(any("probe_installation_effect" in problem for problem in problems))
        self.assertTrue(any("instrument-free" in problem or "instrumentation off/on" in problem for problem in problems))


class DocumentedCommandTests(unittest.TestCase):
    def test_documented_dry_commands_parse_through_argparse(self):
        import contextlib
        import io
        import shlex
        import tempfile
        import issue111_lifecycle_experiment as experiment

        tools = {name: __import__(name) for name in
                 ("issue111_stage_d", "issue111_lifecycle_experiment", "issue111_lifecycle_report",
                  "issue111_lifecycle_compare")}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "experiments" / "plans").mkdir(parents=True)
            (root / "experiments" / "configs").mkdir(parents=True)
            shutil.copy2(ROOT / "experiments" / "plans" / "issue99-shovel-control.json",
                         root / "experiments" / "plans" / "issue99-shovel-control.json")
            shutil.copy2(ROOT / "experiments" / "configs" / "jingdian12.json",
                         root / "experiments" / "configs" / "jingdian12.json")
            for command in self.doc_commands():
                if " run " in command:
                    continue  # not a dry command: it starts the real suite
                argv = shlex.split(command)
                self.assertEqual(argv[0], "python")
                tool = tools[Path(argv[1]).stem]
                tail = [str(root) if item == "." else item for item in argv[2:]]
                tail = [str(root / item) if item.startswith("experiments/plans/") else item for item in tail]
                if "--name" in tail:
                    tail[tail.index("--name") + 1] = "issue111-d-dry-run"
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    code = tool.main(tail)
                self.assertIn(code, (0, 1, 2), command)

    @staticmethod
    def doc_commands():
        doc = json.loads((ROOT / "docs" / "issue111-阶段D计划.json").read_bytes())
        return doc["commands"]

    def test_old_doctor_root_ordering_is_rejected(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                stage_d.main(["doctor", "--root", ".", "--plan", "docs/issue111-阶段D计划.json"])


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
