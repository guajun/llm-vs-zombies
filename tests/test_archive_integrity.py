import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import zipfile

from llm_vs_zombies.cli import ROOT, create_run
from llm_vs_zombies.records import (EventWriter, archive_policy, finish, read_json,
                                   record_initial_state, sha256, validate, write_json)
from llm_vs_zombies.session import SessionTrace


class ArchiveIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = self.root / "experiment.json"
        write_json(self.config, {"state_interval_ticks": 10})

    def tearDown(self):
        self.temporary.cleanup()

    def runfile(self, name="run"):
        run = create_run(self.root, self.config, name)
        with EventWriter(run) as writer:
            writer.emit("state", 0, 0, {"plants": []})
            writer.emit("segment_end", 0, 0, {})
        return run

    def test_audit_trajectory_and_all_root_metadata_are_sealed(self):
        names = ("audit/checksums.jsonl", "audit/reanimation-handles.jsonl", "audit/state-deltas.jsonl",
                 "audit/events.jsonl", "audit/manifest.json", "trajectory/trajectory.json",
                 "trajectory/audit/checksums.jsonl", "launcher.json", "evaluation-cleanup.json",
                 "evaluation-windows.json", "baseline-result.json", "recovery-probe.json",
                 "single-step.json", "pause-probe.json", "replay-initial.json", "initial-comparison.json",
                 "initialization-recipe.json", "experiment-end.json", "future-evidence.json")
        for index, name in enumerate(names):
            with self.subTest(name=name):
                run = self.runfile(f"run-{index}")
                evidence = run / name
                evidence.parent.mkdir(parents=True, exist_ok=True)
                evidence.write_text('{"value":1}\n', encoding="utf-8")
                sealed = finish(run, "test")
                self.assertIn(name, sealed["checksums"])
                self.assertIn("summary.json", sealed["checksums"])
                self.assertEqual(sealed["archive_policy"], archive_policy())
                evidence.write_text('{"value":2}\n', encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "checksum"):
                    validate(run)

    def test_added_and_deleted_evidence_are_detected_but_exports_and_sandbox_are_excluded(self):
        run = self.runfile()
        for name in ("sandbox/game/PlantsVsZombies.exe", "sandbox/private.json", "exports/review.html"):
            path = run / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"private or derived")
        sealed = finish(run, "test")
        self.assertFalse(any(name.startswith(("sandbox/", "exports/")) for name in sealed["checksums"]))
        (run / "exports/review.html").write_text("new derived export")
        (run / "sandbox/private.json").unlink()
        validate(run)
        added = run / "audit/late.jsonl"
        added.write_text("late evidence\n")
        with self.assertRaisesRegex(ValueError, "inventory.*added"):
            validate(run)
        added.unlink()
        (run / "config.json").unlink()
        with self.assertRaisesRegex(ValueError, "inventory.*missing"):
            validate(run)

    def test_open_and_stale_decision_trace_locks_prevent_sealing(self):
        run = self.runfile()
        original = (run / "manifest.json").read_bytes()
        with SessionTrace(run / "decisions/nested/session.jsonl") as trace:
            trace.emit("decision", {"action": "wait"})
            with self.assertRaisesRegex(ValueError, "decision trace.*lock"):
                finish(run, "test")
            self.assertEqual((run / "manifest.json").read_bytes(), original)
        stale = run / "decisions/crashed.jsonl.lock"
        stale.touch()
        with self.assertRaisesRegex(ValueError, "lock"):
            finish(run, "test")
        stale.unlink()
        finish(run, "test")
        validate(run)

    def test_writer_starting_during_hash_does_not_produce_a_finalized_manifest(self):
        run = self.runfile()
        original_hash = sha256
        def racing_hash(path):
            digest = original_hash(path)
            (run / "decisions/late.jsonl.lock").touch()
            return digest
        with patch("llm_vs_zombies.records.sha256", side_effect=racing_hash):
            with self.assertRaisesRegex(ValueError, "lock"):
                finish(run, "test")
        self.assertEqual(read_json(run / "manifest.json")["status"], "recording")

    def test_legacy_finalized_manifest_is_read_only_and_remains_compatible(self):
        run = self.runfile()
        manifest = read_json(run / "manifest.json")
        manifest.update(status="finalized", checksums={"events.jsonl": sha256(run / "events.jsonl")})
        write_json(run / "manifest.json", manifest)
        original = (run / "manifest.json").read_bytes()
        self.assertEqual(validate(run)["status"], "finalized")
        with self.assertRaisesRegex(ValueError, "already finalized"):
            finish(run, "changed")
        self.assertEqual((run / "manifest.json").read_bytes(), original)
        self.assertFalse((run / "summary.json").exists())

    def test_symlink_cannot_pull_private_game_into_evidence(self):
        run = self.runfile()
        external = self.root / "private-game.exe"
        external.write_bytes(b"not evidence")
        linked = run / "audit/game-link.exe"
        try:
            linked.symlink_to(external)
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {error}")
        with self.assertRaisesRegex(ValueError, "linked"):
            finish(run, "test")

    def test_actual_initial_state_and_reported_capabilities_replace_placeholder(self):
        run = self.runfile()
        version = {"epoch": 1, "tick": 0, "revision": 3}
        write_json(run / "observations/initial.json", {"version": version, "scene": 3})
        write_json(run / "replay-initial.json", {"observation": {"version": version}, "state": {"rng": {}}})
        hello = {"capabilities": {"audit_snapshot": True, "commit": True},
                 "game": {"target": "pvz", "coverage": {"complete_game_state": False, "exact_initializer_exit": True}}}
        recorded = record_initial_state(run, observation_path=Path("observations/initial.json"), hello=hello,
            scenario_verified=True, audit_snapshot_path=run / "replay-initial.json")
        self.assertTrue(recorded["initial_state"]["captured"])
        self.assertTrue(recorded["initial_state"]["audit_snapshot_captured"])
        self.assertEqual(recorded["initial_state"]["observation"]["sha256"], sha256(run / "observations/initial.json"))
        self.assertEqual(recorded["capabilities"]["runtime_reported"], hello["capabilities"])
        self.assertFalse(recorded["capabilities"]["original_engine_replay_verified"])
        self.assertNotIn("exact_spawn_hook", recorded["capabilities"])
        finish(run, "test")
        saved = (run / "manifest.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "finalized"):
            record_initial_state(run, observation_path=run / "observations/initial.json", hello=hello, scenario_verified=True)
        self.assertEqual((run / "manifest.json").read_bytes(), saved)

    def test_initial_metadata_rejects_snapshot_from_different_boundary(self):
        run = self.runfile()
        write_json(run / "observations/initial.json", {"version": {"epoch": 1, "tick": 0, "revision": 2}})
        write_json(run / "replay-initial.json", {"version": {"epoch": 1, "tick": 1, "revision": 2}, "state": {}})
        saved = (run / "manifest.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "boundary"):
            record_initial_state(run, observation_path=run / "observations/initial.json", hello={"capabilities": {}},
                                 scenario_verified=True, audit_snapshot_path=run / "replay-initial.json")
        self.assertEqual((run / "manifest.json").read_bytes(), saved)

    def test_source_archive_contains_actual_cmake_sources_tests_and_launcher_scripts(self):
        # Copy open source inputs only, then test the real archive selection.
        for name in ("runtime", "logger", "determinism", "recording", "launcher", "tests", "tools",
                     "avz/framework/inc", "avz/framework/src"):
            shutil.copytree(ROOT / name, self.root / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("CMakeLists.txt", "pyproject.toml", "dependencies.lock.json", ".gitmodules", "LICENSE", "avz/framework/LICENSE"):
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, destination)
        (self.root / "launcher/private.exe").write_bytes(b"generated, excluded")
        run = self.runfile()
        source = run / "inputs/implementation.zip"
        self.assertEqual(read_json(run / "manifest.json")["implementation"]["source_archive_sha256"], sha256(source))
        extracted = self.root / "extracted"
        with zipfile.ZipFile(source) as archive:
            names = set(archive.namelist())
            cmake = archive.read("CMakeLists.txt").decode("utf-8")
            required = set(re.findall(r"(?:tests|logger|runtime|determinism|recording)/[\w./-]+\.(?:cpp|hpp|cmake)", cmake))
            self.assertTrue(required <= names, required - names)
            for name in ("launcher/build.ps1", "tools/build-avz.ps1", "tests/test_records.py",
                         "runtime/vendor/nlohmann/json.hpp", "avz/framework/inc/libavz.h",
                         "avz/framework/src/avz_script.cpp", "avz/framework/LICENSE", "ARCHIVE-REBUILD.md"):
                self.assertIn(name, names)
            self.assertNotIn("launcher/private.exe", names)
            self.assertFalse(any(name.startswith(("game/", "third_party/", "build/")) for name in names))
            archive.extractall(extracted)
        toolchain = ROOT / "third_party/llvm-mingw-20260908-ucrt-x86_64/bin"
        if shutil.which("cmake") and shutil.which("ninja") and (toolchain / "i686-w64-mingw32-clang++.exe").exists():
            result = subprocess.run(["cmake", "-S", str(extracted), "-B", str(extracted / "build/cmake"),
                "-G", "Ninja", "-DCMAKE_SYSTEM_NAME=Windows",
                f"-DCMAKE_C_COMPILER={toolchain / 'i686-w64-mingw32-clang.exe'}",
                f"-DCMAKE_CXX_COMPILER={toolchain / 'i686-w64-mingw32-clang++.exe'}"],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
