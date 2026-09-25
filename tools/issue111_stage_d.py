"""Offline checker and resource doctor for the issue #111 stage-D freeze plan.

``check`` proves the plan is internally consistent and still bound to the
frozen #99 endpoint/tick cap. ``doctor`` reports whether this checkout is
ready to run it (disk reserve, no game process, fresh output directories,
built recorder, optional local game files) without starting anything.

Exit codes: 0 = ok, 1 = problems, 2 = unreadable input.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "docs" / "issue111-阶段D计划.json"
SCHEMA = "lvz.issue111-stage-d-plan.v1"
MODE_VALUES = {"off": "0", "on": "1"}


class PlanError(ValueError):
    """The stage-D plan cannot be read."""


def load(path: Path = PLAN) -> dict:
    try:
        return json.loads(Path(path).read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PlanError(f"stage-D plan unreadable: {exc}") from exc


def check(doc: dict, root: Path) -> list[str]:
    problems: list[str] = []
    if not isinstance(doc, dict):
        return ["stage-D plan must be an object"]
    if doc.get("schema") != SCHEMA:
        problems.append(f"schema must be {SCHEMA!r}")
    if doc.get("status") != "frozen_pending_maintainer_review":
        problems.append("status must stay frozen_pending_maintainer_review until a maintainer accepts it")
    based_on = doc.get("based_on")
    if not isinstance(based_on, dict):
        problems.append("based_on must be an object")
        based_on = {}
    source = Path(root) / str(based_on.get("plan", ""))
    frozen: dict = {}
    if not source.is_file():
        problems.append(f"based_on.plan does not exist: {source}")
    else:
        try:
            frozen = json.loads(source.read_bytes())
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            problems.append(f"based_on.plan is unreadable: {exc}")
            frozen = {}
        if frozen.get("schema") != "lvz.evaluation-plan.v2":
            problems.append("based_on.plan is not lvz.evaluation-plan.v2")
        if frozen.get("tick_budget") != based_on.get("tick_budget"):
            problems.append("based_on.tick_budget no longer matches the frozen #99 plan")
        if frozen.get("stop_when") != based_on.get("stop_when"):
            problems.append("based_on.stop_when no longer matches the frozen #99 plan")
        if frozen.get("scenario") != based_on.get("scenario"):
            problems.append("based_on.scenario no longer matches the frozen #99 plan")
        if frozen.get("seeds") != [based_on.get("seed")]:
            problems.append("based_on.seed no longer matches the frozen #99 plan")
    limits = doc.get("resource_limits")
    if not isinstance(limits, dict):
        problems.append("resource_limits must be an object")
        limits = {}
    if limits.get("tick_cap") != based_on.get("tick_budget"):
        problems.append("resource_limits.tick_cap must equal the frozen #99 tick_budget")
    if limits.get("max_parallel_game_processes") != 1:
        problems.append("max_parallel_game_processes must be 1")
    for key in ("min_free_bytes", "timeout_seconds"):
        if type(limits.get(key)) is not int or limits[key] <= 0:
            problems.append(f"resource_limits.{key} must be a positive integer")

    switch = doc.get("mode_switch")
    if not isinstance(switch, dict) or switch.get("env_var") != "LVZ_LIFECYCLE_RECORDING":
        problems.append("mode_switch must declare the LVZ_LIFECYCLE_RECORDING env switch")
    modes = {mode.get("name"): mode.get("value") for mode in switch.get("modes", [])
             if isinstance(mode, dict)} if isinstance(switch, dict) else {}
    if modes != MODE_VALUES:
        problems.append("mode_switch.modes must define off=0 and on=1")
    if isinstance(switch, dict):
        not_gated = str(switch.get("not_gated", ""))
        if "ZombieInitialize" not in not_gated and "probe" not in not_gated:
            problems.append("mode_switch.not_gated must state that the probe is installed in both modes")
        if not isinstance(switch.get("limitations"), list) or not switch["limitations"]:
            problems.append("mode_switch.limitations must state what the persistence switch cannot test")

    mapping = doc.get("evaluation_child_mapping")
    if not isinstance(mapping, dict) or not isinstance(mapping.get("per_suite"), list) or not mapping["per_suite"]:
        problems.append("evaluation_child_mapping.per_suite must enumerate run_suite child directories")
    elif frozen.get("seeds"):
        expected = []
        for seed in frozen["seeds"]:
            for index in range(max(2, frozen.get("cold_starts", 1))):
                expected.append(f"<suite>-s{seed}-c{index}")
            expected.append(f"<suite>-s{seed}-recovery")
        if sorted(mapping["per_suite"]) != sorted(expected):
            problems.append("evaluation_child_mapping.per_suite does not match the frozen plan seeds/cold_starts")

    runs = doc.get("runs")
    if not isinstance(runs, list) or not runs:
        problems.append("runs must be a non-empty list")
        runs = []
    names: list[str] = []
    for index, run in enumerate(runs):
        if not isinstance(run, dict):
            problems.append(f"runs[{index}] must be an object")
            continue
        name = run.get("name")
        if not isinstance(name, str) or not name.startswith("issue111-d-"):
            problems.append(f"runs[{index}].name must start with issue111-d-")
        elif name in names:
            problems.append(f"duplicate run name: {name}")
        else:
            names.append(name)
        if run.get("mode") not in MODE_VALUES:
            problems.append(f"runs[{index}].mode must be off or on")
    comparisons = doc.get("comparisons")
    if not isinstance(comparisons, list) or not comparisons:
        problems.append("comparisons must be a non-empty list")
        comparisons = []
    for index, item in enumerate(comparisons):
        if not isinstance(item, dict):
            problems.append(f"comparisons[{index}] must be an object")
            continue
        for side in ("left", "right"):
            if item.get(side) not in names:
                problems.append(f"comparisons[{index}].{side} is not a declared run")
    comparison_names = {item.get("name") for item in comparisons if isinstance(item, dict)}
    if "persistence_adapter_non_perturbation" not in comparison_names:
        problems.append("comparisons must name persistence_adapter_non_perturbation")
    if any(isinstance(name, str) and "instrumentation_non_perturbation" in name for name in comparison_names):
        problems.append("comparisons must not claim instrumentation off/on from the persistence switch")
    if not isinstance(doc.get("verdicts"), list) or not doc["verdicts"]:
        problems.append("verdicts must be a non-empty list")
    commands = doc.get("commands")
    if not isinstance(commands, list) or not commands:
        problems.append("commands must be a non-empty list")
    else:
        for command in commands:
            if not isinstance(command, str) or "tools/issue111_" not in command:
                problems.append(f"command must invoke a repository issue111 tool: {command!r}")
    if not isinstance(doc.get("forbidden"), list) or not doc["forbidden"]:
        problems.append("forbidden must be a non-empty list")
    if not isinstance(doc.get("required_reviews"), list) or not doc["required_reviews"]:
        problems.append("required_reviews must be a non-empty list")
    return problems


def running_game_processes() -> list[str]:
    if sys.platform != "win32":
        return []
    try:
        output = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                                timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return ["tasklist_unavailable"]
    names = []
    for line in output.stdout.splitlines():
        if "plantsvszombies" in line.lower():
            names.append(line.split(",")[0].strip('"'))
    return names


def locked_game_files(root: Path) -> list[dict]:
    lock_path = Path(root) / "dependencies.lock.json"
    if not lock_path.is_file():
        return []
    lock = json.loads(lock_path.read_bytes())
    result = []
    for item in lock.get("files", []):
        if not isinstance(item, dict) or not str(item.get("path", "")).startswith("game/"):
            continue
        path = Path(root) / item["path"]
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        result.append({"path": item["path"], "present": path.is_file(),
                       "sha256_match": actual == item.get("sha256"),
                       "expected_sha256": item.get("sha256")})
    return result


def doctor(doc: dict, root: Path, *, disk_free: int | None = None,
           game_processes: list[str] | None = None, require_game: bool = True) -> dict:
    root = Path(root)
    problems = check(doc, root)
    report: dict = {"schema": SCHEMA, "root": str(root), "problems": problems}
    limits = doc.get("resource_limits") or {}
    free = shutil.disk_usage(root).free if disk_free is None else disk_free
    report["disk_free_bytes"] = free
    report["min_free_bytes"] = limits.get("min_free_bytes")
    if isinstance(limits.get("min_free_bytes"), int) and free < limits["min_free_bytes"]:
        problems.append(f"disk free {free} is below the plan reserve {limits['min_free_bytes']}")
    processes = running_game_processes() if game_processes is None else game_processes
    report["game_processes"] = processes
    if processes:
        problems.append(f"a game process is already running: {processes}")
    recorder = root / "build" / "recorder.dll"
    report["recorder"] = {"path": str(recorder), "present": recorder.is_file(),
                          "sha256": hashlib.sha256(recorder.read_bytes()).hexdigest() if recorder.is_file() else None}
    if not recorder.is_file():
        problems.append("build/recorder.dll is missing; run tools/build-avz.ps1 first")
    existing = []
    for run in doc.get("runs") or []:
        if isinstance(run, dict) and isinstance(run.get("name"), str):
            target = root / "experiments" / "runs" / run["name"]
            if target.exists():
                existing.append(str(target))
    report["existing_run_dirs"] = existing
    if existing:
        problems.append("stage-D run directories already exist; use fresh names: " + ", ".join(existing))
    files = locked_game_files(root)
    report["game_files"] = files
    if require_game:
        for item in files:
            if not item["present"]:
                problems.append(f"locked game file is missing: {item['path']}")
            elif not item["sha256_match"]:
                problems.append(f"locked game file changed: {item['path']}")
    report["ok"] = not problems
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the #111 stage-D freeze plan and local readiness")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--plan", type=Path, default=PLAN)
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    doctor_parser = sub.add_parser("doctor")
    doctor_parser.add_argument("--require-game", action="store_true",
                               help="fail when a locked game file is missing or changed")
    args = parser.parse_args(argv)
    try:
        doc = load(args.plan)
    except PlanError as exc:
        print(json.dumps({"schema": SCHEMA, "ok": False, "problems": [str(exc)]}, ensure_ascii=False, indent=2))
        return 2
    if args.command == "check":
        problems = check(doc, args.root)
        report = {"schema": SCHEMA, "ok": not problems, "problems": problems}
    else:
        report = doctor(doc, args.root, require_game=args.require_game)
        report["problems"] = report.pop("problems", [])
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
