"""Public entry guards exercise real session cleanup with offline adapters."""
from contextlib import ExitStack, contextmanager
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from llm_vs_zombies import evaluation as ev
from llm_vs_zombies.records import write_json


class ProductionRuntimeGuardTests(unittest.TestCase):
    @contextmanager
    def session_adapters(self, role, *, trace_fails=False):
        # Keep live_session and _retain_session real. Only the game, native IPC,
        # observer, provenance and archive-writer adapters are replaced.
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            output = root / 'suite'; output.mkdir()
            name = 'suite-s42-c1' if role == 'cold' else 'suite-s42-recovery'
            run = root / 'experiments/runs' / name; run.mkdir(parents=True)
            (run / 'inputs').mkdir()
            (run / 'inputs/implementation.zip').write_bytes(b'offline adapter')
            write_json(run / 'manifest.json', {'status': 'recording'})
            write_json(run / 'launcher.json', {'isolation_ready': True})
            events = []

            class Monitor:
                def __init__(self, pid=None, **kwargs):
                    self.role = 'runtime' if pid else 'launch'
                def __enter__(self): events.append(self.role + '_enter'); return self
                def __exit__(self, *args): events.append(self.role + '_close')
                def bind_pid(self, pid): pass
                def result(self, pid):
                    return {'schema': 'lvz.launch-windows.v3', 'foreground_check_status': 'pass'}

            client = Mock()
            client.hello_result = {'game': {'test_fixture': {'mode': 'test_flag_drop_v1'}}}
            client.observe.return_value = {'version': {'epoch': 1, 'tick': 0, 'revision': 0}}
            def request(method, *args, **kwargs):
                self.assertEqual(method, 'stop_recording', 'fixture guard allowed a controlled request')
                events.append(method)
                return {'closed': True}
            client.request.side_effect = request
            client.close.side_effect = lambda: events.append('client_close')
            trace = Mock()
            def close_trace():
                events.append('trace_close')
                if trace_fails:
                    raise OSError('trace close failed after fixture rejection')
            trace.close.side_effect = close_trace

            def finalize(run, plan, outcome, *, package, primary_error):
                # Retention can only begin after all cleanup operations were
                # attempted. No fake archive is produced by this fixture.
                self.assertIn('process_stop', events)
                self.assertIn('test_fixture runtime', primary_error['message'])
                events.append('retention')
                return {'passed_recording': False, 'archive_sealed': False,
                        'cleanup_passed': not trace_fails, 'secondary_errors': []}, None

            stack.enter_context(patch('llm_vs_zombies.cli.create_run', return_value=run))
            stack.enter_context(patch('llm_vs_zombies.launcher.start', return_value={
                'pid': 123, 'scenario_verified': True, 'isolation_ready': True}))
            stop = stack.enter_context(patch('llm_vs_zombies.launcher.stop',
                side_effect=lambda path: events.append('process_stop')))
            stack.enter_context(patch('llm_vs_zombies.client.connect', return_value=client))
            stack.enter_context(patch('llm_vs_zombies.session.SessionTrace', return_value=trace))
            stack.enter_context(patch.object(ev, 'host_sources', return_value={
                'matches_archive': True, 'unchanged': True}))
            stack.enter_context(patch.object(ev, 'LaunchWindowMonitor', Monitor))
            stack.enter_context(patch.object(ev, 'finalize_run', side_effect=finalize))
            recipe = stack.enter_context(patch.object(ev, 'apply_recipe'))
            raw = stack.enter_context(patch('llm_vs_zombies.client.WindowsNamedPipeStream'))
            yield SimpleNamespace(root=root, output=output, run=run, name=name,
                client=client, trace=trace, events=events, recipe=recipe, raw=raw, stop=stop)
            recipe.assert_not_called()
            raw.assert_not_called()
            client.advance.assert_not_called()
            client.commit.assert_not_called()
            client.request.assert_called_once_with('stop_recording', expect=client.observe.return_value['version'])
            client.close.assert_called_once()
            trace.close.assert_called_once()
            stop.assert_called_once_with(run)
            self.assertLess(events.index('stop_recording'), events.index('runtime_close'))
            self.assertLess(events.index('runtime_close'), events.index('process_stop'))
            cleanup = json.loads((run / 'evaluation-cleanup.json').read_text())
            for key in ('recording_closed', 'client_closed', 'owned_process_stopped'):
                self.assertIs(cleanup[key], True)
            self.assertEqual(cleanup.get('trace_closed') is True, not trace_fails)
            if trace_fails:
                self.assertIn('trace close failed', cleanup['trace_close_error']['message'])
            primary = json.loads((run / 'evaluation-error.json').read_text())
            self.assertEqual(primary['type'], 'ValueError')
            self.assertIn('test_fixture runtime', primary['message'])

    def cold_attempt(self, *, trace_fails=False):
        with self.session_adapters('cold', trace_fails=trace_fails) as env:
            expected = SimpleNamespace(initial={'identity': 'production source'})
            def replay(source, initializer, destination):
                self.assertIs(source, expected)
                with initializer(source, destination):
                    self.fail('fixture cold session was yielded to the replay engine')
            with patch('llm_vs_zombies.engine_replay.replay', side_effect=replay):
                result = ev.run_cold_attempt(env.root, ev.Plan(min_free_bytes=0),
                    env.output, 42, 1, expected)
            self.assertFalse(result['attempt']['passed'])
            self.assertEqual(result['error']['type'], 'ValueError')
            self.assertIn('test_fixture runtime', result['error']['message'])
            self.assertEqual(len(result['sessions']), 1)
            retained = result['sessions'][0]
            self.assertEqual(retained['primary_error'], result['error'])
            self.assertFalse(retained['archive_sealed'])
            self.assertFalse(retained['infrastructure_passed'])
            self.assertLess(env.events.index('process_stop'), env.events.index('retention'))
            if trace_fails:
                self.assertEqual(retained['secondary_errors'][0]['stage'], 'runtime_cleanup')
                self.assertIn('trace_close_error', retained['secondary_errors'][0]['errors'])

    def test_common_cold_initializer_rejects_fixture_before_recipe_and_retains_cleanup(self):
        self.cold_attempt()

    def test_common_cold_keeps_fixture_error_when_trace_cleanup_also_fails(self):
        self.cold_attempt(trace_fails=True)

    def test_recovery_rejects_fixture_before_raw_pipe_and_still_closes(self):
        for trace_fails in (False, True):
            with self.subTest(trace_fails=trace_fails), \
                 self.session_adapters('recovery', trace_fails=trace_fails) as env:
                lifecycle = {}
                with self.assertRaisesRegex(ValueError, 'test_fixture runtime'):
                    ev._recovery_probe(env.root, env.name, ev.Plan(min_free_bytes=0),
                                       42, lifecycle=lifecycle)
                self.assertEqual(lifecycle['created_run'], str(env.run))
                self.assertTrue(lifecycle['cleanup']['owned_process_stopped'])


if __name__ == '__main__':
    unittest.main()
