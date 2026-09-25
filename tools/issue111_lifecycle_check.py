#!/usr/bin/env python
"""Single offline check for the #111 lifecycle recording evidence.

Usage:
    python tools/issue111_lifecycle_check.py <run-or-audit-dir> [--recorder PATH] [--json]

Accepts either an experiment run directory (``<run>/audit`` is used when
present) or the audit directory itself. Never starts the game, loads the
runtime or reads a sealed archive's game data: it validates
``lifecycle-events.jsonl`` against ``lifecycle-close-receipt.jsonl`` and the
audit manifest capability.

Exit codes: 0 for valid/unavailable/disabled/open, 1 for a failed validation,
2 for an unreadable contract. ``--json`` prints only the machine-readable
report.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from llm_vs_zombies import lifecycle_events  # noqa: E402


def resolve_audit(path: Path) -> Path:
    path = Path(path)
    if (path / "audit" / "manifest.json").is_file():
        return path / "audit"
    if (path / "manifest.json").is_file():
        return path
    raise lifecycle_events.LifecycleError(f"no audit manifest under {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate #111 lifecycle recording evidence offline")
    parser.add_argument("path", help="experiment run directory or audit directory")
    parser.add_argument("--recorder", type=Path, default=None,
                        help="expected recorder module whose SHA-256 must match the declared build")
    parser.add_argument("--json", action="store_true", help="print only the JSON report")
    args = parser.parse_args(argv)
    try:
        audit = resolve_audit(args.path)
        report = lifecycle_events.validate(audit, recorder=args.recorder)
    except lifecycle_events.LifecycleError as exc:
        if not args.json:
            print(f"lifecycle evidence contract error: {exc}", file=sys.stderr)
        else:
            print(json.dumps({"schema": lifecycle_events.VALIDATION_SCHEMA,
                              "status": "invalid", "problems": [str(exc)]}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not args.json:
        counts = report["records"]["count"]
        print(f"# lifecycle: {report['status']} records={counts} "
              f"first_kill_proven={report['claims']['first_kill_proven']}", file=sys.stderr)
    if report["status"] == "failed":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
