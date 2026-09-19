"""Protocol fixtures for initialization ordering, not native determinism evidence."""
import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from llm_vs_zombies.client import Client, ProtocolError
from llm_vs_zombies.initialization import (DRAW_MODE, LEGACY_DRAW_MODE, apply_recipe,
                                         clock_anchor, draw_mode, ensure_render_prepared)
from llm_vs_zombies.launcher import DEFAULT_CARDS, REFERENCE_LAYOUT, start
from llm_vs_zombies.session import SessionTrace


class PreparationRuntime:
    """Small stateful RPC fixture; warm genuinely changes its recorded RNG state."""
    def __init__(self, *, controlled=True, menu=False):
        self.requests = []
        self.closed = False
        self.controlled = controlled
        self.ready = not menu
        self.prepared = False
        self.revision = 0
        self.tick = 0
        self.draws = 0
        self.seed = None
        self.corrupt_seed = False
        self.fail_warm = False
        self.bad_warm_clock = False
        self.capture_override = {}
        self.capture_error = None
        self.cache_version = None
        self.state = {"schema": "lvz.audit.v1", "app": {"mj_clock": 30},
            "board": {"00005568": 100, "0000556c": 200},
            "rng": {"target": "fixture-only", "instances": {"global_mt": {"cursor": 0, "words": [0]*624},
                    "game_thread_crt": {"state": 0}}}}
        caps = {"rng_seed": True, "clock_restore": True, "audit_snapshot": True, "capture_frame": True}
        self.hello_value = {"capabilities": caps, "game": {}}
        if controlled:
            caps.update(prepare_render=True, deterministic_draw_schedule_v1=True)
            self.hello_value["game"]["draw_schedule"] = dict(mode=DRAW_MODE, installed=True,
                autonomous_fight_draws=False, capture="cached_bgr24_only", rng_restore_after_draw=False,
                original_engine_bitwise_unmodified=False, live_verified=False)
            self.state['draw_schedule'] = dict(mode=DRAW_MODE, warm_frames=0, step_frames=0)

    @property
    def version(self):
        return dict(epoch=3, tick=self.tick, revision=self.revision)

    def observe(self):
        value = dict(version=self.version, game_clock=self.state['board']['00005568'],
            game_ui=3 if self.ready else 1, scene=3, wave=0, sun=8000,
            initialization={'state': 'ready' if self.ready else 'idle'},
            plants=[dict(type=kind, row=row, col=col, hp=300) for kind, grids in REFERENCE_LAYOUT.items() for row,col in grids],
            seeds=[dict(type=kind) if kind < 49 else dict(type=48, imitator_type=kind-49) for kind in DEFAULT_CARDS], zombies=[])
        if self.controlled:
            value['render_prepared'] = self.prepared
        return value

    def exchange(self, payload, timeout):
        request = json.loads(payload)
        self.requests.append(request)
        method, params = request['method'], request['params']
        if method == 'hello': result = self.hello_value
        elif method == 'observe': result = self.observe()
        elif method == 'status': result = {'state': 'paused_at_boundary'}
        elif method == 'audit_snapshot': result = {'state': self.state, 'version': self.version}
        else:
            if request.get('expect') != self.version:
                raise AssertionError('fixture received stale/absent exact expectation')
            if method == 'initialize':
                self.ready = True
                self.revision += 1
                result = {'observation': self.observe()}
            elif method == 'rng_seed':
                self.seed = params['seed']
                words = [self.seed or 4357]
                for i in range(1,624):
                    words.append((1812433253*(words[-1]^(words[-1]>>30))+i)&0xffffffff)
                if self.corrupt_seed: words[513] ^= 1
                self.state['rng']['instances'] = {'global_mt': {'cursor':624,'words':words}, 'game_thread_crt':{'state':self.seed}}
                self.revision += 1
                result = {'observation':self.observe()}
            elif method == 'clock_restore':
                anchor = params['snapshot']
                self.state['app']['mj_clock'] = anchor['mj_clock']
                self.state['board']['00005568'] = anchor['game_clock']
                self.state['board']['0000556c'] = anchor['effect_clock']
                self.revision += 1
                result = {'observation':self.observe()}
            elif method == 'prepare_render':
                if self.prepared: raise AssertionError('duplicate warm draw')
                if self.fail_warm:
                    return json.dumps(dict(protocol=1,request_id=request['request_id'],ok=False,
                        error=dict(code='render_failed',message='fixture drawing failure'))).encode()
                before_clock = clock_anchor({'state':self.state})
                self.draws += 1
                self.revision += 1
                self.prepared = True
                self.state['draw_schedule']['warm_frames'] = 1
                self.cache_version = self.version
                self.state['rng']['instances']['global_mt']['cursor'] = 3
                self.state['rng']['instances']['global_mt']['words'][0] ^= 0x5432
                self.state['rng']['instances']['game_thread_crt']['state'] = (214013*self.seed+2531011)&0xffffffff
                if self.bad_warm_clock: self.state['board']['00005568'] += 1
                render = dict(mode=DRAW_MODE,phase='warm',frame_version=self.version,rng_restored=False,
                    clocks_before=before_clock,clocks_after=clock_anchor({'state':self.state}),
                    counts=self.state['draw_schedule'], seed_readback=dict(seed=self.seed,global_mt_words=624,
                        global_mt_cursor=624,game_thread_crt=self.seed,verified_before_draw=True))
                result = dict(prepared=True,render=render,observation=self.observe())
            elif method == 'capture_frame':
                if self.capture_error: raise self.capture_error
                result = dict(capture_ok=True,source='original_game_frame',version=self.version,frame_version=self.version,
                    method='cached_controlled_engine_frame',mode=DRAW_MODE,forced_render=False,known_rng_unchanged=True,
                    width=2,height=2,pixel_format='bgr24',pixels_base64=base64.b64encode(b'abcdefghijkl').decode())
                result.update(self.capture_override)
                if self.cache_version != self.version:
                    result = dict(capture_ok=False,forced_render=False,reason='frame_cache_stale',version=self.version)
            elif method == 'commit':
                if params['advance_ticks'] != 0: raise AssertionError('fixture only exercises action-only invalidation')
                self.revision += 1
                result = dict(requested_ticks=0,executed_ticks=0,stop_reason='budget_exhausted',
                    action_results=[{'ok':True,'action':a} for a in params['actions']],observation=self.observe())
            else: raise AssertionError(method)
        return json.dumps(dict(protocol=1,request_id=request['request_id'],ok=True,result=result)).encode()

    def close(self): self.closed = True


class AppAnchorPreparationRuntime(PreparationRuntime):
    """Actual fixture memory changes once; receipts retain both raw states."""
    def __init__(self, count=1295, fault=None):
        from llm_vs_zombies import app_update_anchor as app
        from llm_vs_zombies import sound_effects as audio
        from test_sound_effects import SPEC, sound_state
        from test_draw_schedule import DRAW_SPEC
        super().__init__()
        self.anchored = False
        self.anchor_calls = 0
        self.anchor_fault = fault
        self.state['app']['ui'] = 3
        self.state['sound_effects'] = sound_state()
        self.state['sound_effects']['app_update_count'] = count
        self.hello_value['game'].update(target='fixture-only', sound_effects=copy.deepcopy(SPEC),
            draw_schedule=copy.deepcopy(DRAW_SPEC), app_update_anchor=copy.deepcopy(app.SPEC))
        self.hello_value['capabilities'].update({audio.MODE: True, app.MODE: True, app.METHOD: True})

    def observe(self):
        return dict(super().observe(), app_update_anchored=self.anchored)

    def exchange(self, payload, timeout):
        from llm_vs_zombies.app_update_anchor import MODE
        request = json.loads(payload)
        if request['method'] != 'app_update_anchor':
            reply = super().exchange(payload, timeout)
            if request['method'] == 'rng_seed':
                self.state['rng']['instances']['global_mt']['algorithm'] = 'sexy_mt19937_31'
                self.state['rng']['instances']['game_thread_crt']['algorithm'] = 'msvc_lcg_15'
            return reply
        self.requests.append(request)
        if request.get('expect') != self.version or self.anchored or self.prepared or self.seed is None:
            raise AssertionError('invalid fixture App anchor order')
        before_state, before_version = copy.deepcopy(self.state), self.version
        target = request['params']['app_update_count']
        self.state['sound_effects']['app_update_count'] = target
        self.anchor_calls += 1
        self.anchored = True
        self.revision += 1
        demo = dict.fromkeys(('00000510', '00000511', '00000578', '0000049c', '000004a0'), 0)
        receipt = dict(schema='lvz.app-update-anchor.v1', mode=MODE, requested=target,
            before=before_state['sound_effects']['app_update_count'], after=target,
            before_state=before_state, after_state=copy.deepcopy(self.state),
            before_version=before_version, after_version=self.version, demo_before=demo, demo_after=demo.copy())
        if self.anchor_fault == 'other_field': receipt['after_state']['rng']['instances']['game_thread_crt']['state'] ^= 1
        result = dict(anchored=True, anchor=receipt, observation=self.observe())
        if self.anchor_fault == 'flag': result['observation']['app_update_anchored'] = False
        encoded = json.dumps(dict(protocol=1, request_id=request['request_id'], ok=True, result=result)).encode()
        if self.anchor_fault == 'readback': self.state['sound_effects']['app_update_count'] += 1
        return encoded


class CounterPreparationRuntime(AppAnchorPreparationRuntime):
    """Lifetime instrumentation stays intact; warm contributes to the epoch."""
    def __init__(self, count=1362, raw_calls=14, fault=None):
        from llm_vs_zombies import sound_counter as counter
        super().__init__(count)
        self.raw_calls = raw_calls
        self.counter_bound = False
        self.counter_fault = fault
        self.state['sound_effects'].update(calls=raw_calls, counter_scope='bootstrap_lifetime')
        self.hello_value['game']['sound_counter'] = copy.deepcopy(counter.SPEC)
        self.hello_value['capabilities'].update({counter.MODE: True, counter.METHOD: True})

    def observe(self):
        return dict(super().observe(), counter_origin_bound=self.counter_bound)

    def exchange(self, payload, timeout):
        from llm_vs_zombies import sound_counter as counter
        from test_sound_effects import activation
        request = json.loads(payload)
        if request['method'] != counter.METHOD:
            if request['method'] in ('app_update_anchor', 'prepare_render') and not self.counter_bound:
                raise AssertionError('origin must precede App anchoring and warm')
            reply = super().exchange(payload, timeout)
            if request['method'] == 'prepare_render':
                self.raw_calls += 3
                self.state['sound_effects']['calls'] += 3
            return reply
        self.requests.append(request)
        if (request.get('expect') != self.version or request['params'] != {} or self.counter_bound
                or self.anchored or self.prepared or self.seed is None):
            raise AssertionError('invalid fixture origin order')
        before_state, before_version = copy.deepcopy(self.state), self.version
        raw = activation()['recorder_attach']
        raw['calls'] = self.raw_calls
        self.state['sound_effects'].update(calls=0, counter_scope='experiment')
        self.counter_bound = True
        self.revision += 1
        receipt = dict(schema='lvz.sound-counter-origin.v1', mode=counter.MODE, origin_raw_calls=self.raw_calls,
            raw_before=copy.deepcopy(raw), raw_after=copy.deepcopy(raw), before_state=before_state,
            after_state=copy.deepcopy(self.state), before_version=before_version, after_version=self.version)
        if self.counter_fault == 'other_field':
            receipt['after_state']['rng']['instances']['game_thread_crt']['state'] ^= 1
        if self.counter_fault == 'raw_changed': receipt['raw_after']['calls'] += 1
        result = dict(bound=True, counter_origin=receipt, observation=self.observe())
        if self.counter_fault == 'flag': result['observation']['counter_origin_bound'] = False
        encoded = json.dumps(dict(protocol=1, request_id=request['request_id'], ok=True, result=result)).encode()
        if self.counter_fault == 'readback': self.state['sound_effects']['calls'] = 1
        return encoded


class InitializationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.trace = SessionTrace(self.root/'trace.jsonl')
        self.runtime = PreparationRuntime()
        self.client = Client(self.runtime, trace=self.trace)
        self.client.hello()

    def tearDown(self):
        self.client.close(); self.trace.close(); self.temp.cleanup()

    def methods(self): return [r['method'] for r in self.runtime.requests]

    def test_source_seed_restore_readback_warm_order_preserves_consumed_rng(self):
        recipe = apply_recipe(self.client,42)
        self.assertEqual(self.methods(), ['hello','hello','observe','status','audit_snapshot','rng_seed',
            'clock_restore','audit_snapshot','prepare_render','audit_snapshot','observe'])
        self.assertEqual(self.runtime.draws,1)
        self.assertEqual(self.client.version,dict(epoch=3,tick=0,revision=3))
        self.assertEqual(recipe['execution_mode'],DRAW_MODE)
        self.assertEqual(recipe['clock_anchor'],recipe['postwarm_clock'])
        self.assertNotEqual(recipe['render_preparation']['seeded_rng_sha256'],recipe['render_preparation']['postwarm_rng_sha256'])
        self.assertEqual(self.runtime.state['rng']['instances']['global_mt']['cursor'],3)
        self.assertNotIn('version',recipe)
        self.assertNotIn('render',recipe)

    def test_seed_zero_uses_original_mt_fallback_and_crt_zero_before_warm(self):
        recipe = apply_recipe(self.client,0)
        self.assertEqual(recipe['seed'],0)
        self.assertEqual(self.runtime.draws,1)

    def test_cold_anchor_and_source_recipe_match_without_version_fields(self):
        recipe = apply_recipe(self.client,42)
        second = PreparationRuntime()
        second.state['app']['mj_clock'] = 999
        with SessionTrace(self.root/'second.jsonl') as trace:
            client = Client(second,trace=trace)
            actual = apply_recipe(client,42,recipe['clock_anchor'])
            self.assertEqual(recipe,actual)
            self.assertEqual(client.version,self.client.version)
            client.close()

    def test_duplicate_preparation_rejected_before_any_seed_mutation(self):
        apply_recipe(self.client,42)
        count = self.methods().count('rng_seed')
        with self.assertRaisesRegex(RuntimeError,'do not reseed'):
            apply_recipe(self.client,42)
        self.assertEqual(self.methods().count('rng_seed'),count)
        self.assertEqual(self.runtime.draws,1)
        self.assertIsNone(ensure_render_prepared(self.client,99))
        self.assertEqual(self.methods().count('rng_seed'),count)

    def test_bad_seed_word_stops_before_warm(self):
        self.runtime.corrupt_seed = True
        with self.assertRaisesRegex(RuntimeError,'RNG readback'):
            apply_recipe(self.client,42)
        self.assertNotIn('prepare_render',self.methods())

    def test_failed_draw_is_not_retried_or_reseeded(self):
        from llm_vs_zombies.client import RemoteError
        self.runtime.fail_warm = True
        with self.assertRaises(RemoteError): apply_recipe(self.client,42)
        self.assertEqual(self.methods().count('rng_seed'),1)
        self.assertEqual(self.methods().count('prepare_render'),1)

    def test_changed_warm_clock_is_rejected(self):
        self.runtime.bad_warm_clock = True
        with self.assertRaisesRegex(RuntimeError,'warm boundary'):
            apply_recipe(self.client,42)
        self.assertEqual(self.runtime.draws,1)

    def test_legacy_mode_remains_legacy_and_does_not_call_warm(self):
        legacy = PreparationRuntime(controlled=False)
        with SessionTrace(self.root/'legacy.jsonl') as trace:
            client = Client(legacy,trace=trace)
            recipe = apply_recipe(client,42)
            self.assertEqual(recipe['execution_mode'],LEGACY_DRAW_MODE)
            self.assertNotIn('draw_schedule',recipe)
            self.assertNotIn('prepare_render',[r['method'] for r in legacy.requests])
            self.assertIsNone(ensure_render_prepared(client))
            with self.assertRaises(ProtocolError): client.prepare_render()
            client.close()

    def test_partial_or_unknown_draw_contract_is_rejected(self):
        for bad in ({'capabilities':{'prepare_render':True}},
                    {'capabilities':{},'game':{'draw_schedule':{'mode':'future'}}}):
            with self.assertRaisesRegex(RuntimeError,'draw schedule contract'): draw_mode(bad)

    def test_persistence_binds_postwarm_audit_and_mode_without_acceptance_claim(self):
        run = self.root/'run'; run.mkdir()
        (run/'manifest.json').write_text(json.dumps({'status':'recording','initial_state':{'captured':False}}))
        recipe = apply_recipe(self.client,42,run=run,scenario_verified=True)
        manifest = json.loads((run/'manifest.json').read_text())
        self.assertTrue(manifest['initial_state']['render_prepared'])
        self.assertTrue(manifest['initial_state']['audit_snapshot_captured'])
        self.assertFalse(manifest['capabilities']['original_engine_replay_verified'])
        snap = json.loads((run/'observations/initial-audit.json').read_text())
        self.assertEqual(snap['state']['rng'],self.runtime.state['rng'])
        self.assertEqual(snap['version'],self.client.version)
        self.assertEqual(json.loads((run/'initialization-recipe.json').read_text()),recipe)
        evidence = json.loads((run/'initialization-evidence.json').read_text())
        self.assertEqual(evidence['prepare_render']['render']['frame_version'],self.client.version)

    def test_finalized_run_fails_before_game_mutation(self):
        run = self.root/'sealed'; run.mkdir()
        (run/'manifest.json').write_text('{"status":"finalized"}')
        with self.assertRaisesRegex(ValueError,'finalized'): apply_recipe(self.client,42,run=run)
        self.assertNotIn('rng_seed',self.methods())

    def test_cached_capture_is_read_only_and_does_not_warm_again(self):
        apply_recipe(self.client,42)
        before = copy.deepcopy(self.runtime.state)
        self.client.capture_frame(); self.client.capture_frame()
        self.assertEqual(self.runtime.state,before)
        self.assertEqual(self.runtime.draws,1)
        self.assertEqual(self.methods().count('prepare_render'),1)

    def test_cached_capture_timeout_is_not_a_mutation_unknown_outcome(self):
        apply_recipe(self.client,42)
        self.runtime.capture_error = TimeoutError('fixture read timeout')
        with self.assertRaises(TimeoutError): self.client.capture_frame()

    def test_cached_capture_cannot_relabel_a_stale_revision_or_force_render(self):
        apply_recipe(self.client,42)
        self.runtime.capture_override = {'frame_version':dict(epoch=3,tick=0,revision=2)}
        with self.assertRaisesRegex(ProtocolError,'exact cached boundary'): self.client.capture_frame()

    def test_cached_capture_cannot_force_a_new_engine_draw(self):
        apply_recipe(self.client,42)
        self.runtime.capture_override = {'forced_render':True}
        with self.assertRaisesRegex(ProtocolError,'must not render'): self.client.capture_frame()


class LauncherPreparationTests(unittest.TestCase):
    def test_disk_failure_after_native_launch_still_stops_owned_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = dict(sandbox=str(root/'sandbox'), engine='engine', bootstrap='bootstrap', runtime='recorder')
            with patch('llm_vs_zombies.launcher.prepare', return_value=state), \
                 patch('llm_vs_zombies.launcher._native', side_effect=[dict(pid=123, creation_time=456), {}]) as native, \
                 patch('llm_vs_zombies.launcher.write_json', side_effect=OSError('disk full')):
                with self.assertRaisesRegex(OSError, 'disk full') as caught:
                    start(root, root/'run')
            self.assertEqual([call.args[1] for call in native.call_args_list], ['launch', 'stop'])
            self.assertEqual(native.call_args.args[2:5], (123, 'engine', 456))
            self.assertTrue(state['process_cleaned_up'])
            self.assertTrue(any('Cannot persist' in note for note in caught.exception.__notes__))

    def test_explicit_audio_launch_binds_receipt_and_cleans_up_mismatch(self):
        from llm_vs_zombies.sound_effects import MODE
        from test_sound_effects import SPEC, BOOTSTRAP, activation
        for fault in (None, 'missing_capability', 'wrong_bootstrap', 'late_calls', 'missing_receipt'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); run = root/'experiments/runs/test'
                (run/'observations').mkdir(parents=True); (run/'sandbox').mkdir()
                runtime = PreparationRuntime(menu=True)
                runtime.hello_value['game']['sound_effects'] = copy.deepcopy(SPEC)
                runtime.hello_value['capabilities'][MODE] = fault != 'missing_capability'
                receipt = activation()['pre_resume']
                if fault == 'late_calls': receipt['calls'] = 1
                calls = []
                state = dict(sandbox=str(run/'sandbox'), engine='engine', bootstrap='bootstrap', runtime='recorder',
                             module_hashes={'lvz-bootstrap.dll': 'f'*64 if fault == 'wrong_bootstrap' else BOOTSTRAP})
                def native(state, verb, *args):
                    calls.append((verb, args))
                    if verb == 'launch':
                        result = dict(pid=123, creation_time=456)
                        if fault != 'missing_receipt': result['audio_activation'] = receipt
                        return result
                    return {}
                client = Client(runtime, trace=Mock()); client.hello()
                with patch('llm_vs_zombies.launcher.prepare', return_value=state), \
                     patch('llm_vs_zombies.launcher._native', side_effect=native), \
                     patch('llm_vs_zombies.client.connect', return_value=client):
                    if fault:
                        with self.assertRaises(ValueError):
                            start(root, run, seed=42, defer_preparation=True, audio_mode=MODE)
                    else:
                        result = start(root, run, seed=42, defer_preparation=True, audio_mode=MODE)
                        self.assertEqual(result['audio_mode'], MODE)
                self.assertEqual(calls[0][1][-1], MODE)
                if fault:
                    self.assertEqual(calls[-1][0], 'stop')
                    self.assertTrue(json.loads((run/'launcher.json').read_text())['process_cleaned_up'])
                    self.assertNotIn('initialize', [r['method'] for r in runtime.requests])
                else:
                    self.assertEqual([c[0] for c in calls], ['launch', 'inject'])

    def test_default_launch_prepares_and_deferred_launch_leaves_warm_to_recipe(self):
        for deferred in (False,True):
            with self.subTest(deferred=deferred), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); run=root/'experiments/runs/test'; run.mkdir(parents=True)
                (run/'observations').mkdir(); (run/'sandbox').mkdir()
                (run/'manifest.json').write_text(json.dumps({'status':'recording','initial_state':{'captured':False}}))
                runtime = PreparationRuntime(menu=True)
                with SessionTrace(run/'decisions/fixture.jsonl') as trace:
                    client = Client(runtime,trace=trace); client.hello()
                    state = dict(sandbox=str(run/'sandbox'),engine='engine',bootstrap='bootstrap',runtime='recorder')
                    def native(state,verb,*args):
                        self.assertIn(verb,('launch','inject'))
                        return dict(pid=123,creation_time=456) if verb=='launch' else {}
                    with patch('llm_vs_zombies.launcher.prepare',return_value=state), \
                         patch('llm_vs_zombies.launcher._native',side_effect=native), \
                         patch('llm_vs_zombies.client.connect',return_value=client):
                        result=start(root,run,seed=42,defer_preparation=deferred)
                self.assertEqual(runtime.draws,0 if deferred else 1)
                self.assertEqual(result['render_prepared'],not deferred)
                manifest=json.loads((run/'manifest.json').read_text())
                self.assertEqual(manifest['initial_state']['captured'],not deferred)
                if deferred:
                    self.assertTrue((run/'observations/scenario-ready.json').is_file())
                    self.assertFalse((run/'initialization-recipe.json').exists())

    def test_repl_default_prepares_but_explicit_defer_does_not(self):
        from llm_vs_zombies.repl import main
        for deferred in (False,True):
            with self.subTest(deferred=deferred), tempfile.TemporaryDirectory() as directory:
                root=Path(directory); script=root/'policy.py'; script.write_text('pass')
                runtime=PreparationRuntime()
                def connected(*,trace,**kwargs):
                    client=Client(runtime,trace=trace); client.hello(); return client
                arguments=['--pid','123','--trace',str(root/'repl.jsonl'),'--script',str(script),'--seed','42']
                if deferred: arguments.append('--defer-preparation')
                with patch('llm_vs_zombies.repl.connect',side_effect=connected):
                    self.assertEqual(main(arguments),0)
                self.assertEqual(runtime.draws,0 if deferred else 1)


class SoundPreparationTests(unittest.TestCase):
    def test_recipe_binds_complete_sound_state_and_keeps_raw_history(self):
        from llm_vs_zombies import sound_effects as audio
        from test_sound_effects import SPEC, sound_state
        runtime = PreparationRuntime()
        runtime.hello_value['game']['sound_effects'] = copy.deepcopy(SPEC)
        runtime.hello_value['capabilities'][audio.MODE] = True
        runtime.state['sound_effects'] = sound_state()
        before = copy.deepcopy(runtime.state['sound_effects'])
        with Client(runtime, trace=Mock()) as client:
            client.hello()
            recipe = apply_recipe(client, 42)
        self.assertEqual(recipe['sound_effects'], dict(configuration=SPEC, b0_state_sha256=audio.state_sha256(before)))
        self.assertEqual(runtime.state['sound_effects'], before)
        self.assertEqual(runtime.draws, 1)

    def test_incomplete_or_occupied_sound_state_stops_before_warm(self):
        from llm_vs_zombies import sound_effects as audio
        from test_sound_effects import SPEC, sound_state
        for fault in ('missing_state', 'occupied_last_slot', 'occupied_channel'):
            with self.subTest(fault=fault):
                runtime = PreparationRuntime()
                runtime.hello_value['game']['sound_effects'] = copy.deepcopy(SPEC)
                runtime.hello_value['capabilities'][audio.MODE] = True
                if fault != 'missing_state':
                    runtime.state['sound_effects'] = sound_state()
                    if fault == 'occupied_last_slot':
                        runtime.state['sound_effects']['histories'][109]['slots'][7][0] = 123
                    else:
                        runtime.state['sound_effects']['channels'][31] = 123
                with Client(runtime, trace=Mock()) as client:
                    client.hello()
                    with self.assertRaises(ValueError): apply_recipe(client, 42)
                self.assertEqual(runtime.draws, 0)


class AppAnchorPreparationTests(unittest.TestCase):
    def test_distinct_startup_counts_converge_by_actual_write_before_warm(self):
        from llm_vs_zombies import app_update_anchor as app
        source, cold = AppAnchorPreparationRuntime(1295), AppAnchorPreparationRuntime(1294)
        for runtime, target in ((source, None), (cold, 1295)):
            trace = Mock()
            with Client(runtime, trace=trace) as client:
                client.hello()
                recipe = apply_recipe(client, 42, app_update_count=target)
            runtime.recipe = recipe
            self.assertEqual(runtime.anchor_calls, 1)
            self.assertEqual(runtime.draws, 1)
            methods = [request['method'] for request in runtime.requests]
            self.assertLess(methods.index('clock_restore'), methods.index('app_update_anchor'))
            self.assertLess(methods.index('app_update_anchor'), methods.index('prepare_render'))
            self.assertEqual(app.target_from_recipe(recipe), 1295)
            evidence = next(c.args[1]['evidence'] for c in trace.emit.call_args_list
                            if c.args[0] == 'initialization_prepared')['app_update_anchor']
            self.assertEqual(evidence['before'], 1295 if target is None else 1294)
            self.assertEqual(evidence['before_state']['sound_effects']['app_update_count'], evidence['before'])
            self.assertEqual(evidence['after_state']['sound_effects']['app_update_count'], 1295)
        self.assertEqual(source.state, cold.state)
        self.assertEqual(source.recipe, cold.recipe)
        self.assertEqual(cold.state['sound_effects']['app_update_count'], 1295)

    def test_invalid_receipt_or_actual_readback_cannot_reach_warm(self):
        for fault in ('other_field', 'readback', 'flag'):
            with self.subTest(fault=fault):
                runtime = AppAnchorPreparationRuntime(1294, fault=fault)
                with Client(runtime, trace=Mock()) as client:
                    client.hello()
                    with self.assertRaises((ValueError, RuntimeError)):
                        apply_recipe(client, 42, app_update_count=1295)
                self.assertEqual(runtime.anchor_calls, 1)
                self.assertEqual(runtime.draws, 0)

    def test_explicit_target_on_legacy_runtime_and_invalid_values_fail_before_mutation(self):
        for value in (1295, True, -1, 0x80000000):
            with self.subTest(value=value):
                runtime = PreparationRuntime()
                with Client(runtime, trace=Mock()) as client:
                    client.hello()
                    with self.assertRaises((ValueError, RuntimeError)):
                        apply_recipe(client, 42, app_update_count=value)
                self.assertFalse(any(r['method'] in ('rng_seed', 'clock_restore', 'prepare_render') for r in runtime.requests))


class CounterPreparationTests(unittest.TestCase):
    def test_distinct_lifetime_totals_preserved_while_full_post_initial_state_converges(self):
        source, cold = CounterPreparationRuntime(), CounterPreparationRuntime(1427, 16)
        for runtime, target in ((source, None), (cold, 1362)):
            trace = Mock()
            with Client(runtime, trace=trace) as client:
                client.hello()
                runtime.recipe = apply_recipe(client, 42, app_update_count=target)
            origin = 14 if runtime is source else 16
            self.assertEqual(runtime.raw_calls, origin + 3)
            self.assertEqual(runtime.state['sound_effects']['calls'], 3)
            self.assertEqual(runtime.state['sound_effects']['counter_scope'], 'experiment')
            evidence = next(c.args[1]['evidence'] for c in trace.emit.call_args_list
                            if c.args[0] == 'initialization_prepared')
            self.assertEqual(evidence['sound_counter']['origin_raw_calls'], origin)
            self.assertEqual(evidence['sound_counter']['raw_before']['calls'], origin)
            self.assertEqual(evidence['sound_counter']['raw_after']['calls'], origin)
            self.assertEqual(evidence['sound_counter']['after_state'], evidence['app_update_anchor']['before_state'])
            methods = [r['method'] for r in runtime.requests]
            self.assertLess(methods.index('clock_restore'), methods.index('sound_counter_origin'))
            self.assertLess(methods.index('sound_counter_origin'), methods.index('app_update_anchor'))
            self.assertLess(methods.index('app_update_anchor'), methods.index('prepare_render'))
            self.assertEqual(methods.count('sound_counter_origin'), 1)
        self.assertEqual(source.state, cold.state)
        self.assertEqual(source.recipe, cold.recipe)

    def test_bad_origin_receipt_and_actual_readback_stop_before_app_anchor(self):
        for fault in ('other_field', 'raw_changed', 'readback', 'flag'):
            with self.subTest(fault=fault):
                runtime = CounterPreparationRuntime(fault=fault)
                with Client(runtime, trace=Mock()) as client:
                    client.hello()
                    with self.assertRaises((ValueError, RuntimeError)):
                        apply_recipe(client, 42)
                self.assertEqual(runtime.anchor_calls, 0)
                self.assertEqual(runtime.draws, 0)
                self.assertEqual(runtime.raw_calls, 14)

    def test_counter_capability_mismatch_rejected_before_mutation(self):
        from llm_vs_zombies import sound_counter as counter
        runtime = CounterPreparationRuntime()
        runtime.hello_value['capabilities'][counter.METHOD] = False
        with Client(runtime, trace=Mock()) as client:
            client.hello()
            with self.assertRaises(ValueError): apply_recipe(client, 42)
        self.assertFalse(any(r['method'] in ('rng_seed', 'sound_counter_origin') for r in runtime.requests))


if __name__ == '__main__': unittest.main()
