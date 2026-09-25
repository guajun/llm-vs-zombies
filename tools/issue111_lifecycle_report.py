"""Single offline boundary report for issue #111 stage C.

Usage:
    python tools/issue111_lifecycle_report.py <run-or-audit-dir> [--out report.json] [--markdown report.md]

Reads only already-recorded audit evidence through the strict reader (plus the
lifecycle recording when present) and prints the first confirmed death-stage
observation, first removal observation, unknown removals, and whether a
full-window first kill is proven, each with audit coordinates and coverage
limits. Death/removal/recycle capture points are still review_required, so the
capture-level verdict is always unavailable and first_kill.proven is false.

Exit codes: 0 = report produced, 1 = report has evidence problems, 2 = unreadable input.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from llm_vs_zombies import lifecycle_report  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline #111 boundary lifecycle report")
    parser.add_argument("path", help="experiment run directory or audit directory")
    parser.add_argument("--out", type=Path, default=None, help="write the JSON report here")
    parser.add_argument("--markdown", type=Path, default=None, help="write a Markdown summary here")
    parser.add_argument("--plan", type=Path, default=None,
                        help="frozen evaluation plan used to bind the full-window coverage")
    parser.add_argument("--plan-binding", type=Path, default=None,
                        help="explicit raw plan identity binding (defaults to the suite copy)")
    args = parser.parse_args(argv)
    try:
        report = lifecycle_report.report_for_run(args.path, plan=args.plan, plan_binding=args.plan_binding)
    except lifecycle_report.ReportError as exc:
        print(json.dumps({"schema": lifecycle_report.ANALYSIS_SCHEMA, "ok": False,
                          "problems": [str(exc)]}, ensure_ascii=False, indent=2))
        return 2
    report["ok"] = not report.get("problems")
    if args.out is not None:
        lifecycle_report.write_report(report, json_path=args.out)
    if args.markdown is not None:
        args.markdown.write_text(lifecycle_report.markdown_report(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
