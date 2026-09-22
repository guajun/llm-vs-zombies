"""Same-root branch self-check on a synthetic fixture (N8 / issue #36).

The fixture is a sealed two-branch tree built from the synthetic counter engine
in ``test_engine_replay``; no game process is involved. The executors below
re-execute a sampled node's recorded action sequence through a fresh engine
instance, exactly the contract the real a2 executor has to fulfil.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

from llm_vs_zombies.audit_compare import AuditLog, canonical
from llm_vs_zombies.client import Client
from llm_vs_zombies.engine_replay import (ReplaySession, build_trajectory, capture_initial,
                                          identity_from_launcher, replay)
from llm_vs_zombies.evidence_tree import (branch_placement, end_boundary, package_tree, root_identity,
                                          root_placement)
from llm_vs_zombies.session import SessionTrace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import branch_selfcheck

from test_engine_replay import ARTIFACTS, ModelTransport


def _execute(sample, directory, **options):
    """Re-run one sampled node through a fresh synthetic engine and collect frames."""
    with SessionTrace(directory / "session.jsonl") as trace, \
            Client(ModelTransport(directory / "audit", epoch=7, **options), trace=trace) as client:
        identity = identity_from_launcher(client.hello(), ARTIFACTS)
        marker = capture_initial(client, identity=identity, initialization=sample.root_identity["init_recipe"])
        for step in sample.steps:
            request, params = step["request"], step["request"]["params"]
            if request["method"] == "commit":
                client.commit(params["actions"], advance_ticks=params["advance_ticks"])
            elif request["method"] == "advance":
                client.advance(params["max_ticks"])
            else:
                raise AssertionError(f"fixture executor cannot replay {request['method']}")
        client.request("stop_recording", expect=client.version)
    frames = [{"tick": 0, "kind": "boundary", "state": marker["state"]}]
    for frame in AuditLog(directory / "audit", require_closed=True).frames:
        if frame.kind == "post_step":
            frames.append({"tick": frame.version["tick"], "kind": frame.kind, "state": frame.state})
    image = hashlib.sha256(canonical(marker["state"])).hexdigest()
    return {"mode": "synthetic_fixture_replay", "real_game": False, "identity": identity,
            "restore": {"method": "synthetic_fixture_root_state", "attempted": True,
                        "equal": image == sample.root_identity["image_sha256"], "image_sha256": image},
            "frames": frames}


def fixture_executor(sample, directory):
    """Deterministic fixture executor: both runs must reproduce the sealed frames."""
    return _execute(sample, directory)


def divergent_executor(sample, directory):
    """Only the second run diverges, one tick past the restore boundary (R2)."""
    return _execute(sample, directory, **({"divergence": 1} if directory.name == "run-b" else {}))


def offset_executor(sample, directory):
    """Both runs agree with each other but not with the sealed recording (R3)."""
    return _execute(sample, directory, divergence=2)


def broken_restore_executor(sample, directory):
    """The restore step reports that it was not the identity transform (R0)."""
    record = _execute(sample, directory)
    record["restore"].update(equal=False, note="fixture restore mismatch")
    return record


class BranchSelfcheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.source = cls.root / "source"
        cls.source.mkdir()
        with SessionTrace(cls.source / "session.jsonl") as trace, \
                Client(ModelTransport(cls.source / "audit"), trace=trace) as client:
            cls.identity = identity_from_launcher(client.hello(), ARTIFACTS)
            marker = capture_initial(client, identity=cls.identity, initialization={"synthetic": True})
            client.commit([{"op": "plant", "row": 1, "col": 1}], advance_ticks=2)
            client.advance(1)
            client.request("stop_recording", expect=client.version)
        cls.root_state_sha256 = hashlib.sha256(canonical(marker["state"])).hexdigest()
        cls.source_trajectory = build_trajectory(
            cls.source / "session.jsonl", cls.source / "audit", cls.root / "trunk",
            tree=root_placement("main", root_identity(game=cls.identity["game"], artifacts=cls.identity["artifacts"],
                                                      init_recipe={"synthetic": True},
                                                      image_sha256=cls.root_state_sha256)))
        end = end_boundary(cls.source_trajectory)
        continuation = cls._fork("main", lambda session, _: session.client.advance(1),
                                 trunk=True, target_tick=end["tick"])
        left = cls._fork("A", lambda session, _: session.client.commit([{"op": "plant", "row": 6, "col": 8}],
                                                                      advance_ticks=2), target_tick=end["tick"])
        right = cls._fork("B", lambda session, _: session.client.advance(3), target_tick=end["tick"])
        cls.tree = package_tree(cls.root / "tree", cls.source_trajectory, branches=[continuation, left, right])
        cls.summary = cls.tree.validate()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @classmethod
    def _fork(cls, name, action, *, target_tick, trunk=False):
        @contextmanager
        def initialize(trajectory, output):
            with SessionTrace(output / "actual.jsonl") as trace, \
                    Client(ModelTransport(output / "audit", epoch=99, revision=2), trace=trace) as client:
                yield ReplaySession(client, copy.deepcopy(cls.identity), output / "audit")
                client.request("stop_recording", expect=client.version)

        run = cls.root / f"run-{name}"
        replay(cls.source_trajectory, initialize, run, target_tick=target_tick, on_takeover=action)
        return build_trajectory(run / "actual.jsonl", run / "audit", cls.root / f"node-{name}",
                                tree=branch_placement(cls.source_trajectory, name, trunk=trunk))

    def run_cli(self, output, *arguments):
        """Run the real command line entry point in-process; argparse exits on rejection."""
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = branch_selfcheck.main([str(self.tree.directory), str(output), *arguments])
            except SystemExit as exit:
                code = exit.code
        return code, stderr.getvalue() or stdout.getvalue()

    def check(self, name, *arguments, expect=0):
        output = self.root / name
        code, message = self.run_cli(output, *arguments)
        self.assertEqual(code, expect, message)
        return json.loads((output / branch_selfcheck.REPORT_FILE).read_text(encoding="utf-8")), output

    def test_cli_flow_runs_on_the_fixture_and_binds_tree_and_branch_identity(self):
        report, output = self.check("full", "--executor", "test_branch_selfcheck:fixture_executor",
                                   "--sample-limit", "all")
        self.assertEqual(report["schema"], branch_selfcheck.SCHEMA)
        self.assertEqual(report["verdict"]["status"], "passed")
        self.assertEqual(report["tree"]["tree_id"], self.summary["tree_id"])
        self.assertEqual(report["root_identity"]["image_sha256"], self.root_state_sha256)
        self.assertEqual(report["sampling"]["nodes_total"], 4)
        self.assertEqual(report["sampling"]["nodes_sampled"], 4)
        self.assertEqual(report["sampling"]["node_fraction"], 1.0)
        self.assertEqual(report["sampling"]["branches_sampled"], 3)
        self.assertEqual(report["sampling"]["branch_fraction"], 1.0)
        self.assertEqual(report["sampling"]["edge_ticks_sampled"], report["sampling"]["edge_ticks_total"])
        self.assertEqual(report["sampling"]["edge_ticks_total"], 9)
        self.assertEqual([sample["node"] for sample in report["samples"]], ["root", "main", "A", "B"])
        for sample in report["samples"]:
            manifest = json.loads((self.tree.directory / sample["binding"]["path"] / "trajectory.json")
                                  .read_text(encoding="utf-8"))
            placement = manifest["tree"]
            self.assertEqual(sample["binding"]["tree_id"], self.summary["tree_id"])
            self.assertEqual(sample["binding"]["branch_id"], placement["branch_id"])
            self.assertEqual(sample["binding"]["chain_sha256"], placement["chain"]["sha256"])
            self.assertEqual(sample["binding"]["trajectory_id"], manifest["trajectory_id"])
            if placement["parent"] is not None:
                self.assertEqual(sample["binding"]["parent_trajectory_id"], placement["parent"]["trajectory_id"])
            self.assertEqual([sample["rungs"][rung]["status"] for rung in branch_selfcheck.RUNG_ORDER],
                             ["passed"] * 4)
            self.assertEqual(sample["rungs"]["r0"]["runs"][0]["image_binding"], "matched")
            self.assertEqual([run["root_binding"] for run in sample["runs"]], ["matched", "matched"])
            self.assertIsNone(sample["first_divergence"])
            for run in sample["runs"]:
                record = output / run["record"]
                self.assertTrue(record.is_file())
                self.assertEqual(hashlib.sha256(record.read_bytes()).hexdigest(), run["sha256"])
                self.assertFalse(run["real_game"])
        forks = [sample for sample in report["samples"] if sample["fork"]["status"] != "not_applicable"]
        self.assertTrue(forks)
        for sample in forks:
            self.assertEqual(sample["fork"]["status"], "passed")
            self.assertEqual(sample["fork"]["reference"]["node"], "root")
        self.assertIsNone(report["first_divergence"])
        self.assertEqual(report["report_id"], branch_selfcheck._report_id(report))
        self.assertFalse(report["limitations"]["real_game_run"])
        self.assertFalse(report["limitations"]["original_engine_verified"])
        self.assertIn("no real game process", report["limitations"]["statement"])
        self.assertTrue(report["limitations"]["prerequisites"])

    def test_sampling_policy_keeps_branch_origins_and_reports_the_skipped_node(self):
        nodes = branch_selfcheck.node_records(self.tree)
        selected, coverage = branch_selfcheck.plan_samples(self.tree, nodes, limit=3)
        self.assertEqual(selected, ["root", "A", "B"])
        self.assertEqual(coverage["skipped"], ["main"])
        self.assertEqual(coverage["branches_sampled"], 3)
        self.assertEqual((coverage["edge_ticks_sampled"], coverage["edge_ticks_total"]), (8, 9))
        self.assertEqual(coverage["edge_tick_fraction"], round(8 / 9, 6))
        self.assertEqual(branch_selfcheck.plan_samples(self.tree, nodes, limit="all")[0],
                         ["root", "main", "A", "B"])
        self.assertEqual(branch_selfcheck.plan_samples(self.tree, nodes, limit=4)[0],
                         ["root", "main", "A", "B"])
        with self.assertRaisesRegex(Exception, "below the mandatory"):
            branch_selfcheck.plan_samples(self.tree, nodes, limit=2)
        with self.assertRaisesRegex(Exception, "unknown sampled node"):
            branch_selfcheck.plan_samples(self.tree, nodes, requested=["ghost"])

    def test_cross_run_divergence_is_located_and_later_rungs_are_not_claimed(self):
        report, output = self.check("divergence", "--executor", "test_branch_selfcheck:divergent_executor",
                                   "--sample-nodes", "root,A", expect=1)
        self.assertEqual(report["verdict"]["status"], "failed")
        self.assertEqual(report["verdict"]["failed_nodes"], ["A", "root"])
        self.assertEqual(report["rungs"]["r1"]["passed"], 2)
        self.assertEqual(report["rungs"]["r2"]["failed"], 2)
        self.assertEqual(report["rungs"]["r3"]["not_reached"], 2)
        sample = report["samples"][0]
        self.assertEqual([sample["rungs"][rung]["status"] for rung in branch_selfcheck.RUNG_ORDER],
                         ["passed", "passed", "failed", "not_reached"])
        self.assertEqual(sample["rungs"]["r3"]["blocked_by"], "r2")
        self.assertEqual(sample["rungs"]["r2"]["first_difference"]["difference"],
                         {"path": "/board/hidden", "reason": "value", "expected": 0, "actual": 999})
        self.assertEqual(sample["first_divergence"]["rung"], "r2")
        self.assertEqual(report["first_divergence"]["node"], "root")
        self.assertEqual(report["first_divergence"]["comparison"]["right"], "run-b")
        for run in sample["runs"]:
            record = json.loads((output / run["record"]).read_text(encoding="utf-8"))
            self.assertEqual(record["tick_range"], [0, 3])

    def test_sealed_reference_catches_runs_that_agree_with_each_other_only(self):
        sealed, _ = self.check("offset-sealed", "--executor", "test_branch_selfcheck:offset_executor",
                               "--sample-nodes", "root,A", expect=1)
        self.assertEqual(sealed["verdict"]["status"], "failed")
        sample = sealed["samples"][1]
        self.assertEqual(sample["rungs"]["r2"]["status"], "passed")
        self.assertEqual(sample["rungs"]["r3"]["status"], "failed")
        failure = next(item for item in sample["rungs"]["r3"]["comparisons"] if item["equal"] is False)
        self.assertEqual((failure["right"], failure["tick"], failure["difference"]["path"]),
                         ("sealed", 2, "/board/hidden"))
        unsealed, _ = self.check("offset-unsealed", "--executor", "test_branch_selfcheck:offset_executor",
                                 "--sample-nodes", "root,A", "--reference", "none")
        self.assertEqual(unsealed["verdict"]["status"], "passed")
        self.assertTrue(any("no sealed reference" in note for note in unsealed["verdict"]["notes"]))
        self.assertTrue(unsealed["samples"][0]["rungs"]["r1"]["note"])

    def test_restore_failure_blocks_the_ladder_and_keeps_the_run_evidence(self):
        report, output = self.check("restore", "--executor", "test_branch_selfcheck:broken_restore_executor",
                                   "--sample-nodes", "root", expect=1)
        sample = report["samples"][0]
        self.assertEqual(sample["status"], "failed")
        self.assertEqual([sample["rungs"][rung]["status"] for rung in branch_selfcheck.RUNG_ORDER],
                         ["failed", "not_reached", "not_reached", "not_reached"])
        self.assertEqual(sample["rungs"]["r0"]["first_difference"]["reason"], "restore_not_equal")
        self.assertEqual(sample["rungs"]["r1"]["blocked_by"], "r0")
        self.assertEqual(sample["first_divergence"]["rung"], "r0")
        self.assertEqual(report["rungs"]["r0"]["failed"], 1)
        record = json.loads((output / sample["runs"][0]["record"]).read_text(encoding="utf-8"))
        self.assertFalse(record["restore"]["equal"])
        self.assertEqual(record["restore"]["note"], "fixture restore mismatch")

    def test_existing_output_is_never_overwritten(self):
        report, output = self.check("immutable", "--executor", "test_branch_selfcheck:fixture_executor",
                                   "--sample-nodes", "root")
        before = (output / branch_selfcheck.REPORT_FILE).read_bytes()
        code, message = self.run_cli(output, "--executor", "test_branch_selfcheck:divergent_executor")
        self.assertEqual(code, 1)
        self.assertIn("rejected", message)
        self.assertEqual((output / branch_selfcheck.REPORT_FILE).read_bytes(), before)
        self.assertEqual(report["verdict"]["status"], "passed")

    def test_executor_contract_is_enforced(self):
        sample = branch_selfcheck.build_sample(self.tree, branch_selfcheck.node_records(self.tree), "A")
        self.assertEqual(sample.branch_id, "A")
        self.assertEqual(sample.fork["tick"], 3)
        self.assertEqual(sample.end["tick"], 5)
        self.assertEqual(sample.edge_ticks, 2)
        self.assertEqual(sample.parent_trajectory_id, self.source_trajectory.manifest["trajectory_id"])
        with self.assertRaisesRegex(Exception, "real game process"):
            branch_selfcheck.normalize_run("run-a", {"mode": "x", "frames": []}, sample)
        with self.assertRaisesRegex(Exception, "frames stop at tick"):
            branch_selfcheck.normalize_run("run-a", {"mode": "x", "real_game": False,
                                                     "frames": [{"tick": 0, "state": {"a": 1}}]}, sample)
        with self.assertRaisesRegex(Exception, "tick-zero"):
            branch_selfcheck.normalize_run("run-a", {"mode": "x", "real_game": False,
                                                     "frames": [{"tick": 1, "state": {"a": 1}}]}, sample)


if __name__ == "__main__":
    unittest.main()
