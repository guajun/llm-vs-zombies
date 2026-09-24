"""Offline half of probe A (issue #34): criteria, fixtures and the A1 assessor.

The live half needs a paused original-game process. Everything that can be
decided from a record or from a closed run directory is decided here, so the
real-machine step list only has to collect evidence.
"""
import json
import tempfile
import unittest
from contextlib import redirect_stdout
import io
from pathlib import Path
from unittest.mock import MagicMock, patch

from llm_vs_zombies import evaluation
from llm_vs_zombies import evidence_codec
from llm_vs_zombies import suspend_probes as sp


class FakeHost(sp.HostPerturbations):
    """Deterministic desktop stand-in; the wall clock is virtual on purpose."""

    def __init__(self, *, simulated=False, fail=None):
        self.simulated = simulated
        self.fail = fail
        self.calls = []
        self.clock = 100.0

    def now(self):
        return self.clock

    def sleep(self, seconds):
        self.calls.append(("sleep", seconds))
        self.clock += seconds

    def screen(self):
        return {"width": 1920, "height": 1080}

    def cursor_position(self):
        self.calls.append(("cursor_position",))
        return {"x": 10, "y": 20}

    def move_cursor(self, x, y):
        self.calls.append(("move_cursor", x, y))

    def foreground(self):
        self.calls.append(("foreground",))
        return {"hwnd": 1, "pid": 2, "title": "PvZ"}

    def steal_foreground(self):
        if self.fail == "focus":
            raise sp.PerturbationError("SetForegroundWindow failed with winerror 5")
        self.calls.append(("steal_foreground",))
        return {"target": "shell", "hwnd": 99, "pid": 4, "title": "Program Manager"}

    def restore_foreground(self, hwnd):
        self.calls.append(("restore_foreground", hwnd))
        return hwnd == 1


class FakeClient:
    """Minimal paused runtime; `mutate` makes the captured state drift."""

    def __init__(self, *, state="paused_at_boundary", mutate=False, version=None):
        self.hello_result = {"game": {}}
        self.state = state
        self.mutate = mutate
        self.version = version or {"epoch": 3, "tick": 10, "revision": 0}
        self.observation = {"game_ui": 3, "version": self.version}
        self.requests = []
        self.snapshots = 0
        self.closed = False

    def close(self):
        self.closed = True

    def request(self, method, params=None, expect=None):
        self.requests.append(method)
        if method == "status":
            return {"state": self.state}
        if method == "pause":
            self.state = "paused_at_boundary"
            return {"observation": self.observation, "state": "paused_at_boundary"}
        if method == "audit_snapshot":
            self.snapshots += 1
            state = {"schema": "lvz.audit.v1", "app": {"mj_clock": 30}}
            if self.mutate and self.snapshots >= 2:
                state = {"schema": "lvz.audit.v1", "app": {"mj_clock": 31}}
            return {"version": self.version, "state": state,
                    "engine_call": {"entered_calls": 5, "returned_calls": 5, "faults": 0}}
        if method == "observe":
            return self.observation
        raise AssertionError(f"unexpected request {method}")

    def observe(self):
        return self.request("observe")


def frames(*pairs):
    """Boundary records as `boundary_records` normalizes them; pairs are (seq, call_id, tick)."""
    records = []
    for seq, call, tick in pairs:
        records.append({"seq": seq, "kind": "pre_step", "version": {"epoch": 3, "tick": tick - 1, "revision": 6},
                        "engine_call_id": call})
        records.append({"seq": seq + 1, "kind": "post_step", "version": {"epoch": 3, "tick": tick, "revision": 0},
                        "engine_call_id": call})
    return records


def stream(*pairs):
    """The same boundaries in the shape the native checksums stream actually stores."""
    return [{"seq": item["seq"], "kind": item["kind"], "version": item["version"],
             "payload": {"engine_call": {"engine_call_id": item["engine_call_id"]}}} for item in frames(*pairs)]


class PerturbationTests(unittest.TestCase):
    def test_wall_probe_measures_the_pause_and_passes(self):
        host = FakeHost()
        record = sp.probe(FakeClient(), seconds=300.0, perturbations=("wall",), host=host)
        self.assertEqual(record["schema"], sp.SCHEMA)
        self.assertEqual(record["verdict"], "pass")
        self.assertEqual(record["requested_perturbations"], ["wall"])
        self.assertAlmostEqual(record["wall_seconds"], 300.0)
        self.assertEqual(record["perturbations"][0]["kind"], "wall")
        self.assertTrue(all(value is True for value in record["checks"].values()))
        self.assertEqual(len(record["state_sha256"]), 64)

    def test_focus_and_cursor_are_applied_in_order_then_restored(self):
        host = FakeHost()
        record = sp.probe(FakeClient(), seconds=1.0, perturbations="wall,focus,cursor", host=host)
        self.assertEqual(record["verdict"], "pass")
        self.assertEqual([item["kind"] for item in record["perturbations"]], ["wall", "focus", "cursor"])
        focus = record["perturbations"][1]
        self.assertEqual(focus["target"]["target"], "shell")
        self.assertTrue(focus["restored"])
        cursor = record["perturbations"][2]
        self.assertEqual(cursor["before"], {"x": 10, "y": 20})
        self.assertEqual(cursor["target"], {"x": 1918, "y": 1078})
        self.assertTrue(cursor["restored"])
        self.assertEqual([call[0] for call in host.calls],
                         ["sleep", "foreground", "steal_foreground", "foreground", "restore_foreground",
                          "cursor_position", "move_cursor", "cursor_position", "move_cursor", "cursor_position"])

    def test_simulated_host_records_intent_without_touching_the_desktop(self):
        host = FakeHost(simulated=True)
        record = sp.probe(FakeClient(), seconds=0.5, perturbations=("focus", "cursor"), host=host)
        self.assertEqual(record["verdict"], "pass")
        self.assertTrue(record["simulated"])
        self.assertTrue(all(item["simulated"] for item in record["perturbations"] if item["kind"] != "wall"))
        self.assertEqual([call[0] for call in host.calls], ["sleep", "foreground", "cursor_position"])

    def test_state_change_during_the_pause_fails_the_probe(self):
        record = sp.probe(FakeClient(mutate=True), seconds=1.0, perturbations=("wall",), host=FakeHost())
        self.assertEqual(record["verdict"], "fail")
        self.assertIn("state_unchanged is not true", record["judgement"]["failures"])

    def test_perturbation_that_cannot_be_applied_is_unverified_not_pass(self):
        record = sp.probe(FakeClient(), seconds=1.0, perturbations=("focus",), host=FakeHost(fail="focus"))
        self.assertEqual(record["verdict"], "unverified")
        self.assertEqual(record["perturbations"][1]["applied"], False)
        self.assertIn("winerror 5", " ".join(record["judgement"]["missing"]))

    def test_pause_is_requested_when_the_target_is_still_running(self):
        client = FakeClient(state="running")
        record = sp.probe(client, seconds=0.5, perturbations=("wall",), host=FakeHost())
        self.assertEqual(record["pause"]["state"], "paused_at_boundary")
        self.assertTrue(record["pause"]["observation_unchanged"])
        self.assertIn("pause", client.requests)
        self.assertEqual(record["verdict"], "pass")

    def test_probe_refuses_to_pause_when_told_not_to(self):
        with self.assertRaisesRegex(RuntimeError, "already completed, paused boundary"):
            sp.probe(FakeClient(state="running"), seconds=0.5, perturbations=("wall",),
                     host=FakeHost(), pause=False)

    def test_unknown_perturbation_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown perturbation 'teleport'"):
            sp.apply_perturbations(FakeHost(), ("teleport",), 1.0)
        with self.assertRaisesRegex(ValueError, "unknown pause perturbation"):
            evaluation.Plan(pause_perturbations=("teleport",)).validate()
        evaluation.Plan(pause_perturbations=("wall", "focus", "cursor")).validate()


class BoundaryTests(unittest.TestCase):
    def test_clean_sequence_passes_and_gaps_are_reported(self):
        clean = sp.boundary_checks(frames((1, 1, 1), (3, 2, 2)), [])
        self.assertTrue(all(value is True for value in clean["checks"].values()))
        self.assertEqual(clean["boundary_count"], 2)
        gap = sp.boundary_checks(frames((1, 1, 1), (3, 3, 2)), [])
        self.assertFalse(gap["checks"]["engine_call_ids_contiguous"])
        repeated = sp.boundary_checks(frames((1, 1, 1), (3, 2, 2)) * 2, [])
        self.assertFalse(repeated["checks"]["boundary_sequence_strictly_increasing"])

    def test_paused_boundary_must_be_recorded_exactly_once(self):
        records = frames((1, 1, 1), (3, 2, 2))
        recorded = sp.boundary_checks(records, [{"requested_tick": 2, "version": {"epoch": 3, "tick": 2, "revision": 0}}])
        self.assertTrue(recorded["checks"]["pause_0_paused_boundary_recorded_once"])
        missing = sp.boundary_checks(records, [{"requested_tick": 9, "version": {"epoch": 3, "tick": 9, "revision": 0}}])
        self.assertFalse(missing["checks"]["pause_0_paused_boundary_recorded_once"])

    def test_pre_play_probe_has_no_boundary_and_says_so(self):
        result = sp.boundary_checks(frames((1, 1, 1)), [{"version": {"epoch": 3, "tick": 0, "revision": 6}}])
        self.assertNotIn("pause_0_paused_boundary_recorded_once", result["checks"])
        self.assertEqual(result["details"]["pause_0_boundary"], "not_applicable_before_first_step")


class AssessmentTests(unittest.TestCase):
    def session(self, root: Path, *, coverage="pass", wall=1.0, mutate_replay=False, gzip_audit=False,
                extra_point=None):
        run = root / "experiments/runs/run-1"
        run.mkdir(parents=True)
        version = {"epoch": 3, "tick": 2, "revision": 0}
        schedule = {"configured": [[2, 1.0]], "completed": [{"requested_tick": 2, "wall_seconds": wall,
                                                            "version": version, "state_sha256": "a" * 64}],
                    "unexecuted_reached": [], "not_reached": [], "final_version": version,
                    "coverage_status": coverage}
        if extra_point is not None:
            schedule["configured"].append([9, 0.05])
            schedule["not_reached"].append(dict(extra_point, wall_seconds=0.05))
        (run / "pause-probes-during-play.json").write_text(json.dumps(schedule))
        audit = run / "audit"
        audit.mkdir()
        (audit / "manifest.json").write_text(json.dumps({"schema": "lvz.audit.v1", "target": "pvz-1.0.0.1051-en"}))
        lines = "\n".join(json.dumps(item) for item in stream((1, 1, 1), (3, 2, 2))) + "\n"
        (audit / "checksums.jsonl").write_text(lines)
        if gzip_audit:
            evidence_codec.compress_evidence(audit)
        report = run / "replay-report.json"
        report.write_text(json.dumps({"schema": "lvz.engine-replay.v1", "equal": not mutate_replay,
                                      "requests": [{"ordinal": 0}], "failure": {"reason": "state"} if mutate_replay else None,
                                      "pause_controls": {"recorded": 1, "executed": 1, "verified_noop": 1}}))
        return run, report

    def test_clean_session_passes_and_is_bound_to_the_native_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            run, report = self.session(Path(temp))
            result = sp.assess(run, replay_report=report)
            self.assertEqual(result["verdict"], "pass", result["reasons"])
            self.assertEqual(result["audit"]["boundary_count"], 2)
            self.assertEqual(result["pause_completed"][0]["requested_tick"], 2)
            self.assertTrue(result["checks"]["replay.replay_equal"])

    def test_gzip_audit_stream_is_read_the_same_way(self):
        with tempfile.TemporaryDirectory() as temp:
            run, report = self.session(Path(temp), gzip_audit=True)
            self.assertEqual(sp.assess(run, replay_report=report)["verdict"], "pass")

    def test_short_pause_and_bad_replay_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            run, report = self.session(Path(temp), wall=0.0, mutate_replay=True)
            result = sp.assess(run, replay_report=report)
            self.assertEqual(result["verdict"], "fail")
            self.assertTrue(any("pause_lasted_long_enough" in item for item in result["failures"]))
            self.assertTrue(any("replay is not equal" in item for item in result["failures"]))

    def test_missing_audit_is_unverified_not_pass_or_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            run, report = self.session(Path(temp))
            for name in ("checksums.jsonl", "manifest.json"):
                (run / "audit" / name).unlink()
            result = sp.assess(run, replay_report=report)
            self.assertEqual(result["verdict"], "unverified")
            self.assertTrue(any("native audit could not be scanned" in item for item in result["unverified"]))

    def test_missing_schedule_is_unverified(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "experiments/runs/empty"
            run.mkdir(parents=True)
            result = sp.assess(run)
            self.assertEqual(result["verdict"], "unverified")

    def test_pause_beyond_the_actual_endpoint_keeps_the_runner_semantics(self):
        with tempfile.TemporaryDirectory() as temp:
            run, report = self.session(Path(temp), extra_point={"requested_tick": 9,
                "status": "not_applicable_beyond_actual_endpoint"})
            result = sp.assess(run, replay_report=report)
            self.assertEqual(result["verdict"], "pass", result["reasons"])
            self.assertTrue(result["checks"]["pause_at_9.beyond_actual_endpoint"])
            self.assertNotIn("pause_at_9.completed_probe_recorded", result["checks"])

    def test_cold_leg_without_pause_requests_is_named_not_failed(self):
        with tempfile.TemporaryDirectory() as temp:
            run, report = self.session(Path(temp))
            report.write_text(json.dumps({"schema": "lvz.engine-replay.v1", "equal": True,
                                          "requests": [{"ordinal": 0}],
                                          "pause_controls": {"recorded": 0, "executed": 0, "verified_noop": 0}}))
            result = sp.assess(run, replay_report=report)
            self.assertEqual(result["verdict"], "pass", result["reasons"])
            self.assertEqual(result["replay"]["cold_leg_pauses"], 0)
            self.assertIn("no pause request", result["replay"]["cold_leg_note"])


class EvaluationProbeTests(unittest.TestCase):
    def test_undeclared_run_keeps_the_legacy_probe_record(self):
        probe = evaluation._pause_probe(FakeClient(), 0.0)
        self.assertEqual(set(probe), {"wall_seconds", "version", "state_sha256",
                                      "compared_snapshot_fields", "fixed_fp"})

    def test_declared_perturbations_extend_the_same_probe(self):
        host = FakeHost()
        probe = evaluation._pause_probe(FakeClient(), 1.0, perturbations=("wall", "focus"), host=host)
        self.assertEqual(probe["wall_seconds"], 1.0)
        self.assertEqual(probe["schema"], sp.SCHEMA)
        self.assertEqual(probe["verdict"], "pass")
        self.assertEqual([item["kind"] for item in probe["perturbations"]], ["wall", "focus"])
        self.assertTrue(probe["checks"]["state_unchanged"])
        self.assertEqual(probe["judgement"]["verdict"], "pass")


class CommandTests(unittest.TestCase):
    def test_probe_command_binds_the_branch_and_writes_the_record(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "probe.json"
            client = FakeClient(state="running")
            with patch("llm_vs_zombies.session.SessionTrace") as trace_class, \
                 patch("llm_vs_zombies.client.connect", return_value=client) as connect:
                trace_class.return_value.__enter__.return_value = MagicMock()
                with redirect_stdout(io.StringIO()) as printed:
                    code = sp.main(["probe", "--pid", "123", "--seconds", "0.01", "--perturb", "wall",
                                    "--simulate", "--branch-id", "run-1", "--trace",
                                    str(Path(temp) / "trace.jsonl"), "--output", str(output)])
            self.assertEqual(code, 0)
            self.assertEqual(connect.call_args.kwargs["expected_branch"], "run-1")
            record = json.loads(output.read_text())
            self.assertEqual(record["verdict"], "pass")
            self.assertEqual(record["pause"]["state"], "paused_at_boundary")
            self.assertIn('"verdict": "pass"', printed.getvalue())
            self.assertTrue(client.closed)


if __name__ == "__main__":
    unittest.main()
