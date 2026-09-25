"""Semantic lifecycle comparison of two run/audit directories.

Usage:
    python tools/issue111_lifecycle_compare.py <left-run-or-audit> <right-run-or-audit>

Normalizes only run_id/branch_id/session_id and compares every recorded fact
and receipt fact; never strips counters or sequence values. Exit codes:
0 = equal, 1 = different, 2 = unreadable input.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from llm_vs_zombies import lifecycle_compare  # noqa: E402


def resolve_audit(path: Path) -> Path:
    return path / "audit" if (path / "audit" / "manifest.json").is_file() else path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare two #111 lifecycle recordings semantically")
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--scope", choices=("lifecycle", "common", "both"), default="lifecycle",
                        help="lifecycle facts only (default), common gameplay evidence only, or both")
    args = parser.parse_args(argv)
    try:
        if args.scope == "lifecycle":
            report = lifecycle_compare.compare_lifecycle(resolve_audit(args.left), resolve_audit(args.right))
        else:
            report = lifecycle_compare.compare_scoped(resolve_audit(args.left), resolve_audit(args.right),
                                                      scope=args.scope)
    except lifecycle_compare.CompareError as exc:
        print(json.dumps({"schema": "lvz.lifecycle-compare.v1", "ok": False, "problems": [str(exc)]},
                         ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["equal"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
