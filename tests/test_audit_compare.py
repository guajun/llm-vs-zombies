import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch as mock_patch

from llm_vs_zombies.audit_compare import (AuditLog, AuditTail, EvidenceError, SCHEMA, digests,
    _FrameDecoder, _fnv, _python_fnv, canonical, compare_audits, digest, first_difference, patch)


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


class AuditTests(unittest.TestCase):
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
