"""No-game consumer example for a #111 lifecycle recording.

Usage:
    python examples/issue111_lifecycle_consume.py <run-or-audit-dir> [--limit N]

Reads ``lifecycle-events.jsonl`` and its close receipt through the shared
:mod:`llm_vs_zombies.lifecycle_events` contract and prints a compact summary.
It never imports the native probe, the game, an Agent or training code, which
is the stage-C proof that a consumer does not have to reimplement capture.

Exit codes: 0 = valid/open, 1 = failed validation, 2 = unreadable input.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from llm_vs_zombies import evidence_codec, lifecycle_events  # noqa: E402

CONSUMER_SCHEMA = "lvz.lifecycle-consumer-example.v1"


def resolve_audit(path: Path) -> Path:
    if (path / "audit" / "manifest.json").is_file():
        return path / "audit"
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="No-game #111 lifecycle consumer example")
    parser.add_argument("path", type=Path)
    parser.add_argument("--limit", type=int, default=0, help="stop after N records (0 = all)")
    args = parser.parse_args(argv)
    audit = resolve_audit(args.path)
    try:
        report = lifecycle_events.validate(audit, require_close=False)
        store = evidence_codec.EvidenceStore(audit, error=lifecycle_events.LifecycleError)
        kinds: dict[str, int] = {}
        identities = set()
        first = last = None
        read = 0
        with store.open(lifecycle_events.EVENTS_FILE) as stream:
            for line in stream:
                if not line.strip():
                    continue
                record = json.loads(line)
                event = record.get("event") or {}
                kind = event.get("kind")
                kinds[kind] = kinds.get(kind, 0) + 1
                identities.add((record.get("run_id"), record.get("branch_id"), record.get("session_id")))
                sequence = event.get("capture_sequence")
                if isinstance(sequence, int):
                    first = sequence if first is None else first
                    last = sequence
                read += 1
                if args.limit and read >= args.limit:
                    break
        summary = {
            "schema": CONSUMER_SCHEMA,
            "status": report["status"],
            "records": read,
            "kinds": kinds,
            "identities": sorted(tuple(str(part) for part in identity) for identity in identities),
            "sequence_domain": lifecycle_events.SEQUENCE_DOMAIN,
            "first_capture_sequence": first,
            "last_capture_sequence": last,
            "close_receipt_present": report["close_receipt"]["present"],
            "first_kill_proven": report["claims"]["first_kill_proven"],
        }
    except (lifecycle_events.LifecycleError, OSError, UnicodeError, ValueError) as exc:
        print(json.dumps({"schema": CONSUMER_SCHEMA, "status": "invalid", "problems": [str(exc)]},
                         ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if report["status"] in ("valid", "open") else 1


if __name__ == "__main__":
    raise SystemExit(main())
