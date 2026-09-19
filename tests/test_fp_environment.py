"""Evidence/transport fixtures only; these tests do not execute the original game."""
import copy
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest

from llm_vs_zombies import fp_environment as fp
from llm_vs_zombies.audit_compare import AuditLog, AuditTail, EvidenceError
from llm_vs_zombies.client import Client
from llm_vs_zombies.engine_replay import (ReplayDivergence, ReplaySession, Trajectory,
    _validate_initial, build_trajectory, capture_initial, replay)
from llm_vs_zombies.session import SessionTrace
from test_engine_replay import ModelTransport
from test_sound_counter import CounterTransport
import test_sound_counter


class FPTransport(CounterTransport):
    def __init__(self, directory, *, fp_before=127, fp_thread=1234, fp_status=32,
                 fp_enabled=True, fp_fault=None, fp_extra=1, **kwargs):
        self.fp_thread, self.fp_status = fp_thread, fp_status
        self.fp_enabled, self.fp_fault, self.fp_extra = fp_enabled, fp_fault, fp_extra
        self.fp_snapshots = self.fp_pre = self.fp_post = 0
        self.fp_closed = False
        super().__init__(directory, **kwargs)
        if fp_enabled:
            self.game["fixed_fp"] = copy.deepcopy(fp.SPEC)
            (directory / "manifest.json").write_text(json.dumps(self.game))
            self.files[fp.RAW_FILE] = (directory / fp.RAW_FILE).open("w", encoding="utf-8", newline="\n")
        self.fp_activation = {"schema": "lvz.fp-activation.v1", "mode": fp.MODE,
            "request_id": "initialize-" + str(fp_thread), "version": self.version(),
            "phase": "initialize_before_seed_and_enter_game", "owner_thread_id": fp_thread,
            "actual_thread_id": fp_thread, "game_ui": 1, "board_address": 0,
            "before": dict(self.fp_raw(), x87_control=fp_before), "after": self.fp_raw(),
            "write_attempted": True, "activation_count": 1, "ok": True, "first_fault": None}
        if fp_enabled and fp_fault not in {"missing_activation", "late_activation"}:
            self.event(fp.EVENT, copy.deepcopy(self.fp_activation))
            if fp_fault == "duplicate_activation":
                self.event(fp.EVENT, copy.deepcopy(self.fp_activation))
        if fp_fault == "native_fault":
            self.event("fp_environment_fault", {"reason": "fixture drift", "raw": self.fp_raw()})

    def fp_raw(self):
        return {"x87_control": 639, "x87_status": self.fp_status,
                "mxcsr": 8064 | (self.fp_status & 63), "mxcsr_control": 8064}

    def raw_calls(self):
        # Recording-lifetime instrumentation must not reset with controller
        # tick/epoch when the zero-clock terminal fixture changes scene.
        return self.raw_base + self.warm_calls + 2 * self.calls

    def fp_health(self):
        checks = dict.fromkeys(fp.PHASES, 0)
        checks.update(loop=20 + self.fp_extra, initialization=10 + self.fp_extra, ready=2,
            before_original_update=self.fp_post + 2 + self.fp_extra,
            after_original_update=self.fp_post + 1,
            before_warm_draw=self.warm_frames, after_warm_draw=self.warm_frames,
            before_step_draw=self.step_frames, after_step_draw=self.step_frames,
            pre_step=self.fp_pre, post_step=self.fp_post, snapshot=self.fp_snapshots,
            close=int(self.fp_closed))
        if self.fp_fault == "closed_loop_regression" and self.fp_closed:
            checks["loop"] = 0
        return {"schema": "lvz.fp-health.v1", "mode": fp.MODE, "activated": True,
            "activation_count": 1, "owner_thread_id": self.fp_thread, "closed": self.fp_closed,
            "healthy": True, "wrong_thread_checks": 0, "raw_frames": self.fp_pre + self.fp_post,
            "checks": checks, "first_fault": None, "last_raw": self.fp_raw()}

    def state(self):
        self.fp_snapshots += 1
        value = super().state()
        value["fp_environment"] = {"x87_control": 639, "mxcsr_control": 8064}
        if self.fp_fault == "state_drift" and self.tick:
            value["fp_environment"]["x87_control"] = 127
        return value

    def event(self, kind, payload):
        super().event(kind, payload)
        if kind == "rng_seeded" and self.fp_fault == "late_activation":
            self.fp_activation["version"] = self.version()
            super().event(fp.EVENT, self.fp_activation)

    def write(self, name, value):
        super().write(name, value)
        if not self.fp_enabled:
            return
        if name == "checksums.jsonl":
            if value["kind"] == "pre_step":
                self.fp_pre += 1
            else:
                self.fp_post += 1
            raw = {"schema": "lvz.fp-environment-raw.v1", "seq": value["seq"], "kind": value["kind"],
                "version": value["version"], "engine_call_id": value["payload"]["engine_call"]["engine_call_id"],
                "payload": dict(self.fp_raw(), owner_thread_id=self.fp_thread, actual_thread_id=self.fp_thread)}
            if self.fp_fault == "live_raw" and self.tick >= 1:
                raw["payload"]["x87_control"] = 127
            ModelTransport.write(self, fp.RAW_FILE, raw)
        if name == "events.jsonl" and value["kind"] == "engine_call_closed":
            self.fp_closed = True
            if self.fp_fault != "missing_close":
                ModelTransport.event(self, fp.CLOSED, self.fp_health())

    def exchange(self, payload, timeout):
        result = json.loads(super().exchange(payload, timeout))
        method = json.loads(payload)["method"]
        if method == "hello" and self.fp_enabled:
            result["result"]["capabilities"][fp.MODE] = True
        elif method == "audit_snapshot" and self.fp_enabled:
            result["result"]["fixed_fp"] = {"activation": copy.deepcopy(self.fp_activation), "health": self.fp_health()}
        return json.dumps(result).encode()


class FixedFPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def prepare(self, client, model, app_count=None):
        identity, recipe = test_sound_counter.SoundCounterTests.prepare(self, client, model, app_count)
        if model.fp_enabled:
            recipe["fixed_fp"] = copy.deepcopy(fp.SPEC)
        return identity, recipe

    def source(self, name="source", **kwargs):
        path = self.root / name
        model = FPTransport(path / "audit", zero_tick=kwargs.pop("zero_tick", None), app_count=1362, **kwargs)
        with SessionTrace(path / "trace.jsonl") as trace, Client(model, trace=trace) as client:
            identity, recipe = self.prepare(client, model)
            initial = capture_initial(client, identity=identity, initialization=recipe)
            client.advance(3)
            client.request("stop_recording", expect=client.version)
        return path, model, initial

    def bundle(self, path):
        return build_trajectory(path / "trace.jsonl", path / "audit", path / "bundle")

    def initializer(self, **kwargs):
        @contextmanager
        def initialize(trajectory, output):
            options = dict(kwargs)
            self.actual = FPTransport(output / "audit", zero_tick=options.pop("zero_tick", None), epoch=99, revision=2,
                raw_base=16, app_count=1427, fp_before=639, fp_thread=5678, fp_status=1, fp_extra=25, **options)
            with SessionTrace(output / "trace.jsonl") as trace, Client(self.actual, trace=trace) as client:
                identity, _ = self.prepare(client, self.actual,
                    trajectory.initial["initialization"]["app_update_anchor"]["app_update_count"])
                yield ReplaySession(client, identity, output / "audit")
                client.request("stop_recording", expect=client.version)
        return initialize

    def test_different_actual_activation_inputs_full_replay_and_seek(self):
        path, _, initial = self.source()
        trajectory = self.bundle(path)
        self.assertEqual(initial["fixed_fp_origin"]["activation"]["before"]["x87_control"], 127)
        self.assertIn("audit/" + fp.RAW_FILE, trajectory.manifest["files"])
        for tick in (None, 0, 2):
            with self.subTest(tick=tick):
                report = replay(trajectory, self.initializer(), self.root / str(tick), target_tick=tick)
                self.assertTrue(report["equal"])
                proof = report["fixed_fp"]
                self.assertTrue(proof["activation_compared"] and proof["final_health_verified"])
                self.assertEqual(proof["raw_boundaries_verified"], 2 * (3 if tick is None else tick))
                self.assertFalse(proof["state_normalized"])
                self.assertNotEqual(proof["source_health"]["checks"]["loop"], proof["actual_health"]["checks"]["loop"])

    def test_old_mode_readable_and_cross_mode_rejected_before_actions(self):
        path, _, _ = self.source(fp_enabled=False)
        trajectory = self.bundle(path)
        self.assertTrue(replay(trajectory, self.initializer(fp_enabled=False), self.root / "old")["equal"])
        with self.assertRaises(ReplayDivergence):
            replay(trajectory, self.initializer(), self.root / "cross")
        self.assertFalse(any(x["method"] in {"advance", "commit"} for x in self.actual.requests))

    def test_receipt_rejects_wrong_owner_phase_late_board_or_status_erasure(self):
        _, model, _ = self.source()
        for field, value in (("actual_thread_id", 9), ("owner_thread_id", True), ("game_ui", 3),
                             ("board_address", 4), ("activation_count", True), ("ok", False),
                             ("write_attempted", False), ("phase", "at_B0"), ("first_fault", {})):
            bad = copy.deepcopy(model.fp_activation); bad[field] = value
            with self.subTest(field=field), self.assertRaises(EvidenceError):
                fp.activation(bad, model.game)
        for mutate in (lambda r:r["after"].update(x87_status=0), lambda r:r["after"].update(mxcsr=8064),
                       lambda r:r["after"].update(x87_control=127), lambda r:r["version"].update(tick=1)):
            bad = copy.deepcopy(model.fp_activation); mutate(bad)
            with self.assertRaises(EvidenceError):
                fp.activation(bad, model.game)

    def test_missing_duplicate_late_activation_fault_and_missing_close(self):
        for fault in ("missing_activation", "duplicate_activation", "late_activation", "native_fault", "missing_close"):
            path, _, _ = self.source(fault, fp_fault=fault)
            with self.subTest(fault=fault), self.assertRaises(EvidenceError):
                self.bundle(path)

    def test_raw_bindings_cardinality_owner_and_controls(self):
        for fault in ("seq", "version", "kind", "call", "owner", "boolean", "mask", "drift", "missing", "extra", "reordered"):
            path, _, _ = self.source(fault)
            rawfile = path / "audit" / fp.RAW_FILE
            rows = [json.loads(line) for line in rawfile.read_text().splitlines()]
            row = rows[2]
            if fault == "seq": row["seq"] += 1
            elif fault == "version": row["version"]["revision"] += 1
            elif fault == "kind": row["kind"] = "post_step"
            elif fault == "call": row["engine_call_id"] += 1
            elif fault == "owner": row["payload"]["actual_thread_id"] += 1
            elif fault == "boolean": row["engine_call_id"] = True
            elif fault == "mask": row["payload"]["mxcsr_control"] += 1
            elif fault == "drift": row["payload"]["x87_control"] = 127
            elif fault == "missing": rows.pop()
            elif fault == "extra": rows.append(copy.deepcopy(rows[-1]))
            else: rows[1], rows[2] = rows[2], rows[1]
            rawfile.write_text("".join(json.dumps(row) + "\n" for row in rows))
            with self.subTest(fault=fault), self.assertRaises(EvidenceError):
                self.bundle(path)

    def test_close_monitor_counts_and_guard_coverage(self):
        for key in ("pre_step", "post_step", "before_original_update", "after_original_update", "before_warm_draw",
                    "after_warm_draw", "before_step_draw", "after_step_draw", "ready", "snapshot", "close",
                    "raw_frames", "healthy", "wrong_thread_checks"):
            path, _, _ = self.source(key)
            file = path / "audit/events.jsonl"
            rows = [json.loads(line) for line in file.read_text().splitlines()]
            health = next(row["payload"] for row in rows if row["kind"] == fp.CLOSED)
            if key in health["checks"]: health["checks"][key] = 0
            elif key == "healthy": health[key] = False
            else: health[key] += 1
            file.write_text("".join(json.dumps(row) + "\n" for row in rows))
            with self.subTest(key=key), self.assertRaises(EvidenceError):
                self.bundle(path)

    def test_marker_and_recipe_must_link_to_real_activation(self):
        path, model, initial = self.source()
        log = AuditLog(path / "audit", require_closed=True)
        for fault in ("recipe", "missing_origin", "late_origin", "different_receipt", "wrong_state", "missing_warm"):
            bad = copy.deepcopy(initial)
            if fault == "recipe": bad["initialization"].pop("fixed_fp")
            elif fault == "missing_origin": bad.pop("fixed_fp_origin")
            elif fault == "late_origin": bad["fixed_fp_origin"]["activation"]["version"]["epoch"] += 5
            elif fault == "different_receipt": bad["fixed_fp_origin"]["activation"]["request_id"] += "changed"
            elif fault == "wrong_state": bad["state"]["fp_environment"]["x87_control"] = 127
            else: bad["fixed_fp_origin"]["health"]["checks"]["before_warm_draw"] = 0
            with self.subTest(fault=fault), self.assertRaises(EvidenceError):
                _validate_initial(bad); log.validate_fp_initial(bad)
        hello = {"game": model.game, "capabilities": {fp.MODE: False}}
        with self.assertRaises(EvidenceError): fp.negotiate(hello)
        hello["game"] = copy.deepcopy(model.game); hello["game"]["fixed_fp"]["x87_control"] = 127
        hello["capabilities"][fp.MODE] = True
        with self.assertRaises(EvidenceError): fp.negotiate(hello)

    def test_undeclared_raw_cannot_upgrade_legacy(self):
        path, _, _ = self.source(fp_enabled=False)
        (path / "audit" / fp.RAW_FILE).write_text("")
        with self.assertRaises(EvidenceError): self.bundle(path)

    def test_tail_and_replay_fail_on_actual_raw_or_semantic_drift(self):
        path, _, _ = self.source()
        trajectory = self.bundle(path)
        for fault in ("live_raw", "state_drift", "missing_close"):
            with self.subTest(fault=fault), self.assertRaises(EvidenceError):
                replay(trajectory, self.initializer(fp_fault=fault), self.root / fault)
            self.assertFalse(json.loads((self.root / fault / "replay-report.json").read_text())["equal"])

    def test_takeover_full_branch_retains_extra_guard_counts(self):
        path, _, _ = self.source()
        trajectory = self.bundle(path)
        report = replay(trajectory, self.initializer(), self.root / "branch", target_tick=2,
                        on_takeover=lambda session, meta: session.client.advance(1))
        self.assertTrue(report["equal"])
        self.assertEqual(report["fixed_fp"]["health_scope"], "full_branch")
        self.assertEqual(report["fixed_fp"]["raw_boundaries_verified"], 4)
        self.assertEqual(report["fixed_fp"]["actual_health"]["raw_frames"], 6)

    def test_takeover_new_decoder_rebinds_original_b0_monitor(self):
        path, _, _ = self.source()
        trajectory = self.bundle(path)
        with self.assertRaisesRegex(EvidenceError, "counts precede B0"):
            replay(trajectory, self.initializer(fp_fault="closed_loop_regression"), self.root / "bad-branch", target_tick=2,
                   on_takeover=lambda session, meta: session.client.advance(1))
        self.assertFalse(json.loads((self.root / "bad-branch/replay-report.json").read_text())["equal"])

    def test_all_sidecars_and_full_seven_footer_records(self):
        options = dict(animation_handle=0x10002, particle_pointer=0x10000000, spawn_schedule={1: [11]})
        path, _, _ = self.source(**options)
        trajectory = self.bundle(path)
        report = replay(trajectory, self.initializer(**options), self.root / "all-streams")
        self.assertTrue(report["equal"])
        self.assertEqual(report["fixed_fp"]["raw_boundaries_verified"], 6)
        self.assertTrue(report["spawn_exercised"])

    def test_zero_terminal_has_raw_pair_without_extra_draw_and_seek_stops_before_it(self):
        path, _, _ = self.source(zero_tick=2)
        trajectory = self.bundle(path)
        report = replay(trajectory, self.initializer(zero_tick=2), self.root / "terminal")
        self.assertTrue(report["equal"])
        self.assertEqual(report["fixed_fp"]["raw_boundaries_verified"], 6)
        self.assertEqual(report["fixed_fp"]["actual_health"]["checks"]["after_step_draw"], 2)
        report = replay(trajectory, self.initializer(zero_tick=2), self.root / "before-terminal", target_tick=2)
        self.assertEqual(report["fixed_fp"]["raw_boundaries_verified"], 4)
        self.assertEqual(report["fixed_fp"]["actual_health"]["raw_frames"], 4)

    def test_bundle_rejects_changed_sidecar_after_packaging(self):
        path, _, _ = self.source()
        trajectory = self.bundle(path)
        raw = path / "bundle/audit" / fp.RAW_FILE
        raw.write_bytes(raw.read_bytes().replace(b'"x87_status":32', b'"x87_status":33', 1))
        with self.assertRaises(EvidenceError): Trajectory.load(trajectory.directory)
