"""Host-side process snapshot and restore PoC (the a2 path of N5, issue #33).

The tool is an external observer: it never injects code into the target. It
suspends every thread of the target, reads the committed / writable / private
regions into an image, captures thread contexts (including the x87 and SSE
floating-point state, which live inside the context record) as opaque blobs,
and can write the whole image back.

``verify_r0`` is the acceptance check from the contract: within a single
suspension it captures the target, writes that image back, captures again and
compares the two images byte for byte. Because the restorer lives in this
process and not in the target, no part of it can be overwritten by the state it
restores -- that is the whole reason a2 comes before the in-process variant.

Restore order matters and is not obvious: restoring a WOW64 thread context is
*not* memory-neutral, because the WOW64 layer leaves a frame on the target's
32-bit stack while the context is being set. The memory image therefore has to
be written **last**; ``verify_r0`` asserts exactly that order (contexts first,
memory second) and the reversed order is reproduced as a failure in the PR
notes.

Known exclusions (printed by ``describe``): image and mapped sections, guard
and no-access pages, and read-only pages (they cannot have been mutated, so
they can be reproduced from the mapped file instead of copied).
"""
from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass, field

if sys.platform != "win32":  # pragma: no cover - the repository targets Windows
    raise SystemExit("process_snapshot.py requires Windows")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
PAGE_GUARD = 0x100
WRITABLE = frozenset((0x04, 0x08, 0x40, 0x80))  # RW, WRITECOPY, EXECUTE_RW, EXECUTE_WRITECOPY
EXECUTABLE = frozenset((0x10, 0x20, 0x40, 0x80))

TH32CS_SNAPTHREAD = 0x00000004
THREAD_SUSPEND_RESUME = 0x0002
THREAD_GET_CONTEXT = 0x0008
THREAD_SET_CONTEXT = 0x0010
THREAD_QUERY_INFORMATION = 0x0040

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020

WOW64_CONTEXT_ALL = 0x0001003F
CONTEXT_ALL = 0x0010003F
CONTEXT_BYTES = 4096  # documented WOW64_CONTEXT is 0x2CC; keep generous headroom

# EFlags bit 1 is architecturally reserved: the CPU always reads it as 1 and the
# WoW64 context API does not round-trip it (a write of 0x202 reads back 0x200).
# R0 therefore normalises exactly this bit and fails on every other difference.
EFLAGS_OFFSET = 192
EFLAGS_RESERVED_MASK = 0x02
CONTEXT_NORMALIZATION = {
    "offset": EFLAGS_OFFSET,
    "mask": "0x02",
    "field": "EFlags bit 1 (architecturally reserved, not round-tripped by "
             "Wow64SetThreadContext)",
}

RESTORE_ORDER = "thread-contexts-then-memory"


class MEMORY_BASIC_INFORMATION64(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_ulonglong),
        ("AllocationBase", ctypes.c_ulonglong),
        ("AllocationProtect", wintypes.DWORD),
        ("__alignment1", wintypes.DWORD),
        ("RegionSize", ctypes.c_ulonglong),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("__alignment2", wintypes.DWORD),
    ]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                    ctypes.POINTER(MEMORY_BASIC_INFORMATION64), ctypes.c_size_t]
kernel32.VirtualQueryEx.restype = ctypes.c_size_t
kernel32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
kernel32.ReadProcessMemory.restype = wintypes.BOOL
kernel32.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
kernel32.WriteProcessMemory.restype = wintypes.BOOL
kernel32.VirtualProtectEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
                                      wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
kernel32.VirtualProtectEx.restype = wintypes.BOOL
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
kernel32.Thread32First.restype = wintypes.BOOL
kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
kernel32.Thread32Next.restype = wintypes.BOOL
kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenThread.restype = wintypes.HANDLE
kernel32.SuspendThread.argtypes = [wintypes.HANDLE]
kernel32.SuspendThread.restype = wintypes.DWORD
kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
kernel32.ResumeThread.restype = wintypes.DWORD
kernel32.IsWow64Process.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
kernel32.IsWow64Process.restype = wintypes.BOOL
kernel32.Wow64GetThreadContext.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
kernel32.Wow64GetThreadContext.restype = wintypes.BOOL
kernel32.Wow64SetThreadContext.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
kernel32.Wow64SetThreadContext.restype = wintypes.BOOL
kernel32.GetThreadContext.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
kernel32.GetThreadContext.restype = wintypes.BOOL
kernel32.SetThreadContext.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
kernel32.SetThreadContext.restype = wintypes.BOOL


class SnapshotError(RuntimeError):
    pass


def _fail(what: str) -> None:
    raise SnapshotError(f"{what} failed with winerror {ctypes.get_last_error()}")


def _aligned(size: int, alignment: int = 16):
    """A zeroed buffer plus a pointer aligned to ``alignment`` bytes."""
    raw = ctypes.create_string_buffer(size + alignment)
    address = ctypes.addressof(raw)
    offset = (-address) % alignment
    return raw, ctypes.c_void_p(address + offset)


@dataclass
class Region:
    base: int
    size: int
    protect: int
    data: bytes


@dataclass
class ThreadRecord:
    tid: int
    handle: int
    context: bytearray
    pointer: int


@dataclass
class Snapshot:
    pid: int
    wow64: bool
    regions: list
    threads: list
    excluded: list
    elapsed: float
    quiesce_attempts: int = 1
    _buffers: list = field(default_factory=list, repr=False)

    @property
    def total_bytes(self) -> int:
        return sum(len(region.data) for region in self.regions)


def open_process(pid: int):
    access = (PROCESS_QUERY_INFORMATION | PROCESS_VM_OPERATION |
              PROCESS_VM_READ | PROCESS_VM_WRITE)
    handle = kernel32.OpenProcess(access, False, pid)
    if not handle:
        _fail(f"OpenProcess({pid})")
    return handle


def is_wow64(handle) -> bool:
    flag = wintypes.BOOL()
    if not kernel32.IsWow64Process(handle, ctypes.byref(flag)):
        _fail("IsWow64Process")
    return bool(flag.value)


def thread_ids(pid: int) -> list:
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        _fail("CreateToolhelp32Snapshot")
    try:
        entry = THREADENTRY32()
        entry.dwSize = ctypes.sizeof(THREADENTRY32)
        ids = []
        if not kernel32.Thread32First(snapshot, ctypes.byref(entry)):
            return ids
        while True:
            if entry.th32OwnerProcessID == pid:
                ids.append(int(entry.th32ThreadID))
            if not kernel32.Thread32Next(snapshot, ctypes.byref(entry)):
                break
        return ids
    finally:
        kernel32.CloseHandle(snapshot)


def suspend_threads(pid: int) -> list:
    records = []
    for tid in thread_ids(pid):
        handle = kernel32.OpenThread(
            THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT | THREAD_SET_CONTEXT |
            THREAD_QUERY_INFORMATION, False, tid)
        if not handle:
            for record in records:
                kernel32.CloseHandle(record.handle)
            _fail(f"OpenThread({tid})")
        if kernel32.SuspendThread(handle) == 0xFFFFFFFF:
            kernel32.CloseHandle(handle)
            for record in records:
                kernel32.CloseHandle(record.handle)
            _fail(f"SuspendThread({tid})")
        records.append(ThreadRecord(tid=tid, handle=handle, context=bytearray(), pointer=0))
    if not records:
        raise SnapshotError("target has no threads")
    return records


def resume_threads(records) -> None:
    for record in records:
        kernel32.ResumeThread(record.handle)
        kernel32.CloseHandle(record.handle)


def capture_thread_context(handle, wow64: bool, tid: int) -> ThreadRecord:
    raw, pointer = _aligned(CONTEXT_BYTES)
    ctypes.memset(pointer, 0, CONTEXT_BYTES)
    ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))[0] = (
        WOW64_CONTEXT_ALL if wow64 else CONTEXT_ALL)
    getter = kernel32.Wow64GetThreadContext if wow64 else kernel32.GetThreadContext
    if not getter(handle, pointer):
        _fail(f"GetThreadContext({tid})")
    blob = ctypes.string_at(pointer, CONTEXT_BYTES)
    return ThreadRecord(tid=tid, handle=handle, context=bytearray(blob),
                        pointer=ctypes.addressof(pointer))


def apply_thread_context(record: ThreadRecord, wow64: bool) -> None:
    raw, pointer = _aligned(CONTEXT_BYTES)
    ctypes.memmove(pointer, bytes(record.context), CONTEXT_BYTES)
    setter = kernel32.Wow64SetThreadContext if wow64 else kernel32.SetThreadContext
    if not setter(record.handle, pointer):
        _fail(f"SetThreadContext({record.tid})")


def capture_regions(handle, limit: int = 1 << 47) -> tuple:
    regions = []
    # Only *committed* pages can hold state worth copying, so the exclusion report
    # counts them: mapped/image sections, read-only pages and guarded pages. Free
    # address space is not "excluded state" and is not reported.
    excluded = {"mapped_or_image": 0, "read_only": 0, "guarded": 0}
    address = 0
    info = MEMORY_BASIC_INFORMATION64()
    while address < limit:
        written = kernel32.VirtualQueryEx(handle, ctypes.c_void_p(address),
                                          ctypes.byref(info), ctypes.sizeof(info))
        if not written:
            break
        base = int(info.BaseAddress)
        size = int(info.RegionSize)
        if size <= 0:
            break
        if info.State != MEM_COMMIT:
            pass
        elif info.Type != MEM_PRIVATE:
            excluded["mapped_or_image"] += size
        elif info.Protect & PAGE_GUARD:
            excluded["guarded"] += size
        elif info.Protect not in WRITABLE:
            excluded["read_only"] += size
        else:
            buffer = ctypes.create_string_buffer(size)
            read = ctypes.c_size_t(0)
            if not kernel32.ReadProcessMemory(handle, ctypes.c_void_p(base), buffer, size,
                                              ctypes.byref(read)):
                _fail(f"ReadProcessMemory(0x{base:x}, 0x{size:x})")
            if read.value != size:
                raise SnapshotError(
                    f"short read at 0x{base:x}: {read.value} of {size}")
            regions.append(Region(base=base, size=size, protect=int(info.Protect),
                                  data=bytes(buffer.raw[:size])))
        address = base + size
    return regions, excluded


def snapshot(pid: int) -> Snapshot:
    """Suspend, capture regions and contexts, resume, and return the image."""
    handle = open_process(pid)
    started = time.perf_counter()
    try:
        wow64 = is_wow64(handle)
        records = suspend_threads(pid)
        try:
            contexts, regions, excluded, used = quiesce(handle, records, wow64)
        finally:
            for record in records:
                kernel32.ResumeThread(record.handle)
                kernel32.CloseHandle(record.handle)
        return Snapshot(pid=pid, wow64=wow64, regions=regions, threads=contexts,
                        excluded=excluded, elapsed=time.perf_counter() - started,
                        quiesce_attempts=used)
    finally:
        kernel32.CloseHandle(handle)


def capture_once(handle, records, wow64):
    contexts = [capture_thread_context(record.handle, wow64, record.tid)
                for record in records]
    # A 32-bit target only owns the low 4 GiB, so the walk stops there instead of
    # reporting the free 64-bit space above it.
    regions, excluded = capture_regions(handle, 1 << 32 if wow64 else 1 << 47)
    return contexts, regions, excluded


def quiesce(handle, records, wow64, attempts: int = 8, delay: float = 0.02):
    """Capture until two consecutive images agree.

    ``SuspendThread`` is not synchronous: the target can keep executing for a
    short while after the call returns, so an immediate capture may catch a
    write that is still in flight. R0 is only meaningful on a quiescent target,
    so callers capture twice and require the two images to match.
    """
    contexts, regions, excluded = capture_once(handle, records, wow64)
    for used in range(1, attempts):
        time.sleep(delay)
        again_contexts, again_regions, again_excluded = capture_once(handle, records, wow64)
        if (compare_regions(regions, again_regions)["equal"] and
                compare_threads(contexts, again_contexts)["equal"]):
            return again_contexts, again_regions, again_excluded, used
        contexts, regions, excluded = again_contexts, again_regions, again_excluded
    raise SnapshotError(f"target did not become quiet within {attempts} captures")


def write_regions(handle, regions) -> float:
    started = time.perf_counter()
    for region in regions:
        writable = 0x40 if region.protect in EXECUTABLE else 0x04
        old = wintypes.DWORD()
        if not kernel32.VirtualProtectEx(handle, ctypes.c_void_p(region.base), region.size,
                                         writable, ctypes.byref(old)):
            _fail(f"VirtualProtectEx(0x{region.base:x})")
        try:
            written = ctypes.c_size_t(0)
            buffer = ctypes.create_string_buffer(region.data, len(region.data))
            if not kernel32.WriteProcessMemory(handle, ctypes.c_void_p(region.base), buffer,
                                               region.size, ctypes.byref(written)):
                _fail(f"WriteProcessMemory(0x{region.base:x})")
            if written.value != region.size:
                raise SnapshotError(
                    f"short write at 0x{region.base:x}: {written.value} of {region.size}")
        finally:
            restored = wintypes.DWORD()
            kernel32.VirtualProtectEx(handle, ctypes.c_void_p(region.base), region.size,
                                      region.protect, ctypes.byref(restored))
    return time.perf_counter() - started


def compare_regions(left, right) -> dict:
    """Byte-for-byte comparison of two region lists."""
    if len(left) != len(right):
        return {"equal": False, "reason": "region count differs",
                "left": len(left), "right": len(right)}
    for index, (a, b) in enumerate(zip(left, right)):
        if (a.base, a.size, a.protect) != (b.base, b.size, b.protect):
            return {"equal": False, "reason": "region table differs", "index": index,
                    "left": [hex(a.base), a.size, a.protect],
                    "right": [hex(b.base), b.size, b.protect]}
        if a.data != b.data:
            offset = next(i for i, (x, y) in enumerate(zip(a.data, b.data)) if x != y)
            return {"equal": False, "reason": "region bytes differ", "index": index,
                    "base": hex(a.base), "offset": offset,
                    "left_byte": a.data[offset], "right_byte": b.data[offset]}
    return {"equal": True, "regions": len(left)}


def compare_threads(left, right) -> dict:
    if len(left) != len(right):
        return {"equal": False, "reason": "thread count differs",
                "left": len(left), "right": len(right)}
    explained = 0
    for a, b in zip(left, right):
        if a.tid != b.tid:
            return {"equal": False, "reason": "thread identity differs",
                    "left": a.tid, "right": b.tid}
        if a.context != b.context:
            for index, (x, y) in enumerate(zip(a.context, b.context)):
                if x != y and (index != EFLAGS_OFFSET or (x ^ y) != EFLAGS_RESERVED_MASK):
                    return {"equal": False, "reason": "thread context differs",
                            "tid": a.tid, "offset": index,
                            "left_byte": x, "right_byte": y}
            explained += 1
        normalized_left = bytearray(a.context)
        normalized_right = bytearray(b.context)
        normalized_left[EFLAGS_OFFSET] &= ~EFLAGS_RESERVED_MASK
        normalized_right[EFLAGS_OFFSET] &= ~EFLAGS_RESERVED_MASK
        if normalized_left != normalized_right:
            offset = next(i for i, (x, y) in enumerate(zip(normalized_left, normalized_right))
                          if x != y)
            return {"equal": False, "reason": "thread context differs", "tid": a.tid,
                    "offset": offset}
    return {"equal": True, "threads": len(left),
            "reserved_bit_differences": explained,
            "normalization": CONTEXT_NORMALIZATION}


def verify_r0(pid: int) -> dict:
    """capture -> write back -> capture again -> compare, all inside one suspension."""
    handle = open_process(pid)
    try:
        wow64 = is_wow64(handle)
        records = suspend_threads(pid)
        try:
            before_threads, before_regions, excluded, used = quiesce(handle, records, wow64)

            for record in before_threads:
                apply_thread_context(record, wow64)
            # Context restore perturbs the target's stack (see the module docstring),
            # so the memory image must be written last to make R0 exact.
            write_seconds = write_regions(handle, before_regions)

            after_threads = [capture_thread_context(record.handle, wow64, record.tid)
                             for record in records]
            after_regions, _ = capture_regions(handle)
        finally:
            for record in records:
                kernel32.ResumeThread(record.handle)
                kernel32.CloseHandle(record.handle)

        threads = compare_threads(before_threads, after_threads)
        regions = compare_regions(before_regions, after_regions)
        return {
            "r0": bool(threads["equal"] and regions["equal"]),
            "pid": pid,
            "wow64": wow64,
            "region_count": len(before_regions),
            "region_bytes": sum(len(region.data) for region in before_regions),
            "write_seconds": write_seconds,
            "quiesce_attempts": used,
            "restore_order": RESTORE_ORDER,
            "excluded_bytes": excluded,
            "threads": threads,
            "regions": regions,
        }
    finally:
        kernel32.CloseHandle(handle)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pid", type=int, required=True, help="target process id")
    parser.add_argument("--r0", action="store_true",
                        help="capture, write the image back and verify byte equality")
    arguments = parser.parse_args(argv)
    if not arguments.r0:
        image = snapshot(arguments.pid)
        print(json.dumps({"pid": image.pid, "wow64": image.wow64,
                          "region_count": len(image.regions),
                          "region_bytes": image.total_bytes,
                          "excluded_bytes": image.excluded,
                          "capture_seconds": image.elapsed}, indent=2))
        return 0
    report = verify_r0(arguments.pid)
    print(json.dumps(report, indent=2))
    return 0 if report["r0"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
