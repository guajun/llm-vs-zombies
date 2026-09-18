import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch as mock_patch

from llm_vs_zombies.audit_compare import (AuditLog, EvidenceError, SCHEMA, digests,
    _FrameDecoder, _fnv, _python_fnv, canonical, digest, first_difference, patch)


class AuditTests(unittest.TestCase):
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
