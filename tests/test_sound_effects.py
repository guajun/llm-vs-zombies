"""Allocation-none contract tests; synthetic evidence never proves live determinism."""
import copy
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from llm_vs_zombies import sound_effects as audio
from llm_vs_zombies.audit_compare import AuditLog, AuditTail, EvidenceError, _seeded_rng, canonical
from llm_vs_zombies.client import Client
from llm_vs_zombies.engine_replay import (ReplayDivergence, ReplaySession, Trajectory, build_trajectory,
                                        capture_initial, identity_from_launcher, replay, _validate_initial)
from llm_vs_zombies.session import SessionTrace
from test_engine_calls import CallTransport
from test_draw_schedule import DRAW_SPEC
from test_engine_replay import ModelTransport, ARTIFACTS


BOOTSTRAP = "b" * 64
SPEC = {"mode": audio.MODE, "installed": True, "activation": "before_primary_thread_resume",
    "entry_rva": 0x1c7650, "original_bytes": audio.ORIGINAL, "owner_pinned": True,
    "engine_sha256": audio.ENGINE_SHA256, "bootstrap_sha256": BOOTSTRAP,
    "original_pitch_variation_code": True, "sound_allocation": "always_null", "music_virtualized": False,
    "original_engine_bitwise_unmodified": False, "live_verified": False, "required_evidence": audio.EVIDENCE,
    "closed_event": "sound_effects_closed", "state_scope": audio.SCOPE}
ASSETS = copy.deepcopy(ARTIFACTS)
ASSETS["module_hashes"]["lvz-bootstrap.dll"] = BOOTSTRAP
ASSETS["input_hashes"]["game/local-engine/PlantsVsZombies.exe"] = audio.ENGINE_SHA256


def activation(owner=0x10000000):
    replacement = owner + 0x2000
    receipt = {"mode": audio.MODE, "installed": True, "before_primary_thread_resume": True,
        "phase": "sealed_before_resume", "primary_thread": 42, "owner_module": owner, "owner_pinned": True,
        "entry": 0x5c7650, "replacement": replacement, "patch_owned": True, "calls": 0, "errors": 0,
        "preexisting_app": 0, "original_bytes": audio.ORIGINAL,
        "patch_bytes": [0xe9, *int((replacement - 0x5c7655) & 0xffffffff).to_bytes(4, "little"), 0x90],
        "engine_sha256": audio.ENGINE_SHA256, "bootstrap_sha256": BOOTSTRAP}
    return {"schema": "lvz.audio-activation.v1", "configuration": copy.deepcopy(SPEC),
            "pre_resume": receipt, "recorder_attach": dict(receipt, calls=3)}


def sound_state(tick=0):
    return {"mode": audio.MODE, "calls": 5 + tick * 2, "errors": 0, "app_update_count": 1296 + tick,
        "active_types": 1,
        "histories": [{"last_variation": 0xffffffff, "slots": [[0, 0, 0, 800, 3] for _ in range(8)]} for _ in range(110)],
        "parameters": [{"type": 0, "pitch_bits": 0, "flags": 4,
                        "sound_id_rvas": [0x2a71a0, *([None] * 9)], "sound_ids": [9, *([None] * 9)]}],
        "channels": [0] * 32, "slots_empty": True, "patch_owned": True}


def health(calls):
    return {"mode": audio.MODE, "healthy": True, "calls": calls, "errors": 0, "patch_owned": True,
        "owner_pinned": True, "slots_empty": True,
        "scope": "allocation-none SFX experiment; music/exhaustive determinism unverified"}


class AudioTransport(CallTransport):
    def __init__(self, directory, *, audio_fault=None, owner=0x10000000, enabled=True, **kwargs):
        self.audio_fault, self.audio_enabled = audio_fault, enabled
        super().__init__(directory, **kwargs)
        if enabled:
            self.game["sound_effects"] = copy.deepcopy(SPEC)
            (directory / "manifest.json").write_text(json.dumps(self.game), encoding="utf-8")
            (directory / audio.EVIDENCE).write_text(json.dumps(activation(owner)), encoding="utf-8")

    def state(self):
        result = super().state()
        if self.audio_enabled:
            result["sound_effects"] = sound_state(self.tick)
            if self.audio_fault == "history" and self.tick:
                result["sound_effects"]["histories"][0]["slots"][0][3] += 1
            if self.audio_fault == "app" and self.tick:
                result["sound_effects"]["app_update_count"] += 1
            if self.audio_fault == "rollback" and self.tick == 2:
                result["sound_effects"]["calls"] = 6
            if self.audio_fault == "occupied" and self.tick:
                result["sound_effects"]["histories"][109]["slots"][7][0] = 0x12000000
        return result

    def event(self, kind, payload):
        if kind == "recording_closed" and self.audio_enabled:
            ModelTransport.event(self, kind, payload)
            ModelTransport.event(self, "engine_call_closed", self.health())
            value = health(self.state()["sound_effects"]["calls"])
            if self.audio_fault == "unhealthy":
                value["healthy"] = False
                value["failure"] = "synthetic native failure"
            if self.audio_fault == "close_extra":
                value["calls"] += 1
            if self.audio_fault == "close_rollback":
                value["calls"] -= 1
            if self.audio_fault != "missing_close":
                ModelTransport.event(self, "sound_effects_closed", value)
            ModelTransport.event(self, "draw_schedule_closed", dict(self.counts(), installed=True, latched=True,
                sealed=True, active=False, automatic_allowed=0, automatic_denied=self.diagnostic,
                controlled_calls=self.warm_frames + self.step_frames, faults=0, wrong_thread_calls=0, healthy=True))
            return
        super().event(kind, payload)

    def exchange(self, payload, timeout):
        result = json.loads(super().exchange(payload, timeout))
        if json.loads(payload)["method"] == "hello":
            result["result"]["capabilities"][audio.MODE] = self.audio_enabled
        return json.dumps(result).encode()


class SoundEffectsTests(unittest.TestCase):
    def test_sound_id_word_must_fit_actual_data_section(self):
        for rva in (0x299000, 0x35dc18):
            value = sound_state()
            value["parameters"][0]["sound_id_rvas"][0] = rva
            audio.state({"sound_effects": value}, {"sound_effects": SPEC})
        for rva in (0x1000, 0x298fff, 0x35dc19, 0x35e000, 0x393ffc):
            with self.subTest(rva=hex(rva)):
                value = sound_state()
                value["parameters"][0]["sound_id_rvas"][0] = rva
                with self.assertRaises(EvidenceError):
                    audio.state({"sound_effects": value}, {"sound_effects": SPEC})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, model, client):
        identity = identity_from_launcher(client.hello(), ASSETS)
        client.observe()
        client.request("prepare_render", expect=client.version)
        recipe = {"seed": 42, "execution_mode": DRAW_SPEC["mode"], "draw_schedule": DRAW_SPEC,
            "clock_anchor": model.clocks(), "postwarm_clock": model.clocks(),
            "render_preparation": {"warm_frames": 1, "step_frames": 0, "verified_before_draw": True,
                "seeded_rng_sha256": hashlib.sha256(canonical(_seeded_rng(42))).hexdigest(),
                "postwarm_rng_sha256": hashlib.sha256(canonical(model.rng)).hexdigest()}}
        if model.audio_enabled:
            recipe["sound_effects"] = {"configuration": copy.deepcopy(SPEC),
                "b0_state_sha256": audio.state_sha256(model.state()["sound_effects"])}
        return identity, recipe

    def source(self, name="source", **kwargs):
        directory = self.root / name
        model = AudioTransport(directory / "audit", zero_tick=None, **kwargs)
        with SessionTrace(directory / "trace.jsonl") as trace, Client(model, trace=trace) as client:
            identity, recipe = self.prepare(model, client)
            initial = capture_initial(client, identity=identity, initialization=recipe)
            client.advance(3)
            client.request("stop_recording", expect=client.version)
        return directory, initial

    def bundle(self, directory):
        return build_trajectory(directory / "trace.jsonl", directory / "audit", directory / "bundle")

    def initializer(self, **kwargs):
        @contextmanager
        def initialize(trajectory, output):
            self.actual = AudioTransport(output / "audit", zero_tick=None, epoch=99, revision=2, owner=0x20000000, **kwargs)
            with SessionTrace(output / "trace.jsonl") as trace, Client(self.actual, trace=trace) as client:
                identity, _ = self.prepare(self.actual, client)
                yield ReplaySession(client, identity, output / "audit")
                client.request("stop_recording", expect=client.version)
        return initialize

    def test_new_mode_full_replay_and_seek_validate_static_receipts_and_counters(self):
        source, _ = self.source()
        trajectory = self.bundle(source)
        self.assertIn("audit/" + audio.EVIDENCE, trajectory.manifest["files"])
        for tick in (None, 0, 2):
            with self.subTest(tick=tick):
                report = replay(trajectory, self.initializer(), self.root / f"actual-{tick}", target_tick=tick)
                self.assertTrue(report["equal"])
                self.assertTrue(report["sound_effects"]["activation_evidence_verified"])
                self.assertTrue(report["sound_effects"]["final_health_verified"])
                self.assertEqual(report["sound_effects"]["actual_health"]["calls"], 5 + 2 * (3 if tick is None else tick))

    def test_old_default_mode_archive_and_replay_are_preserved(self):
        source, _ = self.source(enabled=False)
        trajectory = self.bundle(source)
        self.assertNotIn("audit/" + audio.EVIDENCE, trajectory.manifest["files"])
        report = replay(trajectory, self.initializer(enabled=False), self.root / "actual")
        self.assertTrue(report["equal"])
        self.assertEqual(report["sound_effects"]["mode"], "original")

    def test_cross_mode_replay_stops_before_any_recorded_action(self):
        for enabled in (False, True):
            source, _ = self.source(str(enabled), enabled=enabled)
            with self.assertRaises(ReplayDivergence):
                replay(self.bundle(source), self.initializer(enabled=not enabled), self.root / f"cross-{enabled}")
            self.assertFalse(any(r["method"] in {"advance", "commit"} for r in self.actual.requests))

    def test_activation_late_old_bootstrap_changed_owner_and_fake_jump_rejected(self):
        edits = [lambda d: d["pre_resume"].update(calls=1),
                 lambda d: d["pre_resume"].update(before_primary_thread_resume=False),
                 lambda d: d["pre_resume"].update(phase="after_resume"),
                 lambda d: d["pre_resume"].update(preexisting_app=0x600000),
                 lambda d: d["pre_resume"].update(bootstrap_sha256="c" * 64),
                 lambda d: d["recorder_attach"].update(primary_thread=43),
                 lambda d: d["recorder_attach"].update(replacement=123),
                 lambda d: d["pre_resume"].update(calls=False),
                 lambda d: d["configuration"].update(installed=1)]
        for index, edit in enumerate(edits):
            with self.subTest(index=index):
                value = activation()
                edit(value)
                with self.assertRaises(EvidenceError):
                    audio.activation(value, {"sound_effects": SPEC})

    def test_bootstrap_artifact_and_b0_recipe_are_bound(self):
        _, initial = self.source()
        for key in ("bootstrap", "engine", "missing_engine", "recipe", "b0"):
            value = copy.deepcopy(initial)
            if key == "bootstrap":
                value["identity"]["artifacts"]["module_hashes"]["lvz-bootstrap.dll"] = "c" * 64
            elif key == "engine":
                value["identity"]["artifacts"]["input_hashes"]["game/local-engine/PlantsVsZombies.exe"] = "c" * 64
            elif key == "missing_engine":
                value["identity"]["artifacts"]["input_hashes"].pop("game/local-engine/PlantsVsZombies.exe")
            elif key == "recipe":
                value["initialization"]["sound_effects"]["configuration"]["installed"] = 1
            else:
                value["state"]["sound_effects"]["calls"] += 1
            with self.subTest(key=key), self.assertRaises(EvidenceError):
                _validate_initial(value)

    def test_nonempty_slots_channels_and_bad_scalar_types_are_rejected(self):
        edits = [lambda s: s["histories"][109]["slots"][7].__setitem__(0, 100),
                 lambda s: s["histories"][0]["slots"][0].__setitem__(1, 1),
                 lambda s: s["histories"][0]["slots"][0].__setitem__(2, False),
                 lambda s: s["channels"].__setitem__(31, 100),
                 lambda s: s["channels"].__setitem__(0, False),
                 lambda s: s.update(calls=True), lambda s: s["histories"].pop(),
                 lambda s: s["parameters"][0]["sound_ids"].__setitem__(0, None)]
        for index, edit in enumerate(edits):
            value = sound_state()
            edit(value)
            with self.subTest(index=index), self.assertRaises(EvidenceError):
                audio.state({"sound_effects": value}, {"sound_effects": SPEC})

    def test_source_rollback_occupied_unhealthy_or_missing_footer_cannot_package(self):
        for fault in ("rollback", "occupied", "unhealthy", "missing_close", "close_rollback"):
            source, _ = self.source(fault, audio_fault=fault)
            with self.subTest(fault=fault), self.assertRaises(EvidenceError):
                self.bundle(source)

    def test_actual_history_app_and_close_counters_are_not_normalized_away(self):
        source, _ = self.source()
        trajectory = self.bundle(source)
        for fault in ("history", "app", "close_extra"):
            with self.subTest(fault=fault), self.assertRaises(ReplayDivergence):
                replay(trajectory, self.initializer(audio_fault=fault), self.root / fault)
            self.assertFalse(json.loads((self.root / fault / "replay-report.json").read_text())["equal"])

    def test_actual_missing_footer_and_unhealthy_branch_cannot_publish_equal(self):
        source, _ = self.source()
        trajectory = self.bundle(source)
        for fault in ("missing_close", "unhealthy"):
            with self.subTest(fault=fault), self.assertRaises(EvidenceError):
                replay(trajectory, self.initializer(audio_fault=fault), self.root / fault)
            self.assertFalse(json.loads((self.root / fault / "replay-report.json").read_text())["equal"])
        def takeover(session, _link):
            self.actual.audio_fault = "unhealthy"
            session.client.advance(1)
        with self.assertRaises(EvidenceError):
            replay(trajectory, self.initializer(), self.root / "branch", target_tick=1, on_takeover=takeover)

    def test_static_activation_is_bound_in_bundle_and_live_tail(self):
        source, _ = self.source()
        trajectory = self.bundle(source)
        activation_path = trajectory.directory / "audit" / audio.EVIDENCE
        activation_path.write_text(json.dumps(activation(0x30000000)))
        with self.assertRaises(EvidenceError):
            Trajectory.load(trajectory.directory)
        tail = AuditTail(source / "audit")
        (source / "audit" / audio.EVIDENCE).write_text(json.dumps(activation(0x30000000)))
        with self.assertRaises(EvidenceError):
            list(tail.read_request(None))

    def test_required_file_cannot_be_missing_or_undeclared(self):
        source, _ = self.source()
        path = source / "audit" / audio.EVIDENCE
        path.unlink()
        with self.assertRaises(EvidenceError):
            AuditLog(source / "audit", require_closed=True)
        legacy, _ = self.source("legacy", enabled=False)
        (legacy / "audit" / audio.EVIDENCE).write_text(json.dumps(activation()))
        with self.assertRaises(EvidenceError):
            AuditLog(legacy / "audit", require_closed=True)

    def test_activation_attach_b0_and_first_frame_order(self):
        source, initial = self.source()
        path = source / "audit" / audio.EVIDENCE
        value = json.loads(path.read_text())
        value["recorder_attach"]["calls"] = 6
        path.write_text(json.dumps(value))
        with self.assertRaises(EvidenceError):
            AuditLog(source / "audit", require_closed=True)
        value["recorder_attach"]["calls"] = 3
        path.write_text(json.dumps(value))
        audit = AuditLog(source / "audit", require_closed=True)
        initial["state"]["sound_effects"]["calls"] = 6
        initial["initialization"]["sound_effects"]["b0_state_sha256"] = audio.state_sha256(initial["state"]["sound_effects"])
        with self.assertRaises(EvidenceError):
            audit.validate_audio_initial(initial)

    def test_capability_and_null_mode_are_fail_closed(self):
        for hello in ({"game": {"sound_effects": SPEC}, "capabilities": {}},
                      {"game": {}, "capabilities": {audio.MODE: True}},
                      {"game": {"sound_effects": None}, "capabilities": {audio.MODE: False}}):
            with self.assertRaises(EvidenceError):
                audio.negotiate(hello)

    def test_six_footer_sequence_includes_particle_spawn_and_audio_health(self):
        source, _ = self.source(particle_pointer=0x12000300, spawn_schedule={})
        trajectory = self.bundle(source)
        footer = list(trajectory.audit.events)[-6:]
        self.assertEqual([e["kind"] for e in footer], ["recording_closed", "engine_call_closed",
            "sound_effects_closed", "draw_schedule_closed", "particle_shake_closed", "spawn_hook_closed"])
        report = replay(trajectory, self.initializer(particle_pointer=0x22000300, spawn_schedule={}),
                        self.root / "all-modes")
        self.assertTrue(report["equal"])

    def test_duplicate_misordered_and_wrong_boundary_footer_rejected(self):
        for fault in ("duplicate", "order", "version"):
            source, _ = self.source(fault)
            path = source / "audit/events.jsonl"
            events = [json.loads(line) for line in path.read_text().splitlines()]
            index = next(i for i, e in enumerate(events) if e["kind"] == "sound_effects_closed")
            if fault == "duplicate":
                extra = copy.deepcopy(events[index])
                extra["seq"] += 1
                events.insert(index + 1, extra)
                for event in events[index + 2:]:
                    event["seq"] += 1
            elif fault == "order":
                for key in ("kind", "payload"):
                    events[index][key], events[index + 1][key] = events[index + 1][key], events[index][key]
            else:
                events[index]["version"]["revision"] += 1
            path.write_text("".join(json.dumps(e) + "\n" for e in events))
            with self.subTest(fault=fault), self.assertRaises(EvidenceError):
                AuditLog(source / "audit", require_closed=True)


if __name__ == "__main__":
    unittest.main()
