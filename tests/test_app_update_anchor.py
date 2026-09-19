"""Synthetic one-shot anchor evidence; no game launch or live proof."""
import copy
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from llm_vs_zombies import app_update_anchor as anchor, sound_effects as audio
from llm_vs_zombies.audit_compare import AuditLog, EvidenceError, _seeded_rng, canonical
from llm_vs_zombies.client import Client
from llm_vs_zombies.engine_replay import (ReplaySession, ReplayDivergence, build_trajectory,
                                        capture_initial, identity_from_launcher, replay, _validate_initial)
from llm_vs_zombies.session import SessionTrace
from test_sound_effects import AudioTransport, ASSETS, SPEC as AUDIO_SPEC
from test_draw_schedule import DRAW_SPEC


DEMO = {"00000510": 0, "00000511": 0, "00000578": 1250, "0000049c": 0, "000004a0": 0}


class AnchorTransport(AudioTransport):
    def __init__(self, directory, *, app_count=1295, anchored_mode=True, anchor_fault=None, demo_delta=0, **kwargs):
        self.app_count, self.anchored_mode = app_count, anchored_mode
        self.app_anchored, self.anchor_fault, self.demo_delta = False, anchor_fault, demo_delta
        self.anchor_receipt = None
        super().__init__(directory, **kwargs)
        if anchored_mode:
            self.game[anchor.METHOD] = copy.deepcopy(anchor.SPEC)
            (directory / "manifest.json").write_text(json.dumps(self.game), encoding="utf-8")

    def observe(self):
        value = super().observe()
        if self.anchored_mode:
            value["app_update_anchored"] = self.app_anchored
        return value

    def state(self):
        value = super().state()
        value["sound_effects"]["app_update_count"] = self.app_count + self.tick
        if self.anchor_fault == "late_counter" and self.tick:
            value["sound_effects"]["app_update_count"] += 1
        return value

    def exchange(self, payload, timeout):
        request = json.loads(payload)
        method, rid, params = request["method"], request["request_id"], request["params"]
        if method in {"rng_seed", "clock_restore", anchor.METHOD}:
            self.requests.append(request)
            if method == "rng_seed":
                self.rng = _seeded_rng(params["seed"])
                self.revision += 1
                if self.anchor_fault != "missing_seed":
                    self.event("rng_seeded", {"request_id": rid, "seed": params["seed"]})
                result = {"ok": True, "observation": self.observe()}
            elif method == "clock_restore":
                self.revision += 1
                self.event("clocks_restored", {"request_id": rid, "snapshot": params["snapshot"]})
                result = {"ok": True, "observation": self.observe()}
            else:
                before, before_state, pre = self.app_count, self.state(), self.version()
                self.app_count = params["app_update_count"]
                self.app_anchored = True
                self.revision += 1
                demo = dict(DEMO, **{"00000578": DEMO["00000578"] + self.demo_delta})
                receipt = {"schema": "lvz.app-update-anchor.v1", "mode": anchor.MODE,
                    "before": before, "after": self.app_count, "requested": self.app_count,
                    "before_state": before_state, "after_state": self.state(), "before_version": pre,
                    "after_version": self.version(), "demo_before": demo, "demo_after": copy.deepcopy(demo)}
                self.anchor_receipt = copy.deepcopy(receipt)
                if self.anchor_fault != "missing":
                    self.event(anchor.EVENT, {"request_id": rid, "anchor": receipt})
                if self.anchor_fault == "duplicate":
                    self.event(anchor.EVENT, {"request_id": rid, "anchor": receipt})
                if self.anchor_fault == "native_failed":
                    self.event("app_update_anchor_failed", {"request_id": rid, "anchor": receipt, "message": "synthetic"})
                result = {"anchored": True, "anchor": receipt, "observation": self.observe()}
            return json.dumps({"protocol": 1, "request_id": rid, "ok": True, "result": result}).encode()
        result = json.loads(super().exchange(payload, timeout))
        if method == "hello":
            result["result"]["capabilities"].update({anchor.MODE: self.anchored_mode, anchor.METHOD: self.anchored_mode})
        return json.dumps(result).encode()


class AppUpdateAnchorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, client, model, target=None):
        identity = identity_from_launcher(client.hello(), ASSETS)
        client.observe()
        client.request("rng_seed", {"seed": 42}, expect=client.version)
        clocks = model.clocks()
        client.request("clock_restore", {"snapshot": clocks}, expect=client.version)
        value = model.app_count if target is None else target
        if model.anchored_mode:
            client.request(anchor.METHOD, {"app_update_count": value}, expect=client.version)
        client.request("prepare_render", expect=client.version)
        recipe = {"seed": 42, "execution_mode": DRAW_SPEC["mode"], "draw_schedule": DRAW_SPEC,
            "clock_anchor": clocks, "postwarm_clock": clocks,
            "render_preparation": {"warm_frames": 1, "step_frames": 0, "verified_before_draw": True,
                "seeded_rng_sha256": hashlib.sha256(canonical(_seeded_rng(42))).hexdigest(),
                "postwarm_rng_sha256": hashlib.sha256(canonical(model.rng)).hexdigest()},
            "sound_effects": {"configuration": AUDIO_SPEC, "b0_state_sha256": audio.state_sha256(model.state()["sound_effects"])}}
        if model.anchored_mode:
            recipe[anchor.METHOD] = {"configuration": copy.deepcopy(anchor.SPEC), "app_update_count": value}
        return identity, recipe

    def source(self, name="source", **kwargs):
        directory = self.root / name
        model = AnchorTransport(directory / "audit", zero_tick=None, **kwargs)
        with SessionTrace(directory / "trace.jsonl") as trace, Client(model, trace=trace) as client:
            identity, recipe = self.prepare(client, model)
            initial = capture_initial(client, identity=identity, initialization=recipe)
            client.advance(3)
            client.request("stop_recording", expect=client.version)
        return directory, model, initial

    def bundle(self, directory):
        return build_trajectory(directory / "trace.jsonl", directory / "audit", directory / "bundle")

    def initializer(self, *, app_count=1294, **kwargs):
        @contextmanager
        def initialize(trajectory, output):
            self.actual = AnchorTransport(output / "audit", zero_tick=None, epoch=99, revision=2,
                                          app_count=app_count, **kwargs)
            with SessionTrace(output / "trace.jsonl") as trace, Client(self.actual, trace=trace) as client:
                identity, _ = self.prepare(client, self.actual, anchor.target_from_recipe(trajectory.initial["initialization"]))
                yield ReplaySession(client, identity, output / "audit")
                client.request("stop_recording", expect=client.version)
        return initialize

    def test_real_before_values_retained_after_equal_full_replay_and_seek(self):
        source, _, _ = self.source()
        trajectory = self.bundle(source)
        for tick in (None, 0, 2):
            with self.subTest(tick=tick):
                result = replay(trajectory, self.initializer(), self.root / f"actual-{tick}", target_tick=tick)
                self.assertTrue(result["equal"])
                self.assertEqual(result["app_update_anchor"]["source_before"], 1295)
                self.assertEqual(result["app_update_anchor"]["actual_before"], 1294)
                self.assertEqual(result["app_update_anchor"]["actual_after"], 1295)
                self.assertTrue(result["app_update_anchor"]["receipt_compared"])
                self.assertFalse(result["app_update_anchor"]["subsequent_state_normalized"])

    def test_old_audio_spec_without_anchor_still_reads_and_replays(self):
        source, _, _ = self.source(anchored_mode=False)
        trajectory = self.bundle(source)
        result = replay(trajectory, self.initializer(anchored_mode=False, app_count=1295), self.root / "old")
        self.assertTrue(result["equal"])
        self.assertIsNone(trajectory.audit.app_anchor_receipt)

    def test_cross_capability_mode_does_not_upgrade_old_archive(self):
        source, _, _ = self.source(anchored_mode=False)
        with self.assertRaises(ReplayDivergence):
            replay(self.bundle(source), self.initializer(), self.root / "cross")
        self.assertFalse(any(r["method"] in {"commit", "advance"} for r in self.actual.requests))

    def test_missing_duplicate_failed_or_unseeded_anchor_cannot_package(self):
        for fault in ("missing", "duplicate", "native_failed", "missing_seed"):
            source, _, _ = self.source(fault, anchor_fault=fault)
            with self.subTest(fault=fault), self.assertRaises(EvidenceError):
                self.bundle(source)

    def test_receipt_rejects_rng_history_calls_and_other_clock_changes(self):
        _, model, _ = self.source()
        for field in ("rng", "history", "calls", "clock", "counter", "version", "shape"):
            value = copy.deepcopy(model.anchor_receipt)
            if field == "rng":
                value["after_state"]["rng"]["instances"]["global_mt"]["words"][0] += 1
            elif field == "history":
                value["after_state"]["sound_effects"]["histories"][0]["slots"][0][3] += 1
            elif field == "calls":
                value["after_state"]["sound_effects"]["calls"] += 1
            elif field == "clock":
                value["after_state"]["app"]["mj_clock"] += 1
            elif field == "counter":
                value["after"] += 1
            elif field == "version":
                value["after_version"]["revision"] += 1
            else:
                value["write_ok"] = True
            with self.subTest(field=field), self.assertRaises(EvidenceError):
                anchor.receipt(value, model.game, seed=42)

    def test_active_demo_or_modified_demo_guard_refused(self):
        _, model, _ = self.source()
        for field in DEMO:
            value = copy.deepcopy(model.anchor_receipt)
            value["demo_before"][field] += 1
            if field in {"00000510", "00000511"}:
                value["demo_after"][field] += 1
            with self.subTest(field=field), self.assertRaises(EvidenceError):
                anchor.receipt(value, model.game)
        value = copy.deepcopy(model.anchor_receipt)
        value["demo_before"]["00000510"] = value["demo_after"]["00000510"] = False
        with self.assertRaises(EvidenceError):
            anchor.receipt(value, model.game)
        value = copy.deepcopy(model.anchor_receipt)
        value["demo_before"]["000004a0"] = value["demo_after"]["000004a0"] = 256
        with self.assertRaises(EvidenceError):
            anchor.receipt(value, model.game)

    def test_target_range_boolean_and_real_original_value_range(self):
        _, model, _ = self.source()
        for number in (-1, True, 0x80000000, 1.0, None):
            value = copy.deepcopy(model.anchor_receipt)
            value["requested"] = number
            with self.subTest(number=number), self.assertRaises(EvidenceError):
                anchor.receipt(value, model.game)
        value = copy.deepcopy(model.anchor_receipt)
        value["before"] = value["before_state"]["sound_effects"]["app_update_count"] = 0x80000000
        with self.assertRaises(EvidenceError):
            anchor.receipt(value, model.game)

    def test_missing_wrong_recipe_and_false_applied_observation(self):
        _, _, initial = self.source()
        for fault in ("missing", "target", "flag"):
            value = copy.deepcopy(initial)
            if fault == "missing":
                value["initialization"].pop(anchor.METHOD)
            elif fault == "target":
                value["initialization"][anchor.METHOD]["app_update_count"] += 1
            else:
                value["observation"]["app_update_anchored"] = False
            with self.subTest(fault=fault), self.assertRaises(EvidenceError):
                _validate_initial(value)

    def test_demo_auxiliary_and_post_step_app_values_are_still_compared(self):
        source, _, _ = self.source()
        trajectory = self.bundle(source)
        for fault, kwargs in (("demo", {"demo_delta": 1}), ("counter", {"anchor_fault": "late_counter"})):
            with self.subTest(fault=fault), self.assertRaises(ReplayDivergence):
                replay(trajectory, self.initializer(**kwargs), self.root / fault)
            report = json.loads((self.root / fault / "replay-report.json").read_text())
            self.assertFalse(report["equal"])

    def test_success_receipt_cannot_appear_after_warm_or_reseed(self):
        for fault in ("late", "reseed"):
            source, model, _ = self.source(fault)
            path = source / "audit/events.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            anchor_index = next(i for i, event in enumerate(rows) if event["kind"] == anchor.EVENT)
            if fault == "late":
                # Preserve exact sequence numbers: move only semantic content.
                warm_index = next(i for i, event in enumerate(rows) if event["kind"] == "render_preparing")
                for key in ("kind", "payload", "version"):
                    rows[anchor_index][key], rows[warm_index][key] = rows[warm_index][key], rows[anchor_index][key]
            else:
                rows[anchor_index + 1]["kind"] = "rng_seeded"
                rows[anchor_index + 1]["payload"] = {"request_id": "reseed", "seed": 42}
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            with self.subTest(fault=fault), self.assertRaises(EvidenceError):
                AuditLog(source / "audit", require_closed=True)

    def test_capability_mismatch_and_unrecognized_spec_fail_closed(self):
        source, model, _ = self.source()
        for hello in ({"game": model.game, "capabilities": {anchor.MODE: True}},
                      {"game": {}, "capabilities": {anchor.METHOD: True}},
                      {"game": {anchor.METHOD: None}, "capabilities": {}}):
            with self.assertRaises(EvidenceError):
                anchor.negotiate(hello)


if __name__ == "__main__":
    unittest.main()
