"""Synthetic native-scope evidence; no originals, hooks, or game process."""
import copy
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from llm_vs_zombies import sound_counter as counter, sound_effects as audio, app_update_anchor as app_anchor
from llm_vs_zombies.audit_compare import AuditLog, AuditTail, EvidenceError, _seeded_rng, canonical
from llm_vs_zombies.client import Client
from llm_vs_zombies.engine_replay import (ReplaySession, ReplayDivergence, build_trajectory, capture_initial,
                                        identity_from_launcher, replay, _validate_initial, Trajectory)
from llm_vs_zombies.session import SessionTrace
from test_app_update_anchor import AnchorTransport
from test_sound_effects import ASSETS, SPEC as AUDIO_SPEC, health
from test_draw_schedule import DRAW_SPEC
from test_engine_replay import ModelTransport


class CounterTransport(AnchorTransport):
    def __init__(self, directory, *, raw_base=14, counter_mode=True, counter_fault=None, warm_allocations=3, **kwargs):
        self.raw_base, self.origin_raw, self.counter_mode = raw_base, None, counter_mode
        self.counter_fault, self.warm_calls, self.warm_allocations = counter_fault, 0, warm_allocations
        self.origin_receipt = None
        super().__init__(directory, **kwargs)
        self.activation = json.loads((directory / audio.EVIDENCE).read_text())
        if counter_mode:
            self.game["sound_counter"] = copy.deepcopy(counter.SPEC)
            (directory / "manifest.json").write_text(json.dumps(self.game), encoding="utf-8")
            self.files[counter.RAW_FILE] = (directory / counter.RAW_FILE).open("w", encoding="utf-8", newline="\n")

    def raw_calls(self):
        return self.raw_base + self.warm_calls + self.tick * 2

    def state(self):
        value = super().state()
        sound = value["sound_effects"]
        sound["calls"] = self.raw_calls() - (self.origin_raw or 0)
        if self.counter_mode:
            sound["counter_scope"] = "bootstrap_lifetime" if self.origin_raw is None else "experiment"
        return value

    def observe(self):
        value = super().observe()
        if self.counter_mode:
            value["counter_origin_bound"] = self.origin_raw is not None
        return value

    def receipt(self, warm=False):
        if warm:
            self.warm_calls += self.warm_allocations
        return super().receipt(warm)

    def write(self, name, value):
        super().write(name, value)
        if name == "checksums.jsonl" and self.counter_mode:
            raw = {"schema": "lvz.sound-counter-raw.v1", "seq": value["seq"], "kind": value["kind"],
                "version": value["version"], "engine_call_id": value["payload"]["engine_call"]["engine_call_id"],
                "raw_calls": self.raw_calls(), "origin_raw_calls": self.origin_raw,
                "experiment_calls": self.raw_calls() - self.origin_raw}
            super().write(counter.RAW_FILE, raw)

    def event(self, kind, payload):
        if kind == "recording_closed" and self.counter_mode:
            ModelTransport.event(self, kind, payload)
            ModelTransport.event(self, "engine_call_closed", self.health())
            h = dict(health(self.raw_calls() - self.origin_raw), counter_scope="experiment",
                     origin_raw_calls=self.origin_raw, raw_calls=self.raw_calls())
            if self.counter_fault == "bad_close":
                h["raw_calls"] += 1
            if self.counter_fault == "extra_close":
                h["raw_calls"] += 1
                h["calls"] += 1
            ModelTransport.event(self, "sound_effects_closed", h)
            ModelTransport.event(self, "draw_schedule_closed", dict(self.counts(), installed=True, latched=True,
                sealed=True, active=False, automatic_allowed=0, automatic_denied=self.diagnostic,
                controlled_calls=self.warm_frames + self.step_frames, faults=0, wrong_thread_calls=0, healthy=True))
            return
        if kind == app_anchor.EVENT and self.counter_fault == "broken_link":
            payload = copy.deepcopy(payload)
            # Both halves remain a valid App write, but no longer start from
            # the independently captured origin state.
            for key in ("before_state", "after_state"):
                payload["anchor"][key]["board"]["hidden"] += 1
        super().event(kind, payload)

    def exchange(self, payload, timeout):
        request = json.loads(payload)
        if request["method"] == counter.METHOD:
            self.requests.append(request)
            before, pre = self.state(), self.version()
            raw = copy.deepcopy(self.activation["recorder_attach"])
            raw["calls"] = self.raw_calls()
            self.origin_raw = self.raw_calls()
            self.revision += 1
            result = {"schema": "lvz.sound-counter-origin.v1", "mode": counter.MODE,
                "origin_raw_calls": self.origin_raw, "raw_before": raw, "raw_after": copy.deepcopy(raw),
                "before_state": before, "after_state": self.state(), "before_version": pre, "after_version": self.version()}
            self.origin_receipt = copy.deepcopy(result)
            event = {"request_id": request["request_id"], "counter_origin": result}
            if self.counter_fault != "missing":
                self.event(counter.EVENT, event)
            if self.counter_fault == "duplicate":
                self.event(counter.EVENT, event)
            if self.counter_fault == "failed":
                self.event("sound_counter_origin_failed", event)
            return json.dumps({"protocol": 1, "request_id": request["request_id"], "ok": True,
                "result": {"bound": True, "counter_origin": result, "observation": self.observe()}}).encode()
        result = json.loads(super().exchange(payload, timeout))
        if request["method"] == "hello":
            result["result"]["capabilities"].update({counter.MODE: self.counter_mode, counter.METHOD: self.counter_mode})
        return json.dumps(result).encode()


class SoundCounterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, client, model, app_count=None):
        identity = identity_from_launcher(client.hello(), ASSETS)
        client.observe()
        client.request("rng_seed", {"seed": 42}, expect=client.version)
        clocks = model.clocks()
        client.request("clock_restore", {"snapshot": clocks}, expect=client.version)
        if model.counter_mode:
            client.request(counter.METHOD, {}, expect=client.version)
        value = model.app_count if app_count is None else app_count
        client.request(app_anchor.METHOD, {"app_update_count": value}, expect=client.version)
        client.request("prepare_render", expect=client.version)
        recipe = {"seed": 42, "execution_mode": DRAW_SPEC["mode"], "draw_schedule": DRAW_SPEC,
            "clock_anchor": clocks, "postwarm_clock": clocks,
            "render_preparation": {"warm_frames": 1, "step_frames": 0, "verified_before_draw": True,
                "seeded_rng_sha256": hashlib.sha256(canonical(_seeded_rng(42))).hexdigest(),
                "postwarm_rng_sha256": hashlib.sha256(canonical(model.rng)).hexdigest()},
            "sound_effects": {"configuration": AUDIO_SPEC, "b0_state_sha256": audio.state_sha256(model.state()["sound_effects"])},
            app_anchor.METHOD: {"configuration": app_anchor.SPEC, "app_update_count": value}}
        if model.counter_mode:
            recipe["sound_counter"] = {"configuration": counter.SPEC}
        return identity, recipe

    def source(self, name="source", **kwargs):
        path = self.root / name
        model = CounterTransport(path / "audit", zero_tick=None, app_count=1362, **kwargs)
        with SessionTrace(path / "trace.jsonl") as trace, Client(model, trace=trace) as client:
            identity, recipe = self.prepare(client, model)
            initial = capture_initial(client, identity=identity, initialization=recipe)
            client.advance(3)
            client.request("stop_recording", expect=client.version)
        return path, model, initial

    def bundle(self, source):
        return build_trajectory(source / "trace.jsonl", source / "audit", source / "bundle")

    def initializer(self, *, raw_base=16, **kwargs):
        @contextmanager
        def initialize(trajectory, output):
            self.actual = CounterTransport(output / "audit", zero_tick=None, epoch=99, revision=2, owner=0x20000000,
                raw_base=raw_base, app_count=1427, **kwargs)
            with SessionTrace(output / "trace.jsonl") as trace, Client(self.actual, trace=trace) as client:
                identity, _ = self.prepare(client, self.actual, trajectory.initial["initialization"][app_anchor.METHOD]["app_update_count"])
                yield ReplaySession(client, identity, output / "audit")
                client.request("stop_recording", expect=client.version)
        return initialize

    def test_different_raw_origins_equal_native_state_full_replay_and_seek(self):
        source, model, initial = self.source()
        trajectory = self.bundle(source)
        self.assertEqual(initial["state"]["sound_effects"]["calls"], 3)
        self.assertIn("audit/" + counter.RAW_FILE, trajectory.manifest["files"])
        for tick in (None, 0, 2):
            report = replay(trajectory, self.initializer(), self.root / str(tick), target_tick=tick)
            self.assertTrue(report["equal"])
            self.assertEqual(report["sound_counter"]["source_origin_raw_calls"], 14)
            self.assertEqual(report["sound_counter"]["actual_origin_raw_calls"], 16)
            self.assertEqual(report["sound_counter"]["actual_experiment_calls_end"], 3 + 2 * (3 if tick is None else tick))
            self.assertFalse(report["sound_counter"]["state_normalized_by_comparator"])

    def test_unbound_old_scope_is_not_upgraded(self):
        source, _, _ = self.source(counter_mode=False)
        trajectory = self.bundle(source)
        report = replay(trajectory, self.initializer(counter_mode=False, raw_base=14), self.root / "old")
        self.assertTrue(report["equal"])
        with self.assertRaises(ReplayDivergence):
            replay(trajectory, self.initializer(), self.root / "cross")
        self.assertFalse(any(r["method"] in {"advance", "commit"} for r in self.actual.requests))

    def test_origin_changes_only_native_scope_and_count(self):
        _, model, _ = self.source()
        for mutation in ("rng", "history", "raw_error", "raw_counter", "scope", "app", "version"):
            value = copy.deepcopy(model.origin_receipt)
            if mutation == "rng":
                value["after_state"]["rng"]["instances"]["global_mt"]["words"][0] += 1
            elif mutation == "history":
                value["after_state"]["sound_effects"]["histories"][0]["slots"][0][3] += 1
            elif mutation == "raw_error":
                value["raw_after"]["errors"] = 1
            elif mutation == "raw_counter":
                value["raw_after"]["calls"] += 1
            elif mutation == "scope":
                value["before_state"]["sound_effects"]["counter_scope"] = "experiment"
            elif mutation == "app":
                value["after_state"]["sound_effects"]["app_update_count"] += 1
            else:
                value["after_version"]["revision"] += 1
            with self.subTest(mutation=mutation), self.assertRaises(EvidenceError):
                counter.receipt(value, model.game, seed=42)

    def test_missing_duplicate_failed_or_broken_app_chain_rejected(self):
        for fault in ("missing", "duplicate", "failed", "broken_link", "bad_close"):
            source, _, _ = self.source(fault, counter_fault=fault)
            with self.subTest(fault=fault), self.assertRaises(EvidenceError):
                self.bundle(source)

    def test_actual_warm_allocation_and_extra_closed_calls_are_not_ignored(self):
        source, _, _ = self.source()
        trajectory = self.bundle(source)
        for name, kwargs in (("warm", {"warm_allocations": 4}), ("close", {"counter_fault": "extra_close"})):
            with self.subTest(name=name), self.assertRaises(ReplayDivergence):
                replay(trajectory, self.initializer(**kwargs), self.root / name)
            self.assertFalse(json.loads((self.root / name / "replay-report.json").read_text())["equal"])

    def test_raw_alignment_origin_math_and_overflow_are_all_checked(self):
        for fault in ("seq", "kind", "version", "call", "origin", "arithmetic", "regression", "wrap", "boolean", "missing", "extra"):
            source, _, _ = self.source(fault)
            path = source / "audit" / counter.RAW_FILE
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            row = rows[2]
            if fault == "seq": row["seq"] += 1
            elif fault == "kind": row["kind"] = "post_step"
            elif fault == "version": row["version"]["revision"] += 1
            elif fault == "call": row["engine_call_id"] += 1
            elif fault == "origin": row["origin_raw_calls"] += 1
            elif fault == "arithmetic": row["experiment_calls"] += 1
            elif fault == "regression": row["raw_calls"] = 14
            elif fault == "wrap": row["raw_calls"] = 0x100000000
            elif fault == "boolean": rows[0]["engine_call_id"] = True
            elif fault == "missing": rows.pop()
            else: rows.append(copy.deepcopy(rows[-1]))
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
            with self.subTest(fault=fault), self.assertRaises(EvidenceError):
                AuditLog(source / "audit", require_closed=True)

    def test_raw_sidecar_is_required_hash_bound_and_incremental(self):
        source, _, _ = self.source()
        trajectory = self.bundle(source)
        path = trajectory.directory / "audit" / counter.RAW_FILE
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaises(EvidenceError):
            Trajectory.load(trajectory.directory)
        (source / "audit" / counter.RAW_FILE).unlink()
        with self.assertRaises(EvidenceError):
            AuditTail(source / "audit")

    def test_scope_recipe_observation_and_capabilities_cannot_be_forged(self):
        _, model, initial = self.source()
        for key in ("recipe", "scope", "flag"):
            value = copy.deepcopy(initial)
            if key == "recipe": value["initialization"].pop("sound_counter")
            elif key == "scope": value["state"]["sound_effects"]["counter_scope"] = "bootstrap_lifetime"
            else: value["observation"]["counter_origin_bound"] = False
            with self.subTest(key=key), self.assertRaises(EvidenceError):
                _validate_initial(value)
        for hello in ({"game": model.game, "capabilities": {counter.MODE: True}},
                      {"game": {}, "capabilities": {counter.METHOD: True}}):
            with self.assertRaises(EvidenceError):
                counter.negotiate(hello)

    def test_late_origin_or_reseed_and_changed_activation_owner_fail(self):
        for fault in ("late", "reseed", "owner", "below_attach"):
            source, model, _ = self.source(fault)
            path = source / "audit/events.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            index = next(i for i, e in enumerate(rows) if e["kind"] == counter.EVENT)
            if fault == "late":
                for k in ("kind", "payload", "version"):
                    rows[index][k], rows[index+1][k] = rows[index+1][k], rows[index][k]
            elif fault == "reseed":
                rows[index+1]["kind"], rows[index+1]["payload"] = "rng_seeded", {"request_id": "other", "seed": 42}
            elif fault == "owner":
                for k in ("raw_before", "raw_after"):
                    rows[index]["payload"]["counter_origin"][k]["primary_thread"] += 1
            else:
                activation_path = source / "audit" / audio.EVIDENCE
                value = json.loads(activation_path.read_text())
                value["recorder_attach"]["calls"] = 15
                activation_path.write_text(json.dumps(value))
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
            with self.subTest(fault=fault), self.assertRaises(EvidenceError):
                AuditLog(source / "audit", require_closed=True)

    def test_engine_and_counter_raw_streams_keep_their_independent_identity(self):
        source, _, _ = self.source(animation_handle=65538, particle_pointer=0x12000300, spawn_schedule={})
        trajectory = self.bundle(source)
        self.assertIsNotNone(trajectory.audit.frames[0].raw_engine_call)
        report = replay(trajectory, self.initializer(animation_handle=131074, particle_pointer=0x22000300, spawn_schedule={}),
                        self.root / "all-streams")
        self.assertTrue(report["equal"])


if __name__ == "__main__":
    unittest.main()
