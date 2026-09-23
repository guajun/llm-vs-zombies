"""One declared B(0) normalization table: shape, receipt, migration, identity."""
import copy
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest

from llm_vs_zombies import b0_normalization as b0
from llm_vs_zombies import app_update_anchor
from llm_vs_zombies import mj_clock_anchor
from llm_vs_zombies import evaluation as ev
from llm_vs_zombies.audit_compare import AuditLog, EvidenceError, read_json
from llm_vs_zombies.client import Client
from llm_vs_zombies.engine_replay import (ReplayDivergence, ReplaySession, build_trajectory,
                                        capture_initial, identity_from_launcher, replay, _validate_initial)
from llm_vs_zombies.evidence_tree import normalize_identity, root_identity
from llm_vs_zombies.initialization import apply_recipe
from llm_vs_zombies.session import SessionTrace
from test_sound_counter import CounterTransport
from test_sound_effects import ASSETS
from test_draw_schedule import DRAW_SPEC

TABLE = [
    {"field": b0.APP_UPDATE_FIELD, "target": 2048, "reason": "B(0) 前 App 循环圈数，跨世界不可比"},
    {"field": b0.MJ_CLOCK_FIELD, "target": 2048, "reason": "同一循环的绝对计数，舞者相位读它"},
]


class NormalizationTransport(CounterTransport):
    """One ordered table, one revision, one receipt; the real pre-values differ."""

    def __init__(self, directory, *, b0_mode=True, b0_fault=None, app_count=1295, mj=1307,
                 anchored_mode=False, **kwargs):
        self.app_count, self.mj_base = app_count, mj
        self.b0_mode, self.b0_fault = b0_mode, b0_fault
        self.normalized, self.receipt_value = False, None
        super().__init__(directory, anchored_mode=anchored_mode, counter_mode=True, app_count=app_count, **kwargs)
        if b0_mode:
            self.game[b0.METHOD] = copy.deepcopy(b0.SPEC)
            (directory / "manifest.json").write_text(json.dumps(self.game), encoding="utf-8")

    def clocks(self):
        value = super().clocks()
        value["mj_clock"] = self.mj_base + self.calls
        return value

    def value_of(self, field):
        node = self.state()
        for token in field[1:].split("/"):
            node = node[token]
        return node

    def observe(self):
        value = super().observe()
        if self.b0_mode:
            value["b0_normalized"] = self.normalized
        return value

    def exchange(self, payload, timeout):
        request = json.loads(payload)
        if request["method"] == b0.METHOD:
            self.requests.append(request)
            entries = request["params"]["entries"]
            before_state, pre = self.state(), self.version()
            before = [{"field": item["field"], "value": self.value_of(item["field"])} for item in entries]
            for item in entries:
                if item["field"] == b0.APP_UPDATE_FIELD:
                    self.app_count = item["target"] - self.tick
                else:
                    self.mj_base = item["target"] - self.calls
            self.normalized = True
            self.revision += 2 if self.b0_fault == "extra_revision" else 1
            after = [{"field": item["field"], "value": self.value_of(item["field"])} for item in entries]
            if self.b0_fault == "readback":
                after[-1]["value"] += 1
            after_state = self.state()
            if self.b0_fault == "changed_outside":
                after_state["board"]["hidden"] += 1
            if self.b0_fault == "rng_after":
                after_state["rng"]["instances"]["global_mt"]["words"][0] += 1
            receipt = {"schema": b0.SCHEMA, "mode": b0.MODE, "requested": copy.deepcopy(entries),
                "before": before, "after": after, "before_state": before_state, "after_state": after_state,
                "before_version": pre, "after_version": self.version()}
            self.receipt_value = copy.deepcopy(receipt)
            event = {"request_id": request["request_id"], "normalization": receipt}
            if self.b0_fault != "missing_event":
                self.event(b0.EVENT, event)
            if self.b0_fault == "duplicate_event":
                self.event(b0.EVENT, event)
            if self.b0_fault == "native_failed":
                self.event(b0.FAILED_EVENT, dict(event, message="synthetic"))
            return json.dumps({"protocol": 1, "request_id": request["request_id"], "ok": True,
                "result": {"normalized": True, "normalization": receipt,
                           "observation": self.observe()}}).encode()
        result = json.loads(super().exchange(payload, timeout))
        if request["method"] == "hello":
            result["result"]["capabilities"].update({b0.MODE: self.b0_mode, b0.METHOD: self.b0_mode})
        if request["method"] == "clock_restore":
            # The restore really writes the recorded absolute counter back, so a
            # cold replay starts from the recorded pre-normalization clocks.
            self.mj_base = request["params"]["snapshot"]["mj_clock"] - self.calls
        return json.dumps(result).encode()


class UnifiedWithLegacyCapabilitiesTransport(NormalizationTransport):
    """The real DLL: it advertises the table and both legacy anchors at once.

    Nothing here is synthetic convenience: ``ProbeTarget`` gains
    ``sound_effects``, ``app_update_anchor``, ``mj_clock_anchor``,
    ``b0_normalization`` and ``sound_counter`` together whenever silent audio is
    enabled, so a unified archive really declares all three shapes. The legacy
    methods stay implemented and answer, but a unified run never calls them.
    """

    def __init__(self, directory, **kwargs):
        kwargs["anchored_mode"] = True
        super().__init__(directory, **kwargs)
        self.game[mj_clock_anchor.METHOD] = copy.deepcopy(mj_clock_anchor.SPEC)
        (directory / "manifest.json").write_text(json.dumps(self.game), encoding="utf-8")

    def exchange(self, payload, timeout):
        result = json.loads(super().exchange(payload, timeout))
        if json.loads(payload)["method"] == "hello":
            result["result"]["capabilities"].update({mj_clock_anchor.MODE: True, mj_clock_anchor.METHOD: True})
        return json.dumps(result).encode()


class B0NormalizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def source(self, name="source", *, table=TABLE, legacy=False, transport=NormalizationTransport, **kwargs):
        directory = self.root / name
        model = transport(directory / "audit", zero_tick=None, **kwargs)
        with SessionTrace(directory / "trace.jsonl") as trace, Client(model, trace=trace) as client:
            identity = identity_from_launcher(client.hello(), ASSETS)
            recipe = (apply_recipe(client, 42, app_update_count=model.app_count) if legacy
                      else apply_recipe(client, 42, b0_normalization=copy.deepcopy(table)))
            initial = capture_initial(client, identity=identity, initialization=recipe)
            client.advance(3)
            client.request("stop_recording", expect=client.version)
        return directory, model, initial

    def initializer(self, *, app_count=1400, mj=1200, transport=NormalizationTransport, **kwargs):
        @contextmanager
        def initialize(trajectory, output):
            self.actual = transport(output / "audit", zero_tick=None, epoch=99, revision=2,
                                    app_count=app_count, mj=mj, **kwargs)
            with SessionTrace(output / "trace.jsonl") as trace, Client(self.actual, trace=trace) as client:
                identity = identity_from_launcher(client.hello(), ASSETS)
                apply_recipe(client, 42, trajectory.initial["initialization"]["clock_anchor"],
                             b0_normalization=b0.requested_from_recipe(trajectory.initial["initialization"]))
                yield ReplaySession(client, identity, output / "audit")
                client.request("stop_recording", expect=client.version)
        return initialize

    def test_two_worlds_reach_one_declared_table_and_replay(self):
        first, model, initial = self.source(app_count=1295, mj=1307)
        _, other, other_initial = self.source("other", app_count=1500, mj=1340)
        # The real pre-values differ; the declared table is one common target.
        self.assertEqual([item["value"] for item in model.receipt_value["before"]], [1295, 1307])
        self.assertEqual([item["value"] for item in other.receipt_value["before"]], [1500, 1340])
        self.assertEqual([item["value"] for item in model.receipt_value["after"]], [2048, 2048])
        self.assertEqual(model.receipt_value["after_version"]["revision"],
                         model.receipt_value["before_version"]["revision"] + 1)
        self.assertEqual(initial["state"], other_initial["state"])
        self.assertEqual(initial["state"]["app"]["mj_clock"], 2048)
        self.assertEqual(initial["state"]["sound_effects"]["app_update_count"], 2048)
        recipe = initial["initialization"]
        self.assertEqual(recipe[b0.METHOD]["entries"], TABLE)
        self.assertEqual(recipe[b0.METHOD]["configuration"], b0.SPEC)
        self.assertNotEqual(model.receipt_value["before_state"], other.receipt_value["before_state"])
        trajectory = build_trajectory(first / "trace.jsonl", first / "audit", first / "bundle")
        for tick in (None, 0, 2):
            with self.subTest(tick=tick):
                result = replay(trajectory, self.initializer(), self.root / f"actual-{tick}", target_tick=tick)
                self.assertTrue(result["equal"])
                report = result["b0_normalization"]
                self.assertTrue(report["receipt_compared"])
                self.assertEqual(report["source_before"], [1295, 1307])
                # The cold world restores the recorded post-normalization
                # boundary, so its real pre-value equals the target while the
                # source world's own pre-value does not.
                self.assertEqual(report["actual_before"], [1400, 2048])
                self.assertEqual(report["common_targets"], [2048, 2048])
                self.assertEqual(report["source_after"], report["actual_after"])
                self.assertEqual(report["declared_field_set"], list(b0.FIELD_NAMES))
                self.assertFalse(report["subsequent_state_normalized"])
                self.assertFalse(report["before_values_compared"])
                self.assertTrue(report["before_values_recorded"])

    def test_declared_capability_requires_a_declared_nonempty_table(self):
        for index, (table, message) in enumerate(((None, "declare the fixed common target"),
                                                 ([], "an empty table"),
                                                 ([TABLE[0]], "did not declare"))):
            directory = self.root / f"fail-{index}"
            model = NormalizationTransport(directory / "audit", zero_tick=None)
            with SessionTrace(directory / "trace.jsonl") as trace, Client(model, trace=trace) as client:
                with self.subTest(table=table), self.assertRaisesRegex(RuntimeError, message):
                    apply_recipe(client, 42, b0_normalization=copy.deepcopy(table) if table is not None else None)
                self.assertFalse(any(row["method"] in {b0.METHOD, "rng_seed", "clock_restore"}
                                     for row in model.requests))

    def test_out_of_range_null_and_outside_field_tables_fail_before_any_request(self):
        outside = {"field": "/board/00005568", "target": 1, "reason": "not a declared leaf"}
        bad = [
            [dict(TABLE[0], target=None)],
            [dict(TABLE[0], target=True)],
            [dict(TABLE[0], target=2147483648)],
            [dict(TABLE[0], target=1.0)],
            [TABLE[0], TABLE[0]],
            list(reversed(TABLE)),
            [dict(TABLE[0], reason="")],
            [dict(TABLE[0], reason="x" * 201)],
            [dict(TABLE[0], reason="line\nbreak")],
            [dict(TABLE[0], extra=True)],
            [outside, TABLE[0]],
        ]
        model = NormalizationTransport(self.root / "limited" / "audit", zero_tick=None)
        with SessionTrace(self.root / "limited" / "trace.jsonl") as trace, Client(model, trace=trace) as client:
            for table in bad:
                with self.subTest(table=json.dumps(table, ensure_ascii=False)[:60]):
                    with self.assertRaises((EvidenceError, RuntimeError, ValueError)):
                        apply_recipe(client, 42, b0_normalization=copy.deepcopy(table))
            self.assertFalse(any(row["method"] == b0.METHOD for row in model.requests))

    def test_receipt_rejects_tampering_and_a_warm_that_is_not_the_next_revision(self):
        _, model, initial = self.source()
        for fault in ("readback", "changed_outside", "rng_after"):
            directory = self.root / f"fault-{fault}"
            with self.subTest(fault=fault), self.assertRaises(EvidenceError):
                self.source(fault, b0_fault=fault)
        with self.subTest(fault="extra_revision"), self.assertRaises(RuntimeError):
            self.source("extra-revision", b0_fault="extra_revision")
        for fault in ("missing_event", "duplicate_event", "native_failed"):
            with self.subTest(fault=fault), self.assertRaises(EvidenceError):
                directory, _, _ = self.source(f"event-{fault}", b0_fault=fault)
                build_trajectory(directory / "trace.jsonl", directory / "audit", directory / "bundle")
        receipt = copy.deepcopy(model.receipt_value)
        for edit in ("after", "after_state", "before", "version", "shape", "requested", "outside"):
            value = copy.deepcopy(receipt)
            if edit == "after":
                value["after"][0]["value"] += 1
            elif edit == "after_state":
                value["after_state"]["board"]["hidden"] += 1
            elif edit == "before":
                value["before"][0]["value"] = 0
            elif edit == "version":
                value["after_version"]["revision"] += 1
            elif edit == "shape":
                value["mode"] = "other"
            elif edit == "requested":
                value["requested"] = [dict(TABLE[0], target=2049), TABLE[1]]
            else:
                value["after_state"]["app"]["ui"] = 2
            with self.subTest(edit=edit), self.assertRaises(EvidenceError):
                b0.receipt(value, model.game, requested=TABLE, seed=42)
        self.assertEqual(b0.receipt(copy.deepcopy(receipt), model.game, requested=TABLE, seed=42), receipt)
        # The marker itself refuses a missing table or an unconfirmed observation.
        for edit in ("missing", "observation"):
            value = copy.deepcopy(initial)
            if edit == "missing":
                value["initialization"].pop(b0.METHOD)
            else:
                value["observation"]["b0_normalized"] = False
            with self.subTest(edit=edit), self.assertRaises(EvidenceError):
                _validate_initial(value)

    def test_plan_v1_scalars_map_to_entries_and_mixing_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "plan.json"
            path.write_text(json.dumps({"app_update_count": 1500, "mj_clock": 2048}))
            legacy = ev.Plan.load(path)
            self.assertEqual(legacy.b0_normalization, (
                {"field": b0.APP_UPDATE_FIELD, "target": 1500, "reason": "legacy Plan.app_update_count"},
                {"field": b0.MJ_CLOCK_FIELD, "target": 2048, "reason": "legacy Plan.mj_clock"}))
            path.write_text(json.dumps({"schema": "lvz.evaluation-plan.v1", "mj_clock": None}))
            undeclared = ev.Plan.load(path)
            self.assertEqual(undeclared.b0_normalization, ())
            path.write_text(json.dumps({"app_update_count": None, "mj_clock": None}))
            self.assertEqual(ev.Plan.load(path).b0_normalization, ())
            path.write_text(json.dumps({"schema": "lvz.evaluation-plan.v1", "mj_clock": 1,
                                        "b0_normalization": [TABLE[1]]}))
            with self.assertRaisesRegex(ValueError, "v1 plan"):
                ev.Plan.load(path)
            path.write_text(json.dumps({"schema": "lvz.evaluation-plan.v2", "mj_clock": 1,
                                        "b0_normalization": [TABLE[1]]}))
            with self.assertRaisesRegex(ValueError, "cannot mix"):
                ev.Plan.load(path)
            path.write_text(json.dumps({"schema": "lvz.evaluation-plan.v2",
                                        "b0_normalization": [TABLE[1]]}))
            self.assertEqual(ev.Plan.load(path).b0_normalization, (TABLE[1],))
            for bad in ([dict(TABLE[1], target=-1)], ["not an entry"], [dict(TABLE[1], target=0x80000000)]):
                path.write_text(json.dumps({"schema": "lvz.evaluation-plan.v2", "b0_normalization": bad}))
                with self.subTest(bad=bad), self.assertRaises(EvidenceError):
                    ev.Plan.load(path)

    def test_plan_command_accepts_the_unified_table_and_keeps_the_aliases(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "plan.json"
            self.assertEqual(ev.main(["plan", str(path), "--audio-mode", "sound_effects_allocation_none_v1",
                                      "--b0-normalize", json.dumps(TABLE[0], ensure_ascii=False),
                                      "--b0-normalize", json.dumps(TABLE[1], ensure_ascii=False)]), 0)
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(value["schema"], "lvz.evaluation-plan.v2")
            self.assertEqual(value["b0_normalization"], TABLE)
            self.assertNotIn("mj_clock", value)
            path.unlink()
            self.assertEqual(ev.main(["plan", str(path), "--mj-clock", "2048"]), 0)
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(value["b0_normalization"], [
                {"field": b0.MJ_CLOCK_FIELD, "target": 2048, "reason": "legacy flag --mj-clock"}])
            path.unlink()
            self.assertEqual(ev.main(["plan", str(path), "--app-update-count", "1500", "--mj-clock", "2048"]), 0)
            self.assertEqual([item["field"] for item in json.loads(path.read_text(encoding="utf-8"))["b0_normalization"]],
                             [b0.APP_UPDATE_FIELD, b0.MJ_CLOCK_FIELD])
            path.unlink()
            with self.assertRaises(SystemExit):
                ev.main(["plan", str(path), "--mj-clock", "2048",
                         "--b0-normalize", json.dumps(TABLE[1], ensure_ascii=False)])
            self.assertFalse(path.exists())

    def test_legacy_anchor_arguments_still_work_and_cannot_mix_with_a_table(self):
        directory = self.root / "legacy"
        model = NormalizationTransport(directory / "audit", zero_tick=None, b0_mode=False, anchored_mode=True,
                                       app_count=1362)
        with SessionTrace(directory / "trace.jsonl") as trace, Client(model, trace=trace) as client:
            recipe = apply_recipe(client, 42, app_update_count=1362)
            self.assertIn("app_update_anchor", recipe)
            self.assertNotIn(b0.METHOD, recipe)
            self.assertEqual(b0.recipe_entries(recipe), [
                {"field": b0.APP_UPDATE_FIELD, "target": 1362,
                 "reason": "legacy app_update_anchor recipe block"}])
        with SessionTrace(self.root / "mix" / "trace.jsonl") as trace, Client(
                NormalizationTransport(self.root / "mix" / "audit", zero_tick=None), trace=trace) as client:
            with self.assertRaises(ValueError):
                apply_recipe(client, 42, app_update_count=1295, b0_normalization=copy.deepcopy(TABLE))

    def test_legacy_archive_is_not_upgraded_by_the_unified_reader(self):
        source, _, initial = self.source("legacy-source", legacy=True, table=None, b0_mode=False,
                                         anchored_mode=True, app_count=1362)
        # A legacy source cannot be read as unified, and a unified reader cannot
        # reinterpret its manifest.
        recipe = initial["initialization"]
        self.assertIn("app_update_anchor", recipe)
        self.assertIsNone(b0.requested_from_recipe(recipe))
        self.assertIsNone(b0.mode(read_json(Path(source) / "audit" / "manifest.json")))
        # The old readers keep working on the same archive.
        from llm_vs_zombies import app_update_anchor
        self.assertEqual(app_update_anchor.target_from_recipe(recipe), 1362)
        # Both shapes at once are rejected, never merged.
        mixed = dict(recipe, **{b0.METHOD: {"configuration": copy.deepcopy(b0.SPEC), "entries": TABLE}})
        self.assertTrue(b0.mixed_shapes(read_json(Path(source) / "audit" / "manifest.json"), mixed))

    def test_real_capability_combination_initializes_and_replays_the_table_alone(self):
        """The failing production path: one DLL declares all three B(0) shapes."""
        source, model, initial = self.source("capability", transport=UnifiedWithLegacyCapabilitiesTransport,
                                             app_count=1295, mj=1307)
        manifest = read_json(Path(source) / "audit" / "manifest.json")
        for key in (app_update_anchor.METHOD, mj_clock_anchor.METHOD, b0.METHOD):
            self.assertIn(key, manifest)
        # Initialization accepts the request and sends the table, not the anchors.
        legacy_calls = [row["method"] for row in model.requests
                        if row["method"] in {app_update_anchor.METHOD, mj_clock_anchor.METHOD}]
        self.assertEqual(legacy_calls, [])
        self.assertEqual([row["method"] for row in model.requests if row["method"] == b0.METHOD], [b0.METHOD])
        self.assertEqual(initial["initialization"][b0.METHOD]["entries"], TABLE)
        self.assertTrue(initial["observation"]["b0_normalized"])
        # Archive reading: the legacy readers stay out of the unified archive.
        log = AuditLog(Path(source) / "audit")
        self.assertIsNone(log.app_anchor_receipt)
        self.assertIsNone(log.mj_clock_receipt)
        self.assertEqual([{"field": item["field"], "value": item["value"]}
                          for item in log.b0_normalization_receipt["after"]],
                         [{"field": item["field"], "value": item["target"]} for item in TABLE])
        # Replay compares the table and reports the declared anchors as superseded.
        trajectory = build_trajectory(Path(source) / "trace.jsonl", Path(source) / "audit", Path(source) / "bundle")
        result = replay(trajectory, self.initializer(transport=UnifiedWithLegacyCapabilitiesTransport),
                        self.root / "capability-replay")
        self.assertTrue(result["equal"])
        self.assertTrue(result["b0_normalization"]["receipt_compared"])
        self.assertEqual(result["b0_normalization"]["common_targets"], [item["target"] for item in TABLE])
        for name in ("app_update_anchor", "mj_clock_anchor"):
            self.assertEqual(result[name]["mode"], f"superseded_by_{b0.MODE}")
            self.assertFalse(result[name]["receipt_compared"])
            self.assertFalse(result[name]["subsequent_state_normalized"])
            self.assertIsNone(result[name]["target_policy"])
        self.assertEqual([row["method"] for row in self.actual.requests
                          if row["method"] in {app_update_anchor.METHOD, mj_clock_anchor.METHOD}], [])

    def test_archive_reading_keeps_the_legacy_rules_only_without_the_table(self):
        unified_source, _, unified_initial = self.source("read-unified", transport=UnifiedWithLegacyCapabilitiesTransport)
        legacy_source, _, legacy_initial = self.source("read-legacy", legacy=True, table=None, b0_mode=False,
                                                       anchored_mode=True, app_count=1362)
        unified = read_json(Path(unified_source) / "audit" / "manifest.json")
        legacy = read_json(Path(legacy_source) / "audit" / "manifest.json")
        # The one criterion: the declared unified mode owns the two fields.
        self.assertTrue(b0.unified_owns_fields(unified))
        self.assertFalse(b0.unified_owns_fields(legacy))
        self.assertEqual(app_update_anchor.mode(unified), app_update_anchor.MODE)
        self.assertEqual(mj_clock_anchor.mode(unified), mj_clock_anchor.MODE)
        self.assertFalse(app_update_anchor.Evidence(unified).enabled)
        self.assertFalse(mj_clock_anchor.Evidence(unified).enabled)
        self.assertTrue(app_update_anchor.Evidence(legacy).enabled)
        _validate_initial(copy.deepcopy(unified_initial))
        _validate_initial(copy.deepcopy(legacy_initial))
        # The table is still required where it is declared ...
        without_table = copy.deepcopy(unified_initial)
        without_table["initialization"].pop(b0.METHOD)
        with self.assertRaisesRegex(EvidenceError, "explicitly declared nonempty B\\(0\\) normalization"):
            _validate_initial(without_table)
        # ... and the legacy rules still fail closed without it.
        for edit, message in (("target", "fixed initial target"), ("observation", "does not confirm")):
            broken = copy.deepcopy(legacy_initial)
            if edit == "target":
                broken["initialization"].pop(app_update_anchor.METHOD)
            else:
                broken["observation"]["app_update_anchored"] = False
            with self.subTest(edit=edit), self.assertRaisesRegex(EvidenceError, message):
                _validate_initial(broken)
        # A recipe that really carries both shapes stays a mix, never a merge.
        for method, block in ((app_update_anchor.METHOD, {"configuration": copy.deepcopy(app_update_anchor.SPEC),
                                                          "app_update_count": 2048}),
                              (mj_clock_anchor.METHOD, {"configuration": copy.deepcopy(mj_clock_anchor.SPEC),
                                                        "mj_clock": 2048})):
            mixed = copy.deepcopy(unified_initial)
            mixed["initialization"][method] = block
            self.assertTrue(b0.mixed_shapes(unified, mixed["initialization"]))
            with self.subTest(method=method), self.assertRaisesRegex(EvidenceError, "cannot mix"):
                _validate_initial(mixed)
        # A capability list alone never proves a mix.
        self.assertFalse(b0.mixed_shapes(unified, unified_initial["initialization"]))
        self.assertFalse(b0.mixed_shapes(legacy, legacy_initial["initialization"]))

    def test_a_legacy_anchor_event_beside_the_table_is_still_rejected(self):
        unified = UnifiedWithLegacyCapabilitiesTransport(self.root / "event-unified" / "audit", zero_tick=None)
        legacy = NormalizationTransport(self.root / "event-legacy" / "audit", zero_tick=None, b0_mode=False,
                                        anchored_mode=True)
        for kind in (app_update_anchor.EVENT, mj_clock_anchor.EVENT):
            with self.subTest(kind=kind), self.assertRaisesRegex(EvidenceError, "cannot mix"):
                b0.Evidence(unified.game).event({"kind": kind, "payload": {}, "version": {}})
            b0.Evidence(legacy.game).event({"kind": kind, "payload": {}, "version": {}})

    def test_reason_text_never_enters_the_root_identity(self):
        _, _, initial = self.source()
        recipe = initial["initialization"]
        other = copy.deepcopy(recipe)
        other[b0.METHOD]["entries"][0]["reason"] = "同一目标，换一种说法"
        self.assertEqual(root_identity(game=ASSETS, artifacts={"input_hashes": {"a": "b" * 64}},
                                       init_recipe=recipe),
                         root_identity(game=ASSETS, artifacts={"input_hashes": {"a": "b" * 64}},
                                       init_recipe=other))
        self.assertEqual(root_identity(game=ASSETS, artifacts={"input_hashes": {"a": "b" * 64}},
                                       init_recipe=recipe)["init_recipe"][b0.METHOD]["entries"],
                         [[b0.APP_UPDATE_FIELD, 2048], [b0.MJ_CLOCK_FIELD, 2048]])
        # Projection is idempotent, so an identity can be normalized twice.
        once = normalize_identity(root_identity(game=ASSETS, artifacts={"input_hashes": {"a": "b" * 64}},
                                                init_recipe=recipe))
        self.assertEqual(normalize_identity(once), once)
        changed = copy.deepcopy(recipe)
        changed[b0.METHOD]["entries"][1]["target"] = 2049
        self.assertNotEqual(root_identity(game=ASSETS, artifacts={"input_hashes": {"a": "b" * 64}},
                                          init_recipe=recipe),
                            root_identity(game=ASSETS, artifacts={"input_hashes": {"a": "b" * 64}},
                                          init_recipe=changed))


if __name__ == "__main__":
    unittest.main()
