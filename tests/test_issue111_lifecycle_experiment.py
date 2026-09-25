"""Offline tests for the #111 experiment entry, check and seal.

The game-launch boundary is the only thing mocked: ``run`` exercises the real
``evaluation.run_suite`` backend (output creation, child identity checks,
failure accounting, report writing). Completion/mode/audit proofs run against
synthetic completed suites built with the repository's strict audit fixtures.
"""
import contextlib
import copy
import hashlib
import io
import json
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import issue111_lifecycle_experiment as experiment  # noqa: E402
from llm_vs_zombies import evaluation, lifecycle_events  # noqa: E402


def build_root(base: Path) -> Path:
    root = base / "checkout"
    (root / "experiments" / "configs").mkdir(parents=True)
    (root / "experiments" / "plans").mkdir(parents=True)
    (root / "experiments" / "runs").mkdir(parents=True)
    (root / "experiments" / "configs" / "jingdian12.json").write_text(
        json.dumps({"scenario": "jingdian12", "scenario_save": "scenario.dat"}), encoding="utf-8")
    (root / "scenario.dat").write_bytes(b"save")
    plan = root / "experiments" / "plans" / "d.json"
    plan.write_text(json.dumps({
        "schema": "lvz.evaluation-plan.v2", "scenario": "jingdian12",
        "seeds": [42], "stop_when": {"wave_at_least": 2},
        "tick_budget": 2000, "cold_starts": 1,
        "intervention": {"schema": "lvz.intervention.v1", "id": "control-01",
                          "at": {"epoch": 3, "tick": 101, "revision": 0},
                          "require": {"wave": 0, "refresh_countdown": 498, "zombies": 0, "plants": 52},
                          "action": None, "advance_ticks": 0},
    }), encoding="utf-8")
    (root / "dependencies.lock.json").write_text(json.dumps({"files": []}), encoding="utf-8")
    return root


def load_plan(root: Path):
    return evaluation.Plan.load(root / "experiments" / "plans" / "d.json")


def add_audit_mode(audit: Path, run_id: str, *, enabled: bool):
    from tests.test_audit_compare import animation_audit
    from tests.test_lifecycle_events import capability, receipt_for
    animation_audit(audit)
    manifest = json.loads((audit / "manifest.json").read_text(encoding="utf-8"))
    if enabled:
        manifest["lifecycle_recording"] = capability()
    else:
        manifest["lifecycle_recording"] = {"mode": lifecycle_events.MODE, "enabled": False}
    manifest_bytes = (json.dumps(manifest, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    (audit / "manifest.json").write_bytes(manifest_bytes)
    if enabled:
        events_bytes = b""
        (audit / lifecycle_events.EVENTS_FILE).write_bytes(events_bytes)
        receipt = receipt_for(events_bytes, manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                              run_id=run_id)
        (audit / lifecycle_events.RECEIPT_FILE).write_text(
            json.dumps(receipt, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")


def write_run_manifest(directory: Path, child: str):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(json.dumps(
        {"schema_version": 1, "run_id": child, "status": "recording",
         "implementation": {"recorder_sha256": "b" * 64}}), encoding="utf-8")


class ExperimentEntryTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = build_root(Path(self._temp.name))

    def plan_path(self) -> Path:
        return self.root / "experiments" / "plans" / "d.json"

    def prepare(self, name="issue111-d-on-a", mode="on", *, run_builds=False):
        return experiment.prepare(self.root, name, self.plan_path(), mode, run_builds=run_builds)

    # --- prepare ---------------------------------------------------------

    def test_prepare_locks_identity_without_creating_the_suite(self):
        metadata = self.prepare()
        suite = self.root / "experiments" / "runs" / "issue111-d-on-a"
        self.assertFalse(suite.exists())
        self.assertTrue(experiment.mode_path(self.root, "issue111-d-on-a").is_file())
        self.assertEqual(metadata["mode"], "on")
        self.assertEqual(metadata["env"], {"name": "LVZ_LIFECYCLE_RECORDING", "value": "1"})
        self.assertEqual(metadata["expected_children"],
                         ["issue111-d-on-a-s42-c0", "issue111-d-on-a-s42-c1", "issue111-d-on-a-s42-recovery"])
        self.assertEqual(metadata["plan"]["sha256"], hashlib.sha256(self.plan_path().read_bytes()).hexdigest())
        self.assertFalse(metadata["run_builds"])
        self.assertIn("--skip-build", metadata["launch_command"])
        with self.assertRaises(experiment.ExperimentError):
            self.prepare()

    def test_prepare_defaults_to_running_builds_and_refuses_existing_children(self):
        metadata = self.prepare(name="issue111-d-off-a", mode="off", run_builds=True)
        self.assertTrue(metadata["run_builds"])
        self.assertNotIn("--skip-build", metadata["launch_command"])
        (self.root / "experiments" / "runs" / "issue111-d-on-a-s42-c0").mkdir()
        with self.assertRaises(experiment.ExperimentError):
            self.prepare(name="issue111-d-on-a", mode="on")

    # --- run against the real backend ------------------------------------

    def test_run_creates_suite_through_real_backend_and_detects_launch_failure(self):
        self.prepare()
        with mock.patch.object(evaluation, "live_session", side_effect=RuntimeError("mocked game launch")):
            report = experiment.run_experiment(self.root, "issue111-d-on-a",
                                               disk_free=1 << 40, game_processes=[])
        suite = self.root / "experiments" / "runs" / "issue111-d-on-a"
        self.assertTrue(suite.is_dir())
        self.assertTrue((suite / "evaluation.json").is_file())
        self.assertEqual(report["statistics"]["failed_cases"], 1)
        self.assertEqual(report["statistics"]["completed_cases"], 0)
        checked = experiment.check(self.root, "issue111-d-on-a")
        self.assertFalse(checked["ok"])
        self.assertTrue(any("failed cases" in problem or "status is" in problem
                            for problem in checked["problems"]), checked["problems"])
        with self.assertRaises(experiment.ExperimentError):
            experiment.seal(self.root, "issue111-d-on-a")

    def test_run_refuses_changed_plan(self):
        self.prepare(name="issue111-d-off-a", mode="off")
        self.plan_path().write_text(self.plan_path().read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaises(experiment.ExperimentError):
            experiment.run_experiment(self.root, "issue111-d-off-a")

    def test_run_sets_and_restores_the_mode_environment(self):
        metadata = self.prepare(name="issue111-d-off-a", mode="off")
        captured = {}

        class FakeSuite:
            def __call__(self):
                captured["env"] = os_environ = __import__("os").environ.get(experiment.MODE_ENV)
                return {"statistics": {"failed_cases": 0, "completed_cases": 1}}

        self.assertIsNone(__import__("os").environ.get(experiment.MODE_ENV))
        result = experiment.run_experiment(self.root, "issue111-d-off-a", suite_runner=FakeSuite(),
                                           disk_free=1 << 40, game_processes=[])
        self.assertEqual(result["statistics"]["failed_cases"], 0)
        self.assertEqual(captured["env"], "0")
        self.assertIsNone(__import__("os").environ.get(experiment.MODE_ENV))

    # --- completions and mode proofs -------------------------------------

    def _complete_suite(self, name="issue111-d-on-a", mode="on", *, seeds=(42,), cold_starts=1):
        metadata = self.prepare(name=name, mode=mode)
        suite = self.root / "experiments" / "runs" / name
        suite.mkdir(parents=True)
        plan = load_plan(self.root)
        cases = []
        for seed in seeds:
            cases.append({
                "seed": seed, "status": "completed",
                "cold_starts": [
                    {"run": f"{name}-s{seed}-c0", "kind": "source", "passed": True},
                    {"run": f"{name}-s{seed}-c1", "kind": "replay", "passed": True},
                ],
                "sessions": [{"child": f"{name}-s{seed}-c0"}],
            })
        suite_report = {"schema": "lvz.evaluation.v1", "source": "live_engine",
                        "plan": asdict(plan), "cases": cases,
                        "statistics": {"seed_cases": len(seeds), "completed_cases": len(seeds), "failed_cases": 0}}
        (suite / "evaluation.json").write_text(json.dumps(suite_report), encoding="utf-8")
        (suite / "plan.json").write_text(json.dumps(asdict(plan)), encoding="utf-8")
        for child in metadata["expected_children"]:
            directory = self.root / "experiments" / "runs" / child
            write_run_manifest(directory, child)
            add_audit_mode(directory / "audit", child, enabled=(mode == "on"))
        return metadata, suite

    def test_check_and_seal_on_complete_suite(self):
        metadata, suite = self._complete_suite()
        report = experiment.check(self.root, "issue111-d-on-a")
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["build"], "b" * 64)
        self.assertEqual(len(report["children"]), 3)
        seal = experiment.seal(self.root, "issue111-d-on-a")
        self.assertEqual(seal["schema"], experiment.SEAL_SCHEMA)
        self.assertEqual(seal["mode"], "on")
        self.assertTrue(seal["seal_id"])
        self.assertTrue(experiment.seal_path(self.root, "issue111-d-on-a").is_file())
        self.assertTrue(all(child["codec_receipt_sha256"] for child in seal["children"]))
        for child in metadata["expected_children"]:
            self.assertTrue((self.root / "experiments" / "runs" / child / "audit"
                             / "evidence-codec.json").is_file())

    def test_off_mode_completion_is_verified_not_assumed(self):
        self._complete_suite(name="issue111-d-off-a", mode="off")
        report = experiment.check(self.root, "issue111-d-off-a")
        self.assertTrue(report["ok"], report["problems"])
        # Flipping the capability must fail the mode proof.
        child = self.root / "experiments" / "runs" / "issue111-d-off-a-s42-c0"
        manifest = json.loads((child / "audit" / "manifest.json").read_text(encoding="utf-8"))
        manifest["lifecycle_recording"] = {"mode": lifecycle_events.MODE, "enabled": True}
        (child / "audit" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        report = experiment.check(self.root, "issue111-d-off-a")
        self.assertFalse(report["ok"])

    def test_check_rejects_incomplete_failed_and_empty_suites(self):
        metadata = self.prepare(name="issue111-d-on-a", mode="on")
        empty = experiment.check(self.root, "issue111-d-on-a")
        self.assertFalse(empty["ok"])
        self.assertTrue(any("never created" in problem for problem in empty["problems"]))
        self._complete_suite(name="issue111-d-on-b", mode="on")
        suite = self.root / "experiments" / "runs" / "issue111-d-on-b"
        report = json.loads((suite / "evaluation.json").read_text(encoding="utf-8"))
        report["statistics"]["failed_cases"] = 1
        report["cases"][0]["status"] = "failed"
        (suite / "evaluation.json").write_text(json.dumps(report), encoding="utf-8")
        checked = experiment.check(self.root, "issue111-d-on-b")
        self.assertFalse(checked["ok"])

    def test_check_rejects_missing_recording_closed_and_corrupt_audit(self):
        metadata, _ = self._complete_suite(name="issue111-d-on-c")
        child = self.root / "experiments" / "runs" / "issue111-d-on-c-s42-c0"
        events = (child / "audit" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        (child / "audit" / "events.jsonl").write_text("\n".join(events[:-1]) + "\n", encoding="utf-8")
        report = experiment.check(self.root, "issue111-d-on-c")
        self.assertFalse(report["ok"])
        self.assertTrue(any("strict audit verification failed" in problem for problem in report["problems"]))
        metadata, _ = self._complete_suite(name="issue111-d-on-d")
        child = self.root / "experiments" / "runs" / "issue111-d-on-d-s42-c1"
        (child / "audit" / "checksums.jsonl").write_bytes(b"{not json}\n")
        report = experiment.check(self.root, "issue111-d-on-d")
        self.assertFalse(report["ok"])

    def test_check_rejects_missing_lifecycle_evidence_in_on_mode(self):
        self._complete_suite(name="issue111-d-on-e")
        child = self.root / "experiments" / "runs" / "issue111-d-on-e-s42-c0"
        (child / "audit" / lifecycle_events.RECEIPT_FILE).unlink()
        report = experiment.check(self.root, "issue111-d-on-e")
        self.assertFalse(report["ok"])

    def test_seal_refuses_changed_plan_after_completion(self):
        self._complete_suite(name="issue111-d-on-f")
        self.plan_path().write_text(self.plan_path().read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaises(experiment.ExperimentError):
            experiment.seal(self.root, "issue111-d-on-f")

    def test_run_enforces_the_resource_contract_before_the_backend(self):
        self.prepare(name="issue111-d-off-a", mode="off")
        called = {"backend": False}

        def runner():
            called["backend"] = True
            return {"statistics": {"failed_cases": 0, "completed_cases": 1}}

        with self.assertRaises(experiment.ExperimentError):
            experiment.run_experiment(self.root, "issue111-d-off-a", suite_runner=runner,
                                      disk_free=1, game_processes=[])
        self.assertFalse(called["backend"])
        with self.assertRaises(experiment.ExperimentError):
            experiment.run_experiment(self.root, "issue111-d-off-a", suite_runner=runner,
                                      disk_free=1 << 40, game_processes=["PlantsVsZombies.exe"])
        self.assertFalse(called["backend"])
        experiment.run_experiment(self.root, "issue111-d-off-a", suite_runner=runner,
                                  disk_free=1 << 40, game_processes=[])
        self.assertTrue(called["backend"])

    def test_child_manifest_must_be_well_formed_and_build_bound(self):
        self._complete_suite(name="issue111-d-on-g")
        child = self.root / "experiments" / "runs" / "issue111-d-on-g-s42-c0"
        manifest = json.loads((child / "manifest.json").read_text(encoding="utf-8"))
        manifest["implementation"] = {}
        (child / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        report = experiment.check(self.root, "issue111-d-on-g")
        self.assertFalse(report["ok"])
        self.assertTrue(any("recorder_sha256" in problem for problem in report["problems"]))

    def test_capability_build_must_match_the_run_manifest(self):
        self._complete_suite(name="issue111-d-on-h")
        child = self.root / "experiments" / "runs" / "issue111-d-on-h-s42-c1"
        manifest = json.loads((child / "audit" / "manifest.json").read_text(encoding="utf-8"))
        manifest["lifecycle_recording"]["build"]["sha256"] = "c" * 64
        (child / "audit" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        report = experiment.check(self.root, "issue111-d-on-h")
        self.assertFalse(report["ok"])

    def test_seal_is_idempotent_and_verifiable_but_never_silently_replaced(self):
        self._complete_suite(name="issue111-d-on-i")
        first = experiment.seal(self.root, "issue111-d-on-i")
        again = experiment.seal(self.root, "issue111-d-on-i")
        self.assertEqual(first["seal_id"], again["seal_id"])
        self.assertTrue(experiment.verify_seal(self.root, "issue111-d-on-i")["ok"])
        suite = self.root / "experiments" / "runs" / "issue111-d-on-i"
        report = json.loads((suite / "evaluation.json").read_text(encoding="utf-8"))
        report["created_at"] = "changed-after-seal"
        (suite / "evaluation.json").write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaises(experiment.ExperimentError):
            experiment.seal(self.root, "issue111-d-on-i")
        with self.assertRaises(experiment.ExperimentError):
            experiment.verify_seal(self.root, "issue111-d-on-i")

    # --- CLI --------------------------------------------------------------

    def test_cli_prepare_and_invalid_root_order(self):
        with contextlib.redirect_stdout(io.StringIO()):
            result = experiment.main(["--root", str(self.root), "prepare", "--name", "issue111-d-on-z",
                                      "--plan", str(self.plan_path()), "--mode", "on", "--skip-build"])
        self.assertEqual(result, 0)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                experiment.main(["prepare", "--root", str(self.root), "--name", "x",
                                 "--plan", str(self.plan_path()), "--mode", "on"])


if __name__ == "__main__":
    unittest.main()


class ConsumerExampleTests(unittest.TestCase):
    def test_consumer_reads_a_lifecycle_stream_without_the_native_probe(self):
        from tests.test_lifecycle_events import Fixture
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp) / "run")
            result = __import__("subprocess").run(
                [sys.executable, str(ROOT / "examples" / "issue111_lifecycle_consume.py"), str(fixture.audit)],
                capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["records"], 3)
            self.assertEqual(summary["kinds"], {"zombie_initialized": 3})
            self.assertEqual(summary["sequence_domain"], "lvz.measurement.capture-sequence")
            self.assertFalse(summary["first_kill_proven"])

    def test_consumer_exit_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            result = __import__("subprocess").run(
                [sys.executable, str(ROOT / "examples" / "issue111_lifecycle_consume.py"),
                 str(Path(temp) / "absent")], capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 2)
