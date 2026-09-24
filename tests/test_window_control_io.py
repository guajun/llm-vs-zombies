"""Write-once control documents and the named stop event; no game and no GUI calls.

The pre-#21 channel rewrote one mutable ``control.json`` while the worker polled
it, so a reader could collide with a replacement. The replacement channel is
structurally different: the two control documents are published exactly once by
a single rename onto a name that does not exist yet, and the only mutable
cross-process state is a named Windows event object.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

from llm_vs_zombies import window_observer as observer


class SilentWindows:
    """Injectable read-only query surface; no window is created or touched."""

    def __init__(self):
        self.windows = {}
        self.queries = {"foreground": 0, "enumerate": 0, "alive": 0, "visible": 0, "owner": 0}

    def foreground(self):
        self.queries["foreground"] += 1
        return 0, None, True

    def owned_windows(self, pid):
        self.queries["enumerate"] += 1
        return sorted(handle for handle, owner in self.windows.items() if owner == pid)

    def alive(self, handle):
        self.queries["alive"] += 1
        return handle in self.windows

    def owner_pid(self, handle):
        self.queries["owner"] += 1
        return self.windows.get(handle)

    def visible(self, handle):
        self.queries["visible"] += 1
        return False


class WriteOnceControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_second_publication_is_refused_and_the_first_bytes_never_change(self):
        path = self.root / "control.json"
        published = observer._publish_once(path, {"seq": 0, "payload": "first"})
        original = path.read_bytes()
        with self.assertRaisesRegex(FileExistsError, "write-once"):
            observer._publish_once(path, {"seq": 1, "payload": "second"})
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(observer._hash(path), published)
        self.assertEqual(observer._read(path), {"seq": 0, "payload": "first"})
        self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_a_retained_temporary_is_never_clobbered_by_a_later_attempt(self):
        path = self.root / "target.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text('{"retained": true}\n', encoding="utf-8")
        with self.assertRaisesRegex(FileExistsError, "retained"):
            observer._publish_once(path, {"retained": False})
        self.assertEqual(json.loads(temporary.read_text(encoding="utf-8")), {"retained": True})
        self.assertFalse(path.exists())

    def test_only_the_observer_control_documents_can_be_published(self):
        with self.assertRaisesRegex(ValueError, "control documents"):
            observer._publish_once(self.root / "sealed.json", {"sealed": True})

    def test_held_reader_cannot_block_a_later_publication_of_another_document(self):
        control, target = self.root / "control.json", self.root / "target.json"
        observer._publish_once(control, {"token": "kept"})
        with control.open(encoding="utf-8") as held:
            # Nothing is replaced, so an open handle cannot deny publication.
            observer._publish_once(target, {"pid": 42, "creation_time_100ns": 7})
            self.assertEqual(json.load(held), {"token": "kept"})
        self.assertEqual(observer._read(target), {"pid": 42, "creation_time_100ns": 7})

    def test_reader_at_the_publication_instant_sees_absent_or_complete_only(self):
        # Publishing a name can transiently deny a *concurrent* open for about a
        # millisecond while the directory entry becomes visible. That is a
        # name-state artifact, not a partial document: any successful read is
        # the complete document, and a retry always succeeds (see the measured
        # bound below).
        document = {"seq": 7, "payload": "p" * 4096}
        blocked, recovered = 0, []
        for iteration in range(40):
            directory = self.root / f"round-{iteration}"
            directory.mkdir()
            path = directory / "control.json"
            stop, observed = threading.Event(), []

            def reader():
                nonlocal blocked
                while not stop.is_set():
                    try:
                        observed.append(observer._read(path))
                    except FileNotFoundError:
                        observed.append(None)
                    except PermissionError as error:
                        # A published name can be denied for about a millisecond
                        # while its directory entry becomes visible. Recover with
                        # the same bounded wait the worker uses, never a longer one.
                        blocked += 1
                        started = time.monotonic()
                        while time.monotonic() - started < observer.PUBLISH_READ_SECONDS:
                            time.sleep(observer.PUBLISH_READ_INTERVAL)
                            try:
                                observed.append(observer._read(path))
                            except (FileNotFoundError, PermissionError):
                                continue
                            recovered.append((time.monotonic() - started, error))
                            break

            thread = threading.Thread(target=reader)
            thread.start()
            try:
                observer._publish_once(path, document)
                self.assertEqual(observer._read(path), document)
            finally:
                stop.set()
                thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertTrue(observed, "the reader never ran")
            self.assertTrue(all(item in (None, document) for item in observed), observed[:5])
        self.assertEqual(len(recovered), blocked, "a transient publication denial never recovered")
        for elapsed, error in recovered:
            self.assertTrue(observer._transient_publish_failure(error))
            self.assertLess(elapsed, observer.PUBLISH_READ_SECONDS)


class NamedStopChannelNameTests(unittest.TestCase):
    def test_name_is_derived_from_the_monitor_token(self):
        self.assertEqual(observer.stop_channel_name("abc"),
                         "Local\\lvz-window-observer-stop-abc")

    def test_names_that_cannot_be_bound_to_one_monitor_are_rejected(self):
        for bad in ("", "a\\b", "a/b", None, 5, True, "x" * 97):
            with self.subTest(token=bad), self.assertRaises(ValueError):
                observer.stop_channel_name(bad)


@unittest.skipUnless(os.name == "nt", "named event objects require Windows")
class NamedStopChannelTests(unittest.TestCase):
    def setUp(self):
        self.token = uuid.uuid4().hex

    def test_signal_is_delivered_to_a_separate_process_with_a_bounded_wait(self):
        script = ("import sys, time\n"
                  "from llm_vs_zombies.window_observer import open_stop_channel, stop_requested\n"
                  "handle = open_stop_channel(sys.argv[1])\n"
                  "print('opened', flush=True)\n"
                  "deadline = time.monotonic() + 5\n"
                  "requested = False\n"
                  "while not requested and time.monotonic() < deadline:\n"
                  "    requested = stop_requested(handle)\n"
                  "    time.sleep(0.005)\n"
                  "print('signalled' if requested else 'timeout', flush=True)\n")
        channel = observer.StopChannel(self.token)
        try:
            child = subprocess.Popen([sys.executable, "-u", "-c", script, channel.name],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                self.assertEqual(child.stdout.readline().decode().strip(), "opened")
                channel.signal()
                out, err = child.communicate(timeout=10)
                self.assertEqual(out.decode().strip(), "signalled", err.decode())
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
        finally:
            channel.close()

    def test_a_second_monitor_cannot_adopt_an_existing_channel_name(self):
        first = observer.StopChannel(self.token)
        try:
            with self.assertRaisesRegex(observer.ObserverFailure, "already exists") as caught:
                observer.StopChannel(self.token)
            self.assertEqual(caught.exception.component, "stop channel")
        finally:
            first.close()
        # After the owner released it the name is gone and can be created again.
        again = observer.StopChannel(self.token)
        again.close()

    def test_the_event_object_outlives_the_parent_handle_and_vanishes_on_exit(self):
        script = ("import sys, time\n"
                  "from llm_vs_zombies.window_observer import open_stop_channel\n"
                  "handle = open_stop_channel(sys.argv[1])\n"
                  "print('opened', flush=True)\n"
                  "time.sleep(float(sys.argv[2]))\n")
        channel = observer.StopChannel(self.token)
        child = None
        try:
            child = subprocess.Popen([sys.executable, "-u", "-c", script, channel.name, "1.0"],
                stdout=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(child.stdout.readline().decode().strip(), "opened")
            child.stdout.close()
            channel.close()
            self.assertIsNone(child.poll(), "the worker handle keeps the event alive")
            reopened = observer.open_stop_channel(channel.name)
            observer.close_channel(reopened)
            child.wait(timeout=10)
            with self.assertRaisesRegex(observer.ObserverFailure, "cannot open") as caught:
                observer.open_stop_channel(channel.name)
            self.assertEqual(caught.exception.component, "stop channel")
        finally:
            if child is not None and child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            channel.close()


class WorkerChannelFailureTests(unittest.TestCase):
    """A dead or missing channel fails, is bounded and names the component."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.token = uuid.uuid4().hex

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def parent_identity():
        identity = observer.ProcessIdentity(os.getpid())
        try:
            return identity.value
        finally:
            identity.close()

    def publish_control(self, **overrides):
        document = {"schema": observer.SCHEMA, "token": self.token, "parent": self.parent_identity(),
                    "interval_seconds": 0.005, "scan_interval_seconds": 1.0,
                    "stop_channel": observer.stop_channel_name(self.token)}
        document.update(overrides)
        observer._publish_once(self.root / "control.json", document)
        return document

    def seal(self):
        return json.loads((self.root / "sealed.json").read_text(encoding="utf-8"))

    def test_missing_stop_channel_names_the_component_before_any_sample(self):
        self.publish_control()
        result = observer._worker(self.root, self.token, windows=SilentWindows())
        self.assertEqual(result, 1)
        seal = self.seal()
        self.assertEqual(seal["stop_reason"], "worker_error")
        self.assertEqual(seal["samples"], 0)
        self.assertEqual(seal["failure"]["component"], "stop channel")
        self.assertIn("cannot open", seal["failure"]["message"])
        self.assertFalse((self.root / "samples.jsonl").exists())
        self.assertFalse((self.root / "observer.lock").exists())

    def test_missing_control_document_is_a_named_failure(self):
        result = observer._worker(self.root, self.token, windows=SilentWindows())
        self.assertEqual(result, 1)
        seal = self.seal()
        self.assertEqual(seal["failure"]["component"], "control document")
        self.assertEqual(seal["samples"], 0)
        self.assertFalse((self.root / "control.json").exists())

    def test_control_document_for_another_token_is_refused(self):
        self.publish_control(token=uuid.uuid4().hex)
        self.assertEqual(observer._worker(self.root, self.token, windows=SilentWindows()), 1)
        self.assertEqual(self.seal()["failure"]["component"], "control document")

    def test_unreadable_target_binding_is_named_and_keeps_the_raw_prefix(self):
        channel = observer.StopChannel(self.token)
        try:
            self.publish_control()
            (self.root / "target.json").write_text("{not json", encoding="utf-8")
            self.assertEqual(observer._worker(self.root, self.token, windows=SilentWindows()), 1)
            seal = self.seal()
            self.assertEqual(seal["failure"]["component"], "target binding document")
            self.assertEqual(seal["stop_reason"], "worker_error")
            self.assertTrue((self.root / "samples.jsonl").exists())
            self.assertEqual(seal["sha256"], observer._hash(self.root / "samples.jsonl"))
            self.assertEqual(seal["bytes"], (self.root / "samples.jsonl").stat().st_size)
        finally:
            channel.close()

    def test_target_whose_creation_identity_changed_is_refused(self):
        channel = observer.StopChannel(self.token)
        try:
            self.publish_control()
            identity = self.parent_identity()
            identity["creation_time_100ns"] += 1
            observer._publish_once(self.root / "target.json", identity)
            self.assertEqual(observer._worker(self.root, self.token, windows=SilentWindows()), 1)
            self.assertEqual(self.seal()["failure"]["component"], "target identity")
        finally:
            channel.close()

    def test_unbound_worker_samples_without_enumerating_or_inventing_owned_windows(self):
        windows = SilentWindows()
        channel = observer.StopChannel(self.token)
        try:
            self.publish_control()

            def stop_soon():
                time.sleep(0.12)
                channel.signal()

            stopper = threading.Thread(target=stop_soon)
            stopper.start()
            started = time.monotonic()
            result = observer._worker(self.root, self.token, windows=windows)
            elapsed = time.monotonic() - started
            stopper.join(timeout=5)
            self.assertEqual(result, 0)
            seal = self.seal()
            self.assertEqual(seal["stop_reason"], "requested_stop")
            self.assertIsNone(seal["target"])
            self.assertEqual(seal["window_discovery"], {"mode": "target_not_bound"})
            self.assertGreaterEqual(seal["samples"], 2)
            self.assertLess(elapsed, 5)
            samples = [json.loads(line) for line in
                       (self.root / "samples.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(all(sample["target"] is None for sample in samples))
            self.assertTrue(all(sample["owned_windows"] == [] for sample in samples))
            self.assertGreater(windows.queries["foreground"], 0)
            self.assertEqual(windows.queries["enumerate"], 0)
        finally:
            channel.close()


if __name__ == "__main__":
    unittest.main()
