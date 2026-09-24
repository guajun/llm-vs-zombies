"""Held-HWND identity and the private-launch claim; no game is started.

The tracker is exercised through an injectable read-only query surface, and one
real Windows fixture creates an invisible top-level window in a child process so
the real discovery scan is covered too. The fixture never shows or activates a
window; it asserts IsWindowVisible is false.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from llm_vs_zombies import window_observer as observer


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeWindows:
    """Injectable read-only query surface; no window is created or touched."""

    def __init__(self):
        self.windows = {}
        self.foreground_handle = 0
        self.foreground_owner = None
        self.queries = {"foreground": 0, "enumerate": 0, "alive": 0, "visible": 0, "owner": 0}

    def add(self, handle, pid, *, visible=False):
        self.windows[handle] = {"pid": pid, "visible": visible}
        return handle

    def foreground(self):
        self.queries["foreground"] += 1
        return self.foreground_handle, self.foreground_owner, True

    def owned_windows(self, pid):
        self.queries["enumerate"] += 1
        return sorted(handle for handle, window in self.windows.items() if window["pid"] == pid)

    def alive(self, handle):
        self.queries["alive"] += 1
        return handle in self.windows

    def owner_pid(self, handle):
        self.queries["owner"] += 1
        window = self.windows.get(handle)
        return None if window is None else window["pid"]

    def visible(self, handle):
        self.queries["visible"] += 1
        window = self.windows.get(handle)
        return bool(window and window["visible"])


class WindowTrackerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.windows = FakeWindows()
        self.windows.add(100, 42)
        self.tracker = observer.WindowTracker(self.windows, 42, scan_interval_seconds=1.0, clock=self.clock)

    def test_discovery_runs_once_then_every_sample_only_verifies_held_handles(self):
        self.assertEqual(self.tracker.sample(), [{"handle": 100, "visible": False}])
        self.assertEqual(self.windows.queries["enumerate"], 1)
        for _ in range(20):
            self.clock.advance(0.025)
            self.assertEqual(self.tracker.sample(), [{"handle": 100, "visible": False}])
        self.assertEqual(self.windows.queries["enumerate"], 1)
        self.assertEqual(self.windows.queries["owner"], 21)
        self.assertEqual(self.windows.queries["visible"], 21)
        self.assertEqual(self.tracker.diagnostics["scans"], 1)
        self.assertEqual(self.tracker.diagnostics["verifications"], 21)

    def test_every_sample_scans_while_nothing_of_the_target_is_held(self):
        # The launch phase: the game window exists only after resume, so the old
        # per-sample discovery guarantee is kept until the window is found.
        empty = observer.WindowTracker(self.windows, 99, scan_interval_seconds=1.0, clock=self.clock)
        for _ in range(5):
            self.clock.advance(0.025)
            self.assertEqual(empty.sample(), [])
        self.assertEqual(self.windows.queries["enumerate"], 5)
        self.windows.add(300, 99)
        self.assertEqual(empty.sample(), [{"handle": 300, "visible": False}])
        self.clock.advance(0.025)
        self.assertEqual(empty.sample(), [{"handle": 300, "visible": False}])
        self.assertEqual(self.windows.queries["enumerate"], 6)

    def test_periodic_backstop_discovers_a_window_created_after_discovery(self):
        self.tracker.sample()
        self.windows.add(200, 42)
        self.clock.advance(0.5)
        self.assertEqual(self.tracker.sample(), [{"handle": 100, "visible": False}])
        self.clock.advance(0.6)
        self.assertEqual(self.tracker.sample(), [{"handle": 100, "visible": False},
                                                 {"handle": 200, "visible": False}])
        self.assertEqual(self.tracker.diagnostics["scans"], 2)
        self.assertEqual(self.tracker.diagnostics["discovered"], 2)

    def test_destroyed_handle_is_dropped_instead_of_reported(self):
        self.tracker.sample()
        del self.windows.windows[100]
        self.assertEqual(self.tracker.sample(), [])
        self.assertEqual(self.tracker.handles, [])
        self.assertEqual(self.tracker.diagnostics["dropped_destroyed"], 1)

    def test_handle_reused_by_another_process_is_dropped_and_never_enters_evidence(self):
        self.tracker.sample()
        self.windows.windows[100] = {"pid": 7, "visible": True}
        self.assertEqual(self.tracker.sample(), [])
        self.assertEqual(self.tracker.diagnostics["dropped_reused"], 1)
        probe = observer.window_probe(42, tracker=self.tracker)
        self.assertEqual(probe["owned_windows"], [])

    def test_a_replacement_window_is_found_in_the_same_sample_that_lost_the_old_one(self):
        self.tracker.sample()
        del self.windows.windows[100]
        self.windows.add(200, 42)
        self.assertEqual(self.tracker.sample(), [{"handle": 200, "visible": False}])
        self.assertEqual(self.tracker.diagnostics["dropped_destroyed"], 1)
        self.assertEqual(self.tracker.diagnostics["discovered"], 2)

    def test_another_processes_windows_are_never_enumerated_into_our_sample(self):
        self.tracker.sample()
        for handle in range(500, 520):
            self.windows.add(handle, 7, visible=True)
        self.windows.foreground_handle, self.windows.foreground_owner = 505, 7
        probe = observer.window_probe(42, tracker=self.tracker)
        self.assertEqual(probe["owned_windows"], [{"handle": 100, "visible": False}])
        self.assertEqual(probe["foreground"], 505)
        self.assertEqual(probe["foreground_pid"], 7)

    def test_forced_scan_is_used_for_the_sample_after_a_stop_request(self):
        self.tracker.sample()
        self.windows.add(200, 42)
        self.assertEqual(observer.window_probe(42, tracker=self.tracker, force_scan=True)["owned_windows"],
                         [{"handle": 100, "visible": False}, {"handle": 200, "visible": False}])


class PrivateLaunchClaimTests(unittest.TestCase):
    """The verdict rests on our own windows, never on another process's behaviour."""

    @staticmethod
    def sample(index, handle=0, owner=None, *, handles=((100, False),), resolved=True):
        """One valid sample: a foreground handle with its owning PID, or 0/None."""
        return {"seq": index, "monotonic_seconds": 1.0 + index * 0.025,
                "probe_finished_seconds": 1.001 + index * 0.025,
                "foreground": handle, "foreground_pid": owner, "foreground_resolved": resolved,
                "owned_windows": [{"handle": item, "visible": visible} for item, visible in handles]}

    def test_our_own_window_in_the_foreground_fails_even_when_no_pid_matches(self):
        # The sampled foreground handle is one we hold, while its PID read names
        # another process: the handle identity is our own evidence, so it fails.
        evidence = observer.launch_window_evidence(
            [self.sample(0, 100, 7), self.sample(1, 100, 7), self.sample(2, 100, 7)], 42)
        self.assertTrue(evidence["foreground_unchanged"])
        self.assertTrue(evidence["our_window_foreground_observed"])
        self.assertFalse(evidence["game_foreground_observed"])
        self.assertEqual(evidence["foreground_check_status"], "fail")

    def test_our_process_in_the_foreground_fails_through_the_foreign_handle_rule(self):
        evidence = observer.launch_window_evidence(
            [self.sample(0, 0, None), self.sample(1, 20, 42), self.sample(2, 20, 42)], 42)
        self.assertTrue(evidence["game_foreground_observed"])
        self.assertFalse(evidence["our_window_foreground_observed"])
        self.assertEqual(evidence["foreground_check_status"], "fail")

    def test_another_processes_window_churn_never_changes_the_conclusion(self):
        evidence = observer.launch_window_evidence(
            [self.sample(0, 10, 100), self.sample(1, 20, 200), self.sample(2, 30, 300)], 42)
        self.assertFalse(evidence["foreground_unchanged"])
        self.assertFalse(evidence["foreground_change_used_as_evidence"])
        self.assertEqual(evidence["claims"]["global_foreground_equality_is_evidence"], False)
        self.assertEqual(evidence["claims"]["another_processes_window_activity_is_evidence"], False)
        self.assertTrue(evidence["claims"]["our_window_never_foreground"])
        self.assertEqual(evidence["foreground_check_status"], "pass")

    def test_a_visible_intermediate_owned_window_is_not_erased_by_a_later_hidden_sample(self):
        evidence = observer.launch_window_evidence(
            [self.sample(0, 10, 100), self.sample(1, 10, 100), self.sample(2, 10, 100)], 42)
        self.assertEqual(evidence["foreground_check_status"], "pass")
        visible = observer.launch_window_evidence([self.sample(0, 10, 100),
                                                   self.sample(1, 10, 100, handles=((100, True),)),
                                                   self.sample(2, 10, 100)], 42)
        self.assertTrue(visible["our_window_visible_observed"])
        self.assertEqual(visible["foreground_check_status"], "fail")

    def test_gap_and_probe_limits_are_unchanged_and_exactly_bounded(self):
        exact = [self.sample(0, 0, None), self.sample(10, 0, None)]
        exact[1]["monotonic_seconds"], exact[1]["probe_finished_seconds"] = 1.25, 1.251
        evidence = observer.launch_window_evidence(exact, 42)
        self.assertEqual(evidence["sampling"]["allowed_maximum_gap_seconds"], 0.25)
        self.assertEqual(evidence["sampling"]["maximum_gap_seconds"], 0.25)
        self.assertTrue(evidence["sampling"]["complete"])
        self.assertEqual(evidence["foreground_check_status"], "pass")
        over = json.loads(json.dumps(exact))
        over[1]["monotonic_seconds"] = 1.2501
        over[1]["probe_finished_seconds"] = 1.2511
        evidence = observer.launch_window_evidence(over, 42)
        self.assertFalse(evidence["sampling"]["complete"])
        self.assertEqual(evidence["foreground_check_status"], "unverified")

    def test_probe_error_or_unresolved_foreground_still_cannot_pass(self):
        failed = [self.sample(0, 0, None), self.sample(1, 0, None, handles=())]
        failed[1]["error"] = "WinError: probe failed"
        self.assertEqual(observer.launch_window_evidence(failed, 42)["foreground_check_status"], "unverified")
        unresolved = [self.sample(0, 10, None, resolved=False), self.sample(1, 10, None, resolved=False)]
        self.assertEqual(observer.launch_window_evidence(unresolved, 42)["foreground_check_status"], "unverified")


HIDDEN_WINDOW_CHILD = r'''
import ctypes, sys, time
from ctypes import wintypes
user = ctypes.WinDLL("user32", use_last_error=True)
procedure_type = ctypes.WINFUNCTYPE(wintypes.LPARAM, wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM)
user.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user.DefWindowProcW.restype = wintypes.LPARAM

def procedure(window, message, wparam, lparam):
    return user.DefWindowProcW(window, message, wparam, lparam)

callback = procedure_type(procedure)

class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", procedure_type),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

user.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
    wintypes.HINSTANCE, wintypes.LPVOID]
user.CreateWindowExW.restype = wintypes.HWND
name = "lvz-hidden-window-fixture"
window_class = WNDCLASSW(0, callback, 0, 0, None, None, None, None, None, name)
if not user.RegisterClassW(ctypes.byref(window_class)):
    raise SystemExit("RegisterClassW failed: %d" % ctypes.get_last_error())
time.sleep(float(sys.argv[1]))
# WS_OVERLAPPEDWINDOW without WS_VISIBLE: a real top-level window that never shows.
window = user.CreateWindowExW(0, name, "lvz-fixture", 0x00CF0000, 0, 0, 320, 240,
                              None, None, None, None)
if not window:
    raise SystemExit("CreateWindowExW failed: %d" % ctypes.get_last_error())
print(int(window), flush=True)
time.sleep(float(sys.argv[2]))
'''


@unittest.skipUnless(os.name == "nt", "a real hidden top-level window requires Windows")
class RealWindowsDiscoveryTests(unittest.TestCase):
    def test_a_new_hidden_window_is_discovered_by_the_scan_and_then_held(self):
        child = subprocess.Popen([sys.executable, "-u", "-c", HIDDEN_WINDOW_CHILD, "0.05", "20"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            line = child.stdout.readline().decode().strip()
            self.assertTrue(line.isdigit(), line)
            handle = int(line)
            backend = observer.WindowBackend()
            self.assertFalse(backend.visible(handle), "the fixture window must never be visible")
            tracker = observer.WindowTracker(backend, child.pid, scan_interval_seconds=60.0)
            observed = tracker.sample()
            self.assertIn({"handle": handle, "visible": False}, observed)
            scans = tracker.diagnostics["scans"]
            self.assertIn({"handle": handle, "visible": False}, tracker.sample())
            self.assertEqual(tracker.diagnostics["scans"], scans)
            self.assertTrue(backend.alive(handle))
        finally:
            child.kill()
            _, stderr = child.communicate(timeout=10)
            self.assertEqual(stderr, b"", stderr.decode())

    def test_the_observer_passes_a_real_process_that_only_ever_has_a_hidden_window(self):
        # The closest offline proxy for a launch/runtime observation: a real
        # process with one top-level window that is never shown or activated.
        temporary = tempfile.TemporaryDirectory()
        try:
            child = subprocess.Popen([sys.executable, "-u", "-c", HIDDEN_WINDOW_CHILD, "0.0", "20"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                handle = int(child.stdout.readline().decode().strip())
                monitor = observer.LaunchWindowMonitor(pid=child.pid,
                    evidence_directory=Path(temporary.name) / "evidence")
                with monitor:
                    if not os.name == "nt":  # pragma: no cover - skipUnless guards this
                        raise unittest.SkipTest("live observer requires Windows")
                    time.sleep(0.3)
                result = monitor.result(child.pid)
                self.assertEqual(monitor.process.returncode, 0)
                self.assertEqual(result["foreground_check_status"], "pass", result["sampling"]["errors"])
                self.assertTrue(result["sampling"]["complete"], result["sampling"])
                self.assertIn({"handle": handle, "visible": False}, result["after"]["owned_windows"])
                self.assertGreaterEqual(result["observer"]["window_discovery"]["scans"], 1)
                self.assertTrue(result["claims"]["our_window_never_visible"])
                self.assertTrue(result["claims"]["our_window_never_foreground"])
            finally:
                child.kill()
                _, stderr = child.communicate(timeout=10)
                self.assertEqual(stderr, b"", stderr.decode())
        finally:
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
