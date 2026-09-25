"""Contract tests for the #111 lifecycle capture-point table.

No game, no AvZ and no native runtime: these fixtures only prove that the
phase-A table stays internally consistent and anchored, and that the junior
execution gate (no unconfirmed address in a review_required row) is enforced.
"""
import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import issue111_capture_points as capture


class CapturePointContractTests(unittest.TestCase):
    def setUp(self):
        self.table = capture.load()

    def problems(self, table):
        return capture.problems(table, ROOT)

    def assertProblem(self, issues, needle):
        self.assertTrue(any(needle in issue for issue in issues), issues)

    def test_repository_table_passes_and_is_anchored(self):
        self.assertEqual(self.problems(self.table), [])
        self.assertEqual(capture.check_sources(self.table, ROOT), [])
        summary = capture.summary(self.table)
        self.assertEqual(summary["capture_points"], 5)
        self.assertEqual(summary["established"], 2)
        self.assertEqual(summary["review_required"], 3)
        self.assertEqual(summary["hooks_added"], 0)
        self.assertEqual(summary["game_runs"], 0)

    def test_established_row_requires_repository_evidence(self):
        table = copy.deepcopy(self.table)
        row = next(item for item in table["capture_points"] if item["status"] == "established")
        row["abi"]["evidence"] = []
        self.assertProblem(self.problems(table), "needs abi.evidence")

    def test_established_row_evidence_path_must_exist(self):
        table = copy.deepcopy(self.table)
        row = next(item for item in table["capture_points"] if item["status"] == "established")
        row["abi"]["evidence"] = [{"path": "determinism/not-a-real-file.cpp", "availability": "repository"}]
        self.assertProblem(self.problems(table), "does not exist")

    def test_review_required_row_rejects_address_claims(self):
        table = copy.deepcopy(self.table)
        row = next(item for item in table["capture_points"] if item["status"] == "review_required")
        row["abi"]["entry_rva"] = "0x123456"
        self.assertProblem(self.problems(table), "claims addresses")

    def test_review_required_row_requires_open_questions(self):
        table = copy.deepcopy(self.table)
        row = next(item for item in table["capture_points"] if item["status"] == "review_required")
        row["open_questions"] = []
        self.assertProblem(self.problems(table), "needs open_questions")

    def test_review_required_row_must_not_claim_coverage(self):
        table = copy.deepcopy(self.table)
        row = next(item for item in table["capture_points"] if item["status"] == "review_required")
        row["coverage"]["covered_paths"] = ["pretend"]
        self.assertProblem(self.problems(table), "must not claim covered_paths")

    def test_seq_cannot_masquerade_as_capture_sequence(self):
        table = copy.deepcopy(self.table)
        table["data_contract"]["seq_policy"]["seq_is_capture_order"] = True
        self.assertProblem(self.problems(table), "write order")

    def test_capture_sequence_must_survive_drain(self):
        table = copy.deepcopy(self.table)
        table["data_contract"]["seq_policy"]["capture_sequence_reset_on_drain"] = True
        self.assertProblem(self.problems(table), "survive drain")

    def test_missing_capture_sequence_marks_unavailable(self):
        table = copy.deepcopy(self.table)
        table["data_contract"]["seq_policy"]["missing"] = "not mentioned"
        self.assertProblem(self.problems(table), "unavailable")

    def test_duplicate_ids_rejected(self):
        table = copy.deepcopy(self.table)
        table["capture_points"].append(copy.deepcopy(table["capture_points"][0]))
        self.assertProblem(self.problems(table), "duplicate capture point id")

    def test_missing_fact_class_rejected(self):
        table = copy.deepcopy(self.table)
        del table["fact_classes"]["slot_recycle"]
        self.assertProblem(self.problems(table), "fact_classes")

    def test_delivery_must_not_claim_hooks_or_runs(self):
        table = copy.deepcopy(self.table)
        table["delivery"]["hooks_added"] = 1
        table["delivery"]["game_runs"] = 2
        issues = self.problems(table)
        self.assertProblem(issues, "hooks_added must be 0")
        self.assertProblem(issues, "game_runs must be 0")

    def test_anchor_mismatch_between_table_and_spawn_hook_is_caught(self):
        table = copy.deepcopy(self.table)
        row = next(item for item in table["capture_points"] if item["id"] == "zombie-initialize-exit")
        row["abi"]["entry_rva"] = "0x522581"
        self.assertProblem(capture.check_sources(table, ROOT), "spawn_hook")

    def test_cli_passes_on_repository_table_and_fails_on_broken_copy(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            status = capture.main(["--table", str(capture.TABLE), "--root", str(ROOT)])
        self.assertEqual(status, 0, buffer.getvalue())
        self.assertTrue(json.loads(buffer.getvalue())["ok"])

        broken = copy.deepcopy(self.table)
        broken["data_contract"]["seq_policy"]["seq_is_capture_order"] = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.json"
            path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                status = capture.main(["--table", str(path), "--root", str(ROOT)])
        self.assertEqual(status, 1)

    def test_capture_point_missing_caller_basis_rejected(self):
        table = copy.deepcopy(self.table)
        table["capture_points"][0].pop("caller_branch_basis")
        self.assertProblem(self.problems(table), "missing key: caller_branch_basis")

    def test_contract_document_capture_point_ids_must_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            docs = Path(directory) / "docs"
            docs.mkdir()
            (docs / "issue111-生命周期测量合同.md").write_text("参见 `zombie-typo-here`。", encoding="utf-8")
            self.assertProblem(capture.problems(self.table, Path(directory)), "unknown capture point id")

    def test_unreadable_table_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.json"
            with redirect_stdout(io.StringIO()) as buffer:
                status = capture.main(["--table", str(path)])
        self.assertEqual(status, 1)
        self.assertFalse(json.loads(buffer.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
