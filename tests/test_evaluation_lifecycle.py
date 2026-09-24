"""Offline failure/control fixtures only; none certifies live readiness."""
from contextlib import contextmanager, ExitStack
import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import zipfile
from unittest.mock import Mock, patch

from llm_vs_zombies import evaluation as ev
from llm_vs_zombies.evaluation_support import (BoundaryBudget, BoundaryStop,
    ReplayBoundaryClient, error_detail, finalize_run, packaging_space, host_sources)
from llm_vs_zombies.engine_replay import ReplayDivergence
from llm_vs_zombies.records import write_json


def version(tick):
    return {"epoch": 1, "tick": tick, "revision": tick}


def observation(tick, *, completed=False, ui=3):
    return {"version": version(tick), "game_clock": tick, "wave": 20,
            "completed_rounds": int(completed), "game_ui": ui, "scene": 3}


class ResourceTests(unittest.TestCase):
    def test_skip_multiple_intervals_preserves_exact_last_request_and_records_low_disk(self):
        client = Mock()
        client.version = version(499)
        result = {"observation": observation(1501)}
        client.request.return_value = result
        free = Mock(side_effect=[SimpleNamespace(free=100), SimpleNamespace(free=9)])
        budget = BoundaryBudget('.', ev.Plan(min_free_bytes=10), disk_usage=free, clock=lambda: 0)
        after = Mock()
        wrapped = ReplayBoundaryClient(client, budget, after)
        params, expect = {"max_ticks": 1002}, version(499)
        self.assertIs(wrapped.request('advance', params, expect=expect, request_id='original'), result)
        client.request.assert_called_once_with('advance', params, expect=expect, request_id='original')
        self.assertEqual(free.call_count, 2)
        self.assertEqual(budget.stop['version']['tick'], 1501)
        self.assertEqual(budget.stop['reason'], 'disk_reserve_stop')
        after.assert_called_once_with(result['observation']) # callback can record an unexecuted pause
        with self.assertRaises(BoundaryStop):
            wrapped.request('advance', {'max_ticks': 1}, request_id='next')
        self.assertEqual(client.request.call_count, 1)

    def test_cold_has_its_own_wall_limit_and_checks_completed_last_boundary(self):
        now = [0]
        client = Mock(version=version(0))
        def respond(*args, **kwargs):
            now[0] = 3
            return {'observation': observation(1)}
        client.request.side_effect = respond
        budget = BoundaryBudget('.', ev.Plan(wall_budget_seconds=100, cold_wall_budget_seconds=2),
            cold=True, clock=lambda: now[0], disk_usage=lambda _: SimpleNamespace(free=10**12))
        wrapped = ReplayBoundaryClient(client, budget, Mock())
        wrapped.request('advance', {'max_ticks': 1})
        self.assertEqual(budget.stop['reason'], 'wall_budget_exhausted')
        self.assertEqual(budget.stop['limit_seconds'], 2)

    def test_pause_uses_actual_completed_boundary_without_extra_steps(self):
        with tempfile.TemporaryDirectory() as temp:
            client = Mock()
            schedule = ev.PauseSchedule(client, Path(temp), [(1000, 1), (2500, 5)])
            with patch.object(ev, '_pause_probe', side_effect=lambda c, seconds: {
                    'version': version(2600), 'wall_seconds': seconds, 'state_sha256': 'x'}):
                schedule.after_step(observation(2600))
                schedule.after_step(observation(2600))
            self.assertEqual([p['requested_tick'] for p in schedule.completed], [1000, 2500])
            self.assertEqual([p['version']['tick'] for p in schedule.completed], [2600, 2600])
            client.request.assert_not_called()

    def test_plan_cli_exposes_real_long_limits_and_rejects_invalid_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'plan.json'
            with patch('builtins.print'):
                ev.main(['plan', str(path), '--tier', 'strict', '--strategy', 'policy.py',
                    '--seeds', '42', '--min-free-bytes', '999', '--pause-points', '[[123,2]]'])
            plan = ev.Plan.load(path)
            self.assertEqual((plan.timeout_seconds, plan.wall_budget_seconds, plan.cold_wall_budget_seconds), (600,86400,86400))
            self.assertEqual((plan.seeds, plan.cold_starts, plan.min_free_bytes), ((42,),10,999))
            for changes in ({'min_free_bytes': True}, {'disk_check_ticks': 0}, {'packaging_reserve_bytes': -1},
                            {'pause_points': [[2,1],[1,1]]}, {'cold_wall_budget_seconds': float('nan')}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    replace(plan, **changes).validate()

    def test_source_final_live_boundary_and_first_single_step_apply_pause_plan(self):
        for endpoint, point in ((1000,1000),(1,1)):
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as temp:
                client=Mock(); client.commit.return_value={'observation':observation(endpoint),
                    'action_results':[], 'stop_reason':'budget_exhausted'}
                strategy=Mock(); strategy.decide.return_value={'actions':[],'advance_ticks':endpoint-1}
                budget=Mock(stop=None); budget.report.return_value={}
                plan=ev.Plan(tick_budget=endpoint,chunk_ticks=1,pause_points=((point,1),))
                with patch.object(ev,'ScriptStrategy',return_value=strategy), \
                     patch.object(ev,'_pause_probe',return_value={'version':version(endpoint),'wall_seconds':1}) as pause:
                    result=ev._play_source(client,Mock(),None,plan,42,observation(0),
                        {'observation':observation(1)},Path(temp),budget)
                pause.assert_called_once()
                self.assertEqual(result['pause_probes']['coverage_status'],'pass')
                self.assertEqual(len(result['pause_probes']['completed']),1)
                if endpoint==1: client.commit.assert_not_called()

    def test_source_direct_terminal_keeps_reached_unexecuted_probes_unverified(self):
        with tempfile.TemporaryDirectory() as temp:
            client=Mock(); client.commit.return_value={'observation':observation(3000,completed=True,ui=2),
                'action_results':[], 'stop_reason':'scene_changed'}
            strategy=Mock(); strategy.decide.return_value={'actions':[],'advance_ticks':3999}
            budget=Mock(stop=None); budget.report.return_value={}
            plan=ev.Plan(tick_budget=4000)
            with patch.object(ev,'ScriptStrategy',return_value=strategy), patch.object(ev,'_pause_probe') as pause:
                result=ev._play_source(client,Mock(),None,plan,42,observation(0),
                    {'observation':observation(1)},Path(temp),budget)
            pause.assert_not_called()
            self.assertTrue(result['full_cycle'])
            self.assertEqual(result['pause_probes']['coverage_status'],'unverified')
            self.assertEqual([x['requested_tick'] for x in result['pause_probes']['unexecuted_reached']],[1000,2500])
            self.assertIsNone(ev._coverage_passed(result['pause_probes']))

    def test_smoke_exit_rejects_global_failures_even_with_completed_cases(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'plan.json'; write_json(path,{})
            for missing in ('shared_profile_unchanged','runtime_windows','archive_integrity','host_identity'):
                report={'plan':{'tier':'smoke'},'statistics':{'failed_cases':0,'completed_cases':1},
                    'readiness':{'experiment_ready':False,'unmet_gates':['strict_suite_not_requested',missing]}}
                with self.subTest(missing=missing), patch.object(ev,'run_suite',return_value=report), patch('builtins.print'):
                    self.assertEqual(ev.main(['run',str(path),'--output',str(Path(temp)/'out')]),2)


class ProvenanceTests(unittest.TestCase):
    def test_actual_imports_are_bound_and_mismatched_archive_is_not_substituted(self):
        import llm_vs_zombies.evaluation_support as support
        package=Path(support.__file__).resolve().parent
        with tempfile.TemporaryDirectory() as temp:
            run=Path(temp); (run/'inputs').mkdir()
            archive=run/'inputs/implementation.zip'
            def make_archive(changed=False):
                with zipfile.ZipFile(archive,'w') as output:
                    for source in package.rglob('*.py'):
                        data=source.read_bytes()
                        if changed and source.name=='evaluation.py': data+=b'\n# wrong root source\n'
                        output.writestr('src/llm_vs_zombies/'+source.relative_to(package).as_posix(),data)
            make_archive()
            first=host_sources(run)
            self.assertTrue(first['matches_archive'])
            self.assertEqual(first['imported_modules']['llm_vs_zombies.evaluation']['path'],str(Path(ev.__file__).resolve()))
            make_archive(changed=True)
            changed=host_sources(run,first)
            self.assertFalse(changed['matches_archive'])
            self.assertFalse(changed['unchanged'])


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run = Path(self.temp.name)
        (self.run/'audit').mkdir(); (self.run/'decisions').mkdir()
        (self.run/'audit/events.jsonl').write_bytes(b'12345')
        (self.run/'decisions/evaluation.jsonl').write_bytes(b'123')
        write_json(self.run/'manifest.json', {'status': 'recording'})
        self.cleanup = dict(recording_closed=True, client_closed=True, trace_closed=True, owned_process_stopped=True)
        write_json(self.run/'evaluation-cleanup.json', self.cleanup)

    def tearDown(self): self.temp.cleanup()

    def test_each_missing_actual_close_blocks_pack_and_seal_even_without_locks(self):
        for key in self.cleanup:
            with self.subTest(key=key):
                write_json(self.run/'evaluation-cleanup.json', {**self.cleanup, key: False})
                with patch('llm_vs_zombies.engine_replay.build_trajectory') as build, \
                     patch('llm_vs_zombies.evaluation_support.finish') as finish:
                    result, _ = finalize_run(self.run, ev.Plan(), 'failed', package=True)
                build.assert_not_called(); finish.assert_not_called()
                self.assertFalse(result['archive_sealed'])
                self.assertFalse(result['passed_recording'])

    def test_exact_copy_threshold_and_insufficient_space_never_calls_builder(self):
        plan = ev.Plan(packaging_reserve_bytes=10)
        self.assertTrue(packaging_space(self.run, plan, disk_usage=lambda _: SimpleNamespace(free=18))['sufficient'])
        with patch('llm_vs_zombies.evaluation_support.shutil.disk_usage', return_value=SimpleNamespace(free=17)), \
             patch('llm_vs_zombies.engine_replay.build_trajectory') as build, \
             patch('llm_vs_zombies.evaluation_support.finish', return_value={'outcome':'disk'}) as finish, \
             patch('llm_vs_zombies.evaluation_support.validate', return_value={'status':'finalized'}):
            result, _ = finalize_run(self.run, plan, 'disk', package=True)
        build.assert_not_called(); finish.assert_called_once()
        self.assertTrue(result['archive_sealed'])
        self.assertFalse(result['passed_recording'])
        self.assertEqual(result['packaging_skipped'], 'packaging_skipped_insufficient_space')

    def test_primary_divergence_survives_report_write_pack_and_seal_errors(self):
        try:
            try:
                raise ReplayDivergence({'stage':'audit[5].rng', 'difference': {'path':'/rng', 'expected':1, 'actual':2}})
            except ReplayDivergence:
                raise OSError('report disk full')
        except OSError as error:
            primary = error_detail(error, prefer_report=True)
        with patch('llm_vs_zombies.engine_replay.build_trajectory', side_effect=ValueError('native fault')), \
             patch('llm_vs_zombies.evaluation_support.finish', side_effect=OSError('seal full')):
            result, _ = finalize_run(self.run, ev.Plan(), 'failed', package=True, primary_error=primary)
        self.assertEqual(result['primary_error']['type'], 'ReplayDivergence')
        self.assertEqual(result['primary_error']['report']['stage'], 'audit[5].rng')
        self.assertEqual([e['message'] for e in result['secondary_errors']], ['native fault', 'seal full'])
        self.assertEqual(primary['exception_chain'][0]['message'], 'report disk full')

    def test_real_lock_prevents_finish_and_existing_seal_is_never_changed(self):
        (self.run/'capture.lock').write_text('owned writer')
        (self.run/'events.jsonl').write_text('not synthetic success')
        (self.run/'config.json').write_text('{}')
        result, _ = finalize_run(self.run, ev.Plan(), 'failed')
        self.assertFalse(result['archive_sealed'])
        self.assertEqual((self.run/'capture.lock').read_text(), 'owned writer')
        write_json(self.run/'manifest.json', {'status':'finalized'})
        before = {p.name:p.read_bytes() for p in self.run.iterdir() if p.is_file()}
        with self.assertRaisesRegex(ValueError, 'already finalized'):
            finalize_run(self.run, ev.Plan(), 'overwrite')
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.run.iterdir() if p.is_file()})


class SessionTests(unittest.TestCase):
    def test_recorder_closes_before_observer_then_process_and_primary_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            root = Path(temp); run=root/'run'; run.mkdir()
            (run/'inputs').mkdir()
            order=[]
            class Monitor:
                def __init__(self, pid=None, **kwargs): self.label='runtime' if pid else 'launch'
                def __enter__(self): order.append(self.label+'_start'); return self
                def __exit__(self, *args): order.append(self.label+'_close')
                def bind_pid(self, pid): pass
                def result(self, pid):
                    if self.label=='runtime': raise OSError('window read failed')
                    return {'foreground_check_status':'pass'}
            client=Mock()
            client.observe.return_value=observation(0)
            def close_request(*args, **kwargs):
                order.append(args[0])
                return {'closed':True}
            client.request.side_effect=close_request
            trace=Mock(); trace.close.side_effect=OSError('trace close failed')
            stack.enter_context(patch('llm_vs_zombies.cli.create_run', return_value=run))
            stack.enter_context(patch('llm_vs_zombies.launcher.start', return_value={'pid':123}))
            stack.enter_context(patch('llm_vs_zombies.launcher.stop', side_effect=lambda _:order.append('process_stop')))
            stack.enter_context(patch('llm_vs_zombies.client.connect', return_value=client))
            stack.enter_context(patch('llm_vs_zombies.session.SessionTrace', return_value=trace))
            stack.enter_context(patch.object(ev,'host_sources',return_value={'matches_archive':True,'unchanged':True}))
            stack.enter_context(patch.object(ev,'LaunchWindowMonitor',Monitor))
            with self.assertRaises(ReplayDivergence):
                with ev.live_session(root,'run',ev.Plan(),42):
                    raise ReplayDivergence({'stage':'state/rng'})
            self.assertLess(order.index('stop_recording'), order.index('runtime_close'))
            self.assertLess(order.index('runtime_close'), order.index('process_stop'))
            cleanup=json.loads((run/'evaluation-cleanup.json').read_text())
            self.assertEqual(cleanup['runtime_observer_error']['message'],'window read failed')
            self.assertEqual(cleanup['trace_close_error']['message'],'trace close failed')
            self.assertTrue(cleanup['owned_process_stopped'])
            self.assertEqual(json.loads((run/'evaluation-error.json').read_text())['report']['stage'],'state/rng')


class SuiteTests(unittest.TestCase):
    """Small orchestrator model; replay/native evidence are explicitly fixture-only."""
    def test_lifecycle_fixture_cannot_pass_production_suite_or_start_colds(self):
        from llm_vs_zombies import cold_workers
        for workers in (1, 2):
            with self.subTest(workers=workers), \
                 patch.object(cold_workers, 'run_parallel_colds') as pool:
                report = self.run_fixture(fixture_runtime=True, cold_workers=workers)
            pool.assert_not_called()
            self.assertEqual(self.roles, ["source"])
            self.assertEqual(self.closed, ["source"])
            self.assertFalse(report["readiness"]["experiment_ready"])
            self.assertIn("test_fixture runtime", report["cases"][0]["error"]["message"])

    def run_fixture(self, *, strict=False, win=False, runtime_failure=None, strategy_error=False,
                    cold_error=False, disk_stop=False, fixture_runtime=False, cold_workers=1,
                    rewrite_initial=False):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name); (root/'experiments/runs').mkdir(parents=True)
        policy = root/'policy.py'; policy.write_text('def decide(observation, context):\n'
            "    return {'actions': [], 'advance_ticks': min(1, context['remaining_ticks'])}\n")
        plan = ev.Plan(tier='strict' if strict else 'smoke', seeds=(42,), tick_budget=3, chunk_ticks=1,
            cold_starts=10 if strict else 1, cold_workers=cold_workers,
            strategy=str(policy), pause_points=(), min_free_bytes=1)
        initial = {'observation':observation(0), 'state':{'fixture':1},
                   'initialization':{'clock_anchor':{}}, 'identity':{}}
        self.roles=[]; self.closed=[]; self.tail_checked=False
        outer=self

        class FakeClient:
            def __init__(self): self.tick=0; self.trace=Mock(); self.hello_result={"game":{"test_fixture":{"mode":"test_flag_drop_v1"}}} if fixture_runtime else {}
            @property
            def version(self): return version(self.tick)
            def observe(self): return observation(self.tick,completed=win and self.tick>=3,ui=2 if win and self.tick>=3 else 3)
            def request(self, method, *args, **kwargs): return {'state':{'fixture':1}}
            def advance(self,n): self.tick+=n; return {'executed_ticks':n,'observation':self.observe()}
            def commit(self, actions, advance_ticks):
                if strategy_error: raise ValueError('actual strategy request failed')
                self.tick+=advance_ticks
                return {'observation':self.observe(),'action_results':[],
                        'stop_reason':'scene_changed' if win and self.tick>=3 else 'budget_exhausted'}

        @contextmanager
        def session(root,name,plan,seed,*,lifecycle=None):
            role='recovery' if name.endswith('recovery') else 'source' if name.endswith('c0') else 'cold'
            self.roles.append(role)
            run=root/'experiments/runs'/name; run.mkdir()
            if lifecycle is not None: lifecycle['created_run']=str(run)
            (run/'inputs').mkdir(); (run/'inputs/implementation.zip').write_bytes(b'fixture archive')
            (run/'observations').mkdir(); write_json(run/'observations/initial.json',{})
            write_json(run/'manifest.json',{'status':'recording'})
            write_json(run/'launcher.json',{'isolation_ready':True})
            write_json(run/'evaluation-windows.json',{'foreground_check_status':'pass'})
            state={'scenario_verified':True,'isolation_ready':True,'pid':123}
            try:
                client=FakeClient(); client._initialization_run=run
                yield run,state,client,Mock()
            finally:
                self.closed.append(role)
                write_json(run/'evaluation-runtime-windows.json',{'schema':'lvz.launch-windows.v3',
                    'foreground_check_status':'unverified' if runtime_failure==role else 'pass'})
                write_json(run/'evaluation-host-final.json',{'matches_archive':True,'unchanged':True})
                write_json(run/'evaluation-cleanup.json',dict(recording_closed=True,client_closed=True,
                    trace_closed=True,owned_process_stopped=True))

        def finalized(run,plan,outcome,*,package,primary_error):
            role='recovery' if run.name.endswith('recovery') else 'source' if run.name.endswith('c0') else 'cold'
            outer.assertIn(role,outer.closed)
            receipt=dict(passed_recording=True,archive_sealed=True,secondary_errors=[],cleanup_passed=True,
                cleanup={},trajectory_verified=package,primary_error=primary_error)
            return receipt, SimpleNamespace(initial=initial,manifest={'trajectory_id':'fixture'}) if package else None

        def fake_replay(trajectory,initializer,destination):
            destination.mkdir()
            try:
                with initializer(trajectory,destination) as active:
                    if cold_error: raise ReplayDivergence({'stage':'fixture divergence'})
                    # An observer failure at close must not interrupt this tail.
                    active.client.client.tick=3
                self.tail_checked=True
                report={'equal':True,'final_health_verified':True}
                write_json(destination/'replay-report.json',report)
                return report
            except Exception:
                write_json(destination/'replay-report.json',{'equal':False})
                raise

        def recovery(root,name,plan,seed,*,lifecycle):
            with session(root,name,plan,seed,lifecycle=lifecycle) as (run,*_):
                write_json(run/'evaluation-resources.json',{'stop':None})
                write_json(run/'recovery-probe.json',{'fixture':True})
            return run, {'fixture':True}

        with ExitStack() as stack:
            stack.enter_context(patch.object(ev,'os',SimpleNamespace(name='nt')))
            stack.enter_context(patch.object(ev,'profile_fingerprint',return_value={}))
            stack.enter_context(patch.object(ev,'live_session',session))
            if rewrite_initial:
                # The real apply_recipe persists the B0-bound observation into
                # observations/initial.json, which is exactly what a gate
                # recorded before that rewrite would hash incorrectly.
                def apply(client,seed,*args,**kwargs):
                    write_json(Path(client._initialization_run)/'observations/initial.json',
                               {'b0':observation(client.tick)})
                    return {'clock_anchor':{}}
                stack.enter_context(patch.object(ev,'apply_recipe',side_effect=apply))
            else:
                stack.enter_context(patch.object(ev,'apply_recipe',return_value={'clock_anchor':{}}))
            stack.enter_context(patch.object(ev,'target_from_recipe',return_value=None))
            stack.enter_context(patch.object(ev,'_pause_probe',return_value={'fixture':True}))
            stack.enter_context(patch.object(ev,'finalize_run',side_effect=finalized))
            stack.enter_context(patch.object(ev,'_recovery_probe',side_effect=recovery))
            stack.enter_context(patch('llm_vs_zombies.engine_replay.capture_initial',return_value=initial))
            stack.enter_context(patch('llm_vs_zombies.engine_replay.identity_from_launcher',return_value={}))
            stack.enter_context(patch('llm_vs_zombies.engine_replay.replay',side_effect=fake_replay))
            stack.enter_context(patch('llm_vs_zombies.audit_compare.AuditLog',return_value=SimpleNamespace(
                frames=[],verify_files=lambda:None,engine_call_health={},sound_effects_health={},draw_health={})))
            if disk_stop:
                stack.enter_context(patch('llm_vs_zombies.evaluation_support.shutil.disk_usage',return_value=SimpleNamespace(free=0)))
            result=ev.run_suite(root,plan,root/'experiments/runs/suite',run_builds=False)
        self.assertFalse(result['readiness']['experiment_ready']) # Fixtures never establish ready.
        return result

    def test_strict_incomplete_skips_nine_cold_but_retains_real_source(self):
        report=self.run_fixture(strict=True)
        self.assertEqual(self.roles,['source'])
        self.assertEqual(self.closed,['source'])
        self.assertEqual(report['cases'][0]['status'],'incomplete')
        self.assertEqual(report['checks']['full_cycle']['status'],'fail')
        self.assertEqual(report['checks']['archive_integrity']['status'],'pass')

    def test_smoke_runs_true_cold_and_recovery_and_retention_hashes_stay_valid(self):
        report=self.run_fixture()
        self.assertEqual(self.roles,['source','cold','recovery'])
        self.assertTrue(self.tail_checked)
        self.assertEqual(report['cases'][0]['status'],'completed')
        self.assertEqual(report['statistics']['cold_starts_verified'],2)
        self.assertNotIn('archive_integrity',report['readiness']['unmet_gates'])
        self.assertNotIn('session_cleanup',report['readiness']['unmet_gates'])

    def test_scenario_gate_hashes_the_observation_written_by_the_recipe(self):
        report=self.run_fixture(rewrite_initial=True)
        self.assertEqual(self.roles,['source','cold','recovery'])
        self.assertEqual(report['cases'][0]['status'],'completed')
        run=Path(report['cases'][0]['run'])
        self.assertEqual(json.loads((run/'observations/initial.json').read_text())['b0'],observation(0))
        for name in ev.LIVE_GATES:
            for artifact in report['checks'][name].get('artifacts',[]):
                path=Path(artifact['path'])
                if path.is_relative_to(run):
                    self.assertEqual(ev.sha256(path),artifact['sha256'],name)

    def test_run_local_gate_artifact_is_rejected_after_a_rewrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            run=Path(temporary)/'run'; run.mkdir()
            target=run/'evidence.json'; write_json(target,{'a':1})
            item={'artifacts':[{'path':str(target),'sha256':ev.sha256(target)}]}
            ev.verify_run_artifacts([item],run)
            write_json(target,{'a':2})
            with self.assertRaisesRegex(RuntimeError,'gate artifact changed'):
                ev.verify_run_artifacts([item],run)

    def test_cold_window_failure_does_not_skip_native_tail_or_seal(self):
        report=self.run_fixture(runtime_failure='cold')
        self.assertTrue(self.tail_checked)
        self.assertEqual(self.closed,['source','cold'])
        self.assertEqual(report['checks']['engine_replay']['status'],'pass')
        self.assertEqual(report['checks']['runtime_windows']['status'],'unverified')
        self.assertEqual(report['cases'][0]['status'],'failed')
        self.assertTrue(report['cases'][0]['sessions'][-1]['archive_sealed'])

    def test_source_window_failure_names_the_blocking_component(self):
        # A 1,642s source run once recorded a single 0.605s observer probe out
        # of 65,437 samples (limit 0.25s). The aggregate was correctly
        # unverified, but the skip reason blamed "infrastructure or recording"
        # while every recording gate passed, which sent the reader looking at
        # the replay side. Name the component that actually blocked.
        report=self.run_fixture(runtime_failure='source')
        self.assertEqual(self.roles,['source'])
        case=report['cases'][0]
        self.assertEqual(case['status'],'failed')
        self.assertEqual(case['replay_blockers'],['runtime_windows'])
        self.assertEqual(case['replays_skipped'],
                         'source infrastructure or recording did not pass: runtime_windows')
        session=case['sessions'][0]
        self.assertEqual(session['infrastructure_components'],
                         {'recording':'pass','runtime_windows':'unverified','private_launch':'pass',
                          'host_identity':'pass','resource_limits':'pass'})
        self.assertEqual(session['infrastructure_failures'],['runtime_windows'])
        self.assertFalse(session['infrastructure_passed'])
        self.assertEqual(report['checks']['recording']['status'],'pass')

    def test_recovery_window_failure_is_not_hidden_by_same_seed_passes(self):
        report=self.run_fixture(runtime_failure='recovery')
        self.assertEqual(self.roles,['source','cold','recovery'])
        self.assertEqual(report['checks']['runtime_windows']['status'],'unverified')
        self.assertEqual(report['checks']['disconnect_recovery']['status'],'fail')
        self.assertEqual(report['cases'][0]['status'],'failed')

    def test_strategy_and_cold_divergence_close_and_retain_primary(self):
        report=self.run_fixture(strategy_error=True)
        self.assertEqual(self.roles,['source'])
        self.assertEqual(report['cases'][0]['error']['message'],'actual strategy request failed')
        self.assertTrue(report['cases'][0]['sessions'][0]['archive_sealed'])
        report=self.run_fixture(cold_error=True)
        self.assertEqual(self.roles,['source','cold'])
        self.assertEqual(report['cases'][0]['error']['report']['stage'],'fixture divergence')
        self.assertTrue(report['cases'][0]['sessions'][-1]['archive_sealed'])

    def test_source_disk_stop_closes_and_prevents_later_sessions(self):
        report=self.run_fixture(disk_stop=True)
        self.assertEqual(self.roles,['source'])
        self.assertEqual(self.closed,['source'])
        self.assertEqual(report['cases'][0]['outcome'],'disk_reserve_stop')
        self.assertEqual(report['checks']['resource_limits']['status'],'fail')

    def test_completed_cycle_runs_all_nine_exact_cold_sessions(self):
        report=self.run_fixture(strict=True,win=True)
        self.assertEqual(self.roles,['source']+['cold']*9+['recovery'])
        self.assertEqual(report['statistics']['cold_starts_verified'],10)
        self.assertEqual(report['checks']['ten_cold_starts']['status'],'pass')
        self.assertEqual(report['statistics']['full_cycle_successes'],1)
