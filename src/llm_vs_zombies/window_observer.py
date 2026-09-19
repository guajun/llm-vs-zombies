"""Read-only window observation in an owned, console-free subprocess.

Executable as a file so Windows child startup never imports a game driver.
All sampling evidence is retained; errors and gaps never become successful proof.
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

def window_probe(pid: int | None = None) -> dict:
    """Read foreground ownership and owned visibility; never activate or send input."""
    import ctypes
    from ctypes import wintypes
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetForegroundWindow.restype = wintypes.HWND
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    # A foreground switch or destroyed HWND can race the PID lookup. Keep an
    # unresolved sample rather than attributing it to either the user or game.
    foreground, foreground_pid, resolved = 0, None, False
    for _ in range(3):
        foreground = int(user.GetForegroundWindow() or 0)
        owner = wintypes.DWORD()
        thread_id = user.GetWindowThreadProcessId(foreground, ctypes.byref(owner)) if foreground else 0
        if foreground == int(user.GetForegroundWindow() or 0):
            resolved = not foreground or bool(thread_id and owner.value)
            foreground_pid = owner.value if foreground and resolved else None
            break
    windows = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    @callback_type
    def visit(window, _):
        owner = wintypes.DWORD()
        user.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value == pid:
            windows.append({"handle": window, "visible": bool(user.IsWindowVisible(window))})
        return True
    if pid is not None:
        user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        if not user.EnumWindows(visit, 0):
            raise ctypes.WinError(ctypes.get_last_error())
    return {"foreground": foreground, "foreground_pid": foreground_pid,
            "foreground_resolved": resolved, "owned_windows": windows}


def launch_window_evidence(samples: list[dict], pid: int | None, *, errors=(),
                           interval_seconds: float = 0.025, max_gap_seconds: float = 0.250) -> dict:
    """Classify sampled game ownership, never infer a cause from HWND inequality."""
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
    unresolved = [index for index, sample in enumerate(samples) if sample.get("foreground_resolved") is not True]
    gaps = [right["monotonic_seconds"]-left["monotonic_seconds"] for left, right in zip(samples, samples[1:])]
    maximum_gap = max(gaps, default=0)
    owned = after.get("owned_windows", [])
    hidden = bool(owned) and not any(window["visible"] for window in owned)
    complete = (type(pid) is int and pid > 0 and len(samples) >= 2 and not errors and not unresolved
                and maximum_gap <= max_gap_seconds)
    # A sampled ownership match is positive evidence even if other samples are
    # missing. A negative conclusion requires complete bounded sampling.
    visible = [window for sample in samples for window in sample["owned_windows"] if window["visible"]]
    status = ("fail" if observed or visible
              else "pass" if complete and hidden else "unverified")
    return {"schema": "lvz.launch-windows.v2", "pid": pid, "before": before, "after": after,
            "hidden": hidden, "foreground_unchanged": before.get("foreground") == after.get("foreground"),
            "foreground_change_cause": "not_inferred",
            "game_foreground_observed": bool(observed), "game_foreground_samples": observed,
            "foreground_check_status": status, "samples": original_samples,
            "sampling": {"method": "read_only_foreground_hwnd_and_pid_polling",
                         "interval_seconds": interval_seconds, "maximum_gap_seconds": maximum_gap,
                         "allowed_maximum_gap_seconds": max_gap_seconds, "sample_count": len(samples),
                         "duration_seconds": after.get("monotonic_seconds", 0)-before.get("monotonic_seconds", 0),
                         "unresolved_sample_indices": unresolved, "errors": list(errors), "complete": complete,
                         "scope": "launch start through initialized return; final game-window visibility",
                         "limitation": "finite samples can miss activation between reads; not a continuous-focus guarantee"}}


SCHEMA = "lvz.window-observer.v1"
MAX_SAMPLES = 4_000_000
MAX_BYTES = 2 * 1024**3
MAX_DURATION = 86400.0
CONTROL_READ_ATTEMPTS = 8
CONTROL_READ_RETRY_SECONDS = 0.050
CONTROL_READ_RETRY_INTERVAL = 0.005


def _read(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


@lru_cache(maxsize=1)
def _control_file_api():
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.ReplaceFileW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID]
    api.ReplaceFileW.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    return api


def _open_control(path):
    """Read one atomic old/new file while allowing the parent to replace it."""
    if os.name != "nt":
        return Path(path).open(encoding="utf-8")
    import ctypes
    import msvcrt
    from ctypes import wintypes
    api = _control_file_api()
    # Python's ordinary CRT open does not share deletion on Windows. Holding
    # that read handle can make a simultaneous os.replace fail, and opening a
    # destination being replaced can itself hit a transient sharing error.
    handle = api.CreateFileW(str(Path(path)), 0x80000000, 0x1 | 0x2 | 0x4,
                             None, 3, 0x80, None)  # READ; share READ/WRITE/DELETE; OPEN_EXISTING
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        api.CloseHandle(handle)
        raise
    try:
        return os.fdopen(fd, "r", encoding="utf-8")
    except BaseException:
        os.close(fd)  # fd owns the Win32 handle after open_osfhandle succeeds.
        raise


def _read_control(path, evidence):
    """Bound transient Win32 retries without changing any sampling timestamp."""
    started = time.monotonic()
    failures = 0
    while True:
        evidence["attempts"] += 1
        try:
            with _open_control(path) as stream:
                value = json.load(stream)
        except OSError as error:
            # ReplaceFileW can also expose a brief name-lookup gap. Retry only
            # these native Windows codes; permanent missing/denied files still
            # fail within the same bound. Bad JSON and other errors stop now.
            if getattr(error, "winerror", None) not in (2, 5, 32, 33):
                raise
            failures += 1
            evidence["transient_failures"] += 1
            elapsed = time.monotonic() - started
            evidence["maximum_retry_duration_seconds"] = max(
                evidence["maximum_retry_duration_seconds"], elapsed)
            if len(evidence["failures"]) < 32:
                evidence["failures"].append({"monotonic_seconds": time.monotonic(),
                    "attempt": failures, "winerror": error.winerror,
                    "errno": error.errno, "error": f"{type(error).__name__}: {error}"})
            if failures >= CONTROL_READ_ATTEMPTS or elapsed >= CONTROL_READ_RETRY_SECONDS:
                raise  # The worker seals the real error and cannot pass.
            time.sleep(min(CONTROL_READ_RETRY_INTERVAL, CONTROL_READ_RETRY_SECONDS - elapsed))
            elapsed = time.monotonic() - started
            evidence["maximum_retry_duration_seconds"] = max(
                evidence["maximum_retry_duration_seconds"], elapsed)
            if elapsed >= CONTROL_READ_RETRY_SECONDS:
                raise
        else:
            if failures:
                evidence["recovered_reads"] += 1
                evidence["maximum_retry_duration_seconds"] = max(
                    evidence["maximum_retry_duration_seconds"], time.monotonic() - started)
            return value


def _write(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
    os.replace(temporary, path)


def _write_control(path, value):
    """Publish only the owned mutable control file; other evidence is unchanged."""
    path = Path(path)
    if path.name != "control.json":
        raise ValueError("control publisher requires control.json")
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
    if os.name == "nt" and path.is_file():
        # MoveFileEx (os.replace) can deny replacement of an open destination
        # even when its reader shares deletion. ReplaceFile preserves the old
        # handle's snapshot while publishing the complete replacement by name.
        # On failure retain the temporary file and original OS error; never
        # delete/recreate the destination or fall back to non-atomic writes.
        import ctypes
        if not _control_file_api().ReplaceFileW(str(path), str(temporary), None, 0, None, None):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(temporary, path)


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


def _worker(directory, token):
    """No driver imports, input, focus, visibility changes, or game writes."""
    directory = Path(directory)
    parent = owner = target = None
    count = byte_count = 0
    digest = hashlib.sha256()
    errors, error_count = [], 0
    reason, target_identity = "worker_error", None
    started, last_seq = time.monotonic(), -1
    first_sample = last_sample = None
    source_hash = _hash(Path(__file__))
    control_reads = {"attempts": 0, "transient_failures": 0, "recovered_reads": 0,
                     "maximum_retry_duration_seconds": 0.0, "failures": []}
    try:
        control = _read_control(directory / "control.json", control_reads)
        if control.get("token") != token or control.get("schema") != SCHEMA:
            raise ValueError("observer control identity mismatch")
        parent = ProcessIdentity(control["parent"]["pid"])
        if parent.value != control["parent"]:
            raise ValueError("parent process identity changed")
        owner = ProcessIdentity(os.getpid())
        interval = control["interval_seconds"]
        if type(interval) not in (int, float) or not 0.001 <= interval <= 0.25:
            raise ValueError("invalid sampling interval")
        next_sample = started
        with (directory / "samples.jsonl").open("xb") as output:
            while True:
                control = _read_control(directory / "control.json", control_reads)
                if (control.get("schema") != SCHEMA or control.get("token") != token
                        or control.get("parent") != parent.value or control.get("interval_seconds") != interval
                        or type(control.get("seq")) is not int or control["seq"] < last_seq
                        or type(control.get("stop")) is not bool):
                    raise ValueError("observer control changed identity or order")
                last_seq = control["seq"]
                requested_target = control.get("target")
                if requested_target is not None and target is None:
                    target = ProcessIdentity(requested_target["pid"])
                    if target.value != requested_target:
                        raise ValueError("target process creation identity changed")
                    target_identity = target.value
                if target_identity != requested_target:
                    raise ValueError("target process binding cannot change")
                if not parent.alive():
                    reason = "parent_exited"
                    break
                if target is not None and not target.alive():
                    reason = "target_exited"
                    break
                if count >= MAX_SAMPLES or byte_count >= MAX_BYTES or time.monotonic() - started >= MAX_DURATION:
                    reason = "observer_limit"
                    break
                stamp = time.monotonic()
                try:
                    sample = window_probe(target_identity["pid"] if target_identity else None)
                except Exception as error:
                    error_count += 1
                    message = f"{type(error).__name__}: {error}"
                    if len(errors) < 32:
                        errors.append(message)
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
                        "parent": parent.value, "source_sha256": source_hash, "first_sample": sample,
                        "first_sample_ok": "error" not in sample})
                if control["stop"]:
                    reason = "requested_stop"
                    break
                next_sample = max(next_sample + interval, time.monotonic())
                time.sleep(max(0, next_sample - time.monotonic()))
            output.flush()
            os.fsync(output.fileno())
    except Exception as error:
        error_count += 1
        errors.append(f"{type(error).__name__}: {error}")
    finally:
        summary = {"schema": SCHEMA, "token": token, "sealed": True,
            "owner": owner.value if owner else None, "parent": parent.value if parent else None,
            "target": target_identity, "source_sha256": source_hash,
            "stop_reason": reason, "samples": count, "bytes": byte_count, "sha256": digest.hexdigest(),
            "errors": errors, "error_count": error_count,
            "control_reads": control_reads,
            "first_sample_seconds": first_sample["monotonic_seconds"] if first_sample else None,
            "last_sample_finished_seconds": last_sample["probe_finished_seconds"] if last_sample else None}
        for process in (target, owner, parent):
            if process is not None:
                process.close()
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
    max_gap_seconds = 0.250
    start_timeout_seconds = 5.0
    stop_timeout_seconds = 2.0

    def __init__(self, pid=None, *, evidence_directory=None):
        self.pid = pid
        self.directory = Path(evidence_directory) if evidence_directory else Path(tempfile.mkdtemp(prefix="lvz-window-observer-"))
        self.samples, self.errors = [], []
        self.process = None
        self.control = None
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
        if self.control["target"] is not None and self.control["target"] != target:
            raise RuntimeError("game process identity cannot change")
        self.pid = pid
        self.control.update(target=target, seq=self.control["seq"] + 1)
        _write_control(self.directory / "control.json", self.control)

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
        with (self.directory / "observer.lock").open("x", encoding="ascii") as claim:
            self._owns_directory = True
            claim.write("owned observer startup pending\n")
        parent = ProcessIdentity(os.getpid())
        try:
            self.control = {"schema": SCHEMA, "token": uuid.uuid4().hex, "parent": parent.value,
                "seq": 0, "target": None, "stop": False, "interval_seconds": self.interval_seconds}
        finally:
            parent.close()
        _write_control(self.directory / "control.json", self.control)
        if self.pid is not None:
            self.bind_pid(self.pid)
        (self.directory / "observer.lock").write_text(self.control["token"], encoding="ascii")
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
                        or self.ready.get("source_sha256") != self._source_hash or self.ready.get("first_sample_ok") is not True):
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
                try:
                    self.control.update(stop=True, seq=self.control["seq"] + 1)
                    _write_control(self.directory / "control.json", self.control)
                except Exception as error:
                    self.errors.append(f"observer stop command: {error}")
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
            if (seal["schema"] != SCHEMA or seal["token"] != self.control["token"]
                    or seal["sealed"] is not True or seal["stop_reason"] != "requested_stop"
                    or seal["owner"] != self.ready["owner"] or seal["parent"] != self.control["parent"]
                    or seal["target"] != self.control["target"] or seal["source_sha256"] != self._source_hash
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
                    if sample["target"] != self.control["target"]:
                        raise ValueError("sample target identity changed")
                elif bound or sample["owned_windows"]:
                    raise ValueError("sample lost target identity or invented owned windows")
                start, end = sample["monotonic_seconds"], sample["probe_finished_seconds"]
                if (type(end) not in (int, float) or not math.isfinite(end) or end < start
                        or end - start > self.max_gap_seconds or "error" in sample):
                    raise ValueError("failed or excessively slow actual window probe")
            if self.samples[-1]["target"] != self.control["target"]:
                raise ValueError("final sample did not bind actual target")
        except Exception as error:
            self.errors.append(f"observer evidence rejected: {error}")

    def result(self, pid):
        if not self._exited:
            raise RuntimeError("close observer context before collecting evidence")
        self._load()
        if pid != self.pid or (pid is not None and (not self.control or (self.control.get("target") or {}).get("pid") != pid)):
            self.errors.append("result PID was not bound before observer shutdown")
        evidence = launch_window_evidence(self.samples, pid, errors=self.errors,
            interval_seconds=self.interval_seconds, max_gap_seconds=self.max_gap_seconds)
        evidence["schema"] = "lvz.launch-windows.v3"
        evidence["sampling"]["scope"] = "entire parent context; child final sample precedes shutdown; no post-parse parent sample"
        evidence["sampling"]["isolation"] = "owned_subprocess"
        evidence["observer"] = {"schema": SCHEMA, "directory": str(self.directory.resolve()),
            "source": str(Path(__file__).resolve()), "source_sha256": self._source_hash,
            "pid": self.process.pid if self.process else None, "target": self.control.get("target") if self.control else None,
            "parent_scope_start": self.started_at, "parent_scope_end": self.stopped_at,
            "ready": self.ready, "sealed": self.sealed, "raw_samples_retained": True}
        return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("token")
    arguments = parser.parse_args()
    raise SystemExit(_worker(arguments.worker, arguments.token))

