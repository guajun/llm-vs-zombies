"""Tree evidence: root identity, parent links, branch scope and node chains (#31)."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path

from llm_vs_zombies.audit_compare import EvidenceError
from llm_vs_zombies.client import Client
from llm_vs_zombies.engine_replay import (ReplaySession, Trajectory, build_trajectory,
                                          capture_initial, identity_from_launcher, replay)
from llm_vs_zombies.evidence_tree import (TREE_FILE, branch_placement, chain_sha256, content_identity,
                                          end_boundary, export_tree, package_tree, root_identity,
                                          root_placement, validate_embedded_tree, validate_tree)
from llm_vs_zombies.session import SessionTrace

from test_engine_replay import ARTIFACTS, ModelTransport


class TreeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        with SessionTrace(self.source / "session.jsonl") as trace, \
                Client(ModelTransport(self.source / "audit"), trace=trace) as client:
            self.identity = identity_from_launcher(client.hello(), ARTIFACTS)
            capture_initial(client, identity=self.identity, initialization={"synthetic": True})
            client.commit([{"op": "plant", "row": 1, "col": 1}], advance_ticks=4)
            client.advance(1)
            client.request("stop_recording", expect=client.version)
        self.identity_record = root_identity(game=self.identity["game"], artifacts=self.identity["artifacts"],
                                             init_recipe={"synthetic": True, "seed": 42})
        self.trunk = build_trajectory(self.source / "session.jsonl", self.source / "audit",
                                      self.root / "trunk", tree=root_placement("main", self.identity_record))
        self.end = end_boundary(self.trunk)

    def tearDown(self):
        self.temp.cleanup()

    def branch(self, name, action, *, node_name=None, trunk=False):
        """Build one forked recording and place it under the trunk node."""

        def initializer(trajectory, output):
            @contextmanager
            def initialize():
                live = ModelTransport(output / "audit", epoch=99, revision=2, pixel_byte=2)
                with SessionTrace(output / "actual.jsonl") as trace, Client(live, trace=trace) as client:
                    yield ReplaySession(client, copy.deepcopy(self.identity), output / "audit")
                    client.request("stop_recording", expect=client.version)
            return initialize()

        run = self.root / f"run-{name}"
        replay(self.trunk, initializer, run, target_tick=self.end["tick"], on_takeover=action)
        return build_trajectory(run / "actual.jsonl", run / "audit", self.root / f"node-{name}",
                                tree=branch_placement(self.trunk, node_name or name, trunk=trunk))

    def fork(self, name, action):
        return self.branch(name, action)

    def two_branch_tree(self):
        left = self.fork("A", lambda session, _: session.client.commit([{"op": "plant", "row": 6, "col": 8}],
                                                                     advance_ticks=2))
        right = self.fork("B", lambda session, _: session.client.advance(2))
        tree = package_tree(self.root / "tree", self.trunk, branches=[left, right])
        return tree, left, right

    @staticmethod
    def rewrite(directory, change):
        path = directory / "trajectory.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        change(manifest)
        manifest["trajectory_id"] = content_identity(manifest)
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

    @staticmethod
    def rewrite_index(directory, change):
        path = directory / TREE_FILE
        record = json.loads(path.read_text(encoding="utf-8"))
        change(record)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

    def test_two_branch_tree_packages_validates_and_exports_deterministically(self):
        tree, left, right = self.two_branch_tree()
        self.assertEqual(left.manifest["tree"]["parent"]["trajectory_id"], self.trunk.manifest["trajectory_id"])
        self.assertEqual(right.manifest["tree"]["branch_id"], "B")
        self.assertFalse(left.manifest["tree"]["trunk"])
        report = tree.validate()
        self.assertEqual(report["nodes"], 3)
        self.assertEqual(report["root"], "root")
        self.assertEqual(report["branches"], ["A", "B", "main"])
        self.assertEqual(report["trunk_nodes"], ["root"])
        self.assertEqual(sorted(point["parent"] for point in report["branch_points"]), ["root", "root"])
        self.assertEqual([point["boundary"] for point in report["branch_points"]], [self.end, self.end])
        self.assertEqual(report["tree_id"], self.trunk.manifest["tree"]["tree_id"])
        self.assertEqual(validate_tree(self.root / "tree"), report)
        first = export_tree(self.root / "tree", self.root / "tree.zip")
        second = tree.export(self.root / "tree-second.zip")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with zipfile.ZipFile(first) as archive:
            self.assertIn("nodes/root/trajectory.json", archive.namelist())
            self.assertIn(TREE_FILE, archive.namelist())

    def test_trunk_continuation_carries_the_real_baseline_and_branches_do_not(self):
        continuation = self.branch("main", lambda session, _: session.client.advance(2), trunk=True)
        left = self.fork("A", lambda session, _: session.client.advance(3))
        tree = package_tree(self.root / "tree", self.trunk, branches=[continuation, left])
        report = tree.validate()
        self.assertEqual(report["trunk_nodes"], ["main", "root"])
        self.assertEqual(report["branches"], ["A", "main"])
        self.assertFalse(left.manifest["tree"]["trunk"])

    def test_placement_does_not_change_the_recording_content_identity(self):
        plain = build_trajectory(self.source / "session.jsonl", self.source / "audit", self.root / "plain")
        self.assertNotIn("tree", plain.manifest)
        self.assertEqual(plain.manifest["trajectory_id"], self.trunk.manifest["trajectory_id"])
        self.assertEqual(Trajectory.load(self.root / "plain").manifest["trajectory_id"],
                         self.trunk.manifest["trajectory_id"])
        self.assertEqual(validate_embedded_tree(plain.manifest), {"present": False})
        with self.assertRaisesRegex(EvidenceError, "tree placement"):
            package_tree(self.root / "unplaced", plain)
        self.assertFalse((self.root / "unplaced").exists())

    def test_rewritten_root_content_breaks_the_verified_chain(self):
        tree, _, _ = self.two_branch_tree()
        self.rewrite(tree.directory / "nodes/root", lambda manifest: manifest.update(scope="rewritten after sealing"))
        with self.assertRaisesRegex(EvidenceError, "root reference|hash chain"):
            validate_tree(tree.directory)

    def test_tampered_child_parent_reference_breaks_its_own_link(self):
        tree, _, _ = self.two_branch_tree()
        self.rewrite(tree.directory / "nodes/A",
                     lambda manifest: manifest["tree"]["parent"].update(trajectory_id="f" * 64))
        with self.assertRaisesRegex(EvidenceError, "does not continue the recorded parent|hash chain is broken"):
            validate_tree(tree.directory)

    def test_chain_must_continue_the_actual_parent_node(self):
        tree, _, _ = self.two_branch_tree()

        def reanchor(manifest):
            manifest["tree"]["parent"]["chain_sha256"] = "a" * 64
            manifest["tree"]["chain"]["parent_sha256"] = "a" * 64
            manifest["tree"]["chain"]["sha256"] = chain_sha256(
                trajectory_id=manifest["trajectory_id"], branch=manifest["tree"]["branch_id"], trunk=False,
                parent=manifest["tree"]["parent"], parent_sha256="a" * 64,
                tree_id=manifest["tree"]["root"]["sha256"])

        self.rewrite(tree.directory / "nodes/A", reanchor)
        with self.assertRaisesRegex(EvidenceError, "does not continue its actual parent"):
            validate_tree(tree.directory)

    def test_index_must_name_the_actual_parent_node(self):
        tree, _, _ = self.two_branch_tree()
        self.rewrite_index(tree.directory, lambda record: next(
            node for node in record["nodes"] if node["key"] == "A").update(parent_key="B"))
        with self.assertRaisesRegex(EvidenceError, "parent disagrees with the tree index"):
            validate_tree(tree.directory)

    def test_branch_node_cannot_claim_the_real_continuation_baseline(self):
        tree, _, _ = self.two_branch_tree()

        def claim_trunk(manifest):
            manifest["tree"]["trunk"] = True
            manifest["tree"]["chain"]["sha256"] = chain_sha256(
                trajectory_id=manifest["trajectory_id"], branch=manifest["tree"]["branch_id"], trunk=True,
                parent=manifest["tree"]["parent"],
                parent_sha256=manifest["tree"]["parent"]["chain_sha256"],
                tree_id=manifest["tree"]["root"]["sha256"])

        self.rewrite(tree.directory / "nodes/A", claim_trunk)
        self.rewrite_index(tree.directory, lambda record: next(
            node for node in record["nodes"] if node["key"] == "A").update(trunk=True))
        with self.assertRaisesRegex(EvidenceError, "real continuation baseline"):
            validate_tree(tree.directory)

    def test_duplicate_branch_id_is_rejected(self):
        tree, _, _ = self.two_branch_tree()

        def rename_to_a(manifest):
            manifest["tree"]["branch_id"] = "A"
            manifest["tree"]["chain"]["sha256"] = chain_sha256(
                trajectory_id=manifest["trajectory_id"], branch="A", trunk=False,
                parent=manifest["tree"]["parent"],
                parent_sha256=manifest["tree"]["parent"]["chain_sha256"],
                tree_id=manifest["tree"]["root"]["sha256"])

        self.rewrite(tree.directory / "nodes/B", rename_to_a)
        self.rewrite_index(tree.directory, lambda record: next(
            node for node in record["nodes"] if node["key"] == "B").update(branch_id="A"))
        with self.assertRaisesRegex(EvidenceError, "separate paths"):
            validate_tree(tree.directory)

    def test_parent_outside_the_packaged_tree_is_rejected(self):
        tree, _, _ = self.two_branch_tree()
        self.rewrite_index(tree.directory, lambda record: next(
            node for node in record["nodes"] if node["key"] == "A").update(parent_key="ghost"))
        with self.assertRaisesRegex(EvidenceError, "parent node is missing: ghost"):
            validate_tree(tree.directory)

    def test_placement_helpers_reject_unverifiable_input(self):
        self.assertEqual(branch_placement(self.trunk, "A").parent["boundary"], self.end)
        with self.assertRaisesRegex(EvidenceError, "lowercase SHA-256"):
            root_identity(game={"a": 1}, artifacts={"game": "1" * 63}, init_recipe={"x": 1})
        with self.assertRaisesRegex(EvidenceError, "initialization recipe"):
            root_identity(game={"a": 1}, artifacts={"game": "1" * 64}, init_recipe={})
        with self.assertRaisesRegex(EvidenceError, "branch_id"):
            branch_placement(self.trunk, "not a branch id")
        with self.assertRaisesRegex(EvidenceError, "continue its parent's branch_id"):
            branch_placement(self.trunk, "other", trunk=True)
        with self.assertRaisesRegex(EvidenceError, "nonnegative integers"):
            branch_placement(self.trunk, "A", at={"epoch": 1, "tick": -1, "revision": 0})
        with self.assertRaisesRegex(EvidenceError, "exactly epoch, tick and revision"):
            branch_placement(self.trunk, "A", at={"epoch": 1, "tick": 0})


if __name__ == "__main__":
    unittest.main()
