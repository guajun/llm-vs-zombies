import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch as mock_patch

from llm_vs_zombies.audit_compare import (AuditLog, AuditTail, EvidenceError, SCHEMA, digests,
    _FrameDecoder, _fnv, _python_fnv, canonical, compare_audits, digest, first_difference, patch,
    PARTICLE_SHAKE_MODE, _tokens, _cached_tokens, _POINTER_CACHE_SIZE, _POINTER_CACHE_MAX_CHARS, decode,
    EventStream, _fnv_continue)


ANIMATION_COVERAGE = {"schema": "lvz.reanimation-links.v1", "normalize_scope": "semantic identity",
    "raw_handle_evidence": {"path": "audit/reanimation-handles.jsonl", "encoding": "initial_plus_json_patch",
                            "binding": ["seq", "kind", "version"], "required": True}}


def animation_audit(directory, handle=65538):
    directory.mkdir()
    reference = {"status": "live", "node": "board/test#0"}
    raw = {"schema": "lvz.reanimation-raw.v1",
           "pool": {"capacity": 4, "used": 3, "count": 1, "free_head": 3, "next_key": handle >> 16},
           "actual_slot_ids": {"2": handle}, "links": [{"path": "/board/animation", "anchor": "board/test",
               "raw_handle": handle, "slot": 2, "owner_dead": False, "lookup_matches": True,
               "actual_slot_id": handle, "logical_node": "board/test#0", "normalized_reference": reference}]}
    state = {"schema": SCHEMA, "board": {"tick": 0, "animation": reference},
             "reanimations": {"schema": "lvz.reanimation-links.v1", "valid": True, "issues": [],
                 "nodes": {"board/test#0": {"state": {"type": 1}, "owners": ["board/test"]}}}}
    manifest = {"schema": SCHEMA, "target": "synthetic", "loaded_signatures_match": True,
                "coverage": {"reanimations": ANIMATION_COVERAGE}}
    (directory / "manifest.json").write_text(json.dumps(manifest))
    records = {name: [] for name in ("checksums.jsonl", "state-deltas.jsonl", "reanimation-handles.jsonl", "events.jsonl")}
    for index in range(2):
        state["board"]["tick"] = index
        envelope = {"schema": SCHEMA, "seq": index, "kind": "pre_step" if index == 0 else "post_step",
            "version": {"epoch": 1, "tick": index, "revision": 0},
            "payload": {"request_id": "a", **({"native_tick_delta": 1} if index else {})}}
        records["checksums.jsonl"].append(dict(envelope, digests=digests(state)))
        records["state-deltas.jsonl"].append(dict(envelope, **({"initial": copy.deepcopy(state)} if index == 0 else
            {"patch": [{"op": "replace", "path": "/board/tick", "value": index}]})))
        records["reanimation-handles.jsonl"].append(dict(envelope, **({"initial": raw} if index == 0 else
            {"patch": [{"op": "replace", "path": "/pool/next_key", "value": (handle >> 16) + 1}]})))
    records["events.jsonl"].append({"schema": SCHEMA, "seq": 2, "kind": "recording_closed",
        "version": {"epoch": 1, "tick": 1, "revision": 0}, "payload": {}})
    for name, rows in records.items():
        (directory / name).write_text("".join(json.dumps(row) + "\n" for row in rows))
    return records


def spawn_audit(directory, *, at="update", tick=0, initialization=False, count=1):
    rows = animation_audit(directory)
    offset = 0 if at == "action" else (1 if at == "update" else 2)
    for name in ("checksums.jsonl", "state-deltas.jsonl", "reanimation-handles.jsonl", "events.jsonl"):
        for row in rows[name]:
            if row["seq"] >= offset:
                row["seq"] += count
    events = []
    for ordinal in range(count):
        events.append({"schema": SCHEMA, "seq": offset + ordinal, "kind": "zombie_initialized",
            "phase": "initialization" if initialization else "controlled_boundary", "native_phase": "zombie_initialize_exit",
            "version": None if initialization else {"epoch": 1, "tick": tick, "revision": 0},
            "payload": {"schema": "lvz.spawn.v1", "kind": "zombie_initialized", "phase": "zombie_initialize_exit",
                "ordinal": ordinal, "boundary": None if initialization else {"segment": 1, "tick": tick, "revision": 0}}})
    rows["events.jsonl"] = events + rows["events.jsonl"]
    for name, values in rows.items():
        (directory / name).write_text("".join(json.dumps(row) + "\n" for row in values))
    return rows


def plant_animation_audit(directory, *, dead=0, squished=1, valid=False, rules=True):
    records = animation_audit(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    if rules:
        manifest["coverage"]["reanimations"]["owner_retirement_rules"] = ["owner_dead", "plant_squished_remove_effects"]
    (directory / "manifest.json").write_text(json.dumps(manifest))
    state = records["state-deltas.jsonl"][0]["initial"]
    state["board"].pop("animation")
    anchor = "plants/00010041/body"
    reference = {"status": "live", "node": anchor + "#0"} if valid else {"status": "expired"}
    state["plants"] = {"slots": {"65": {"id_or_free_next": 65601,
        "fields": {"00000141": dead, "00000142": squished, "00000094": reference}}}}
    state["reanimations"]["nodes"] = {anchor + "#0": {"state": {"type": 1}, "owners": [anchor]}} if valid else {}
    raw = records["reanimation-handles.jsonl"][0]["initial"]
    link = raw["links"][0]
    link.update(path="/plants/slots/65/fields/00000094", anchor=anchor, owner_dead=bool(dead),
                lookup_matches=valid, actual_slot_id=65538 if valid else None, normalized_reference=reference)
    if rules:
        link["owner_squished"] = bool(squished)
    if valid:
        link["logical_node"] = anchor + "#0"
    else:
        link.pop("logical_node")
        link["lookup_failure"] = "not_allocated"
        raw["pool"]["count"] = 0
        raw["actual_slot_ids"] = {}
        if rules:
            link["retirement_reason"] = "owner_dead" if dead else "plant_squished_remove_effects"
    for index in range(2):
        actual = copy.deepcopy(state)
        actual["board"]["tick"] = index
        records["checksums.jsonl"][index]["digests"] = digests(actual)
    for name, rows in records.items():
        (directory / name).write_text("".join(json.dumps(row) + "\n" for row in rows))
    return records


PARTICLE_MODE = {"mode": PARTICLE_SHAKE_MODE, "installed": True, "original_engine_bitwise_unmodified": False,
                 "semantic_change": "test verified ID seed substitution", "raw_evidence": "particle-shake-seeds.jsonl"}


def particle_audit(directory, pointer=0x10000140, identity=65538, *, age=5, site=0x116ba5, crossfade=0):
    directory.mkdir()
    (directory / "manifest.json").write_text(json.dumps({"schema": SCHEMA, "target": "synthetic",
        "loaded_signatures_match": True, "particle_shake": PARTICLE_MODE}))
    factor = (19 if age == 0 else age - 1) if site == 0x116b3c else age
    payload = {"mode": PARTICLE_SHAKE_MODE, "ordinal": 0, "callsite_rva": site,
        "particle_id": identity, "slot": 2, "generation": identity >> 16, "age": age, "duration": 20, "crossfade_duration": crossfade,
        "factor": factor, "canonical_seed": (identity * factor) & 0xffffffff, "pool_verified": True,
        "control_phase": "pre_step", "pool": {"used": 3, "capacity": 4, "count": 1, "free_head": 3, "next_key": 2}}
    event = {"schema": SCHEMA, "seq": 1, "kind": "particle_shake_seed", "phase": "controlled_boundary",
        "native_phase": "before_srand", "version": {"epoch": 1, "tick": 0, "revision": 0}, "payload": payload}
    raw = dict(event, schema="lvz.particle-shake-raw.v1", payload=dict(payload,
        particle_address=pointer, emitter_address=100, system_address=200, holder_address=300,
        pool_block_address=pointer - 2 * 0xa0, original_seed=(pointer * factor) & 0xffffffff))
    words = [site, identity, factor, payload["canonical_seed"], age, 20, crossfade, 3, 4, 3, 1, 2]
    rolling = _python_fnv(b"".join(word.to_bytes(8, "little") for word in words))
    records = {"events.jsonl": [event], "particle-shake-seeds.jsonl": [raw], "checksums.jsonl": [], "state-deltas.jsonl": []}
    for index in range(2):
        state = {"schema": SCHEMA, "board": {"tick": index}, "particle_shake": {"mode": PARTICLE_SHAKE_MODE,
            "controlled_calls": index, "controlled_digest": rolling if index else 14695981039346656037}}
        envelope = {"schema": SCHEMA, "seq": index * 2, "kind": "post_step" if index else "pre_step",
            "version": {"epoch": 1, "tick": index, "revision": 0},
            "payload": {"request_id": "a", **({"native_tick_delta": 1} if index else {})}}
        records["checksums.jsonl"].append(dict(envelope, digests=digests(state)))
        records["state-deltas.jsonl"].append(dict(envelope, **({"initial": state} if not index else
            {"patch": [{"op": "replace", "path": "", "value": state}]})))
    records["events.jsonl"] += [
        {"schema": SCHEMA, "seq": 3, "kind": "recording_closed", "version": {"epoch": 1, "tick": 1, "revision": 0}, "payload": {}},
        {"schema": SCHEMA, "seq": 4, "kind": "particle_shake_closed", "version": {"epoch": 1, "tick": 1, "revision": 0},
         "payload": {"installed": True, "captured": 1, "controlled_calls": 1, "queued": 0,
                     "wrong_thread_calls": 0, "faults": 0, "overflow": 0, "healthy": True}}]
    for name, rows in records.items():
        (directory / name).write_text("".join(json.dumps(row) + "\n" for row in rows))
    return records


class AuditTests(unittest.TestCase):
    def test_controlled_births_bind_to_update_or_next_action_boundary(self):
        for phase, expected in (("update", [0, 1]), ("action", [1, 0])):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp) / "audit"
                spawn_audit(directory, at=phase)
                audit = AuditLog(directory, require_closed=True)
                self.assertEqual([len(frame.spawn_events) for frame in audit.frames], expected)
                self.assertEqual(audit.birth_counts, {"controlled": 1, "initialization": 0})
                tail = AuditTail(directory)
                self.assertEqual([len(frame.spawn_events) for frame in tail.read_request("a")], expected)
                tail.verify_closed()

    def test_spawn_wrong_or_missing_boundary_and_live_queue_overflow_fail_closed(self):
        cases = (({"tick": 8}, "actual pre/post"),
                 ({"at": "action", "tick": 8}, "next audited pre-step"),
                 ({"at": "after", "tick": 1}, "no following audited boundary"),
                 ({"initialization": True}, "lost its controlled boundary"))
        for options, reason in cases:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp) / "audit"
                spawn_audit(directory, **options)
                with self.assertRaisesRegex(EvidenceError, reason):
                    AuditLog(directory, require_closed=True)
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "audit"
            spawn_audit(directory, count=2)
            with mock_patch("llm_vs_zombies.audit_compare._SPAWN_BOUNDARY_LIMIT", 1):
                with self.assertRaisesRegex(EvidenceError, "reader bound"):
                    list(AuditTail(directory).read_request("a"))

    def test_continued_fnv_matches_independent_reference_across_chunks_and_seeds(self):
        data = bytes(range(256)) * 3 + b"\x00\xfflast"
        for seed in (0, 1, 14695981039346656037, 0xffffffffffffffff):
            expected = _python_fnv(data, seed)
            actual = seed
            for start in range(0, len(data), 17):
                actual = _fnv_continue(actual, data[start:start + 17])
            self.assertEqual(actual, expected)
            with mock_patch("llm_vs_zombies.audit_compare._hash_continue", None):
                self.assertEqual(_fnv_continue(seed, data), expected)

    def test_explicit_hook_fault_invalidates_otherwise_complete_closed_evidence(self):
        for kind in ("reanimation_link_fault", "spawn_hook_fault", "particle_shake_fault"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp) / "audit"
                rows = animation_audit(directory)["events.jsonl"]
                close = rows[0]
                fault = dict(close, kind=kind)
                close["seq"] += 1
                (directory / "events.jsonl").write_text(json.dumps(fault) + "\n" + json.dumps(close) + "\n")
                with self.assertRaisesRegex(EvidenceError, "hook fault"):
                    AuditLog(directory, require_closed=True)
                with self.assertRaisesRegex(EvidenceError, "hook fault"):
                    list(AuditTail(directory).read_request("a"))

    def test_event_stream_is_repeatable_but_rejects_replacement_truncation_and_same_size_edit(self):
        for change in ("replace", "truncate", "same_size"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "events.jsonl"
                path.write_bytes(b'{"a":1}\n')
                records = EventStream(path)
                self.assertEqual(list(records), [{"a": 1}])
                self.assertEqual(list(records), [{"a": 1}])
                if change == "replace":
                    new = path.with_suffix(".new")
                    new.write_bytes(path.read_bytes())
                    os.replace(new, path)
                elif change == "truncate":
                    path.write_bytes(b"")
                else:
                    path.write_bytes(b'{"a":2}\n')
                    # A restored mtime cannot circumvent the byte binding.
                    stat = path.stat()
                    os.utime(path, ns=(stat.st_atime_ns, records.signature[3]))
                with self.assertRaises(EvidenceError):
                    list(records)

    def test_closed_frame_reread_revalidates_all_evidence_and_large_rows_are_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "audit"
            particle_audit(directory)
            audit = AuditLog(directory, require_closed=True)
            self.assertIsInstance(audit.events, EventStream)
            self.assertEqual(sum(event["kind"] == "particle_shake_seed" for event in audit.events), 1)
            self.assertTrue(all(event["kind"] != "particle_shake_seed" for event in audit.control_events))
            self.assertEqual(audit.peak_pending_particle_calls, 1)
            path = directory / "particle-shake-seeds.jsonl"
            path.write_text(path.read_text().replace('"particle_address": 268435776', '"particle_address": 268435777'))
            with self.assertRaises(EvidenceError):
                list(audit.frames)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            path.write_bytes(b'{"large": "123456789"}\n')
            with mock_patch("llm_vs_zombies.audit_compare._JSONL_MAX_RECORD_BYTES", 8):
                with self.assertRaisesRegex(EvidenceError, "reader bound"):
                    list(EventStream(path))

    def test_particle_window_limit_and_live_prefix_binding_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "audit"
            particle_audit(directory)
            with mock_patch("llm_vs_zombies.audit_compare._PARTICLE_BOUNDARY_LIMIT", 0):
                with self.assertRaisesRegex(EvidenceError, "reader bound"):
                    AuditLog(directory)
                with self.assertRaisesRegex(EvidenceError, "reader bound"):
                    list(AuditTail(directory).read_request("a"))
            tail = AuditTail(directory)
            self.assertEqual(len(list(tail.read_request("a"))), 2)
            self.assertFalse(isinstance(tail.events, list))
            self.assertEqual(tail.peak_pending_particle_calls, 1)
            self.assertEqual(sum(event["kind"] == "particle_shake_seed" for event in tail.events), 1)
            self.assertTrue(all(event["kind"] != "particle_shake_seed" for event in tail._control_events))
            tail.verify_closed()
            path = directory / "particle-shake-seeds.jsonl"
            data = path.read_bytes()
            path.write_bytes(data.replace(b'"emitter_address": 100', b'"emitter_address": 101'))
            with self.assertRaisesRegex(EvidenceError, "SHA-256"):
                tail.verify_closed()

    def test_squished_plant_expiration_is_verified_against_actual_field(self):
        for dead, squished, valid, rules in ((0, 1, False, True), (1, 1, False, True),
                                            (0, 1, True, True), (1, 0, False, False)):
            with self.subTest(dead=dead, squished=squished, valid=valid, rules=rules), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp) / "audit"
                plant_animation_audit(directory, dead=dead, squished=squished, valid=valid, rules=rules)
                self.assertEqual(len(AuditLog(directory, require_closed=True).frames), 2)
                self.assertEqual(len(list(AuditTail(directory).read_request("a"))), 2)

    def test_squish_does_not_allow_forged_flags_live_dangling_or_expired_valid_handle(self):
        mutations = {
            "forged_flag": lambda records: records["reanimation-handles.jsonl"][0]["initial"]["links"][0].update(owner_squished=False),
            "missing_flag": lambda records: records["reanimation-handles.jsonl"][0]["initial"]["links"][0].pop("owner_squished"),
            "forged_dead": lambda records: records["reanimation-handles.jsonl"][0]["initial"]["links"][0].update(owner_dead=True),
            "reason": lambda records: records["reanimation-handles.jsonl"][0]["initial"]["links"][0].update(retirement_reason="owner_dead"),
            "missing_reason": lambda records: records["reanimation-handles.jsonl"][0]["initial"]["links"][0].pop("retirement_reason"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp) / "audit"
                records = plant_animation_audit(directory)
                mutate(records)
                for name, rows in records.items():
                    (directory / name).write_text("".join(json.dumps(row) + "\n" for row in rows))
                with self.assertRaises(EvidenceError):
                    AuditLog(directory)
        for dead, squished, valid, rules in ((0, 0, False, True), (0, 1, False, False)):
            with self.subTest(squished=squished, rules=rules), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp) / "audit"
                plant_animation_audit(directory, dead=dead, squished=squished, valid=valid, rules=rules)
                with self.assertRaises(EvidenceError):
                    AuditLog(directory)
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "audit"
            records = plant_animation_audit(directory, valid=True)
            raw = records["reanimation-handles.jsonl"][0]["initial"]["links"][0]
            raw["normalized_reference"]["status"] = "expired"
            raw["retirement_reason"] = "plant_squished_remove_effects"
            path = directory / "reanimation-handles.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in records[path.name]))
            with self.assertRaises(EvidenceError):
                AuditLog(directory)

    def test_non_plant_cannot_claim_squished_retirement(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "audit"
            records = animation_audit(directory)
            records["reanimation-handles.jsonl"][0]["initial"]["links"][0]["owner_squished"] = True
            path = directory / "reanimation-handles.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in records[path.name]))
            with self.assertRaisesRegex(EvidenceError, "plant owner"):
                AuditLog(directory)

    def test_pointer_cache_is_immutable_bounded_and_retains_no_long_or_invalid_paths(self):
        _cached_tokens.cache_clear()
        tokens = _tokens("/a~1b/~0/0")
        self.assertEqual(tokens, ("a/b", "~", "0"))
        self.assertIs(tokens, _tokens("/a~1b/~0/0"))
        with self.assertRaises(TypeError):
            tokens[0] = "different"
        for value in (None, [], {}, 1, "bad", "/bad~", "/bad~2"):
            with self.subTest(value=value), self.assertRaises(EvidenceError):
                _tokens(value)
        self.assertEqual(_cached_tokens.cache_info().currsize, 1)
        long_path = "/" + "x" * _POINTER_CACHE_MAX_CHARS
        self.assertEqual(_tokens(long_path), (long_path[1:],))
        self.assertEqual(_cached_tokens.cache_info().currsize, 1)
        for index in range(_POINTER_CACHE_SIZE + 7):
            _tokens("/field/" + str(index))
        self.assertEqual(_cached_tokens.cache_info().currsize, _POINTER_CACHE_SIZE)
        _cached_tokens.cache_clear()

    def test_cached_pointer_does_not_cache_container_or_target_validity(self):
        _tokens("/items/1")
        self.assertEqual(patch({"items": [0, 1]}, [{"op": "replace", "path": "/items/1", "value": 9}]), {"items": [0, 9]})
        with self.assertRaisesRegex(EvidenceError, "out of bounds"):
            patch({"items": [0]}, [{"op": "replace", "path": "/items/1", "value": 9}])
        with self.assertRaises(EvidenceError):
            patch({"items": 1}, [{"op": "replace", "path": "/items/1", "value": 9}])
        _tokens("/x")
        with self.assertRaisesRegex(EvidenceError, "duplicate JSON key"):
            decode('{"patch":[{"op":"replace","path":"/x","path":"/y","value":1}]}')

    def test_particle_seed_raw_pointers_may_differ_but_identity_may_not(self):
        with tempfile.TemporaryDirectory() as temp:
            left, right, other = [Path(temp) / name for name in ("left", "right", "other")]
            particle_audit(left)
            particle_audit(right, pointer=0x20000140)
            particle_audit(other, identity=131074)
            source = AuditLog(left, require_closed=True)
            self.assertTrue(compare_audits(source, AuditLog(right, require_closed=True))["equal"])
            mismatch = compare_audits(source, AuditLog(other, require_closed=True))
            self.assertFalse(mismatch["equal"])
            self.assertEqual(mismatch["reason"], "particle_shake")
            tail = AuditTail(right)
            self.assertEqual(sum(len(frame.particle_seeds) for frame in tail.read_request("a")), 1)
            tail.verify_closed()

    def test_particle_previous_seed_wrap_factor_is_verified(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "audit"
            particle_audit(path, age=0, site=0x116b3c)
            self.assertEqual(len(AuditLog(path, require_closed=True).frames), 2)

    def test_particle_crossfade_may_preserve_age_beyond_normal_lifetime(self):
        with tempfile.TemporaryDirectory() as temp:
            path, invalid = Path(temp) / "audit", Path(temp) / "invalid"
            particle_audit(path, age=25, crossfade=3)
            self.assertEqual(len(AuditLog(path, require_closed=True).frames), 2)
            particle_audit(invalid, age=25)
            with self.assertRaisesRegex(EvidenceError, "crossfade"):
                AuditLog(invalid)

    def test_particle_mode_raw_evidence_health_and_semantics_fail_closed(self):
        mutations = {
            "missing_raw": lambda p, rows: (p / "particle-shake-seeds.jsonl").unlink(),
            "raw_count": lambda p, rows: rows["particle-shake-seeds.jsonl"].clear(),
            "raw_seq": lambda p, rows: rows["particle-shake-seeds.jsonl"][0].update(seq=2),
            "original_seed": lambda p, rows: rows["particle-shake-seeds.jsonl"][0]["payload"].update(original_seed=3),
            "ordinal": lambda p, rows: rows["particle-shake-seeds.jsonl"][0]["payload"].update(ordinal=1),
            "close_missing": lambda p, rows: rows["events.jsonl"].pop(),
            "overflow": lambda p, rows: rows["events.jsonl"][-1]["payload"].update(overflow=1),
            "close_count": lambda p, rows: rows["events.jsonl"][-1]["payload"].update(captured=2),
            "undeclared": lambda p, rows: (p / "manifest.json").write_text(json.dumps(
                {"schema": SCHEMA, "target": "synthetic", "loaded_signatures_match": True})),
            "declared_spawn_missing_close": lambda p, rows: (p / "manifest.json").write_text(json.dumps(
                dict(json.loads((p / "manifest.json").read_text()), spawn_hook={"installed": True}))),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp) / "audit"
                rows = particle_audit(directory)
                mutate(directory, rows)
                for name, values in rows.items():
                    if name != "particle-shake-seeds.jsonl" or label != "missing_raw":
                        (directory / name).write_text("".join(json.dumps(row) + "\n" for row in values))
                with self.assertRaises(EvidenceError):
                    AuditLog(directory, require_closed=True)

    def test_particle_digest_cannot_be_replaced_by_a_forged_state_counter(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "audit"
            rows = particle_audit(directory)
            state = rows["state-deltas.jsonl"][1]["patch"][0]["value"]
            state["particle_shake"]["controlled_digest"] = 0
            rows["checksums.jsonl"][1]["digests"] = digests(state)
            for name, values in rows.items():
                (directory / name).write_text("".join(json.dumps(row) + "\n" for row in values))
            with self.assertRaisesRegex(EvidenceError, "count/digest"):
                AuditLog(directory)

    def test_initialization_spawn_may_have_null_version_but_controlled_spawn_may_not(self):
        spawn = {"schema": "lvz.spawn.v1", "kind": "zombie_initialized", "phase": "zombie_initialize_exit",
                 "boundary": None, "ordinal": 0}
        record = {"schema": SCHEMA, "seq": 0, "kind": "zombie_initialized", "native_phase": "zombie_initialize_exit",
                  "phase": "initialization", "payload": spawn, "version": None}
        AuditLog._envelope(record)
        for change in ({"kind": "post_step"}, {"phase": "controlled_boundary"}, {"native_phase": "unverified"}):
            with self.subTest(change=change), self.assertRaises(EvidenceError):
                AuditLog._envelope(dict(record, **change))
        record.update(phase="controlled_boundary", version={"epoch": 4, "tick": 6, "revision": 0})
        spawn["boundary"] = {"segment": 4, "tick": 6, "revision": 0}
        AuditLog._envelope(record)
        spawn["boundary"]["tick"] = 7
        with self.assertRaisesRegex(EvidenceError, "boundary/version"):
            AuditLog._envelope(record)

    def test_healthy_spawn_shutdown_can_follow_recording_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "audit"
            animation_audit(directory)
            health = {"healthy": True, "captured": 0, "faults": 0, "overflow": 0,
                      "wrong_thread_calls": 0, "queued": 0, "active_initializers": 0}
            event = {"schema": SCHEMA, "seq": 3, "kind": "spawn_hook_closed", "payload": health,
                     "version": {"epoch": 1, "tick": 1, "revision": 0}}
            path = directory / "events.jsonl"
            before = path.read_text()
            path.write_text(before + json.dumps(event) + "\n")
            AuditLog(directory, require_closed=True)
            health["overflow"] = 1
            path.write_text(before + json.dumps(event) + "\n")
            with self.assertRaisesRegex(EvidenceError, "unhealthy or incomplete"):
                AuditLog(directory, require_closed=True)

    def test_raw_animation_handles_may_differ_while_semantics_match(self):
        with tempfile.TemporaryDirectory() as temp:
            left, right = Path(temp) / "left", Path(temp) / "right"
            animation_audit(left, 65538)
            animation_audit(right, 131074)
            self.assertTrue(compare_audits(AuditLog(left, require_closed=True), AuditLog(right, require_closed=True))["equal"])
            tail = AuditTail(left)
            self.assertEqual(len(list(tail.read_request("a"))), 2)
            self.assertEqual(tail._animation.state["pool"]["next_key"], 2)
            (left / "reanimation-handles.jsonl").write_bytes(b"")
            with self.assertRaisesRegex(EvidenceError, "truncated"):
                list(tail.read_request("a"))

    def test_animation_sidecar_required_by_descriptor_or_normalization_flag(self):
        for coverage in ({"reanimations": ANIMATION_COVERAGE}, {"animation_normalization": True}):
            with self.subTest(coverage=coverage), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp) / "audit"
                animation_audit(directory)
                manifest = json.loads((directory / "manifest.json").read_text())
                manifest["coverage"] = coverage
                (directory / "manifest.json").write_text(json.dumps(manifest))
                (directory / "reanimation-handles.jsonl").unlink()
                with self.assertRaisesRegex(EvidenceError, "required raw animation evidence"):
                    AuditLog(directory)

    def test_animation_sidecar_count_envelope_patch_and_lookup_corruption_fail_closed(self):
        mutations = {
            "missing": lambda rows: rows.pop(),
            "extra": lambda rows: rows.append(copy.deepcopy(rows[-1])),
            "seq": lambda rows: rows[1].update(seq=4),
            "version": lambda rows: rows[1]["version"].update(tick=2),
            "payload": lambda rows: rows[1]["payload"].update(request_id="different"),
            "patch": lambda rows: rows[1].update(patch=[{"op": "remove", "path": "/missing"}]),
            "lookup": lambda rows: rows[0]["initial"]["links"][0].update(actual_slot_id=131074),
            "reference": lambda rows: rows[0]["initial"]["links"][0]["normalized_reference"].update(node="other"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp) / "audit"
                rows = animation_audit(directory)["reanimation-handles.jsonl"]
                mutate(rows)
                (directory / "reanimation-handles.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
                with self.assertRaises(EvidenceError):
                    AuditLog(directory)
                with self.assertRaises(EvidenceError):
                    list(AuditTail(directory).read_request("a"))

    def test_old_and_new_birth_sequence_conventions_are_both_checked(self):
        for shared in (True, False):
            with self.subTest(shared=shared), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp) / "audit"
                records = animation_audit(directory)
                birth = {"schema": SCHEMA, "seq": 0 if shared else 1, "kind": "zombie_first_boundary_observed",
                         "version": {"epoch": 1, "tick": 0, "revision": 0}, "payload": {"id": 42}}
                if not shared:
                    for name in ("checksums.jsonl", "state-deltas.jsonl", "reanimation-handles.jsonl"):
                        records[name][1]["seq"] += 1
                    records["events.jsonl"][0]["seq"] += 1
                records["events.jsonl"].insert(0, birth)
                for name, rows in records.items():
                    (directory / name).write_text("".join(json.dumps(row) + "\n" for row in rows))
                self.assertEqual(len(AuditLog(directory).frames), 2)
                self.assertEqual(len(list(AuditTail(directory).read_request("a"))), 2)

    def test_fnv_matches_standard_ascii_test_vector(self):
        # JSON string encoding adds quotes; fixed independent FNV computation.
        self.assertEqual(digest({}), "08f44b07b5901a25")

    def test_accelerated_and_fallback_hashes_match_for_binary_and_unicode(self):
        for payload in (b"", bytes(range(256)) * 31, "雾夜两仪\x00🌻".encode("utf-8"), b"x" * 100003):
            expected = f"{_python_fnv(payload):016x}"
            self.assertEqual(_fnv(payload), expected)
            with mock_patch("llm_vs_zombies.audit_compare._hash_initialized", True), mock_patch("llm_vs_zombies.audit_compare._hash_native", None):
                self.assertEqual(_fnv(payload), expected)

    def test_incremental_component_encoding_still_verifies_every_digest(self):
        decoder = _FrameDecoder(reuse_state=True)
        state = {"schema": SCHEMA, "board": {"tick": 0}, "fixed": [1, 2, 3]}
        envelope = {"schema": SCHEMA, "seq": 0, "kind": "pre_step", "payload": {"request_id": "a"},
                    "version": {"epoch": 1, "tick": 0, "revision": 0}}
        frame = decoder.accept(dict(envelope, digests=digests(state)), dict(envelope, initial=state))
        self.assertEqual(frame.canonical_state, canonical(state))
        after = {"schema": SCHEMA, "board": {"tick": 1}, "fixed": [1, 2, 3]}
        envelope.update(seq=1, kind="post_step", payload={"request_id": "a", "native_tick_delta": 1},
                        version={"epoch": 1, "tick": 1, "revision": 0})
        frame = decoder.accept(dict(envelope, digests=digests(after)),
            dict(envelope, patch=[{"op": "replace", "path": "/board/tick", "value": 1}]))
        self.assertEqual(frame.canonical_state, canonical(after))
        envelope.update(seq=2, kind="pre_step", payload={"request_id": "a"})
        corrupted = digests(after)
        corrupted["fixed"] = "0" * 16
        with self.assertRaisesRegex(EvidenceError, "digest/schema"):
            decoder.accept(dict(envelope, digests=corrupted), dict(envelope, patch=[]))

    def test_stream_decoder_rejects_float_int_substitution_in_patch(self):
        decoder = _FrameDecoder(reuse_state=True)
        state = {"schema": SCHEMA, "board": {"tick": 0}}
        envelope = {"schema": SCHEMA, "seq": 0, "kind": "pre_step", "payload": {"request_id": "a"},
                    "version": {"epoch": 1, "tick": 0, "revision": 0}}
        decoder.accept(dict(envelope, digests=digests(state)), dict(envelope, initial=state))
        envelope.update(seq=1, kind="post_step", payload={"request_id": "a", "native_tick_delta": 1},
                        version={"epoch": 1, "tick": 1, "revision": 0})
        with self.assertRaisesRegex(EvidenceError, "integer bits"):
            decoder.accept(dict(envelope, digests=digests({"schema": SCHEMA, "board": {"tick": 1}})),
                dict(envelope, patch=[{"op": "replace", "path": "/board/tick", "value": 1.0}]))

    def test_patch_object_array_and_pointer_escaping(self):
        source = {"a/b": {"~key": [1, 2]}, "gone": True}
        result = patch(source, [{"op": "replace", "path": "/a~1b/~0key/0", "value": 3},
                                {"op": "add", "path": "/a~1b/~0key/-", "value": 4},
                                {"op": "remove", "path": "/gone"}])
        self.assertEqual(result, {"a/b": {"~key": [3, 2, 4]}})
        self.assertEqual(source["a/b"]["~key"], [1, 2])

    def test_patch_rejects_missing_targets_and_ambiguous_indices(self):
        for operation in [{"op": "replace", "path": "/missing", "value": 1},
                          {"op": "remove", "path": "/array/01"},
                          {"op": "add", "path": "/array/4", "value": 1},
                          {"op": "remove", "path": "/bad~2"},
                          {"op": "move", "path": "/array/0", "from": "/array/1"}]:
            with self.subTest(operation=operation), self.assertRaises(EvidenceError):
                patch({"array": [1, 2]}, [operation])

    def test_diagnostic_preserves_type_and_raw_bits(self):
        self.assertEqual(first_difference({"x": 0}, {"x": False})["reason"], "type")
        self.assertEqual(first_difference({"zombies": {"3": {"x": 0}}},
                                          {"zombies": {"3": {"x": 2147483648}}})["path"], "/zombies/3/x")
        with self.assertRaises(EvidenceError):
            digest({"float": 1.2})

    def test_audit_corrupt_delta_and_missing_step_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            manifest = {"schema": SCHEMA, "target": "test", "loaded_signatures_match": True}
            (directory / "manifest.json").write_text(json.dumps(manifest))
            (directory / "events.jsonl").write_text("")
            state = {"schema": SCHEMA, "board": {"tick": 0}}
            frame = {"schema": SCHEMA, "seq": 0, "kind": "pre_step", "payload": {"request_id": "a"},
                     "version": {"epoch": 1, "tick": 0, "revision": 0}}
            (directory / "checksums.jsonl").write_text(json.dumps(dict(frame, digests=digests(state))) + "\n")
            (directory / "state-deltas.jsonl").write_text(json.dumps(dict(frame, initial=state)) + "\n")
            with self.assertRaisesRegex(EvidenceError, "unfinished step"):
                AuditLog(directory)
            state["board"]["tick"] = 1
            (directory / "state-deltas.jsonl").write_text(json.dumps(dict(frame, initial=state)) + "\n")
            with self.assertRaisesRegex(EvidenceError, "digest/schema"):
                AuditLog(directory)


if __name__ == "__main__":
    unittest.main()
