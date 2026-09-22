"""Offline tests for the gzip audit-evidence container contract."""
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from llm_vs_zombies import evidence_codec as ec
from llm_vs_zombies.audit_compare import AuditTail, EventStream, EvidenceError


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sample(directory: Path, rows=3):
    rows = [json.dumps({"kind": "row", "seq": index,
                        "payload": {"value": "x" * 64, "index": index}}, sort_keys=True)
            for index in range(rows)]
    for name in ("events.jsonl", "state-deltas.jsonl"):
        (directory / name).write_text("\n".join(rows) + "\n", encoding="utf-8")
    return rows


class CompressionTests(unittest.TestCase):
    def test_containers_reproduce_plain_bytes_and_are_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            rows = sample(directory)
            plain = {name: (directory / name).read_bytes() for name in ("events.jsonl", "state-deltas.jsonl")}
            receipt = ec.compress_evidence(directory)
            self.assertEqual(receipt["state"], "complete")
            self.assertTrue((directory / ec.RECEIPT).is_file())
            for name, raw in plain.items():
                stored = directory / (name + ec.SUFFIX)
                self.assertTrue(stored.is_file())
                self.assertFalse((directory / name).exists())
                self.assertEqual(gzip.decompress(stored.read_bytes()), raw)
                entry = receipt["files"][name]
                self.assertEqual((entry["plain_sha256"], entry["plain_bytes"]), (sha_hash(raw), len(raw)))
                self.assertEqual(entry["stored_sha256"], sha(stored))
                self.assertLess(entry["stored_bytes"], entry["plain_bytes"])
            store = ec.EvidenceStore(directory)
            self.assertTrue(store.compressed)
            self.assertEqual([row for row in EventStream(store, "events.jsonl")],
                             [json.loads(row) for row in rows])
        # Deterministic containers: identical input bytes give identical output.
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            sample(Path(first)); sample(Path(second))
            ec.compress_evidence(Path(first)); ec.compress_evidence(Path(second))
            self.assertEqual((Path(first) / ("events.jsonl" + ec.SUFFIX)).read_bytes(),
                             (Path(second) / ("events.jsonl" + ec.SUFFIX)).read_bytes())

    def test_interrupted_compression_rolls_back_to_plain(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            sample(directory)
            original = ec._compress_file
            calls = []

            def failing(source, destination):
                calls.append(source.name)
                if len(calls) > 1:
                    raise RuntimeError("simulated disk failure")
                return original(source, destination)

            ec._compress_file = failing
            try:
                with self.assertRaises(RuntimeError):
                    ec.compress_evidence(directory)
            finally:
                ec._compress_file = original
            self.assertEqual(sorted(path.name for path in directory.iterdir()),
                             ["events.jsonl", "state-deltas.jsonl"])
            store = ec.EvidenceStore(directory)
            self.assertFalse(store.compressed)
            self.assertTrue(store.exists("events.jsonl"))

    def test_tampered_container_and_undeclared_forms_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            sample(directory)
            ec.compress_evidence(directory)
            stored = directory / ("events.jsonl" + ec.SUFFIX)
            raw = bytearray(stored.read_bytes())
            raw[-5] ^= 0x40
            stored.write_bytes(bytes(raw))
            store = ec.EvidenceStore(directory)
            with self.assertRaisesRegex(ValueError, "stored audit evidence SHA-256 changed"):
                store.verify("events.jsonl")
            with self.assertRaisesRegex(Exception, "SHA-256 changed"):
                list(EventStream(store, "events.jsonl"))
        with tempfile.TemporaryDirectory() as temporary:
            # A container without its receipt is an incomplete archive.
            directory = Path(temporary)
            sample(directory)
            (directory / ("events.jsonl" + ec.SUFFIX)).write_bytes(gzip.compress(b"{}\n"))
            with self.assertRaisesRegex(ValueError, "lacks its codec receipt"):
                ec.EvidenceStore(directory)
        with tempfile.TemporaryDirectory() as temporary:
            # A leftover plain file that differs from the recorded bytes fails.
            directory = Path(temporary)
            sample(directory)
            ec.compress_evidence(directory)
            (directory / "events.jsonl").write_text('{"different": true}\n', encoding="utf-8")
            store = ec.EvidenceStore(directory)
            with self.assertRaisesRegex(ValueError, "leftover plain evidence differs"):
                store.verify("events.jsonl")
        with tempfile.TemporaryDirectory() as temporary:
            # A declared container that disappeared is a missing file, never a
            # silent fall back to a different form.
            directory = Path(temporary)
            sample(directory)
            ec.compress_evidence(directory)
            (directory / ("events.jsonl" + ec.SUFFIX)).unlink()
            store = ec.EvidenceStore(directory)
            with self.assertRaisesRegex(ValueError, "missing audit evidence"):
                store.open("events.jsonl")

    def test_legacy_plain_archives_keep_their_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            sample(directory)
            store = ec.EvidenceStore(directory)
            self.assertFalse(store.compressed)
            self.assertEqual(store.stored_path("events.jsonl"), directory / "events.jsonl")
            self.assertEqual(len(list(EventStream(store, "events.jsonl"))), 3)

    def test_live_tail_refuses_a_compressed_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            sample(directory)
            (directory / "manifest.json").write_text(json.dumps({"schema": "lvz.audit.v1"}), encoding="utf-8")
            ec.compress_evidence(directory)
            with self.assertRaisesRegex(EvidenceError, "requires plain evidence"):
                AuditTail(directory)


def sha_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


if __name__ == "__main__":
    unittest.main()
