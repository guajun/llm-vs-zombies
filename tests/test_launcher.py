import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from llm_vs_zombies.launcher import DEFAULT_CARDS, REFERENCE_LAYOUT, REQUIRED_INPUTS, prepare, stop, synthetic_profile, verify_inputs, verify_scenario


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
        entries = []
        for name in REQUIRED_INPUTS:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            content = name.encode()
            path.write_bytes(content)
            entries.append({"path": name, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)})
        (root / "dependencies.lock.json").write_text(json.dumps({"files": entries}))

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


if __name__ == "__main__":
    unittest.main()
