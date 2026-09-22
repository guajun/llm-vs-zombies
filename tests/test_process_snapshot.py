"""Acceptance test for the host-side snapshot PoC (N5 / issue #33).

Builds the 32-bit fixture with the vendored LLVM-MinGW toolchain, runs it, and
requires the R0 check to pass: capture, write the image back, capture again, and
compare both images byte for byte. Skipped when the toolchain or the platform is
unavailable so the rest of the suite keeps working.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

COMPILER = ROOT / "third_party/llvm-mingw-20260908-ucrt-x86_64/bin/i686-w64-mingw32-clang.exe"
FIXTURE_SOURCE = ROOT / "tools/fixtures/snapshot_fixture.c"


@unittest.skipUnless(sys.platform == "win32", "snapshot tool is Windows only")
@unittest.skipUnless(COMPILER.exists(), "vendored i686 toolchain is unavailable")
class ProcessSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import process_snapshot
        cls.tool = process_snapshot
        cls.binary = Path(tempfile.mkdtemp(prefix="lvz-snapshot-")) / "snapshot_fixture.exe"
        subprocess.run([str(COMPILER), "-O1", "-static", str(FIXTURE_SOURCE),
                        "-o", str(cls.binary)], check=True, capture_output=True)
        cls.process = subprocess.Popen([str(cls.binary)], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
        line = cls.process.stdout.readline().strip()
        assert line.startswith("PID="), f"unexpected fixture output: {line!r}"
        cls.pid = int(line.split("=", 1)[1])
        time.sleep(0.3)  # let the worker thread start mutating the block

    @classmethod
    def tearDownClass(cls):
        cls.process.kill()
        cls.process.wait(timeout=10)

    def test_capture_reports_private_writable_regions(self):
        image = self.tool.snapshot(self.pid)
        self.assertTrue(image.wow64, "fixture is expected to be a 32-bit (WOW64) target")
        self.assertGreater(len(image.regions), 0)
        self.assertGreater(image.total_bytes, 4 * 1024 * 1024)
        for region in image.regions:
            self.assertLess(region.base + region.size, 1 << 32)
            self.assertIn(region.protect, self.tool.WRITABLE)
        self.assertGreater(len(image.threads), 0)
        self.assertGreater(len(image.threads[0].context), 0)

    def test_r0_restore_is_identity(self):
        report = self.tool.verify_r0(self.pid)
        self.assertTrue(report["threads"]["equal"], report["threads"])
        self.assertTrue(report["regions"]["equal"], report["regions"])
        self.assertTrue(report["r0"], report)
        self.assertGreater(report["region_count"], 0)
        self.assertGreater(report["region_bytes"], 4 * 1024 * 1024)
        self.assertEqual(report["restore_order"], "thread-contexts-then-memory")
        # The only tolerated difference is the reserved EFlags bit, and only when it
        # is the sole difference in that thread's context record.
        self.assertEqual(report["threads"]["normalization"], self.tool.CONTEXT_NORMALIZATION)
        self.assertLessEqual(report["threads"]["reserved_bit_differences"], len(report["threads"]))

    def test_context_normalization_rejects_anything_else(self):
        left = [self.tool.ThreadRecord(tid=1, handle=0, context=bytearray(512), pointer=0)]
        right = [self.tool.ThreadRecord(tid=1, handle=0, context=bytearray(512), pointer=0)]
        right[0].context[self.tool.EFLAGS_OFFSET] |= self.tool.EFLAGS_RESERVED_MASK
        self.assertTrue(self.tool.compare_threads(left, right)["equal"])
        right[0].context[self.tool.EFLAGS_OFFSET + 4] = 0x7F
        result = self.tool.compare_threads(left, right)
        self.assertFalse(result["equal"])
        self.assertEqual(result["offset"], self.tool.EFLAGS_OFFSET + 4)


if __name__ == "__main__":
    unittest.main()
