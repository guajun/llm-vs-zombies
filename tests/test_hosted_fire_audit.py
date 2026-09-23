"""Hosted AvZ cannon shots read as audit-only evidence (issue #84, item 2).

`aCobManager.Fire` is a direct engine call, so it is not a journaled `action`
and never becomes one: `src/llm_vs_zombies/audit_compare.py` accepts it only as
`kind == "hosted_fire"` declared by `manifest.json`, bound to the next audited
`pre_step`, and counted with a digest in every boundary's comparable state.
These tests drive the strict reader with synthetic evidence in the same shape
the native writer produces; the C++ half of the proof is
tests/avz_hosted_fire_tests.cpp.
"""
from __future__ import annotations

import json
import struct
from pathlib import Path
import tempfile
import unittest

from llm_vs_zombies.audit_compare import (AUDIT_FILES, AuditLog, EvidenceError, HOSTED_FIRE_KIND,
    HOSTED_FIRE_MODE, SCHEMA, _hosted_fire_digest, audit_files, digests, hosted_fire_mode)


MODE = {"mode": HOSTED_FIRE_MODE, "installed": True, "kind": HOSTED_FIRE_KIND,
        "source": "hosted_avz_script",
        "hook": "avz_cob_manager._BasicFire after the reviewed AAsm::Fire call",
        "original_engine_bitwise_unmodified": False,
        "semantic_change": "none: audit-only; no engine call is added, removed or reordered",
        "boundary": "the shot is bound to the next audited pre_step (its version)"}

# (plant_index, plant_id, plant_row, plant_col, target_row, target_col, tick):
# the same three shots tests/avz_hosted_fire_tests.cpp drives through the real
# overlay of the pinned cob manager.
SHOTS = ((3, 65539, 1, 3, 2, 9.0, 40), (7, 131079, 4, 6, 5, 9.0, 40), (3, 65539, 1, 3, 4, 7.625, 41))


def fire_event(seq, shot):
    plant_index, plant_id, plant_row, plant_col, target_row, target_col, tick = shot
    bits = struct.unpack("<I", struct.pack("<f", target_col))[0]
    return {"schema": SCHEMA, "seq": seq, "kind": HOSTED_FIRE_KIND, "phase": "controlled_boundary",
        "native_phase": "avz_basic_fire", "version": {"epoch": 1, "tick": tick, "revision": 0},
        "payload": {"source": "hosted", "op": "fire", "plant_index": plant_index, "plant_id": plant_id,
            "plant_row": plant_row, "plant_col": plant_col, "target_row": target_row,
            "target_col_bits": bits, "target_col_text": f"{target_col:.3f}", "tick": tick}}


def rolling(shots):
    value = 14695981039346656037
    for shot in shots:
        value = _hosted_fire_digest(value, fire_event(0, shot)["payload"])
    return value


def fire_state(count, digest):
    return {"mode": HOSTED_FIRE_MODE, "count": count, "digest": digest}


def hosted_fire_audit(directory, *, shots=SHOTS[:2], stated=SHOTS[:2], declared=True,
                      ticks=None, trailing=False):
    """Builds a synthetic audit whose fires sit where the native writer puts them.

    Sequence per audited boundary: the shots the hook recorded before it, then
    the `pre_step`/`post_step` pair whose comparable state must count them. The
    `stated` argument lets a test state a different history from the one the
    records show, which is exactly the tampering the reader has to catch.
    """
    directory.mkdir()
    manifest = {"schema": SCHEMA, "target": "synthetic", "loaded_signatures_match": True}
    if declared:
        manifest["hosted_fire"] = MODE
    (directory / "manifest.json").write_text(json.dumps(manifest))
    groups = sorted(ticks) if ticks is not None else sorted({shot[6] for shot in shots})
    records = {"events.jsonl": [], "checksums.jsonl": [], "state-deltas.jsonl": []}
    sequence, seen = 0, 0
    for group in groups:
        for shot in [shot for shot in shots if shot[6] == group]:
            records["events.jsonl"].append(fire_event(sequence, shot))
            sequence += 1
        seen += len([shot for shot in stated if shot[6] == group])
        state = {"schema": SCHEMA, "board": {"tick": group}}
        if declared:
            state["hosted_fire"] = fire_state(seen, rolling(stated[:seen]))
        for boundary, tick in (("pre_step", group), ("post_step", group + 1)):
            envelope = {"schema": SCHEMA, "seq": sequence, "kind": boundary,
                "version": {"epoch": 1, "tick": tick, "revision": 0},
                "payload": {"request_id": "a", **({"native_tick_delta": 1} if boundary == "post_step" else {})}}
            records["checksums.jsonl"].append(dict(envelope, digests=digests(state)))
            patch = ({"initial": state} if not records["state-deltas.jsonl"] else
                     {"patch": [{"op": "replace", "path": "", "value": state}]})
            records["state-deltas.jsonl"].append(dict(envelope, **patch))
            sequence += 1
    if trailing:
        records["events.jsonl"].append(fire_event(sequence, SHOTS[2]))
        sequence += 1
    last = groups[-1] if groups else 40
    records["events.jsonl"].append({"schema": SCHEMA, "seq": sequence, "kind": "recording_closed",
        "version": {"epoch": 1, "tick": last + 1, "revision": 0}, "payload": {}})
    for name, rows in records.items():
        (directory / name).write_text("".join(json.dumps(row) + "\n" for row in rows))
    return records


class HostedFireAuditTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name) / "audit"

    def rewrite(self, directory, records, name):
        (directory / name).write_text("".join(json.dumps(row) + "\n" for row in records[name]))

    def state_of(self, records, index):
        row = records["state-deltas.jsonl"][index]
        return row["initial"] if "initial" in row else row["patch"][0]["value"]

    def rewrite_state(self, directory, records, index):
        """Writes a tampered frame with checksums that agree with it.

        The checksum/delta pair is verified before the hosted-fire bookkeeping
        runs, so a test that wants the latter to be the failing check has to
        keep the former consistent. A state object can be shared by the
        pre_step/post_step pair, so every frame's checksum is recomputed from
        the state it actually carries.
        """
        for frame in range(len(records["state-deltas.jsonl"])):
            records["checksums.jsonl"][frame]["digests"] = digests(self.state_of(records, frame))
        self.rewrite(directory, records, "checksums.jsonl")
        self.rewrite(directory, records, "state-deltas.jsonl")

    def events(self, log):
        return [event for event in log.control_events if event["kind"] == HOSTED_FIRE_KIND]

    def test_declared_records_are_visible_and_the_state_tracks_them(self):
        directory = self.fixture()
        hosted_fire_audit(directory, shots=SHOTS, stated=SHOTS)
        log = AuditLog(directory)
        fires = self.events(log)
        self.assertEqual([event["payload"]["tick"] for event in fires], [40, 40, 41])
        self.assertEqual([event["payload"]["plant_index"] for event in fires], [3, 7, 3])
        self.assertEqual([event["payload"]["plant_row"] for event in fires], [1, 4, 1])
        self.assertEqual([event["payload"]["target_row"] for event in fires], [2, 5, 4])
        self.assertEqual([event["payload"]["target_col_text"] for event in fires], ["9.000", "9.000", "7.625"])
        # Audit-only: a hosted shot is never a journaled action, so the reader
        # sees no `action` event and no request that could replay it.
        self.assertEqual([event for event in log.request_events(None) if event["kind"] == "action"], [])
        self.assertEqual([request for request in {event["payload"].get("request_id") for event in fires}], [None])
        states = [frame.state["hosted_fire"] for frame in log.frames]
        self.assertEqual([state["count"] for state in states], [2, 2, 3, 3])
        self.assertEqual([state["digest"] for state in states], [rolling(SHOTS[:2]), rolling(SHOTS[:2]),
                                                                 rolling(SHOTS), rolling(SHOTS)])
        self.assertTrue(all(state["mode"] == HOSTED_FIRE_MODE for state in states))

    def test_declared_mode_without_any_shot_is_valid(self):
        directory = self.fixture()
        hosted_fire_audit(directory, shots=(), stated=(), ticks=(40,))
        log = AuditLog(directory)
        self.assertEqual(self.events(log), [])
        self.assertEqual([frame.state["hosted_fire"]["count"] for frame in log.frames], [0, 0])

    def test_records_without_the_declared_mode_are_refused(self):
        directory = self.fixture()
        hosted_fire_audit(directory, declared=False)
        with self.assertRaisesRegex(EvidenceError, "declared audit mode"):
            AuditLog(directory)

    def test_state_component_without_the_declared_mode_is_refused(self):
        directory = self.fixture()
        records = hosted_fire_audit(directory, shots=(), stated=(), declared=False, ticks=(40,))
        self.state_of(records, -1)["hosted_fire"] = fire_state(1, rolling(SHOTS[:1]))
        self.rewrite_state(directory, records, -1)
        with self.assertRaisesRegex(EvidenceError, "explicit audit mode"):
            AuditLog(directory)

    def test_state_count_must_account_for_every_record(self):
        directory = self.fixture()
        records = hosted_fire_audit(directory, stated=SHOTS, shots=SHOTS)
        self.state_of(records, -1)["hosted_fire"] = fire_state(2, rolling(SHOTS[:2]))
        self.rewrite_state(directory, records, -1)
        with self.assertRaisesRegex(EvidenceError, "count/digest"):
            AuditLog(directory)

    def test_state_digest_must_account_for_every_record(self):
        directory = self.fixture()
        records = hosted_fire_audit(directory, stated=SHOTS, shots=SHOTS)
        self.state_of(records, -1)["hosted_fire"] = fire_state(3, rolling(SHOTS) ^ 1)
        self.rewrite_state(directory, records, -1)
        with self.assertRaisesRegex(EvidenceError, "count/digest"):
            AuditLog(directory)

    def test_a_reordered_record_changes_the_digest(self):
        directory = self.fixture()
        records = hosted_fire_audit(directory)
        fires = [row for row in records["events.jsonl"] if row["kind"] == HOSTED_FIRE_KIND]
        fires[0]["payload"], fires[1]["payload"] = fires[1]["payload"], fires[0]["payload"]
        self.rewrite(directory, records, "events.jsonl")
        with self.assertRaisesRegex(EvidenceError, "count/digest"):
            AuditLog(directory)

    def test_record_bound_to_another_boundary_is_refused(self):
        directory = self.fixture()
        records = hosted_fire_audit(directory)
        first = next(row for row in records["events.jsonl"] if row["kind"] == HOSTED_FIRE_KIND)
        first["version"] = {"epoch": 1, "tick": 39, "revision": 0}
        first["payload"]["tick"] = 39
        self.rewrite(directory, records, "events.jsonl")
        with self.assertRaisesRegex(EvidenceError, "next audited pre-step"):
            AuditLog(directory)

    def test_trailing_record_without_a_boundary_is_refused(self):
        directory = self.fixture()
        hosted_fire_audit(directory, trailing=True)
        with self.assertRaisesRegex(EvidenceError, "no following audited boundary"):
            AuditLog(directory)

    def test_readable_column_must_match_its_ieee_bits(self):
        directory = self.fixture()
        records = hosted_fire_audit(directory)
        next(row for row in records["events.jsonl"] if row["kind"] == HOSTED_FIRE_KIND)["payload"]["target_col_text"] = "9.001"
        self.rewrite(directory, records, "events.jsonl")
        with self.assertRaisesRegex(EvidenceError, "readable column"):
            AuditLog(directory)

    def test_mode_declaration_is_strict(self):
        directory = self.fixture()
        hosted_fire_audit(directory)
        manifest = json.loads((directory / "manifest.json").read_text())
        manifest["hosted_fire"]["semantic_change"] = "audit-only"
        (directory / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(EvidenceError, "hosted fire audit mode"):
            AuditLog(directory)
        self.assertIsNone(hosted_fire_mode({"schema": SCHEMA, "target": "synthetic"}))

    def test_digest_encoding_is_pinned_independently(self):
        # Independent implementation of the documented encoding, plus the value
        # the native module produces for the same payload
        # (tests/avz_hosted_fire_tests.cpp prints and asserts it too).
        payload = fire_event(0, SHOTS[2])["payload"]
        value = 14695981039346656037
        for field in ("plant_index", "plant_id", "plant_row", "plant_col",
                      "target_row", "target_col_bits", "tick"):
            word = payload[field] & 0xffffffffffffffff
            for byte in range(8):
                value = ((value ^ ((word >> (8 * byte)) & 0xff)) * 1099511628211) & 0xffffffffffffffff
        self.assertEqual(_hosted_fire_digest(14695981039346656037, payload), value)
        self.assertEqual(value, 0x325e047e1d57a4d7)

    def test_streams_without_the_mode_keep_their_evidence_set(self):
        directory = self.fixture()
        hosted_fire_audit(directory, shots=(), stated=(), declared=False, ticks=(40,))
        log = AuditLog(directory)
        self.assertEqual(audit_files(directory, log.manifest), AUDIT_FILES)
        self.assertEqual([event["kind"] for event in log.control_events], ["recording_closed"])
        self.assertEqual([frame.state.get("hosted_fire") for frame in log.frames], [None, None])


if __name__ == "__main__":
    unittest.main()
