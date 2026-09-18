from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1
ARCHIVE_DIRECTORIES = ("inputs", "checkpoints", "video", "observations", "decisions", "audit", "trajectory")
ARCHIVE_ROOT_FILES = ("events.jsonl", "config.json", "capture.closed", "summary.json", "evaluation.md", "README.md")


def archive_policy() -> dict:
    return {"schema": 1, "directories": list(ARCHIVE_DIRECTORIES),
            "root_files": list(ARCHIVE_ROOT_FILES), "root_patterns": ["*.json"],
            "excluded": ["manifest.json", "sandbox/**", "exports/**"],
            "inventory": "exact allowlisted file set; SHA256 of each file"}


def local_files(root: Path, directory: Path) -> Iterator[Path]:
    """Walk explicit evidence/source roots, rejecting symlinks and junctions."""
    for path in sorted(directory.iterdir()):
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError(f"linked evidence/source path is not allowed: {path}")
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"evidence/source path escapes root: {path}")
        if path.is_dir():
            yield from local_files(root, path)
        elif path.is_file():
            yield path


def _evidence_files(run: Path) -> list[Path]:
    files = []
    for path in run.iterdir():
        included = path.name in ARCHIVE_DIRECTORIES or path.name in ARCHIVE_ROOT_FILES or path.suffix == ".json"
        if not included or path.name == "manifest.json":
            continue
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError(f"linked evidence path is not allowed: {path}")
        if path.name in ARCHIVE_DIRECTORIES:
            if not path.is_dir():
                raise ValueError(f"evidence directory is not a directory: {path.name}")
            files.extend(local_files(run, path))
        elif path.is_file():
            files.append(path)
    return sorted(files)


def _assert_closed(run: Path, files: list[Path]) -> None:
    if (run / "capture.lock").exists():
        raise ValueError("native capture is active or crashed; do not finalize until capture is closed/recovered")
    locks = [p.relative_to(run).as_posix() for p in files if p.name.endswith(".lock")]
    if locks:
        raise ValueError(f"decision trace/evidence writer is active or crashed; inspect and close its lock: {locks[0]}")


def record_initial_state(run: Path, *, observation_path: Path, hello: dict,
                         scenario_verified: bool, audit_snapshot_path: Path | None = None) -> dict:
    """Bind saved live initialization evidence; never upgrade replay verification.

    Call after launcher saves its actual observation. The optional snapshot is a
    replay-initial marker or an audit_snapshot envelope from that same boundary.
    """
    manifest = read_json(run / "manifest.json")
    if manifest.get("status") != "recording":
        raise ValueError("cannot update initialization metadata of a finalized run")
    if type(scenario_verified) is not bool or not isinstance(hello, dict) or not isinstance(hello.get("capabilities"), dict):
        raise ValueError("live initialization requires hello capabilities and a scenario verification result")

    def evidence(path: Path) -> tuple[dict, dict]:
        if not path.is_absolute():
            path = run / path
        path = path.resolve()
        if not path.is_relative_to(run.resolve()) or path == (run / "manifest.json").resolve():
            raise ValueError("initial evidence must be a file inside the run")
        relative = path.relative_to(run.resolve())
        if not (relative.parts[0] in ARCHIVE_DIRECTORIES or len(relative.parts) == 1 and path.suffix == ".json"):
            raise ValueError("initial evidence must be inside the archive allowlist")
        value = read_json(path)
        if not isinstance(value, dict):
            raise ValueError("initial evidence must be a JSON object")
        return value, {"path": path.relative_to(run.resolve()).as_posix(), "sha256": sha256(path)}

    observation, observed_file = evidence(observation_path)
    version = observation.get("version")
    if not isinstance(version, dict) or any(type(version.get(k)) is not int or version[k] < 0
                                            for k in ("epoch", "tick", "revision")):
        raise ValueError("initial observation lacks a valid actual runtime version")
    initial = {"captured": True, "captured_at": now(), "observation": observed_file,
               "version": version, "scenario_verified": scenario_verified,
               "complete_game_state": False, "audit_snapshot_captured": False}
    if audit_snapshot_path is not None:
        snapshot, snapshot_file = evidence(audit_snapshot_path)
        captured_version = snapshot.get("version")
        if captured_version is None and isinstance(snapshot.get("observation"), dict):
            captured_version = snapshot["observation"].get("version")
        if (not isinstance(captured_version, dict)
                or any(type(captured_version.get(k)) is not int for k in ("epoch", "tick", "revision"))
                or captured_version != version or not isinstance(snapshot.get("state"), dict)):
            raise ValueError("initial audit snapshot is not from the observation boundary")
        initial.update(audit_snapshot_captured=True, audit_snapshot=snapshot_file)
    game = hello.get("game", {})
    if not isinstance(game, dict):
        raise ValueError("hello game identity must be an object")
    manifest["initial_state"] = initial
    manifest["capabilities"] = {"state_review": True, "runtime_reported": hello["capabilities"],
                                "audit_coverage": game.get("coverage"),
                                "target_reported": game,
                                "original_engine_replay_verified": False,
                                "source": "live hello; implementation claims, not experiment acceptance"}
    manifest["runtime_hello_sha256"] = hashlib.sha256(canonical(hello)).hexdigest()
    write_json(run / "manifest.json", manifest)
    return manifest


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
    if "archive_policy" in manifest:
        if manifest["archive_policy"] != archive_policy() or manifest.get("status") != "finalized":
            raise ValueError("unsupported archive policy or unfinalized seal")
        files = _evidence_files(run)
        _assert_closed(run, files)
        actual = {p.relative_to(run).as_posix() for p in files}
        expected = set(manifest.get("checksums", {}))
        if actual != expected:
            raise ValueError(f"archive inventory mismatch: missing={sorted(expected-actual)}, added={sorted(actual-expected)}")
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
        if not candidate.is_file() or sha256(candidate) != digest:
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
    manifest = read_json(run / "manifest.json")
    if manifest["status"] != "recording":
        raise ValueError("run is already finalized")
    if not all((run / name).is_file() for name in ("events.jsonl", "config.json")):
        raise ValueError("recording requires events.jsonl and config.json before finalization")
    _assert_closed(run, _evidence_files(run))
    report = validate(run)
    write_json(run / "summary.json", {**report, "status": "finalized", "outcome": outcome})
    immutable_files = _evidence_files(run)
    before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in immutable_files}
    checksums = {p.relative_to(run).as_posix(): sha256(p) for p in immutable_files}
    current_files = _evidence_files(run)
    _assert_closed(run, current_files)
    if current_files != immutable_files or any((p.stat().st_size, p.stat().st_mtime_ns) != before[p] for p in current_files):
        raise ValueError("evidence changed during finalization; close all writers before retrying")
    manifest.update(status="finalized", finished_at=now(), outcome=outcome,
                    archive_policy=archive_policy(), checksums=checksums)
    pending = run / "manifest.json.pending"
    write_json(pending, manifest)
    pending.replace(run / "manifest.json")
    return manifest
