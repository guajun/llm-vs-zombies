import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from llm_vs_zombies.evaluation import (LIVE_GATES, LaunchWindowMonitor, Plan, ScriptStrategy, apply_recipe,
                                      clock_anchor, evidence, full_cycle_completed,
                                      launch_window_evidence, private_launch_passed, readiness)
from llm_vs_zombies.session import SessionTrace


class EvaluationTests(unittest.TestCase):
    @staticmethod
    def window_sample(timestamp, handle=10, foreground_pid=100, *, resolved=True, owned=False, visible=False):
        return {"monotonic_seconds": timestamp, "foreground": handle, "foreground_pid": foreground_pid,
                "foreground_resolved": resolved,
                "owned_windows": [{"handle": 500, "visible": visible}] if owned else []}

    def test_unrelated_foreground_switch_does_not_fail_private_launch(self):
        windows = launch_window_evidence([self.window_sample(0),
                                         self.window_sample(.025, 20, 200, owned=True)], 42)
        self.assertFalse(windows["foreground_unchanged"])
        self.assertEqual(windows["foreground_change_cause"], "not_inferred")
        self.assertFalse(windows["game_foreground_observed"])
        self.assertEqual(windows["foreground_check_status"], "pass")
        self.assertIs(private_launch_passed({"isolation_ready": True, "evaluation_windows": windows}), True)

    def test_transient_game_foreground_fails_even_when_endpoints_match(self):
        windows = launch_window_evidence([self.window_sample(0), self.window_sample(.025, 500, 42),
                                         self.window_sample(.05, owned=True)], 42)
        self.assertTrue(windows["foreground_unchanged"])
        self.assertTrue(windows["hidden"])
        self.assertTrue(windows["game_foreground_observed"])
        self.assertEqual(windows["foreground_check_status"], "fail")
        self.assertIs(private_launch_passed({"isolation_ready": True, "evaluation_windows": windows}), False)

    def test_missing_foreground_ownership_or_excessive_gap_is_unverified(self):
        for samples in ([self.window_sample(0), self.window_sample(.025, resolved=False, owned=True)],
                        [self.window_sample(0), self.window_sample(.3, owned=True)],
                        [self.window_sample(0), self.window_sample(.025)]):
            windows = launch_window_evidence(samples, 42)
            self.assertEqual(windows["foreground_check_status"], "unverified")
            self.assertIsNone(private_launch_passed({"isolation_ready": True, "evaluation_windows": windows}))

    def test_visible_window_is_failure_and_legacy_endpoint_only_evidence_is_not_reclassified(self):
        windows = launch_window_evidence([self.window_sample(0), self.window_sample(.025, owned=True, visible=True)], 42)
        self.assertEqual(windows["foreground_check_status"], "fail")
        self.assertIsNone(private_launch_passed({"isolation_ready": True,
                                               "evaluation_windows": {"hidden": True, "foreground_unchanged": True}}))

    def test_sampling_error_cannot_pass_but_observed_game_ownership_still_fails(self):
        for foreground_pid, status in ((100, "unverified"), (42, "fail")):
            windows = launch_window_evidence([self.window_sample(0), self.window_sample(.025, foreground_pid=foreground_pid, owned=True)],
                                             42, errors=["sample failed"])
            self.assertEqual(windows["foreground_check_status"], status)

    def test_monitor_samples_during_blocking_launch_and_resolves_owned_windows_afterward(self):
        sampled = threading.Event()
        calls = []
        def probe(pid=None):
            calls.append(pid)
            if len(calls) >= 2:
                sampled.set()
            return {"foreground": 10, "foreground_pid": 100, "foreground_resolved": True,
                    "owned_windows": [{"handle": 500, "visible": False}] if pid == 42 else []}
        with patch("llm_vs_zombies.evaluation.window_probe", side_effect=probe):
            monitor = LaunchWindowMonitor()
            with monitor:
                self.assertIsNone(calls[0])
                # Stand in for a blocking launcher while the observer runs.
                self.assertTrue(sampled.wait(timeout=1))
            windows = monitor.result(42)
        self.assertFalse(monitor.thread.is_alive())
        self.assertEqual(calls[-1], 42)
        self.assertGreaterEqual(windows["sampling"]["sample_count"], 3)
        self.assertFalse(windows["game_foreground_observed"])

    def test_invalid_seed_and_incomplete_strict_plan_are_rejected(self):
        for seeds in ((True,), (-1,), (2**32,), (1, 1), ()):
            with self.assertRaises(ValueError):
                Plan(seeds=seeds).validate()
        with self.assertRaisesRegex(ValueError, "strict requires"):
            Plan(tier="strict").validate()
        with self.assertRaisesRegex(ValueError, "strict requires"):
            Plan(tier="strict", cold_starts=9, strategy="policy.py").validate()
        Plan(tier="strict", cold_starts=10, strategy="policy.py").validate()

    def test_plan_strategy_resolves_relative_to_plan_not_process_cwd(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            path = directory / "plan.json"
            path.write_text(json.dumps({"seeds": [42], "strategy": "policies/policy.py"}))
            self.assertEqual(Plan.load(path).strategy, str((directory / "policies/policy.py").resolve()))

    def test_mock_evidence_cannot_make_strict_ready(self):
        with tempfile.TemporaryDirectory() as temp:
            artifact = Path(temp) / "mock-result.json"
            artifact.write_text('{"passed":true}')
            checks = {gate: evidence("pass", source="mock", detail="simulated transport", artifacts=[artifact])
                      for gate in ("build_and_tests", *LIVE_GATES)}
            result = readiness(Plan(tier="strict", cold_starts=10, strategy="p.py"), checks)
            self.assertFalse(result["experiment_ready"])
            self.assertIn("engine_replay", result["unmet_gates"])
            self.assertFalse(result["strict_engine_determinism_proven"])

    def test_smoke_never_claims_strict_ready_and_mutated_artifacts_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            artifact = Path(temp) / "evidence.json"
            artifact.write_text("test of gate mechanics only")
            checks = {gate: evidence("pass", source="live_engine", detail="unit fixture", artifacts=[artifact]) for gate in LIVE_GATES}
            checks["build_and_tests"] = evidence("pass", source="local_build", detail="unit fixture", artifacts=[artifact])
            self.assertFalse(readiness(Plan(), checks)["experiment_ready"])
            # This test checks invalidation, not a live readiness certificate.
            artifact.write_text("changed")
            strict = Plan(tier="strict", cold_starts=10, strategy="p.py")
            self.assertFalse(readiness(strict, checks)["experiment_ready"])
            self.assertIn("initial_state", readiness(strict, checks)["unmet_gates"])

    def test_missing_artifacts_are_unverified_even_with_pass_label(self):
        plan = Plan(tier="strict", cold_starts=10, strategy="p.py")
        checks = {gate: evidence("pass", source="live_engine", detail="unsupported claim") for gate in LIVE_GATES}
        self.assertFalse(readiness(plan, checks)["experiment_ready"])

    def test_twentieth_wave_is_not_a_completed_two_flag_cycle(self):
        initial = {"completed_rounds": 4, "scene": 3}
        self.assertFalse(full_cycle_completed(initial, {"completed_rounds": 4, "scene": 3}, 20))
        self.assertFalse(full_cycle_completed(initial, {"completed_rounds": 5, "scene": 3}, 19))
        self.assertFalse(full_cycle_completed(initial, {"completed_rounds": 5, "scene": 2}, 20))
        self.assertFalse(full_cycle_completed(initial, {"scene": 3}, 20))
        self.assertTrue(full_cycle_completed(initial, {"completed_rounds": 5, "scene": 3}, 20))

    def test_seed_capability_failure_never_sends_a_mutation(self):
        class Unsupported:
            def hello(self):
                return {"capabilities": {"rng_seed": False}}
            def request(self, *args, **kwargs):
                raise AssertionError("unsupported seed must not be sent")
        with self.assertRaisesRegex(RuntimeError, "planned seed is not evidence"):
            apply_recipe(Unsupported(), 42)

    def test_clock_anchor_uses_captured_raw_clocks(self):
        result = clock_anchor({"state": {"schema": "lvz.audit.v1", "rng": {"target": "test"},
                    "board": {"00005568": 10, "0000556c": 20}, "app": {"mj_clock": 30}}})
        self.assertEqual((result["game_clock"], result["effect_clock"], result["mj_clock"]), (10, 20, 30))

    def test_strategy_source_model_exchange_and_decision_are_audited(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            strategy = directory / "strategy.py"
            strategy.write_text("def decide(observation, context):\n"
                                "    record_exchange('local-test', {'tick': observation['tick']}, {'wait': 2})\n"
                                "    return {'actions': [], 'advance_ticks': 2}\n")
            with SessionTrace(directory / "trace.jsonl") as trace:
                policy = ScriptStrategy(object(), trace, strategy, 100)
                self.assertEqual(policy.decide({"tick": 0}, {"remaining_ticks": 2})["advance_ticks"], 2)
                with self.assertRaisesRegex(ValueError, "remaining experiment budget"):
                    policy.decide({"tick": 0}, {"remaining_ticks": 1})
            events = [json.loads(line) for line in (directory / "trace.jsonl").read_text().splitlines()]
            kinds = {e["kind"] for e in events}
            self.assertTrue({"strategy_source", "cell", "cell_complete", "model_exchange", "strategy_decision"} <= kinds)
            self.assertEqual(next(e for e in events if e["kind"] == "model_exchange")["data"]["provider"], "local-test")


if __name__ == "__main__":
    unittest.main()
