"""Offline tests for the #111 explicit experiment entry and no-game consumer.

No game is started: ``prepare`` only creates a run directory, ``run`` is tested
with an injected runner, and ``check``/``seal`` operate on a synthetic audit
fixture built by the lifecycle test helpers.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import issue111_lifecycle_experiment as experiment  # noqa: E402


class ExperimentEntryTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        (self.root / "experiments" / "configs").mkdir(parents=True)
        (self.root / "experiments" / "plans").mkdir(parents=True)
        (self.root / "experiments" / "configs" / "jingdian12.json").write_text(
            json.dumps({"scenario": "jingdian12", "scenario_save": "scenario.dat"}), encoding="utf-8")
        (self.root / "scenario.dat").write_bytes(b"save")
        self.plan = self.root / "experiments" / "plans" / "d.json"
        self.plan.write_text(json.dumps({"schema": "lvz.evaluation-plan.v2", "scenario": "jingdian12",
                                         "seeds": [42], "stop_when": {"wave_at_least": 2},
                                         "tick_budget": 2000}), encoding="utf-8")
        (self.root / "dependencies.lock.json").write_text(json.dumps({"files": []}), encoding="utf-8")

    def prepare(self, name="issue111-d-on-a", mode="on"):
        return experiment.prepare(self.root, name, self.plan, mode)

    def test_prepare_binds_mode_and_refuses_reuse(self):
        report = self.prepare()
        run = self.root / "experiments" / "runs" / "issue111-d-on-a"
        self.assertTrue((run / "manifest.json").is_file())
        self.assertEqual(report["env"], {"name": "LVZ_LIFECYCLE_RECORDING", "value": "1"})
        self.assertIn("llm_vs_zombies.evaluation", " ".join(report["launch_command"]))
        with self.assertRaises(experiment.ExperimentError):
            self.prepare()

    def test_run_sets_and_restores_the_explicit_environment(self):
        self.prepare(mode="off", name="issue111-d-off-a")
        captured = {}

        def runner(command):
            captured["command"] = command
            captured["env"] = os.environ.get(experiment.MODE_ENV)
            return 0

        self.assertIsNone(os.environ.get(experiment.MODE_ENV))
        code = experiment.run_experiment(self.root, "issue111-d-off-a", self.plan, "off", runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(captured["env"], "0")
        self.assertIn("llm_vs_zombies.evaluation", " ".join(captured["command"]))
        self.assertIsNone(os.environ.get(experiment.MODE_ENV))

    def test_run_refuses_a_mode_mismatch(self):
        self.prepare(mode="on", name="issue111-d-on-a")
        with self.assertRaises(experiment.ExperimentError):
            experiment.run_experiment(self.root, "issue111-d-on-a", self.plan, "off", runner=lambda command: 0)

    def test_check_and_seal_round_trip(self):
        from tests.test_lifecycle_events import Fixture
        self.prepare(mode="on")
        run = self.root / "experiments" / "runs" / "issue111-d-on-a"
        Fixture(run, run_id="issue111-d-on-a")
        check = experiment.check(self.root, "issue111-d-on-a")
        self.assertTrue(check["ok"])
        self.assertEqual(check["status"], "valid")
        sealed = experiment.seal(self.root, "issue111-d-on-a")
        self.assertTrue(sealed["ok"])
        self.assertTrue((run / "audit" / "evidence-codec.json").is_file())
        report_path = run / "lifecycle-report.json"
        self.assertTrue(report_path.is_file())
        self.assertEqual(json.loads(report_path.read_text(encoding="utf-8"))["lifecycle"]["status"], "valid")
        # Sealing again must verify rather than rewrite.
        again = experiment.seal(self.root, "issue111-d-on-a")
        self.assertTrue(again["ok"])

    def test_seal_refuses_invalid_lifecycle_evidence(self):
        from tests.test_lifecycle_events import Fixture
        self.prepare(mode="on")
        run = self.root / "experiments" / "runs" / "issue111-d-on-a"
        Fixture(run, run_id="issue111-d-on-a")
        (run / "audit" / "lifecycle-close-receipt.jsonl").unlink()
        self.assertFalse(experiment.check(self.root, "issue111-d-on-a")["ok"])
        with self.assertRaises(experiment.ExperimentError):
            experiment.seal(self.root, "issue111-d-on-a")

    def test_disabled_mode_refuses_stray_lifecycle_evidence(self):
        from tests.test_lifecycle_events import Fixture
        self.prepare(mode="off", name="issue111-d-off-a")
        run = self.root / "experiments" / "runs" / "issue111-d-off-a"
        Fixture(run, run_id="issue111-d-off-a")
        with self.assertRaises(experiment.ExperimentError):
            experiment.seal(self.root, "issue111-d-off-a")

    def test_cli_prepare(self):
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "issue111_lifecycle_experiment.py"),
                                 "--root", str(self.root), "prepare", "--name", "issue111-d-on-b",
                                 "--plan", str(self.plan), "--mode", "on"],
                                capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["mode"], "on")


class ConsumerExampleTests(unittest.TestCase):
    def test_consumer_reads_a_lifecycle_stream_without_the_native_probe(self):
        from tests.test_lifecycle_events import Fixture
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp) / "run")
            result = subprocess.run([sys.executable, str(ROOT / "examples" / "issue111_lifecycle_consume.py"),
                                     str(fixture.audit)], capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["records"], 3)
            self.assertEqual(summary["kinds"], {"zombie_initialized": 3})
            self.assertEqual(summary["sequence_domain"], "lvz.measurement.capture-sequence")
            self.assertFalse(summary["first_kill_proven"])

    def test_consumer_exit_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run([sys.executable, str(ROOT / "examples" / "issue111_lifecycle_consume.py"),
                                     str(Path(temp) / "absent")], capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
