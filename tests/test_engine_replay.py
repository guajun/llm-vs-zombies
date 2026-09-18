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

from llm_vs_zombies.audit_compare import (AuditTail, SCHEMA, EvidenceError, canonical, digests, file_hash,
                                        PARTICLE_SHAKE_MODE, _particle_digest)
from llm_vs_zombies.client import Client, OutcomeUnknown, RemoteError
from llm_vs_zombies.engine_replay import (ReplayDivergence, ReplaySession, Trajectory,
    build_trajectory, capture_initial, identity_from_launcher, replay)
from llm_vs_zombies.session import SessionTrace


GAME = {"schema": SCHEMA, "target": "synthetic-test-only", "loaded_signatures_match": True,
        "coverage": {"complete_game_state": False}, "original_engine_replay_verified": False}
BUILD = {"runtime_protocol": 1, "avz_commit": "synthetic-test-only", "pointer_bits": 32}
ARTIFACTS = {"input_hashes": {"game": "1" * 64}, "module_hashes": {"runtime": "2" * 64},
             "profile_hashes": {"profile": "3" * 64}}
PARTICLE_MODE = {"mode": PARTICLE_SHAKE_MODE, "installed": True, "original_engine_bitwise_unmodified": False,
                 "semantic_change": "test verified ID substitution", "raw_evidence": "particle-shake-seeds.jsonl"}


class ModelTransport:
    def __init__(self, directory, *, epoch=1, revision=0, divergence=None, wrong_result=False, terminal_tick=None,
                 pixel_byte=1, wrong_capture_guard=False, capture_timeout=False, animation_handle=None,
                 particle_pointer=None, particle_id=65538, particle_overflow=False, pause_changes_state=False, status_pending=False,
                 deny_pause=False, spawn_schedule=None, initialization_spawns=0, spawn_overflow=False, late_spawn=False):
        self.directory = directory
        directory.mkdir(parents=True)
        self.game, self.animation_handle = copy.deepcopy(GAME), animation_handle
        self.particle_pointer, self.particle_id, self.particle_overflow = particle_pointer, particle_id, particle_overflow
        self.particle_count, self.particle_digest = 0, 14695981039346656037
        self.pause_changes_state, self.status_pending = pause_changes_state, status_pending
        self.deny_pause = deny_pause
        self.spawn_schedule = spawn_schedule
        self.spawn_count, self.spawn_overflow, self.late_spawn = 0, spawn_overflow, late_spawn
        if spawn_schedule is not None:
            self.game["spawn_hook"] = {"installed": True, "semantic": "exact_initializer_exit"}
        if particle_pointer is not None:
            self.game["particle_shake"] = PARTICLE_MODE
        if animation_handle is not None:
            self.game["coverage"]["reanimations"] = {"raw_handle_evidence": {
                "path": "audit/reanimation-handles.jsonl", "encoding": "initial_plus_json_patch",
                "binding": ["seq", "kind", "version"], "required": True}}
        (directory / "manifest.json").write_text(json.dumps(self.game), encoding="utf-8")
        self.files = {name: (directory / name).open("w", encoding="utf-8", newline="\n")
                      for name in ("events.jsonl", "checksums.jsonl", "state-deltas.jsonl")}
        if animation_handle is not None:
            self.files["reanimation-handles.jsonl"] = (directory / "reanimation-handles.jsonl").open("w", encoding="utf-8", newline="\n")
        if particle_pointer is not None:
            self.files["particle-shake-seeds.jsonl"] = (directory / "particle-shake-seeds.jsonl").open("w", encoding="utf-8", newline="\n")
        self.seq, self.tick, self.revision, self.epoch, self.sun = 0, 0, revision, epoch, 500
        self.previous, self.divergence, self.wrong_result = None, divergence, wrong_result
        self.requests = []
        self.closed = False
        self.terminal_tick, self.game_ui = terminal_tick, 3
        self.draw_count, self.rng_delta = 0, 0
        self.pixel_byte, self.wrong_capture_guard, self.capture_timeout = pixel_byte, wrong_capture_guard, capture_timeout
        for preview in range(initialization_spawns):
            self.spawn_call(100 + preview, initialization=True)

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
        if self.particle_pointer is not None:
            state["particle_shake"] = {"mode": PARTICLE_SHAKE_MODE, "controlled_calls": self.particle_count,
                                       "controlled_digest": self.particle_digest}
        return state

    def particle_call(self):
        identity, pointer, factor = self.particle_id, self.particle_pointer, self.tick + 1
        payload = {"mode": PARTICLE_SHAKE_MODE, "ordinal": self.particle_count, "callsite_rva": 0x116ba5,
            "particle_id": identity, "slot": 2, "generation": identity >> 16, "age": factor, "duration": 100000, "crossfade_duration": 0,
            "factor": factor, "canonical_seed": (identity * factor) & 0xffffffff, "pool_verified": True,
            "control_phase": "pre_step", "pool": {"used": 3, "capacity": 4, "count": 1, "free_head": 3, "next_key": 2}}
        event = {"schema": SCHEMA, "seq": self.seq, "kind": "particle_shake_seed", "phase": "controlled_boundary",
                 "native_phase": "before_srand", "version": self.version(), "payload": payload}
        self.seq += 1
        self.write("events.jsonl", event)
        self.write("particle-shake-seeds.jsonl", dict(event, schema="lvz.particle-shake-raw.v1", payload=dict(payload,
            particle_address=pointer, emitter_address=100, system_address=200, holder_address=300,
            pool_block_address=pointer - 2 * 0xa0, original_seed=(pointer * factor) & 0xffffffff)))
        self.particle_count += 1
        self.particle_digest = _particle_digest(self.particle_digest, payload)

    def write(self, name, value):
        self.files[name].write(json.dumps(value, separators=(",", ":")) + "\n")
        self.files[name].flush()

    def spawn_call(self, label, *, initialization=False):
        overrides = label if isinstance(label, dict) else {}
        label = overrides.get("label", 11) if overrides else label
        boundary = None if initialization else {"segment": self.epoch, "tick": self.tick, "revision": self.revision}
        fields = {offset: 0 for offset in ("00000118", "00000140", "00000144", "00000150")}
        fields["0000002c"] = overrides.get("x_bits", 0x3f800000)
        payload = {"schema": "lvz.spawn.v1", "kind": "zombie_initialized", "phase": "zombie_initialize_exit",
            "boundary": boundary, "ordinal": self.spawn_count, "id": 65536 + label, "slot": label, "generation": 1,
            "caller_rva": 0x1234, "inputs": {"row0": 2, "type": 0, "variant_byte": 0, "wave_raw": 0, "parent_id": None},
            "initial": {"raw_scalar_fields": fields, "row0": 2, "x_bits": fields["0000002c"], "speed_bits": 123,
                        "variant": overrides.get("variant", 0)},
            "game_clock_before": self.tick, "game_clock_after": self.tick,
            "global_mt_before": {"words": [1, 2], "cursor": overrides.get("rng_cursor", 0)},
            "global_mt_after": {"words": [1, 2], "cursor": 1}}
        self.write("events.jsonl", {"schema": SCHEMA, "kind": "zombie_initialized", "seq": self.seq,
            "native_phase": "zombie_initialize_exit", "phase": "initialization" if initialization else "controlled_boundary",
            "version": None if initialization else self.version(), "payload": payload})
        self.seq += 1
        self.spawn_count += 1

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
            if self.deny_pause:
                result["capabilities"]["pause"] = False
        elif method == "observe":
            result = self.observe()
        elif method == "audit_snapshot":
            result = {"state": self.state(), "version": self.version()}
        elif method == "status":
            result = {"state": "stepping" if self.status_pending else "paused_at_boundary", "fault": None,
                      "version": self.version(), "pending_request_id": "other" if self.status_pending else None,
                      "executed_ticks": 0}
        elif method == "pause":
            if self.pause_changes_state:
                self.draw_count += 1
            result = {"state": "paused_at_boundary", "observation": self.observe()}
        elif method == "stop_recording":
            if self.late_spawn:
                self.spawn_call(99)
            self.event("recording_closed", {"request_id": rid})
            if self.particle_pointer is not None:
                self.event("particle_shake_closed", {"installed": True, "captured": self.particle_count,
                    "controlled_calls": self.particle_count, "queued": 0, "wrong_thread_calls": 0,
                    "faults": 0, "overflow": int(self.particle_overflow), "healthy": not self.particle_overflow})
            if self.spawn_schedule is not None:
                self.event("spawn_hook_closed", {"installed": True, "captured": self.spawn_count,
                    "queued": 0, "wrong_thread_calls": 0, "active_initializers": 0,
                    "faults": 0, "overflow": int(self.spawn_overflow), "healthy": not self.spawn_overflow})
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
                    for birth in (self.spawn_schedule or {}).get(self.tick, []):
                        self.spawn_call(birth)
                    if self.particle_pointer is not None:
                        self.particle_call()
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
                    wrong_capture_guard=False, capture_timeout=False, wrong_particle_id=False, particle_overflow=False,
                    pause_changes_state=False, status_pending=False, deny_pause=False, spawn_schedule=None,
                    initialization_spawns=0, spawn_overflow=False, late_spawn=False):
        @contextmanager
        def initialize(trajectory, output):
            self.live = ModelTransport(output / "audit", epoch=99, revision=2,
                                       divergence=divergence, wrong_result=wrong_result, terminal_tick=terminal_tick,
                                       pixel_byte=2, wrong_capture_guard=wrong_capture_guard, capture_timeout=capture_timeout,
                                       animation_handle=131074 if "reanimations" in trajectory.audit.manifest.get("coverage", {}) else None,
                                       particle_pointer=0x20000140 if trajectory.audit.manifest.get("particle_shake") else None,
                                       particle_id=131074 if wrong_particle_id else 65538, particle_overflow=particle_overflow,
                                       pause_changes_state=pause_changes_state, status_pending=status_pending, deny_pause=deny_pause,
                                       spawn_schedule=spawn_schedule, initialization_spawns=initialization_spawns,
                                       spawn_overflow=spawn_overflow, late_spawn=late_spawn)
            identity = copy.deepcopy(self.identity)
            if wrong_identity:
                identity["artifacts"]["module_hashes"]["runtime"] = "4" * 64
            with SessionTrace(output / "actual.jsonl") as trace, Client(self.live, trace=trace) as client:
                yield ReplaySession(client, identity, output / "audit")
                if self.live.particle_pointer is not None or self.live.spawn_schedule is not None:
                    client.request("stop_recording", expect=client.version)
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

    def spawn_recording(self):
        source = self.root / "spawn-source"
        source.mkdir()
        model = ModelTransport(source / "audit", animation_handle=65538,
                               spawn_schedule={0: [11, 22], 1: [33]}, initialization_spawns=1)
        with SessionTrace(source / "session.jsonl") as trace, Client(model, trace=trace) as client:
            self.identity = identity_from_launcher(client.hello(), ARTIFACTS)
            capture_initial(client, identity=self.identity, initialization={"synthetic": True})
            client.advance(2)
            client.request("stop_recording", expect=client.version)
        return build_trajectory(source / "session.jsonl", source / "audit", self.root / "spawn-bundle")

    def test_spawn_replay_maps_epoch_retains_order_and_separates_preview_history(self):
        trajectory = self.spawn_recording()
        result = replay(trajectory, self.initializer(spawn_schedule={0: [11, 22], 1: [33]},
                                                     initialization_spawns=3), self.root / "spawn-live")
        self.assertTrue(result["equal"])
        self.assertEqual(result["spawn_events_compared"], 3)
        self.assertTrue(result["spawn_exercised"])
        spawn = result["spawn_comparison"]
        self.assertEqual(spawn["source_initialization_history"], 1)
        self.assertEqual(spawn["actual_initialization_history"], 3)
        self.assertFalse(spawn["initialization_history_compared"])
        self.assertTrue(spawn["initial_state_compared"])
        self.assertTrue(spawn["final_health_verified"])

    def test_spawn_initializer_exit_changes_fail_even_when_every_final_state_matches(self):
        trajectory = self.spawn_recording()
        for number, (field, changed, path) in enumerate((
                ("position", {"x_bits": 0x40000000}, "/payload/initial/raw_scalar_fields/0000002c"),
                ("random_variant", {"variant": 1}, "/payload/initial/variant"),
                ("rng_before", {"rng_cursor": 17}, "/payload/global_mt_before/cursor"))):
            with self.subTest(field=field):
                schedule = {0: [11, dict(label=22, **changed)], 1: [33]}
                with self.assertRaises(ReplayDivergence) as caught:
                    replay(trajectory, self.initializer(spawn_schedule=schedule), self.root / f"spawn-change-{number}")
                detail = caught.exception.report
                self.assertEqual(detail["stage"], "audit[0:1].spawn[1]")
                self.assertEqual(detail["controlled_ordinal"], 1)
                self.assertEqual(detail["difference"]["path"], path)
                self.assertEqual(trajectory.audit.frames[-1].state, self.live.previous)

    def test_spawn_missing_extra_and_reordered_events_are_independent_failures(self):
        trajectory = self.spawn_recording()
        for label, schedule, birth_index in (("missing", {0: [11], 1: [33]}, 1),
                                             ("extra", {0: [11, 22, 44], 1: [33]}, 2),
                                             ("order", {0: [22, 11], 1: [33]}, 0)):
            with self.subTest(label=label), self.assertRaises(ReplayDivergence) as caught:
                replay(trajectory, self.initializer(spawn_schedule=schedule), self.root / f"spawn-{label}")
            self.assertEqual(caught.exception.report["stage"], f"audit[0:1].spawn[{birth_index}]")
            self.assertEqual(trajectory.audit.frames[-1].state, self.live.previous)

    def test_spawn_seek_compares_only_recomputed_prefix_and_requires_healthy_close(self):
        trajectory = self.spawn_recording()
        schedule = {0: [11, 22], 1: [33]}
        result = replay(trajectory, self.initializer(spawn_schedule=schedule), self.root / "spawn-seek", target_tick=1)
        self.assertEqual(result["spawn_events_compared"], 2)
        self.assertEqual(result["spawn_comparison"]["source_controlled_total"], 3)
        self.assertEqual(result["spawn_comparison"]["scope"], "executed_prefix")
        for label, options, failure in (("overflow", {"spawn_overflow": True}, "unhealthy or incomplete"),
                                        ("late", {"late_spawn": True}, "no following audited boundary")):
            with self.subTest(label=label), self.assertRaisesRegex(EvidenceError, failure):
                replay(trajectory, self.initializer(spawn_schedule=schedule, **options), self.root / f"spawn-close-{label}")
            report = json.loads((self.root / f"spawn-close-{label}" / "replay-report.json").read_text())
            self.assertFalse(report["equal"])

    def test_seek_source_suffix_tamper_is_rejected_before_success_or_takeover(self):
        base = self.initializer()
        taken_over = []

        @contextmanager
        def mutate_during_seek(trajectory, output):
            with base(trajectory, output) as session:
                original = self.live.exchange

                def exchange(payload, timeout):
                    result = original(payload, timeout)
                    if json.loads(payload)["method"] == "audit_snapshot" and self.live.tick == 2:
                        path = trajectory.audit.directory / "events.jsonl"
                        # Modify the unread footer after the partial request
                        # was checked, while the source generator is suspended.
                        with path.open("ab") as stream:
                            stream.write(b"\n")
                    return result

                self.live.exchange = exchange
                yield session

        output = self.root / "seek-tamper"
        with self.assertRaises(EvidenceError):
            replay(self.trajectory, mutate_during_seek, output, target_tick=2,
                   on_takeover=lambda *args: taken_over.append(args))
        self.assertFalse(json.loads((output / "replay-report.json").read_text())["equal"])
        self.assertEqual(taken_over, [])

    def animation_recording(self):
        source = self.root / "animation-source"
        model = ModelTransport(source / "audit", animation_handle=65538)
        with SessionTrace(source / "session.jsonl") as trace, Client(model, trace=trace) as client:
            self.identity = identity_from_launcher(client.hello(), ARTIFACTS)
            capture_initial(client, identity=self.identity, initialization={"synthetic": True})
            client.advance(2)
            client.request("stop_recording", expect=client.version)
        return build_trajectory(source / "session.jsonl", source / "audit", self.root / "animation-bundle")

    def particle_recording(self):
        source = self.root / "particle-source"
        model = ModelTransport(source / "audit", particle_pointer=0x10000140)
        with SessionTrace(source / "session.jsonl") as trace, Client(model, trace=trace) as client:
            self.identity = identity_from_launcher(client.hello(), ARTIFACTS)
            capture_initial(client, identity=self.identity, initialization={"seed": 42, "synthetic": True})
            client.advance(2)
            client.advance(1)
            client.request("stop_recording", expect=client.version)
        return build_trajectory(source / "session.jsonl", source / "audit", self.root / "particle-bundle")

    def pause_recording(self, *, snapshot=True, side_effect=False):
        source = self.root / "pause-source"
        model = ModelTransport(source / "audit", pause_changes_state=side_effect)
        with SessionTrace(source / "session.jsonl") as trace, Client(model, trace=trace) as client:
            capture_initial(client, identity=self.identity, initialization={"synthetic": True})
            client.advance(2)
            client.request("pause")  # Historical runtime contract does not require expect.
            if snapshot:
                client.request("audit_snapshot")
            client.request("stop_recording", expect=client.version)
        return source

    def test_completed_pause_is_replayed_and_proved_noop_with_actual_snapshots(self):
        source = self.pause_recording()
        trajectory = build_trajectory(source / "session.jsonl", source / "audit", self.root / "pause-bundle")
        self.assertEqual(trajectory.steps[-1]["request"]["method"], "pause")
        result = replay(trajectory, self.initializer(), self.root / "pause-actual")
        self.assertTrue(result["equal"])
        self.assertEqual(result["pause_controls"], {"recorded": 1, "executed": 1, "verified_noop": 1})
        self.assertTrue(any(request["method"] == "pause" for request in self.live.requests))
        seek = replay(trajectory, self.initializer(), self.root / "pause-seek", target_tick=2)
        self.assertEqual(seek["pause_controls"]["executed"], 0)

    def test_pause_without_snapshot_or_with_source_side_effect_is_rejected(self):
        for snapshot, effect, reason in ((False, False, "post-control audit snapshot"), (True, True, "changed the captured")):
            with self.subTest(snapshot=snapshot, effect=effect):
                source = self.pause_recording(snapshot=snapshot, side_effect=effect)
                with self.assertRaisesRegex(EvidenceError, reason):
                    build_trajectory(source / "session.jsonl", source / "audit", self.root / "rejected-pause")
                # Keep independent real traces while allowing this fixture helper's fixed name.
                source.rename(self.root / ("pause-source-saved-" + str(snapshot)))

    def test_pause_refuses_pending_work_and_detects_actual_hidden_side_effect(self):
        source = self.pause_recording()
        trajectory = build_trajectory(source / "session.jsonl", source / "audit", self.root / "pause-bundle")
        with self.assertRaisesRegex(EvidenceError, "explicitly denies"):
            replay(trajectory, self.initializer(deny_pause=True), self.root / "pause-denied")
        self.assertFalse(any(request["method"] == "pause" for request in self.live.requests))
        with self.assertRaisesRegex(EvidenceError, "without pending work"):
            replay(trajectory, self.initializer(status_pending=True), self.root / "pause-busy")
        self.assertFalse(any(request["method"] == "pause" for request in self.live.requests))
        with self.assertRaises(ReplayDivergence) as caught:
            replay(trajectory, self.initializer(pause_changes_state=True), self.root / "pause-changed")
        self.assertIn("state_unchanged", caught.exception.report["stage"])

    def test_particle_mode_replays_all_seed_calls_with_epoch_mapping_and_raw_binding(self):
        trajectory = self.particle_recording()
        key = "audit/particle-shake-seeds.jsonl"
        self.assertEqual(trajectory.manifest["files"][key], file_hash(trajectory.directory / key))
        result = replay(trajectory, self.initializer(), self.root / "particle-replayed")
        self.assertTrue(result["equal"])
        self.assertEqual(result["particle_shake"]["mode"], PARTICLE_SHAKE_MODE)
        self.assertEqual(result["particle_shake"]["semantic_seed_calls_compared"], 3)
        self.assertTrue(result["particle_shake"]["final_health_verified"])
        self.assertFalse(result["particle_shake"]["raw_pointer_seeds_compared"])
        self.assertNotEqual(file_hash(trajectory.directory / key), file_hash(self.root / "particle-replayed" / key))
        with (trajectory.directory / key).open("a") as stream:
            stream.write("{}\n")
        with self.assertRaisesRegex(EvidenceError, "SHA-256"):
            Trajectory.load(trajectory.directory)

    def test_particle_changed_id_fails_even_when_original_addresses_are_not_compared(self):
        trajectory = self.particle_recording()
        with self.assertRaises(ReplayDivergence) as caught:
            replay(trajectory, self.initializer(wrong_particle_id=True), self.root / "particle-id-diverged")
        self.assertIn("particle_shake", caught.exception.report["stage"])

    def test_particle_final_overflow_fails_instead_of_publishing_equal(self):
        trajectory = self.particle_recording()
        with self.assertRaisesRegex(EvidenceError, "health/count"):
            replay(trajectory, self.initializer(particle_overflow=True), self.root / "particle-overflow")
        report = json.loads((self.root / "particle-overflow/replay-report.json").read_text())
        self.assertFalse(report["equal"])
        self.assertFalse(report["particle_shake"]["final_health_verified"])

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
