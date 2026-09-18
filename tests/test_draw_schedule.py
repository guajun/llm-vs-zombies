"""Controlled-draw evidence tests use a simulator, not an original game process."""
import base64
import copy
import hashlib
from contextlib import contextmanager
import json
from pathlib import Path
import runpy
import tempfile
import unittest

from llm_vs_zombies.audit_compare import (AuditLog, EvidenceError, SCHEMA, DRAW_SCHEDULE_MODE,
                                        _seeded_rng, canonical, digests)
from llm_vs_zombies.client import Client
from llm_vs_zombies.engine_replay import (ReplayDivergence, ReplaySession, build_trajectory,
                                        capture_initial, identity_from_launcher, replay)
from llm_vs_zombies.session import SessionTrace
from test_engine_replay import ModelTransport, ARTIFACTS


DRAW_SPEC = {"mode": DRAW_SCHEDULE_MODE, "installed": True, "original_engine_bitwise_unmodified": False,
    "target_entry_rva": 0x138eb0, "schedule": "one warm draw before B0; one original draw after each verified update before post_step",
    "autonomous_fight_draws": False, "capture": "cached_bgr24_only", "rng_restore_after_draw": False, "live_verified": False}


class DrawTransport(ModelTransport):
    def __init__(self, directory, *, fault=None, diagnostic=10, draw_births=False, **kwargs):
        super().__init__(directory, **kwargs)
        self.game["draw_schedule"] = copy.deepcopy(DRAW_SPEC)
        (directory / "manifest.json").write_text(json.dumps(self.game), encoding="utf-8")
        self.warm_frames = self.step_frames = 0
        self.rng = _seeded_rng(42)
        self.fault, self.diagnostic, self.draw_births = fault, diagnostic, draw_births
        self.pre_version = None

    def counts(self):
        return {"mode": DRAW_SCHEDULE_MODE, "warm_frames": self.warm_frames, "step_frames": self.step_frames}

    def clocks(self):
        return {"schema": SCHEMA, "target": self.game["target"], "game_clock": self.tick,
                "effect_clock": 100 + self.tick, "mj_clock": 200 + self.tick}

    def observe(self):
        return dict(super().observe(), render_prepared=self.warm_frames == 1)

    def state(self):
        value = super().state()
        value["board"].update({"00005568": self.tick, "0000556c": 100 + self.tick})
        value["app"] = {"ui": self.game_ui, "mj_clock": 200 + self.tick}
        value["rng"] = {"target": self.game["target"], "instances": copy.deepcopy(self.rng)}
        value["draw_schedule"] = self.counts()
        return value

    def receipt(self, warm=False):
        before = digests(self.rng)
        # Rendering really consumes a scalar RNG value in this fixture. The
        # state and receipt must retain it, rather than restoring the seed.
        self.rng["game_thread_crt"]["state"] = (self.rng["game_thread_crt"]["state"] * 214013 + 2531011) & 0xffffffff
        after = digests(self.rng)
        value = {"schema": "lvz.controlled-render.v1", "mode": DRAW_SCHEDULE_MODE,
            "phase": "warm" if warm else "step", "frame_version": self.version(), "native_clock": self.tick,
            "width": 800, "height": 600, "pixel_format": "bgr24", "clocks_before": self.clocks(), "clocks_after": self.clocks(),
            "rng_before": before, "rng_after": after, "rng_unchanged": before == after, "rng_restored": False, "counts": self.counts()}
        if warm:
            value["seed_readback"] = {"seed": 42, "global_mt_words": 624, "global_mt_cursor": 624,
                                      "game_thread_crt": 42, "verified_before_draw": True}
        if self.fault == "receipt_rng" and not warm:
            value["rng_before"]["all"] = "f" * 16
        return value

    def event(self, kind, payload):
        payload = copy.deepcopy(payload)
        if kind == "pre_step":
            self.pre_version = self.version()
        if kind == "post_step":
            if self.draw_births:
                current = self.version()
                self.epoch, self.tick, self.revision = (self.pre_version[k] for k in ("epoch", "tick", "revision"))
                self.spawn_call(11)
                if self.particle_pointer is not None:
                    self.particle_call()
                self.epoch, self.tick, self.revision = (current[k] for k in ("epoch", "tick", "revision"))
            if self.game_ui != 3 or self.fault == "fake_terminal":
                payload["render"] = {"schema": "lvz.controlled-render.v1", "mode": DRAW_SCHEDULE_MODE,
                    "phase": "terminal", "skipped": True, "reason": "left_ready_fight", "frame_version": self.version(),
                    "native_clock": self.tick, "cache_invalidated": True}
            else:
                self.step_frames += 1
                payload["render"] = self.receipt()
            if self.fault == "missing":
                payload.pop("render")
            elif self.fault == "duplicate_count":
                payload["render"]["counts"]["step_frames"] += 1
            elif self.fault == "wrong_version":
                payload["render"]["frame_version"]["tick"] += 1
            elif self.fault == "wrong_clock":
                payload["render"]["native_clock"] += 1
            elif self.fault == "misplaced":
                super().event("render_prepared", {"request_id": payload["request_id"], "render": payload["render"]})
        if self.fault == "missing_terminal" and kind == "terminal_transition":
            return
        if self.fault == "zero_delta" and kind == "post_step":
            payload["native_tick_delta"] = 0
        super().event(kind, payload)
        if kind == "post_step" and self.game_ui != 3 and self.fault == "terminal_auxiliary":
            self.write("events.jsonl", {"schema": SCHEMA, "seq": self.seq, "kind": "zombie_first_boundary_observed",
                                       "version": self.version(), "payload": {"exact_spawn": False}})
            self.seq += 1
        if kind == "recording_closed":
            health = dict(self.counts(), installed=True, latched=True, sealed=True, active=False,
                          automatic_allowed=2, automatic_denied=self.diagnostic,
                          controlled_calls=self.warm_frames + self.step_frames, faults=0, wrong_thread_calls=0, healthy=True)
            if self.fault == "bad_health":
                health["faults"] = 1
            super().event("draw_schedule_closed", health)

    def exchange(self, payload, timeout):
        request = json.loads(payload)
        method, rid = request["method"], request["request_id"]
        if method == "prepare_render":
            self.requests.append(request)
            self.revision += 1
            self.event("render_preparing", {"request_id": rid})
            self.warm_frames += 1
            receipt = self.receipt(warm=True)
            if self.fault == "warm_seed":
                receipt["seed_readback"]["seed"] += 1
            if self.fault == "warm_missing":
                return json.dumps({"protocol": 1, "request_id": rid, "ok": True,
                    "result": {"prepared": True, "observation": self.observe()}}).encode()
            self.event("render_prepared", {"request_id": rid, "render": receipt})
            if self.fault == "warm_duplicate":
                self.event("render_prepared", {"request_id": rid, "render": receipt})
            result = {"prepared": True, "render": receipt, "observation": self.observe()}
        elif method == "capture_frame":
            self.requests.append(request)
            result = {"capture_ok": request["params"].get("outcome") != "unavailable", "version": self.version(), "forced_render": False}
            if result["capture_ok"]:
                result.update(mode=DRAW_SCHEDULE_MODE, method="cached_controlled_engine_frame", frame_version=self.version(),
                    source="original_game_frame", width=800, height=600, pixel_format="bgr24", row_stride=2400, origin="top_left",
                    used_3d=False, known_rng_unchanged=True, game_clock_before=self.tick, game_clock_after=self.tick,
                    pixels_base64=base64.b64encode(bytes([self.pixel_byte]) * 1440000).decode())
                if self.fault == "stale_capture":
                    result["frame_version"]["tick"] += 1
                if self.fault == "forced_capture":
                    result["forced_render"] = True
                if self.fault == "capture_mutation":
                    self.sun += 1
            else:
                result["reason"] = "synthetic unavailable cache"
        else:
            response = json.loads(super().exchange(payload, timeout))
            if method == "hello":
                response["result"]["capabilities"].update(prepare_render=True, deterministic_draw_schedule_v1=True)
            return json.dumps(response).encode()
        return json.dumps({"protocol": 1, "request_id": rid, "ok": True, "result": result}).encode()


class DrawScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def source(self, name="source", *, count=3, captures=False, singles=False, actions=False, **kwargs):
        directory = self.root / name
        model = DrawTransport(directory / "audit", **kwargs)
        with SessionTrace(directory / "session.jsonl") as trace, Client(model, trace=trace) as client:
            identity = identity_from_launcher(client.hello(), ARTIFACTS)
            client.observe()
            client.request("prepare_render", expect=client.version)
            initial = capture_initial(client, identity=identity,
                initialization={"seed": 42, "execution_mode": DRAW_SCHEDULE_MODE, "draw_schedule": DRAW_SPEC,
                    "clock_anchor": model.clocks(), "postwarm_clock": model.clocks(),
                    "render_preparation": {"warm_frames": 1, "step_frames": 0, "verified_before_draw": True,
                        "seeded_rng_sha256": hashlib.sha256(canonical(_seeded_rng(42))).hexdigest(),
                        "postwarm_rng_sha256": hashlib.sha256(canonical(model.rng)).hexdigest()}})
            if captures:
                client.request("capture_frame", expect=client.version)
                client.request("capture_frame", {"outcome": "unavailable"}, expect=client.version)
                client.request("audit_snapshot")
            if actions:
                client.commit([{"op": "plant", "row": 1, "col": 2}], advance_ticks=0)
            for _ in range(count if singles else 1):
                client.advance(1 if singles else count)
            if client.version["epoch"] != model.epoch:
                client.observe()
            client.request("stop_recording", expect=client.version)
        return directory, initial

    def bundle(self, directory):
        return build_trajectory(directory / "session.jsonl", directory / "audit", directory / "bundle")

    def initializer(self, *, warm_visibility=None, **kwargs):
        @contextmanager
        def initialize(trajectory, output):
            self.actual = DrawTransport(output / "audit", epoch=99, revision=2, pixel_byte=2, diagnostic=999, **kwargs)
            with SessionTrace(output / "session.jsonl") as trace, Client(self.actual, trace=trace) as client:
                identity = identity_from_launcher(client.hello(), ARTIFACTS)
                client.observe()
                client.request("prepare_render", expect=client.version)
                if warm_visibility:
                    path = output / "audit" / "events.jsonl"
                    lines = path.read_bytes().splitlines(keepends=True)
                    path.write_bytes(lines[0] if warm_visibility == "missing" else b"".join(lines)[:-5])
                yield ReplaySession(client, identity, output / "audit")
                client.request("stop_recording", expect=client.version)
        return initialize

    def test_full_and_seek_warm_step_counts_and_diagnostic_exclusion(self):
        source, _ = self.source()
        trajectory = self.bundle(source)
        for tick in (None, 0, 2):
            with self.subTest(tick=tick):
                report = replay(trajectory, self.initializer(), self.root / f"replay-{tick}", target_tick=tick)
                self.assertTrue(report["equal"])
                draw = report["draw_schedule"]
                self.assertEqual(draw["warm_receipts_compared"], 1)
                self.assertEqual(draw["step_receipts_compared"], 3 if tick is None else tick)
                self.assertEqual(draw["actual_health"]["step_frames"], 3 if tick is None else tick)
                self.assertTrue(draw["final_health_verified"])
                self.assertNotEqual(draw["source_health"]["automatic_denied"], draw["actual_health"]["automatic_denied"])

    def test_missing_duplicate_misplaced_wrong_version_clock_and_warm_rejected(self):
        for fault in ("missing", "duplicate_count", "misplaced", "wrong_version", "wrong_clock", "warm_seed", "warm_missing", "warm_duplicate", "bad_health"):
            with self.subTest(fault=fault):
                source, _ = self.source(fault, fault=fault)
                with self.assertRaises(EvidenceError):
                    self.bundle(source)

    def test_mode_mismatch_and_unprepared_initial_rejected(self):
        source, initial = self.source()
        manifest = source / "audit" / "manifest.json"
        game = json.loads(manifest.read_text())
        game.pop("draw_schedule")
        manifest.write_text(json.dumps(game))
        with self.assertRaisesRegex(EvidenceError, "draw.*mode"):
            AuditLog(source / "audit")

    def test_unflushed_warm_tail_fails_before_any_recorded_action(self):
        source, _ = self.source()
        trajectory = self.bundle(source)
        for visibility in ("missing", "partial"):
            with self.subTest(visibility=visibility), self.assertRaises(EvidenceError):
                replay(trajectory, self.initializer(warm_visibility=visibility), self.root / visibility)
            self.assertFalse(any(request["method"] in {"commit", "advance"} for request in self.actual.requests))

    def test_actual_close_health_cannot_publish_equal(self):
        source, _ = self.source()
        with self.assertRaisesRegex(EvidenceError, "close health"):
            replay(self.bundle(source), self.initializer(fault="bad_health"), self.root / "actual")
        report = json.loads((self.root / "actual" / "replay-report.json").read_text())
        self.assertFalse(report["equal"])

    def test_takeover_checks_entire_branch_receipts_and_close(self):
        source, _ = self.source()
        trajectory = self.bundle(source)
        for fault in (None, "missing", "bad_health"):
            def takeover(session, _link):
                self.actual.fault = fault
                session.client.advance(1)
            output = self.root / f"branch-{fault}"
            with self.subTest(fault=fault):
                if fault:
                    with self.assertRaises(EvidenceError):
                        replay(trajectory, self.initializer(), output, target_tick=1, on_takeover=takeover)
                    self.assertFalse(json.loads((output / "replay-report.json").read_text())["equal"])
                else:
                    report = replay(trajectory, self.initializer(), output, target_tick=1, on_takeover=takeover)
                    self.assertTrue(report["equal"])
                    self.assertEqual(report["draw_schedule"]["step_receipts_compared"], 1)
                    self.assertEqual(report["draw_schedule"]["actual_health"]["step_frames"], 2)
                    self.assertEqual(report["draw_schedule"]["health_scope"], "full_branch")

    def test_recipe_cannot_claim_a_different_preparation(self):
        for field in ("seed", "clock_anchor", "render_preparation"):
            source, _ = self.source(field)
            path = source / "session.jsonl"
            records = [json.loads(line) for line in path.read_text().splitlines()]
            recipe = next(record["data"]["initialization"] for record in records if record["kind"] == "replay_initial")
            recipe[field] = None
            path.write_text("".join(json.dumps(record) + "\n" for record in records))
            with self.subTest(field=field), self.assertRaisesRegex(EvidenceError, "recipe"):
                self.bundle(source)

    def test_cached_success_failure_and_metadata_without_pixel_comparison(self):
        source, _ = self.source(captures=True)
        report = replay(self.bundle(source), self.initializer(), self.root / "replay")
        self.assertTrue(report["equal"])
        self.assertEqual(report["capture_interventions"]["compared"], 2)
        self.assertTrue(report["capture_interventions"]["reproduced"])

    def test_stale_or_forced_cache_receipts_rejected(self):
        for fault in ("stale_capture", "forced_capture"):
            with self.subTest(fault=fault):
                source, _ = self.source(fault, captures=True)
                trace = source / "session.jsonl"
                records = [json.loads(line) for line in trace.read_text().splitlines()]
                for record in records:
                    result = record.get("data", {}).get("result", {})
                    if record["kind"] == "response" and result.get("capture_ok") is True:
                        if fault == "stale_capture":
                            result["frame_version"]["tick"] += 1
                        else:
                            result["forced_render"] = True
                trace.write_text("".join(json.dumps(record) + "\n" for record in records))
                with self.assertRaises(EvidenceError):
                    self.bundle(source)

    def test_actual_capture_cannot_mutate_state(self):
        source, _ = self.source(captures=True)
        with self.assertRaises(ReplayDivergence):
            replay(self.bundle(source), self.initializer(fault="capture_mutation"), self.root / "actual")

    def test_receipt_rng_difference_detected_even_if_post_state_equal(self):
        source, _ = self.source()
        with self.assertRaises(ReplayDivergence) as caught:
            replay(self.bundle(source), self.initializer(fault="receipt_rng"), self.root / "actual")
        self.assertIn("render", caught.exception.report["stage"])
        self.assertIn("rng_before", caught.exception.report["difference"]["path"])

    def test_terminal_skip_counts_and_same_post_auxiliary_allowed(self):
        source, _ = self.source(terminal_tick=2, fault="terminal_auxiliary")
        report = replay(self.bundle(source), self.initializer(terminal_tick=2, fault="terminal_auxiliary"), self.root / "actual")
        self.assertTrue(report["equal"])
        self.assertEqual(report["draw_schedule"]["step_receipts_compared"], 1)
        self.assertEqual(report["draw_schedule"]["terminal_skips_compared"], 1)

    def test_fake_terminal_missing_transition_and_zero_clock_remain_rejected(self):
        for fault in ("fake_terminal", "missing_terminal", "zero_delta", "wrong_clock"):
            with self.subTest(fault=fault):
                source, _ = self.source(fault, fault=fault, terminal_tick=None if fault == "fake_terminal" else 2)
                with self.assertRaises(EvidenceError):
                    self.bundle(source)

    def test_draw_phase_births_and_particles_bind_to_pre_update(self):
        source, _ = self.source(draw_births=True, spawn_schedule={}, particle_pointer=0x10000140, animation_handle=65538)
        report = replay(self.bundle(source), self.initializer(draw_births=True, spawn_schedule={}, particle_pointer=0x20000140, animation_handle=131074), self.root / "actual")
        self.assertTrue(report["equal"])
        self.assertEqual(report["spawn_events_compared"], 3)
        self.assertEqual(report["particle_shake"]["semantic_seed_calls_compared"], 6)

    def test_batch_equivalence_compares_render_receipts(self):
        left, _ = self.source("single", singles=True)
        right, _ = self.source("batch")
        compare = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools" / "check-boundary-equivalence.py"))["compare"]
        report = compare(self.bundle(left).directory, self.bundle(right).directory, purpose="batch", ticks=3)
        self.assertTrue(report["passed"])
        self.assertEqual(report["draw_schedule"]["step_receipts_compared"], 3)

    def test_schedule_equivalence_keeps_actions_and_rejects_identical_budgets(self):
        left, _ = self.source("single", singles=True, actions=True)
        right, _ = self.source("batch", actions=True)
        compare = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools" / "check-boundary-equivalence.py"))["compare"]
        a, b = self.bundle(left).directory, self.bundle(right).directory
        report = compare(a, b, purpose="schedule", ticks=3)
        self.assertTrue(report["passed"])
        self.assertEqual(report["draw_schedule"]["step_receipts_compared"], 3)
        with self.assertRaisesRegex(EvidenceError, "different request budgets"):
            compare(a, a, purpose="schedule", ticks=3)
        with self.assertRaises(EvidenceError):
            compare(a, b, purpose="batch", ticks=3)


if __name__ == "__main__":
    unittest.main()
