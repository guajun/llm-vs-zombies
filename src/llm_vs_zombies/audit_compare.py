"""Strict readers and first-field diagnostics for native lvz.audit.v1 evidence.

Matching this deliberately incomplete state schema is not a determinism proof.
"""
from __future__ import annotations

import copy
import ctypes
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

SCHEMA = "lvz.audit.v1"
_FNV_SOURCE = r"""
#include <stddef.h>
#include <stdint.h>
#if defined(_WIN32)
__declspec(dllexport)
#endif
uint64_t lvz_fnv1a(const unsigned char* bytes, size_t length) {
    uint64_t hash = UINT64_C(14695981039346656037);
    for (size_t i = 0; i < length; ++i) {
        hash ^= bytes[i];
        hash *= UINT64_C(1099511628211);
    }
    return hash;
}
"""
_hash_lock = threading.Lock()
_hash_initialized = False
_hash_native = None


def _python_fnv(encoded: bytes) -> int:
    result = 14695981039346656037
    for byte in encoded:
        result = ((result ^ byte) * 1099511628211) & ((1 << 64) - 1)
    return result


def _native_hash():
    """Optional tiny local C accelerator; never loads into the game process.

    Stdlib-only installations retain the exact Python implementation. Project
    LLVM-MinGW or a native cc/clang/gcc can compile the reviewed source above.
    """
    global _hash_initialized, _hash_native
    with _hash_lock:
        if _hash_initialized:
            return _hash_native
        _hash_initialized = True
        if os.environ.get("LVZ_PYTHON_FNV") == "1":
            return None
        root = Path(__file__).resolve().parents[2]
        compiler = None
        if os.name == "nt":
            target = "i686" if ctypes.sizeof(ctypes.c_void_p) == 4 else (
                "aarch64" if platform.machine().lower() in {"arm64", "aarch64"} else "x86_64")
            compiler = next(iter(sorted(root.glob(f"third_party/llvm-mingw-*/bin/{target}-w64-mingw32-clang.exe"))), None)
        compiler = compiler or shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
        if not compiler:
            return None
        identity = hashlib.sha256((_FNV_SOURCE + platform.machine() + str(ctypes.sizeof(ctypes.c_void_p))).encode()).hexdigest()[:20]
        directory = root / "work" / "audit-hash" / identity
        try:
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError:
                directory = Path(tempfile.mkdtemp(prefix="lvz-audit-hash-"))
            library = directory / ("fnv.dll" if os.name == "nt" else "fnv.so")
            if not library.exists():
                source = directory / "fnv.c"
                source.write_text(_FNV_SOURCE, encoding="ascii")
                output = directory / (uuid.uuid4().hex + library.suffix)
                command = [str(compiler), "-O3", "-std=c11", "-shared", str(source), "-o", str(output)]
                command += ["-static"] if os.name == "nt" else ["-fPIC"]
                result = subprocess.run(command, capture_output=True, timeout=45,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                if result.returncode:
                    return None
                if not library.exists():
                    os.replace(output, library)
                elif output.exists():
                    output.unlink()
            module = ctypes.CDLL(str(library.resolve()))
            function = module.lvz_fnv1a
            function.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
            function.restype = ctypes.c_uint64
            for fixture in (b"", b"hello", bytes(range(256)), b"\x00\xff\x00"):
                if function(fixture, len(fixture)) != _python_fnv(fixture):
                    return None
            _hash_native = function  # ctypes retains its owning loaded library.
        except (OSError, AttributeError, subprocess.SubprocessError):
            return None
        return _hash_native


def hash_backend() -> str:
    return "native_c_fnv1a" if _native_hash() is not None else "python_fnv1a"


def _fnv(encoded: bytes) -> str:
    function = _hash_native if _hash_initialized else _native_hash()
    value = function(encoded, len(encoded)) if function is not None else _python_fnv(encoded)
    return f"{value:016x}"


class EvidenceError(ValueError):
    pass


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def decode(data: str | bytes) -> Any:
    try:
        return json.loads(data, object_pairs_hook=_unique,
                          parse_constant=lambda value: (_ for _ in ()).throw(EvidenceError(value)))
    except (ValueError, UnicodeError) as error:
        raise EvidenceError(f"invalid JSON: {error}") from error


def read_json(path: Path) -> Any:
    return decode(path.read_bytes())


def jsonl(path: Path) -> Iterator[dict]:
    with path.open("rb") as stream:
        for number, line in enumerate(stream, 1):
            if not line.endswith(b"\n"):
                raise EvidenceError(f"{path.name}:{number}: incomplete JSONL tail")
            value = decode(line)
            if not isinstance(value, dict):
                raise EvidenceError(f"{path.name}:{number}: object required")
            yield value


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _check_state_values(item):
    # Native captures use integer bits, not JSON floating-point values. Reject
    # floats rather than assuming Python and nlohmann serialize them identically.
    if isinstance(item, float):
        raise EvidenceError("audit state must encode floats as integer bits")
    if isinstance(item, dict):
        for child in item.values():
            _check_state_values(child)
    elif isinstance(item, list):
        for child in item:
            _check_state_values(child)


def digest(value: Any) -> str:
    _check_state_values(value)
    return _fnv(canonical(value))


def digests(state: dict) -> dict[str, str]:
    if not isinstance(state, dict):
        raise EvidenceError("audit state must be an object")
    _check_state_values(state)
    return _state_digests(state, {}, set(state))


def _state_digests(state: dict, cache: dict, changed: set[str], *, with_encoded=False):
    """Reuse unchanged component bytes; still verify every stored digest."""
    parts, result = [], {}
    for key in sorted(state):
        if key in changed or key not in cache:
            encoded = canonical(state[key])
            cache[key] = (canonical(key) + b":" + encoded, _fnv(encoded))
        part, result[key] = cache[key]
        parts.append(part)
    for key in list(cache):
        if key not in state:
            del cache[key]
    encoded_state = b"{" + b",".join(parts) + b"}"
    result["all"] = _fnv(encoded_state)
    return (result, encoded_state) if with_encoded else result


def version(value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != {"epoch", "tick", "revision"}:
        raise EvidenceError("invalid version fields")
    if any(type(item) is not int or item < 0 for item in value.values()):
        raise EvidenceError("invalid version values")
    return value


def _tokens(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise EvidenceError("invalid JSON Pointer")
    result = []
    for part in pointer[1:].split("/"):
        index = 0
        while index < len(part):
            if part[index] == "~":
                if index + 1 >= len(part) or part[index + 1] not in "01":
                    raise EvidenceError("invalid JSON Pointer escape")
                index += 1
            index += 1
        result.append(part.replace("~1", "/").replace("~0", "~"))
    return result


def _index(container, token: str, *, add=False):
    if isinstance(container, dict):
        return token
    if not isinstance(container, list):
        raise EvidenceError("JSON Pointer traverses a scalar")
    if add and token == "-":
        return len(container)
    if not token.isascii() or not token.isdecimal() or (len(token) > 1 and token[0] == "0"):
        raise EvidenceError("invalid JSON Pointer array index")
    index = int(token)
    if index >= len(container) + int(add):
        raise EvidenceError("JSON Pointer array index out of bounds")
    return index


def patch(document: Any, operations: list[dict], *, in_place: bool = False) -> Any:
    """Apply RFC 6902 add/remove/replace emitted by nlohmann::json::diff.

    Other operations are rejected explicitly; this is a native-format reader,
    not a permissive general JSON Patch implementation.
    """
    if not isinstance(operations, list):
        raise EvidenceError("patch must be an array")
    result = document if in_place else copy.deepcopy(document)
    for operation in operations:
        if not isinstance(operation, dict) or operation.get("op") not in {"add", "remove", "replace"}:
            raise EvidenceError("unsupported JSON Patch operation")
        op = operation["op"]
        if set(operation) != ({"op", "path"} if op == "remove" else {"op", "path", "value"}):
            raise EvidenceError("invalid JSON Patch fields")
        tokens = _tokens(operation["path"])
        if not tokens:
            if op == "remove":
                raise EvidenceError("native state root cannot be removed")
            result = copy.deepcopy(operation["value"])
            continue
        parent = result
        try:
            for token in tokens[:-1]:
                parent = parent[_index(parent, token)]
            key = _index(parent, tokens[-1], add=op == "add")
            if op == "add":
                if isinstance(parent, list):
                    parent.insert(key, copy.deepcopy(operation["value"]))
                else:
                    parent[key] = copy.deepcopy(operation["value"])
            elif op == "remove":
                del parent[key]
            else:
                parent[key]  # Replacement requires an existing target.
                parent[key] = copy.deepcopy(operation["value"])
        except (IndexError, KeyError, TypeError) as error:
            raise EvidenceError(f"invalid patch target: {operation['path']}") from error
    return result


def first_difference(expected: Any, actual: Any, path: str = "") -> dict | None:
    if type(expected) is not type(actual):
        return {"path": path, "expected": expected, "actual": actual, "reason": "type"}
    if isinstance(expected, dict):
        for key in sorted(set(expected) | set(actual)):
            child = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in expected or key not in actual:
                return {"path": child, "reason": "missing_field",
                        "expected_present": key in expected, "actual_present": key in actual,
                        "expected": expected.get(key), "actual": actual.get(key)}
            difference = first_difference(expected[key], actual[key], child)
            if difference:
                return difference
    elif isinstance(expected, list):
        for index, (left, right) in enumerate(zip(expected, actual)):
            difference = first_difference(left, right, f"{path}/{index}")
            if difference:
                return difference
        if len(expected) != len(actual):
            return {"path": path, "reason": "array_length", "expected": len(expected), "actual": len(actual)}
    elif expected != actual:
        return {"path": path, "expected": expected, "actual": actual, "reason": "value"}
    return None


@dataclass(frozen=True)
class AuditFrame:
    seq: int
    kind: str
    version: dict
    payload: dict
    state: dict
    digests: dict
    canonical_state: bytes | None = None


class _FrameDecoder:
    def __init__(self, *, reuse_state=False):
        self.state = None
        self.previous = None
        self.cache = {}
        self.reuse_state = reuse_state

    def accept(self, hashes, delta):
        AuditLog._envelope(hashes)
        AuditLog._envelope(delta)
        for key in ("schema", "seq", "kind", "version", "payload"):
            if hashes[key] != delta[key]:
                raise EvidenceError(f"checksum/delta envelope mismatch: {key}")
        if hashes["kind"] not in {"pre_step", "post_step"}:
            raise EvidenceError("unexpected audit frame kind")
        previous = self.previous
        if previous and hashes["seq"] <= previous.seq:
            raise EvidenceError("audit frame sequence is not increasing")
        if self.state is None:
            if "initial" not in delta or "patch" in delta or not isinstance(delta["initial"], dict):
                raise EvidenceError("first audit frame requires initial state")
            self.state = delta["initial"]
            _check_state_values(self.state)
            changed = set(self.state)
        else:
            if "initial" in delta or not isinstance(delta.get("patch"), list):
                raise EvidenceError("subsequent audit frame requires patch")
            changed, root_changed = set(), False
            for operation in delta["patch"]:
                if not isinstance(operation, dict):
                    raise EvidenceError("invalid JSON Patch operation")
                tokens = _tokens(operation.get("path"))
                if tokens:
                    changed.add(tokens[0])
                else:
                    root_changed = True
                if "value" in operation:
                    _check_state_values(operation["value"])
            self.state = patch(self.state, delta["patch"], in_place=self.reuse_state)
            if not isinstance(self.state, dict):
                raise EvidenceError("audit state root must remain an object")
            if root_changed:
                changed = set(self.state)
        computed, encoded = _state_digests(self.state, self.cache, changed, with_encoded=True)
        if self.state.get("schema") != SCHEMA or hashes.get("digests") != computed:
            raise EvidenceError("state digest/schema mismatch after JSON Patch reconstruction")
        frame = AuditFrame(hashes["seq"], hashes["kind"], hashes["version"],
                           hashes["payload"], self.state, hashes["digests"], encoded)
        if frame.kind == "pre_step":
            if previous and previous.kind != "post_step":
                raise EvidenceError("missing post_step")
        else:
            if previous is None or previous.kind != "pre_step":
                raise EvidenceError("missing pre_step")
            before, after = previous.version, frame.version
            if (after["epoch"] != before["epoch"] or after["tick"] != before["tick"] + 1
                    or after["revision"] != 0 or frame.payload.get("native_tick_delta") != 1
                    or frame.payload.get("request_id") != previous.payload.get("request_id")):
                raise EvidenceError("invalid actual one-tick audit boundary")
        self.previous = frame
        return frame

    def finish(self):
        if self.previous and self.previous.kind != "post_step":
            raise EvidenceError("audit ends in an unfinished step")


class FrameSelection:
    """Reconstruct lazily so a full match does not retain every full state."""
    def __init__(self, owner, indices=None):
        self.owner = owner
        self.indices = range(len(owner._frame_headers)) if indices is None else indices

    def __len__(self):
        return len(self.indices)

    def __iter__(self):
        wanted = iter(self.indices)
        current = next(wanted, None)
        if current is None:
            return
        for index, frame in enumerate(self.owner._frames()):
            if index == current:
                yield frame
                current = next(wanted, None)
                if current is None:
                    return

    def __getitem__(self, index):
        if isinstance(index, slice):
            return FrameSelection(self.owner, self.indices[index])
        wanted = self.indices[index]
        return next(iter(FrameSelection(self.owner, [wanted])))


class AuditLog:
    def __init__(self, directory: str | Path, *, require_closed: bool = False):
        self.directory = Path(directory)
        self.manifest = read_json(self.directory / "manifest.json")
        if self.manifest.get("schema") != SCHEMA or not isinstance(self.manifest.get("target"), str):
            raise EvidenceError("invalid native audit manifest")
        if self.manifest.get("loaded_signatures_match") is not True:
            raise EvidenceError("native target signatures did not match")
        self.events = list(jsonl(self.directory / "events.jsonl"))
        self._headers = [AuditFrame(frame.seq, frame.kind, frame.version, frame.payload, {}, frame.digests)
                         for frame in self._frames(reuse_state=True)]
        self._frame_headers = [(frame.seq, frame.payload.get("request_id")) for frame in self._headers]
        self._requests, self._request_frames, self._frame_indices = {}, {}, {}
        for index, frame in enumerate(self._headers):
            rid = frame.payload.get("request_id")
            self._request_frames.setdefault(rid, []).append(frame)
            self._frame_indices.setdefault(rid, []).append(index)
        self.frames = FrameSelection(self)
        primary = [seq for seq, _ in self._frame_headers]
        last_event = -1
        for event in self.events:
            self._envelope(event)
            if event["seq"] < last_event:
                raise EvidenceError("audit event sequence moved backwards")
            last_event = event["seq"]
            self._requests.setdefault(event["payload"].get("request_id"), []).append(event)
            # Birth observations share the step sequence; they are annotations.
            if event["kind"] != "zombie_first_boundary_observed":
                primary.append(event["seq"])
        if sorted(primary) != list(range(len(primary))):
            raise EvidenceError("native audit sequence is missing or duplicated")
        if require_closed and (not self.events or self.events[-1]["kind"] != "recording_closed"):
            raise EvidenceError("native recording lacks a completed recording_closed boundary")

    @staticmethod
    def _envelope(record):
        if record.get("schema") != SCHEMA or type(record.get("seq")) is not int or record["seq"] < 0:
            raise EvidenceError("invalid native audit schema/sequence")
        version(record.get("version"))
        if not isinstance(record.get("payload"), dict) or not isinstance(record.get("kind"), str):
            raise EvidenceError("invalid native audit envelope")

    def _frames(self, *, reuse_state=False):
        from itertools import zip_longest
        decoder = _FrameDecoder(reuse_state=reuse_state)
        for hashes, delta in zip_longest(jsonl(self.directory / "checksums.jsonl"),
                                         jsonl(self.directory / "state-deltas.jsonl")):
            if hashes is None or delta is None:
                raise EvidenceError("checksum/delta record count mismatch")
            yield decoder.accept(hashes, delta)
        decoder.finish()

    def request_frames(self, request_id: str) -> FrameSelection:
        return FrameSelection(self, self._frame_indices.get(request_id, []))

    def request_headers(self, request_id: str) -> list[AuditFrame]:
        """Metadata only; useful for ordering checks without state reconstruction."""
        return self._request_frames.get(request_id, [])

    def request_events(self, request_id: str) -> list[dict]:
        return self._requests.get(request_id, [])


class AuditTail:
    """Incrementally verify a runtime's append-only audit after each completion.

    Frames are ephemeral: consume/compare state before requesting the next one.
    Only one state and per-frame metadata are retained. No previous prefix is
    reparsed, re-patched or re-hashed as a recording grows.
    """
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.manifest = read_json(self.directory / "manifest.json")
        if (self.manifest.get("schema") != SCHEMA or not isinstance(self.manifest.get("target"), str)
                or self.manifest.get("loaded_signatures_match") is not True):
            raise EvidenceError("invalid live audit target manifest")
        self.events, self._headers = [], []
        self._positions = {name: 0 for name in ("events.jsonl", "checksums.jsonl", "state-deltas.jsonl")}
        self._decoder = _FrameDecoder(reuse_state=True)
        self._next_seq, self._last_event_seq = 0, -1
        self._identities = {}
        self._requests, self._request_frames = {}, {}

    def _new_records(self, name):
        path = self.directory / name
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino)
        if name in self._identities and self._identities[name] != identity:
            raise EvidenceError("live audit file was replaced")
        self._identities[name] = identity
        if stat.st_size < self._positions[name]:
            raise EvidenceError("live audit file was truncated")
        with path.open("rb") as stream:
            stream.seek(self._positions[name])
            while line := stream.readline():
                if not line.endswith(b"\n"):
                    raise EvidenceError("live audit contains an incomplete flushed record")
                value = decode(line)
                if not isinstance(value, dict):
                    raise EvidenceError("live audit record must be an object")
                self._positions[name] = stream.tell()
                yield value

    def read_request(self, request_id: str):
        from itertools import zip_longest
        primary = []
        for event in self._new_records("events.jsonl"):
            AuditLog._envelope(event)
            if event["seq"] < self._last_event_seq:
                raise EvidenceError("live audit event sequence moved backwards")
            self._last_event_seq = event["seq"]
            self.events.append(event)
            self._requests.setdefault(event["payload"].get("request_id"), []).append(event)
            if event["kind"] != "zombie_first_boundary_observed":
                primary.append(event["seq"])
        for hashes, delta in zip_longest(self._new_records("checksums.jsonl"), self._new_records("state-deltas.jsonl")):
            if hashes is None or delta is None:
                raise EvidenceError("live checksum/delta record count mismatch")
            frame = self._decoder.accept(hashes, delta)
            if frame.payload.get("request_id") != request_id:
                raise EvidenceError("unexpected intervening request in live frame audit")
            header = AuditFrame(frame.seq, frame.kind, frame.version, frame.payload, {}, frame.digests)
            self._headers.append(header)
            self._request_frames.setdefault(request_id, []).append(header)
            primary.append(frame.seq)
            yield frame
        self._decoder.finish()
        if sorted(primary) != list(range(self._next_seq, self._next_seq + len(primary))):
            raise EvidenceError("live audit sequence is missing or duplicated")
        self._next_seq += len(primary)

    def request_headers(self, request_id: str) -> list[AuditFrame]:
        return self._request_frames.get(request_id, [])

    def request_events(self, request_id: str) -> list[dict]:
        return self._requests.get(request_id, [])


def compare_audits(expected: AuditLog, actual: AuditLog, *,
                   request_map: dict[str, str] | None = None) -> dict:
    if expected.manifest != actual.manifest:
        return {"equal": False, "reason": "audit_manifest", "difference": first_difference(expected.manifest, actual.manifest)}
    left, right = expected.frames, actual.frames
    if request_map is not None:
        left = FrameSelection(expected, [i for i, (_, rid) in enumerate(expected._frame_headers) if rid in request_map])
        wanted = set(request_map.values())
        right = FrameSelection(actual, [i for i, (_, rid) in enumerate(actual._frame_headers) if rid in wanted])
    for index, (a, b) in enumerate(zip(left, right)):
        if a.kind != b.kind or a.version["tick"] != b.version["tick"]:
            return {"equal": False, "reason": "boundary", "index": index}
        if request_map and request_map[a.payload["request_id"]] != b.payload["request_id"]:
            return {"equal": False, "reason": "request_order", "index": index}
        difference = first_difference(a.state, b.state) if a.canonical_state != b.canonical_state else None
        if difference:
            return {"equal": False, "reason": "state", "index": index, "tick": a.version["tick"],
                    "phase": a.kind, "expected_seq": a.seq, "actual_seq": b.seq, "difference": difference}
    if len(left) != len(right):
        return {"equal": False, "reason": "frame_count", "expected": len(left), "actual": len(right)}
    return {"equal": True, "frames": len(left), "scope": "captured audit fields only",
            "original_engine_replay_verified": False}
