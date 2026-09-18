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
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
import struct
from collections import deque
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

SCHEMA = "lvz.audit.v1"
RAW_ANIMATIONS = "reanimation-handles.jsonl"
PARTICLE_SHAKE_RAW = "particle-shake-seeds.jsonl"
PARTICLE_SHAKE_MODE = "deterministic_particle_shake_v1"
DRAW_SCHEDULE_MODE = "deterministic_draw_schedule_v1"
AUDIT_FILES = ("manifest.json", "events.jsonl", "checksums.jsonl", "state-deltas.jsonl")
_POINTER_CACHE_SIZE = 16384
_POINTER_CACHE_MAX_CHARS = 512
_OWNER_RETIREMENT_RULES = ["owner_dead", "plant_squished_remove_effects"]
_PARTICLE_BOUNDARY_LIMIT = 8192
_SPAWN_BOUNDARY_LIMIT = 1024
_JSONL_MAX_RECORD_BYTES = 32 << 20
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
#if defined(_WIN32)
__declspec(dllexport)
#endif
uint64_t lvz_fnv1a_continue(uint64_t hash, const unsigned char* bytes, size_t length) {
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
_hash_continue = None


def _python_fnv(encoded: bytes, result: int = 14695981039346656037) -> int:
    for byte in encoded:
        result = ((result ^ byte) * 1099511628211) & ((1 << 64) - 1)
    return result


def _native_hash():
    """Optional tiny local C accelerator; never loads into the game process.

    Stdlib-only installations retain the exact Python implementation. Project
    LLVM-MinGW or a native cc/clang/gcc can compile the reviewed source above.
    """
    global _hash_initialized, _hash_native, _hash_continue
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
            continued = module.lvz_fnv1a_continue
            continued.argtypes = [ctypes.c_uint64, ctypes.c_char_p, ctypes.c_size_t]
            continued.restype = ctypes.c_uint64
            for fixture in (b"", b"hello", bytes(range(256)), b"\x00\xff\x00"):
                if function(fixture, len(fixture)) != _python_fnv(fixture):
                    return None
                for seed in (0, 1, 0xffffffffffffffff, 14695981039346656037):
                    if continued(seed, fixture, len(fixture)) != _python_fnv(fixture, seed):
                        return None
            _hash_native = function  # ctypes retains its owning loaded library.
            _hash_continue = continued
        except (OSError, AttributeError, subprocess.SubprocessError):
            return None
        return _hash_native


def hash_backend() -> str:
    return "native_c_fnv1a" if _native_hash() is not None else "python_fnv1a"


def _fnv(encoded: bytes) -> str:
    function = _hash_native if _hash_initialized else _native_hash()
    value = function(encoded, len(encoded)) if function is not None else _python_fnv(encoded)
    return f"{value:016x}"


def _fnv_continue(seed: int, encoded: bytes) -> int:
    if not _hash_initialized:
        _native_hash()
    return _hash_continue(seed, encoded, len(encoded)) if _hash_continue is not None else _python_fnv(encoded, seed)


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


class EventStream:
    """Repeatable, hash-bound JSONL view; no event payloads remain resident.

    Each read checks inode/size/timestamps and all bytes against the original
    SHA-256. Closing an early iterator also verifies the unread suffix.
    """
    def __init__(self, path: Path):
        self.path = path
        self.signature = self._signature()
        self.sha256 = file_hash(path)
        self._check_signature()

    def _signature(self):
        stat = self.path.stat()
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

    def _check_signature(self):
        if self._signature() != self.signature:
            raise EvidenceError(f"closed audit file was changed/replaced/truncated: {self.path.name}")

    def verify(self):
        self._check_signature()
        if file_hash(self.path) != self.sha256:
            raise EvidenceError(f"closed audit file SHA-256 changed: {self.path.name}")
        self._check_signature()

    def __iter__(self):
        self._check_signature()
        digest, completed = hashlib.sha256(), False
        try:
            with self.path.open("rb") as stream:
                number = 0
                while line := stream.readline(_JSONL_MAX_RECORD_BYTES + 1):
                    number += 1
                    if len(line) > _JSONL_MAX_RECORD_BYTES:
                        raise EvidenceError("audit JSONL record exceeds reader bound 32 MiB")
                    digest.update(line)
                    if not line.endswith(b"\n"):
                        raise EvidenceError(f"{self.path.name}:{number}: incomplete JSONL tail")
                    value = decode(line)
                    if not isinstance(value, dict):
                        raise EvidenceError(f"{self.path.name}:{number}: object required")
                    yield value
            completed = True
            if digest.hexdigest() != self.sha256:
                raise EvidenceError(f"closed audit file SHA-256 changed: {self.path.name}")
        finally:
            self._check_signature()
            if not completed and file_hash(self.path) != self.sha256:
                raise EvidenceError(f"closed audit file SHA-256 changed: {self.path.name}")


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def draw_mode(manifest):
    spec = manifest.get("draw_schedule")
    if spec is None:
        return None
    required = {"mode": DRAW_SCHEDULE_MODE, "installed": True,
                "original_engine_bitwise_unmodified": False, "target_entry_rva": 0x138eb0,
                "autonomous_fight_draws": False, "capture": "cached_bgr24_only", "rng_restore_after_draw": False,
                "schedule": "one warm draw before B0; one original draw after each verified update before post_step"}
    if (not isinstance(spec, dict) or any(type(spec.get(key)) is not type(value) or spec[key] != value
                                          for key, value in required.items())
            or type(spec.get("live_verified")) is not bool):
        raise EvidenceError("unsupported or undeclared controlled draw semantics")
    return DRAW_SCHEDULE_MODE


def audit_files(directory: Path, manifest: dict) -> tuple[str, ...]:
    """Resolve supported evidence files without trusting a manifest path."""
    draw_mode(manifest)
    coverage = manifest.get("coverage", {})
    if not isinstance(coverage, dict):
        raise EvidenceError("invalid native audit coverage")
    animations = coverage.get("reanimations", {})
    if not isinstance(animations, dict):
        raise EvidenceError("invalid animation coverage")
    if "owner_retirement_rules" in animations and animations["owner_retirement_rules"] != _OWNER_RETIREMENT_RULES:
        raise EvidenceError("unsupported animation owner retirement rules")
    descriptor = animations.get("raw_handle_evidence")
    required = coverage.get("animation_normalization") is True or manifest.get("animation_normalization") is True
    if descriptor is not None:
        if (not isinstance(descriptor, dict) or descriptor.get("path") != "audit/" + RAW_ANIMATIONS
                or descriptor.get("encoding") != "initial_plus_json_patch"
                or descriptor.get("binding") != ["seq", "kind", "version"]
                or type(descriptor.get("required")) is not bool):
            raise EvidenceError("unsupported raw animation evidence descriptor")
        required |= descriptor["required"]
    present = (directory / RAW_ANIMATIONS).is_file()
    if required and not present:
        raise EvidenceError("required raw animation evidence is missing")
    result = AUDIT_FILES + ((RAW_ANIMATIONS,) if present else ())
    particle = manifest.get("particle_shake")
    particle_file = (directory / PARTICLE_SHAKE_RAW).is_file()
    if particle is not None:
        if (not isinstance(particle, dict) or particle.get("mode") != PARTICLE_SHAKE_MODE
                or particle.get("installed") is not True or particle.get("original_engine_bitwise_unmodified") is not False
                or particle.get("raw_evidence") != PARTICLE_SHAKE_RAW or not isinstance(particle.get("semantic_change"), str)):
            raise EvidenceError("unsupported or undeclared particle shake semantics")
        if not particle_file:
            raise EvidenceError("required raw particle shake evidence is missing")
        result += (PARTICLE_SHAKE_RAW,)
    elif particle_file:
        raise EvidenceError("raw particle shake evidence lacks an explicit engine mode")
    return result


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


def _parse_tokens(pointer: str) -> tuple[str, ...]:
    if pointer == "":
        return ()
    if not pointer.startswith("/"):
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
    return tuple(result)


@lru_cache(maxsize=_POINTER_CACHE_SIZE)
def _cached_tokens(pointer: str) -> tuple[str, ...]:
    # lru_cache stores successful returns only. Tuples cannot be changed by a
    # caller; the cache never stores a target container or dynamic validity.
    return _parse_tokens(pointer)


def _tokens(pointer: str) -> tuple[str, ...]:
    if not isinstance(pointer, str):
        raise EvidenceError("invalid JSON Pointer")
    # Avoid hashing malformed/unhashable inputs and retaining arbitrarily long
    # valid paths. Long paths still receive the same complete strict parsing.
    if type(pointer) is str and len(pointer) <= _POINTER_CACHE_MAX_CHARS:
        return _cached_tokens(pointer)
    return _parse_tokens(pointer)


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
    raw_animations: dict | None = None
    particle_seeds: tuple[dict, ...] = ()
    spawn_events: tuple[dict, ...] = ()


def spawn_payload_semantics(event, frame):
    """Retain initializer-exit scalars/RNG, mapping only four proven handles.

    The initializer's caller may replace a handle before the next boundary.
    That case cannot be normalized from this evidence and must fail closed.
    Global ordinal includes menu/preview history; controlled order is checked
    separately. The duplicate boundary is checked against event.version.
    """
    payload = copy.deepcopy(event["payload"])
    payload.pop("boundary", None)
    payload.pop("ordinal", None)
    fields = payload.get("initial", {}).get("raw_scalar_fields")
    if not isinstance(fields, dict) or type(payload.get("slot")) is not int:
        raise EvidenceError("spawn lacks exact initializer raw fields/slot")
    raw = frame.raw_animations
    if not isinstance(raw, dict):
        raise EvidenceError("spawn normalization requires checked raw animation evidence")
    by_path = {link["path"]: link for link in raw["links"]}
    for offset in ("00000118", "00000140", "00000144", "00000150"):
        if offset not in fields:
            raise EvidenceError("spawn lacks an audited zombie animation role")
        handle = fields[offset]
        if type(handle) is not int or not 0 <= handle <= 0xffffffff:
            raise EvidenceError("spawn animation handle is not uint32")
        if handle == 0:
            fields[offset] = {"status": "null"}
            continue
        link = by_path.get(f"/zombies/slots/{payload['slot']}/fields/{offset}")
        if link is None or link["raw_handle"] != handle or not link["lookup_matches"]:
            raise EvidenceError("initializer animation handle cannot be proven at the following boundary")
        fields[offset] = copy.deepcopy(link["normalized_reference"])
    return payload


def spawn_semantics(event, frame, *, map_version=lambda value: value):
    """Compare the assigned boundary and complete normalized birth payload."""
    AuditLog._envelope(event)
    if event.get("phase") != "controlled_boundary":
        raise EvidenceError("unassigned initialization history has no replay boundary")
    return {"version": map_version(event["version"]), "phase": event["phase"],
            "native_phase": event["native_phase"], "payload": spawn_payload_semantics(event, frame)}


def particle_semantics(event, *, map_version=lambda value: value):
    """Exclude raw addresses/global ordinal, retaining actual call order."""
    return {"version": map_version(event["version"]), "phase": event["phase"],
            "native_phase": event["native_phase"],
            "payload": {key: value for key, value in event["payload"].items() if key != "ordinal"}}


def render_semantics(receipt, *, map_version=lambda value: value):
    if receipt is None:
        return None
    result = copy.deepcopy(receipt)
    result["frame_version"] = map_version(result["frame_version"])
    return result


def _state_clocks(state):
    try:
        return {"schema": SCHEMA, "target": state["rng"]["target"],
                "game_clock": state["board"]["00005568"], "effect_clock": state["board"]["0000556c"],
                "mj_clock": state["app"]["mj_clock"]}
    except (KeyError, TypeError) as error:
        raise EvidenceError("controlled draw state lacks audited clocks") from error


def _seeded_rng(seed):
    words = [seed or 4357]
    for index in range(1, 624):
        previous = words[-1]
        words.append((1812433253 * (previous ^ (previous >> 30)) + index) & 0xffffffff)
    return {"global_mt": {"algorithm": "sexy_mt19937_31", "words": words, "cursor": 624},
            "game_thread_crt": {"algorithm": "msvc_lcg_15", "state": seed}}


class _DrawEvidence:
    """One warm operation, one checked receipt per post, bounded terminal tail."""
    def __init__(self, manifest):
        self.target = manifest["target"]
        self.preparing = self.warm = self.terminal = self.health = None
        self.warm_frames = self.step_frames = self.posts = self.terminal_skips = 0
        self.terminal_completed = False
        self.seen_frame = False

    def counts(self):
        return {"mode": DRAW_SCHEDULE_MODE, "warm_frames": self.warm_frames, "step_frames": self.step_frames}

    def _receipt(self, receipt, boundary, phase):
        if (not isinstance(receipt, dict) or receipt.get("schema") != "lvz.controlled-render.v1"
                or receipt.get("mode") != DRAW_SCHEDULE_MODE or receipt.get("phase") != phase
                or version(receipt.get("frame_version")) != boundary
                or type(receipt.get("native_clock")) is not int or receipt["native_clock"] < 0):
            raise EvidenceError("controlled draw receipt identity/phase/version is invalid")
        if phase == "terminal":
            expected = {"schema", "mode", "phase", "frame_version", "native_clock", "skipped", "reason", "cache_invalidated"}
            if (set(receipt) != expected or receipt.get("skipped") is not True
                    or receipt.get("reason") != "left_ready_fight" or receipt.get("cache_invalidated") is not True):
                raise EvidenceError("terminal draw skip is not explicit or invents draw evidence")
            return
        if (any(key in receipt for key in ("skipped", "reason", "cache_invalidated"))
                or type(receipt.get("width")) is not int or receipt["width"] != 800
                or type(receipt.get("height")) is not int or receipt["height"] != 600
                or receipt.get("pixel_format") != "bgr24" or receipt.get("rng_restored") is not False
                or type(receipt.get("rng_unchanged")) is not bool):
            raise EvidenceError("controlled draw receipt format/guard is invalid")
        clocks = receipt.get("clocks_before")
        if (not isinstance(clocks, dict) or set(clocks) != {"schema", "target", "game_clock", "effect_clock", "mj_clock"}
                or clocks["schema"] != SCHEMA or clocks["target"] != self.target
                or any(type(clocks[key]) is not int or clocks[key] < 0 for key in ("game_clock", "effect_clock", "mj_clock"))
                or clocks != receipt.get("clocks_after") or clocks["game_clock"] != receipt["native_clock"]):
            raise EvidenceError("controlled draw changed or omitted clock evidence")
        for name in ("rng_before", "rng_after"):
            hashes = receipt.get(name)
            if (not isinstance(hashes, dict) or set(hashes) != {"all", "global_mt", "game_thread_crt"}
                    or any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{16}", value) is None for value in hashes.values())):
                raise EvidenceError("controlled draw lacks complete RNG digests")
        if receipt["rng_unchanged"] != (receipt["rng_before"] == receipt["rng_after"]):
            raise EvidenceError("controlled draw RNG guard disagrees with digests")
        if first_difference(receipt.get("counts"), self.counts()):
            raise EvidenceError("controlled draw receipt count is missing, duplicated, or reordered")

    def event(self, event):
        kind, payload = event["kind"], event["payload"]
        auxiliary = (self.terminal is not None and kind == "zombie_first_boundary_observed"
                     and not self.terminal.get("transition_seen") and event["seq"] >= self.terminal["seq"]
                     and event["version"] == self.terminal["version"] and payload.get("exact_spawn") is False)
        if self.preparing is not None and self.warm is None and kind != "render_prepared":
            if kind not in {"zombie_initialized", "particle_shake_seed"} or event.get("phase") != "initialization":
                raise EvidenceError("warm drawing contains an unexpected controlled mutation")
        if self.terminal_completed and kind not in {"recording_closed", "draw_schedule_closed", "particle_shake_closed", "spawn_hook_closed"}:
            raise EvidenceError("simulation event follows terminal completion")
        if kind == "render_preparing":
            if (self.preparing is not None or self.warm is not None or self.seen_frame
                    or event["version"]["tick"] != 0 or not isinstance(payload.get("request_id"), str)):
                raise EvidenceError("warm drawing must occur exactly once before the first audited step")
            self.preparing = event
        elif kind == "render_prepared":
            if (self.preparing is None or self.warm is not None or self.seen_frame
                    or event["version"] != self.preparing["version"]
                    or payload.get("request_id") != self.preparing["payload"]["request_id"]):
                raise EvidenceError("warm drawing receipt has no matching preparation")
            self.warm_frames = 1
            receipt = payload.get("render")
            self._receipt(receipt, event["version"], "warm")
            seed = receipt.get("seed_readback", {}).get("seed")
            if type(seed) is not int or not 0 <= seed <= 0xffffffff:
                raise EvidenceError("warm drawing lacks an explicit uint32 seed")
            expected = {"seed": seed, "global_mt_words": 624, "global_mt_cursor": 624,
                        "game_thread_crt": seed, "verified_before_draw": True}
            if first_difference(expected, receipt["seed_readback"]) or receipt["rng_before"] != digests(_seeded_rng(seed)):
                raise EvidenceError("warm drawing seed readback does not match the captured RNG digests")
            self.warm = receipt
        elif kind == "terminal_transition":
            if (self.terminal is None or self.terminal.get("transition_seen")
                    or event["version"] != self.terminal["version"]
                    or payload.get("request_id") != self.terminal["request_id"]
                    or payload.get("tick_delta_verified") is not True or payload.get("board_identity_preserved") is not True
                    or type(payload.get("native_tick_delta")) is not int or payload["native_tick_delta"] != 1):
                raise EvidenceError("terminal draw skip lacks a verified same-Board transition")
            self.terminal["transition_seen"] = True
        elif self.terminal is not None and kind == "request_completed":
            result = payload.get("result", {})
            observation = result.get("observation", {})
            if (self.terminal_completed or not self.terminal.get("transition_seen")
                    or payload.get("request_id") != self.terminal["request_id"]
                    or event["version"] != self.terminal["version"] or observation.get("version") != event["version"]
                    or result.get("stop_reason") != "scene_changed" or observation.get("game_ui") == 3):
                raise EvidenceError("terminal draw skip lacks its final scene_changed completion")
            self.terminal_completed = True
        elif self.terminal is not None and kind in {"request_started", "action", "render_preparing", "render_prepared"}:
            raise EvidenceError("simulation mutation follows terminal draw skip")
        if (self.terminal is not None and not self.terminal_completed
                and kind not in {"terminal_transition", "request_completed"} and not auxiliary):
            raise EvidenceError("terminal draw skip has an intervening event")

    def state(self, state, receipt=None):
        if first_difference(state.get("draw_schedule"), self.counts()):
            raise EvidenceError("audited draw count differs from checked receipts")
        if receipt is not None:
            clocks = _state_clocks(state)
            if receipt["native_clock"] != clocks["game_clock"]:
                raise EvidenceError("draw receipt native clock differs from audited state")
            if receipt["phase"] != "terminal":
                if receipt["clocks_after"] != clocks or receipt["rng_after"] != digests(state["rng"]["instances"]):
                    raise EvidenceError("draw receipt clocks/RNG differ from actual post state")

    def frame(self, frame):
        if self.warm is None or self.terminal is not None:
            raise EvidenceError("audited update lacks warm preparation or follows terminal draw skip")
        self.seen_frame = True
        if frame.version["epoch"] != self.warm["frame_version"]["epoch"]:
            raise EvidenceError("controlled drawing crosses an unprepared epoch")
        if frame.kind == "pre_step":
            if "render" in frame.payload or frame.version["tick"] != self.posts:
                raise EvidenceError("draw receipt is misplaced or update ordinal differs")
            self.state(frame.state)
            return
        receipt = frame.payload.get("render")
        if not isinstance(receipt, dict):
            raise EvidenceError("verified post_step requires exactly one controlled draw receipt")
        terminal = receipt.get("phase") == "terminal"
        self.posts += 1
        if terminal:
            self.terminal_skips += 1
        else:
            self.step_frames += 1
        self._receipt(receipt, frame.version, "terminal" if terminal else "step")
        if (frame.state.get("app", {}).get("ui") != 3) != terminal:
            raise EvidenceError("terminal draw skip and audited fight UI disagree")
        self.state(frame.state, receipt)
        if terminal:
            self.terminal = {"version": frame.version, "request_id": frame.payload["request_id"], "seq": frame.seq}

    def initial(self, initial):
        if self.warm is None or initial["observation"]["version"] != self.warm["frame_version"]:
            raise EvidenceError("initial marker is not the prepared warm boundary")
        if initial["observation"].get("render_prepared") is not True:
            raise EvidenceError("initial observation does not confirm render preparation")
        # Validate B0 independently of counters advanced by later frames.
        expected = {"mode": DRAW_SCHEDULE_MODE, "warm_frames": 1, "step_frames": 0}
        if first_difference(initial["state"].get("draw_schedule"), expected):
            raise EvidenceError("initial draw state is not warm1/step0")
        if (self.warm["clocks_after"] != _state_clocks(initial["state"])
                or self.warm["rng_after"] != digests(initial["state"]["rng"]["instances"])):
            raise EvidenceError("initial marker differs from the actual postwarm clocks/RNG")
        if "initialization" in initial:
            recipe = initial["initialization"]
            seed = self.warm["seed_readback"]["seed"]
            preparation = {"warm_frames": 1, "step_frames": 0, "verified_before_draw": True,
                "seeded_rng_sha256": hashlib.sha256(canonical(_seeded_rng(seed))).hexdigest(),
                "postwarm_rng_sha256": hashlib.sha256(canonical(initial["state"]["rng"]["instances"])).hexdigest()}
            if (type(recipe.get("seed")) is not int or recipe["seed"] != seed
                    or recipe.get("clock_anchor") != self.warm["clocks_before"]
                    or recipe.get("postwarm_clock") != self.warm["clocks_after"]
                    or first_difference(recipe.get("render_preparation"), preparation)):
                raise EvidenceError("initialization recipe differs from actual warm seed/clock/RNG evidence")

    def finish(self):
        if self.preparing is not None and self.warm is None:
            raise EvidenceError("warm drawing never completed")
        if self.terminal is not None and not self.terminal_completed:
            raise EvidenceError("terminal draw skip is missing its transition/completion")

    def close(self, event):
        health = event["payload"]
        required = {"mode": DRAW_SCHEDULE_MODE, "installed": True, "latched": True, "sealed": True,
                    "active": False, "healthy": True, "faults": 0, "wrong_thread_calls": 0,
                    "controlled_calls": self.warm_frames + self.step_frames,
                    "warm_frames": self.warm_frames, "step_frames": self.step_frames}
        if (self.warm is None or any(type(health.get(key)) is not type(value) or health[key] != value for key, value in required.items())
                or any(type(health.get(key)) is not int or health[key] < 0 for key in ("automatic_allowed", "automatic_denied"))):
            raise EvidenceError("draw schedule close health/count is incomplete or unhealthy")
        self.health = copy.deepcopy(health)


class _ParticleEvidence:
    def __init__(self):
        self.captured = 0
        self.controlled_calls = 0

    def accept(self, event, raw):
        AuditLog._envelope(event)
        if not isinstance(raw, dict) or raw.get("schema") != "lvz.particle-shake-raw.v1":
            raise EvidenceError("invalid raw particle shake evidence schema")
        if any(raw.get(key) != event.get(key) for key in ("seq", "kind", "version", "phase", "native_phase")):
            raise EvidenceError("particle shake raw/semantic envelope mismatch")
        payload, original = event["payload"], raw.get("payload")
        extras = {"particle_address", "emitter_address", "system_address", "holder_address", "pool_block_address", "original_seed"}
        if not isinstance(original, dict) or set(original) != set(payload) | extras:
            raise EvidenceError("particle shake raw payload must retain complete semantic and pointer evidence")
        if any(original[key] != value for key, value in payload.items()):
            raise EvidenceError("particle shake raw/semantic payload mismatch")
        uint = lambda value: type(value) is int and 0 <= value <= 0xffffffff
        if (payload.get("mode") != PARTICLE_SHAKE_MODE or payload.get("pool_verified") is not True
                or type(payload.get("ordinal")) is not int or payload["ordinal"] != self.captured
                or any(not uint(payload.get(key)) for key in ("particle_id", "slot", "generation", "factor", "canonical_seed"))
                or any(not uint(original[key]) for key in extras)):
            raise EvidenceError("invalid particle shake mode, identity, ordinal, or seed")
        expected_phases = {"pre_step", "request_started", "action"} if event["phase"] == "controlled_boundary" else {"initialization"}
        if payload.get("control_phase") not in expected_phases:
            raise EvidenceError("particle shake control phase is inconsistent")
        age, duration, site = payload.get("age"), payload.get("duration"), payload.get("callsite_rva")
        crossfade = payload.get("crossfade_duration")
        if (type(age) is not int or type(duration) is not int or type(crossfade) is not int
                or not 0 <= age <= 0x7fffffff or not 1 <= duration <= 0x7fffffff
                or not 0 <= crossfade <= 0x7fffffff or (age >= duration and crossfade == 0)):
            raise EvidenceError("invalid particle shake age/duration/crossfade")
        if type(site) is not int or site not in {0x116b3c, 0x116ba5}:
            raise EvidenceError("particle shake event came from an unsupported callsite")
        factor = (duration - 1 if age == 0 else age - 1) if site == 0x116b3c else age
        identity = payload["particle_id"]
        if (payload["factor"] != factor or payload["slot"] != identity & 0xffff
                or payload["generation"] != identity >> 16 or payload["generation"] == 0
                or payload["canonical_seed"] != (identity * factor) & 0xffffffff
                or original["original_seed"] != (original["particle_address"] * factor) & 0xffffffff
                or any(original[key] == 0 for key in extras - {"original_seed"})
                or original["particle_address"] != original["pool_block_address"] + payload["slot"] * 0xa0):
            raise EvidenceError("particle shake generation/factor/seed relation is invalid")
        pool = payload.get("pool")
        if (not isinstance(pool, dict) or set(pool) != {"used", "capacity", "count", "free_head", "next_key"}
                or any(not uint(value) for value in pool.values())
                or not 1 <= pool["count"] <= pool["used"] <= pool["capacity"] <= 1024
                or pool["free_head"] > pool["used"] or not 1 <= pool["next_key"] <= 65535
                or payload["slot"] >= pool["used"]):
            raise EvidenceError("invalid verified particle pool evidence")
        self.captured += 1
        self.controlled_calls += event["phase"] == "controlled_boundary"

    def close(self, event):
        health = event["payload"]
        if (health.get("installed") is not True or health.get("healthy") is not True
                or any(type(health.get(key)) is not int or health[key] != 0
                       for key in ("queued", "wrong_thread_calls", "faults", "overflow"))
                or type(health.get("captured")) is not int or health["captured"] != self.captured
                or type(health.get("controlled_calls")) is not int or health["controlled_calls"] != self.controlled_calls
                or ("last_fault_code" in health and (type(health["last_fault_code"]) is not int or health["last_fault_code"] != 0))):
            raise EvidenceError("particle shake hook health/count is incomplete or unhealthy")


class _EventSummary:
    def __init__(self):
        self.tail = deque(maxlen=4)
        self.birth_count = 0
        self.controlled_birth_count = 0
        self.initialization_birth_count = 0
        self.count = 0
        self.last_seq = -1
        self.recording_closed = False

    def accept(self, event):
        AuditLog._envelope(event)
        if event["kind"] == "reanimation_link_fault":
            raise EvidenceError("native animation link fault invalidates strict evidence")
        if event["seq"] < self.last_seq:
            raise EvidenceError("audit event sequence moved backwards")
        if self.recording_closed and event["kind"] not in {"draw_schedule_closed", "particle_shake_closed", "spawn_hook_closed"}:
            raise EvidenceError("native event after recording close")
        if not self.recording_closed and event["kind"] in {"draw_schedule_closed", "particle_shake_closed", "spawn_hook_closed"}:
            raise EvidenceError("native hook close precedes recording close")
        self.last_seq = event["seq"]
        if event["kind"] == "zombie_initialized":
            if type(event["payload"].get("ordinal")) is not int or event["payload"]["ordinal"] != self.birth_count:
                raise EvidenceError("spawn ordinal is missing or duplicated")
            self.birth_count += 1
            if event["phase"] == "controlled_boundary":
                self.controlled_birth_count += 1
            else:
                self.initialization_birth_count += 1
        self.recording_closed |= event["kind"] == "recording_closed"
        self.tail.append(event)
        self.count += 1


def _closed_events(summary, *, particle=None, draw=None, spawn_required=False):
    """Validate the declared close sequence and all hook health summaries."""
    final = list(summary.tail)
    if spawn_required and (not final or final[-1]["kind"] != "spawn_hook_closed"):
        raise EvidenceError("required spawn hook close health is missing")
    if final and final[-1]["kind"] == "spawn_hook_closed":
        health = final[-1]["payload"]
        if (health.get("healthy") is not True or any(type(health.get(key)) is not int or health[key] != 0
                for key in ("faults", "overflow", "wrong_thread_calls", "queued", "active_initializers"))
                or type(health.get("captured")) is not int or health["captured"] != summary.birth_count):
            raise EvidenceError("final spawn hook evidence is unhealthy or incomplete")
        if len(final) < 2 or final[-1]["version"] != final[-2]["version"]:
            raise EvidenceError("spawn hook close boundary differs from recording close")
        final = final[:-1]
    if particle is not None:
        if not final or final[-1]["kind"] != "particle_shake_closed":
            raise EvidenceError("required particle shake close health is missing")
        particle.close(final[-1])
        if len(final) < 2 or final[-1]["version"] != final[-2]["version"]:
            raise EvidenceError("particle shake close boundary differs from recording close")
        final = final[:-1]
    elif any(event["kind"] == "particle_shake_closed" for event in final):
        raise EvidenceError("particle shake close has no declared engine mode")
    if draw is not None:
        if not final or final[-1]["kind"] != "draw_schedule_closed":
            raise EvidenceError("required draw schedule close health is missing")
        draw.close(final[-1])
        if len(final) < 2 or final[-1]["version"] != final[-2]["version"]:
            raise EvidenceError("draw schedule close boundary differs from recording close")
        final = final[:-1]
    elif any(event["kind"] == "draw_schedule_closed" for event in final):
        raise EvidenceError("draw close has no declared engine mode")
    if not final or final[-1]["kind"] != "recording_closed":
        raise EvidenceError("native recording lacks a completed recording_closed boundary")


def _particle_boundary(event, frame, previous):
    before = event["version"]
    if event["payload"]["control_phase"] == "pre_step":
        if previous is None or previous.kind != "pre_step" or frame.kind != "post_step" or before != previous.version:
            raise EvidenceError("particle shake call is not bound to its actual pre/post update")
    elif (frame.kind != "pre_step" or before["epoch"] != frame.version["epoch"]
            or before["tick"] != frame.version["tick"] or before["revision"] > frame.version["revision"]):
        raise EvidenceError("particle shake action call is not bound to its next audited pre-step")


def _particle_digest(previous, payload):
    """Native canonical word order; runtime epoch/revision never enter state."""
    values = [payload[key] for key in ("callsite_rva", "particle_id", "factor", "canonical_seed", "age", "duration", "crossfade_duration")]
    values += [payload["pool"][key] for key in ("used", "capacity", "free_head", "count", "next_key")]
    return _fnv_continue(previous, struct.pack("<12Q", *values))


class _AnimationDecoder:
    """Validate raw allocation evidence against each normalized frame.

    Raw handles are retained and checked locally, never equated across runs.
    The decoder retains one reconstructed raw snapshot, not the full history.
    """
    def __init__(self, manifest=None):
        self.state = None
        self.owner_retirement_rules = (manifest or {}).get("coverage", {}).get("reanimations", {}).get("owner_retirement_rules") == _OWNER_RETIREMENT_RULES

    def accept(self, record, frame):
        AuditLog._envelope(record)
        envelope = {"schema": SCHEMA, "seq": frame.seq, "kind": frame.kind,
                    "version": frame.version, "payload": frame.payload}
        if any(record[key] != value for key, value in envelope.items()):
            raise EvidenceError("raw animation/frame envelope mismatch")
        if self.state is None:
            if "initial" not in record or "patch" in record:
                raise EvidenceError("raw animation evidence requires initial state")
            self.state = record["initial"]
        else:
            if "initial" in record or not isinstance(record.get("patch"), list):
                raise EvidenceError("raw animation evidence requires subsequent patch")
            self.state = patch(self.state, record["patch"], in_place=True)
        raw = self.state
        if raw is None and frame.state.get("board") is None and "reanimations" not in frame.state:
            return
        if not isinstance(raw, dict) or raw.get("schema") != "lvz.reanimation-raw.v1":
            raise EvidenceError("invalid raw animation state schema")
        pool, slots, links = raw.get("pool"), raw.get("actual_slot_ids"), raw.get("links")
        uint = lambda value: type(value) is int and 0 <= value <= 0xffffffff
        if (not isinstance(pool, dict) or set(pool) != {"capacity", "used", "count", "free_head", "next_key"}
                or not all(uint(value) for value in pool.values()) or not pool["used"] <= pool["capacity"] <= 65536
                or not isinstance(slots, dict) or pool["count"] != len(slots) or not isinstance(links, list)):
            raise EvidenceError("invalid raw animation pool")
        for slot, identity in slots.items():
            if (not slot.isascii() or not slot.isdecimal() or str(int(slot)) != slot
                    or int(slot) >= pool["used"] or not uint(identity) or identity >> 16 == 0
                    or identity & 0xffff != int(slot)):
                raise EvidenceError("raw animation slot/generation mismatch")
        normalized = frame.state.get("reanimations")
        if (not isinstance(normalized, dict) or normalized.get("schema") != "lvz.reanimation-links.v1"
                or normalized.get("valid") is not True or normalized.get("issues") != []
                or not isinstance(normalized.get("nodes"), dict)):
            raise EvidenceError("invalid comparable animation state")
        paths, anchors, owners, checked_flags = set(), set(), {}, {}
        for link in links:
            if not isinstance(link, dict):
                raise EvidenceError("invalid raw animation link")
            path, anchor, handle = link.get("path"), link.get("anchor"), link.get("raw_handle")
            if (not isinstance(path, str) or not path or path in paths or not isinstance(anchor, str)
                    or not anchor or anchor in anchors or not uint(handle) or type(link.get("slot")) is not int
                    or link["slot"] != handle & 0xffff or type(link.get("owner_dead")) is not bool
                    or type(link.get("lookup_matches")) is not bool):
                raise EvidenceError("invalid raw animation link identity")
            paths.add(path)
            anchors.add(anchor)
            actual = slots.get(str(link["slot"]))
            matches = handle != 0 and actual == handle
            if link.get("actual_slot_id") != actual or link["lookup_matches"] != matches:
                raise EvidenceError("raw animation lookup evidence mismatch")
            reference = link.get("normalized_reference")
            if not isinstance(reference, dict):
                raise EvidenceError("invalid normalized animation reference")
            try:
                field = frame.state
                tokens = _tokens(path)
                for token in tokens:
                    field = field[_index(field, token)]
            except (KeyError, TypeError) as error:
                raise EvidenceError("raw animation owner path is missing") from error
            if reference != field:
                raise EvidenceError("raw animation reference differs from comparable state")
            owner_squished = False
            plant_path = bool(tokens and tokens[0] == "plants")
            if "owner_squished" in link and (not plant_path or not self.owner_retirement_rules):
                raise EvidenceError("owner_squished requires a plant owner and declared retirement rules")
            if self.owner_retirement_rules:
                dead_offsets = {"plants": "00000141", "zombies": "000000ec", "mowers": "00000030", "grid_items": "00000020"}
                if tokens and tokens[0] in dead_offsets:
                    if len(tokens) != 5 or tokens[1] != "slots" or tokens[3] != "fields":
                        raise EvidenceError("invalid entity animation owner path")
                    owner_key = tokens[:3]
                    if owner_key not in checked_flags:
                        fields = frame.state[tokens[0]]["slots"][tokens[2]]["fields"]
                        dead = fields.get(dead_offsets[tokens[0]])
                        squished = fields.get("00000142") if plant_path else 0
                        if not uint(dead) or not uint(squished):
                            raise EvidenceError("animation owner retirement fields are missing or invalid")
                        checked_flags[owner_key] = (dead != 0, squished != 0)
                    actual_dead, actual_squished = checked_flags[owner_key]
                    if link["owner_dead"] != actual_dead:
                        raise EvidenceError("raw owner_dead differs from the actual owner field")
                    if plant_path:
                        if type(link.get("owner_squished")) is not bool or link["owner_squished"] != actual_squished:
                            raise EvidenceError("raw owner_squished differs from the actual plant field")
                        owner_squished = actual_squished
                elif link["owner_dead"]:
                    raise EvidenceError("non-entity animation owner cannot claim owner_dead")
            if "retirement_reason" in link and (not self.owner_retirement_rules or reference.get("status") != "expired"):
                raise EvidenceError("unexpected animation retirement reason")
            if not handle:
                if reference != {"status": "null"}:
                    raise EvidenceError("non-null reference for zero animation handle")
            elif not matches:
                reason = "out_of_range" if link["slot"] >= pool["capacity"] else (
                    "not_allocated" if actual is None else "generation_mismatch")
                if not (link["owner_dead"] or owner_squished) or reference != {"status": "expired"} or link.get("lookup_failure") != reason:
                    raise EvidenceError("invalid or dangling live animation link")
                if self.owner_retirement_rules:
                    retirement = "owner_dead" if link["owner_dead"] else "plant_squished_remove_effects"
                    if link.get("retirement_reason") != retirement:
                        raise EvidenceError("animation retirement reason differs from actual lifecycle flags")
            else:
                node = link.get("logical_node")
                if (not isinstance(node, str) or reference.get("node") != node
                        or reference.get("status") not in {"live", "retiring"} or node not in normalized["nodes"]):
                    raise EvidenceError("raw animation logical node mismatch")
                owners.setdefault(node, []).append(anchor)
        if set(owners) != set(normalized["nodes"]):
            raise EvidenceError("raw animation node evidence is incomplete")
        expected_paths = set()
        board = frame.state.get("board", {})
        for key, value in board.items() if isinstance(board, dict) else ():
            if isinstance(value, dict) and "status" in value:
                expected_paths.add("/board/" + key.replace("~", "~0").replace("/", "~1"))
        for pool_name in ("zombies", "plants", "mowers", "grid_items"):
            for slot, entity in frame.state.get(pool_name, {}).get("slots", {}).items():
                for key, value in entity.get("fields", {}).items():
                    if isinstance(value, dict) and "status" in value:
                        expected_paths.add(f"/{pool_name}/slots/{slot}/fields/{key}")
        if paths != expected_paths:
            raise EvidenceError("raw animation owner evidence is incomplete")
        for node, anchors_for_node in owners.items():
            if normalized["nodes"][node].get("owners") != anchors_for_node:
                raise EvidenceError("raw animation owner alias graph mismatch")


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
        if "reanimations" in self.state and (not isinstance(self.state["reanimations"], dict)
                                             or self.state["reanimations"].get("valid") is not True):
            raise EvidenceError("native animation links are invalid")
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


class _AuditStreamDecoder:
    """Single merge cursor: one state, one frame of seeds, and small counters."""
    def __init__(self, manifest, files, *, reuse_state):
        self.frames = _FrameDecoder(reuse_state=reuse_state)
        self.animation = _AnimationDecoder(manifest) if RAW_ANIMATIONS in files else None
        self.particle = _ParticleEvidence() if PARTICLE_SHAKE_RAW in files else None
        self.draw = _DrawEvidence(manifest) if draw_mode(manifest) else None
        self.summary = _EventSummary()
        self.pending = []
        self.pending_spawns = []
        self.next_seq = 0
        self.last_frame = None
        self.controlled_calls = 0
        self.rolling = 14695981039346656037
        self.reuse_state = reuse_state
        self.peak_pending = 0

    def event(self, event, raw):
        self.summary.accept(event)
        if self.draw:
            self.draw.event(event)
        elif event["kind"] in {"render_preparing", "render_prepared", "draw_schedule_closed"}:
            raise EvidenceError("controlled draw event lacks an explicit engine mode")
        shared = (event["kind"] == "zombie_first_boundary_observed" and self.last_frame is not None
                  and event["seq"] == self.last_frame.seq)
        if shared:
            if event["version"] != self.last_frame.version:
                raise EvidenceError("birth annotation/frame version mismatch")
        elif event["seq"] != self.next_seq:
            raise EvidenceError("native audit sequence is missing or duplicated")
        else:
            self.next_seq += 1
        if event["kind"] == "particle_shake_seed":
            if self.particle is None or raw is None:
                raise EvidenceError("particle shake semantic/raw evidence or declared engine mode is missing")
            self.particle.accept(event, raw)
            if event["phase"] == "controlled_boundary":
                if len(self.pending) >= _PARTICLE_BOUNDARY_LIMIT:
                    raise EvidenceError("particle calls before an audited boundary exceed reader bound 8192")
                self.pending.append(event)
                self.peak_pending = max(self.peak_pending, len(self.pending))
            elif self.last_frame is not None:
                raise EvidenceError("particle shake lost its controlled boundary after recording started")
        elif event["kind"] == "particle_shake_closed":
            if self.particle is None:
                raise EvidenceError("particle shake close has no declared engine mode")
            self.particle.close(event)
        elif event["kind"] == "zombie_initialized":
            if event["phase"] == "controlled_boundary":
                if len(self.pending_spawns) >= _SPAWN_BOUNDARY_LIMIT:
                    raise EvidenceError("spawns before an audited boundary exceed reader bound 1024")
                self.pending_spawns.append(event)
            elif self.last_frame is not None and self.last_frame.kind == "pre_step":
                raise EvidenceError("spawn lost its controlled boundary during an audited update")

    def frame(self, records):
        if self.summary.recording_closed:
            raise EvidenceError("native frame after recording close")
        frame = self.frames.accept(records[0], records[1])
        if self.draw:
            self.draw.frame(frame)
        elif "draw_schedule" in frame.state or "render" in frame.payload:
            raise EvidenceError("controlled draw state/receipt requires an explicit engine mode")
        if frame.seq != self.next_seq:
            raise EvidenceError("native audit sequence is missing or duplicated")
        self.next_seq += 1
        if self.animation:
            self.animation.accept(records[2], frame)
            frame = replace(frame, raw_animations=self.animation.state if self.reuse_state else copy.deepcopy(self.animation.state))
        for event in self.pending:
            _particle_boundary(event, frame, self.last_frame)
            self.controlled_calls += 1
            self.rolling = _particle_digest(self.rolling, event["payload"])
        if self.particle:
            state = frame.state.get("particle_shake")
            if (not isinstance(state, dict) or state.get("mode") != PARTICLE_SHAKE_MODE
                    or type(state.get("controlled_calls")) is not int or state["controlled_calls"] != self.controlled_calls
                    or type(state.get("controlled_digest")) is not int or state["controlled_digest"] != self.rolling):
                raise EvidenceError("particle shake state count/digest differs from verified seed events")
        elif "particle_shake" in frame.state:
            raise EvidenceError("particle shake state requires an explicit engine mode")
        for event in self.pending_spawns:
            before = event["version"]
            if frame.kind == "post_step":
                if self.last_frame is None or self.last_frame.kind != "pre_step" or before != self.last_frame.version:
                    raise EvidenceError("spawn is not bound to its actual pre/post update")
            elif (before["epoch"] != frame.version["epoch"] or before["tick"] != frame.version["tick"]
                  or before["revision"] > frame.version["revision"]):
                raise EvidenceError("spawn action is not bound to its next audited pre-step")
        result = replace(frame, particle_seeds=tuple(self.pending), spawn_events=tuple(self.pending_spawns))
        self.pending.clear()
        self.pending_spawns.clear()
        self.last_frame = AuditFrame(frame.seq, frame.kind, frame.version, frame.payload, {}, {})
        return result

    def finish(self, *, final=False):
        self.frames.finish()
        if self.draw:
            self.draw.finish()
        if final and self.pending:
            raise EvidenceError("particle shake calls have no following audited boundary")
        if final and self.pending_spawns:
            raise EvidenceError("controlled spawns have no following audited boundary")


def _walk_audit(decoder, read, *, retain_event=None, request_id=None, constrain_request=False):
    """Merge sorted event/frame sequences without retaining the event history."""
    from itertools import zip_longest
    event_reader = iter(read("events.jsonl"))
    names = ["checksums.jsonl", "state-deltas.jsonl"]
    if decoder.animation:
        names.append(RAW_ANIMATIONS)
    frame_readers = [iter(read(name)) for name in names]
    rows = zip_longest(*frame_readers)
    raw = iter(read(PARTICLE_SHAKE_RAW)) if decoder.particle else iter(())

    def next_event():
        event = next(event_reader, None)
        if event is not None:
            AuditLog._envelope(event)
        return event

    def next_frame():
        records = next(rows, None)
        if records is not None:
            if any(record is None for record in records):
                raise EvidenceError("checksum/delta/raw animation record count mismatch")
            AuditLog._envelope(records[0])
        return records

    try:
        event, records = next_event(), next_frame()
        while event is not None or records is not None:
            if records is not None and (event is None or records[0]["seq"] <= event["seq"]):
                frame = decoder.frame(records)
                if constrain_request and frame.payload.get("request_id") != request_id:
                    raise EvidenceError("unexpected intervening request in live frame audit")
                yield frame
                records = next_frame()
            else:
                decoder.event(event, next(raw, None) if event["kind"] == "particle_shake_seed" else None)
                if retain_event is not None and event["kind"] != "particle_shake_seed":
                    retain_event(event)
                event = next_event()
        if next(raw, None) is not None:
            raise EvidenceError("particle shake semantic/raw record count mismatch")
        decoder.finish()
    finally:
        first_error = None
        for reader in [event_reader, *frame_readers, raw]:
            close = getattr(reader, "close", None)
            if close:
                try:
                    close()
                except Exception as error:
                    first_error = first_error or error
        if first_error is not None:
            raise first_error


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
        frames = self.owner._frames()
        try:
            for index, frame in enumerate(frames):
                if index == current:
                    yield frame
                    current = next(wanted, None)
                    if current is None:
                        return
        finally:
            frames.close()

    def __getitem__(self, index):
        if isinstance(index, slice):
            return FrameSelection(self.owner, self.indices[index])
        wanted = self.indices[index]
        selection = iter(FrameSelection(self.owner, [wanted]))
        try:
            return next(selection)
        finally:
            selection.close()


class AuditLog:
    def __init__(self, directory: str | Path, *, require_closed: bool = False):
        self.directory = Path(directory)
        self.manifest = read_json(self.directory / "manifest.json")
        if self.manifest.get("schema") != SCHEMA or not isinstance(self.manifest.get("target"), str):
            raise EvidenceError("invalid native audit manifest")
        if self.manifest.get("loaded_signatures_match") is not True:
            raise EvidenceError("native target signatures did not match")
        self.evidence_files = audit_files(self.directory, self.manifest)
        self._files = {name: EventStream(self.directory / name) for name in self.evidence_files}
        self.events = self._files["events.jsonl"]
        self._control_events, self._headers, self._frame_headers = [], [], []
        self._requests, self._request_frames, self._frame_indices = {}, {}, {}
        decoder = _AuditStreamDecoder(self.manifest, self.evidence_files, reuse_state=True)
        for index, frame in enumerate(_walk_audit(decoder, self._files.__getitem__, retain_event=self._retain_event)):
            header = AuditFrame(frame.seq, frame.kind, frame.version, frame.payload, {}, {})
            rid = frame.payload.get("request_id")
            self._headers.append(header)
            self._frame_headers.append((frame.seq, rid))
            self._request_frames.setdefault(rid, []).append(header)
            self._frame_indices.setdefault(rid, []).append(index)
        decoder.finish(final=True)
        self._particle, self._summary, self._draw = decoder.particle, decoder.summary, decoder.draw
        self.peak_pending_particle_calls = decoder.peak_pending
        self.frames = FrameSelection(self)
        if require_closed:
            _closed_events(self._summary, particle=self._particle, draw=self._draw,
                           spawn_required=self.manifest.get("spawn_hook", {}).get("installed") is True)

    def _retain_event(self, event):
        self._control_events.append(event)
        self._requests.setdefault(event["payload"].get("request_id"), []).append(event)

    def verify_files(self):
        """Explicit success barrier, including sources never opened by a seek."""
        if audit_files(self.directory, self.manifest) != self.evidence_files:
            raise EvidenceError("closed audit evidence file set changed")
        for evidence in self._files.values():
            evidence.verify()

    @property
    def birth_counts(self):
        return {"controlled": self._summary.controlled_birth_count,
                "initialization": self._summary.initialization_birth_count}

    def validate_draw_initial(self, initial):
        if self._draw:
            self._draw.initial(initial)

    @property
    def warm_render(self):
        return copy.deepcopy(self._draw.warm) if self._draw else None

    @property
    def draw_health(self):
        return copy.deepcopy(self._draw.health) if self._draw else None

    @property
    def control_events(self):
        """Verified control/birth index, excluding high-volume particle calls."""
        self.events.verify()
        return iter(self._control_events)

    @staticmethod
    def _envelope(record):
        if record.get("schema") != SCHEMA or type(record.get("seq")) is not int or record["seq"] < 0:
            raise EvidenceError("invalid native audit schema/sequence")
        if not isinstance(record.get("payload"), dict) or not isinstance(record.get("kind"), str):
            raise EvidenceError("invalid native audit envelope")
        if record["kind"] in {"spawn_hook_fault", "particle_shake_fault", "reanimation_link_fault", "render_failed", "draw_schedule_fault"}:
            raise EvidenceError("native hook fault invalidates strict evidence")
        if record["kind"] == "particle_shake_seed":
            if record.get("native_phase") != "before_srand" or record.get("phase") not in {"controlled_boundary", "initialization"}:
                raise EvidenceError("invalid particle shake capture phase")
            if record["phase"] == "initialization":
                if "version" not in record or record["version"] is not None:
                    raise EvidenceError("initialization particle shake must have explicitly unassigned version")
                return
        if record["kind"] == "zombie_initialized":
            spawn = record["payload"]
            if (record.get("native_phase") != "zombie_initialize_exit" or spawn.get("schema") != "lvz.spawn.v1"
                    or spawn.get("kind") != "zombie_initialized" or spawn.get("phase") != "zombie_initialize_exit"):
                raise EvidenceError("invalid exact zombie initialization evidence")
            if record.get("phase") == "initialization":
                if record.get("version") is not None or "boundary" not in spawn or spawn["boundary"] is not None:
                    raise EvidenceError("initialization spawn must have an explicitly unassigned boundary")
                return
            boundary = spawn.get("boundary")
            if (record.get("phase") != "controlled_boundary" or not isinstance(boundary, dict)
                    or record.get("version") != {"epoch": boundary.get("segment"), "tick": boundary.get("tick"),
                                                 "revision": boundary.get("revision")}):
                raise EvidenceError("controlled spawn boundary/version mismatch")
        version(record.get("version"))

    def _frames(self, *, reuse_state=False):
        self._files["manifest.json"].verify()
        if audit_files(self.directory, self.manifest) != self.evidence_files:
            raise EvidenceError("closed audit evidence file set changed")
        decoder = _AuditStreamDecoder(self.manifest, self.evidence_files, reuse_state=reuse_state)
        yield from _walk_audit(decoder, self._files.__getitem__)
        decoder.finish(final=True)

    def request_frames(self, request_id: str) -> FrameSelection:
        return FrameSelection(self, self._frame_indices.get(request_id, []))

    def request_headers(self, request_id: str) -> list[AuditFrame]:
        """Metadata only; useful for ordering checks without state reconstruction."""
        return self._request_frames.get(request_id, [])

    def request_events(self, request_id: str) -> list[dict]:
        return self._requests.get(request_id, [])


class _ConsumedEventStream:
    """Repeatable view of the consumed live prefix; never retains its rows."""
    def __init__(self, owner):
        self.owner = owner

    def __iter__(self):
        yield from self.owner._prefix_records("events.jsonl")


class AuditTail:
    """Verify append-only evidence with one state and at most 8192 pending seeds.

    Control/birth records and small frame headers remain indexed. Every seed's
    raw and semantic records are verified as they arrive, then released after
    its frame is consumed. Closing rechecks consumed byte hashes, without
    decoding or patching the history again.
    """
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.manifest = read_json(self.directory / "manifest.json")
        if (self.manifest.get("schema") != SCHEMA or not isinstance(self.manifest.get("target"), str)
                or self.manifest.get("loaded_signatures_match") is not True):
            raise EvidenceError("invalid live audit target manifest")
        self._manifest_file = EventStream(self.directory / "manifest.json")
        self.evidence_files = audit_files(self.directory, self.manifest)
        self._headers, self._control_events = [], []
        self._positions = {name: 0 for name in self.evidence_files if name != "manifest.json"}
        self._hashes = {name: hashlib.sha256() for name in self._positions}
        self._stream = _AuditStreamDecoder(self.manifest, self.evidence_files, reuse_state=True)
        self._decoder, self._animation, self._particle = self._stream.frames, self._stream.animation, self._stream.particle
        self._draw = self._stream.draw
        self._identities = {}
        self._requests, self._request_frames = {}, {}
        self.events = _ConsumedEventStream(self)

    @property
    def peak_pending_particle_calls(self):
        return self._stream.peak_pending

    @property
    def birth_counts(self):
        return {"controlled": self._stream.summary.controlled_birth_count,
                "initialization": self._stream.summary.initialization_birth_count}

    def validate_draw_initial(self, initial):
        if self._draw:
            self._draw.initial(initial)

    @property
    def warm_render(self):
        return copy.deepcopy(self._draw.warm) if self._draw else None

    @property
    def draw_health(self):
        return copy.deepcopy(self._draw.health) if self._draw else None

    def _check_file(self, name, *, required_size=None):
        stat = (self.directory / name).stat()
        identity = (stat.st_dev, stat.st_ino)
        if name in self._identities and self._identities[name] != identity:
            raise EvidenceError("live audit file was replaced")
        self._identities[name] = identity
        if stat.st_size < (self._positions[name] if required_size is None else required_size):
            raise EvidenceError("live audit file was truncated")
        return stat

    def _new_records(self, name):
        # A completed response flushes its evidence. Read exactly this prefix;
        # an unrelated later request cannot extend a cursor indefinitely.
        limit = self._check_file(name).st_size
        with (self.directory / name).open("rb") as stream:
            stream.seek(self._positions[name])
            while stream.tell() < limit:
                line = stream.readline(min(limit - stream.tell(), _JSONL_MAX_RECORD_BYTES + 1))
                if len(line) > _JSONL_MAX_RECORD_BYTES:
                    raise EvidenceError("audit JSONL record exceeds reader bound 32 MiB")
                if not line.endswith(b"\n"):
                    raise EvidenceError("live audit contains an incomplete flushed record")
                value = decode(line)
                if not isinstance(value, dict):
                    raise EvidenceError("live audit record must be an object")
                self._hashes[name].update(line)
                self._positions[name] = stream.tell()
                yield value
        self._check_file(name, required_size=limit)

    def _verify_prefix(self, name, limit, expected):
        self._check_file(name, required_size=limit)
        digest = hashlib.sha256()
        with (self.directory / name).open("rb") as stream:
            remaining = limit
            while remaining:
                block = stream.read(min(1 << 20, remaining))
                if not block:
                    raise EvidenceError("live audit file was truncated")
                digest.update(block)
                remaining -= len(block)
        self._check_file(name, required_size=limit)
        if digest.hexdigest() != expected:
            raise EvidenceError("previously consumed live audit prefix SHA-256 changed")

    def _prefix_records(self, name):
        limit, expected = self._positions[name], self._hashes[name].hexdigest()
        self._check_file(name, required_size=limit)
        digest, complete = hashlib.sha256(), False
        try:
            with (self.directory / name).open("rb") as stream:
                while stream.tell() < limit:
                    line = stream.readline(min(limit - stream.tell(), _JSONL_MAX_RECORD_BYTES + 1))
                    if len(line) > _JSONL_MAX_RECORD_BYTES:
                        raise EvidenceError("audit JSONL record exceeds reader bound 32 MiB")
                    if not line.endswith(b"\n"):
                        raise EvidenceError("live audit contains an incomplete consumed record")
                    digest.update(line)
                    value = decode(line)
                    if not isinstance(value, dict):
                        raise EvidenceError("live audit record must be an object")
                    yield value
            complete = True
            self._check_file(name, required_size=limit)
            if digest.hexdigest() != expected:
                raise EvidenceError("previously consumed live audit prefix SHA-256 changed")
        finally:
            if not complete:
                self._verify_prefix(name, limit, expected)

    def _retain_event(self, event):
        self._control_events.append(event)
        self._requests.setdefault(event["payload"].get("request_id"), []).append(event)

    def read_request(self, request_id: str | None):
        self._manifest_file.verify()
        if audit_files(self.directory, self.manifest) != self.evidence_files:
            raise EvidenceError("live audit evidence file set changed")
        for frame in _walk_audit(self._stream, self._new_records, retain_event=self._retain_event,
                                 request_id=request_id, constrain_request=True):
            header = AuditFrame(frame.seq, frame.kind, frame.version, frame.payload, {}, {})
            self._headers.append(header)
            self._request_frames.setdefault(request_id, []).append(header)
            yield frame

    def verify_closed(self):
        for _ in self.read_request(None):
            raise EvidenceError("unexpected unconsumed frames at recording close")
        self._stream.finish(final=True)
        _closed_events(self._stream.summary, particle=self._particle, draw=self._draw,
                       spawn_required=self.manifest.get("spawn_hook", {}).get("installed") is True)
        for name, position in self._positions.items():
            if self._check_file(name).st_size != position:
                raise EvidenceError("unconsumed bytes after recording close")
            self._verify_prefix(name, position, self._hashes[name].hexdigest())

    def request_headers(self, request_id: str) -> list[AuditFrame]:
        return self._request_frames.get(request_id, [])

    def request_events(self, request_id: str) -> list[dict]:
        return self._requests.get(request_id, [])


def compare_audits(expected: AuditLog, actual: AuditLog, *,
                   request_map: dict[str, str] | None = None) -> dict:
    if expected.manifest != actual.manifest:
        return {"equal": False, "reason": "audit_manifest", "difference": first_difference(expected.manifest, actual.manifest)}
    unname_epoch = lambda value: {"tick": value["tick"], "revision": value["revision"]}
    difference = first_difference(render_semantics(expected.warm_render, map_version=unname_epoch),
                                  render_semantics(actual.warm_render, map_version=unname_epoch))
    if difference:
        return {"equal": False, "reason": "warm_render", "difference": difference}
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
        difference = first_difference(render_semantics(a.payload.get("render"), map_version=unname_epoch),
                                      render_semantics(b.payload.get("render"), map_version=unname_epoch))
        if difference:
            return {"equal": False, "reason": "render", "index": index, "difference": difference}
        difference = first_difference([particle_semantics(event, map_version=unname_epoch) for event in a.particle_seeds],
                                      [particle_semantics(event, map_version=unname_epoch) for event in b.particle_seeds])
        if difference:
            return {"equal": False, "reason": "particle_shake", "index": index, "difference": difference}
        difference = first_difference(a.state, b.state) if a.canonical_state != b.canonical_state else None
        if difference:
            return {"equal": False, "reason": "state", "index": index, "tick": a.version["tick"],
                    "phase": a.kind, "expected_seq": a.seq, "actual_seq": b.seq, "difference": difference}
    if len(left) != len(right):
        return {"equal": False, "reason": "frame_count", "expected": len(left), "actual": len(right)}
    expected.verify_files()
    actual.verify_files()
    return {"equal": True, "frames": len(left), "scope": "captured audit fields only",
            "original_engine_replay_verified": False}
