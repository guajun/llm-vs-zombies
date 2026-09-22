"""Tests for the static import-table scanner (N6 / issue #34).

The scanner is the only evidence source for the "static import table" class in
``docs/跨界耦合清单.md``, so it is checked two ways: against a synthetic PE
built here (always runs, no game needed), and against the pinned engine when
the private local copy happens to be present.
"""
from __future__ import annotations

import struct
import sys
import tempfile
import unittest
import contextlib
import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import import_scan  # noqa: E402  (path set up above)

ENGINE = ROOT / "game/local-engine/PlantsVsZombies.exe"
ENGINE_SHA256 = "f9669af338964787a3785a7895791297d599295b8bb669b0db49443f736a1322"


def build_pe(*, imports=(("KERNEL32.dll", "GetTickCount"),), image_base=0x400000,
             idata_rva=0x1000, raw_pointer=0x200) -> bytes:
    """A minimal but structurally real PE32 with one import descriptor."""
    opt_size = 0xE0
    headers_end = 0x40 + 4 + 20 + opt_size + 40  # DOS + PE + COFF + optional + 1 section
    data = bytearray(max(headers_end, raw_pointer + 0x400))
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x40)
    data[0x40:0x44] = b"PE\x00\x00"
    coff = 0x44
    struct.pack_into("<HHIIIHH", data, coff, 0x14C, 1, 0, 0, 0, opt_size, 0x0102)
    opt = coff + 20
    struct.pack_into("<H", data, opt, 0x10B)                      # PE32
    struct.pack_into("<I", data, opt + 24, 4)                     # linker versions
    struct.pack_into("<I", data, opt + 28, image_base)
    struct.pack_into("<I", data, opt + 32, 0x1000)                # SectionAlignment
    struct.pack_into("<I", data, opt + 36, 0x200)                 # FileAlignment
    struct.pack_into("<I", data, opt + 56, 0x2000)                # SizeOfImage
    struct.pack_into("<I", data, opt + 60, headers_end)           # SizeOfHeaders
    struct.pack_into("<H", data, opt + 68, 2)                     # Subsystem
    struct.pack_into("<I", data, opt + 92, 16)                    # NumberOfRvaAndSizes
    struct.pack_into("<II", data, opt + 96 + 8, idata_rva, 0x28)  # data dir 1 = import
    section = opt + opt_size
    data[section:section + 8] = b".idata\x00\x00"
    struct.pack_into("<IIII", data, section + 8, 0x1000, idata_rva, 0x400, raw_pointer)
    struct.pack_into("<I", data, section + 36, 0xC0000040)
    base = raw_pointer
    int_rva, iat_rva = idata_rva + 0x28, idata_rva + 0x50
    name_rva = idata_rva + 0x40
    struct.pack_into("<IIIII", data, base, int_rva, 0, 0, name_rva, iat_rva)
    struct.pack_into("<IIIII", data, base + 20, 0, 0, 0, 0, 0)    # terminator
    hint_rva = idata_rva + 0x60
    struct.pack_into("<II", data, base + 0x28, hint_rva, 0)
    struct.pack_into("<II", data, base + 0x50, hint_rva, 0)
    data[base + 0x40:base + 0x40 + 13] = b"KERNEL32.dll\x00"
    cursor = base + 0x60
    offset = base + 0x60
    for dll, *names in imports:
        for name in names:
            struct.pack_into("<H", data, offset, 0)
            encoded = name.encode("ascii") + b"\x00"
            data[offset + 2:offset + 2 + len(encoded)] = encoded
            offset += 2 + len(encoded)
    assert cursor == base + 0x60
    return bytes(data)


class SyntheticPETests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="lvz-import-scan-"))

    def write(self, name: str, payload: bytes) -> Path:
        path = self.directory / name
        path.write_bytes(payload)
        return path

    def test_import_descriptor_and_iat_va(self):
        path = self.write("synthetic.exe", build_pe())
        report = import_scan.scan_file(path, list(import_scan.WATCHLIST), False)
        self.assertEqual(report["parse"], "ok")
        self.assertEqual(report["kind"], "PE32")
        self.assertEqual(report["image_base"], "0x400000")
        self.assertEqual(report["imported_dlls"], ["KERNEL32.dll"])
        self.assertEqual([match["api"] for match in report["matches"]], ["GetTickCount"])
        match = report["matches"][0]
        self.assertEqual(match["family"], "time")
        self.assertEqual(match["source"], "import")
        # The IAT slot is image_base + FirstThunk RVA: this is the exact address a
        # launcher-style patch would overwrite.
        self.assertEqual(match["iat_va"], hex(0x400000 + 0x1050))

    def test_watchlist_families_are_disjoint_and_named(self):
        for family, apis in import_scan.WATCHLIST.items():
            self.assertTrue(apis, family)
            self.assertEqual(len(apis), len(set(apis)), family)
        seen = {}
        for family, apis in import_scan.WATCHLIST.items():
            for api in apis:
                self.assertNotIn(api, seen, f"{api} appears in {seen.get(api)} and {family}")
                seen[api] = family
        for required in ("GetTickCount", "GetTickCount64", "QueryPerformanceCounter",
                         "QueryPerformanceFrequency", "time", "_time64",
                         "GetCursorPos", "GetForegroundWindow", "CreateFileA",
                         "CreateMutexA"):
            self.assertIn(required, seen)

    def test_absent_api_is_reported_not_invented(self):
        path = self.write("synthetic.exe", build_pe())
        report = import_scan.scan_file(path, list(import_scan.WATCHLIST), False)
        summary = import_scan.build_summary([report], list(import_scan.WATCHLIST))
        self.assertEqual(summary["families"]["time"], ["GetTickCount"])
        self.assertIn("QueryPerformanceCounter", summary["absent_from_import_tables"]["time"])
        self.assertIn("GetTickCount64", summary["absent_from_import_tables"]["time"])

    def test_string_scan_is_separate_from_import_scan(self):
        # A name that only appears as a GetProcAddress argument must show up as a
        # string hit and never as an import match.
        path = self.write("synthetic.exe", build_pe())
        raw = Path(path).read_bytes().replace(b"GetTickCount\x00",
                                              b"NotFoundApi\x00" + b"GetLastInputInfo\x00")
        path.write_bytes(raw)
        report = import_scan.scan_file(path, list(import_scan.WATCHLIST), True)
        self.assertEqual(report["matches"], [])
        hits = {hit["api"] for hit in report["string_hits"]}
        self.assertIn("GetLastInputInfo", hits)

    def test_non_pe_input_is_an_error_not_a_crash(self):
        path = self.write("not-pe.bin", b"this is not a PE file" * 4)
        report = import_scan.scan_file(path, ["time"], False)
        self.assertTrue(report["parse"].startswith("error:"))
        self.assertEqual(report["matches"], [])

    def test_truncated_import_table_is_an_error_not_a_crash(self):
        payload = build_pe()
        path = self.write("truncated.exe", payload[:0x230])
        report = import_scan.scan_file(path, ["time"], False)
        self.assertTrue(report["parse"].startswith("error:"))

    def test_cli_writes_machine_readable_report(self):
        path = self.write("synthetic.exe", build_pe())
        out = self.directory / "report.json"
        text = self.directory / "report.txt"
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            status = import_scan.main(["--json", str(out), "--text", str(text), str(path)])
        self.assertEqual(status, 0)
        report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(report["schema"], "lvz.import-scan.v1")
        self.assertEqual(report["files"][0]["matches"][0]["api"], "GetTickCount")
        self.assertIn("GetTickCount", text.read_text(encoding="utf-8"))
        self.assertIn("GetTickCount", printed.getvalue())

    def test_missing_file_reports_failure(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            status = import_scan.main([str(self.directory / "absent.exe")])
        self.assertEqual(status, 1)

    def test_unknown_family_is_rejected(self):
        path = self.write("synthetic.exe", build_pe())
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            status = import_scan.main(["--families", "time,nonsense", str(path)])
        self.assertEqual(status, 2)


@unittest.skipUnless(ENGINE.is_file(), "private local engine copy is unavailable")
class PinnedEngineTests(unittest.TestCase):
    """Re-checks the IAT slots quoted in the coupling inventory."""

    def test_engine_time_slots_match_the_documented_addresses(self):
        report = import_scan.scan_file(ENGINE, ["time", "dynamic_loader"], False)
        self.assertEqual(report["parse"], "ok")
        self.assertEqual(report["sha256"], ENGINE_SHA256)
        slots = {match["api"]: match["iat_va"] for match in report["matches"]}
        self.assertEqual(slots["GetTickCount"], "0x65208c")
        self.assertEqual(slots["QueryPerformanceCounter"], "0x652088")
        self.assertEqual(slots["QueryPerformanceFrequency"], "0x652084")
        self.assertEqual(slots["GetSystemTimeAsFileTime"], "0x652190")
        self.assertEqual(slots["GetLocalTime"], "0x6521a8")

    def test_engine_has_no_import_slot_for_the_absent_names(self):
        report = import_scan.scan_file(ENGINE, list(import_scan.WATCHLIST), True)
        imported = {match["api"] for match in report["matches"]}
        for absent in ("GetTickCount64", "DirectSoundCreate", "_time64", "fopen",
                       "GetForegroundWindow", "GetAsyncKeyState"):
            self.assertNotIn(absent, imported)
        # DirectSound is reached through GetProcAddress, so only the raw string
        # can show it; the tool must keep the two classes apart.
        self.assertIn("DirectSoundCreate",
                      {hit["api"] for hit in report["string_hits"]})


if __name__ == "__main__":
    unittest.main()
