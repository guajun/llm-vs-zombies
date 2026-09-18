"""Independent small simulator: tests scheduling, not original-game determinism."""
import copy
import base64
from contextlib import contextmanager
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch as mock_patch

from llm_vs_zombies.audit_compare import AuditTail, SCHEMA, EvidenceError, canonical, digests, file_hash
from llm_vs_zombies.client import Client, OutcomeUnknown, RemoteError
from llm_vs_zombies.engine_replay import (ReplayDivergence, ReplaySession, Trajectory,
    build_trajectory, capture_initial, identity_from_launcher, replay)
from llm_vs_zombies.session import SessionTrace


GAME = {"schema": SCHEMA, "target": "synthetic-test-only", "loaded_signatures_match": True,
        "coverage": {"complete_game_state": False}, "original_engine_replay_verified": False}
BUILD = {"runtime_protocol": 1, "avz_commit": "synthetic-test-only", "pointer_bits": 32}
ARTIFACTS = {"input_hashes": {"game": "1" * 64}, "module_hashes": {"runtime": "2" * 64},
             "profile_hashes": {"profile": "3" * 64}}


class ModelTransport:
    def __init__(self, directory, *, epoch=1, revision=0, divergence=None, wrong_result=False, terminal_tick=None,
                 pixel_byte=1, wrong_capture_guard=False, capture_timeout=False, animation_handle=None):
        self.directory = directory
        directory.mkdir(parents=True)
        self.game, self.animation_handle = copy.deepcopy(GAME), animation_handle
        if animation_handle is not None:
            self.game["coverage"]["reanimations"] = {"raw_handle_evidence": {
                "path": "audit/reanimation-handles.jsonl", "encoding": "initial_plus_json_patch",
                "binding": ["seq", "kind", "version"], "required": True}}
        (directory / "manifest.json").write_text(json.dumps(self.game), encoding="utf-8")
        self.files = {name: (directory / name).open("w", encoding="utf-8", newline="\n")
                      for name in ("events.jsonl", "checksums.jsonl", "state-deltas.jsonl")}
        if animation_handle is not None:
            self.files["reanimation-handles.jsonl"] = (directory / "reanimation-handles.jsonl").open("w", encoding="utf-8", newline="\n")
        self.seq, self.tick, self.revision, self.epoch, self.sun = 0, 0, revision, epoch, 500
        self.previous, self.divergence, self.wrong_result = None, divergence, wrong_result
        self.requests = []
        self.closed = False
        self.terminal_tick, self.game_ui = terminal_tick, 3
        self.draw_count, self.rng_delta = 0, 0
        self.pixel_byte, self.wrong_capture_guard, self.capture_timeout = pixel_byte, wrong_capture_guard, capture_timeout

    def version(self):
        return {"epoch": self.epoch, "tick": self.tick, "revision": self.revision}

    def observe(self):
        return {"version": self.version(), "game_ui": self.game_ui, "scene": 3, "sun": self.sun,
                "game_clock": self.tick, "wave": 1, "plants": [], "zombies": [], "seeds": []}

    def state(self):
        state = {"schema": SCHEMA, "rng": {"target": GAME["target"], "state": 1 + self.tick + self.rng_delta},
                "board": {"tick": self.tick, "sun": self.sun,
                          "render_effect": self.draw_count,
                          "hidden": 999 if self.divergence == self.tick else 0}}
        if self.animation_handle is not None:
            state["board"]["animation"] = {"status": "live", "node": "test#0"}
            state["reanimations"] = {"schema": "lvz.reanimation-links.v1", "valid": True, "issues": [],
                "nodes": {"test#0": {"state": {"time": self.tick}, "owners": ["test"]}}}
        return state

    def write(self, name, value):
        self.files[name].write(json.dumps(value, separators=(",", ":")) + "\n")
        self.files[name].flush()

    def event(self, kind, payload):
        value = {"schema": SCHEMA, "seq": self.seq, "kind": kind,
                 "version": self.version(), "payload": payload}
        self.seq += 1
        if kind in {"pre_step", "post_step"}:
            state = self.state()
            self.write("checksums.jsonl", dict(value, digests=digests(state)))
            delta = {"initial": state} if self.previous is None else {"patch": [{"op": "replace", "path": "", "value": state}]}
            self.write("state-deltas.jsonl", dict(value, **delta))
            if self.animation_handle is not None:
                handle = self.animation_handle
                raw = {"schema": "lvz.reanimation-raw.v1", "pool": {"capacity": 4, "used": 3, "count": 1,
                    "free_head": 3, "next_key": handle >> 16}, "actual_slot_ids": {"2": handle},
                    "links": [{"path": "/board/animation", "anchor": "test", "raw_handle": handle,
                        "slot": 2, "owner_dead": False, "lookup_matches": True, "actual_slot_id": handle,
                        "logical_node": "test#0", "normalized_reference": state["board"]["animation"]}]}
                self.write("reanimation-handles.jsonl", dict(value, **({"initial": raw} if self.previous is None else {"patch": []})))
            self.previous = state
        else:
            self.write("events.jsonl", value)

    def exchange(self, payload, timeout):
        terminal = False
        request = json.loads(payload)
        self.requests.append(copy.deepcopy(request))
        method, params, rid = request["method"], request["params"], request["request_id"]
        if method == "hello":
            result = {"build": BUILD, "game": self.game, "session": "synthetic", "pid": 123,
                      "capabilities": dict.fromkeys(("observe", "advance", "commit", "audit_snapshot", "capture_frame"), True)}
        elif method == "observe":
            result = self.observe()
        elif method == "audit_snapshot":
            result = {"state": self.state(), "version": self.version()}
        elif method == "stop_recording":
            self.event("recording_closed", {"request_id": rid})
            result = {"closed": True, "observation": self.observe()}
        elif method == "capture_frame":
            if self.capture_timeout:
                self.draw_count += 1
                raise TimeoutError("synthetic capture lost acknowledgement")
            outcome = params.get("outcome", "success")
            if outcome == "rpc_error":
                self.draw_count += 1
                self.revision += 1
                return json.dumps({"protocol": 1, "request_id": rid, "ok": False,
                    "error": {"code": "capture_failed", "message": "synthetic render error"}}).encode()
            result = {"capture_ok": outcome == "success", "version": self.version(), "forced_render": outcome != "unavailable"}
            if outcome == "unavailable":
                result["reason"] = "synthetic unsupported format"
            else:
                self.draw_count += 1
                result.update(source="original_game_frame", width=2, height=1, pixel_format="bgr24", row_stride=6,
                              origin="top_left", method="synthetic_draw", used_3d=False,
                              known_rng_unchanged=(outcome == "success" and not self.wrong_capture_guard),
                              game_clock_before=self.tick, game_clock_after=self.tick)
                if outcome == "guard_failed":
                    self.rng_delta += 1
                    self.revision += 1
                    result.update(version=self.version(), reason="capture_changed_game_state")
                else:
                    result["pixels_base64"] = base64.b64encode(bytes([self.pixel_byte]) * 6).decode()
        elif method in {"commit", "advance"}:
            if request["expect"] != self.version():
                raise AssertionError("replayer sent a stale expect")
            self.event("request_started", {"request_id": rid, "request": request})
            outcomes = []
            count = params["max_ticks" if method == "advance" else "advance_ticks"]
            executed, reason = 0, "budget_exhausted"
            for ordinal, action in enumerate(params.get("actions", [])):
                outcome = {"ok": action["op"] == "plant", "action": action, "ordinal": ordinal}
                if outcome["ok"]:
                    self.sun -= 25
                else:
                    outcome["error"] = "synthetic_failure"
                outcomes.append(outcome)
                self.revision += 1
                self.event("action", {"request_id": rid, "ordinal": ordinal, "action": action, "result": outcome})
                if not outcome["ok"]:
                    reason = "action_failed"
                    break
            if reason != "action_failed":
                for _ in range(count):
                    self.event("pre_step", {"request_id": rid, "requested_ticks": count, "executed_ticks": executed})
                    self.tick += 1
                    self.revision = 0
                    executed += 1
                    terminal = self.tick == self.terminal_tick
                    if terminal:
                        self.game_ui = 2
                    self.event("post_step", {"request_id": rid, "native_tick_delta": 1, "executed_ticks": executed})
                    if terminal:
                        self.event("terminal_transition", {"request_id": rid, "native_tick_delta": 1,
                            "tick_delta_verified": True, "board_identity_preserved": True})
                        reason = "scene_changed"
                        break
            result = {"action_results": outcomes, "requested_ticks": count, "executed_ticks": executed,
                      "stop_reason": reason, "observation": self.observe()}
            self.event("request_completed", {"request_id": rid, "result": result})
            if self.wrong_result and executed:
                result["executed_ticks"] -= 1
        else:
            raise AssertionError(method)
        response = json.dumps({"protocol": 1, "request_id": rid, "ok": True, "result": result}).encode()
        if terminal:
            self.epoch += 1
            self.tick = self.revision = 0
        return response

    def close(self):
        if not self.closed:
            for stream in self.files.values():
                stream.close()
            self.closed = True


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        model = ModelTransport(self.source / "audit")
        with SessionTrace(self.source / "session.jsonl") as trace, Client(model, trace=trace) as client:
            hello = client.hello()
            self.identity = identity_from_launcher(hello, ARTIFACTS)
            self.marker = capture_initial(client, identity=self.identity, initialization={"synthetic": True})
            self.actions = [{"op": "plant", "row": 1, "col": 1}, {"op": "invalid"}, {"op": "never_attempted"}]
            client.commit(self.actions, advance_ticks=5)
            client.commit([{"op": "plant", "row": 2, "col": 2}])
            client.advance(2)
            client.commit([{"op": "plant", "row": 3, "col": 3}], advance_ticks=3)
            client.request("stop_recording", expect=client.version)
        self.trajectory = build_trajectory(self.source / "session.jsonl", self.source / "audit", self.root / "bundle")
        self.live = None

    def tearDown(self):
        self.temp.cleanup()

    def initializer(self, *, divergence=None, wrong_result=False, wrong_identity=False, terminal_tick=None,
                    wrong_capture_guard=False, capture_timeout=False):
        @contextmanager
        def initialize(trajectory, output):
            self.live = ModelTransport(output / "audit", epoch=99, revision=2,
                                       divergence=divergence, wrong_result=wrong_result, terminal_tick=terminal_tick,
                                       pixel_byte=2, wrong_capture_guard=wrong_capture_guard, capture_timeout=capture_timeout,
                                       animation_handle=131074 if "reanimations" in trajectory.audit.manifest.get("coverage", {}) else None)
            identity = copy.deepcopy(self.identity)
            if wrong_identity:
                identity["artifacts"]["module_hashes"]["runtime"] = "4" * 64
            with SessionTrace(output / "actual.jsonl") as trace, Client(self.live, trace=trace) as client:
                yield ReplaySession(client, identity, output / "audit")
        return initialize

    def test_full_replay_preserves_failed_attempt_order_and_epoch_mapping(self):
        result = replay(self.trajectory, self.initializer(), self.root / "actual")
        self.assertTrue(result["equal"])
        self.assertFalse(result["original_engine_replay_verified"])
        steps = [request for request in self.live.requests if request["method"] in {"commit", "advance"}]
        self.assertEqual(steps[0]["params"]["actions"], self.actions)
        self.assertEqual(len(result["requests"][0]["result"]["action_results"]), 2)
        self.assertEqual(steps[1]["expect"], {"epoch": 99, "tick": 0, "revision": 4})
        self.assertEqual(result["reached_version"]["tick"], 5)

    def animation_recording(self):
        source = self.root / "animation-source"
        model = ModelTransport(source / "audit", animation_handle=65538)
        with SessionTrace(source / "session.jsonl") as trace, Client(model, trace=trace) as client:
            self.identity = identity_from_launcher(client.hello(), ARTIFACTS)
            capture_initial(client, identity=self.identity, initialization={"synthetic": True})
            client.advance(2)
            client.request("stop_recording", expect=client.version)
        return build_trajectory(source / "session.jsonl", source / "audit", self.root / "animation-bundle")

    def test_animation_sidecar_is_bound_copied_and_checked_during_actual_replay(self):
        trajectory = self.animation_recording()
        key = "audit/reanimation-handles.jsonl"
        self.assertEqual(trajectory.manifest["files"][key], file_hash(trajectory.directory / key))
        result = replay(trajectory, self.initializer(), self.root / "animation-replayed")
        self.assertTrue(result["equal"])
        self.assertNotEqual(file_hash(trajectory.directory / key), file_hash(self.root / "animation-replayed" / key))
        with (trajectory.directory / key).open("a") as stream:
            stream.write("{}\n")
        with self.assertRaisesRegex(EvidenceError, "SHA-256"):
            Trajectory.load(trajectory.directory)

    def test_normalized_trajectory_cannot_drop_raw_evidence_binding(self):
        import hashlib
        trajectory = self.animation_recording()
        manifest = trajectory.manifest
        del manifest["files"]["audit/reanimation-handles.jsonl"]
        manifest["trajectory_id"] = hashlib.sha256(canonical({key: value for key, value in manifest.items()
                                                            if key != "trajectory_id"})).hexdigest()
        (trajectory.directory / "trajectory.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(EvidenceError, "file set is incomplete"):
            Trajectory.load(trajectory.directory)
        (trajectory.directory / "audit/reanimation-handles.jsonl").unlink()
        with self.assertRaisesRegex(EvidenceError, "required raw animation"):
            Trajectory.load(trajectory.directory)

    def boundary_recording(self, name, groups, *, capture=False, transparent=False):
        source = self.root / name

        class TransparentDraw(ModelTransport):
            def state(self):
                state = super().state()
                state["board"]["render_effect"] = 0
                return state

        model = (TransparentDraw if transparent else ModelTransport)(source / "audit")
        with SessionTrace(source / "session.jsonl") as trace, Client(model, trace=trace) as client:
            identity = identity_from_launcher(client.hello(), ARTIFACTS)
            capture_initial(client, identity=identity, initialization={"seed": 42, "synthetic": True})
            for ticks in groups:
                if capture:
                    client.request("capture_frame", expect=client.version)
                client.advance(ticks)
            client.request("stop_recording", expect=client.version)
        return build_trajectory(source / "session.jsonl", source / "audit", self.root / (name + "-bundle"))

    def boundary_tools(self):
        return runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools/check-boundary-equivalence.py"))

    def test_boundary_equivalence_compares_single_vs_batch_without_request_metadata(self):
        left = self.boundary_recording("single", [1] * 100)
        right = self.boundary_recording("batch", [100])
        result = self.boundary_tools()["compare"](left.directory, right.directory, purpose="batch", ticks=100)
        self.assertTrue(result["passed"])
        self.assertEqual(result["frames_compared"], 200)
        self.assertEqual(result["requests"], [100, 1])
        self.assertFalse(result["spawn_exercised"])
        self.assertFalse(result["original_engine_replay_verified"])

    def test_boundary_render_comparison_detects_hidden_effect_despite_unchanged_rng_and_clock(self):
        left = self.boundary_recording("render-off", [2])
        right = self.boundary_recording("render-on", [2], capture=True)
        compare = self.boundary_tools()["compare"]
        result = compare(left.directory, right.directory, purpose="render", ticks=2)
        self.assertFalse(result["passed"])
        self.assertEqual(result["difference"]["path"], "/board/render_effect")
        self.assertEqual(result["phase"], "pre_step")
        transparent = self.boundary_recording("render-transparent", [2], capture=True, transparent=True)
        self.assertTrue(compare(left.directory, transparent.directory, purpose="render", ticks=2)["passed"])

    def test_spawn_semantic_comparison_requires_verified_handle_mapping(self):
        from llm_vs_zombies.audit_compare import AuditFrame
        normalize = self.boundary_tools()["_spawn_semantics"]
        normalized = []
        for handle in (65538, 131074):
            event = {"payload": {"slot": 7, "ordinal": 9, "boundary": {},
                "initial": {"raw_scalar_fields": {"00000118": handle, "00000140": 0, "00000144": 0,
                                                   "00000150": 0, "0000002c": 0x3f800000}},
                "global_mt_after": {"cursor": 123}}}
            raw = {"links": [{"path": "/zombies/slots/7/fields/00000118", "raw_handle": handle,
                              "lookup_matches": True, "normalized_reference": {"status": "live", "node": "owner#0"}}]}
            frame = AuditFrame(0, "post_step", {}, {}, {}, {}, raw_animations=raw)
            normalized.append(normalize(event, frame))
            raw["links"][0]["raw_handle"] += 1
            with self.assertRaisesRegex(EvidenceError, "cannot be proven"):
                normalize(event, frame)
        self.assertEqual(normalized[0], normalized[1])
        self.assertEqual(normalized[0]["initial"]["raw_scalar_fields"]["0000002c"], 0x3f800000)
        self.assertEqual(normalized[0]["global_mt_after"]["cursor"], 123)

    def test_seek_recomputes_intermediate_actions_and_truncates_only_budget(self):
        taken = []
        result = replay(self.trajectory, self.initializer(), self.root / "seek", target_tick=4,
                        on_takeover=lambda session, parent: taken.append((session.client.version, parent)))
        self.assertEqual(result["reached_version"]["tick"], 4)
        self.assertEqual(len(result["requests"]), 4)
        self.assertEqual(result["requests"][-1]["params"]["advance_ticks"], 2)
        self.assertEqual(self.live.sun, 425)
        self.assertEqual(taken[0][1]["parent_trajectory_id"], self.trajectory.manifest["trajectory_id"])

    def test_seek_zero_does_not_execute_same_tick_actions(self):
        result = replay(self.trajectory, self.initializer(), self.root / "zero", target_tick=0)
        self.assertEqual(result["requests"], [])
        self.assertEqual(self.live.sun, 500)

    def test_seek_boundary_does_not_execute_later_actions(self):
        result = replay(self.trajectory, self.initializer(), self.root / "two", target_tick=2)
        self.assertEqual(len(result["requests"]), 3)
        self.assertEqual(self.live.sun, 450)

    def test_hidden_field_divergence_reports_first_patch_path_and_stops(self):
        with self.assertRaises(ReplayDivergence) as caught:
            replay(self.trajectory, self.initializer(divergence=2), self.root / "diverged")
        self.assertEqual(caught.exception.report["difference"]["path"], "/board/hidden")
        self.assertEqual(self.live.tick, 2)
        report = json.loads((self.root / "diverged/replay-report.json").read_text())
        self.assertFalse(report["equal"])
        self.assertEqual(report["requests"][-1]["result"]["executed_ticks"], 2)

    def test_actual_result_is_not_filled_from_expected(self):
        with self.assertRaises(ReplayDivergence):
            replay(self.trajectory, self.initializer(wrong_result=True), self.root / "bad-result")
        report = json.loads((self.root / "bad-result/replay-report.json").read_text())
        self.assertEqual(report["requests"][-1]["result"]["executed_ticks"], 1)

    def test_identity_mismatch_fails_before_actions(self):
        with self.assertRaises(ReplayDivergence):
            replay(self.trajectory, self.initializer(wrong_identity=True), self.root / "identity")
        self.assertEqual(self.live.requests, [])

    def test_evidence_tamper_is_rejected_before_initializer(self):
        with (self.root / "bundle/audit/events.jsonl").open("a") as stream:
            stream.write("{}\n")
        with self.assertRaisesRegex(EvidenceError, "SHA-256"):
            replay(self.trajectory, self.initializer(), self.root / "tampered")
        self.assertIsNone(self.live)

    def test_native_only_source_keeps_all_action_attempts(self):
        marker = self.root / "initial.json"
        marker.write_text(json.dumps(self.marker), encoding="utf-8")
        native = build_trajectory(None, self.source / "audit", self.root / "native", initial=marker)
        self.assertEqual(native.steps, self.trajectory.steps)

    def test_incomplete_source_trace_and_unknown_mutation_are_rejected(self):
        trace = self.source / "session.jsonl"
        records = [json.loads(line) for line in trace.read_text().splitlines()]
        for record in records:
            if record["kind"] == "request" and record["data"]["method"] == "commit":
                record["data"]["method"] = "rng_restore"
                break
        trace.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        with self.assertRaisesRegex(EvidenceError, "intervening mutation"):
            build_trajectory(trace, self.source / "audit", self.root / "bad")

    def test_target_out_of_range_is_rejected(self):
        with self.assertRaises(ValueError):
            replay(self.trajectory, self.initializer(), self.root / "out", target_tick=6)

    def terminal_recording(self):
        directory = self.root / "terminal-source"
        directory.mkdir()
        with SessionTrace(directory / "session.jsonl") as trace, Client(ModelTransport(directory / "audit", terminal_tick=5), trace=trace) as client:
            capture_initial(client, identity=self.identity, initialization={"synthetic": True})
            client.advance(10)
            client.observe()  # Native Boundary has moved to the next epoch.
            client.request("stop_recording", expect=client.version)
        return directory

    def test_verified_terminal_transition_replays_old_boundary_and_refreshes_new_epoch(self):
        directory = self.terminal_recording()
        trajectory = build_trajectory(directory / "session.jsonl", directory / "audit", self.root / "terminal-bundle")
        result = replay(trajectory, self.initializer(terminal_tick=5), self.root / "terminal-actual")
        self.assertTrue(result["equal"])
        self.assertEqual(result["reached_version"], {"epoch": 99, "tick": 5, "revision": 0})
        self.assertEqual(result["current_runtime_version"], {"epoch": 100, "tick": 0, "revision": 0})
        self.assertTrue(result["terminal_transition"]["payload"]["tick_delta_verified"])

    def test_unverified_terminal_transition_is_rejected(self):
        directory = self.terminal_recording()
        path = directory / "audit/events.jsonl"
        records = [json.loads(line) for line in path.read_text().splitlines()]
        for record in records:
            if record["kind"] == "terminal_transition":
                record["payload"]["tick_delta_verified"] = False
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
        with self.assertRaisesRegex(EvidenceError, "cannot prove"):
            build_trajectory(directory / "session.jsonl", directory / "audit", self.root / "unverified")

    def test_takeover_trace_can_be_packaged_as_complete_branch(self):
        base = self.initializer()

        @contextmanager
        def closing_initializer(trajectory, output):
            with base(trajectory, output) as session:
                yield session
                session.client.observe()
                session.client.request("stop_recording", expect=session.client.version)

        def take_over(session, parent):
            session.client.commit([{"op": "plant", "row": 6, "col": 8}], advance_ticks=2)

        output = self.root / "branch-run"
        replay(self.trajectory, closing_initializer, output, target_tick=2, on_takeover=take_over)
        branch = build_trajectory(output / "actual.jsonl", output / "audit", self.root / "branch-bundle")
        self.assertEqual(branch.initial["parent"]["trajectory_id"], self.trajectory.manifest["trajectory_id"])
        self.assertEqual(branch.steps[-1]["result"]["observation"]["version"]["tick"], 4)
        self.assertEqual(branch.steps[-1]["request"]["params"]["actions"][0]["row"], 6)

    def capture_recording(self, outcomes, *, end_with_capture=False):
        directory = self.root / "capture-source"
        directory.mkdir()
        with SessionTrace(directory / "session.jsonl") as trace, Client(ModelTransport(directory / "audit"), trace=trace) as client:
            capture_initial(client, identity=self.identity, initialization={"synthetic": True})
            client.advance(1)
            for outcome in outcomes:
                try:
                    client.request("capture_frame", {"outcome": outcome}, expect=client.version)
                except RemoteError:
                    pass
                client.observe()
                client.request("audit_snapshot")
            if not end_with_capture:
                client.advance(2)
            client.request("stop_recording", expect=client.version)
        return directory

    def test_capture_interventions_replay_actual_draws_but_do_not_compare_pixels(self):
        directory = self.capture_recording(["success", "unavailable", "guard_failed", "rpc_error"])
        trajectory = build_trajectory(directory / "session.jsonl", directory / "audit", self.root / "capture-bundle")
        captures = [step for step in trajectory.steps if step["request"]["method"] == "capture_frame"]
        self.assertEqual(len(captures), 4)
        self.assertNotIn("pixels_base64", captures[0]["capture_response"]["result"])
        self.assertEqual(captures[0]["pixels_evidence"]["byte_length"], 6)
        result = replay(trajectory, self.initializer(), self.root / "capture-actual")
        self.assertTrue(result["equal"])
        self.assertEqual(self.live.draw_count, 3)
        self.assertEqual(self.live.rng_delta, 1)
        self.assertEqual(result["capture_interventions"]["executed"], 4)
        self.assertTrue(result["capture_interventions"]["reproduced"])
        actual = next(step for step in result["requests"] if step.get("method") == "capture_frame")
        self.assertNotEqual(actual["pixels_evidence"]["sha256"], captures[0]["pixels_evidence"]["sha256"])
        self.assertTrue(actual["post_capture_state_compared"])

    def test_capture_can_be_last_intervention_and_seek_excludes_later_same_tick_draw(self):
        directory = self.capture_recording(["success"], end_with_capture=True)
        trajectory = build_trajectory(directory / "session.jsonl", directory / "audit", self.root / "capture-last")
        full = replay(trajectory, self.initializer(), self.root / "capture-last-actual")
        self.assertTrue(full["capture_interventions"]["reproduced"])
        self.assertEqual(full["reached_version"]["tick"], 1)
        seek = replay(trajectory, self.initializer(), self.root / "capture-before-draw", target_tick=1)
        self.assertEqual(seek["capture_interventions"]["executed"], 0)
        self.assertEqual(self.live.draw_count, 0)

    def test_capture_guard_mismatch_fails_even_when_step_state_has_not_changed(self):
        directory = self.capture_recording(["success"])
        trajectory = build_trajectory(directory / "session.jsonl", directory / "audit", self.root / "capture-guard")
        with self.assertRaises(ReplayDivergence) as caught:
            replay(trajectory, self.initializer(wrong_capture_guard=True), self.root / "capture-guard-actual")
        self.assertEqual(caught.exception.report["difference"]["path"], "/result/known_rng_unchanged")
        report = json.loads((self.root / "capture-guard-actual/replay-report.json").read_text())
        self.assertFalse(report["capture_interventions"]["reproduced"])
        self.assertEqual(self.live.tick, 1)

    def test_capture_unknown_live_outcome_is_not_retried_or_reported_as_reproduced(self):
        directory = self.capture_recording(["success"])
        trajectory = build_trajectory(directory / "session.jsonl", directory / "audit", self.root / "capture-unknown")
        with self.assertRaises(OutcomeUnknown):
            replay(trajectory, self.initializer(capture_timeout=True), self.root / "capture-unknown-actual")
        report = json.loads((self.root / "capture-unknown-actual/replay-report.json").read_text())
        self.assertTrue(report["capture_interventions"]["unknown_outcome"])
        self.assertEqual(report["capture_interventions"]["attempted"], 1)
        self.assertEqual(report["capture_interventions"]["executed"], 0)
        self.assertFalse(report["capture_interventions"]["reproduced"])

    def test_capture_error_without_post_observation_is_incomplete_evidence(self):
        directory = self.capture_recording(["rpc_error"], end_with_capture=True)
        path = directory / "session.jsonl"
        records = [json.loads(line) for line in path.read_text().splitlines()]
        # Remove the post-capture observe and snapshot exchanges, preserving a
        # contiguous trace so rejection concerns missing boundary evidence.
        capture_seen = False
        removed_ids = set()
        kept = []
        for record in records:
            if record["kind"] == "request":
                method = record["data"]["method"]
                capture_seen = capture_seen or method == "capture_frame"
                if capture_seen and method in {"observe", "audit_snapshot"}:
                    removed_ids.add(record["data"]["request_id"])
            if record.get("data", {}).get("request_id") in removed_ids:
                continue
            record["seq"] = len(kept)
            kept.append(record)
        path.write_text("".join(json.dumps(record) + "\n" for record in kept))
        with self.assertRaises(EvidenceError):
            build_trajectory(path, directory / "audit", self.root / "capture-incomplete")

    def test_lightweight_capture_evidence_is_accepted_without_pixels(self):
        directory = self.capture_recording(["success"])
        rows = [json.loads(line) for line in (directory / "session.jsonl").read_text().splitlines()]
        response = next(row["data"]["result"] for row in rows if row["kind"] == "response"
                        and row["data"].get("result", {}).get("capture_ok") is True)
        self.assertNotIn("pixels_base64", response)
        self.assertTrue(response["trace_metadata_only"])
        trajectory = build_trajectory(directory / "session.jsonl", directory / "audit", self.root / "capture-lightweight")
        result = replay(trajectory, self.initializer(), self.root / "capture-lightweight-actual")
        self.assertTrue(result["capture_interventions"]["reproduced"])

    def test_legacy_raw_capture_response_remains_replayable(self):
        directory = self.capture_recording(["success"])
        path = directory / "session.jsonl"
        records = [json.loads(line) for line in path.read_text().splitlines()]
        for record in records:
            if record["kind"] == "response" and record["data"].get("result", {}).get("capture_ok") is True:
                result = record["data"]["result"]
                result.pop("pixels_evidence")
                result.pop("trace_metadata_only")
                result["pixels_base64"] = base64.b64encode(bytes([1]) * 6).decode()
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
        trajectory = build_trajectory(path, directory / "audit", self.root / "legacy-capture")
        result = replay(trajectory, self.initializer(), self.root / "legacy-capture-actual")
        self.assertTrue(result["capture_interventions"]["reproduced"])

    def test_unknown_capture_source_outcome_is_not_packaged(self):
        directory = self.capture_recording(["success"])
        path = directory / "session.jsonl"
        records = [json.loads(line) for line in path.read_text().splitlines()]
        for record in records:
            if record["kind"] == "response" and record["data"].get("result", {}).get("capture_ok") is True:
                record["kind"] = "exception"
                record["data"] = {"request_id": record["data"]["request_id"], "type": "TimeoutError",
                                  "message": "lost capture acknowledgement", "outcome_unknown": True}
                break
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
        with self.assertRaisesRegex(EvidenceError, "uncertain"):
            build_trajectory(path, directory / "audit", self.root / "unknown-capture-source")

    def test_live_audit_tail_only_decodes_new_frames_and_rejects_truncation(self):
        directory = self.root / "tail"
        model = ModelTransport(directory / "audit")
        tail = AuditTail(directory / "audit")
        with SessionTrace(directory / "session.jsonl") as trace, Client(model, trace=trace) as client:
            client.advance(2)
            first_id = model.requests[-1]["request_id"]
            self.assertEqual(sum(1 for _ in tail.read_request(first_id)), 4)
            client.advance(1)
            second_id = model.requests[-1]["request_id"]
            with mock_patch.object(tail._decoder, "accept", wraps=tail._decoder.accept) as decode_frame:
                self.assertEqual(sum(1 for _ in tail.read_request(second_id)), 2)
                self.assertEqual(decode_frame.call_count, 2)
            self.assertEqual(len(tail.request_headers(first_id)), 4)
            self.assertEqual(len(tail.request_headers(second_id)), 2)
        (directory / "audit/events.jsonl").write_bytes(b"")
        with self.assertRaisesRegex(EvidenceError, "truncated"):
            list(tail.read_request("not-run"))


if __name__ == "__main__":
    unittest.main()
