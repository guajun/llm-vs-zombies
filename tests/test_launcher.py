import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from llm_vs_zombies.launcher import (DEFAULT_CARDS, REFERENCE_LAYOUT, REQUIRED_INPUTS, branch_identity, prepare,
                                     start, stop, synthetic_profile, verify_branch_scope, verify_inputs, verify_scenario)


class StubClient:
    """Minimal connect() stand-in for the launcher's pre-initialize window."""

    def __init__(self, hello):
        self.hello_result = hello
        self.observation = {"game_ui": 0, "loading_complete": False,
                            "version": {"epoch": 1, "tick": 0, "revision": 0}}

    def observe(self):
        return dict(self.observation)

    def request(self, method, params=None, expect=None):
        raise AssertionError(f"stub client received an unexpected request: {method}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def lock_inputs(root: Path) -> None:
    """Write the six locked local prerequisites and their hash manifest."""
    entries = []
    for name in REQUIRED_INPUTS:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        content = name.encode()
        path.write_bytes(content)
        entries.append({"path": name, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)})
    (root / "dependencies.lock.json").write_text(json.dumps({"files": entries}))


class LauncherTests(unittest.TestCase):
    def test_profile_has_one_known_user_and_ten_slots(self):
        files = synthetic_profile()
        users = files["users.dat"]
        self.assertEqual(struct.unpack_from("<IH", users), (14, 1))
        length, = struct.unpack_from("<H", users, 6)
        self.assertEqual(users[8:8+length], b"Experiment")
        self.assertEqual(struct.unpack_from("<II", users, 8+length), (1, 1))
        values = struct.unpack("<205I", files["user1.dat"])
        self.assertEqual(values[:4], (12, 50, 0, 1))
        self.assertEqual(values[125], 4)
        self.assertEqual(synthetic_profile(), files)

    def inputs(self, root):
        lock_inputs(root)

    def test_locked_files_fail_closed_on_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.inputs(root)
            self.assertEqual(len(verify_inputs(root)), len(REQUIRED_INPUTS))
            (root / REQUIRED_INPUTS[0]).write_bytes(b"different")
            with self.assertRaisesRegex(ValueError, "hash/size mismatch"):
                verify_inputs(root)

    def test_missing_engine_is_a_clear_local_prerequisite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.inputs(root)
            (root / "game/local-engine/PlantsVsZombies.exe").unlink()
            with self.assertRaisesRegex(FileNotFoundError, "local prerequisite"):
                verify_inputs(root)

    def test_prepare_uses_private_resources_without_mutating_original(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.inputs(root)
            (root / "game/original/properties").mkdir()
            (root / "game/original/properties/partner.xml").write_text("preserved")
            for name in ("recorder.dll", "launcher/lvz-launcher.exe", "launcher/lvz-bootstrap.dll"):
                path = root / "build" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(name.encode())
            run = root / "experiments/runs/test"
            run.mkdir(parents=True)
            (run / "manifest.json").write_text(json.dumps({"status": "recording", "configuration": {},
                "implementation": {"recorder_sha256": hashlib.sha256(b"recorder.dll").hexdigest()}}))
            original = (root / REQUIRED_INPUTS[0]).read_bytes()
            with patch("llm_vs_zombies.launcher._native", side_effect=AssertionError("preparation must not start a process")):
                state = prepare(root, run)
            self.assertEqual(state["status"], "prepared")
            self.assertEqual((root / REQUIRED_INPUTS[0]).read_bytes(), original)
            self.assertEqual(Path(state["engine"]).read_bytes(), (root / "game/local-engine/PlantsVsZombies.exe").read_bytes())
            self.assertTrue((run / "sandbox/appdata/PopCap Games/PlantsVsZombies/userdata/game1_13.dat").is_file())
            with self.assertRaises(FileExistsError):
                prepare(root, run)

    def test_scenario_requires_actual_scene_layout_and_selected_cards(self):
        observation = {"game_ui": 3, "scene": 3,
                       "plants": [{"type": t, "row": row, "col": col} for t, positions in REFERENCE_LAYOUT.items() for row, col in positions],
                       "seeds": [{"type": t} if t < 49 else {"type": 48, "imitator_type": t-49} for t in DEFAULT_CARDS]}
        verify_scenario(observation)
        observation["scene"] = 2
        with self.assertRaisesRegex(ValueError, "Scene 3"):
            verify_scenario(observation)
        observation["scene"] = 3
        observation["plants"].pop()
        with self.assertRaisesRegex(ValueError, "layout mismatch"):
            verify_scenario(observation)

    def test_stop_recovers_pre_resume_receipt_after_client_interruption(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            sandbox = run / "sandbox"
            sandbox.mkdir()
            state = {"sandbox": str(sandbox), "engine": str(sandbox / "game.exe"), "status": "prepared"}
            (run / "launcher.json").write_text(json.dumps(state))
            (sandbox / "native-receipt.json").write_text(json.dumps({"pid": 1234, "creation_time": 987654, "isolation_ready": True}))
            with patch("llm_vs_zombies.launcher._native", return_value={"stopped": True}) as native:
                self.assertEqual(stop(run), {"stopped": True})
            arguments = native.call_args.args
            self.assertEqual(arguments[1:], ("stop", 1234, state["engine"], 987654, "unused"))
            self.assertEqual(json.loads((run / "launcher.json").read_text())["status"], "stopped")


class BranchIdentityTests(unittest.TestCase):
    """The runtime scope of a run and its evidence-tree branch name are one string."""

    def prepared(self, root: Path, name: str, **kwargs) -> dict:
        lock_inputs(root)
        (root / "game/original/properties").mkdir(parents=True)
        for relative in ("recorder.dll", "launcher/lvz-launcher.exe", "launcher/lvz-bootstrap.dll"):
            path = root / "build" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(relative.encode())
        run = root / "experiments/runs" / name
        run.mkdir(parents=True)
        (run / "manifest.json").write_text(json.dumps({"status": "recording", "configuration": {},
            "implementation": {"recorder_sha256": hashlib.sha256(b"recorder.dll").hexdigest()}}))
        return prepare(root, run, **kwargs)

    def test_default_branch_is_the_run_directory_name(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "experiments/runs/m1-par-d-s42-c0"
            self.assertEqual(branch_identity(run), {"branch_id": "m1-par-d-s42-c0",
                                                    "branch_source": "run-directory-name"})
            self.assertEqual(branch_identity(run, "m1-fork-a"), {"branch_id": "m1-fork-a",
                                                                 "branch_source": "explicit"})

    def test_prepare_records_the_branch_id_and_its_channel(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self.prepared(root, "m1-branch-001")
            self.assertEqual(state["branch_id"], "m1-branch-001")
            self.assertEqual(state["branch_source"], "run-directory-name")
            self.assertEqual(state["branch_channel"], "LVZ_BRANCH_ID")
            self.assertEqual(json.loads((root / "experiments/runs/m1-branch-001/launcher.json").read_text())["branch_id"],
                             "m1-branch-001")

    def test_illegal_and_oversized_branch_ids_fail_before_anything_is_prepared(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lock_inputs(root)
            for value in ("", " ", "-leading", "has space", "a" * 65, "café", 7):
                with self.subTest(value=value):
                    run = root / "experiments/runs/run-name"
                    run.mkdir(parents=True, exist_ok=True)
                    with patch("llm_vs_zombies.launcher._native",
                               side_effect=AssertionError("an unusable branch id must not launch anything")):
                        with self.assertRaisesRegex(ValueError, "branch id"):
                            prepare(root, run, branch_id=value)
                    self.assertFalse((run / "launcher.json").exists())

    def test_run_directory_name_that_cannot_be_a_branch_id_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lock_inputs(root)
            run = root / "experiments/runs/has space"
            run.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "run-directory-name.*rename the run or pass --branch-id"):
                prepare(root, run)

    def test_launcher_and_protocol_and_evidence_tree_share_one_grammar(self):
        from llm_vs_zombies.client import branch_scope_id
        from llm_vs_zombies.evidence_tree import branch_id as tree_branch_id
        corpus = ["a", "0", "m1-par-d-s42-c0", "branch.a_b:c-d", "a" + "b" * 63,
                  "", "-lead", ".lead", ":", "has space", "a/b", "café", "a" + "b" * 64]
        run = Path("experiments/runs/probe")
        for value in corpus:
            outcomes = []
            for validator in (lambda item: branch_identity(run, item), branch_scope_id, tree_branch_id):
                try:
                    outcomes.append(validator(value))
                except ValueError:
                    outcomes.append(None)
            with self.subTest(value=value):
                self.assertTrue(all((item is None) == (outcomes[0] is None) for item in outcomes),
                                f"grammars disagree on {value!r}: {outcomes}")

    def test_runtime_scope_must_equal_the_run_branch(self):
        scope = {"schema": "lvz.branch-scope.v1", "mode": "branch", "branch_id": "run-1",
                 "parent_branch_id": None, "origin": "session", "dedup_key": "branch_id+epoch+request_id"}
        state = {"branch_channel": "LVZ_BRANCH_ID", "branch_id": "run-1"}
        self.assertEqual(verify_branch_scope(state, {"branch": scope}), scope)
        with self.assertRaisesRegex(ValueError, "declared no branch scope"):
            verify_branch_scope(state, {})
        with self.assertRaisesRegex(ValueError, "differs from this run's branch"):
            verify_branch_scope(state, {"branch": {**scope, "branch_id": "run-2"}})
        # A state written before this channel existed keeps the previous behavior.
        self.assertIsNone(verify_branch_scope({"branch_id": "run-1"}, {"branch": scope}))
        self.assertIsNone(verify_branch_scope({"branch_id": "run-1"}, {}))

    def launch_state(self, run: Path) -> dict:
        return {"sandbox": str(run / "sandbox"), "engine": "engine", "bootstrap": "bootstrap",
                "runtime": "recorder", "audio_mode": "original", "branch_id": run.name,
                "branch_source": "run-directory-name", "branch_channel": "LVZ_BRANCH_ID"}

    def test_launch_passes_the_branch_id_and_binds_the_runtime_identity(self):
        for audio in (None, "sound_effects_allocation_none_v1"):
            with self.subTest(audio=audio), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); run = root / "experiments/runs/run-1"
                (run / "observations").mkdir(parents=True); (run / "sandbox").mkdir()
                state = self.launch_state(run)
                hello = {"game": {}, "branch": {"schema": "lvz.branch-scope.v1", "mode": "branch",
                         "branch_id": "run-1", "parent_branch_id": None, "origin": "session",
                         "dedup_key": "branch_id+epoch+request_id"}}
                def native(state, verb, *arguments):
                    return {"pid": 123, "creation_time": 456, "branch_id": "run-1"} if verb == "launch" else {}
                with patch("llm_vs_zombies.launcher.prepare", return_value=state), \
                     patch("llm_vs_zombies.launcher._native", side_effect=native) as helper, \
                     patch("llm_vs_zombies.launcher.verify_audio_activation"), \
                     patch("llm_vs_zombies.client.connect", return_value=StubClient(hello)):
                    result = start(root, run, initialize=False, audio_mode=audio or "original")
                launch = helper.call_args_list[0].args[1:]
                self.assertEqual(launch[:6], ("launch", "engine", "bootstrap", str(run / "sandbox"),
                                              str(run / "sandbox/game"), str(run / "sandbox/native-receipt.json")))
                self.assertEqual(launch[6:], ("run-1",) if audio is None else ("run-1", audio))
                self.assertEqual(result["runtime_branch"]["branch_id"], "run-1")
                self.assertEqual(json.loads((run / "launcher.json").read_text())["runtime_branch"]["branch_id"], "run-1")

    def test_runtime_scope_mismatch_stops_own_process_and_keeps_the_failure(self):
        for scope in (None, {"schema": "lvz.branch-scope.v1", "mode": "branch", "branch_id": "run-2",
                             "parent_branch_id": None, "origin": "session", "dedup_key": "branch_id+epoch+request_id"}):
            with self.subTest(scope=scope), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); run = root / "experiments/runs/run-1"
                (run / "observations").mkdir(parents=True); (run / "sandbox").mkdir()
                state = self.launch_state(run)
                hello = {"game": {}} if scope is None else {"game": {}, "branch": scope}
                verbs = []
                def native(state, verb, *arguments):
                    verbs.append(verb)
                    return {"pid": 123, "creation_time": 456, "branch_id": "run-1"} if verb == "launch" else {}
                with patch("llm_vs_zombies.launcher.prepare", return_value=state), \
                     patch("llm_vs_zombies.launcher._native", side_effect=native), \
                     patch("llm_vs_zombies.client.connect", return_value=StubClient(hello)):
                    with self.assertRaisesRegex(ValueError, "branch scope|differs from this run's branch"):
                        start(root, run, initialize=False)
                self.assertEqual(verbs, ["launch", "inject", "stop"])
                self.assertTrue(json.loads((run / "launcher.json").read_text())["process_cleaned_up"])

    def test_pre_resume_receipt_must_confirm_the_injected_branch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); run = root / "experiments/runs/run-1"
            (run / "observations").mkdir(parents=True); (run / "sandbox").mkdir()
            state = self.launch_state(run)
            # The native launcher seals this receipt before it resumes the
            # engine, which is how the owner recovers a PID it never saw.
            (run / "sandbox/native-receipt.json").write_text(json.dumps(
                {"pid": 123, "creation_time": 456, "branch_id": "somebody-else", "isolation_ready": True}))
            verbs = []
            def native(state, verb, *arguments):
                verbs.append(verb)
                if verb == "launch":
                    return {"pid": 123, "creation_time": 456, "branch_id": "somebody-else"}
                return {}
            with patch("llm_vs_zombies.launcher.prepare", return_value=state), \
                 patch("llm_vs_zombies.launcher._native", side_effect=native), \
                 patch("llm_vs_zombies.client.connect", return_value=StubClient({"game": {}})):
                with self.assertRaisesRegex(ValueError, "receipt does not confirm"):
                    start(root, run, initialize=False)
            self.assertEqual(verbs, ["launch", "stop"])


if __name__ == "__main__":
    unittest.main()
