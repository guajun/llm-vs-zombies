from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


class EventWriter:
    """Buffered, append-only records; one writer per events file."""

    def __init__(self, run: Path):
        self.manifest = read_json(run / "manifest.json")
        self.stream = (run / "events.jsonl").open("x", encoding="utf-8", buffering=65536)
        self.seq = 0

    def emit(self, kind: str, segment: int, tick: int, payload: dict, phase="observation"):
        record = dict(schema_version=SCHEMA_VERSION, run_id=self.manifest["run_id"],
                      seq=self.seq, segment=segment, tick=tick, phase=phase, kind=kind, payload=payload)
        self.stream.write(canonical(record).decode("utf-8") + "\n")
        self.seq += 1

    def close(self):
        self.stream.flush()
        self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def events(run: Path) -> Iterator[dict]:
    manifest = read_json(run / "manifest.json")
    previous = (-1, -1)
    with (run / "events.jsonl").open(encoding="utf-8") as stream:
        for seq, line in enumerate(stream):
            if not line.endswith("\n"):
                raise ValueError(f"record {seq}: incomplete final line (capture may have crashed)")
            try:
                record = json.loads(line, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
            except (ValueError, TypeError) as exc:
                raise ValueError(f"record {seq}: invalid JSON") from exc
            required = {"schema_version", "run_id", "seq", "segment", "tick", "phase", "kind", "payload"}
            if not isinstance(record, dict) or not required <= record.keys():
                raise ValueError(f"record {seq}: missing envelope fields")
            if record["schema_version"] != SCHEMA_VERSION or record["run_id"] != manifest["run_id"]:
                raise ValueError(f"record {seq}: schema/run mismatch")
            if type(record["seq"]) is not int or record["seq"] != seq:
                raise ValueError(f"record {seq}: non-contiguous sequence")
            if any(type(record[k]) is not int or record[k] < 0 for k in ("segment", "tick")):
                raise ValueError(f"record {seq}: invalid segment/tick")
            key = (record["segment"], record["tick"])
            if key < previous:
                raise ValueError(f"record {seq}: time moved backwards without a new segment")
            if not isinstance(record["payload"], dict) or not isinstance(record["kind"], str):
                raise ValueError(f"record {seq}: invalid payload/kind")
            previous = key
            yield record


def validate(run: Path) -> dict:
    manifest = read_json(run / "manifest.json")
    count = states = 0
    last = None
    terminal = None
    for record in events(run):
        count += 1
        states += record["kind"] == "state"
        last = record
        terminal = record["kind"]
    if not count or not states:
        raise ValueError("recording has no state observations")
    for filename, digest in manifest.get("checksums", {}).items():
        candidate = (run / filename).resolve()
        if not candidate.is_relative_to(run.resolve()):
            raise ValueError("checksum path escapes run directory")
        if sha256(candidate) != digest:
            raise ValueError(f"checksum mismatch: {filename}")
    return dict(records=count, states=states, last_tick=last["tick"],
                status=manifest["status"], synthetic=manifest.get("synthetic", False),
                terminal_event=terminal)


def states(run: Path):
    return (record for record in events(run) if record["kind"] == "state")


def compare(left: Path, right: Path) -> dict:
    """Exact comparison of recorded fields, not proof of engine determinism."""
    from itertools import zip_longest
    validate(left)
    validate(right)
    count = 0
    for a, b in zip_longest(states(left), states(right)):
        if a is None or b is None:
            return dict(equal=False, compared=count, reason="different state counts")
        ka, kb = (a["segment"], a["tick"], a["phase"]), (b["segment"], b["tick"], b["phase"])
        if ka != kb:
            return dict(equal=False, compared=count, reason="different sample times", left=ka, right=kb)
        if canonical(a["payload"]) != canonical(b["payload"]):
            return dict(equal=False, compared=count, reason="recorded state diverged", at=ka)
        count += 1
    return dict(equal=True, compared=count, scope="recorded state fields only")


def finish(run: Path, outcome: str) -> dict:
    if (run / "capture.lock").exists():
        raise ValueError("native capture is active or crashed; do not finalize until capture is closed/recovered")
    report = validate(run)
    manifest = read_json(run / "manifest.json")
    if manifest["status"] != "recording":
        raise ValueError("run is already finalized")
    immutable_files = [run / "events.jsonl", run / "config.json"]
    for folder in ("inputs", "checkpoints", "video", "observations", "decisions"):
        if (run / folder).exists():
            immutable_files.extend(p for p in (run / folder).rglob("*") if p.is_file())
    manifest.update(status="finalized", finished_at=now(), outcome=outcome,
                    checksums={p.relative_to(run).as_posix(): sha256(p) for p in immutable_files})
    write_json(run / "manifest.json", manifest)
    write_json(run / "summary.json", {**report, "status": "finalized", "outcome": outcome})
    return manifest
