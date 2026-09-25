"""Offline tests for the #111 candidate native-path evidence checker.

No game, no executable required: the repository document is checked for
structure/anchors, negative mutations must fail, and the PE RVA mapping and
byte verification are exercised against a synthetic PE image.
"""
import copy
import hashlib
import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

import issue111_native_candidates as candidates  # noqa: E402


def synthetic_pe(payload: bytes) -> bytes:
    data = bytearray(0x400)
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    coff = 0x84
    struct.pack_into("<H", data, coff + 2, 1)       # NumberOfSections
    struct.pack_into("<H", data, coff + 16, 0xE0)   # SizeOfOptionalHeader
    struct.pack_into("<H", data, coff + 20, 0x10B)  # PE32 magic
    section = coff + 20 + 0xE0
    data[section:section + 8] = b".text\0\0\0"
    struct.pack_into("<I", data, section + 8, len(payload))   # VirtualSize
    struct.pack_into("<I", data, section + 12, 0x1000)        # VirtualAddress
    struct.pack_into("<I", data, section + 16, len(payload))  # SizeOfRawData
    struct.pack_into("<I", data, section + 20, 0x200)         # PointerToRawData
    data[0x200:0x200 + len(payload)] = payload
    return bytes(data)


class EvidenceDocumentTests(unittest.TestCase):
    def setUp(self):
        self.doc = candidates.load()

    def problems(self, doc):
        return candidates.check(doc, root=ROOT)

    def test_repository_evidence_passes_and_is_candidate_only(self):
        self.assertEqual(self.problems(self.doc), [])
        self.assertEqual(self.doc["schema"], candidates.SCHEMA)
        self.assertEqual(self.doc["status"], "candidate_review_required")
        self.assertGreaterEqual(len(self.doc["candidates"]), 8)
        self.assertTrue(all(item["status"] == "candidate_review_required" for item in self.doc["candidates"]))
        self.assertIsNone(self.doc["reviewed_by"])

    def test_duplicate_candidate_id_fails(self):
        doc = copy.deepcopy(self.doc)
        doc["candidates"][1]["id"] = doc["candidates"][0]["id"]
        self.assertTrue(any("duplicate candidate id" in problem for problem in self.problems(doc)))

    def test_rva_must_match_entry(self):
        doc = copy.deepcopy(self.doc)
        doc["candidates"][0]["rva"] = "0x1"
        self.assertTrue(any("rva must equal" in problem for problem in self.problems(doc)))

    def test_established_requires_review(self):
        doc = copy.deepcopy(self.doc)
        doc["candidates"][0]["status"] = "established"
        problems = self.problems(doc)
        self.assertTrue(any("without a maintainer review note" in problem for problem in problems))
        self.assertTrue(any("reviewed_by" in problem for problem in problems))

    def test_missing_key_and_bad_bytes_fail(self):
        doc = copy.deepcopy(self.doc)
        doc["candidates"][0].pop("abi")
        doc["candidates"][1]["bytes32"] = "zz"
        problems = self.problems(doc)
        self.assertTrue(any("missing key: abi" in problem for problem in problems))
        self.assertTrue(any("bytes32" in problem for problem in problems))

    def test_repository_target_cross_checks(self):
        doc = copy.deepcopy(self.doc)
        doc["target"]["sha256"] = "0" * 64
        self.assertTrue(any("determinism/evidence.json" in problem for problem in self.problems(doc)))
        doc = copy.deepcopy(self.doc)
        doc["target"]["avz_commit"] = "0" * 40
        self.assertTrue(any("dependencies.lock.json" in problem for problem in self.problems(doc)))

    def test_semantic_fields_and_interception_plan_are_required(self):
        facts = {item["fact"] for item in self.doc["interception_plan"]}
        self.assertEqual(facts, {"zombie_removal_unclassified", "zombie_death_stage_enter", "zombie_slot_recycled"})
        self.assertTrue(self.doc["residual_questions"])
        doc = copy.deepcopy(self.doc)
        doc["candidates"][0].pop("semantic_gate")
        doc["interception_plan"] = []
        problems = self.problems(doc)
        self.assertTrue(any("semantic_gate" in problem for problem in problems))
        self.assertTrue(any("interception_plan" in problem for problem in problems))


class PeVerificationTests(unittest.TestCase):
    def setUp(self):
        self.payload = bytes(range(64))
        self.exe = synthetic_pe(self.payload)
        doc = copy.deepcopy(candidates.load())
        candidate = doc["candidates"][0]
        candidate["entry_va"] = "0x401000"
        candidate["rva"] = "0x1000"
        candidate["function_start_va"] = "0x401000"
        candidate["bytes32"] = self.payload[:32].hex()
        doc["target"]["sha256"] = hashlib.sha256(self.exe).hexdigest()
        doc["candidates"] = [candidate]
        self.doc = doc

    def test_pe_mapping_and_successful_verification(self):
        self.assertEqual(candidates.bytes_at(self.exe, 0x1000, 4), self.payload[:4])
        report = candidates.verify(self.doc, self.bytes_path())
        self.assertTrue(report["sha256_match"])
        self.assertTrue(report["ok"])
        self.assertEqual(report["results"][0]["status"], "match")

    def bytes_path(self):
        handle = tempfile.NamedTemporaryFile(suffix=".exe", delete=False)
        handle.write(self.exe)
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return Path(handle.name)

    def test_corrupted_payload_is_reported(self):
        path = self.bytes_path()
        data = bytearray(path.read_bytes())
        data[0x200] ^= 0xFF
        path.write_bytes(bytes(data))
        report = candidates.verify(self.doc, path)
        self.assertFalse(report["ok"])
        self.assertEqual(report["results"][0]["status"], "mismatch")

    def test_sha_mismatch_is_reported(self):
        doc = copy.deepcopy(self.doc)
        doc["target"]["sha256"] = "0" * 64
        report = candidates.verify(doc, self.bytes_path())
        self.assertFalse(report["sha256_match"])
        self.assertFalse(report["ok"])

    def test_bytes_outside_sections_is_an_error(self):
        with self.assertRaises(candidates.CandidateError):
            candidates.bytes_at(self.exe, 0x5000, 4)


class CliTests(unittest.TestCase):
    def test_check_cli_ok(self):
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "issue111_native_candidates.py"), "check"],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])

    def test_check_cli_unreadable_is_exit_2(self):
        with tempfile.TemporaryDirectory() as temp:
            bad = Path(temp) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            result = subprocess.run([sys.executable, str(ROOT / "tools" / "issue111_native_candidates.py"),
                                     "--evidence", str(bad), "check"], capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 2)

    def test_verify_cli_with_synthetic_image(self):
        payload = b"\x90" * 32
        exe = synthetic_pe(payload)
        doc = copy.deepcopy(candidates.load())
        candidate = doc["candidates"][0]
        candidate["entry_va"] = "0x401000"
        candidate["rva"] = "0x1000"
        candidate["function_start_va"] = "0x401000"
        candidate["bytes32"] = payload.hex()
        doc["target"]["sha256"] = hashlib.sha256(exe).hexdigest()
        doc["candidates"] = [candidate]
        with tempfile.TemporaryDirectory() as temp:
            evidence = Path(temp) / "evidence.json"
            evidence.write_text(json.dumps(doc), encoding="utf-8")
            exe_path = Path(temp) / "image.exe"
            exe_path.write_bytes(exe)
            result = subprocess.run([sys.executable, str(ROOT / "tools" / "issue111_native_candidates.py"),
                                     "--evidence", str(evidence), "verify", "--exe", str(exe_path)],
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()
