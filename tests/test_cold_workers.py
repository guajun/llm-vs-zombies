"""Offline scheduling/receipt tests. These are not live parallel evidence."""
import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from llm_vs_zombies import cold_workers as cw
from llm_vs_zombies import evaluation as ev
from llm_vs_zombies.evaluation_support import ReplayBoundaryClient


def task(repeat=1):
    return {"token": 'a'*31+str(repeat), "seed":42,"repeat":repeat,
            "run":str(Path('/run/suite-s42-c'+str(repeat))), "destination":str(Path('/output/replay-'+str(repeat))),
            "output":str(Path('/output')), "source":str(Path('/source')), "source_manifest_sha256":'b'*64,
            "source_trajectory_id":'c'*64, "host":{"directory":"fixture","files":{}},"cache":{"fixture":True}}


def receipt(t, passed=True):
    gates = {name: [] for name in ev.LIVE_GATES}
    for name in cw.REQUIRED_GATES:
        gates[name] = [{"seed":42,"status":"pass","source":"live_engine","detail":{"synthetic_fixture":True},"artifacts":[]}]
    result={"seed":42,"repeat":t['repeat'],"attempt":{"kind":"replay","run":t['run'],
        "report":str(Path(t['destination'])/'replay-report.json'),"passed":passed,"error":None},
        "gates":gates,"sessions":[{"run":t['run'],"infrastructure_passed":True,
            "archive_sealed":True,"cleanup_passed":True}],"error":None,"rpc_intervals":[]}
    return {"schema":cw.SCHEMA,"binding":cw.binding(t),"owner":{"pid":123,"creation_time_100ns":456},
            "source_unchanged":True,"host_unchanged":True,"result":result}


class PlanTests(unittest.TestCase):
    def test_default_one_and_explicit_two_only(self):
        self.assertEqual(ev.Plan().cold_workers,1)
        ev.Plan(cold_workers=2).validate()
        for value in (True,False,0,3,-1,1.0,'2',None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError,'cold_workers'):
                ev.Plan(cold_workers=value).validate()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'plan.json'
            with patch('builtins.print'):
                ev.main(['plan',str(path),'--cold-workers','2'])
            self.assertEqual(ev.Plan.load(path).cold_workers,2)

    def test_serial_suite_never_calls_parallel_pool(self):
        # Existing model verifies source first advance, replay and recovery;
        # invoking it with a spawn trap preserves the actual serial control path.
        from test_evaluation_lifecycle import SuiteTests
        with patch.object(cw,'run_parallel_colds',side_effect=AssertionError('serial spawned')):
            model=SuiteTests()
            try:
                result=model.run_fixture()
                self.assertEqual(result['statistics']['completed_cases'],1)
                self.assertEqual(model.roles,['source','cold','recovery'])
            finally:
                model.doCleanups()


class ReceiptTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='nt','parallel cold worker hosts require Windows Job Objects')
    def test_constructor_cleanup_keeps_original_failure_and_all_notes(self):
        from types import SimpleNamespace
        original=RuntimeError('owner identity lookup failed')
        calls=[]
        def fail(label):
            calls.append(label)
            raise OSError(label)
        host=SimpleNamespace(pid=123,poll=lambda:None,terminate=lambda:fail('terminate'),
                             wait=lambda timeout:fail('wait'))
        job=SimpleNamespace(close=lambda:fail('job close'))
        with tempfile.TemporaryDirectory() as directory:
            t=task();t.update(root=directory,plan=ev.asdict(ev.Plan(cold_workers=2)))
            with patch.object(cw,'OwnedJob',return_value=job), \
                 patch.object(cw.subprocess,'Popen',return_value=host), \
                 patch.object(cw,'ProcessIdentity',side_effect=original), \
                 self.assertRaises(RuntimeError) as captured:
                cw.WorkerProcess(t,Path(directory)/'worker')
            self.assertIs(captured.exception,original)
            self.assertEqual(calls,['terminate','job close','wait'])
            detail=cw.failure_result(t,'worker_start',captured.exception)['error']
            self.assertEqual(detail['message'],'owner identity lookup failed')
            self.assertEqual(len(detail['exception_notes']),2)
            self.assertIn('wait',detail['exception_notes'][1])
            # Closed streams make normal TemporaryDirectory cleanup possible.
            for name in ('stdout.log','stderr.log'):
                (Path(directory)/'worker'/name).unlink()

    def test_close_attempts_both_streams_after_job_wait_and_stdout_fail(self):
        from types import SimpleNamespace
        calls=[]
        def fail(label):
            calls.append(label)
            raise OSError(label)
        handle=cw.WorkerProcess.__new__(cw.WorkerProcess)
        handle.job=SimpleNamespace(close=lambda:fail('job'))
        handle.process=SimpleNamespace(wait=lambda timeout:fail('wait'))
        handle.stdout=SimpleNamespace(close=lambda:fail('stdout'))
        handle.stderr=SimpleNamespace(close=lambda:calls.append('stderr'))
        with self.assertRaises(OSError) as captured:handle.close()
        self.assertEqual(calls,['job','wait','stdout','stderr'])
        self.assertEqual(str(captured.exception),'job')
        self.assertEqual(len(captured.exception.__notes__),3)

    def test_real_verification_rejects_success_with_empty_gate_artifacts(self):
        t=task();good=receipt(t)
        with self.assertRaisesRegex(RuntimeError,'required per-cold gate'):
            cw.verify_result(t,good,good['owner'],0,check_files=True)

    def test_job_diagnostic_failure_still_closes_and_joins_owner(self):
        from types import SimpleNamespace
        calls=[]
        host=SimpleNamespace(returncode=None,poll=lambda:host.returncode)
        def failed_count():raise OSError('QueryInformationJobObject failed')
        def close():calls.append('job close + actual host wait');host.returncode=1
        handle=cw.WorkerProcess.__new__(cw.WorkerProcess)
        handle.task=task();handle.job=SimpleNamespace(handle=123,active=failed_count)
        handle.owner={'pid':123,'creation_time_100ns':456};handle.process=host;handle.close=close
        result=handle.emergency_finish('fixture deadline')
        self.assertEqual(calls,['job close + actual host wait'])
        self.assertFalse(result['attempt']['passed'])
        self.assertEqual(result['host_process']['returncode'],1)
        self.assertEqual(result['secondary_errors'][0]['stage'],'job_count')

    def test_failed_close_does_not_claim_job_closed_or_host_exited(self):
        from types import SimpleNamespace
        handle=cw.WorkerProcess.__new__(cw.WorkerProcess)
        handle.task=task();handle.job=SimpleNamespace(handle=123,active=lambda:1)
        handle.owner={'pid':123,'creation_time_100ns':456}
        handle.process=SimpleNamespace(returncode=None,poll=lambda:None)
        def fail():raise OSError('CloseHandle failed')
        handle.close=fail
        result=handle.emergency_finish('fixture deadline')
        self.assertFalse(result['host_process']['emergency_job_closed'])
        self.assertFalse(result['host_process']['owned_host_exited'])
        self.assertEqual(result['secondary_errors'][0]['stage'],'owned_job_close_join')

    @unittest.skipUnless(os.name=='nt','real Windows host/Job startup failure fixture')
    def test_real_hidden_worker_rejects_host_before_any_game_or_cache_use(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();output=root/'experiments/runs/suite';output.mkdir(parents=True)
            source=root/'experiments/runs/source';source.mkdir()
            parent=cw.ProcessIdentity(os.getpid())
            handle=None
            try:
                t=task();t.update(schema=cw.SCHEMA,root=str(root),output=str(output),source=str(source),
                    run=str(root/'experiments/runs/suite-s42-c1'),destination=str(output/'seed-42-replay-1'),
                    parent=parent.value,plan=ev.asdict(ev.Plan(cold_workers=2)),
                    host={'intentionally_wrong_before_cache_or_launch':True})
                handle=cw.WorkerProcess(t,output/'worker-fixture')
                deadline=time.monotonic()+15
                while handle.poll() is None:
                    self.assertLess(time.monotonic(),deadline,'owned fixture host did not exit')
                    time.sleep(.025)
                result=handle.finish()
                self.assertFalse(result['attempt']['passed'])
                self.assertIn('worker host source differs',result['error']['message'])
                self.assertEqual(result['host_process']['active_descendants_after_exit'],0)
                self.assertFalse(Path(t['run']).exists())
                self.assertTrue((output/'worker-fixture/receipt.json').is_file())
            finally:
                if handle is not None:handle.close()
                parent.close()

    def test_required_identity_and_every_per_cold_gate(self):
        t=task(); good=receipt(t)
        cw.verify_result(t,good,good['owner'],0,check_files=False)
        for name in cw.REQUIRED_GATES:
            bad=copy.deepcopy(good);bad['result']['gates'][name]=[]
            with self.subTest(gate=name),self.assertRaisesRegex(RuntimeError,'required per-cold gate'):
                cw.verify_result(t,bad,bad['owner'],0,check_files=False)
        for mutate in (lambda v:v['binding'].update(source_manifest_sha256='d'*64),
                       lambda v:v['binding'].update(repeat=2),lambda v:v['owner'].update(creation_time_100ns=457),
                       lambda v:v['result']['attempt'].update(run='/different'),
                       lambda v:v.update(host_unchanged=False)):
            bad=copy.deepcopy(good);mutate(bad)
            with self.assertRaises(RuntimeError):cw.verify_result(t,bad,good['owner'],0,check_files=False)
        with self.assertRaisesRegex(RuntimeError,'normal exit'):
            cw.verify_result(t,good,good['owner'],1,check_files=False)

    def test_failed_receipt_retained_but_zero_exit_cannot_pass(self):
        t=task();bad=receipt(t,False)
        self.assertIs(cw.verify_result(t,bad,bad['owner'],1,check_files=False),bad['result'])
        with self.assertRaisesRegex(RuntimeError,'incorrectly exited zero'):
            cw.verify_result(t,bad,bad['owner'],0,check_files=False)

    def test_timing_binding_and_overlap_excludes_nonpositive_unverified_calls(self):
        version={"epoch":1,"tick":5,"revision":0}
        interval={"request_id":"cold1","method":"advance","start_ns":100,"end_ns":200,
                  "version":version,"executed_ticks":5,"executed_engine_calls":5}
        official={"requests":[{"actual_request_id":"cold1","result":{"observation":{"version":version},
                    "executed_ticks":5,"executed_engine_calls":5}}]}
        cw.verify_intervals([interval],official)
        with self.assertRaisesRegex(RuntimeError,'verified original RPC'):
            cw.verify_intervals([{**interval,'request_id':'fake'}],official)
        a=receipt(task())['result'];a['rpc_intervals']=[interval]
        b=receipt(task(2))['result'];b['rpc_intervals']=[{**interval,'request_id':'cold2','start_ns':190,'end_ns':250}]
        self.assertEqual(cw.measured_overlaps([a,b])[0]['overlap_ns'],10)
        b['attempt']['passed']=False
        self.assertEqual(cw.measured_overlaps([a,b]),[])


class SchedulingTests(unittest.TestCase):
    def execute(self, specifications, *, fail_wait=False):
        now=[0]; started=[]; completed=[]; handles=[]; waits=[0]
        class Handle:
            def __init__(self,t):
                self.task=t;self.repeat=t['repeat'];self.deadline=20;self.exited=False
            def poll(self):
                if specifications[self.repeat].get('poll_raises') and now[0]>=specifications[self.repeat]['at']:
                    raise OSError('owned process query failed')
                if now[0]>=specifications[self.repeat]['at']:self.exited=True
                return (0 if specifications[self.repeat].get('passed',True) else 1) if self.exited else None
            def finish(self):
                completed.append(self.repeat)
                if specifications[self.repeat].get('finish_raises'):raise OSError('receipt unreadable')
                return receipt(self.task,specifications[self.repeat].get('passed',True))['result']
            def emergency_finish(self,reason):
                self.exited=True
                completed.append(('emergency',self.repeat))
                return cw.failure_result(self.task,'emergency',RuntimeError(reason))
        def start(t):
            started.append(t['repeat']);h=Handle(t);handles.append(h);return h
        def wait(seconds):
            waits[0]+=1;now[0]+=1
            if fail_wait and waits[0]==1:raise OSError('parent wait failed')
        summary=cw.schedule([task(i) for i in (1,2,3)],start,clock=lambda:now[0],wait=wait)
        return summary,started,completed,handles

    def test_failure_stops_third_but_second_really_finishes(self):
        summary,started,completed,handles=self.execute({1:{'at':1,'passed':False},2:{'at':5},3:{'at':6}})
        self.assertEqual(started,[1,2]);self.assertEqual(completed,[1,2])
        self.assertEqual(summary['not_started'],[3]);self.assertTrue(summary['all_started_exited'])
        self.assertFalse(summary['results'][0]['attempt']['passed']);self.assertTrue(summary['results'][1]['attempt']['passed'])
        self.assertTrue(all(h.exited for h in handles))

    def test_all_completions_polled_before_refill(self):
        summary,started,_,_=self.execute({1:{'at':1},2:{'at':1,'passed':False},3:{'at':2}})
        self.assertEqual(started,[1,2]);self.assertEqual(summary['not_started'],[3])

    def test_success_refills_capacity_and_results_sorted(self):
        summary,started,completed,_=self.execute({1:{'at':5},2:{'at':1},3:{'at':3}})
        self.assertEqual(started,[1,2,3]);self.assertEqual(completed,[2,3,1])
        self.assertEqual([r['repeat'] for r in summary['results']],[1,2,3]);self.assertFalse(summary['not_started'])

    def test_parent_failure_waits_for_existing_normal_health(self):
        summary,started,completed,_=self.execute({1:{'at':2},2:{'at':4},3:{'at':5}},fail_wait=True)
        self.assertEqual(started,[1,2]);self.assertEqual(completed,[1,2])
        self.assertIn('parent wait failed',summary['parent_error']['message'])
        self.assertTrue(summary['all_started_exited'])

    def test_status_query_failure_during_parent_cleanup_still_finishes_peer(self):
        summary,started,completed,handles=self.execute({1:{'at':2,'poll_raises':True},2:{'at':4},3:{'at':5}},fail_wait=True)
        self.assertEqual(started,[1,2]);self.assertEqual(completed,[('emergency',1),2])
        self.assertFalse(summary['all_started_exited'])
        self.assertEqual(summary['owned_exit_query_errors'][0]['repeat'],1)
        self.assertIn('owned process query failed',summary['results'][0]['poll_error']['message'])
        self.assertTrue(summary['results'][1]['attempt']['passed'])
        self.assertTrue(handles[1].exited)

    def test_deadline_and_bad_result_are_failures_not_claimed_seals(self):
        summary,started,completed,_=self.execute({1:{'at':100},2:{'at':2,'finish_raises':True},3:{'at':5}})
        self.assertEqual(started,[1,2]);self.assertIn(('emergency',1),completed);self.assertIn(('emergency',2),completed)
        self.assertTrue(summary['all_started_exited']);self.assertTrue(all(not r['attempt']['passed'] for r in summary['results']))
        self.assertTrue(all(not r['sessions'] for r in summary['results']))


class ColdAttemptFailureTests(unittest.TestCase):
    def test_original_replay_report_error_survives_retention_write_failure(self):
        from llm_vs_zombies.engine_replay import ReplayDivergence
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);output=root/'suite';output.mkdir()
            original=ReplayDivergence({'stage':'actual_post_state','difference':{'expected':1,'actual':2}})
            with patch('llm_vs_zombies.engine_replay.replay',side_effect=original), \
                 patch.object(ev,'_retain_session',side_effect=OSError('retention disk full')):
                result=ev.run_cold_attempt(root,ev.Plan(),output,42,1,object())
            self.assertFalse(result['attempt']['passed'])
            self.assertEqual(result['error']['report']['stage'],'actual_post_state')
            self.assertEqual(result['secondary_errors'][0]['message'],'retention disk full')

    def test_rpc_clock_ends_before_after_step_pause_callback(self):
        from types import SimpleNamespace
        client=SimpleNamespace(version={'epoch':1,'tick':0,'revision':0})
        result={'observation':{'version':{'epoch':1,'tick':5,'revision':0}},'executed_ticks':5,'executed_engine_calls':5}
        sent=[];callbacks=[]
        def request(*args,**kwargs):sent.append((args,kwargs));return result
        client.request=request
        budget=SimpleNamespace(check=lambda *args,**kwargs:None)
        wrapped=ReplayBoundaryClient(client,budget,lambda observation:callbacks.append('pause'),on_rpc=lambda value:callbacks.append(value))
        params={'max_ticks':5};expect=client.version
        with patch('llm_vs_zombies.evaluation_support.time.monotonic_ns',side_effect=[100,120]):
            self.assertIs(wrapped.request('advance',params,expect=expect,request_id='same'),result)
        self.assertEqual(sent,[(('advance',params),{'expect':expect,'request_id':'same'})])
        self.assertEqual(callbacks[0]['end_ns'],120)
        self.assertEqual(callbacks[1],'pause')


if __name__=='__main__':unittest.main()
