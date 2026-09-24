"""Read-only window observation in an owned, console-free subprocess.

Executable as a file so Windows child startup never imports a game driver.
All sampling evidence is retained; errors and gaps never become successful proof.

Cross-process control is not a rewritten shared file. The parent publishes
``control.json`` and ``target.json`` exactly once - a single rename onto a name
that does not exist yet, so a reader can only observe the document absent or
complete - and the stop request travels over a named Windows event object. Every
read-side wait stays bounded and a failure names the component that failed.

Sampling holds window identities instead of re-enumerating the whole desktop on
every tick: the target's top-level HWNDs are discovered by a scan and then
re-verified with IsWindow/GetWindowThreadProcessId/IsWindowVisible, with the
full scan repeated while nothing is held, on request and as a slow backstop.
Issue #94 (per-tick coverage of granted tick ranges) is deliberately untouched;
the per-sample ``seq``/timestamps remain the pairing cursor for that work.
"""
from __future__ import annotations

import argparse
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

SCHEMA = "lvz.window-observer.v1"
MAX_SAMPLES = 4_000_000
MAX_BYTES = 2 * 1024**3
MAX_DURATION = 86400.0
SCAN_INTERVAL_SECONDS = 1.0
STOP_CHANNEL_PREFIX = "Local\\lvz-window-observer-stop-"
SYNCHRONIZE = 0x00100000
EVENT_MODIFY_STATE = 0x0002
ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
# A name that is being published can transiently deny or miss an open:
# ERROR_ACCESS_DENIED / ERROR_SHARING_VIOLATION / ERROR_LOCK_VIOLATION.
TRANSIENT_WINDOWS_ERRORS = (5, 32, 33)
PUBLISH_READ_ATTEMPTS = 8
PUBLISH_READ_SECONDS = 0.5
PUBLISH_READ_INTERVAL = 0.005


class WindowBackend:
    """Real read-only Win32 queries; never activates, shows, hides or sends input."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.wintypes = ctypes, wintypes
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        user = self.user
        user.GetForegroundWindow.restype = wintypes.HWND
        user.IsWindow.argtypes = [wintypes.HWND]
        user.IsWindow.restype = wintypes.BOOL
        user.IsWindowVisible.argtypes = [wintypes.HWND]
        user.IsWindowVisible.restype = wintypes.BOOL
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user.GetWindowThreadProcessId.restype = wintypes.DWORD

    def foreground(self):
        """Query foreground ownership; an unresolved window is reported, never guessed."""
        ctypes, wintypes = self.ctypes, self.wintypes
        user = self.user
        # A foreground switch or destroyed HWND can race the PID lookup. Keep an
        # unresolved sample rather than attributing it to either the user or game.
        handle, owner_pid, resolved = 0, None, False
        for _ in range(3):
            handle = int(user.GetForegroundWindow() or 0)
            owner = wintypes.DWORD()
            thread_id = user.GetWindowThreadProcessId(handle, ctypes.byref(owner)) if handle else 0
            if handle == int(user.GetForegroundWindow() or 0):
                resolved = not handle or bool(thread_id and owner.value)
                owner_pid = owner.value if handle and resolved else None
                break
        return handle, owner_pid, resolved

    def owned_windows(self, pid):
        """One complete top-level enumeration filtered by owning process."""
        ctypes, wintypes = self.ctypes, self.wintypes
        user = self.user
        windows = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def visit(window, _):
            owner = wintypes.DWORD()
            user.GetWindowThreadProcessId(window, ctypes.byref(owner))
            if owner.value == pid:
                windows.append(int(window))
            return True

        user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        if not user.EnumWindows(visit, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        return windows

    def owner_pid(self, handle):
        owner = self.wintypes.DWORD()
        self.user.GetWindowThreadProcessId(handle, self.ctypes.byref(owner))
        return owner.value or None

    def alive(self, handle):
        return bool(self.user.IsWindow(handle))

    def visible(self, handle):
        return bool(self.user.IsWindowVisible(handle))


class WindowTracker:
    """Hold the target's top-level HWNDs; re-verify every sample, rescan as a backstop.

    Before this change every sample enumerated every top-level window on the
    system and filtered by PID, so one whole-desktop pass had to finish inside
    every 25ms tick. Here discovery happens in a scan, each sample only asks
    IsWindow/GetWindowThreadProcessId/IsWindowVisible about the handles already
    held, and the full pass repeats only while nothing is held, when the caller
    requests it and every ``scan_interval_seconds`` as a backstop for windows
    created after discovery. A handle that was destroyed or that now belongs to
    another process is dropped, so a reused HWND value is never reported as the
    target's window and another process can never enter this evidence.
    """

    def __init__(self, backend, pid, *, scan_interval_seconds=SCAN_INTERVAL_SECONDS, clock=time.monotonic):
        if type(pid) is not int or pid <= 0:
            raise ValueError("window tracker requires a positive target PID")
        if type(scan_interval_seconds) not in (int, float) or not 0.001 <= scan_interval_seconds <= 60:
            raise ValueError("invalid window scan interval")
        self.backend, self.pid, self.clock = backend, pid, clock
        self.scan_interval_seconds = float(scan_interval_seconds)
        self.handles = []
        self.next_scan = 0.0
        self.diagnostics = {"scans": 0, "verifications": 0, "discovered": 0,
                            "dropped_destroyed": 0, "dropped_reused": 0,
                            "maximum_scan_seconds": 0.0,
                            "scan_interval_seconds": self.scan_interval_seconds}

    def scan(self, *, force=False):
        """One complete enumeration; returns whether a scan was actually performed."""
        now = self.clock()
        if not force and self.handles and now < self.next_scan:
            return False
        started = self.clock()
        found = set(self.backend.owned_windows(self.pid))
        self.diagnostics["scans"] += 1
        self.diagnostics["maximum_scan_seconds"] = max(self.diagnostics["maximum_scan_seconds"],
                                                      self.clock() - started)
        self.diagnostics["discovered"] += len(found - set(self.handles))
        self.handles = sorted(found)
        self.next_scan = now + self.scan_interval_seconds
        return True

    def sample(self, *, force_scan=False):
        """Verified owned windows for one sample, in a stable handle order."""
        self.scan(force=force_scan)
        observed, dropped = self._verify()
        if dropped and not self.handles:
            # Losing the last held window is exactly when the target can have
            # created its replacement, so rediscover inside the same sample
            # instead of reporting an empty set a fresh window already contradicts.
            self.scan(force=True)
            observed = self._verify()[0]
        return observed

    def _verify(self):
        """Re-check every held handle; returns the sampled set and whether any went."""
        observed = []
        dropped = False
        for handle in list(self.handles):
            if not self.backend.alive(handle):
                self.handles.remove(handle)
                self.diagnostics["dropped_destroyed"] += 1
                dropped = True
                continue
            if self.backend.owner_pid(handle) != self.pid:
                self.handles.remove(handle)
                self.diagnostics["dropped_reused"] += 1
                dropped = True
                continue
            self.diagnostics["verifications"] += 1
            observed.append({"handle": handle, "visible": bool(self.backend.visible(handle))})
        return observed, dropped


def window_probe(pid: int | None = None, *, tracker=None, backend=None, force_scan=False) -> dict:
    """Read foreground ownership and owned visibility; never activate or send input."""
    if tracker is None:
        tracker = WindowTracker(backend or WindowBackend(), pid) if pid is not None else None
    elif tracker.pid != pid:
        raise ValueError("window tracker belongs to a different target process")
    backend = backend or (tracker.backend if tracker is not None else WindowBackend())
    foreground, foreground_pid, resolved = backend.foreground()
    owned = tracker.sample(force_scan=force_scan) if tracker is not None else []
    return {"foreground": foreground, "foreground_pid": foreground_pid,
            "foreground_resolved": resolved, "owned_windows": owned}


def launch_window_evidence(samples: list[dict], pid: int | None, *, errors=(),
                           interval_seconds: float = 0.025, max_gap_seconds: float = 0.250) -> dict:
    """Classify sampled game ownership; never infer a cause from HWND inequality.

    The verdict rests on this observation's own evidence: a held window of the
    target process that was sampled as visible, or a sampled foreground handle /
    owning PID that is one of ours. ``foreground_unchanged`` only compares two
    global foreground handles, and another process can create or activate a
    window without this process doing anything, so it is reported as a
    diagnostic and is never the reason for a pass. A negative conclusion still
    requires complete, bounded sampling of an owned hidden window.
    """
    errors = list(errors)
    # Preserve actual order and bad rows as evidence. Never sort a corrupted
    # stream into apparent chronological validity or quietly omit bad samples.
    valid = []
    for index, sample in enumerate(samples):
        try:
            stamp = sample["monotonic_seconds"]
            if (type(stamp) not in (int, float) or not math.isfinite(stamp)
                    or type(sample.get("foreground_resolved")) is not bool
                    or not isinstance(sample.get("owned_windows"), list)
                    or any(not isinstance(w, dict) or type(w.get("handle")) is not int
                           or type(w.get("visible")) is not bool for w in sample["owned_windows"])):
                raise ValueError("invalid sample shape")
            foreground, owner = sample.get("foreground"), sample.get("foreground_pid")
            if sample["foreground_resolved"] and (type(foreground) is not int or foreground < 0
                    or (foreground == 0 and owner is not None)
                    or (foreground != 0 and (type(owner) is not int or owner <= 0))):
                raise ValueError("invalid resolved foreground owner")
            if valid and stamp <= valid[-1]["monotonic_seconds"]:
                raise ValueError("non-increasing sample time")
            valid.append(sample)
        except (KeyError, TypeError, ValueError) as error:
            errors.append(f"malformed sample {index}: {error}")
    original_samples = samples
    samples = valid
    before, after = (samples[0], samples[-1]) if samples else ({}, {})
    observed = [sample for sample in samples if pid is not None and sample.get("foreground_pid") == pid]
    # Compare the sampled foreground handle against the handles this sample
    # verified as owned; no other process's window can enter this comparison.
    owned_foreground = [sample for sample in samples if pid is not None
                        and sample.get("foreground") in {window["handle"] for window in sample["owned_windows"]}]
    unresolved = [index for index, sample in enumerate(samples) if sample.get("foreground_resolved") is not True]
    gaps = [right["monotonic_seconds"]-left["monotonic_seconds"] for left, right in zip(samples, samples[1:])]
    maximum_gap = max(gaps, default=0)
    owned = after.get("owned_windows", [])
    visible = [window for sample in samples for window in sample["owned_windows"] if window["visible"]]
    hidden = bool(owned) and not any(window["visible"] for window in owned)
    complete = (type(pid) is int and pid > 0 and len(samples) >= 2 and not errors and not unresolved
                and maximum_gap <= max_gap_seconds)
    # A sampled ownership match is positive evidence even if other samples are
    # missing. A negative conclusion requires complete bounded sampling.
    status = ("fail" if observed or owned_foreground or visible
              else "pass" if complete and hidden else "unverified")
    return {"schema": "lvz.launch-windows.v2", "pid": pid, "before": before, "after": after,
            "hidden": hidden, "foreground_unchanged": before.get("foreground") == after.get("foreground"),
            "foreground_change_cause": "not_inferred",
            "foreground_change_used_as_evidence": False,
            "game_foreground_observed": bool(observed), "game_foreground_samples": observed,
            "our_window_foreground_observed": bool(owned_foreground),
            "our_window_foreground_samples": owned_foreground,
            "our_window_visible_observed": bool(visible),
            "claims": {"basis": "sampled targets of this observation only",
                       "our_window_never_foreground": not owned_foreground,
                       "our_window_never_visible": not visible,
                       "game_process_never_foreground": not observed,
                       "global_foreground_equality_is_evidence": False,
                       "another_processes_window_activity_is_evidence": False},
            "foreground_check_status": status, "samples": original_samples,
            "sampling": {"method": "read_only_foreground_hwnd_and_pid_polling",
                         "window_identity": "held_hwnd_reverified_each_sample_with_periodic_scan",
                         "interval_seconds": interval_seconds, "maximum_gap_seconds": maximum_gap,
                         "allowed_maximum_gap_seconds": max_gap_seconds, "sample_count": len(samples),
                         "duration_seconds": after.get("monotonic_seconds", 0)-before.get("monotonic_seconds", 0),
                         "unresolved_sample_indices": unresolved, "errors": list(errors), "complete": complete,
                         "scope": "launch start through initialized return; final game-window visibility",
                         "limitation": "finite samples can miss activation between reads; not a continuous-focus guarantee"}}


def _read(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def _publish_once(path, value):
    """Publish one complete document by rename; a second publication is refused.

    The destination name does not exist before the single rename, so a
    concurrent reader observes either no document or the complete document: the
    read side never has to survive a replacement, and the bytes never change
    afterwards. Publication is deliberately not retried and never falls back to
    an in-place write; a failed rename keeps the complete temporary file.
    """
    path = Path(path)
    if path.name not in ("control.json", "target.json"):
        raise ValueError("only the observer control documents are published")
    if path.exists():
        raise FileExistsError(f"{path.name} is write-once and already published")
    temporary = path.with_suffix(path.suffix + ".tmp")
    if temporary.exists():
        raise FileExistsError(f"refusing to overwrite retained {temporary.name}")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
    os.replace(temporary, path)
    return _hash(path)


def _write(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
    os.replace(temporary, path)


class ObserverFailure(RuntimeError):
    """A failure that names the component it came from; the seal retains it."""

    def __init__(self, component, message):
        super().__init__(f"{component}: {message}")
        self.component = component


def stop_channel_name(token):
    """Session-local name of the stop event; the token binds it to one monitor."""
    if (not isinstance(token, str) or not token or len(token) > 96
            or "\\" in token or "/" in token):
        raise ValueError("observer token cannot name a stop channel")
    return STOP_CHANNEL_PREFIX + token


def _expected_channel(token):
    try:
        return stop_channel_name(token)
    except ValueError as error:
        raise ObserverFailure("control document", f"invalid observer token: {error}") from error


@lru_cache(maxsize=1)
def _channel_api():
    """The named-event APIs: the stop request is an object, not a shared file."""
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    api.CreateEventW.restype = wintypes.HANDLE
    api.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    api.OpenEventW.restype = wintypes.HANDLE
    api.SetEvent.argtypes = [wintypes.HANDLE]
    api.SetEvent.restype = wintypes.BOOL
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    return api


class StopChannel:
    """Parent-owned manual-reset event; the only mutable cross-process channel."""

    def __init__(self, token):
        import ctypes
        self.name = stop_channel_name(token)
        self.api = _channel_api()
        self.handle = self.api.CreateEventW(None, 1, 0, self.name)
        if not self.handle:
            raise ObserverFailure("stop channel",
                                  f"cannot create {self.name}: {ctypes.WinError(ctypes.get_last_error())}")
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            self.close()
            raise ObserverFailure("stop channel", f"name already exists: {self.name}")

    def signal(self):
        import ctypes
        if not self.handle:
            raise ObserverFailure("stop channel", f"{self.name} is closed")
        if not self.api.SetEvent(self.handle):
            raise ObserverFailure("stop channel",
                                  f"cannot signal {self.name}: {ctypes.WinError(ctypes.get_last_error())}")

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def open_stop_channel(name):
    """Worker-side open of the parent's event; failure names it and never waits."""
    import ctypes
    handle = _channel_api().OpenEventW(SYNCHRONIZE | EVENT_MODIFY_STATE, False, name)
    if not handle:
        raise ObserverFailure("stop channel",
                              f"cannot open {name}: {ctypes.WinError(ctypes.get_last_error())}")
    return handle


def stop_requested(handle):
    """Bounded (zero-timeout) poll used by the sampling loop."""
    value = _channel_api().WaitForSingleObject(handle, 0)
    if value not in (WAIT_OBJECT_0, WAIT_TIMEOUT):
        raise ObserverFailure("stop channel", f"cannot read channel state: wait returned {value}")
    return value == WAIT_OBJECT_0


def close_channel(handle):
    if handle:
        _channel_api().CloseHandle(handle)


def _read_published_document(path, component, evidence, *, missing_ok=False):
    """Bounded read of a write-once document that another process published.

    Publishing a name can transiently deny an open for about a millisecond
    (Windows reports ERROR_ACCESS_DENIED/SHARING_VIOLATION/LOCK_VIOLATION while
    the directory entry becomes visible). The document never changes and was
    closed before its rename, so a later attempt cannot observe a different or
    partial document; the retries are bounded, recorded as evidence, and a
    persistent failure names the component. Absence is only an error when the
    caller cannot proceed without the document.
    """
    path, started = Path(path), time.monotonic()
    while True:
        try:
            with path.open(encoding="utf-8") as stream:
                document = json.load(stream)
            if evidence["transient_failures"]:
                evidence["recovered_reads"] += 1
            return document, _hash(path)
        except FileNotFoundError:
            if missing_ok:
                return None, None
            raise ObserverFailure(component, f"{path.name} is missing")
        except OSError as error:
            if not _transient_publish_failure(error):
                raise ObserverFailure(component, f"cannot open {path.name}: {error}") from error
            evidence["transient_failures"] += 1
            if len(evidence["failures"]) < 32:
                evidence["failures"].append({"monotonic_seconds": time.monotonic(),
                    "winerror": getattr(error, "winerror", None), "errno": error.errno,
                    "error": f"{type(error).__name__}: {error}"})
            elapsed = time.monotonic() - started
            evidence["maximum_retry_duration_seconds"] = max(
                evidence["maximum_retry_duration_seconds"], elapsed)
            if evidence["transient_failures"] >= PUBLISH_READ_ATTEMPTS or elapsed >= PUBLISH_READ_SECONDS:
                raise ObserverFailure(component, f"cannot read {path.name}: {error}") from error
            time.sleep(min(PUBLISH_READ_INTERVAL, PUBLISH_READ_SECONDS - elapsed))
        except ValueError as error:
            raise ObserverFailure(component, f"invalid {path.name}: {error}") from error


def _read_evidence():
    return {"transient_failures": 0, "recovered_reads": 0,
            "maximum_retry_duration_seconds": 0.0, "failures": []}


def _transient_publish_failure(error):
    """A name being published can briefly deny an open; the wait stays bounded.

    Python's CRT open reports this as ``PermissionError(errno=13)`` without a
    Win32 code, while a raw ``CreateFile`` reports ERROR_ACCESS_DENIED (5),
    ERROR_SHARING_VIOLATION (32) or ERROR_LOCK_VIOLATION (33). A permanent denial
    takes the same bounded path and still ends in a named component failure.
    """
    if isinstance(error, PermissionError):
        return True
    return getattr(error, "winerror", None) in TRANSIENT_WINDOWS_ERRORS


def _hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class ProcessIdentity:
    """Query-only handles bind PID to creation time, including the observer owner."""
    def __init__(self, pid):
        if type(pid) is not int or pid <= 0:
            raise ValueError("process PID must be positive")
        if os.name != "nt":
            raise OSError("live window observer requires Windows")
        import ctypes
        from ctypes import wintypes
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenProcess.restype = wintypes.HANDLE
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        self.api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.api.WaitForSingleObject.restype = wintypes.DWORD
        self.handle = self.api.OpenProcess(0x1000 | 0x100000, False, pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        stamps = [wintypes.FILETIME() for _ in range(4)]
        if not self.api.GetProcessTimes(self.handle, *[ctypes.byref(value) for value in stamps]):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())
        self.value = {"pid": pid, "creation_time_100ns": (stamps[0].dwHighDateTime << 32) | stamps[0].dwLowDateTime}

    def alive(self):
        value = self.api.WaitForSingleObject(self.handle, 0)
        if value not in (0, 258):
            raise OSError("cannot read process liveness")
        return value == 258

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def _worker(directory, token, *, windows=None):
    """No driver imports, input, focus, visibility changes, or game writes."""
    directory = Path(directory)
    parent = owner = target = tracker = channel = None
    channel_name = None
    count = byte_count = 0
    digest = hashlib.sha256()
    errors, error_count, failure = [], 0, None
    reason, target_identity, target_sha256, control_sha256 = "worker_error", None, None, None
    started = time.monotonic()
    first_sample = last_sample = None
    source_hash = _hash(Path(__file__))
    windows = windows if windows is not None else WindowBackend()
    control_reads, target_reads = _read_evidence(), _read_evidence()
    try:
        control_path = directory / "control.json"
        control, control_sha256 = _read_published_document(control_path, "control document", control_reads)
        if (not isinstance(control, dict) or control.get("schema") != SCHEMA
                or control.get("token") != token or control.get("stop_channel") != _expected_channel(token)):
            raise ObserverFailure("control document", "observer control identity or stop channel mismatch")
        channel_name = control["stop_channel"]
        channel = open_stop_channel(channel_name)
        parent = ProcessIdentity(control["parent"]["pid"])
        if parent.value != control["parent"]:
            raise ObserverFailure("parent identity", "parent process identity changed")
        owner = ProcessIdentity(os.getpid())
        interval = control["interval_seconds"]
        if type(interval) not in (int, float) or not 0.001 <= interval <= 0.25:
            raise ObserverFailure("control document", "invalid sampling interval")
        scan_interval = control["scan_interval_seconds"]
        if type(scan_interval) not in (int, float) or not 0.001 <= scan_interval <= 60:
            raise ObserverFailure("control document", "invalid window scan interval")
        next_sample = started
        with (directory / "samples.jsonl").open("xb") as output:
            while True:
                if target is None:
                    binding, binding_sha256 = _read_published_document(
                        directory / "target.json", "target binding document", target_reads, missing_ok=True)
                    if binding is not None:
                        if (not isinstance(binding, dict) or type(binding.get("pid")) is not int
                                or type(binding.get("creation_time_100ns")) is not int):
                            raise ObserverFailure("target binding document", "invalid target identity shape")
                        target = ProcessIdentity(binding["pid"])
                        if target.value != binding:
                            raise ObserverFailure("target identity",
                                                  "target process creation identity changed")
                        target_identity, target_sha256 = target.value, binding_sha256
                        tracker = WindowTracker(windows, target_identity["pid"],
                                                scan_interval_seconds=scan_interval)
                if not parent.alive():
                    reason = "parent_exited"
                    break
                if target is not None and not target.alive():
                    reason = "target_exited"
                    break
                if count >= MAX_SAMPLES or byte_count >= MAX_BYTES or time.monotonic() - started >= MAX_DURATION:
                    reason = "observer_limit"
                    break
                # The stop request is polled with a zero-timeout wait, then one
                # more sample (scan-backed) still has to be written and flushed.
                stopping = stop_requested(channel)
                stamp = time.monotonic()
                try:
                    sample = window_probe(target_identity["pid"] if target_identity else None,
                                          tracker=tracker, backend=windows, force_scan=stopping)
                except Exception as error:
                    error_count += 1
                    message = f"{type(error).__name__}: {error}"
                    if len(errors) < 32:
                        errors.append(f"window discovery: {message}")
                    sample = {"foreground": None, "foreground_pid": None, "foreground_resolved": False,
                              "owned_windows": [], "error": message}
                sample = {"seq": count, "monotonic_seconds": stamp, "probe_finished_seconds": time.monotonic(),
                          "target": target_identity, **sample}
                line = (json.dumps(sample, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
                if byte_count + len(line) > MAX_BYTES:
                    reason = "observer_limit"
                    break
                output.write(line)
                output.flush()
                digest.update(line)
                count += 1
                byte_count += len(line)
                first_sample = sample if first_sample is None else first_sample
                last_sample = sample
                if count == 1:
                    _write(directory / "ready.json", {"schema": SCHEMA, "token": token, "owner": owner.value,
                        "parent": parent.value, "source_sha256": source_hash,
                        "control_sha256": control_sha256, "stop_channel": channel_name,
                        "first_sample": sample, "first_sample_ok": "error" not in sample})
                if stopping:
                    reason = "requested_stop"
                    break
                next_sample = max(next_sample + interval, time.monotonic())
                time.sleep(max(0, next_sample - time.monotonic()))
            output.flush()
            os.fsync(output.fileno())
    except Exception as error:
        error_count += 1
        errors.append(f"{type(error).__name__}: {error}")
        failure = {"component": getattr(error, "component", "observer"),
                   "error": type(error).__name__, "message": str(error)}
    finally:
        summary = {"schema": SCHEMA, "token": token, "sealed": True,
            "owner": owner.value if owner else None, "parent": parent.value if parent else None,
            "target": target_identity, "source_sha256": source_hash,
            "control_sha256": control_sha256, "target_sha256": target_sha256,
            "stop_channel": channel_name, "stop_reason": reason,
            "samples": count, "bytes": byte_count, "sha256": digest.hexdigest(),
            "errors": errors, "error_count": error_count, "failure": failure,
            "control_reads": control_reads, "target_reads": target_reads,
            "window_discovery": dict(tracker.diagnostics) if tracker is not None else {"mode": "target_not_bound"},
            "first_sample_seconds": first_sample["monotonic_seconds"] if first_sample else None,
            "last_sample_finished_seconds": last_sample["probe_finished_seconds"] if last_sample else None}
        for process in (target, owner, parent):
            if process is not None:
                process.close()
        if channel:
            close_channel(channel)
        _write(directory / "sealed.json", summary)
        (directory / "observer.lock").unlink(missing_ok=True)
    return 0 if reason == "requested_stop" and not error_count else 1


class LaunchWindowMonitor:
    """Owned subprocess sampler; bind PID before leaving the monitored context.

    Runtime callers use ``LaunchWindowMonitor(pid=..., evidence_directory=...)``.
    Startup callers call bind_pid after launcher returns, inside the context.
    Old subclasses overriding sample are rejected instead of silently ignoring it.
    """
    interval_seconds = 0.025
    scan_interval_seconds = SCAN_INTERVAL_SECONDS
    max_gap_seconds = 0.250
    start_timeout_seconds = 5.0
    stop_timeout_seconds = 2.0

    def __init__(self, pid=None, *, evidence_directory=None):
        self.pid = pid
        self.directory = Path(evidence_directory) if evidence_directory else Path(tempfile.mkdtemp(prefix="lvz-window-observer-"))
        self.samples, self.errors = [], []
        self.process = None
        self.control = None
        self.target = None
        self.channel = None
        self.control_sha256 = self.target_sha256 = None
        self.ready = self.sealed = None
        self.started_at = self.stopped_at = None
        self._stderr = None
        self._exited = self._loaded = False
        self._owns_directory = False
        self._source_hash = _hash(Path(__file__))

    def bind_pid(self, pid):
        if self._exited:
            raise RuntimeError("bind game PID before observer shutdown")
        identity = ProcessIdentity(pid)
        try:
            if not identity.alive():
                raise RuntimeError("cannot bind an exited game process")
            target = identity.value
        finally:
            identity.close()
        if self.control is None:
            self.pid = pid
            return
        if self.target is not None:
            if self.target != target:
                raise RuntimeError("game process identity cannot change")
            self.pid = pid
            return
        # Write-once binding: the worker can only observe this document absent
        # or complete, and it never changes afterwards.
        self.target_sha256 = _publish_once(self.directory / "target.json", target)
        self.target = target
        self.pid = pid

    def __enter__(self):
        try:
            return self._start()
        except BaseException as error:
            if not self._exited:
                self.errors.append(f"observer startup: {type(error).__name__}: {error}")
                self.__exit__(None, None, None)
            raise

    def _start(self):
        if any("sample" in cls.__dict__ for cls in type(self).__mro__ if cls is not LaunchWindowMonitor):
            raise TypeError("sample overrides are not supported by the subprocess observer; pass pid= explicitly")
        if self.process is not None or self._exited:
            raise RuntimeError("observer cannot be reused")
        self.directory.mkdir(parents=True, exist_ok=True)
        if any(self.directory.iterdir()):
            raise FileExistsError("observer evidence directory must be empty")
        # Claim exclusively before any mutable control/evidence write. A second
        # monitor racing for this directory must never clean up the first one's
        # lock or overwrite its retained closure report.
        token = uuid.uuid4().hex
        with (self.directory / "observer.lock").open("x", encoding="ascii") as claim:
            self._owns_directory = True
            claim.write(token)  # One write; this file is never rewritten either.
        parent = ProcessIdentity(os.getpid())
        try:
            # The stop event exists before the worker starts looking for it; the
            # name is part of the immutable control document it verifies.
            self.channel = StopChannel(token)
            self.control = {"schema": SCHEMA, "token": token, "parent": parent.value,
                "interval_seconds": self.interval_seconds,
                "scan_interval_seconds": self.scan_interval_seconds,
                "stop_channel": self.channel.name}
        finally:
            parent.close()
        self.control_sha256 = _publish_once(self.directory / "control.json", self.control)
        if self.pid is not None:
            self.bind_pid(self.pid)
        try:
            self._stderr = (self.directory / "stderr.log").open("xb")
            command = [sys.executable, "-u", str(Path(__file__).resolve()), "--worker", str(self.directory.resolve()), self.control["token"]]
            self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=self._stderr, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            deadline = time.monotonic() + self.start_timeout_seconds
            while not (self.directory / "ready.json").is_file():
                if self.process.poll() is not None:
                    raise RuntimeError(f"window observer exited during startup: {self.process.returncode}")
                if time.monotonic() >= deadline:
                    raise TimeoutError("window observer did not become ready")
                time.sleep(0.01)
            self.ready = _read(self.directory / "ready.json")
            owner = ProcessIdentity(self.process.pid)
            try:
                if (self.ready.get("schema") != SCHEMA or self.ready.get("token") != self.control["token"]
                        or self.ready.get("owner") != owner.value or self.ready.get("parent") != self.control["parent"]
                        or self.ready.get("source_sha256") != self._source_hash
                        or self.ready.get("control_sha256") != self.control_sha256
                        or self.ready.get("first_sample_ok") is not True):
                    raise RuntimeError("observer startup/first sample evidence is invalid")
            finally:
                owner.close()
            self.started_at = time.monotonic()  # Only now may the caller launch a game.
            return self
        except BaseException as error:
            self.errors.append(f"observer startup: {type(error).__name__}: {error}")
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        if self._exited:
            return
        self.stopped_at = time.monotonic()
        self._exited = True
        if not self._owns_directory:
            return
        try:
            if self.process is not None:
                # The stop request is one SetEvent on the owned event object;
                # nothing on disk is rewritten to send it.
                if self.channel is not None:
                    try:
                        self.channel.signal()
                    except Exception as error:
                        self.errors.append(f"observer stop channel: {error}")
                try:
                    self.process.wait(timeout=self.stop_timeout_seconds)
                except subprocess.TimeoutExpired:
                    self.errors.append("observer did not stop within deadline; owned observer terminated")
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=self.stop_timeout_seconds)
                    except subprocess.TimeoutExpired:
                        self.errors.append("owned observer termination did not complete")
                if self.process.poll() is not None and self.process.returncode != 0:
                    self.errors.append(f"observer exit status {self.process.returncode}")
        except Exception as error:
            self.errors.append(f"observer shutdown: {type(error).__name__}: {error}")
        finally:
            if self.channel is not None:
                try:
                    self.channel.close()
                except Exception as error:
                    self.errors.append(f"observer stop channel close: {error}")
            if self._stderr:
                try:
                    self._stderr.close()
                except Exception as error:
                    self.errors.append(f"observer stderr close: {error}")
            # Only a confirmed dead writer can have its lock removed. This is
            # an explicit incomplete shutdown receipt, never a forged seal.
            if self.process is None or self.process.poll() is not None:
                try:
                    _write(self.directory / "parent-closed.json", {"schema": SCHEMA,
                        "observer_pid": self.process.pid if self.process else None,
                        "returncode": self.process.returncode if self.process else None,
                        "parent_scope_start": self.started_at, "parent_scope_end": self.stopped_at,
                        "errors": self.errors})
                    (self.directory / "observer.lock").unlink(missing_ok=True)
                except Exception as error:
                    self.errors.append(f"observer shutdown evidence: {error}")

    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        if not self._owns_directory:
            self.errors.append("observer never owned this evidence directory")
            return
        try:
            self.sealed = _read(self.directory / "sealed.json")
        except Exception as error:
            self.errors.append(f"missing/invalid observer seal: {error}")
        digest, size = hashlib.sha256(), 0
        try:
            with (self.directory / "samples.jsonl").open("rb") as stream:
                for index, line in enumerate(stream):
                    if index >= MAX_SAMPLES or len(line) > 1024 * 1024 or size + len(line) > MAX_BYTES:
                        raise ValueError("observer evidence limit exceeded")
                    digest.update(line)
                    size += len(line)
                    try:
                        if not line.endswith(b"\n"):
                            raise ValueError("incomplete raw sample line")
                        sample = json.loads(line)
                        if not isinstance(sample, dict) or sample.get("seq") != index:
                            raise ValueError("sample sequence mismatch")
                        self.samples.append(sample)
                    except Exception as error:
                        self.errors.append(f"sample {index}: {error}")
                        # The original bytes remain on disk, including bad rows.
        except Exception as error:
            self.errors.append(f"observer samples: {error}")
        try:
            seal = self.sealed
            if not isinstance(seal, dict):
                raise ValueError("observer seal is missing")
            named = seal.get("failure")
            if isinstance(named, dict) and named.get("component"):
                # Name the component that failed instead of a generic verdict.
                self.errors.append(f"observer component failure: {named['component']}: {named.get('message')}")
            if (seal["schema"] != SCHEMA or seal["token"] != self.control["token"]
                    or seal["sealed"] is not True or seal["stop_reason"] != "requested_stop"
                    or seal["owner"] != self.ready["owner"] or seal["parent"] != self.control["parent"]
                    or seal["target"] != self.target or seal["source_sha256"] != self._source_hash
                    or seal["control_sha256"] != self.control_sha256
                    or seal["target_sha256"] != self.target_sha256
                    or seal["samples"] != len(self.samples) or seal["bytes"] != size or seal["sha256"] != digest.hexdigest()
                    or seal["error_count"] != 0 or seal["errors"]):
                raise ValueError("seal ownership, count, hash or healthy shutdown mismatch")
            if (not self.samples or self.samples[0] != self.ready["first_sample"]
                    or seal["first_sample_seconds"] != self.samples[0]["monotonic_seconds"]
                    or seal["last_sample_finished_seconds"] != self.samples[-1]["probe_finished_seconds"]
                    or not seal["first_sample_seconds"] <= self.started_at <= self.stopped_at <= seal["last_sample_finished_seconds"]):
                raise ValueError("observer samples do not cover the complete parent context")
            bound = False
            for sample in self.samples:
                if sample["target"] is not None:
                    bound = True
                    if sample["target"] != self.target:
                        raise ValueError("sample target identity changed")
                elif bound or sample["owned_windows"]:
                    raise ValueError("sample lost target identity or invented owned windows")
                start, end = sample["monotonic_seconds"], sample["probe_finished_seconds"]
                if (type(end) not in (int, float) or not math.isfinite(end) or end < start
                        or end - start > self.max_gap_seconds or "error" in sample):
                    raise ValueError("failed or excessively slow actual window probe")
            if self.samples[-1]["target"] != self.target:
                raise ValueError("final sample did not bind actual target")
        except Exception as error:
            self.errors.append(f"observer evidence rejected: {error}")

    def result(self, pid):
        if not self._exited:
            raise RuntimeError("close observer context before collecting evidence")
        self._load()
        if pid != self.pid or (pid is not None and (not self.target or self.target.get("pid") != pid)):
            self.errors.append("result PID was not bound before observer shutdown")
        evidence = launch_window_evidence(self.samples, pid, errors=self.errors,
            interval_seconds=self.interval_seconds, max_gap_seconds=self.max_gap_seconds)
        evidence["schema"] = "lvz.launch-windows.v3"
        evidence["sampling"]["scope"] = "entire parent context; child final sample precedes shutdown; no post-parse parent sample"
        evidence["sampling"]["isolation"] = "owned_subprocess"
        evidence["observer"] = {"schema": SCHEMA, "directory": str(self.directory.resolve()),
            "source": str(Path(__file__).resolve()), "source_sha256": self._source_hash,
            "pid": self.process.pid if self.process else None, "target": self.target,
            "transport": "write_once_control_documents_and_named_stop_event",
            "control_sha256": self.control_sha256, "target_sha256": self.target_sha256,
            "stop_channel": self.channel.name if self.channel else None,
            "window_discovery": self.sealed.get("window_discovery") if isinstance(self.sealed, dict) else None,
            "parent_scope_start": self.started_at, "parent_scope_end": self.stopped_at,
            "ready": self.ready, "sealed": self.sealed, "raw_samples_retained": True}
        return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("token")
    arguments = parser.parse_args()
    raise SystemExit(_worker(arguments.worker, arguments.token))

