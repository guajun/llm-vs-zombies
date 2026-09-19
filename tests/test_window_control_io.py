"""Real Windows file-sharing races and bounded recovery; no game or GUI calls."""
from concurrent.futures import ThreadPoolExecutor
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from llm_vs_zombies import window_observer as observer


def telemetry():
    return {"attempts": 0, "transient_failures": 0, "recovered_reads": 0,
            "maximum_retry_duration_seconds": 0.0, "failures": []}


def sharing_error(code=32):
    error = PermissionError(13, 'injected Windows sharing conflict')
    error.winerror = code
    return error


class ControlReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root/'control.json'
        self.stats = telemetry()

    def tearDown(self):
        self.temp.cleanup()

    def test_recovery_retains_actual_failures_and_accepts_only_a_full_document(self):
        failures = [sharing_error(5), sharing_error(32), sharing_error(33)]
        with patch.object(observer, '_open_control', side_effect=failures+[io.StringIO('{"seq": 7}')]):
            self.assertEqual(observer._read_control(self.path, self.stats), {'seq':7})
        self.assertEqual(self.stats['attempts'],4)
        self.assertEqual(self.stats['transient_failures'],3)
        self.assertEqual(self.stats['recovered_reads'],1)
        self.assertEqual([x['winerror'] for x in self.stats['failures']],[5,32,33])
        self.assertGreater(self.stats['maximum_retry_duration_seconds'],0)

    def test_permanent_access_failure_is_bounded_and_raises_original_error(self):
        error = sharing_error(5)
        with patch.object(observer, '_open_control', side_effect=error) as opened, \
             patch.object(observer.time, 'sleep'):
            with self.assertRaises(PermissionError) as caught:
                observer._read_control(self.path,self.stats)
        self.assertIs(caught.exception,error)
        self.assertEqual(opened.call_count,observer.CONTROL_READ_ATTEMPTS)
        self.assertEqual(self.stats['transient_failures'],observer.CONTROL_READ_ATTEMPTS)
        self.assertEqual(self.stats['recovered_reads'],0)

    def test_retry_deadline_does_not_start_another_attempt_after_overslept_wait(self):
        # Started=0, first failure=0.01, diagnostic timestamp=0.01, wake=0.051.
        with patch.object(observer.time,'monotonic',side_effect=[0.,.01,.01,.051]), \
             patch.object(observer.time,'sleep'), \
             patch.object(observer,'_open_control',side_effect=sharing_error()) as opened:
            with self.assertRaises(PermissionError):
                observer._read_control(self.path,self.stats)
        self.assertEqual(opened.call_count,1)

    def test_missing_malformed_and_other_io_failures_do_not_retry(self):
        for failure in [FileNotFoundError(2,'missing'), OSError(23,'data error'),
                        PermissionError(13,'not a Win32 replace conflict')]:
            with self.subTest(error=repr(failure)), \
                 patch.object(observer,'_open_control',side_effect=failure) as opened:
                with self.assertRaises(type(failure)):
                    observer._read_control(self.path,telemetry())
                self.assertEqual(opened.call_count,1)
        with patch.object(observer,'_open_control',return_value=io.StringIO('{"seq":')) as opened:
            with self.assertRaises(json.JSONDecodeError):
                observer._read_control(self.path,telemetry())
            self.assertEqual(opened.call_count,1)

    def test_error_detail_memory_is_bounded_without_losing_total_count(self):
        stats = telemetry()
        for _ in range(35):
            with patch.object(observer,'_open_control',side_effect=[sharing_error(),io.StringIO('{}')]), \
                 patch.object(observer.time,'sleep'):
                observer._read_control(self.path,stats)
        self.assertEqual(stats['transient_failures'],35)
        self.assertEqual(stats['recovered_reads'],35)
        self.assertEqual(len(stats['failures']),32)

    def test_large_real_sample_gap_still_rejects_recovered_control_read(self):
        samples=[{'monotonic_seconds':x,'foreground':0,'foreground_pid':None,
                  'foreground_resolved':True,'owned_windows':[{'handle':99,'visible':False}]}
                 for x in (10.,10.251)]
        result=observer.launch_window_evidence(samples,42)
        self.assertEqual(result['sampling']['allowed_maximum_gap_seconds'],.25)
        self.assertFalse(result['sampling']['complete'])
        self.assertEqual(result['foreground_check_status'],'unverified')


@unittest.skipUnless(os.name=='nt','actual Windows share/delete contract')
class WindowsControlFileTests(unittest.TestCase):
    setUp = ControlReadTests.setUp
    tearDown = ControlReadTests.tearDown
    def exclusive_handle(self):
        from ctypes import wintypes
        api=observer._control_file_api()
        handle=api.CreateFileW(str(self.path),0x80000000,0,None,3,0x80,None)
        if handle==wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def test_live_reader_handle_allows_atomic_replace_and_reads_its_old_snapshot(self):
        old={'seq':0,'payload':'old'*5000}
        new={'seq':1,'payload':'new'*5000}
        observer._write_control(self.path,old)
        with observer._open_control(self.path) as held:
            # The actual Win32 handle stays open while another thread replaces
            # the exact destination. Default CRT sharing makes this fail.
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(observer._write_control,self.path,new).result(timeout=3)
            self.assertEqual(json.load(held),old)
            self.assertEqual(observer._read_control(self.path,self.stats),new)

    def test_repeated_publications_deliver_complete_ordered_documents_to_each_reader(self):
        def document(seq):
            payload=f'{seq:06d}'*512
            return {'seq':seq,'payload':payload,'sha256':hashlib.sha256(payload.encode()).hexdigest()}
        observer._write_control(self.path,document(0))
        barrier=threading.Barrier(4)
        finished=threading.Event()
        acknowledged=threading.Condition()
        seen=[-1]*3
        published=[0]
        failures=[]
        def reader(index):
            barrier.wait(timeout=3)
            last=-1; count=0; stats=telemetry()
            try:
                while not finished.is_set():
                    with acknowledged:
                        acknowledged.wait_for(lambda:published[0]>last or finished.is_set(),timeout=5)
                        if finished.is_set():
                            break
                    value=observer._read_control(self.path,stats)
                    self.assertGreaterEqual(value['seq'],last)
                    self.assertEqual(value,document(value['seq']))
                    last=value['seq'];count+=1
                    with acknowledged:
                        seen[index]=last
                        acknowledged.notify_all()
            except BaseException as error:
                with acknowledged:
                    failures.append(error)
                    acknowledged.notify_all()
                raise
            return count,stats
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures=[pool.submit(reader,index) for index in range(3)]
            barrier.wait(timeout=3)
            try:
                for seq in range(1,401):
                    observer._write_control(self.path,document(seq))
                    # Give each reader one job per publication instead of
                    # spinning three readers continuously on an unchanged file.
                    # The held-handle test above guarantees actual replacement
                    # overlap; this test verifies repeated complete publication
                    # and monotonic reads without benchmarking CI scheduling.
                    with acknowledged:
                        published[0]=seq
                        acknowledged.notify_all()
                        completed=acknowledged.wait_for(lambda:all(v>=seq for v in seen) or failures,timeout=5)
                        if failures:
                            raise failures[0]  # Preserve the actual reader failure.
                        self.assertTrue(completed,f'publication {seq} not acknowledged: {seen}')
            finally:
                finished.set()
                with acknowledged:
                    acknowledged.notify_all()
            results=[future.result(timeout=5) for future in futures]
        self.assertTrue(all(count>=400 for count,_ in results),results)
        self.assertEqual(observer._read_control(self.path,self.stats),document(400))

    def test_actual_exclusive_lock_recovery_keeps_retry_evidence(self):
        observer._write_control(self.path,{'seq':3})
        handle=self.exclusive_handle()
        blocked=threading.Event()
        original=observer._open_control
        def observed_open(path):
            try:
                return original(path)
            except OSError:
                blocked.set()
                raise
        try:
            with patch.object(observer,'_open_control',side_effect=observed_open), \
                 ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(observer._read_control,self.path,self.stats)
                self.assertTrue(blocked.wait(timeout=2))
                observer._control_file_api().CloseHandle(handle);handle=None
                self.assertEqual(future.result(timeout=2),{'seq':3})
        finally:
            if handle is not None:
                observer._control_file_api().CloseHandle(handle)
        self.assertGreaterEqual(self.stats['transient_failures'],1)
        self.assertEqual(self.stats['recovered_reads'],1)

    def test_actual_permanent_exclusive_lock_seals_worker_failure_without_sampling(self):
        observer._write_control(self.path,{'seq':0})
        before=self.path.read_bytes()
        handle=self.exclusive_handle()
        try:
            with patch.object(observer,'window_probe',side_effect=AssertionError('must not sample')) as probe:
                result=observer._worker(self.root,'test')
            probe.assert_not_called()
        finally:
            observer._control_file_api().CloseHandle(handle)
        self.assertEqual(result,1)
        seal=observer._read(self.root/'sealed.json')
        self.assertEqual(seal['stop_reason'],'worker_error')
        self.assertEqual(seal['samples'],0)
        self.assertEqual(seal['error_count'],1)
        self.assertIn('PermissionError',seal['errors'][0])
        self.assertGreater(seal['control_reads']['transient_failures'],0)
        self.assertEqual(seal['control_reads']['recovered_reads'],0)
        self.assertEqual(self.path.read_bytes(),before)

    def test_actual_permanent_missing_control_fails_without_inventing_a_file(self):
        with self.assertRaises(FileNotFoundError):
            observer._read_control(self.path,self.stats)
        self.assertFalse(self.path.exists())
        self.assertGreater(self.stats['transient_failures'],0)
        self.assertEqual(self.stats['recovered_reads'],0)
        self.assertTrue(all(x['winerror']==2 for x in self.stats['failures']))

    def test_permanent_race_after_a_real_raw_sample_keeps_prefix_and_failed_seal(self):
        class Identity:
            def __init__(self,pid): self.value={'pid':pid,'creation_time_100ns':pid*100}
            def alive(self): return True
            def close(self): pass
        observer._write_control(self.path,{'schema':observer.SCHEMA,'token':'test',
            'parent':Identity(42).value,'seq':0,'target':None,'stop':False,'interval_seconds':.001})
        held=[]
        def probe(_):
            held.append(self.exclusive_handle())
            return {'foreground':0,'foreground_pid':None,'foreground_resolved':True,'owned_windows':[]}
        try:
            with patch.object(observer,'ProcessIdentity',Identity),patch.object(observer,'window_probe',side_effect=probe):
                result=observer._worker(self.root,'test')
        finally:
            for handle in held:
                observer._control_file_api().CloseHandle(handle)
        raw=(self.root/'samples.jsonl').read_bytes()
        seal=observer._read(self.root/'sealed.json')
        self.assertEqual(result,1)
        self.assertEqual(len(raw.splitlines()),1)
        self.assertEqual(seal['samples'],1)
        self.assertEqual(seal['sha256'],hashlib.sha256(raw).hexdigest())
        self.assertEqual(seal['bytes'],len(raw))
        self.assertEqual(seal['stop_reason'],'worker_error')
        self.assertEqual(seal['error_count'],1)
        self.assertEqual(seal['control_reads']['recovered_reads'],0)
        self.assertTrue(observer._read(self.root/'ready.json')['first_sample_ok'])

    def test_permanent_publish_denial_retains_old_control_and_complete_temporary(self):
        observer._write_control(self.path,{'seq':0})
        handle=self.exclusive_handle()
        try:
            with self.assertRaises(PermissionError):
                observer._write_control(self.path,{'seq':1})
        finally:
            observer._control_file_api().CloseHandle(handle)
        self.assertEqual(observer._read(self.path),{'seq':0})
        self.assertEqual(observer._read(self.path.with_suffix('.json.tmp')),{'seq':1})


if __name__=='__main__':
    unittest.main()
