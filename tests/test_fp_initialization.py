"""Offline frontend tests; FP instruction semantics are native fixtures."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from llm_vs_zombies import fp_environment as fp
from llm_vs_zombies import evaluation
from llm_vs_zombies.client import Client
from llm_vs_zombies.initialization import apply_recipe
from llm_vs_zombies.launcher import start
from test_initialization import PreparationRuntime


class FpRuntime(PreparationRuntime):
    def __init__(self, *, menu=False, start_cw=127, fail=None):
        super().__init__(menu=menu)
        self.fail = fail
        self.hello_value['capabilities'][fp.MODE] = True
        game = self.hello_value['game']
        game['fixed_fp'] = copy.deepcopy(fp.SPEC)
        game['engine_call_boundary'] = {'mode': 'controlled_engine_call_v1'}
        self.state['fp_environment'] = {'x87_control': 639, 'mxcsr_control': 8064}
        raw = {'x87_control': 639, 'x87_status': 0, 'mxcsr': 8064, 'mxcsr_control': 8064}
        self.activation = {'schema': 'lvz.fp-activation.v1', 'mode': fp.MODE,
            'request_id': 'initialize', 'version': {'epoch': 1, 'tick': 0, 'revision': 0},
            'phase': 'initialize_before_seed_and_enter_game', 'owner_thread_id': 123, 'actual_thread_id': 123,
            'game_ui': 1, 'board_address': 0, 'before': {**raw, 'x87_control': start_cw},
            'after': raw, 'write_attempted': True, 'activation_count': 1, 'ok': True, 'first_fault': None}

    def exchange(self, payload, timeout):
        request = json.loads(payload)
        response = json.loads(super().exchange(payload, timeout))
        result = response.get('result', {})
        if request['method'] == 'initialize':
            self.activation.update(request_id=request['request_id'], version=request['expect'])
            result['fixed_fp'] = copy.deepcopy(self.activation)
            if self.fail == 'ack':
                result.pop('fixed_fp')
        elif request['method'] == 'audit_snapshot':
            raw = copy.deepcopy(self.activation['after'])
            checks = dict.fromkeys(fp.PHASES, 0)
            checks.update(ready=1, snapshot=1, before_warm_draw=int(self.prepared), after_warm_draw=int(self.prepared))
            result['fixed_fp'] = {'activation': copy.deepcopy(self.activation), 'health': {
                'schema': 'lvz.fp-health.v1', 'mode': fp.MODE, 'activated': True, 'activation_count': 1,
                'owner_thread_id': 123, 'closed': False, 'healthy': True, 'wrong_thread_checks': 0,
                'raw_frames': 0, 'checks': checks, 'first_fault': None, 'last_raw': raw}}
            if self.fail == 'drift' or self.fail == 'warm_drift' and self.prepared:
                result['state']['fp_environment']['x87_control'] = 127
            elif self.fail == 'changed_origin':
                result['fixed_fp']['activation']['request_id'] = 'different-initialize'
        return json.dumps(response).encode()


class FpInitializationTests(unittest.TestCase):
    def test_different_raw_activation_inputs_produce_same_stable_recipe_and_preserve_evidence(self):
        recipes = []
        for start_cw in (127, 639):
            runtime = FpRuntime(start_cw=start_cw)
            client = Client(runtime, trace=Mock())
            with tempfile.TemporaryDirectory() as temporary:
                run = Path(temporary)
                (run / 'manifest.json').write_text(json.dumps({'status': 'recording', 'initial_state': {'captured': False}}))
                recipe = apply_recipe(client, 42, run=run, scenario_verified=True)
                evidence = json.loads((run / 'initialization-evidence.json').read_text())
                self.assertEqual(evidence['fixed_fp']['activation']['before']['x87_control'], start_cw)
                self.assertEqual(evidence['fixed_fp']['activation']['after']['x87_control'], 639)
                self.assertEqual(recipe['fixed_fp'], fp.SPEC)
                recipes.append(recipe)
            client.close()
        self.assertEqual(*recipes)

    def test_drift_before_recipe_rejects_before_any_mutation(self):
        runtime = FpRuntime(fail='drift'); client = Client(runtime, trace=Mock())
        with self.assertRaisesRegex(ValueError, 'captured control'):
            apply_recipe(client, 42)
        self.assertNotIn('rng_seed', [r['method'] for r in runtime.requests])
        client.close()

    def test_drift_after_warm_rejects_without_reseed_or_second_draw(self):
        runtime = FpRuntime(fail='warm_drift'); client = Client(runtime, trace=Mock())
        with self.assertRaisesRegex(ValueError, 'captured control'):
            apply_recipe(client, 42)
        methods = [r['method'] for r in runtime.requests]
        self.assertEqual(methods.count('rng_seed'), 1)
        self.assertEqual(methods.count('prepare_render'), 1)
        client.close()

    def test_launcher_binds_activation_and_cleans_up_ack_or_ready_failure(self):
        for failure in (None, 'ack', 'changed_origin'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); run = root / 'run'
                (run / 'observations').mkdir(parents=True); (run / 'sandbox').mkdir()
                runtime = FpRuntime(menu=True, fail=failure)
                client = Client(runtime, trace=Mock()); client.hello()
                state = dict(sandbox=str(run/'sandbox'), engine='engine', bootstrap='bootstrap', runtime='recorder')
                calls = []
                def native(state, verb, *args):
                    calls.append(verb)
                    return dict(pid=123, creation_time=456) if verb == 'launch' else {}
                with patch('llm_vs_zombies.launcher.prepare', return_value=state), \
                     patch('llm_vs_zombies.launcher._native', side_effect=native), \
                     patch('llm_vs_zombies.client.connect', return_value=client):
                    if failure:
                        with self.assertRaises(ValueError):
                            start(root, run, seed=42, defer_preparation=True)
                        self.assertEqual(calls, ['launch', 'inject', 'stop'])
                        self.assertTrue(state['process_cleaned_up'])
                    else:
                        actual = start(root, run, seed=42, defer_preparation=True)
                        self.assertEqual(actual['fixed_fp_activation'], actual['fixed_fp_ready']['activation'])
                        self.assertEqual(calls, ['launch', 'inject'])
                self.assertNotIn('rng_seed', [r['method'] for r in runtime.requests])
                self.assertTrue(runtime.closed)


class PauseProbeRuntime(FpRuntime):
    """Fixture whose snapshot monitor bookkeeping advances like the real runtime."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.snapshots = 0
        self.change_state = False
        self.drift_after_pause = False

    def exchange(self, payload, timeout):
        request = json.loads(payload)
        response = json.loads(super().exchange(payload, timeout))
        if request['method'] == 'audit_snapshot':
            self.snapshots += 1
            result = response['result']
            health = result['fixed_fp']['health']
            health['checks']['snapshot'] = self.snapshots
            health['checks']['loop'] = self.snapshots
            if self.snapshots >= 2 and self.change_state:
                result['state']['app'] = dict(result['state']['app'], mj_clock=31)
            if self.snapshots >= 2 and self.drift_after_pause:
                health['last_raw'] = dict(health['last_raw'], x87_control=127)
        return json.dumps(response).encode()


class PauseProbeFpTests(unittest.TestCase):
    def test_advancing_monitor_counters_keep_the_paused_state_probe_honest(self):
        runtime = PauseProbeRuntime(); client = Client(runtime, trace=Mock()); client.hello()
        probe = evaluation._pause_probe(client, 0.0)
        self.assertEqual(runtime.snapshots, 2)
        self.assertEqual(probe['fixed_fp'], fp.MODE)
        self.assertEqual(probe['compared_snapshot_fields'],
                         ['state', 'version', 'fixed_fp.activation', 'fixed_fp.health'])
        self.assertEqual(probe['state_sha256'],
                         hashlib.sha256(json.dumps({'state': runtime.state, 'version': runtime.version},
                                                   sort_keys=True).encode()).hexdigest())
        client.close()

    def test_paused_state_change_is_still_rejected(self):
        runtime = PauseProbeRuntime(); runtime.change_state = True
        client = Client(runtime, trace=Mock()); client.hello()
        with self.assertRaisesRegex(RuntimeError, 'paused wall time changed the captured simulation state'):
            evaluation._pause_probe(client, 0.0)
        client.close()

    def test_paused_fp_drift_is_still_rejected(self):
        runtime = PauseProbeRuntime(); runtime.drift_after_pause = True
        client = Client(runtime, trace=Mock()); client.hello()
        with self.assertRaisesRegex(ValueError, 'persistent FP control drift'):
            evaluation._pause_probe(client, 0.0)
        client.close()

    def test_legacy_runtime_probe_keeps_its_original_comparison(self):
        runtime = PreparationRuntime(); client = Client(runtime, trace=Mock()); client.hello()
        probe = evaluation._pause_probe(client, 0.0)
        self.assertEqual(probe['compared_snapshot_fields'], ['state', 'version'])
        self.assertEqual(probe['fixed_fp'], 'not_declared')
        client.close()


if __name__ == '__main__':
    unittest.main()
