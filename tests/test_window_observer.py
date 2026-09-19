"""Observer lifecycle/decoding fixtures; never start or interact with a game."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from llm_vs_zombies import window_observer as observer


class FixtureIdentity:
    def __init__(self, pid): self.value = {'pid': pid, 'creation_time_100ns': pid * 100}
    def close(self): pass
    def alive(self): return True


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self): self.temp.cleanup()

    def fixture(self):
        """Explicit synthetic evidence to test the production closed reader."""
        monitor = observer.LaunchWindowMonitor(42, evidence_directory=self.root / 'evidence')
        monitor.directory.mkdir()
        monitor._owns_directory = True
        monitor._exited = True
        monitor.started_at, monitor.stopped_at = 10.01, 10.04
        target = FixtureIdentity(42).value
        owner, parent = FixtureIdentity(2).value, FixtureIdentity(1).value
        monitor.control = {'schema': observer.SCHEMA, 'token': 'test', 'target': target, 'parent': parent}
        samples = [{'seq': i, 'monotonic_seconds': stamp, 'probe_finished_seconds': stamp + .001,
            'target': target, 'foreground': 0, 'foreground_pid': None, 'foreground_resolved': True,
            'owned_windows': [{'handle': 99, 'visible': False}]} for i, stamp in enumerate((10., 10.025, 10.05))]
        monitor.ready = {'owner': owner, 'first_sample': samples[0]}
        raw = b''.join((json.dumps(s) + '\n').encode() for s in samples)
        (monitor.directory / 'samples.jsonl').write_bytes(raw)
        seal = {'schema': observer.SCHEMA, 'token': 'test', 'sealed': True, 'stop_reason': 'requested_stop',
            'target': target, 'owner': owner, 'parent': parent, 'source_sha256': monitor._source_hash,
            'samples': 3, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            'error_count': 0, 'errors': [], 'first_sample_seconds': 10., 'last_sample_finished_seconds': 10.051}
        observer._write(monitor.directory / 'sealed.json', seal)
        return monitor, samples, seal

    def test_closed_raw_binding_and_no_postparse_sample(self):
        monitor, _, _ = self.fixture()
        with patch.object(observer, 'window_probe', side_effect=AssertionError('parent must not sample')):
            result = monitor.result(42)
            self.assertEqual(result['foreground_check_status'], 'pass')
            self.assertEqual(result['sampling']['sample_count'], 3)
            self.assertEqual(result['after']['monotonic_seconds'], 10.05)
            self.assertTrue((monitor.directory / 'samples.jsonl').is_file())

    def test_unbound_wrong_pid_missing_seal_and_truncated_raw_are_unverified(self):
        for fault in ('pid', 'seal', 'truncate', 'target', 'scope', 'count', 'errors', 'source'):
            with self.subTest(fault=fault):
                original = self.root
                self.root = original / fault; self.root.mkdir()
                monitor, samples, seal = self.fixture()
                if fault == 'seal': (monitor.directory / 'sealed.json').unlink()
                elif fault == 'truncate':
                    path = monitor.directory / 'samples.jsonl'; path.write_bytes(path.read_bytes()[:-1])
                else:
                    if fault == 'target': seal['target']['creation_time_100ns'] += 1
                    elif fault == 'scope': monitor.stopped_at = 10.5
                    elif fault == 'count': seal['samples'] += 1
                    elif fault == 'errors': seal['error_count'] = 1
                    elif fault == 'source': seal['source_sha256'] = '0' * 64
                    observer._write(monitor.directory / 'sealed.json', seal)
                self.assertEqual(monitor.result(43 if fault == 'pid' else 42)['foreground_check_status'], 'unverified')
                self.root = original

    def test_startup_failure_keeps_original_error_and_retained_diagnostics(self):
        monitor = observer.LaunchWindowMonitor(evidence_directory=self.root / 'failed-start')
        with patch.object(observer, 'ProcessIdentity', FixtureIdentity), \
             patch.object(observer.subprocess, 'Popen', side_effect=OSError('cannot start owned observer')):
            with self.assertRaisesRegex(OSError, 'cannot start owned observer'):
                with monitor: self.fail('caller must not launch a game before ready')
        result = monitor.result(None)
        self.assertEqual(result['foreground_check_status'], 'unverified')
        self.assertTrue((monitor.directory / 'parent-closed.json').is_file())
        self.assertFalse((monitor.directory / 'observer.lock').exists())

    def test_shutdown_timeout_only_terminates_owned_popen_and_retains_lock_if_alive(self):
        monitor = observer.LaunchWindowMonitor(evidence_directory=self.root / 'hung')
        monitor.directory.mkdir()
        monitor._owns_directory = True
        monitor.control = {'seq': 0, 'stop': False}
        (monitor.directory / 'observer.lock').write_text('test')
        process = Mock(pid=999, returncode=None)
        process.wait.side_effect = subprocess.TimeoutExpired(['owned-observer'], 2)
        process.poll.return_value = None
        monitor.process = process
        monitor.__exit__(None, None, None)
        process.terminate.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)
        self.assertTrue((monitor.directory / 'observer.lock').exists())
        self.assertEqual(monitor.result(None)['foreground_check_status'], 'unverified')

    def test_legacy_sample_override_is_rejected(self):
        class OldDriver(observer.LaunchWindowMonitor):
            def sample(self, pid=None): pass
        monitor = OldDriver(evidence_directory=self.root / 'legacy')
        with self.assertRaisesRegex(TypeError, 'pass pid= explicitly'):
            with monitor: self.fail('must not silently ignore custom PID sampling')

    def test_existing_evidence_and_other_writer_lock_are_never_modified(self):
        directory = self.root / 'already-owned'; directory.mkdir()
        for name in ('marker.json', 'observer.lock', 'parent-closed.json'):
            (directory / name).write_bytes(('original ' + name).encode())
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir()}
        monitor = observer.LaunchWindowMonitor(evidence_directory=directory)
        with self.assertRaisesRegex(FileExistsError, 'must be empty'):
            with monitor: self.fail('must not enter an existing recording')
        self.assertEqual(monitor.result(None)['foreground_check_status'], 'unverified')
        after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir()}
        self.assertEqual(before, after)

    @unittest.skipUnless(os.name == 'nt', 'actual Win32 read-only observer requires Windows')
    def test_busy_parent_does_not_starve_owned_subprocess(self):
        monitor = observer.LaunchWindowMonitor(pid=os.getpid(), evidence_directory=self.root / 'busy')
        previous = sys.getswitchinterval()
        try:
            with monitor:
                sys.setswitchinterval(2.0)
                until = time.monotonic() + .8
                accumulator = 1
                while time.monotonic() < until:
                    accumulator = (accumulator * 1664525 + 1013904223) & 0xffffffff
        finally:
            sys.setswitchinterval(previous)
        result = monitor.result(os.getpid())
        self.assertEqual(monitor.process.returncode, 0)
        self.assertTrue(result['sampling']['complete'], result['sampling'])
        self.assertGreater(result['sampling']['sample_count'], 15)
        self.assertLessEqual(result['sampling']['maximum_gap_seconds'], .25)
        self.assertNotEqual(monitor.process.pid, os.getpid())
        self.assertEqual(result['observer']['target']['pid'], os.getpid())
        # This probes the test process, not a game; no live readiness claim.

    @unittest.skipUnless(os.name == 'nt', 'actual Win32 read-only observer requires Windows')
    def test_unknown_launch_pid_can_bind_before_exit_and_exception_stops_observer(self):
        monitor = observer.LaunchWindowMonitor(evidence_directory=self.root / 'bind')
        with self.assertRaisesRegex(RuntimeError, 'body failure'):
            with monitor:
                monitor.bind_pid(os.getpid())
                time.sleep(.075)
                raise RuntimeError('body failure')
        result = monitor.result(os.getpid())
        self.assertEqual(monitor.process.returncode, 0)
        self.assertTrue(result['sampling']['complete'], result['sampling'])
        self.assertIsNone(result['samples'][0]['target'])
        self.assertEqual(result['samples'][-1]['target']['pid'], os.getpid())
        self.assertFalse((monitor.directory / 'observer.lock').exists())

    @unittest.skipUnless(os.name == 'nt', 'actual Win32 process lifecycle requires Windows')
    def test_parent_exit_closes_only_its_observer_with_incomplete_evidence(self):
        directory = self.root / 'parent-exit'
        script = ('import os,sys; from pathlib import Path; '
                  'sys.path.insert(0,sys.argv[2]); '
                  'from llm_vs_zombies.window_observer import LaunchWindowMonitor; '
                  'm=LaunchWindowMonitor(pid=os.getpid(),evidence_directory=Path(sys.argv[1])); '
                  'm.__enter__(); os._exit(23)')
        parent = subprocess.run([sys.executable, '-c', script, str(directory),
            str(Path(observer.__file__).resolve().parents[1])], stdin=subprocess.DEVNULL,
            capture_output=True, timeout=8, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(parent.returncode, 23, parent.stderr)
        deadline = time.monotonic() + 3
        while not (directory / 'sealed.json').exists() and time.monotonic() < deadline:
            time.sleep(.01)
        seal = observer._read(directory / 'sealed.json')
        self.assertEqual(seal['stop_reason'], 'parent_exited')
        self.assertGreaterEqual(seal['samples'], 1)
        self.assertFalse((directory / 'observer.lock').exists())
        self.assertTrue((directory / 'samples.jsonl').is_file())


if __name__ == '__main__': unittest.main()
