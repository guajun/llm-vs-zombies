"""Versioned, audited client for the local PvZ runtime protocol.

No request is automatically retried. In particular, a timeout after writing a
mutation is an unknown outcome, not evidence that the action did not execute.
"""

from __future__ import annotations

import copy
import base64
import hashlib
import json
import os
import re
import struct
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from .session import SessionTrace
from .initialization import DRAW_MODE, draw_mode

MAX_FRAME_BYTES = 4 * 1024 * 1024
READ_ONLY_METHODS = frozenset({"hello", "observe", "status", "audit_snapshot"})
BRANCH_SCHEMA = "lvz.branch-scope.v1"
_BRANCH_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")


class ProtocolError(RuntimeError):
    pass


def branch_scope_id(value: Any) -> str:
    """The branch-scope grammar, shared with the evidence tree's branch_id."""
    if not isinstance(value, str) or _BRANCH_ID.fullmatch(value) is None:
        raise ValueError("branch_id must be 1-64 characters of [A-Za-z0-9._:-] starting alphanumeric")
    return value


def declared_branch_scope(hello: Mapping[str, Any]) -> dict[str, Any] | None:
    """The runtime's own branch scope, or None for a pre-branch runtime.

    A runtime instance owns exactly one scope. Its dedup namespace is
    ``(branch_id, epoch, request_id)``; requests carry the scope id so a cloned
    or rebound process can never answer for a branch it did not execute.
    """
    value = hello.get("branch")
    if value is None:
        return None
    if not isinstance(value, Mapping) or value.get("schema") != BRANCH_SCHEMA:
        raise ProtocolError("hello branch scope is missing or malformed")
    mode = value.get("mode")
    if mode not in ("branch", "unscoped"):
        raise ProtocolError("hello branch scope mode is invalid")
    declared = value.get("branch_id")
    if mode == "branch":
        if not isinstance(declared, str):
            raise ProtocolError("a branch-scoped runtime must declare branch_id")
        try:
            declared = branch_scope_id(declared)
        except ValueError as error:
            raise ProtocolError(f"hello branch_id is invalid: {error}") from error
    elif declared is not None:
        raise ProtocolError("an unscoped runtime must not declare branch_id")
    parent = value.get("parent_branch_id")
    if parent is not None:
        try:
            parent = branch_scope_id(parent)
        except ValueError as error:
            raise ProtocolError(f"hello parent branch is invalid: {error}") from error
    origin = value.get("origin")
    if origin is not None and not isinstance(origin, str):
        raise ProtocolError("hello branch origin is invalid")
    dedup_key = value.get("dedup_key")
    if dedup_key is not None and not isinstance(dedup_key, str):
        raise ProtocolError("hello dedup key is invalid")
    return {"schema": BRANCH_SCHEMA, "mode": mode, "branch_id": declared, "parent_branch_id": parent,
            "origin": origin, "dedup_key": dedup_key}


class RemoteError(RuntimeError):
    def __init__(self, error: dict[str, Any], response: dict[str, Any]):
        self.code = error["code"]
        self.response = response
        super().__init__(f"{self.code}: {error['message']}")


class OutcomeUnknown(RuntimeError):
    """The mutation may have executed; inspect the trace and runtime state."""

    def __init__(self, request: dict[str, Any], cause: BaseException):
        self.request = copy.deepcopy(request)
        self.request_id = request["request_id"]
        super().__init__(f"Outcome unknown for {request['method']} ({self.request_id}): {cause}. "
                         "Do not repeat the action with a new request ID.")


class Transport(Protocol):
    def exchange(self, payload: bytes, timeout: float) -> bytes: ...
    def close(self) -> None: ...


class ByteStream(Protocol):
    def read(self, size: int, timeout: float) -> bytes: ...
    def write(self, data: bytes, timeout: float) -> int: ...
    def close(self) -> None: ...


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("named pipe request deadline exceeded")
    return remaining


class FramedTransport:
    """Length-prefixed exchange with one total deadline and exact short-I/O handling."""

    def __init__(self, stream: ByteStream):
        self.stream = stream
        self.closed = False

    def _read_exact(self, size: int, deadline: float) -> bytes:
        result = bytearray()
        while len(result) < size:
            chunk = self.stream.read(size - len(result), _remaining(deadline))
            if not chunk:
                raise EOFError("named pipe disconnected during response")
            if len(chunk) > size - len(result):
                raise ProtocolError("byte stream returned more bytes than requested")
            result.extend(chunk)
        return bytes(result)

    def exchange(self, payload: bytes, timeout: float) -> bytes:
        if self.closed:
            raise ConnectionError("transport is closed; reconnect explicitly")
        if not payload or len(payload) > MAX_FRAME_BYTES:
            raise ValueError("request payload must contain 1..4194304 bytes")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        deadline = time.monotonic() + timeout
        frame = struct.pack("<I", len(payload)) + payload
        try:
            offset = 0
            while offset < len(frame):
                count = self.stream.write(frame[offset:], _remaining(deadline))
                if count <= 0 or count > len(frame) - offset:
                    raise ConnectionError("invalid or zero-byte pipe write")
                offset += count
            size, = struct.unpack("<I", self._read_exact(4, deadline))
            if not 1 <= size <= MAX_FRAME_BYTES:
                raise ProtocolError(f"invalid response frame length: {size}")
            return self._read_exact(size, deadline)
        except BaseException:
            # A partial exchange cannot safely be reused for the next request.
            self.close()
            raise

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.stream.close()


class WindowsNamedPipeStream:
    """Windows overlapped I/O; a timed-out operation is cancelled and drained."""

    def __init__(self, endpoint: str, timeout: float = 10.0):
        if os.name != "nt":
            raise OSError("named pipe transport requires Windows")
        if not endpoint.startswith("\\\\.\\pipe\\llm-vs-zombies-") or "\\" in endpoint[9:]:
            raise ValueError("endpoint must be a local llm-vs-zombies named pipe")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        import ctypes
        from ctypes import wintypes

        class OVERLAPPED(ctypes.Structure):
            _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                        ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                        ("hEvent", wintypes.HANDLE)]

        self._ctypes, self._wintypes, self._overlapped = ctypes, wintypes, OVERLAPPED
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateFileW": (wintypes.HANDLE, [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]),
            "WaitNamedPipeW": (wintypes.BOOL, [wintypes.LPCWSTR, wintypes.DWORD]),
            "CreateEventW": (wintypes.HANDLE, [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]),
            "ReadFile": (wintypes.BOOL, [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                         ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED)]),
            "WriteFile": (wintypes.BOOL, [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED)]),
            "GetOverlappedResult": (wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED),
                                      ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]),
            "CancelIoEx": (wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]),
            "WaitForSingleObject": (wintypes.DWORD, [wintypes.HANDLE, wintypes.DWORD]),
            "CloseHandle": (wintypes.BOOL, [wintypes.HANDLE]),
        }
        for name, (restype, argtypes) in signatures.items():
            function = getattr(self._kernel, name)
            function.restype, function.argtypes = restype, argtypes
        deadline = time.monotonic() + timeout
        while True:
            handle = self._kernel.CreateFileW(endpoint, 0xC0000000, 0, None, 3, 0x40000000, None)
            if handle != ctypes.c_void_p(-1).value:
                self._handle = handle
                break
            error = ctypes.get_last_error()
            if error not in (2, 231):  # Not yet created, or all instances busy.
                raise ctypes.WinError(error)
            wait_seconds = min(_remaining(deadline), 0.05)
            if error == 231:
                self._kernel.WaitNamedPipeW(endpoint, max(1, int(wait_seconds * 1000)))
            else:
                time.sleep(wait_seconds)

    def _io(self, data: bytes | int, timeout: float, *, reading: bool) -> bytes | int:
        if self._handle is None:
            raise ConnectionError("named pipe is closed")
        c, w, k = self._ctypes, self._wintypes, self._kernel
        size = data if reading else len(data)
        buffer = c.create_string_buffer(size) if reading else c.create_string_buffer(data, size)
        event = k.CreateEventW(None, True, False, None)
        if not event:
            raise c.WinError(c.get_last_error())
        overlapped = self._overlapped()
        overlapped.hEvent = event
        transferred = w.DWORD()
        try:
            call = k.ReadFile if reading else k.WriteFile
            completed = call(self._handle, buffer, size, c.byref(transferred), c.byref(overlapped))
            if not completed:
                error = c.get_last_error()
                if error != 997:  # ERROR_IO_PENDING
                    raise c.WinError(error)
                wait = k.WaitForSingleObject(event, max(1, min(0xFFFFFFFE, int(timeout * 1000))))
                if wait != 0:
                    wait_error = c.get_last_error()
                    k.CancelIoEx(self._handle, c.byref(overlapped))
                    # Keep OVERLAPPED and buffer alive until cancellation completes.
                    k.GetOverlappedResult(self._handle, c.byref(overlapped), c.byref(transferred), True)
                    if wait == 258:
                        raise TimeoutError("named pipe I/O timed out; request outcome may be unknown")
                    raise c.WinError(wait_error)
                if not k.GetOverlappedResult(self._handle, c.byref(overlapped), c.byref(transferred), False):
                    raise c.WinError(c.get_last_error())
            return buffer.raw[:transferred.value] if reading else transferred.value
        finally:
            k.CloseHandle(event)

    def read(self, size: int, timeout: float) -> bytes:
        return self._io(size, timeout, reading=True)

    def write(self, data: bytes, timeout: float) -> int:
        return self._io(data, timeout, reading=False)

    def close(self) -> None:
        if self._handle is not None:
            self._kernel.CloseHandle(self._handle)
            self._handle = None


def _integer(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def plant(type: int, row: int, col: int) -> dict[str, Any]:
    """Create an action. Plant type is the numeric AvZ plant enum, not a seed slot."""
    return {"op": "plant", "type": _integer(type, "type"),
            "row": _integer(row, "row", 1), "col": _integer(col, "col", 1)}


def shovel(row: int, col: int, target_type: int = -1) -> dict[str, Any]:
    return {"op": "shovel", "row": _integer(row, "row", 1),
            "col": _integer(col, "col", 1), "target_type": _integer(target_type, "target_type", -1)}


def _version(value: Mapping[str, Any]) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ProtocolError("observation version must be an object")
    try:
        return {name: _integer(value[name], name) for name in ("epoch", "tick", "revision")}
    except (KeyError, ValueError) as error:
        raise ProtocolError(f"invalid observation version: {error}") from error


def _completed_step(method: str, result: dict[str, Any], *, engine_calls: bool = False) -> None:
    if method not in {"commit", "advance"}:
        return
    try:
        requested = _integer(result["requested_ticks"], "requested_ticks")
        executed = _integer(result["executed_ticks"], "executed_ticks")
        if executed > requested or not isinstance(result["stop_reason"], str):
            raise ValueError("invalid step count or stop reason")
        if not isinstance(result["observation"], dict):
            raise ValueError("missing completed observation")
        if method == "commit" and not isinstance(result["action_results"], list):
            raise ValueError("missing action results")
        if engine_calls:
            calls = _integer(result["executed_engine_calls"], "executed_engine_calls")
            zero = _integer(result["terminal_zero_clock_calls"], "terminal_zero_clock_calls")
            last = result["last_engine_call_id"]
            if zero not in (0, 1) or calls != executed + zero or calls > requested:
                raise ValueError("engine call counts differ from measured progress")
            if (calls == 0 and last is not None) or (calls > 0 and (type(last) is not int or not 1 <= last <= 0xffffffffffffffff)):
                raise ValueError("missing or invented last engine call ID")
            terminal_kind = result.get("terminal_kind")
            if zero and (executed >= requested or result["stop_reason"] != "scene_changed" or terminal_kind != "terminal_zero_clock_update"):
                raise ValueError("zero-clock call is not a completed terminal with remaining budget")
            if result["stop_reason"] == "scene_changed" and not zero and (calls == 0 or terminal_kind != "terminal_clock_step"):
                raise ValueError("terminal response lacks its measured call kind")
            if result["stop_reason"] != "scene_changed" and terminal_kind is not None:
                raise ValueError("nonterminal response claims a terminal call")
    except (KeyError, ValueError) as error:
        raise ProtocolError(f"incomplete {method} response: {error}") from error


class Client:
    def __init__(self, transport: Transport, *, trace: SessionTrace, timeout: float = 15.0,
                 expected_branch: str | None = None):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.transport, self.trace, self.timeout = transport, trace, timeout
        self._expected_branch = None if expected_branch is None else branch_scope_id(expected_branch)
        self._branch_scope: dict[str, Any] | None = None
        self._branch_id: str | None = None
        self._mutex = threading.RLock()
        self._request_ids: set[str] = set()
        self._last_observation: dict[str, Any] | None = None
        self._closed = False
        self.hello_result: dict[str, Any] | None = None

    @property
    def observation(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._last_observation)

    @property
    def version(self) -> dict[str, int] | None:
        return None if self._last_observation is None else _version(self._last_observation["version"])

    @property
    def branch_scope(self) -> dict[str, Any] | None:
        """The negotiated scope of the runtime this client is bound to."""
        return None if self._branch_scope is None else copy.deepcopy(self._branch_scope)

    @property
    def branch_id(self) -> str | None:
        """The branch id stamped on requests, or None for a pre-branch runtime."""
        return self._branch_id

    def _adopt_branch_scope(self, hello: dict[str, Any], *, enforce_expected: bool = True) -> None:
        scope = declared_branch_scope(hello)
        if enforce_expected and self._expected_branch is not None:
            if scope is None or scope["branch_id"] is None:
                raise ProtocolError("runtime does not declare a branch scope; the expected branch cannot be verified")
            if scope["branch_id"] != self._expected_branch:
                raise ProtocolError(f"runtime owns branch {scope['branch_id']}, not the expected {self._expected_branch}")
        self._branch_scope = scope
        self._branch_id = None if scope is None else scope["branch_id"]

    def request(self, method: str, params: Mapping[str, Any] | None = None, *,
                expect: Mapping[str, Any] | None = None, request_id: str | None = None,
                timeout: float | None = None) -> dict[str, Any]:
        with self._mutex:
            if self._closed:
                raise ConnectionError("client is closed")
            if not isinstance(method, str) or not method:
                raise ValueError("method must be a nonempty string")
            # New protocol methods may mutate; classify conservatively until known.
            controlled_capture = (method == "capture_frame" and self.hello_result is not None
                                  and draw_mode(self.hello_result) == DRAW_MODE)
            mutation = method not in READ_ONLY_METHODS and not controlled_capture
            if (method in {"commit", "advance", "prepare_render"} or controlled_capture) and expect is None:
                if self._last_observation is None:
                    self.observe()
                expect = self.version
            request = {"protocol": 1, "request_id": uuid.uuid4().hex if request_id is None else request_id,
                       "method": method, "params": dict(params or {})}
            if not isinstance(request["request_id"], str) or not request["request_id"]:
                raise ValueError("request_id must be a nonempty string")
            # The scope claim is not request content: the runtime strips it
            # before canonicalizing, so the same logical request stays identical
            # in every branch while still being bound to one namespace.
            if self._branch_id is not None:
                request["branch"] = self._branch_id
            if expect is not None:
                request["expect"] = _version(expect)
            if request["request_id"] in self._request_ids:
                raise ValueError("request ID already used by this client; requests are never automatically retried")
            payload = json.dumps(request, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
            if len(payload) > MAX_FRAME_BYTES:
                raise ValueError("request exceeds the 4 MiB frame limit")
            effective_timeout = self.timeout if timeout is None else timeout
            if effective_timeout <= 0:
                raise ValueError("timeout must be positive")
            self.trace.emit("request", request)
            self._request_ids.add(request["request_id"])
            try:
                raw = self.transport.exchange(payload, effective_timeout)
                try:
                    def invalid_constant(value: str) -> None:
                        raise ValueError(f"non-JSON numeric constant: {value}")
                    response = json.loads(raw.decode("utf-8"), parse_constant=invalid_constant)
                except (ValueError, UnicodeError) as error:
                    self.trace.emit("invalid_response", {"request_id": request["request_id"], "hex": raw.hex()})
                    raise ProtocolError("response is not UTF-8 JSON") from error
                trace_response = response
                if (method == "capture_frame" and isinstance(response, dict)
                        and isinstance(response.get("result"), dict)
                        and isinstance(response["result"].get("pixels_base64"), str)):
                    # Video pixels flow to the encoder; repeating their Base64
                    # in JSONL would cost gigabytes during a full experiment.
                    # Keep exact pixel evidence and all intervention metadata.
                    pixels = base64.b64decode(response["result"]["pixels_base64"], validate=True)
                    metadata = {key: value for key, value in response["result"].items() if key != "pixels_base64"}
                    metadata["pixels_evidence"] = {"sha256": hashlib.sha256(pixels).hexdigest(), "byte_length": len(pixels)}
                    metadata["trace_metadata_only"] = True
                    trace_response = {**response, "result": metadata}
                self.trace.emit("response", trace_response)
                if not isinstance(response, dict) or type(response.get("protocol")) is not int or response.get("protocol") != 1 or response.get("request_id") != request["request_id"]:
                    raise ProtocolError("response protocol or request_id mismatch")
                if response.get("ok") is False:
                    error = response.get("error")
                    if not isinstance(error, dict) or not all(isinstance(error.get(k), str) for k in ("code", "message")):
                        raise ProtocolError("invalid error response")
                    raise RemoteError(error, response)
                if response.get("ok") is not True or not isinstance(response.get("result"), dict):
                    raise ProtocolError("invalid success response")
                result = response["result"]
                _completed_step(method, result, engine_calls=(self.hello_result or {}).get("game", {}).get("engine_call_boundary", {}).get("mode") == "controlled_engine_call_v1")
                if controlled_capture:
                    if result.get("forced_render") is not False:
                        raise ProtocolError("controlled capture must not render")
                    if result.get("capture_ok") is True:
                        if (result.get("method") != "cached_controlled_engine_frame"
                                or result.get("mode") != DRAW_MODE
                                or _version(result.get("frame_version")) != request["expect"]
                                or _version(result.get("version")) != request["expect"]
                                or result.get("known_rng_unchanged") is not True):
                            raise ProtocolError("controlled capture did not return the exact cached boundary")
                observation = result if method == "observe" else result.get("observation")
                if observation is not None:
                    if not isinstance(observation, dict) or "version" not in observation:
                        raise ProtocolError("invalid observation")
                    _version(observation["version"])
                    self._last_observation = copy.deepcopy(observation)
                    self.trace.emit("observation", {"request_id": request["request_id"], "observation": observation})
                if method == "hello":
                    self._adopt_branch_scope(result)
                    self.hello_result = copy.deepcopy(result)
                return result
            except RemoteError as error:
                self.trace.emit("exception", {"request_id": request["request_id"], "type": type(error).__name__,
                                              "message": str(error), "outcome_unknown": False})
                if mutation:
                    self._last_observation = None
                raise
            except BaseException as error:
                # Protocol/transport failures break stream trust; force explicit reconnect.
                self.transport.close()
                self._closed = True
                self._last_observation = None
                self.trace.emit("exception", {"request_id": request["request_id"], "type": type(error).__name__,
                                              "message": str(error), "outcome_unknown": mutation})
                if mutation and isinstance(error, Exception):
                    raise OutcomeUnknown(request, error) from error
                raise

    def hello(self) -> dict[str, Any]:
        return self.request("hello")

    def branch_rebind(self, branch_id: str, *, parent_branch_id: str | None = None,
                      timeout: float | None = None) -> dict[str, Any]:
        """Move a cloned runtime to its own branch scope.

        A runtime cannot verify that a fork happened, so the request must name
        the scope it is leaving. Records already admitted keep their original
        scope, and this client stamps the new branch on later requests.
        """
        target = branch_scope_id(branch_id)
        if self._branch_id is None:
            raise ProtocolError("runtime does not declare a branch scope")
        params: dict[str, Any] = {"branch_id": target, "from_branch_id": self._branch_id}
        if parent_branch_id is not None:
            params["parent_branch_id"] = branch_scope_id(parent_branch_id)
        result = self.request("branch_rebind", params, timeout=timeout)
        if not isinstance(result.get("branch"), Mapping):
            raise ProtocolError("branch_rebind response lacks the runtime branch identity")
        self._adopt_branch_scope({"branch": result["branch"]}, enforce_expected=False)
        return result

    def observe(self) -> dict[str, Any]:
        return self.request("observe")

    def status(self) -> dict[str, Any]:
        return self.request("status")

    def prepare_render(self, *, expect: Mapping[str, Any] | None = None) -> dict[str, Any]:
        hello = self.hello_result if self.hello_result is not None else self.hello()
        if draw_mode(hello) != DRAW_MODE:
            raise ProtocolError("runtime does not support controlled render preparation")
        return self.request("prepare_render", {}, expect=expect)

    def capture_frame(self, *, expect: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Read cached pixels in new mode; legacy capture retains its old semantics."""
        if self.hello_result is None:
            self.hello()
        if expect is None:
            if self.version is None:
                self.observe()
            expect = self.version
        return self.request("capture_frame", {"format": "bgr24"}, expect=expect)

    def commit(self, actions: Sequence[Mapping[str, Any]], *, advance_ticks: int = 0,
               expect: Mapping[str, Any] | None = None, request_id: str | None = None,
               timeout: float | None = None) -> dict[str, Any]:
        if isinstance(actions, (str, bytes, Mapping)):
            raise ValueError("actions must be a sequence of action objects")
        return self.request("commit", {"actions": [dict(a) for a in actions],
                            "advance_ticks": _integer(advance_ticks, "advance_ticks")},
                            expect=expect, request_id=request_id, timeout=timeout)

    def advance(self, max_ticks: int, *, until: Mapping[str, Any] | None = None,
                expect: Mapping[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"max_ticks": _integer(max_ticks, "max_ticks")}
        if until is not None:
            if not isinstance(until, Mapping):
                raise TypeError("until must be a protocol condition object, not a callback")
            params["until"] = dict(until)
        return self.request("advance", params, expect=expect, timeout=timeout)

    def plant(self, type: int, row: int, col: int, **kwargs: Any) -> dict[str, Any]:
        return self.commit([plant(type, row, col)], **kwargs)

    def shovel(self, row: int, col: int, target_type: int = -1, **kwargs: Any) -> dict[str, Any]:
        return self.commit([shovel(row, col, target_type)], **kwargs)

    def pause(self, *, expect: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.request("pause", expect=expect)

    def cancel(self, *, expect: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.request("cancel", expect=expect)

    def close(self) -> None:
        with self._mutex:
            if not self._closed:
                self.transport.close()
                self._closed = True

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def connect(*, pid: int | None = None, endpoint: str | None = None,
            trace: SessionTrace, timeout: float = 15.0,
            expected_branch: str | None = None) -> Client:
    """Connect and negotiate hello. A supplied trace is owned by its caller.

    Pass a SessionTrace context manager explicitly. Closing a client closes
    only the transport, so the same audit file can record an explicit reconnect.
    ``expected_branch`` binds the connection to one runtime branch scope: a
    clone that forgot to rebind fails at hello instead of answering for another
    branch.
    """
    if (pid is None) == (endpoint is None):
        raise ValueError("provide exactly one of pid or endpoint")
    if pid is not None:
        endpoint = f"\\\\.\\pipe\\llm-vs-zombies-{_integer(pid, 'pid', 1)}"
    if not isinstance(trace, SessionTrace):
        raise TypeError("trace must be an open SessionTrace; use its context manager")
    transport = FramedTransport(WindowsNamedPipeStream(endpoint, timeout))
    client = Client(transport, trace=trace, timeout=timeout, expected_branch=expected_branch)
    try:
        client.hello()
    except BaseException:
        client.close()
        raise
    return client
