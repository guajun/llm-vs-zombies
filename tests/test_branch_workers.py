"""Parallel branch workers on synthetic fixtures (N10 / issue #38).

The executor fixture drives the synthetic counter engine from
``test_engine_replay`` - the same fixture N8 uses - inside real child worker
processes, so scheduling, isolation, failure handling and the P1 frame
comparison are exercised without any game, launcher or user profile. Every
report produced here states ``real_game_run: false``; real-game execution
stays out of scope and is listed in the report's prerequisites.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from contextlib import contextmanager
import copy
import hashlib

from llm_vs_zombies import branch_workers as bw
from llm_vs_zombies import evaluation as ev
from llm_vs_zombies.audit_compare import AuditLog, canonical
from llm_vs_zombies.client import Client
from llm_vs_zombies.engine_replay import (ReplaySession, build_trajectory, capture_initial,
                                          identity_from_launcher, replay)
from llm_vs_zombies.evidence_tree import (branch_placement, end_boundary, package_tree, root_identity,
                                          root_placement)
from llm_vs_zombies.session import SessionTrace

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import branch_workers as tool  # noqa: E402  tools/branch_workers.py

from test_engine_replay import ARTIFACTS, ModelTransport  # noqa: E402

STEPS = ({"request": {"method": "commit",
                      "params": {"actions": [{"op": "plant", "row": 1, "col": 1}], "advance_ticks": 2}}},
         {"request": {"method": "advance", "params": {"max_ticks": 1}}})
OTHER_STEPS = ({"request": {"method": "advance", "params": {"max_ticks": 3}}},)


def synthetic_frames(task, directory):
    """Replay the branch's steps through a fresh synthetic engine and collect frames."""
    evidence = Path(directory) / "evidence"
    with SessionTrace(evidence / "session.jsonl") as trace, \
            Client(ModelTransport(evidence / "audit", epoch=7), trace=trace) as client:
        identity = identity_from_launcher(client.hello(), ARTIFACTS)
        marker = capture_initial(client, identity=identity, initialization=task["root_identity"]["init_recipe"])
        for step in task["steps"]:
            request = step["request"]
            if request["method"] == "commit":
                client.commit(request["params"]["actions"], advance_ticks=request["params"]["advance_ticks"])
            elif request["method"] == "advance":
                client.advance(request["params"]["max_ticks"])
            else:
                raise AssertionError(f"fixture executor cannot replay {request['method']}")
        client.request("stop_recording", expect=client.version)
    frames = [{"tick": 0, "state": marker["state"]}]
    for frame in AuditLog(evidence / "audit", require_closed=True).frames:
        if frame.kind == "post_step":
            frames.append({"tick": frame.version["tick"], "state": frame.state})
    return frames, identity


def fixture_executor(task, directory):
    """Deterministic child-process executor with explicit failure modes."""
    directory = Path(directory)
    mode = os.environ.get("FIXTURE_MODE", "deterministic")
    if mode == "fail":
        (directory / "evidence" / "attempt.txt").write_text("this branch fails on purpose\n", encoding="utf-8")
        raise RuntimeError("fixture branch failure")
    if mode == "hang":
        (directory / "evidence" / "attempt.txt").write_text("this branch hangs on purpose\n", encoding="utf-8")
        while True:
            time.sleep(0.05)
    if mode == "peer_write":
        peer = Path(os.environ["FIXTURE_PEER_DIR"])
        deadline = time.monotonic() + 30
        while not (peer / bw.RECEIPT_FILE).is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        (peer / "artifacts" / "peer-intrusion.txt").write_text(
            "written into another branch's directory\n", encoding="utf-8")
    frames, identity = synthetic_frames(task, directory)
    return {"mode": "synthetic_fixture_replay", "real_game": False, "identity": identity, "frames": frames,
            "note": f"pid {os.getpid()} ran {task['branch_id']} in {Path.cwd()}"}


def fixture_identity(directory):
    """The identity a fixture executor will report for this test run."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with SessionTrace(directory / "identity.jsonl") as trace, \
            Client(ModelTransport(directory / "audit"), trace=trace) as client:
        identity = identity_from_launcher(client.hello(), ARTIFACTS)
        client.request("stop_recording", expect=client.version)
    return identity


class BranchWorkerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        identity = fixture_identity(cls.root / "identity")
        cls.root_identity = root_identity(game=identity["game"], artifacts=identity["artifacts"],
                                         init_recipe={"synthetic": True})

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def executor_spec(self):
        return "test_branch_workers:fixture_executor"

    def plan(self, branches, **overrides):
        document = {"schema": bw.SCHEMA, "executor": self.executor_spec(), "max_workers": len(branches),
                    "timeout_seconds": 120, "root_identity": self.root_identity,
                    "device": {"policy": "exclusive_lease", "lease_timeout_seconds": 60},
                    "env": {"PYTHONPATH": str(TESTS)},
                    "branches": branches}
        document.update(overrides)
        return document

    def inline(self, branch_id, steps=STEPS, **extra):
        return {"branch_id": branch_id, "steps": [json.loads(json.dumps(step)) for step in steps], **extra}

    def run_plan(self, document, name, **options):
        workspace = self.root / name
        return bw.run_branches(bw.Plan.from_dict(document), workspace, **options), workspace

    def run_tool(self, arguments):
        """Call the real command line entry point in-process; argparse exits on rejection."""
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = tool.main([str(item) for item in arguments])
            except SystemExit as exit:
                code = exit.code
        return code, stderr.getvalue() or stdout.getvalue()


class PlanTests(BranchWorkerTestCase):
    def test_plan_round_trip_keeps_a_derived_identity(self):
        plan = bw.Plan.from_dict(self.plan([self.inline("A"), self.inline("B")]))
        again = bw.Plan.from_dict(plan.to_dict())
        self.assertEqual(again.plan_id, plan.plan_id)
        self.assertEqual([branch.branch_id for branch in again.branches], ["A", "B"])
        self.assertEqual(again.branches[0].steps_sha256, plan.branches[0].steps_sha256)
        self.assertNotEqual(plan.branches[0].steps_sha256, bw.Plan.from_dict(
            self.plan([self.inline("A", OTHER_STEPS), self.inline("B")])).branches[0].steps_sha256)
        document = plan.to_dict()
        document["plan_id"] = "0" * 64
        with self.assertRaisesRegex(bw.EvidenceError, "plan_id"):
            bw.Plan.from_dict(document)

    def test_scoring_pipeline_keys_are_rejected_at_every_level(self):
        for key, value in (("cold_starts", 3), ("cold_workers", 2), ("strict", True)):
            with self.subTest(key=key), self.assertRaisesRegex(bw.EvidenceError, "scoring-pipeline"):
                bw.Plan.from_dict(self.plan([self.inline("A"), self.inline("B")], **{key: value}))
        with self.assertRaisesRegex(bw.EvidenceError, "scoring-pipeline"):
            bw.Plan.from_dict(self.plan([self.inline("A", cold_starts=1), self.inline("B")]))

    def test_unknown_keys_duplicate_labels_and_reused_nodes_are_rejected(self):
        with self.assertRaisesRegex(bw.EvidenceError, "unknown keys"):
            bw.Plan.from_dict(self.plan([self.inline("A"), self.inline("B")], surprise=1))
        with self.assertRaisesRegex(bw.EvidenceError, "used twice"):
            bw.Plan.from_dict(self.plan([self.inline("A"), self.inline("A")]))
        for branch_id in ("../escape", "A/B", "", "A" * 65):
            with self.subTest(branch_id=branch_id), self.assertRaisesRegex(bw.EvidenceError, "filesystem-safe"):
                bw.Plan.from_dict(self.plan([self.inline(branch_id), self.inline("B")]))
        with self.assertRaisesRegex(bw.EvidenceError, "cannot exceed the number of branches"):
            bw.Plan.from_dict(self.plan([self.inline("A"), self.inline("B")], max_workers=3))
        with self.assertRaisesRegex(bw.EvidenceError, "max_workers"):
            bw.Plan.from_dict(self.plan([self.inline("A"), self.inline("B")], max_workers=5))

    def test_comparisons_need_one_root_and_one_action_sequence(self):
        document = self.plan([self.inline("A"), self.inline("B", OTHER_STEPS)],
                             comparisons=[{"name": "cross", "branches": ["A", "B"]}])
        with self.assertRaisesRegex(bw.EvidenceError, "different action sequences"):
            bw.Plan.from_dict(document)
        document = self.plan([self.inline("A"), self.inline("B")],
                             comparisons=[{"name": "cross", "branches": ["A", "C"]}])
        with self.assertRaisesRegex(bw.EvidenceError, "unknown branch"):
            bw.Plan.from_dict(document)
        document = self.plan([self.inline("A")], comparisons=[{"name": "cross", "branches": ["A", "A"]}])
        with self.assertRaisesRegex(bw.EvidenceError, "two distinct branch ids"):
            bw.Plan.from_dict(document)

    def test_device_policy_must_be_chosen_explicitly(self):
        with self.assertRaisesRegex(bw.EvidenceError, "device policy"):
            bw.Plan.from_dict(self.plan([self.inline("A")], device={"policy": "virtual_device"}))
        with self.assertRaisesRegex(bw.EvidenceError, "accept_shared"):
            bw.Plan.from_dict(self.plan([self.inline("A")], device={"policy": "shared_declared"}))
        with self.assertRaisesRegex(bw.EvidenceError, "requires a reason"):
            bw.Plan.from_dict(self.plan([self.inline("A")],
                                        device={"policy": "shared_declared", "accept_shared": True}))
        plan = bw.Plan.from_dict(self.plan([self.inline("A")], device={"policy": "shared_declared",
                                                                      "accept_shared": True,
                                                                      "reason": "fixture only"}))
        self.assertEqual(plan.device["policy"], "shared_declared")

    def test_executor_specs_accept_windows_paths_and_modules(self):
        target, name = bw.parse_executor(r"F:\somewhere\executor.py:run")
        self.assertEqual((target, name), (r"F:\somewhere\executor.py", "run"))
        self.assertEqual(bw.parse_executor("package.module:run"), ("package.module", "run"))
        with self.assertRaisesRegex(bw.EvidenceError, "module:function"):
            bw.parse_executor("module-only")


class JournalTests(BranchWorkerTestCase):
    def journal(self, records, name):
        path = self.root / f"{name}.jsonl"
        path.write_text("".join(json.dumps(record, sort_keys=True) + "\n" for record in records), encoding="utf-8")
        return path

    def acquire(self, branch, token, ns, wait=0.0):
        return {"event": "acquire", "branch_id": branch, "token": token, "wait_seconds": wait, "monotonic_ns": ns}

    def test_paired_windows_pass_and_overlaps_fail(self):
        paired = [self.acquire("A", "1", 10), {"event": "release", "branch_id": "A", "token": "1", "monotonic_ns": 20},
                  self.acquire("B", "2", 30), {"event": "release", "branch_id": "B", "token": "2", "monotonic_ns": 40}]
        result = bw.verify_lease_journal(self.journal(paired, "paired"), expect=2)
        self.assertEqual(result["status"], "passed")
        self.assertEqual((result["acquires"], result["releases"]), (2, 2))
        self.assertEqual(result["overlaps"], [])
        overlapping = [self.acquire("A", "1", 10),
                       self.acquire("B", "2", 15),
                       {"event": "release", "branch_id": "A", "token": "1", "monotonic_ns": 25},
                       {"event": "release", "branch_id": "B", "token": "2", "monotonic_ns": 35}]
        result = bw.verify_lease_journal(self.journal(overlapping, "overlapping"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(result["overlaps"]), 1)
        unclosed = [self.acquire("A", "1", 10)]
        result = bw.verify_lease_journal(self.journal(unclosed, "unclosed"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["problems"][0]["reason"], "unclosed lease window")
        result = bw.verify_lease_journal(self.journal(unclosed, "unclosed"), expect=2)
        self.assertTrue(any("expected 2" in problem["reason"] for problem in result["problems"]))

    def test_parent_abandonment_seals_a_window_without_faking_a_release(self):
        path = self.journal([self.acquire("A", "1", 10)], "abandoned")
        sealed = bw.close_open_lease(path, "A", "the branch did not release its device window")
        self.assertEqual(len(sealed), 1)
        self.assertEqual(bw.close_open_lease(path, "A", "again"), [])
        result = bw.verify_lease_journal(path)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["abandoned"], 1)
        self.assertEqual(result["unbounded"][0]["branch_id"], "A")


class TreePlanTests(BranchWorkerTestCase):
    """Two branches of one sealed tree, executed in parallel from that tree's root."""

    @classmethod
    def _fork(cls, identity, parent, name, end):
        """Record a node whose takeover action is identical for every replica."""

        @contextmanager
        def initialize(trajectory, output):
            with SessionTrace(output / "actual.jsonl") as trace, \
                    Client(ModelTransport(output / "audit", epoch=99, revision=2), trace=trace) as client:
                yield ReplaySession(client, copy.deepcopy(identity), output / "audit")
                client.request("stop_recording", expect=client.version)

        run = cls.tree_root / f"run-{name}"
        replay(parent, initialize, run, target_tick=end["tick"],
               on_takeover=lambda session, _: session.client.commit([{"op": "plant", "row": 6, "col": 8}],
                                                                    advance_ticks=2))
        return build_trajectory(run / "actual.jsonl", run / "audit", cls.tree_root / f"node-{name}",
                                tree=branch_placement(parent, name))

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tree_root = cls.root / "sealed"
        cls.tree_root.mkdir()
        source = cls.tree_root / "source"
        source.mkdir()
        with SessionTrace(source / "session.jsonl") as trace, \
                Client(ModelTransport(source / "audit"), trace=trace) as client:
            identity = identity_from_launcher(client.hello(), ARTIFACTS)
            marker = capture_initial(client, identity=identity, initialization={"synthetic": True})
            client.commit([{"op": "plant", "row": 1, "col": 1}], advance_ticks=2)
            client.advance(1)
            client.request("stop_recording", expect=client.version)
        cls.sealed_identity = identity
        cls.root_image = hashlib.sha256(canonical(marker["state"])).hexdigest()
        trunk = build_trajectory(
            source / "session.jsonl", source / "audit", cls.tree_root / "trunk",
            tree=root_placement("main", root_identity(game=identity["game"], artifacts=identity["artifacts"],
                                                      init_recipe={"synthetic": True},
                                                      image_sha256=cls.root_image)))
        cls.trunk_end = end_boundary(trunk)
        cls.tree = package_tree(cls.tree_root / "tree", trunk,
                                branches=[cls._fork(identity, trunk, "A", cls.trunk_end),
                                          cls._fork(identity, trunk, "A2", cls.trunk_end)])
        cls.tree_summary = cls.tree.validate()

    def test_tree_branches_share_the_root_and_still_produce_identical_frames(self):
        document = {"schema": bw.SCHEMA, "executor": self.executor_spec(), "max_workers": 2,
                    "timeout_seconds": 120, "tree": str(self.tree.directory),
                    "device": {"policy": "exclusive_lease", "lease_timeout_seconds": 60},
                    "env": {"PYTHONPATH": str(TESTS)},
                    "branches": [{"branch_id": "A", "node": "A"},
                                 {"branch_id": "replica", "node": "A2"}],
                    "comparisons": [{"name": "same-actions", "branches": ["A", "replica"]}]}
        plan = bw.Plan.from_dict(document)
        self.assertEqual(plan.tree["tree_id"], self.tree_summary["tree_id"])
        self.assertEqual(plan.root_identity["image_sha256"], self.root_image)
        self.assertEqual([branch.node for branch in plan.branches], ["A", "A2"])
        self.assertEqual(plan.branches[0].steps_sha256, plan.branches[1].steps_sha256)
        with self.assertRaisesRegex(bw.EvidenceError, "root identity"):
            bw.Plan.from_dict(dict(document, root_identity=dict(plan.root_identity, image_sha256="0" * 64)))
        (report, _), workspace = self.run_plan(document, "tree")
        self.assertEqual(report["verdict"]["status"], "passed")
        self.assertEqual(report["comparisons"][0]["status"], "passed")
        self.assertTrue(report["comparisons"][0]["comparisons"][0]["equal"])
        placements = {node["key"]: node for node in self.tree.nodes}
        for record in report["branches"]:
            node = record["node"]
            sealed = placements[node]
            self.assertEqual(record["tree"]["tree_id"], self.tree_summary["tree_id"])
            self.assertEqual(record["tree"]["branch_id"], sealed["branch_id"])
            self.assertEqual(record["tree"]["trajectory_id"], sealed["trajectory_id"])
            self.assertEqual(record["tree"]["node"], node)
            self.assertFalse(record["tree"]["trunk"])
            self.assertEqual(record["tree"]["fork"]["tick"], self.trunk_end["tick"])
            self.assertEqual(record["status"], "passed")
            self.assertEqual(record["receipt"]["document"]["result"]["identity_binding"], "matched")
            frames = json.loads((workspace / record["frames"]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(frames["tick_range"][1], record["end"]["tick"])
            self.assertEqual(len(frames["frames"]), report["comparisons"][0]["comparisons"][0]["frames"])
            self.assertEqual(frames["steps_sha256"], record["steps_sha256"])
        self.assertEqual(self.tree.validate()["tree_id"], self.tree_summary["tree_id"])


class HarnessTests(BranchWorkerTestCase):
    def branch(self, report, branch_id):
        return next(record for record in report["branches"] if record["branch_id"] == branch_id)

    def test_parallel_same_actions_produce_identical_frames_and_isolated_evidence(self):
        document = self.plan([self.inline("A"), self.inline("B")],
                             comparisons=[{"name": "replicas", "branches": ["A", "B"]}])
        (report, path), workspace = self.run_plan(document, "parallel")
        self.assertEqual(report["verdict"]["status"], "passed")
        self.assertEqual(report["verdict"]["branch_status"], {"A": "passed", "B": "passed"})
        self.assertEqual(report["isolation"]["status"], "passed")
        self.assertEqual(report["comparisons"][0]["status"], "passed")
        comparison = report["comparisons"][0]["comparisons"][0]
        self.assertTrue(comparison["equal"])
        self.assertGreater(comparison["frames"], 1)
        first, second = self.branch(report, "A"), self.branch(report, "B")
        self.assertNotEqual(first["host_process"]["owner"]["pid"], second["host_process"]["owner"]["pid"])
        self.assertNotEqual(first["host_process"]["owner"]["pid"], os.getpid())
        for record, branch_id in ((first, "A"), (second, "B")):
            frames = json.loads((workspace / record["frames"]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(frames["branch_id"], branch_id)
            self.assertEqual(frames["steps_sha256"], record["steps_sha256"])
            ticks = [frame["tick"] for frame in frames["frames"]]
            self.assertEqual(ticks[0], 0)
            self.assertEqual(len(set(ticks)), len(ticks))
            states = {json.dumps(frame["state"], sort_keys=True) for frame in frames["frames"]}
            self.assertGreater(len(states), 1)
        left = json.loads((workspace / first["frames"]["path"]).read_text(encoding="utf-8"))["frames"]
        right = json.loads((workspace / second["frames"]["path"]).read_text(encoding="utf-8"))["frames"]
        self.assertEqual(left, right)
        for branch_id in ("A", "B"):
            directory = workspace / bw.BRANCHES_DIRECTORY / branch_id
            for name in bw.BRANCH_DIRECTORIES:
                self.assertTrue((directory / name).is_dir())
            self.assertEqual(str(directory / "profile"),
                             self.branch(report, branch_id)["receipt"]["document"]["environment"]["LVZ_BRANCH_PROFILE"])
            self.assertEqual(str(directory.resolve()),
                             self.branch(report, branch_id)["receipt"]["document"]["environment"]["LVZ_BRANCH_DIR"])
        lease = report["isolation"]["device"]["lease"]
        self.assertEqual(lease["status"], "passed")
        self.assertEqual(lease["acquires"], 2)
        self.assertEqual(lease["overlaps"], [])
        self.assertEqual(sorted(interval["branch_id"] for interval in lease["intervals"]), ["A", "B"])
        self.assertEqual(report["report_id"], bw.digest_bytes({key: value for key, value in report.items()
                                                               if key != "report_id"}))
        self.assertEqual(report["plan"]["path"], bw.PLAN_FILE)
        self.assertTrue(path.is_file())

    def test_failing_branch_does_not_stop_its_siblings_and_keeps_its_archive(self):
        document = self.plan([self.inline("A"), self.inline("B", env={"FIXTURE_MODE": "fail"}),
                              self.inline("C")], max_workers=2, device={"policy": "shared_declared",
                                                                        "accept_shared": True,
                                                                        "reason": "fixture only"})
        (report, _), workspace = self.run_plan(document, "failure")
        self.assertEqual(report["verdict"]["status"], "failed")
        self.assertEqual(report["verdict"]["failed_branches"], ["B"])
        self.assertEqual(report["verdict"]["branch_status"], {"A": "passed", "B": "failed", "C": "passed"})
        failed = self.branch(report, "B")
        receipt = failed["receipt"]["document"]
        self.assertEqual(receipt["status"], "fail")
        self.assertEqual(receipt["error"]["stage"], "execute")
        self.assertIn("fixture branch failure", receipt["error"]["message"])
        self.assertIsNone(failed["failure_seal"])
        self.assertIsNone(failed["frames"])
        directory = workspace / bw.BRANCHES_DIRECTORY / "B"
        self.assertTrue((directory / bw.RECEIPT_FILE).is_file())
        self.assertEqual((directory / "evidence" / "attempt.txt").read_text(encoding="utf-8"),
                         "this branch fails on purpose\n")
        for branch_id in ("A", "C"):
            self.assertTrue((workspace / bw.BRANCHES_DIRECTORY / branch_id / bw.FRAMES_FILE).is_file())
        self.assertEqual(report["isolation"]["interference"]["mutations"], [])
        self.assertGreater(self.branch(report, "C")["host_process"]["dispatch_monotonic"],
                           self.branch(report, "B")["host_process"]["exit_monotonic"])

    def test_hung_branch_is_killed_without_touching_its_sibling(self):
        document = self.plan([self.inline("A"), self.inline("B", env={"FIXTURE_MODE": "hang"})],
                             max_workers=2, timeout_seconds=6,
                             device={"policy": "shared_declared", "accept_shared": True, "reason": "fixture only"})
        (report, _), workspace = self.run_plan(document, "deadline")
        self.assertEqual(report["verdict"]["branch_status"], {"A": "passed", "B": "failed"})
        failed = self.branch(report, "B")
        self.assertIn("wall deadline", failed["error"]["message"])
        self.assertTrue(failed["host_process"]["killed"])
        self.assertEqual(failed["failure_seal"]["path"], bw.FAILURE_FILE)
        directory = workspace / bw.BRANCHES_DIRECTORY / "B"
        seal = json.loads((directory / bw.FAILURE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(seal["branch_id"], "B")
        self.assertIn("wall deadline", seal["detail"]["message"])
        self.assertEqual((directory / "evidence" / "attempt.txt").read_text(encoding="utf-8"),
                         "this branch hangs on purpose\n")
        self.assertTrue((workspace / bw.BRANCHES_DIRECTORY / "A" / bw.FRAMES_FILE).is_file())
        self.assertEqual(report["isolation"]["interference"]["mutations"], [])

    def test_foreign_write_into_a_finished_branch_is_detected(self):
        document = self.plan([self.inline("A"),
                              self.inline("B", env={"FIXTURE_MODE": "peer_write",
                                                    "FIXTURE_PEER_DIR": str(self.root / "interference"
                                                                            / bw.BRANCHES_DIRECTORY / "A")})],
                             max_workers=2, device={"policy": "shared_declared", "accept_shared": True,
                                                    "reason": "fixture only"})
        (report, _), workspace = self.run_plan(document, "interference")
        self.assertEqual(report["isolation"]["interference"]["status"], "failed")
        self.assertEqual(report["verdict"]["status"], "failed")
        mutations = report["isolation"]["interference"]["mutations"]
        self.assertEqual(len(mutations), 1)
        self.assertEqual(mutations[0]["branch_id"], "A")
        self.assertEqual(mutations[0]["added"], ["artifacts/peer-intrusion.txt"])
        mutated = self.branch(report, "A")
        self.assertEqual(mutated["status"], "failed")
        self.assertEqual(mutated["error"]["stage"], "immutability")
        self.assertEqual(mutated["manifest"]["mutation"]["added"], ["artifacts/peer-intrusion.txt"])
        self.assertEqual(self.branch(report, "B")["status"], "passed")
        self.assertTrue((workspace / bw.BRANCHES_DIRECTORY / "A" / "artifacts"
                         / "peer-intrusion.txt").is_file())

    def test_unarmed_worker_never_runs_its_executor(self):
        document = self.plan([self.inline("A")], max_workers=1)
        plan = bw.Plan.from_dict(document)
        workspace = self.root / "unarmed"
        directory = workspace / bw.BRANCHES_DIRECTORY / "A"
        directory.mkdir(parents=True)
        task = bw.task_for(plan, plan.branch("A"), workspace, self.executor_spec(), {"pid": 999999999}, workers=1)
        task_path = bw.atomic(directory / bw.TASK_FILE, task)
        self.assertEqual(bw.worker(task_path), 1)
        receipt = json.loads((directory / bw.RECEIPT_FILE).read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "fail")
        self.assertEqual(receipt["error"]["stage"], "arm")
        self.assertIsNone(receipt["result"])
        self.assertFalse((directory / bw.FRAMES_FILE).exists())
        self.assertFalse((directory / "evidence" / "session.jsonl").exists())

    def test_verify_rechecks_evidence_without_rerunning_anything(self):
        document = self.plan([self.inline("A"), self.inline("B")],
                             comparisons=[{"name": "replicas", "branches": ["A", "B"]}])
        (report, _), workspace = self.run_plan(document, "verify")
        self.assertEqual(bw.verify_workspace(workspace)["verdict"]["status"], "passed")
        (workspace / bw.BRANCHES_DIRECTORY / "A" / "artifacts" / "tamper.txt").write_text(
            "late write\n", encoding="utf-8")
        result = bw.verify_workspace(workspace)
        self.assertEqual(result["verdict"]["status"], "failed")
        self.assertTrue(any(problem["reason"] == "branch evidence changed after the report"
                            for problem in result["verdict"]["problems"]))
        second = self.root / "verify-tool"
        code, message = self.run_tool(["run", self.write_plan("tool-plan.json", document), second])
        self.assertEqual(code, 0, message)
        code, message = self.run_tool(["verify", second])
        self.assertEqual(code, 0, message)
        self.assertEqual(json.loads(message)["verdict"]["status"], "passed")

    def test_reports_stay_out_of_the_scoring_cold_replay_pipeline(self):
        document = self.plan([self.inline("A"), self.inline("B")],
                             comparisons=[{"name": "replicas", "branches": ["A", "B"]}])
        (report, _), _ = self.run_plan(document, "boundary")
        self.assertEqual(report["pipeline"]["name"], "training-branch")
        self.assertEqual(report["pipeline"]["scoring_cold_replay"], "not_applicable")
        self.assertFalse(set(ev.LIVE_GATES) & set(report))
        for record in report["branches"]:
            self.assertFalse(set(ev.LIVE_GATES) & set(record))
        text = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("cold_starts", text)
        self.assertIn("not_applicable", text)
        self.assertFalse(report["limitations"]["real_game_run"])
        self.assertFalse(report["limitations"]["original_engine_verified"])
        self.assertIn("no real game process", report["limitations"]["statement"])
        self.assertTrue(report["limitations"]["prerequisites"])
        resources = {item["id"]: item for item in report["isolation"]["shared_resources"]}
        self.assertTrue({"C28", "C29", "C30", "C31", "C32", "C33", "C34"} <= set(resources))
        self.assertEqual(resources["C32"]["status"], "待定")
        self.assertEqual(resources["C33"]["status"], "待定")
        self.assertIn("docs/跨界耦合清单.md", resources["C32"]["evidence"])
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "src")
        probe = subprocess.run([sys.executable, "-c",
                                "import sys, llm_vs_zombies.branch_workers;"
                                "print('llm_vs_zombies.evaluation' in sys.modules)"],
                               capture_output=True, text=True, env=environment, check=True)
        self.assertEqual(probe.stdout.strip(), "False")

    def write_plan(self, name, document):
        path = self.root / name
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main()
