"""Static PE import-table scan for cross-boundary coupling candidates (N6 / #34).

The tool answers one narrow question: which of the cross-boundary APIs named by
``docs/分支数据合同与实施计划.md`` §3.4 are reachable *through the static import
table* of a target PE. It never loads, maps or executes the file, and it never
attaches to a process; the file is read as bytes and parsed.

It is deliberately not a general disassembler. Two consequences are reported
explicitly instead of being guessed at:

* An API that is absent from the import table can still be called, because a
  statically linked CRT (``time`` / ``_time64``) implements it inside the image
  and because ``GetProcAddress`` resolves names at runtime. Absence is only
  "no static import", never "not called".
* C++ virtual methods such as ``DirectSoundBuffer::GetStatus`` (the device
  query behind ``IsPlaying``) are not imports at all. The scan reports the
  owning module (``dsound.dll``) and the driver entry points instead.

``--strings`` adds a raw byte search for the same names. A hit there is weaker
evidence than an import descriptor: it shows the name is present in the file
(a runtime lookup or a debug string), not that it is bound.

Output schema: ``lvz.import-scan.v1``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

SCHEMA = "lvz.import-scan.v1"

#: Cross-boundary API families from the contract's residual-coupling list
#: (§3.4) plus the immediate neighbours that share a read point. The family is
#: the analysis unit here, matching the contract's grouping.
WATCHLIST: dict[str, list[str]] = {
    "time": [
        "GetTickCount",
        "GetTickCount64",
        "QueryPerformanceCounter",
        "QueryPerformanceFrequency",
        "timeGetTime",
        "timeGetSystemTime",
        "GetSystemTimeAsFileTime",
        "GetSystemTimePreciseAsFileTime",
        "GetLocalTime",
        "GetSystemTime",
        "GetMessageTime",
        "time",
        "_time32",
        "_time64",
        "clock",
        "GetCurrentThreadTimes",
        "GetProcessTimes",
        "timeSetEvent",
        "timeKillEvent",
    ],
    "audio": [
        "DirectSoundCreate",
        "DirectSoundCreate8",
        "DirectSoundEnumerateA",
        "DirectSoundEnumerateW",
        "DirectSoundCaptureCreate",
        "DirectSoundCaptureCreate8",
        "waveOutOpen",
        "waveOutClose",
        "waveOutWrite",
        "waveOutGetPosition",
        "waveOutReset",
        "waveOutGetDevCapsA",
        "PlaySoundA",
        "PlaySoundW",
        "sndPlaySoundA",
        "sndPlaySoundW",
        "auxGetVolume",
        "mixerOpen",
        "mciSendStringA",
        "mciSendStringW",
        "BASS_Init",
        "BASS_Free",
        "BASS_ChannelIsActive",
        "BASS_ChannelGetPosition",
        "BASS_ChannelPlay",
        "BASS_ChannelStop",
        "BASS_StreamCreateFile",
        "BASS_MusicLoad",
    ],
    "cursor_focus_window": [
        "GetCursorPos",
        "SetCursorPos",
        "GetCursorInfo",
        "ClipCursor",
        "ShowCursor",
        "SetCursor",
        "LoadCursorA",
        "LoadCursorW",
        "GetForegroundWindow",
        "SetForegroundWindow",
        "GetActiveWindow",
        "SetActiveWindow",
        "GetFocus",
        "SetFocus",
        "GetAsyncKeyState",
        "GetLastInputInfo",
        "GetInputState",
        "GetKeyState",
        "GetKeyboardState",
        "SendInput",
        "mouse_event",
        "keybd_event",
        "GetMessageA",
        "GetMessageW",
        "PeekMessageA",
        "PeekMessageW",
        "DispatchMessageA",
        "DispatchMessageW",
        "TranslateMessage",
        "PostMessageA",
        "PostMessageW",
        "SendMessageA",
        "SendMessageW",
        "SetCapture",
        "ReleaseCapture",
        "ScreenToClient",
        "ClientToScreen",
        "GetClientRect",
        "GetWindowRect",
        "IsWindowVisible",
        "ShowWindow",
        "SetWindowPos",
        "GetSystemMetrics",
        "EnumWindows",
        "FindWindowA",
        "FindWindowW",
        "SetWindowsHookExA",
        "SetWindowsHookExW",
        "UnhookWindowsHookEx",
        "RegisterWindowMessageA",
        "RegisterWindowMessageW",
        "SetTimer",
        "KillTimer",
    ],
    # Not one of the five groups; the loader is scanned because a name that is
    # resolved with GetProcAddress has no import descriptor at all. Without
    # this family, "absent from the import table" would be read as "not called".
    "dynamic_loader": [
        "LoadLibraryA",
        "LoadLibraryW",
        "LoadLibraryExA",
        "LoadLibraryExW",
        "GetProcAddress",
        "FreeLibrary",
        "LdrGetProcedureAddress",
    ],
    "file_log_crt": [
        "CreateFileA",
        "CreateFileW",
        "ReadFile",
        "WriteFile",
        "SetFilePointer",
        "SetFilePointerEx",
        "FlushFileBuffers",
        "GetFileSize",
        "GetFileSizeEx",
        "fopen",
        "_wfopen",
        "_fsopen",
        "fread",
        "fwrite",
        "fflush",
        "fclose",
        "fseek",
        "_fseeki64",
        "ftell",
        "_ftelli64",
        "setvbuf",
        "_iob",
        "__iob_func",
        "fprintf",
        "printf",
        "sprintf",
        "_open",
        "_read",
        "_write",
        "_close",
        "remove",
        "rename",
        "FindFirstFileA",
        "FindFirstFileW",
        "GetCurrentDirectoryA",
        "GetCurrentDirectoryW",
        "SetCurrentDirectoryA",
        "SetCurrentDirectoryW",
        "GetModuleFileNameA",
        "GetModuleFileNameW",
        "CreateDirectoryA",
        "CreateDirectoryW",
        "GetTempPathA",
        "SHGetFolderPathA",
        "SHGetFolderPathW",
        "SHGetSpecialFolderPathA",
        "GetPrivateProfileIntA",
        "GetPrivateProfileStringA",
        "WritePrivateProfileStringA",
        "RegOpenKeyExA",
        "RegOpenKeyExW",
        "RegCreateKeyExA",
        "RegCreateKeyExW",
        "RegQueryValueExA",
        "RegQueryValueExW",
        "RegSetValueExA",
        "RegSetValueExW",
    ],
    "parallel_shared": [
        "CreateMutexA",
        "CreateMutexW",
        "OpenMutexA",
        "OpenMutexW",
        "CreateEventA",
        "CreateEventW",
        "OpenEventA",
        "OpenEventW",
        "CreateSemaphoreA",
        "CreateSemaphoreW",
        "CreateFileMappingA",
        "CreateFileMappingW",
        "OpenFileMappingA",
        "OpenFileMappingW",
        "MapViewOfFile",
        "CreateProcessA",
        "CreateProcessW",
        "ShellExecuteA",
        "ShellExecuteW",
        "WinExec",
        "GetModuleHandleA",
        "GetModuleHandleW",
        "GetCurrentProcessId",
        "GetCurrentThreadId",
        "GetEnvironmentVariableA",
        "GetEnvironmentVariableW",
        "GetComputerNameA",
        "GetUserNameA",
        "GlobalAddAtomA",
        "GlobalFindAtomA",
    ],
}

FAMILY_ORDER = list(WATCHLIST)
API_TO_FAMILY = {api: family for family, apis in WATCHLIST.items() for api in apis}


class PEError(RuntimeError):
    """Raised when the file is not a parseable PE image."""


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _u64(data: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", data, offset)[0]


def _cstring(data: bytes, offset: int, limit: int = 512) -> str:
    if offset < 0 or offset >= len(data):
        raise PEError(f"string offset 0x{offset:x} is outside the file")
    end = data.find(b"\x00", offset, min(len(data), offset + limit))
    if end < 0:
        raise PEError(f"unterminated string at 0x{offset:x}")
    return data[offset:end].decode("latin-1")


class PEFile:
    """The parts of a PE image this scanner needs, and nothing else."""

    def __init__(self, data: bytes, path: str) -> None:
        self.data = data
        self.path = path
        self.sections: list[dict] = []
        self.imports: list[dict] = []
        self.delay_imports: list[dict] = []
        self._parse_headers()
        self._parse_imports()
        self._parse_delay_imports()

    # -- headers ---------------------------------------------------------
    def _parse_headers(self) -> None:
        data = self.data
        if len(data) < 0x40 or data[:2] != b"MZ":
            raise PEError("missing MZ signature")
        pe_offset = _u32(data, 0x3C)
        if pe_offset + 24 > len(data) or data[pe_offset:pe_offset + 4] != b"PE\x00\x00":
            raise PEError("missing PE signature")
        coff = pe_offset + 4
        self.machine = _u16(data, coff)
        self.section_count = _u16(data, coff + 2)
        self.timestamp = _u32(data, coff + 4)
        self.characteristics = _u16(data, coff + 18)
        optional = coff + 20
        magic = _u16(data, optional)
        if magic == 0x10B:
            self.kind = "PE32"
            self.word = 4
            directory_count_offset = optional + 92
            directory_offset = optional + 96
            self.size_of_image = _u32(data, optional + 56)
        elif magic == 0x20B:
            self.kind = "PE32+"
            self.word = 8
            directory_count_offset = optional + 108
            directory_offset = optional + 112
            self.size_of_image = _u32(data, optional + 56)
        else:
            raise PEError(f"unsupported optional header magic 0x{magic:04x}")
        self.image_base = _u64(data, optional + 24) if self.word == 8 else _u32(data, optional + 28)
        self.subsystem = _u16(data, optional + 68)
        self.directory_count = _u32(data, directory_count_offset)
        self.directories = [
            (_u32(data, directory_offset + 8 * index), _u32(data, directory_offset + 8 * index + 4))
            for index in range(min(self.directory_count, 16))
        ]
        section_table = optional + _u16(data, coff + 16)
        for index in range(self.section_count):
            base = section_table + 40 * index
            if base + 40 > len(data):
                raise PEError("section table is truncated")
            raw_name = data[base:base + 8].split(b"\x00", 1)[0]
            self.sections.append({
                "name": raw_name.decode("latin-1"),
                "virtual_size": _u32(data, base + 8),
                "virtual_address": _u32(data, base + 12),
                "raw_size": _u32(data, base + 16),
                "raw_pointer": _u32(data, base + 20),
                "characteristics": _u32(data, base + 36),
            })

    def directory(self, index: int) -> tuple[int, int]:
        if index >= len(self.directories):
            return 0, 0
        return self.directories[index]

    def offset_of_rva(self, rva: int) -> int:
        for section in self.sections:
            extent = max(section["virtual_size"], section["raw_size"])
            if section["virtual_address"] <= rva < section["virtual_address"] + extent:
                delta = rva - section["virtual_address"]
                if delta >= section["raw_size"]:
                    raise PEError(f"rva 0x{rva:x} lies in zero-filled tail of "
                                  f"{section['name']!r}")
                return section["raw_pointer"] + delta
        # Import tables occasionally sit at an offset inside the headers.
        if rva < self.section_table_end():
            return rva
        raise PEError(f"rva 0x{rva:x} is not mapped by any section")

    def section_table_end(self) -> int:
        pe_offset = _u32(self.data, 0x3C)
        coff = pe_offset + 4
        size_of_optional = _u16(self.data, coff + 16)
        return coff + 20 + size_of_optional + 40 * self.section_count

    # -- imports ---------------------------------------------------------
    def _thunk_names(self, first_thunk_rva: int, original_rva: int,
                     bound_va: bool) -> list[dict]:
        """Return ``{"name", "ordinal", "index"}`` items for one thunk array.

        ``index`` is the position in the array, which is what turns the base of
        the IAT into the exact slot a future patch would overwrite.
        """
        data = self.data
        table_rva = original_rva or first_thunk_rva
        size = 8 if self.word == 8 else 4
        mask = (1 << (size * 8 - 1))
        names: list[dict] = []
        for index in range(65536):
            offset = self.offset_of_rva(table_rva + index * size)
            value = _u64(data, offset) if size == 8 else _u32(data, offset)
            if value == 0:
                return names
            if value & mask:
                names.append({"name": None, "ordinal": value & 0xFFFF, "index": index})
                continue
            # When only FirstThunk exists the array may already hold bound VAs.
            name_rva = value
            if not original_rva and bound_va and value > self.image_base:
                name_rva = value - self.image_base
            name_offset = self.offset_of_rva(name_rva)
            names.append({"name": _cstring(data, name_offset + 2), "ordinal": None,
                          "index": index})
        raise PEError("import thunk array is implausibly long")

    def _parse_imports(self) -> None:
        rva, size = self.directory(1)
        if not rva or not size:
            return
        descriptor = self.offset_of_rva(rva)
        for index in range(4096):
            base = descriptor + 20 * index
            entries = struct.unpack_from("<IIIII", self.data, base)
            if entries == (0, 0, 0, 0, 0):
                return
            original, time_date, forwarder, name_rva, first_thunk = entries
            dll = _cstring(self.data, self.offset_of_rva(name_rva))
            size = 8 if self.word == 8 else 4
            for item in self._thunk_names(first_thunk, original, True):
                self.imports.append({
                    "dll": dll,
                    "name": item["name"],
                    "ordinal": item["ordinal"],
                    "iat_va": self.image_base + first_thunk + item["index"] * size,
                    "source": "import",
                    "timestamp": time_date,
                    "forwarder": bool(forwarder) if self.word == 8 else False,
                })
        raise PEError("import descriptor table is implausibly long")

    def _parse_delay_imports(self) -> None:
        if len(self.directories) <= 13:
            return
        rva, size = self.directory(13)
        if not rva or not size:
            return
        descriptor = self.offset_of_rva(rva)
        for index in range(4096):
            base = descriptor + 32 * index
            fields = struct.unpack_from("<IIIIIIII", self.data, base)
            if fields == (0,) * 8:
                return
            attributes, name_field, _, iat_field, int_field, _, _, _ = fields
            as_rva = bool(attributes & 1)

            def to_rva(value: int) -> int:
                if as_rva or value < self.image_base:
                    return value
                return value - self.image_base

            try:
                dll = _cstring(self.data, self.offset_of_rva(to_rva(name_field)))
            except PEError:
                continue
            try:
                names = self._thunk_names(to_rva(iat_field), to_rva(int_field), False)
            except PEError:
                names = []
            size = 8 if self.word == 8 else 4
            for item in names:
                self.delay_imports.append({
                    "dll": dll,
                    "name": item["name"],
                    "ordinal": item["ordinal"],
                    "iat_va": self.image_base + to_rva(iat_field) + item["index"] * size,
                    "source": "delay_import",
                    "attributes": attributes,
                })
        raise PEError("delay-import descriptor table is implausibly long")


def scan_file(path: Path, families: list[str], want_strings: bool) -> dict:
    raw = path.read_bytes()
    report = {
        "path": str(path),
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "parse": "ok",
        "matches": [],
        "string_hits": [],
    }
    try:
        pe = PEFile(raw, str(path))
    except (PEError, struct.error, ValueError) as error:
        report["parse"] = f"error: {error}"
        return report
    report.update({
        "kind": pe.kind,
        "machine": f"0x{pe.machine:04x}",
        "timestamp": f"0x{pe.timestamp:08x}",
        "subsystem": pe.subsystem,
        "image_base": f"0x{pe.image_base:x}",
        "size_of_image": pe.size_of_image,
        "sections": [{"name": section["name"],
                      "virtual_address": f"0x{section['virtual_address']:x}",
                      "virtual_size": section["virtual_size"],
                      "raw_size": section["raw_size"]}
                     for section in pe.sections],
        "imported_dlls": sorted({entry["dll"] for entry in pe.imports + pe.delay_imports}),
        "import_count": len(pe.imports),
        "delay_import_count": len(pe.delay_imports),
    })
    for entry in pe.imports + pe.delay_imports:
        name = entry["name"]
        family = API_TO_FAMILY.get(name or "")
        if family is None or family not in families:
            continue
        report["matches"].append({
            "family": family,
            "api": name,
            "dll": entry["dll"],
            "source": entry["source"],
            "iat_va": f"0x{entry['iat_va']:x}",
        })
    report["matches"].sort(key=lambda item: (item["family"], item["api"].lower(),
                                             item["dll"].lower()))
    if want_strings:
        for family in families:
            for api in WATCHLIST[family]:
                needle = api.encode("ascii") + b"\x00"
                count = raw.count(needle)
                if count:
                    report["string_hits"].append({"family": family, "api": api,
                                                  "occurrences": count})
    return report


def build_summary(files: list[dict], families: list[str]) -> dict:
    found: dict[str, set[str]] = {family: set() for family in families}
    for entry in files:
        for match in entry["matches"]:
            found[match["family"]].add(match["api"])
    return {
        "families": {family: sorted(found[family]) for family in families},
        "absent_from_import_tables": {
            family: sorted(set(WATCHLIST[family]) - found[family]) for family in families
        },
    }


def format_text(report: dict) -> str:
    lines = [f"schema: {report['schema']}",
             f"scanned: {len(report['files'])} file(s)"]
    for entry in report["files"]:
        lines.append("")
        lines.append(f"{entry['path']}")
        lines.append(f"  sha256={entry['sha256']} size={entry['size']} "
                     f"kind={entry.get('kind', '?')} image_base={entry.get('image_base', '?')}")
        if entry["parse"] != "ok":
            lines.append(f"  parse: {entry['parse']}")
            continue
        if entry["matches"]:
            for match in entry["matches"]:
                lines.append(f"  [{match['family']}] {match['api']} <- {match['dll']} "
                             f"({match['source']}, iat={match['iat_va']})")
        else:
            lines.append("  no watchlist API in the static import table")
        if entry["string_hits"]:
            hits = ", ".join(f"{hit['api']}x{hit['occurrences']}"
                             for hit in entry["string_hits"])
            lines.append(f"  raw-string hits: {hits}")
    lines.append("")
    for family, apis in report["summary"]["families"].items():
        lines.append(f"{family}: imported by scan = {', '.join(apis) if apis else '(none)'}")
        absent = report["summary"]["absent_from_import_tables"][family]
        lines.append(f"{family}: not in any import table = "
                     f"{', '.join(absent) if absent else '(none)'}")
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Static PE import-table scan for cross-boundary APIs (N6 / #34).",
        epilog="The scan never loads or executes the target.")
    parser.add_argument("targets", nargs="+", type=Path,
                        help="PE files to read (engine exe, wrapper exe, locked DLLs)")
    parser.add_argument("--families", default=",".join(FAMILY_ORDER),
                        help=f"comma-separated subset of {','.join(FAMILY_ORDER)}")
    parser.add_argument("--strings", action="store_true",
                        help="also search raw bytes for the API names (weaker evidence)")
    parser.add_argument("--json", type=Path, help="write the full report as JSON")
    parser.add_argument("--text", type=Path, help="write the human-readable report")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    families = [item for item in arguments.families.split(",") if item]
    unknown = [item for item in families if item not in WATCHLIST]
    if unknown:
        print(f"unknown family/families: {', '.join(unknown)}", file=sys.stderr)
        return 2
    files = []
    status = 0
    for target in arguments.targets:
        if not target.is_file():
            print(f"not a file: {target}", file=sys.stderr)
            status = 1
            continue
        entry = scan_file(target, families, arguments.strings)
        if entry["parse"] != "ok":
            status = max(status, 1)
        files.append(entry)
    report = {
        "schema": SCHEMA,
        "tool": "tools/import_scan.py",
        "method": "static import table + optional raw string search; never loads the image",
        "families": {family: WATCHLIST[family] for family in families},
        "files": files,
        "summary": build_summary(files, families),
    }
    text = format_text(report)
    if arguments.json:
        arguments.json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")
    if arguments.text:
        arguments.text.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
