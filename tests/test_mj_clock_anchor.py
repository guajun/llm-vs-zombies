"""Synthetic fixed-target App counter anchors; no game launch or live proof."""
import copy
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from llm_vs_zombies import app_update_anchor as app_anchor
from llm_vs_zombies import mj_clock_anchor as anchor
from llm_vs_zombies import sound_effects as audio
from llm_vs_zombies.audit_compare import AuditLog, EvidenceError, _seeded_rng, canonical
from llm_vs_zombies.client import Client
from llm_vs_zombies.engine_replay import (ReplaySession, ReplayDivergence, build_trajectory, capture_initial,
                                        identity_from_launcher, replay, _validate_initial)
from llm_vs_zombies.session import SessionTrace
from test_app_update_anchor import AnchorTransport
from test_sound_effects import ASSETS, SPEC as AUDIO_SPEC
from test_draw_schedule import DRAW_SPEC


class MjTransport(AnchorTransport):
    """Two worlds differ in the real counter but declare one fixed target."""

    def __init__(self, directory, *, mj=1307, mj_mode=True, mj_fault=None, **kwargs):
        self.mj_base, self.mj_mode, self.mj_fault = mj, mj_mode, mj_fault
        self.mj_anchored, self.mj_receipt = False, None
        super().__init__(directory, **kwargs)
        if mj_mode:
            self.game[anchor.METHOD] = copy.deepcopy(anchor.SPEC)
            (directory / "manifest.json").write_text(json.dumps(self.game), encoding="utf-8")

    def clocks(self):
        value = super().clocks()
        value["mj_clock"] = self.mj_base + self.calls
        return value

    def observe(self):
        value = super().observe()
        if self.mj_mode:
            value["mj_clock_anchored"] = self.mj_anchored
        return value

    def exchange(self, payload, timeout):
        request = json.loads(payload)
        method, rid, params = request["method"], request["request_id"], request["params"]
        if method == "clock_restore":
            # The restore really writes the recorded absolute counter back, so
            # a cold replay starts from the recorded prewarm clocks.
            self.mj_base = params["snapshot"]["mj_clock"] - self.calls
        if method == anchor.METHOD:
            self.requests.append(request)
            before, before_state, pre = self.clocks()["mj_clock"], self.state(), self.version()
            self.mj_base = params["mj_clock"] - self.calls
            self.mj_anchored = True
            self.revision += 1
            receipt = {"schema": anchor.SCHEMA, "mode": anchor.MODE, "before": before,
                "after": self.clocks()["mj_clock"], "requested": params["mj_clock"],
                "before_state": before_state, "after_state": self.state(),
                "before_version": pre, "after_version": self.version()}
            self.mj_receipt = copy.deepcopy(receipt)
            if self.mj_fault != "missing":
                self.event(anchor.EVENT, {"request_id": rid, "anchor": receipt})
            if self.mj_fault == "duplicate":
                self.event(anchor.EVENT, {"request_id": rid, "anchor": receipt})
            if self.mj_fault == "native_failed":
                self.event(anchor.FAILED_EVENT, {"request_id": rid, "anchor": receipt, "message": "synthetic"})
            return json.dumps({"protocol": 1, "request_id": rid, "ok": True,
                "result": {"anchored": True, "anchor": receipt, "observation": self.observe()}}).encode()
        result = json.loads(super().exchange(payload, timeout))
        if method == "hello":
            result["result"]["capabilities"].update({anchor.MODE: self.mj_mode, anchor.METHOD: self.mj_mode})
        return json.dumps(result).encode()


class MjClockAnchorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, client, model, target=None, restore=None):
        identity = identity_from_launcher(client.hello(), ASSETS)
        client.observe()
        client.request("rng_seed", {"seed": 42}, expect=client.version)
        # A cold replay restores the recorded prewarm clocks exactly like the
        # production recipe; a source world records its own actual values.
        clocks = copy.deepcopy(restore) if restore is not None else model.clocks()
        client.request("clock_restore", {"snapshot": clocks}, expect=client.version)
        if model.anchored_mode:
            client.request(app_anchor.METHOD, {"app_update_count": model.app_count}, expect=client.version)
        fixed = model.clocks()["mj_clock"] + 117 if target is None else target
        if model.mj_mode:
            client.request(anchor.METHOD, {"mj_clock": fixed}, expect=client.version)
        client.request("prepare_render", expect=client.version)
        anchored = model.clocks()
        recipe = {"seed": 42, "execution_mode": DRAW_SPEC["mode"], "draw_schedule": DRAW_SPEC,
            "clock_anchor": anchored, "postwarm_clock": anchored,
            "render_preparation": {"warm_frames": 1, "step_frames": 0, "verified_before_draw": True,
                "seeded_rng_sha256": hashlib.sha256(canonical(_seeded_rng(42))).hexdigest(),
                "postwarm_rng_sha256": hashlib.sha256(canonical(model.rng)).hexdigest()},
            "sound_effects": {"configuration": AUDIO_SPEC,
                "b0_state_sha256": audio.state_sha256(model.state()["sound_effects"])}}
        if model.anchored_mode:
            recipe[app_anchor.METHOD] = {"configuration": copy.deepcopy(app_anchor.SPEC),
                                         "app_update_count": model.app_count}
        if model.mj_mode:
            recipe[anchor.METHOD] = {"configuration": copy.deepcopy(anchor.SPEC), "mj_clock": fixed}
        return identity, recipe

    def source(self, name="source", **kwargs):
        target = kwargs.pop("target", None)
        directory = self.root / name
        model = MjTransport(directory / "audit", zero_tick=None, **kwargs)
        with SessionTrace(directory / "trace.jsonl") as trace, Client(model, trace=trace) as client:
            identity, recipe = self.prepare(client, model, target)
            initial = capture_initial(client, identity=identity, initialization=recipe)
            client.advance(3)
            client.request("stop_recording", expect=client.version)
        return directory, model, initial

    def bundle(self, directory):
        return build_trajectory(directory / "trace.jsonl", directory / "audit", directory / "bundle")

    def initializer(self, *, mj=1340, **kwargs):
        @contextmanager
        def initialize(trajectory, output):
            self.actual = MjTransport(output / "audit", zero_tick=None, epoch=99, revision=2, mj=mj, **kwargs)
            with SessionTrace(output / "trace.jsonl") as trace, Client(self.actual, trace=trace) as client:
                identity, _ = self.prepare(client, self.actual,
                    anchor.target_from_recipe(trajectory.initial["initialization"]),
                    restore=trajectory.initial["initialization"]["clock_anchor"])
                yield ReplaySession(client, identity, output / "audit")
                client.request("stop_recording", expect=client.version)
        return initialize

    def test_different_before_values_reach_one_common_target_and_replay(self):
        target = 2048
        source, model, initial = self.source(target=target)
        _, other_model, other_initial = self.source("other", mj=1340, target=target)
        self.assertEqual((model.mj_receipt["before"], other_model.mj_receipt["before"]), (1307, 1340))
        self.assertEqual((model.mj_receipt["after"], other_model.mj_receipt["after"]), (target, target))
        self.assertEqual(initial["state"]["app"]["mj_clock"], target)
        self.assertEqual(other_initial["state"]["app"]["mj_clock"], target)
        # Both worlds differ before the anchor and are identical at B(0).
        self.assertNotEqual(model.mj_receipt["before_state"], other_model.mj_receipt["before_state"])
        self.assertEqual(initial["state"], other_initial["state"])
        trajectory = self.bundle(source)
        self.assertEqual(anchor.target_from_recipe(initial["initialization"]), model.mj_receipt["requested"])
        for tick in (None, 0, 2):
            with self.subTest(tick=tick):
                result = replay(trajectory, self.initializer(), self.root / f"actual-{tick}", target_tick=tick)
                self.assertTrue(result["equal"])
                self.assertEqual(result["mj_clock_anchor"]["source_before"], 1307)
                self.assertEqual(result["mj_clock_anchor"]["actual_before"], target)
                self.assertEqual(result["mj_clock_anchor"]["common_target"], target)
                self.assertEqual(result["mj_clock_anchor"]["source_after"],
                                 result["mj_clock_anchor"]["actual_after"])
                self.assertTrue(result["mj_clock_anchor"]["receipt_compared"])
                self.assertFalse(result["mj_clock_anchor"]["subsequent_state_normalized"])
                self.assertFalse(result["mj_clock_anchor"]["before_values_compared"])

    def test_declared_capability_requires_an_explicit_fixed_target(self):
        directory = self.root / "fail"
        model = MjTransport(directory / "audit", zero_tick=None, mj=1307)
        with SessionTrace(directory / "trace.jsonl") as trace, Client(model, trace=trace) as client:
            from llm_vs_zombies.initialization import apply_recipe
            # The App update target is declared here so the refusal under test
            # is the missing fixed MJ clock target, not the App one.
            with self.assertRaisesRegex(RuntimeError, r"declare the fixed B\(0\) target"):
                apply_recipe(client, 42, app_update_count=1307)
            self.assertFalse(any(row["method"] in {anchor.METHOD, app_anchor.METHOD} for row in model.requests))

    def test_old_capability_archive_and_replay_are_preserved(self):
        source, _, initial = self.source(mj_mode=False)
        self.assertNotIn(anchor.METHOD, initial["initialization"])
        trajectory = self.bundle(source)
        self.assertIsNone(trajectory.audit.mj_clock_receipt)
        result = replay(trajectory, self.initializer(mj_mode=False), self.root / "old")
        self.assertTrue(result["equal"])
        self.assertEqual(result["mj_clock_anchor"]["mode"], "not_declared")

    def test_cross_capability_mode_does_not_upgrade_old_archive(self):
        source, _, _ = self.source(mj_mode=False)
        with self.assertRaises(ReplayDivergence):
            replay(self.bundle(source), self.initializer(), self.root / "cross")
        self.assertFalse(any(r["method"] in {"commit", "advance"} for r in self.actual.requests))

    def test_missing_duplicate_and_native_failed_events_cannot_package(self):
        for fault in ("missing", "duplicate", "native_failed"):
            source, _, _ = self.source(fault, mj_fault=fault)
            with self.subTest(fault=fault), self.assertRaises(EvidenceError):
                self.bundle(source)

    def test_missing_or_wrong_recipe_and_flag_fail_closed(self):
        _, _, initial = self.source()
        for fault in ("missing", "target", "flag"):
            value = copy.deepcopy(initial)
            if fault == "missing":
                value["initialization"].pop(anchor.METHOD)
            elif fault == "target":
                value["initialization"][anchor.METHOD]["mj_clock"] += 1
            else:
                value["observation"]["mj_clock_anchored"] = False
            with self.subTest(fault=fault), self.assertRaises(EvidenceError):
                _validate_initial(value)
        value = copy.deepcopy(initial)
        value["initialization"][anchor.METHOD]["configuration"]["source_actual_target"] = True
        with self.assertRaises(EvidenceError):
            _validate_initial(value)

    def test_receipt_rejects_target_range_and_unrelated_state_changes(self):
        _, model, _ = self.source()
        for field in ("rng", "calls", "clock", "counter", "board", "version", "shape"):
            value = copy.deepcopy(model.mj_receipt)
            if field == "rng":
                value["after_state"]["rng"]["instances"]["global_mt"]["words"][0] += 1
            elif field == "calls":
                value["after_state"]["sound_effects"]["calls"] += 1
            elif field == "clock":
                value["after_state"]["board"]["00005568"] += 1
            elif field == "counter":
                value["before"] += 1
            elif field == "board":
                value["after_state"]["unchanged_guard"] = 0
            elif field == "version":
                value["after_version"]["revision"] += 1
            else:
                value["write_ok"] = True
            with self.subTest(field=field), self.assertRaises(EvidenceError):
                anchor.receipt(value, model.game, seed=42)
        for number in (-1, True, 0x80000000, 1.0, None):
            value = copy.deepcopy(model.mj_receipt)
            value["requested"] = number
            with self.subTest(number=number), self.assertRaises(EvidenceError):
                anchor.receipt(value, model.game, seed=42)
        for number in (0x80000000, False, 1.5):
            value = copy.deepcopy(model.mj_receipt)
            value["before"] = value["before_state"]["app"]["mj_clock"] = number
            with self.subTest(before=number), self.assertRaises(EvidenceError):
                anchor.receipt(value, model.game, seed=42)

    def test_receipt_rejects_readback_that_differs_from_the_fixed_target(self):
        _, model, _ = self.source()
        for edit in ("after", "after_state", "requested"):
            value = copy.deepcopy(model.mj_receipt)
            if edit == "after":
                value["after"] += 1
            elif edit == "after_state":
                value["after_state"]["app"]["mj_clock"] += 1
            else:
                value["requested"] += 1
            with self.subTest(edit=edit), self.assertRaises(EvidenceError):
                anchor.receipt(value, model.game, seed=42)
        value = copy.deepcopy(model.mj_receipt)
        value["before_state"]["app"]["mj_clock"] = value["before"] + 1
        with self.assertRaises(EvidenceError):
            anchor.receipt(value, model.game, seed=42)

    def test_duplicate_late_reseed_and_chain_tampering_are_refused(self):
        source, _, _ = self.source("late")
        path = source / "audit/events.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        fixed_index = next(i for i, event in enumerate(rows) if event["kind"] == anchor.EVENT)
        app_index = next(i for i, event in enumerate(rows) if event["kind"] == app_anchor.EVENT)
        warm_index = next(i for i, event in enumerate(rows) if event["kind"] == "render_preparing")
        self.assertTrue(app_index < fixed_index < warm_index)
        # Preserve exact sequence numbers: move only semantic content.
        for key in ("kind", "payload", "version"):
            rows[fixed_index][key], rows[warm_index][key] = rows[warm_index][key], rows[fixed_index][key]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        with self.assertRaises(EvidenceError):
            AuditLog(source / "audit", require_closed=True)
        source, _, _ = self.source("chain")
        path = source / "audit/events.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        app_index = next(i for i, event in enumerate(rows) if event["kind"] == app_anchor.EVENT)
        rows[app_index]["version"]["revision"] += 1
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        with self.assertRaises(EvidenceError):
            AuditLog(source / "audit", require_closed=True)
        source, _, _ = self.source("reseed")
        path = source / "audit/events.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        fixed_index = next(i for i, event in enumerate(rows) if event["kind"] == anchor.EVENT)
        rows[fixed_index + 1]["kind"] = "rng_seeded"
        rows[fixed_index + 1]["payload"] = {"request_id": "reseed", "seed": 42}
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        with self.assertRaises(EvidenceError):
            AuditLog(source / "audit", require_closed=True)

    def test_capability_mismatch_and_unrecognized_spec_fail_closed(self):
        _, model, _ = self.source()
        for hello in ({"game": model.game, "capabilities": {anchor.MODE: True}},
                      {"game": {}, "capabilities": {anchor.METHOD: True}},
                      {"game": {anchor.METHOD: None}, "capabilities": {}}):
            with self.assertRaises(EvidenceError):
                anchor.negotiate(hello)
        hello = {"game": dict(model.game, mj_clock_anchor=dict(anchor.SPEC, receipt_event="other")),
                 "capabilities": {}}
        with self.assertRaises(EvidenceError):
            anchor.mode(hello["game"])


if __name__ == "__main__":
    unittest.main()
