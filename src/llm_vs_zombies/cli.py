from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .records import EventWriter, compare, events, finish, now, read_json, sha256, validate, write_json

ROOT = Path(__file__).resolve().parents[2]


def create_run(root: Path, config: Path, name: str | None = None, *, synthetic=False) -> Path:
    configuration = read_json(config)
    run_id = name or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    if not run_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in run_id):
        raise ValueError("run name must contain only letters, digits, '-' or '_'")
    run = root / "experiments" / "runs" / run_id
    run.mkdir(parents=True, exist_ok=False)
    for subdir in ("inputs", "checkpoints", "video", "observations", "decisions", "exports"):
        (run / subdir).mkdir()
    source = root / "dependencies.lock.json"
    dependencies = read_json(source) if source.exists() else {}
    candidate = (root / configuration.get("scenario_save", "__missing__")).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError("scenario_save must be inside the project")
    if candidate.is_file():
        shutil.copy2(candidate, run / "inputs/reference-save.dat")
    source_files = []
    for folder, suffixes in (("src", {".py"}), ("logger", {".cpp", ".hpp", ".json"}),
                             ("runtime", {".cpp", ".hpp", ".h", ".cmake", ".MIT"}),
                             ("determinism", {".cpp", ".hpp", ".h", ".json"}),
                             ("examples", {".py", ".json"}),
                             ("tools", {".py", ".ps1"}), ("replay", {".html", ".md"})):
        source_files.extend(p for p in (root / folder).rglob("*") if p.is_file() and p.suffix in suffixes)
    source_files.extend(root / p for p in ("CMakeLists.txt", "pyproject.toml", "dependencies.lock.json", "LICENSE", "docs/runtime-protocol.md") if (root / p).exists())
    with zipfile.ZipFile(run / "inputs/implementation.zip", "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for source_file in sorted(source_files):
            archive.write(source_file, source_file.relative_to(root).as_posix())
    dll = root / "build/recorder.dll"
    write_json(run / "config.json", configuration)
    write_json(run / "manifest.json", dict(schema_version=1, run_id=run_id,
        created_at=now(), status="recording", synthetic=synthetic, configuration=configuration,
        provenance=dependencies, capabilities=dict(state_review=True, game_input_replay=False,
        complete_rng_restore=False, exact_spawn_hook=False),
        implementation={"source_archive": "inputs/implementation.zip",
                        "recorder_sha256": sha256(dll) if dll.exists() else None},
        initial_state={"captured": False, "note": "Scenario file is a candidate; record the actual loaded state before evaluation."}))
    return run


def activate(root: Path, run: Path):
    manifest = read_json(run / "manifest.json")
    if manifest["status"] != "recording" or (run / "events.jsonl").exists():
        raise ValueError("activate only a new run; native capture refuses to append to an existing recording")
    (root / "build").mkdir(exist_ok=True)
    interval = int(manifest["configuration"].get("state_interval_ticks", 10))
    if not 1 <= interval <= 1000:
        raise ValueError("state_interval_ticks must be in 1..1000")
    # Native adapter config, adjacent to recorder.dll. First line is an absolute path.
    (root / "build" / "recorder.cfg").write_text(str(run.resolve()) + "\n" + str(interval) + "\n", encoding="utf-8")


def attach(run: Path, source: Path, category: str):
    if read_json(run / "manifest.json")["status"] != "recording":
        raise ValueError("cannot attach files to a finalized run")
    if not source.is_file():
        raise ValueError("attachment must be a file")
    target = run / category / source.name
    if target.exists():
        raise ValueError("attachment name already exists")
    shutil.copy2(source, target)
    return {"artifact": str(target), "sha256": sha256(target)}


def demo(root: Path, config: Path, name=None) -> Path:
    run = create_run(root, config, name, synthetic=True)
    with EventWriter(run) as writer:
        writer.emit("segment_start", 0, 0, {"source": "synthetic fixture; not a played PvZ match"})
        for tick in range(0, 1201, 10):
            plants = [dict(id=1, type=42, row=2, col=2, hp=max(1, 300-tick//10)),
                      dict(id=2, type=42, row=5, col=2, hp=300),
                      dict(id=3, type=42, row=3, col=6, hp=300),
                      dict(id=4, type=42, row=4, col=6, hp=300)]
            zombies = [dict(id=100, type=32, row=2, x=780-tick*0.43, y=165, hp=max(0, 6000-tick*4),
                            state=0, speed=0.43, freeze=0, slow=0)]
            if tick == 300:
                writer.emit("action", 0, tick, {"op": "plant", "type": 8, "row": 2, "col": 5, "success": True})
            writer.emit("state", 0, tick, dict(wave=1, sun=5000-tick//10, scene=3, plants=plants, zombies=zombies, seeds=[]))
        writer.emit("segment_end", 0, 1200, {"reason": "synthetic_complete"})
    finish(run, "synthetic_demo")
    export(root, run)
    return run


def export(root: Path, run: Path) -> Path:
    validate(run)
    manifest = read_json(run / "manifest.json")
    records = []
    for record in events(run):
        if len(records) >= 200000:
            raise ValueError("viewer export limit is 200,000 records; split the run before exporting")
        records.append(record)
    # Escape '<' so user-controlled text cannot terminate the script element.
    payload = json.dumps(dict(manifest=manifest, events=records), ensure_ascii=False).replace("<", "\\u003c")
    template = (root / "replay" / "viewer" / "template.html").read_text(encoding="utf-8")
    output = run / "exports" / "review.html"
    output.parent.mkdir(exist_ok=True)
    output.write_text(template.replace("__RECORDING_JSON__", payload), encoding="utf-8")
    return output


def doctor(root: Path):
    lock_path = root / "dependencies.lock.json"
    lock = read_json(lock_path) if lock_path.exists() else {}
    checks = {}
    for item in lock.get("files", []):
        path = root / item["path"]
        checks[item["path"]] = "ok" if path.is_file() and sha256(path) == item["sha256"] else "missing_or_changed"
    avz = root / "avz/framework"
    try:
        actual = subprocess.check_output(["git", "-C", str(avz), "rev-parse", "HEAD"], text=True).strip()
        checks["avz_commit"] = "ok" if actual == lock.get("avz_commit") else "changed"
    except (OSError, subprocess.CalledProcessError):
        checks["avz_commit"] = "unavailable"
    checks["python"] = sys.version.split()[0]
    checks["recorder_dll"] = "built" if (root / "build/recorder.dll").exists() else "not_built"
    checks["ffmpeg"] = shutil.which("ffmpeg") or "optional_not_found"
    checks["live_game_test"] = "not_performed"
    return checks


def main(argv=None):
    parser = argparse.ArgumentParser(description="LLM vs Zombies experiment tools")
    parser.add_argument("--root", type=Path, default=ROOT)
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("doctor")
    for command in ("new-run", "demo"):
        p = subs.add_parser(command)
        p.add_argument("--name")
        p.add_argument("--config", type=Path)
    for command in ("validate", "export", "activate", "finalize"):
        p = subs.add_parser(command)
        p.add_argument("run", type=Path)
        if command == "finalize":
            p.add_argument("--outcome", required=True)
    p = subs.add_parser("compare")
    p.add_argument("left", type=Path)
    p.add_argument("right", type=Path)
    p = subs.add_parser("attach")
    p.add_argument("run", type=Path)
    p.add_argument("source", type=Path)
    p.add_argument("--category", choices=("inputs", "observations", "decisions", "checkpoints", "video"), required=True)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "doctor":
            result = doctor(root)
        elif args.command in ("new-run", "demo"):
            config = args.config or root / "experiments/configs/liangyi.json"
            run = demo(root, config, args.name) if args.command == "demo" else create_run(root, config, args.name)
            if args.command == "new-run":
                activate(root, run)
            result = {"run": str(run)}
        elif args.command == "compare":
            result = compare(args.left, args.right)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["equal"] else 2
        elif args.command == "validate":
            result = validate(args.run)
        elif args.command == "export":
            result = {"html": str(export(root, args.run))}
        elif args.command == "finalize":
            finish(args.run, args.outcome)
            result = validate(args.run)
        elif args.command == "activate":
            activate(root, args.run)
            result = {"active": str(args.run.resolve())}
        elif args.command == "attach":
            result = attach(args.run, args.source, args.category)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
