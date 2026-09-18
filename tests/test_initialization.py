"""Protocol fixtures for initialization ordering, not native determinism evidence."""
import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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


if __name__ == '__main__': unittest.main()
