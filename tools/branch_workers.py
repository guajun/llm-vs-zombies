"""Run or verify parallel branch workers (N10 / issue #38).

``run`` starts one isolated worker process per branch of a plan
(``docs/并行分支worker.md``), keeps a failing branch local instead of aborting
its siblings, and writes ``<workspace>/report.json`` next to the per-branch
evidence. ``verify`` re-checks a finished workspace from disk without starting
anything. Neither command touches the scoring pipeline's cold-replay gates.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from llm_vs_zombies.audit_compare import EvidenceError
from llm_vs_zombies.branch_workers import Plan, run_branches, verify_workspace


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("run", help="start one worker per branch of a plan")
    start.add_argument("plan", type=Path, help="branch plan (schema lvz.branch-workers.v1)")
    start.add_argument("workspace", type=Path, help="fresh directory for the report and branch evidence")
    start.add_argument("--executor", help="module:function or path/to/executor.py:function "
                                          "(overrides the plan's executor)")
    start.add_argument("--max-workers", type=int, help="parallel branch workers (1..4, default from the plan)")
    check = commands.add_parser("verify", help="re-check a finished workspace from disk")
    check.add_argument("workspace", type=Path)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "verify":
            result = verify_workspace(arguments.workspace)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0 if result["verdict"]["status"] == "passed" else 1
        report, path = run_branches(Plan.load(arguments.plan), arguments.workspace,
                                    executor_spec=arguments.executor, max_workers=arguments.max_workers)
    except (EvidenceError, OSError, KeyError, TypeError, ValueError) as error:
        parser.exit(1, f"branch workers rejected: {error}\n")
    print(json.dumps({"report": str(path), "report_id": report["report_id"],
                      "verdict": report["verdict"]["status"],
                      "branches": report["verdict"]["branch_status"],
                      "comparisons": {item["name"]: item["status"] for item in report["comparisons"]},
                      "isolation": report["isolation"]["status"],
                      "max_workers": report["plan"]["max_workers"]}, ensure_ascii=False, sort_keys=True))
    return 0 if report["verdict"]["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
