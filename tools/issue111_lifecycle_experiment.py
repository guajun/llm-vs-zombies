"""Explicit lifecycle-recording experiment entry for issue #111.

This is the independent experiment path required by stage B: it prepares a
fresh run directory (through the existing public ``create_run``), records the
explicit ``LVZ_LIFECYCLE_RECORDING`` mode for that run, can launch the existing
evaluation entry with that environment, and seals the result by reusing the
existing evidence codec plus the lifecycle validator. No Agent service,
training package or online consumer is imported or required.

``prepare`` never starts a game. ``run`` sets the mode environment variable and
invokes the existing evaluation command (which does start the game); it is not
executed by the repository tests. ``check``/``seal`` are pure offline reads of
an already-recorded run directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from llm_vs_zombies import evidence_codec, lifecycle_events  # noqa: E402

MODE_SCHEMA = "lvz.lifecycle-experiment.v1"
REPORT_SCHEMA = "lvz.lifecycle-experiment-report.v1"
MODE_ENV = "LVZ_LIFECYCLE_RECORDING"
MODE_VALUES = {"off": "0", "on": "1"}
_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


class ExperimentError(ValueError):
    """The experiment request or run directory is invalid."""


def run_directory(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ExperimentError("run name must contain only letters, digits, '-' or '_'")
    return Path(root) / "experiments" / "runs" / name


def mode_path(run: Path) -> Path:
    return Path(run) / "lifecycle-mode.json"


def read_mode(run: Path) -> dict:
    path = mode_path(run)
    if not path.is_file():
        raise ExperimentError(f"run is not prepared for lifecycle recording: {path}")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExperimentError(f"lifecycle mode file unreadable: {exc}") from exc
    if value.get("schema") != MODE_SCHEMA or value.get("mode") not in MODE_VALUES:
        raise ExperimentError("unsupported lifecycle mode file")
    if value.get("env", {}).get("name") != MODE_ENV or value.get("env", {}).get("value") != MODE_VALUES[value["mode"]]:
        raise ExperimentError("lifecycle mode file does not bind the explicit environment switch")
    return value


def launch_command(root: Path, name: str, plan: Path) -> list[str]:
    return [sys.executable, "-m", "llm_vs_zombies.evaluation", "run", str(plan),
            "--output", str(run_directory(root, name)), "--skip-build"]


def prepare(root: Path, name: str, plan: Path, mode: str, *, create_run=None) -> dict:
    """Create a fresh run and bind the explicit lifecycle mode; never launches."""
    root = Path(root).resolve()
    if mode not in MODE_VALUES:
        raise ExperimentError("mode must be off or on")
    plan = Path(plan).resolve()
    if not plan.is_file():
        raise ExperimentError(f"evaluation plan does not exist: {plan}")
    try:
        frozen = json.loads(plan.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExperimentError(f"evaluation plan unreadable: {exc}") from exc
    if frozen.get("schema") != "lvz.evaluation-plan.v2":
        raise ExperimentError("evaluation plan must be lvz.evaluation-plan.v2")
    scenario = frozen.get("scenario")
    config = root / "experiments" / "configs" / f"{scenario}.json"
    if not config.is_file():
        raise ExperimentError(f"scenario config does not exist: {config}")
    run = run_directory(root, name)
    if run.exists():
        raise ExperimentError(f"run directory already exists; create a fresh run: {run}")
    if create_run is None:
        from llm_vs_zombies.cli import create_run as create_run_function
        create_run = create_run_function
    created = Path(create_run(root, config, name))
    if created.resolve() != run.resolve():
        raise ExperimentError("create_run returned an unexpected directory")
    mode_value = {
        "schema": MODE_SCHEMA,
        "run": name,
        "mode": mode,
        "env": {"name": MODE_ENV, "value": MODE_VALUES[mode]},
        "plan": {"path": str(plan.relative_to(root)) if plan.is_relative_to(root) else str(plan),
                 "sha256": hashlib.sha256(plan.read_bytes()).hexdigest()},
        "launch_command": launch_command(root, name, plan),
    }
    mode_path(run).write_text(json.dumps(mode_value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return mode_value


def run_experiment(root: Path, name: str, plan: Path, mode: str, *, runner=None) -> int:
    """Invoke the existing evaluation entry with the explicit mode environment."""
    root = Path(root).resolve()
    recorded = read_mode(run_directory(root, name))
    if recorded.get("mode") != mode:
        raise ExperimentError(f"run was prepared for mode {recorded.get('mode')!r}, not {mode!r}")
    command = launch_command(root, name, Path(plan).resolve())
    previous = os.environ.get(MODE_ENV)
    os.environ[MODE_ENV] = MODE_VALUES[mode]
    try:
        if runner is None:
            return subprocess.run(command, check=False).returncode
        return int(runner(command))
    finally:
        if previous is None:
            os.environ.pop(MODE_ENV, None)
        else:
            os.environ[MODE_ENV] = previous


def check(root: Path, name: str) -> dict:
    root = Path(root).resolve()
    run = run_directory(root, name)
    recorded = read_mode(run)
    audit = run / "audit"
    if recorded["mode"] == "on":
        try:
            report = lifecycle_events.validate(audit, require_close=True)
        except lifecycle_events.LifecycleError as exc:
            return {"schema": REPORT_SCHEMA, "ok": False, "run": name, "mode": recorded["mode"],
                    "problems": [str(exc)]}
        return {"schema": REPORT_SCHEMA, "ok": report["status"] == "valid", "run": name,
                "mode": recorded["mode"], "status": report["status"], "records": report["records"]["count"],
                "problems": report["problems"]}
    return {"schema": REPORT_SCHEMA, "ok": True, "run": name, "mode": recorded["mode"],
            "status": "disabled", "records": None, "problems": []}


def seal(root: Path, name: str) -> dict:
    """Validate and seal one run with the existing evidence codec."""
    root = Path(root).resolve()
    run = run_directory(root, name)
    recorded = read_mode(run)
    audit = run / "audit"
    report = {"schema": REPORT_SCHEMA, "run": name, "mode": recorded["mode"], "plan": recorded.get("plan"),
              "lifecycle": None, "codec_receipt": None, "problems": []}
    if recorded["mode"] == "on":
        try:
            validation = lifecycle_events.validate(audit, require_close=True)
        except lifecycle_events.LifecycleError as exc:
            raise ExperimentError(f"lifecycle evidence unreadable: {exc}") from exc
        if validation["status"] != "valid":
            raise ExperimentError("lifecycle evidence is not valid: " + "; ".join(validation["problems"]))
        report["lifecycle"] = {"status": validation["status"], "records": validation["records"]["count"],
                               "session_id": validation["capability"]["session_id"],
                               "receipt_present": validation["close_receipt"]["present"]}
    else:
        # An explicitly disabled run must not contain lifecycle evidence.
        for name_ in (lifecycle_events.EVENTS_FILE, lifecycle_events.RECEIPT_FILE):
            if (audit / name_).is_file() or (audit / (name_ + ".gz")).is_file():
                raise ExperimentError(f"disabled run unexpectedly contains {name_}")
    receipt_path = audit / evidence_codec.RECEIPT
    if receipt_path.is_file():
        codec_receipt = json.loads(receipt_path.read_bytes())
    else:
        codec_receipt = evidence_codec.compress_evidence(audit)
    if recorded["mode"] == "on":
        after = lifecycle_events.validate(audit, require_close=True)
        if after["status"] != "valid":
            raise ExperimentError("sealed lifecycle evidence is not valid: " + "; ".join(after["problems"]))
        report["lifecycle"]["sealed_status"] = after["status"]
    run_manifest = run / "manifest.json"
    if run_manifest.is_file():
        manifest = json.loads(run_manifest.read_bytes())
        report["build"] = manifest.get("implementation")
    report["codec_receipt"] = {"path": evidence_codec.RECEIPT,
                               "sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
                               "files": sorted(codec_receipt.get("files", {}))}
    report["ok"] = True
    (run / "lifecycle-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                               encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Explicit #111 lifecycle-recording experiment entry")
    parser.add_argument("--root", type=Path, default=ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare", help="create a fresh run and bind the lifecycle mode (no launch)")
    prepare_parser.add_argument("--name", required=True)
    prepare_parser.add_argument("--plan", type=Path, required=True)
    prepare_parser.add_argument("--mode", choices=sorted(MODE_VALUES), required=True)
    run_parser = sub.add_parser("run", help="launch the existing evaluation entry with the mode environment")
    run_parser.add_argument("--name", required=True)
    run_parser.add_argument("--plan", type=Path, required=True)
    run_parser.add_argument("--mode", choices=sorted(MODE_VALUES), required=True)
    check_parser = sub.add_parser("check", help="offline lifecycle check of a recorded run")
    check_parser.add_argument("--run", required=True)
    seal_parser = sub.add_parser("seal", help="validate and seal a recorded run with the evidence codec")
    seal_parser.add_argument("--run", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            report = prepare(args.root, args.name, args.plan, args.mode)
        elif args.command == "run":
            return run_experiment(args.root, args.name, args.plan, args.mode)
        elif args.command == "check":
            report = check(args.root, args.run)
        else:
            report = seal(args.root, args.run)
    except ExperimentError as exc:
        print(json.dumps({"schema": REPORT_SCHEMA, "ok": False, "problems": [str(exc)]},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
