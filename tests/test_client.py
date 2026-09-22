import json
import base64
import copy
import hashlib
import os
import struct
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from llm_vs_zombies.client import (BRANCH_SCHEMA, Client, FramedTransport, MAX_FRAME_BYTES, OutcomeUnknown,
                                   ProtocolError, RemoteError, WindowsNamedPipeStream, branch_scope_id,
                                   declared_branch_scope, plant, shovel)
from llm_vs_zombies.evidence_tree import branch_id as evidence_branch_id
from llm_vs_zombies.session import SessionTrace


def observation(epoch=4, tick=2, revision=0):
    return {"version": {"epoch": epoch, "tick": tick, "revision": revision}, "game_clock": 100,
            "game_ui": 3, "scene": 4, "wave": 1, "sun": 50,
            "plants": [], "zombies": [], "seeds": []}


def branch(branch_id="branch-a", mode="branch", **overrides):
    return {"schema": BRANCH_SCHEMA, "mode": mode, "branch_id": branch_id,
            "parent_branch_id": None, "origin": "session",
            "dedup_key": "branch_id+epoch+request_id" if mode == "branch" else "epoch+request_id", **overrides}


class FakeRuntime:
    def __init__(self):
        self.requests = []
        self.closed = False
        self.response = None

    def exchange(self, payload, timeout):
        request = json.loads(payload)
        self.requests.append(request)
        if self.response:
            return self.response(request)
        return json.dumps({"protocol": 1, "request_id": request["request_id"], "ok": True,
                           "result": observation()}).encode()

    def close(self):
        self.closed = True


class BranchRuntime(FakeRuntime):
    """A runtime that answers hello with a declared branch scope."""

    def __init__(self, hello_branch=None):
        super().__init__()
        self.hello_branch = hello_branch

    def exchange(self, payload, timeout):
        request = json.loads(payload)
        self.requests.append(request)
        if self.response:
            return self.response(request)
        if request["method"] == "hello":
            result = {"build": {"runtime_protocol": 1}, "game": {"schema": "lvz.audit.v1"},
                      "capabilities": {"observe": True, "branch_scope_v1": True, "branch_rebind": True}}
            if self.hello_branch is not None:
                result["branch"] = copy.deepcopy(self.hello_branch)
        else:
            result = observation()
        return json.dumps({"protocol": 1, "request_id": request["request_id"], "ok": True, "result": result}).encode()


class FragmentedStream:
    def __init__(self, response):
        self.incoming = bytearray(response)
        self.written = bytearray()
        self.closed = False

    def read(self, size, timeout):
        size = min(size, 2)
        result = bytes(self.incoming[:size])
        del self.incoming[:size]
        return result

    def write(self, payload, timeout):
        size = min(3, len(payload))
        self.written.extend(payload[:size])
        return size

    def close(self):
        self.closed = True


class FramingTests(unittest.TestCase):
    def test_short_reads_and_writes_preserve_utf8_byte_lengths(self):
        reply = '{"text":"雾夜"}'.encode()
        stream = FragmentedStream(struct.pack("<I", len(reply)) + reply)
        request = '{"text":"两仪"}'.encode()
        self.assertEqual(FramedTransport(stream).exchange(request, 1), reply)
        self.assertEqual(stream.written, struct.pack("<I", len(request)) + request)

    def test_disconnect_midframe_invalidates_transport(self):
        stream = FragmentedStream(struct.pack("<I", 10) + b"short")
        transport = FramedTransport(stream)
        with self.assertRaises(EOFError):
            transport.exchange(b"{}", 1)
        self.assertTrue(stream.closed)
        with self.assertRaises(ConnectionError):
            transport.exchange(b"{}", 1)

    def test_oversized_frame_rejected_before_body_read(self):
        stream = FragmentedStream(struct.pack("<I", MAX_FRAME_BYTES + 1))
        with self.assertRaises(ProtocolError):
            FramedTransport(stream).exchange(b"{}", 1)
        self.assertTrue(stream.closed)

    def test_total_deadline_does_not_restart_per_fragment(self):
        class SlowStream(FragmentedStream):
            def read(self, size, timeout):
                time.sleep(min(timeout, 0.015))
                return super().read(size, timeout)
        stream = SlowStream(struct.pack("<I", 20) + b"x" * 20)
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            FramedTransport(stream).exchange(b"{}", 0.035)
        self.assertLess(time.monotonic() - start, 0.2)
        self.assertTrue(stream.closed)


class BranchScopeTests(unittest.TestCase):
    """Issue #32: one runtime instance owns one request/dedup namespace."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.trace = SessionTrace(Path(self.tmp.name) / "trace.jsonl")

    def tearDown(self):
        self.trace.close()
        self.tmp.cleanup()

    def client(self, transport, **kwargs):
        client = Client(transport, trace=self.trace, **kwargs)
        self.addCleanup(client.close)
        return client

    def test_runtime_branch_is_learned_and_stamped_on_later_requests(self):
        transport = BranchRuntime(branch())
        client = self.client(transport)
        client.hello()
        self.assertEqual(client.branch_id, "branch-a")
        self.assertEqual(client.branch_scope["dedup_key"], "branch_id+epoch+request_id")
        self.assertEqual(client.branch_scope["origin"], "session")
        client.observe()
        self.assertIsNone(transport.requests[0].get("branch"))
        self.assertEqual(transport.requests[1]["branch"], "branch-a")

    def test_pre_branch_runtime_keeps_the_old_unstamped_contract(self):
        transport = BranchRuntime(None)
        client = self.client(transport)
        client.hello()
        self.assertIsNone(client.branch_scope)
        self.assertIsNone(client.branch_id)
        client.observe()
        self.assertNotIn("branch", transport.requests[-1])

    def test_expected_branch_mismatch_fails_at_hello(self):
        with self.assertRaises(ProtocolError):
            self.client(BranchRuntime(branch("branch-b")), expected_branch="branch-a").hello()
        with self.assertRaises(ProtocolError):
            self.client(BranchRuntime(None), expected_branch="branch-a").hello()
        matching = self.client(BranchRuntime(branch("branch-a")), expected_branch="branch-a")
        self.assertEqual(matching.hello()["branch"]["branch_id"], "branch-a")

    def test_cross_branch_error_is_surfaced_without_automatic_retry(self):
        transport = BranchRuntime(branch())
        client = self.client(transport)
        client.hello()
        transport.response = lambda request: json.dumps({"protocol": 1, "request_id": request["request_id"],
            "ok": False, "error": {"code": "cross_branch_request_id_conflict",
                                   "message": "Request ID is already bound to different content in branch parent-a"}}).encode()
        with self.assertRaises(RemoteError) as caught:
            client.commit([plant(8, 2, 5)], advance_ticks=0)
        self.assertEqual(caught.exception.code, "cross_branch_request_id_conflict")
        self.assertIn("parent-a", str(caught.exception))
        self.assertEqual(len(transport.requests), 2)

    def test_rebind_moves_the_client_to_the_new_scope(self):
        transport = BranchRuntime(branch())
        client = self.client(transport)
        client.hello()
        seen = {}

        def rebind(request):
            seen.update(request["params"])
            return json.dumps({"protocol": 1, "request_id": request["request_id"], "ok": True,
                "result": {"rebound": True, "previous_branch_id": "branch-a",
                           "branch": branch("branch-b", origin="rebound", parent_branch_id="branch-a")}}).encode()
        transport.response = rebind
        result = client.branch_rebind("branch-b", parent_branch_id="branch-a")
        self.assertTrue(result["rebound"])
        self.assertEqual(seen, {"branch_id": "branch-b", "from_branch_id": "branch-a", "parent_branch_id": "branch-a"})
        self.assertEqual(client.branch_id, "branch-b")
        transport.response = None
        client.observe()
        self.assertEqual(transport.requests[-1]["branch"], "branch-b")

    def test_rebind_needs_a_declared_scope_and_a_valid_id(self):
        client = self.client(BranchRuntime(None))
        client.hello()
        with self.assertRaises(ProtocolError):
            client.branch_rebind("branch-b")
        with self.assertRaises(ValueError):
            self.client(BranchRuntime(branch()), expected_branch="not a branch")

    def test_malformed_hello_scopes_are_rejected(self):
        for hello in ({"schema": BRANCH_SCHEMA, "mode": "branch"},
                      {"schema": BRANCH_SCHEMA, "mode": "unscoped", "branch_id": "branch-a"},
                      {"schema": BRANCH_SCHEMA, "mode": "invented", "branch_id": "branch-a"},
                      {"schema": "lvz.other.v1", "mode": "branch", "branch_id": "branch-a"},
                      {"schema": BRANCH_SCHEMA, "mode": "branch", "branch_id": "bad id"}):
            with self.assertRaises(ProtocolError):
                self.client(BranchRuntime(hello)).hello()

    def test_protocol_and_evidence_branch_grammars_agree(self):
        samples = ["a", "A-1_b.c:d", "0" * 64, "x" * 65, "", "-a", "a b", "a/b", "雾夜", "branch:2-3"]
        accepted = {}
        for name, validator in (("protocol", branch_scope_id), ("evidence", evidence_branch_id)):
            accepted[name] = {value for value in samples if _accepts(validator, value)}
        self.assertEqual(accepted["protocol"], accepted["evidence"])
        self.assertIn("branch:2-3", accepted["protocol"])
        self.assertIn("0" * 64, accepted["protocol"])


def _accepts(validator, value):
    try:
        validator(value)
        return True
    except Exception:
        return False


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "trace.jsonl"
        self.trace = SessionTrace(self.path)
        self.transport = FakeRuntime()
        self.client = Client(self.transport, trace=self.trace)

    def tearDown(self):
        self.client.close()
        self.trace.close()
        self.tmp.cleanup()

    def events(self):
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def test_capture_pixels_reach_encoder_without_expanding_decision_log(self):
        pixels = bytes(range(256)) * 5625
        encoded = base64.b64encode(pixels).decode("ascii")
        self.transport.response = lambda request: json.dumps({"protocol": 1, "request_id": request["request_id"], "ok": True,
            "result": {"capture_ok": True, "pixels_base64": encoded, "known_rng_unchanged": True}}).encode()
        result = self.client.request("capture_frame", {"format": "bgr24"}, expect=observation()["version"])
        self.assertEqual(base64.b64decode(result["pixels_base64"]), pixels)
        recorded = [event for event in self.events() if event["kind"] == "response"][0]["data"]["result"]
        self.assertNotIn("pixels_base64", recorded)
        self.assertEqual(recorded["pixels_evidence"], {"sha256": hashlib.sha256(pixels).hexdigest(), "byte_length": len(pixels)})
        self.assertTrue(recorded["known_rng_unchanged"])
        self.assertLess(self.path.stat().st_size, 2048)

    def test_implicit_expect_and_actual_failed_actions_are_retained(self):
        self.client.observe()
        def fail_second(request):
            return json.dumps({"protocol": 1, "request_id": request["request_id"], "ok": True,
                "result": {"action_results": [{"ok": True}, {"ok": False, "reason": "unusable_seed"}],
                           "requested_ticks": 9, "executed_ticks": 0, "stop_reason": "action_failed",
                           "observation": observation(revision=1)}}).encode()
        self.transport.response = fail_second
        result = self.client.commit([shovel(2, 5), plant(8, 2, 5)], advance_ticks=9)
        self.assertEqual(self.transport.requests[-1]["expect"], {"epoch": 4, "tick": 2, "revision": 0})
        self.assertFalse(result["action_results"][1]["ok"])
        self.assertEqual(self.client.version["revision"], 1)
        result["observation"]["version"]["revision"] = 999
        self.assertEqual(self.client.version["revision"], 1)
        actual = [e for e in self.events() if e["kind"] == "response"][-1]
        self.assertFalse(actual["data"]["result"]["action_results"][1]["ok"])

    def test_timeout_is_unknown_outcome_without_retry(self):
        self.client.observe()
        def timeout(request):
            raise TimeoutError("lost acknowledgement")
        self.transport.response = timeout
        with self.assertRaises(OutcomeUnknown) as context:
            self.client.plant(8, 2, 5, request_id="action-1")
        self.assertEqual(context.exception.request_id, "action-1")
        self.assertEqual(len(self.transport.requests), 2)
        self.assertTrue(self.transport.closed)
        with self.assertRaises(ConnectionError):
            self.client.plant(8, 2, 5)
        self.assertEqual(len(self.transport.requests), 2)
        self.assertTrue(self.events()[-1]["data"]["outcome_unknown"])

    def test_server_epoch_change_is_not_hidden_or_retried(self):
        self.client.observe()
        def stale(request):
            return json.dumps({"protocol": 1, "request_id": request["request_id"], "ok": False,
                "error": {"code": "stale_version", "message": "epoch changed"}}).encode()
        self.transport.response = stale
        with self.assertRaises(RemoteError) as context:
            self.client.advance(1)
        self.assertEqual(context.exception.code, "stale_version")
        self.assertIsNone(self.client.version)
        self.assertEqual(len(self.transport.requests), 2)

    def test_mismatched_response_id_invalidates_client(self):
        self.transport.response = lambda request: b'{"protocol":1,"request_id":"someone-else","ok":true,"result":{}}'
        with self.assertRaises(ProtocolError):
            self.client.observe()
        self.assertTrue(self.transport.closed)

    def test_request_ids_cannot_be_reused_even_after_success(self):
        self.client.request("observe", request_id="fixed")
        with self.assertRaises(ValueError):
            self.client.request("observe", request_id="fixed")
        self.assertEqual(len(self.transport.requests), 1)

    def test_pause_and_cancel_do_not_require_preliminary_observe(self):
        self.transport.response = lambda request: json.dumps({"protocol": 1, "request_id": request["request_id"],
            "ok": True, "result": {"state": "paused"}}).encode()
        self.client.pause()
        self.client.cancel()
        self.assertEqual([r["method"] for r in self.transport.requests], ["pause", "cancel"])
        self.assertNotIn("expect", self.transport.requests[0])

    def test_complete_response_not_acceptance_and_invalid_versions(self):
        self.transport.response = lambda request: json.dumps({"protocol": 1, "request_id": request["request_id"],
            "ok": True, "result": {"version": {"epoch": True, "tick": 0, "revision": 0}}}).encode()
        with self.assertRaises(ProtocolError):
            self.client.observe()

    def test_queue_acceptance_is_not_successful_completion(self):
        self.client.observe()
        self.transport.response = lambda request: json.dumps({"protocol": 1, "request_id": request["request_id"],
            "ok": True, "result": {"accepted": True}}).encode()
        with self.assertRaises(OutcomeUnknown):
            self.client.advance(1)
        self.assertTrue(self.transport.closed)

    def test_action_validation_and_callback_until_are_local(self):
        with self.assertRaises(ValueError):
            plant(True, 2, 5)
        with self.assertRaises(ValueError):
            shovel(0, 3)
        with self.assertRaises(TypeError):
            self.client.advance(1, until=lambda obs: True)
        self.assertEqual(self.transport.requests, [])


@unittest.skipUnless(os.name == "nt", "requires real Windows named pipe I/O")
class WindowsPipeTests(unittest.TestCase):
    def server(self, callback):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
        kernel.CreateNamedPipeW.restype = wintypes.HANDLE
        kernel.ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
        kernel.ConnectNamedPipe.restype = wintypes.BOOL
        kernel.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        kernel.ReadFile.restype = wintypes.BOOL
        kernel.WriteFile.argtypes = kernel.ReadFile.argtypes
        kernel.WriteFile.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        endpoint = r"\\.\pipe\llm-vs-zombies-test-" + uuid.uuid4().hex
        handle = kernel.CreateNamedPipeW(endpoint, 3, 0, 1, 65536, 65536, 0, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        errors = []
        def run():
            try:
                if not kernel.ConnectNamedPipe(handle, None) and ctypes.get_last_error() != 535:
                    raise ctypes.WinError(ctypes.get_last_error())
                def read(size):
                    buffer, count = ctypes.create_string_buffer(size), wintypes.DWORD()
                    if not kernel.ReadFile(handle, buffer, size, ctypes.byref(count), None):
                        raise ctypes.WinError(ctypes.get_last_error())
                    return buffer.raw[:count.value]
                def write(data):
                    count = wintypes.DWORD()
                    if not kernel.WriteFile(handle, data, len(data), ctypes.byref(count), None):
                        raise ctypes.WinError(ctypes.get_last_error())
                callback(read, write)
            except BaseException as error:
                errors.append(error)
            finally:
                kernel.CloseHandle(handle)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return endpoint, thread, errors

    def test_real_windows_fragmented_exchange(self):
        request_payload = b'{"method":"hello"}'
        response_payload = '{"reply":"测试"}'.encode()
        def serve(read, write):
            expected = struct.pack("<I", len(request_payload)) + request_payload
            received = bytearray()
            while len(received) < len(expected):
                received.extend(read(len(expected) - len(received)))
            self.assertEqual(received, expected)
            reply = struct.pack("<I", len(response_payload)) + response_payload
            for byte in reply:
                write(bytes([byte]))
        endpoint, thread, errors = self.server(serve)
        transport = FramedTransport(WindowsNamedPipeStream(endpoint, 1))
        try:
            self.assertEqual(transport.exchange(request_payload, 2), response_payload)
        finally:
            transport.close()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_real_windows_timeout_cancels_pending_read(self):
        release = threading.Event()
        def serve(read, write):
            read(100)
            release.wait(2)
        endpoint, thread, errors = self.server(serve)
        transport = FramedTransport(WindowsNamedPipeStream(endpoint, 1))
        start = time.monotonic()
        try:
            with self.assertRaises(TimeoutError):
                transport.exchange(b"{}", 0.06)
            self.assertTrue(transport.closed)
            self.assertLess(time.monotonic() - start, 0.5)
        finally:
            release.set()
            transport.close()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
