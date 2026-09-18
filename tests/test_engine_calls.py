"""Simulated returned-call evidence: these tests never execute the game."""
import copy
import hashlib
from contextlib import contextmanager
import json
from pathlib import Path
import runpy
import tempfile
import unittest

from llm_vs_zombies.audit_compare import (AuditLog, AuditTail, EvidenceError, ENGINE_CALL_MODE, ENGINE_CALL_RAW,
                                        _seeded_rng, canonical)
from llm_vs_zombies.client import Client, _completed_step, ProtocolError
from llm_vs_zombies.engine_replay import (ReplayDivergence, ReplaySession, capture_initial, identity_from_launcher,
                                        build_trajectory, replay)
from llm_vs_zombies.session import SessionTrace
from test_engine_replay import ModelTransport, ARTIFACTS
from test_draw_schedule import DrawTransport, DRAW_SPEC


CALL_SPEC = {"mode": ENGINE_CALL_MODE, "entry_rva": 337488,
    "invocation_evidence": "checked_wrapper_enter_and_return", "call_id_scope": "process_controlled_calls_only",
    "counter_bits": 64, "initialization_calls_counted": False, "ordinary_clock_delta": 1,
    "terminal_zero_clock_update": True, "zero_terminal_ui_pairs": [[3, 4]],
    "board_identity_basis": "non-null App.Board pointer preserved across the checked call",
    "internal_board_tick_count_verified": False, "live_verified": False, "raw_evidence": ENGINE_CALL_RAW}


class CallTransport(DrawTransport):
    def __init__(self, directory, *, zero_tick=2, pointer=0x10000000, call_fault=None, clock_terminal=False, **kwargs):
        self.calls = self.clock_steps = self.zeros = self.terminal_steps = self.reserved = self.active = 0
        self.request_calls = self.request_ticks = self.request_zero = 0
        self.call_fault, self.zero_tick, self.pointer = call_fault, zero_tick, pointer
        self.clock_terminal = clock_terminal
        self.call_pre = self.terminal_metadata = None
        super().__init__(directory, terminal_tick=zero_tick + 1 if zero_tick is not None else None, **kwargs)
        self.game["engine_call_boundary"] = copy.deepcopy(CALL_SPEC)
        (directory / "manifest.json").write_text(json.dumps(self.game), encoding="utf-8")
        self.files[ENGINE_CALL_RAW] = (directory / ENGINE_CALL_RAW).open("w", encoding="utf-8", newline="\n")

    def health(self):
        return {"mode": ENGINE_CALL_MODE, "reserved_calls": self.reserved, "entered_calls": self.calls,
            "returned_calls": self.calls, "written_post_boundaries": self.calls,
            "verified_clock_steps": self.clock_steps, "verified_terminal_zero_calls": self.zeros,
            "terminal_clock_steps": self.terminal_steps, "active_call_id": self.active or None,
            "faults": 1 if self.call_fault == "bad_health" else 0, "reentrant_calls": 0,
            "wrong_thread_calls": 0, "aborted_calls": 0, "healthy": self.call_fault != "bad_health"}

    def clocks(self):
        value = super().clocks()
        value.update(effect_clock=100 + self.calls, mj_clock=200 + self.calls)
        return value

    def state(self):
        value = super().state()
        value["board"]["0000556c"] = self.clocks()["effect_clock"]
        value["app"]["mj_clock"] = self.clocks()["mj_clock"]
        if self.call_fault == "terminal_state" and self.game_ui == 4:
            value["board"]["hidden"] = 123
        return value

    def write(self, name, value):
        if value.get("kind") in {"zombie_initialized", "particle_shake_seed"}:
            value["payload"]["engine_call_id"] = self.active or None
            if self.call_fault == "wrong_hook":
                value["payload"]["engine_call_id"] = 999
        super().write(name, value)
        if name == "checksums.jsonl":
            metadata = value["payload"]["engine_call"]
            raw = {"schema": "lvz.engine-call-raw.v1", "seq": value["seq"], "kind": value["kind"],
                "version": value["version"], "engine_call_id": metadata["engine_call_id"],
                "payload": {"board_address_before": self.pointer,
                            "board_address_after": self.pointer if value["kind"] == "post_step" else None}}
            if self.call_fault == "raw_pointer" and value["kind"] == "post_step":
                raw["payload"]["board_address_after"] += 4
            if self.call_fault == "raw_id":
                raw["engine_call_id"] += 1
            super().write(ENGINE_CALL_RAW, raw)

    def event(self, kind, payload):
        if kind == "request_started":
            self.request_calls = self.request_ticks = self.request_zero = 0
        if kind == "pre_step":
            self.reserved += 1
            self.active = self.reserved
            self.call_pre = {"schema": "lvz.engine-call.v1", "engine_call_id": self.active,
                "request_call_index": self.request_calls + 1, "pre_version": self.version(), "lifecycle": "prepared",
                "native_clock_before": self.tick, "native_clock_after": None, "native_tick_delta": None,
                "clock_delta_measured": False, "engine_call_entered": False, "engine_call_completed": False,
                "board_identity_preserved": None, "ready_before": True, "game_ui_before": 3,
                "clocks_before": self.clocks(), "clocks_after": None,
                "entered_calls_total": self.calls, "returned_calls_total": self.calls}
            payload = dict(payload, executed_ticks=self.request_ticks, engine_call=copy.deepcopy(self.call_pre))
        if kind == "post_step":
            terminal_zero = not self.clock_terminal and self.zero_tick is not None and self.tick == self.zero_tick + 1
            if terminal_zero:
                self.tick -= 1
                self.revision = self.call_pre["pre_version"]["revision"]
                self.game_ui = 4
                if self.call_fault == "live_zero":
                    self.game_ui = 3
                if self.call_fault == "unsupported_ui":
                    self.game_ui = 2
                if self.call_fault == "double_clock":
                    self.tick += 2
                if self.call_fault == "negative_clock":
                    self.tick -= 1
            delta = self.tick - self.call_pre["native_clock_before"]
            self.calls += 1
            self.request_calls += 1
            self.clock_steps += delta
            self.request_ticks += delta
            self.zeros += int(terminal_zero)
            self.request_zero += int(terminal_zero)
            terminal = self.game_ui != 3
            self.terminal_steps += int(terminal and delta == 1)
            # Original update changes RNG even when no draw or clock step occurs.
            self.rng["game_thread_crt"]["state"] ^= 0xabc
            transition = "terminal_zero_clock_update" if terminal_zero else "terminal_clock_step" if terminal else "clock_step"
            metadata = dict(self.call_pre, lifecycle="returned", native_clock_after=self.tick, native_tick_delta=delta,
                clock_delta_measured=True, engine_call_entered=True, engine_call_completed=True, board_identity_preserved=True,
                ready_after=not terminal, game_ui_after=self.game_ui, clocks_after=self.clocks(), transition_kind=transition,
                entered_calls_total=self.calls, returned_calls_total=self.calls)
            if self.call_fault == "missing_return":
                metadata["engine_call_completed"] = False
            if self.call_fault == "wrong_id":
                metadata["engine_call_id"] += 1
            if self.call_fault == "wrong_index":
                metadata["request_call_index"] += 1
            if self.call_fault == "wrong_clock_fact":
                metadata["clocks_after"]["game_clock"] += 1
            if terminal:
                self.terminal_metadata = copy.deepcopy(metadata)
            payload = dict(payload, native_tick_delta=delta, executed_ticks=self.request_ticks, engine_call=metadata)
        if kind == "terminal_transition":
            if self.call_fault == "missing_transition":
                return
            delta = self.terminal_metadata["native_tick_delta"]
            payload = dict(payload, native_tick_delta=delta, tick_delta_verified=delta == 1, terminal_call_verified=True,
                clock_delta_measured=True, transition_kind=self.terminal_metadata["transition_kind"], engine_call=self.terminal_metadata)
        if kind == "request_completed":
            result = payload["result"]
            result.update(executed_ticks=self.request_ticks, executed_engine_calls=self.request_calls,
                          terminal_zero_clock_calls=self.request_zero, last_engine_call_id=self.calls if self.request_calls else None)
            if self.terminal_metadata:
                result["terminal_kind"] = self.terminal_metadata["transition_kind"]
        if kind == "recording_closed":
            ModelTransport.event(self, kind, payload)
            ModelTransport.event(self, "engine_call_closed", self.health())
            ModelTransport.event(self, "draw_schedule_closed", dict(self.counts(), installed=True, latched=True,
                sealed=True, active=False, automatic_allowed=0, automatic_denied=self.diagnostic,
                controlled_calls=self.warm_frames + self.step_frames, faults=0, wrong_thread_calls=0, healthy=True))
            return
        super().event(kind, payload)
        if kind == "post_step":
            self.active = 0

    def exchange(self, payload, timeout):
        response = json.loads(super().exchange(payload, timeout))
        method = json.loads(payload)["method"]
        if method == "hello":
            response["result"]["capabilities"][ENGINE_CALL_MODE] = True
        elif method == "audit_snapshot":
            response["result"]["engine_call"] = self.health()
        return json.dumps(response).encode()


class EngineCallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def source(self, name="source", *, zero_tick=2, actions=None, split=False, singles=False, **kwargs):
        directory = self.root / name
        model = CallTransport(directory / "audit", zero_tick=zero_tick, **kwargs)
        with SessionTrace(directory / "trace.jsonl") as trace, Client(model, trace=trace) as client:
            identity = identity_from_launcher(client.hello(), ARTIFACTS)
            client.observe()
            client.request("prepare_render", expect=client.version)
            recipe = {"seed": 42, "execution_mode": DRAW_SPEC["mode"], "draw_schedule": DRAW_SPEC,
                "clock_anchor": model.clocks(), "postwarm_clock": model.clocks(),
                "render_preparation": {"warm_frames": 1, "step_frames": 0, "verified_before_draw": True,
                    "seeded_rng_sha256": hashlib.sha256(canonical(_seeded_rng(42))).hexdigest(),
                    "postwarm_rng_sha256": hashlib.sha256(canonical(model.rng)).hexdigest()}}
            capture_initial(client, identity=identity, initialization=recipe)
            count = zero_tick + 1 if zero_tick is not None else 3
            if split:
                client.advance(zero_tick)
                count = 1
            if singles:
                for _ in range(count):
                    client.advance(1)
            elif zero_tick is None and not actions:
                client.advance(count)
            else:
                client.commit(actions or [], advance_ticks=count)
            client.observe()
            client.request("stop_recording", expect=client.version)
        return directory

    def bundle(self, directory):
        return build_trajectory(directory / "trace.jsonl", directory / "audit", directory / "bundle")

    def initializer(self, **kwargs):
        @contextmanager
        def initialize(trajectory, output):
            self.actual = CallTransport(output / "audit", epoch=99, revision=2, pointer=0x20000000, **kwargs)
            with SessionTrace(output / "trace.jsonl") as trace, Client(self.actual, trace=trace) as client:
                identity = identity_from_launcher(client.hello(), ARTIFACTS)
                client.observe()
                client.request("prepare_render", expect=client.version)
                yield ReplaySession(client, identity, output / "audit")
                client.request("stop_recording", expect=client.version)
        return initialize

    def test_batch_zero_terminal_preserves_three_calls_two_ticks_and_all_states(self):
        trajectory = self.bundle(self.source())
        self.assertEqual(len(trajectory.audit.frames), 6)
        result = trajectory.steps[-1]["result"]
        self.assertEqual((result["executed_ticks"], result["executed_engine_calls"]), (2, 3))
        report = replay(trajectory, self.initializer(), self.root / "actual")
        self.assertTrue(report["equal"])
        self.assertEqual(report["engine_calls"]["terminal_zero_calls_compared"], 1)
        self.assertEqual(report["engine_calls"]["returned_calls_compared"], 3)
        self.assertEqual(report["draw_schedule"]["step_receipts_compared"], 2)
        self.assertTrue(report["engine_calls"]["final_health_verified"])

    def test_zero_terminal_with_actions_keeps_same_tick_and_revision(self):
        trajectory = self.bundle(self.source(zero_tick=0, actions=[{"op": "plant", "row": 1, "col": 1}]))
        frames = list(trajectory.audit.frames)
        self.assertEqual(frames[0].version, frames[1].version)
        self.assertNotEqual(frames[0].state, frames[1].state)
        self.assertEqual(trajectory.steps[0]["result"]["executed_ticks"], 0)
        report = replay(trajectory, self.initializer(zero_tick=0), self.root / "actual")
        self.assertTrue(report["equal"])

    def test_negotiated_terminal_clock_step_keeps_existing_semantics(self):
        trajectory = self.bundle(self.source(clock_terminal=True))
        report = replay(trajectory, self.initializer(clock_terminal=True), self.root / "actual")
        self.assertTrue(report["equal"])
        self.assertEqual(report["engine_calls"]["clock_steps_compared"], 3)
        self.assertEqual(report["engine_calls"]["terminal_zero_calls_compared"], 0)
        self.assertEqual(report["draw_schedule"]["step_receipts_compared"], 2)
        with self.assertRaises(ValueError):
            replay(trajectory, self.initializer(clock_terminal=True), self.root / "no-takeover", target_tick=3, on_takeover=lambda *_: None)

    def test_seek_at_zero_terminal_end_truncates_batch_and_can_take_over(self):
        for split in (False, True):
            actions = [{"op": "plant", "row": 1, "col": 1}]
            trajectory = self.bundle(self.source(str(split), split=split, actions=actions))
            taken = []
            report = replay(trajectory, self.initializer(), self.root / f"actual-{split}", target_tick=2,
                            on_takeover=lambda session, link: taken.append((session.client.observation, link)))
            self.assertTrue(report["equal"])
            self.assertEqual(report["engine_calls"]["returned_calls_compared"], 2)
            self.assertEqual(report["engine_calls"]["terminal_zero_calls_compared"], 0)
            self.assertEqual(taken[0][0]["game_ui"], 3)
            self.assertFalse(taken[0][1]["terminal_consumed"])
            committed = [request for request in self.actual.requests if request["method"] == "commit"]
            self.assertEqual(len(committed), 0 if split else 1)

    def test_seek_zero_does_not_execute_terminal_request_actions(self):
        trajectory = self.bundle(self.source(zero_tick=0, actions=[{"op": "plant"}]))
        report = replay(trajectory, self.initializer(zero_tick=0), self.root / "actual", target_tick=0, on_takeover=lambda *_: None)
        self.assertTrue(report["equal"])
        self.assertEqual(report["engine_calls"]["returned_calls_compared"], 0)
        self.assertFalse(any(request["method"] == "commit" for request in self.actual.requests))

    def test_terminal_state_difference_fails_after_normal_prefix(self):
        trajectory = self.bundle(self.source())
        with self.assertRaises(ReplayDivergence):
            replay(trajectory, self.initializer(call_fault="terminal_state"), self.root / "actual")
        report = json.loads((self.root / "actual" / "replay-report.json").read_text())
        self.assertFalse(report["equal"])

    def test_call_metadata_and_raw_corruption_fail_closed(self):
        for fault in ("missing_return", "wrong_id", "wrong_index", "wrong_clock_fact", "raw_pointer", "raw_id", "missing_transition"):
            with self.subTest(fault=fault):
                directory = self.source(fault, call_fault=fault)
                with self.assertRaises(EvidenceError):
                    self.bundle(directory)

    def test_same_tick_live_fight_or_unsupported_terminal_cannot_qualify(self):
        for fault in ("live_zero", "unsupported_ui", "negative_clock", "double_clock"):
            with self.subTest(fault=fault):
                # Client may reject impossible response counts before packaging;
                # native evidence must independently fail the strict reader.
                try:
                    directory = self.source(fault, call_fault=fault)
                except Exception:
                    directory = self.root / fault
                with self.assertRaises(EvidenceError):
                    AuditLog(directory / "audit")

    def test_sidecar_required_and_old_mode_cannot_adopt_zero_call(self):
        source = self.source()
        (source / "audit" / ENGINE_CALL_RAW).unlink()
        with self.assertRaisesRegex(EvidenceError, "raw engine call"):
            self.bundle(source)
        source = self.source("legacy")
        path = source / "audit" / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest.pop("engine_call_boundary")
        path.write_text(json.dumps(manifest))
        with self.assertRaises(EvidenceError):
            AuditLog(source / "audit")

    def test_terminal_hooks_keep_call_ownership_and_raw_particle_id(self):
        trajectory = self.bundle(self.source(spawn_schedule={0: [11], 2: [22]}, particle_pointer=0x10000140, animation_handle=65538))
        report = replay(trajectory, self.initializer(spawn_schedule={0: [11], 2: [22]}, particle_pointer=0x20000140,
            animation_handle=131074), self.root / "actual")
        self.assertTrue(report["equal"])
        self.assertEqual(report["spawn_events_compared"], 2)
        self.assertEqual(report["particle_shake"]["semantic_seed_calls_compared"], 3)
        bad = self.source("bad-hook", spawn_schedule={0: [11]}, animation_handle=65538, call_fault="wrong_hook")
        with self.assertRaisesRegex(EvidenceError, "hook engine call identity"):
            self.bundle(bad)

    def test_zero_post_is_not_yielded_until_terminal_completion(self):
        source = self.source(zero_tick=0, call_fault="missing_transition")
        stream = AuditTail(source / "audit").read_request(next(json.loads(line)["payload"]["request_id"]
            for line in (source / "audit" / "events.jsonl").read_text().splitlines()
            if json.loads(line)["kind"] == "request_started"))
        self.assertEqual(next(stream).kind, "pre_step")
        with self.assertRaises(EvidenceError):
            next(stream)

    def test_final_health_and_packaged_raw_hash_are_required(self):
        source = self.source()
        path = source / "audit" / "events.jsonl"
        records = [json.loads(line) for line in path.read_text().splitlines()]
        next(record for record in records if record["kind"] == "engine_call_closed")["payload"]["returned_calls"] += 1
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
        with self.assertRaisesRegex(EvidenceError, "engine call close"):
            self.bundle(source)
        trajectory = self.bundle(self.source("hash"))
        raw = trajectory.directory / "audit" / ENGINE_CALL_RAW
        raw.write_bytes(raw.read_bytes() + b" ")
        with self.assertRaises(EvidenceError):
            replay(trajectory, self.initializer(), self.root / "never-started")

    def test_ordinary_batch_tool_checks_calls_but_allows_request_local_index(self):
        left = self.bundle(self.source("single", zero_tick=None, singles=True))
        right = self.bundle(self.source("batch", zero_tick=None))
        compare = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools" / "check-boundary-equivalence.py"))["compare"]
        report = compare(left.directory, right.directory, purpose="batch", ticks=3)
        self.assertTrue(report["passed"])
        self.assertEqual(report["engine_calls"]["returned_calls_compared"], 3)
        zero = self.bundle(self.source("zero"))
        with self.assertRaisesRegex(EvidenceError, "zero-clock terminals"):
            compare(zero.directory, zero.directory, purpose="schedule", ticks=2)

    def test_client_legacy_zero_response_and_negotiated_counter_shapes(self):
        legacy = {"requested_ticks": 1, "executed_ticks": 0, "stop_reason": "scene_changed", "observation": {}, "action_results": []}
        _completed_step("commit", legacy)
        with self.assertRaises(ProtocolError):
            _completed_step("commit", legacy, engine_calls=True)
        good = dict(legacy, executed_engine_calls=1, terminal_zero_clock_calls=1, last_engine_call_id=1,
                    terminal_kind="terminal_zero_clock_update")
        _completed_step("commit", good, engine_calls=True)
        for change in ({"executed_engine_calls": 0}, {"terminal_zero_clock_calls": True}, {"last_engine_call_id": None}, {"requested_ticks": 0}):
            with self.subTest(change=change), self.assertRaises(ProtocolError):
                _completed_step("commit", dict(good, **change), engine_calls=True)


if __name__ == "__main__":
    unittest.main()
