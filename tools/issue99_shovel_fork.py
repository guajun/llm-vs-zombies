"""Issue #99: one entry for the controlled shovel experiment, its report and its tree.

The entry has four subcommands:

* ``run``     - drive the four declared trajectories (control, control rerun,
  intervention, intervention rerun) through the public evaluation entry, then
  compare them and package the tree;
* ``compare`` - read-only comparison of four already sealed runs;
* ``tree``    - read-only packaging of the four runs into one evidence tree;
* ``read``    - the read-only loader: open a packaged tree without AvZ, without a
  game process and without the launcher, and print what it can re-derive.

Nothing here starts a game by itself: ``run`` calls the same public command a
human would (``python -m llm_vs_zombies.evaluation run ...``) and the other
three subcommands only read sealed evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from compare_worlds import EvidenceStore, _group, all_differences, iter_states, read_rows  # read-only readers
from llm_vs_zombies.evidence_tree import (EvidenceTree, attach_tree, branch_placement, end_boundary,
                                          package_tree, root_identity, root_placement, validate_tree)

SCHEMA = "lvz.issue99-shovel-fork.v1"
ROLES = ("control", "control-rerun", "intervention", "intervention-rerun")
# Component digests of one boundary, grouped by what a difference means. The
# groups are reported separately on purpose: the precheck showed this action
# changes the random state and the sound history, so "the trajectories differ"
# must never stand in for "the gameplay differs".
GROUPS = {
    "rng": ("rng",),
    "audio": ("sound_effects",),
    "diagnostic": ("fp_environment", "particle_shake", "reanimations", "draw_schedule", "hosted_fire", "schema"),
    "simulation": ("board", "plants", "zombies", "projectiles", "mowers", "grid_items", "coins",
                   "challenge", "seeds", "app"),
}


def read_json(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def boundaries(run) -> dict:
    """One entry per audited boundary tick: the latest pre-step state digest.

    The declared action bumps the revision inside the tick it runs in, so the
    key is the tick and the revision is reported next to it instead of being
    folded into the key.
    """
    store = EvidenceStore(Path(run) / "audit")
    table: dict = {}
    for row in read_rows(store, "checksums.jsonl"):
        if row.get("kind") != "pre_step":
            continue
        version = row["version"]
        entry = table.setdefault(version["tick"],
                                 {"epoch": version["epoch"], "revisions": [], "digests": row["digests"]})
        entry["revisions"].append(version["revision"])
        entry["digests"] = row["digests"]
    return table


def compare_pair(left, right, *, left_name: str, right_name: str, limit: int = 8) -> dict:
    """Tick-aligned comparison of two sealed runs, grouped by what differs."""
    left_table, right_table = boundaries(left), boundaries(right)
    common_prefix, first_difference = 0, None
    first_by_group: dict = {}
    samples: list = []
    for tick in sorted(set(left_table) | set(right_table)):
        left_entry, right_entry = left_table.get(tick), right_table.get(tick)
        if left_entry is None or right_entry is None:
            difference = {"tick": tick,
                          "reason": f"boundary missing in {right_name if left_entry is None else left_name}",
                          "present": {"a": left_entry is not None, "b": right_entry is not None}}
        else:
            changed = sorted(name for name, digest in left_entry["digests"].items()
                             if right_entry["digests"].get(name) != digest)
            revision = {"a": left_entry["revisions"], "b": right_entry["revisions"]}
            if not changed and left_entry["epoch"] == right_entry["epoch"] and revision["a"] == revision["b"]:
                difference = None
            else:
                difference = {"tick": tick, "epoch": {"a": left_entry["epoch"], "b": right_entry["epoch"]},
                              "revision": revision, "components": changed,
                              "groups": sorted({group for group, names in GROUPS.items()
                                                if set(names) & set(changed)}),
                              "all_equal": left_entry["digests"].get("all") == right_entry["digests"].get("all")}
        if difference is None:
            if first_difference is None:
                common_prefix += 1
            continue
        if first_difference is None:
            first_difference = difference
        for group in difference.get("groups") or []:
            first_by_group.setdefault(group, {"tick": tick, "components": difference.get("components")})
        if len(samples) < limit:
            samples.append(difference)
    return {"pair": [left_name, right_name], "boundaries": {"a": len(left_table), "b": len(right_table)},
            "common_prefix_ticks": common_prefix, "first_difference": first_difference,
            "first_difference_by_group": first_by_group, "samples": samples,
            "identical": first_difference is None}


def first_removal(run) -> dict | None:
    """First zombie slot that goes from occupied to free, with what it last was.

    This is an observation, not a damage metric: the audit carries no separate
    "killed" flag, so the report names the evidence it actually read. A slot
    only counts after it was occupied at two consecutive boundaries: the state
    left behind by the cold start lists one stale occupied slot that the first
    frame retires, and calling that "the first zombie removed" would be wrong.
    """
    store = EvidenceStore(Path(run) / "audit")
    occupied: dict = {}
    removals = []
    for item in iter_states(store):
        state = item["state"]
        version = item["row"].get("version") or {}
        slots = (state.get("zombies") or {}).get("slots") or {}
        for index, entry in slots.items():
            fields = entry.get("fields") if isinstance(entry, dict) else None
            if not fields:
                continue
            previous = occupied.get(index)
            occupied[index] = {"type": fields.get("00000024"), "x_bits": fields.get("0000002c"),
                               "tick": version.get("tick"),
                               "observations": (previous["observations"] + 1) if previous else 1}
        for index in [name for name in occupied if not (slots.get(name) or {}).get("fields")]:
            previous = occupied.pop(index)
            if previous["observations"] < 2:
                continue
            removals.append({"tick": version.get("tick"), "slot": index, "type": previous["type"],
                             "x_bits": previous["x_bits"], "last_occupied_tick": previous["tick"]})
    if not removals:
        return None
    return {"count": len(removals), "first": removals[0], "last": removals[-1],
            "note": "slot left the zombie array after being occupied at two or more boundaries; "
                    "the audit has no separate kill flag, so this is not a kill count"}


def state_at_ticks(run, ticks) -> dict:
    """Expand the state stream once and keep the last state of the wanted ticks.

    The audit writes a state row per boundary coordinate; a tick can hold more
    than one (the declared action adds a revision inside its own tick), so the
    last row of a tick is the one the request boundary observes.
    """
    wanted, found = set(ticks), {}
    for item in iter_states(EvidenceStore(Path(run) / "audit")):
        version = item["row"].get("version") or {}
        tick = version.get("tick")
        if tick in wanted:
            found[tick] = item["state"]
    return found


def pointer_differences(left, right, ticks, *, limit: int = 24) -> dict:
    """Which canonical state pointers differ at specific ticks, and in what group."""
    left_states = state_at_ticks(left, ticks)
    right_states = state_at_ticks(right, ticks)
    out = {}
    for tick in ticks:
        a, b = left_states.get(tick), right_states.get(tick)
        if a is None or b is None:
            out[str(tick)] = {"available": False}
            continue
        differences = list(all_differences(a, b))
        out[str(tick)] = {"available": True, "count": len(differences),
                          "groups": sorted({_group(item["path"]) for item in differences}),
                          "pointers": [{"path": item["path"], "a": item["a"], "b": item["b"]}
                                       for item in sorted(differences, key=lambda value: value["path"])[:limit]]}
    return out


def run_summary(run, suite=None) -> dict:
    """What a sealed run says about itself, without re-deriving its evidence."""
    run = Path(run)
    manifest = read_json(run / "manifest.json")
    trajectory = read_json(run / "trajectory/trajectory.json")
    ending = read_json(run / "experiment-end.json") if (run / "experiment-end.json").is_file() else {}
    replay = None
    if suite is not None:
        report_path = Path(suite) / "seed-42-replay-1/replay-report.json"
        if report_path.is_file():
            full = read_json(report_path)
            replay = {key: full.get(key) for key in
                      ("equal", "mode", "original_engine_replay_verified", "requests", "engine_calls",
                       "draw_schedule", "sound_effects", "branch_scope")}
            replay["report"] = str(report_path)
    fires, actions = [], []
    for row in read_rows(EvidenceStore(run / "audit"), "events.jsonl"):
        if row.get("kind") == "hosted_fire":
            fires.append(row)
        elif row.get("kind") == "action":
            actions.append(row)
    return {"path": str(run), "branch_id": manifest.get("branch"),
            "recorder_sha256": (manifest.get("implementation") or {}).get("recorder_sha256"),
            "trajectory_id": trajectory.get("trajectory_id"),
            "outcome": ending.get("outcome"), "decisions": ending.get("decisions"),
            "final_version": (ending.get("final_observation") or {}).get("version"),
            "maximum_wave": ending.get("maximum_wave"), "intervention": ending.get("intervention"),
            "hosted_fire_count": len(fires),
            "first_hosted_fire_tick": (fires[0].get("payload", {}).get("tick") if fires else None),
            "cold_replay": replay,
            "action_events": len(actions), "first_removal": first_removal(run),
            "identity": (trajectory.get("initial") or {}).get("identity"),
            "initial_version": ((trajectory.get("initial") or {}).get("observation") or {}).get("version"),
            "manifest_sha256": sha256_file(run / "manifest.json")}


def run_plan(suite) -> dict:
    """The plan a suite wrote, i.e. what its four runs were told to do."""
    return read_json(Path(suite) / "plan.json")


def run_paths(suites: dict) -> dict:
    """The source run of each suite: a sibling directory, not a child of it."""
    return {role: Path(suite).parent / f"{Path(suite).name}-s42-c0" for role, suite in suites.items()}


def plan_differences(plans: dict) -> dict:
    """Which plan fields differ from the control, field by field.

    The two branches of this experiment must share every declaration except the
    intervention action; the report prints the actual difference set instead of
    asserting it.
    """
    base = plans["control"]
    differences = {}
    for role in ROLES[1:]:
        other = plans[role]
        keys = sorted(set(base) | set(other))
        differences[role] = sorted(key for key in keys
                                   if json.dumps(base.get(key), sort_keys=True)
                                   != json.dumps(other.get(key), sort_keys=True))
    return differences


def compare_suites(suites: dict, *, limit: int = 8) -> dict:
    """The whole comparison: four run summaries plus three tick-aligned pairs."""
    runs, plans = run_paths(suites), {role: run_plan(suite) for role, suite in suites.items()}
    report = {"schema": SCHEMA, "plans": plans, "plan_differences": plan_differences(plans),
              "runs": {role: run_summary(runs[role], suites[role]) for role in ROLES}, "pairs": {}}
    report["pairs"]["control-rerun"] = compare_pair(runs["control"], runs["control-rerun"],
                                                    left_name="control", right_name="control-rerun", limit=limit)
    report["pairs"]["intervention-rerun"] = compare_pair(runs["intervention"], runs["intervention-rerun"],
                                                         left_name="intervention", right_name="intervention-rerun",
                                                         limit=limit)
    report["pairs"]["control-vs-intervention"] = compare_pair(runs["control"], runs["intervention"],
                                                              left_name="control", right_name="intervention",
                                                              limit=limit)
    report["verdict"] = verdict(report)
    return report


def verdict(report: dict) -> dict:
    """What this evidence does and does not demonstrate, in one block.

    The experiment is a capability probe: the branches must each reproduce, the
    intervention must sit exactly where the plan declared it, and a gameplay
    difference is only claimed when a simulation component actually diverges.
    """
    cross = report["pairs"]["control-vs-intervention"]
    by_group = cross["first_difference_by_group"]
    intervention = report["runs"]["intervention"]["intervention"] or {}
    summary = intervention.get("summary") or {}
    reruns_reproduce = (report["pairs"]["control-rerun"]["identical"]
                        and report["pairs"]["intervention-rerun"]["identical"])
    action_ok = summary.get("ok") is True and not summary.get("plants_changed")
    return {"branches_reproduce": bool(reruns_reproduce),
            "intervention_fired_as_declared": bool(intervention.get("fired"))
            and intervention.get("fired_at") == intervention.get("declared_at"),
            "intervention_action_verified": bool(action_ok),
            "shared_prefix_ticks": cross["common_prefix_ticks"],
            "first_rng_difference": by_group.get("rng"),
            "first_audio_difference": by_group.get("audio"),
            "first_simulation_difference": by_group.get("simulation"),
            "random_state_fork_demonstrated": "rng" in by_group,
            "gameplay_fork_demonstrated": "simulation" in by_group,
            "scope": "one seed, one root, one scenario, one build; cold-start branches, not process forks"}


def initial_version(run) -> dict:
    return read_json(Path(run) / "trajectory/trajectory.json")["initial"]["observation"]["version"]


def build_tree(suites: dict, output, *, roles: tuple = ROLES) -> dict:
    """Package the four sealed runs as one tree: root identity, parents, chains.

    Each node keeps its own recording and its own ``trajectory_id``; what the
    tree adds is *where* it sits. A rerun departs from its parent's cold-start
    boundary, and the intervention branch departs from the trunk at the
    boundary the plan declared. None of these four recordings is a process
    fork: the departure point is checked by the comparison report, not by the
    tree, and the tree says so through the boundaries it records.
    """
    runs = run_paths(suites)
    output = Path(output)
    root_manifest = read_json(runs["control"] / "trajectory/trajectory.json")
    identity = root_identity(game=root_manifest["initial"]["identity"]["game"],
                             artifacts=root_manifest["initial"]["identity"]["artifacts"],
                             init_recipe=root_manifest["initial"]["initialization"])
    declared = (run_plan(suites["intervention"]).get("intervention") or {}).get("at")
    if declared is None:
        raise SystemExit("the intervention plan declares no boundary")
    staging = Path(tempfile.mkdtemp(prefix="issue99-tree-staging-"))
    placed: dict = {}

    def stage(role, placement):
        target = staging / role
        shutil.copytree(runs[role] / "trajectory", target)
        manifest = read_json(target / "trajectory.json")
        manifest["tree"] = attach_tree(manifest, placement)
        (target / "trajectory.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                                encoding="utf-8", newline="\n")
        placed[role] = target

    stage("control", root_placement("issue99-control-a", identity))
    stage("control-rerun", branch_placement(placed["control"], "issue99-control-b", at=initial_version(runs["control"])))
    stage("intervention", branch_placement(placed["control"], "issue99-intervention-a", at=declared))
    stage("intervention-rerun", branch_placement(placed["intervention"], "issue99-intervention-b",
                                                 at=initial_version(runs["intervention"])))
    tree = package_tree(output, placed["control"],
                        branches=[placed["control-rerun"], placed["intervention"], placed["intervention-rerun"]])
    summary = tree.validate()
    shutil.rmtree(staging, ignore_errors=True)
    return summary


def read_tree(path) -> dict:
    """Load one packaged tree the way an offline consumer would: read-only."""
    tree = EvidenceTree.load(path)
    summary = tree.validate()
    nodes = []
    for node in tree.nodes:
        manifest = read_json(Path(path) / node["path"] / "trajectory.json")
        nodes.append({"key": node["key"], "branch_id": node["branch_id"], "trunk": node["trunk"],
                      "parent_key": node["parent_key"], "trajectory_id": node["trajectory_id"],
                      "steps": len(manifest.get("steps") or []),
                      "initial_version": manifest["initial"]["observation"]["version"],
                      "end_boundary": end_boundary(manifest),
                      "parent_boundary": (manifest["tree"]["parent"] or {}).get("boundary")})
    return {"tree_id": summary["tree_id"], "root": summary["root"], "nodes": nodes,
            "branches": summary["branches"], "trunk_nodes": summary["trunk_nodes"], "scope": summary["scope"]}


def boundary_text(point) -> str:
    if not point:
        return "—"
    return f"epoch {point['epoch']} / tick {point['tick']} / revision {point['revision']}"


def difference_text(difference) -> str:
    if not difference:
        return "无差异"
    if "components" not in difference:
        return f"tick {difference['tick']}：{difference['reason']}"
    components = ", ".join(difference["components"]) or "（只有版本坐标不同）"
    if "revision" in difference:
        return (f"tick {difference['tick']}：组件 {components}；"
                f"revision {difference['revision']['a']}→{difference['revision']['b']}")
    return f"tick {difference['tick']}：组件 {components}"


def markdown_report(report: dict) -> str:
    """A human-readable view of ``compare_suites`` output."""
    verdict = report["verdict"]
    lines = ["# 经典十二炮同根分叉：铲子干预与薄数据闭环（issue #99）", "",
             "本报告由 `tools/issue99_shovel_fork.py` 从四份已封存运行、树索引与复跑证据生成；"
             "不改写任何一条已封存记录。", "",
             "## 结论摘要", "",
             "| 结论 | 值 |", "|---|---|",
             f"| 两条分支各自可复现 | {'是' if verdict['branches_reproduce'] else '否'} |",
             f"| 干预按声明坐标执行 | {'是' if verdict['intervention_fired_as_declared'] else '否'} |",
             f"| 干预动作自检通过（无铲植物、光标归位） | {'是' if verdict['intervention_action_verified'] else '否'} |",
             f"| 干预前共同前缀 | {verdict['shared_prefix_ticks']} 个边界 |",
             f"| 随机状态分叉 | {'已定位：' + difference_text(verdict['first_rng_difference']) if verdict['random_state_fork_demonstrated'] else '未演示'} |",
             f"| 玩法差异 | {'已定位：' + difference_text(verdict['first_simulation_difference']) if verdict['gameplay_fork_demonstrated'] else '未演示'} |",
             f"| 适用范围 | {verdict['scope']} |", ""]
    lines += ["## 四条轨迹", "",
              "| 角色 | 运行 | 结局 | 结束边界 | 最大波次 | 托管炮击 | 僵尸槽位离场（非击杀计数） |",
              "|---|---|---|---|---|---|---|"]
    for role in ROLES:
        run = report["runs"][role]
        removal = run.get("first_removal") or {}
        removal_text = (f"{removal['count']} 个槽位，首个在 tick {removal['first']['tick']}"
                        if removal else "窗口内未观测到")
        lines.append(f"| {role} | `{Path(run['path']).name}` | {run.get('outcome')} | "
                     f"{boundary_text(run.get('final_version'))} | {run.get('maximum_wave')} | "
                     f"{run.get('hosted_fire_count')} 发（首次 tick {run.get('first_hosted_fire_tick')}）| {removal_text} |")
    lines += ["", "## 计划差异（对照 vs 其余角色）", ""]
    for role, keys in report["plan_differences"].items():
        if role == "control-rerun":
            continue
        lines.append(f"- `{role}` 与对照的差异字段：{', '.join(keys) if keys else '无'}")
    lines += ["", "## 逐对比较（按 tick 对齐，逐边界摘要）", "",
              "| 对 | 共同前缀 | 首个差异 | 首个随机差异 | 首个玩法差异 |", "|---|---|---|---|---|"]
    for name, pair in report["pairs"].items():
        groups = pair["first_difference_by_group"]
        lines.append(f"| {name} | {pair['common_prefix_ticks']} 个边界 | {difference_text(pair['first_difference'])} | "
                     f"{difference_text(groups.get('rng'))} | {difference_text(groups.get('simulation'))} |")
    lines += ["", "## 干预动作记录", ""]
    for role in ("intervention", "intervention-rerun"):
        record = report["runs"][role].get("intervention") or {}
        summary = record.get("summary") or {}
        lines += [f"### {role}", "",
                  f"- 声明边界：{boundary_text(record.get('declared_at'))}",
                  f"- 实际执行：{boundary_text(record.get('fired_at'))}（fired={record.get('fired')}）",
                  f"- 动作：`{json.dumps(record.get('action'), ensure_ascii=False)}`",
                  f"- 自检：ok={summary.get('ok')}，光标 {summary.get('cursor_type')}，"
                  f"RNG 实例 `{summary.get('rng_instances')}` 游标 {summary.get('rng_cursor')}，"
                  f"624 词全同={summary.get('rng_words_equal')}，游戏线程 CRT 不变={summary.get('game_thread_crt_equal')}",
                  f"- 音效副作用：{json.dumps(summary.get('sound_effects_changes'), ensure_ascii=False)}",
                  f"- 植物集合变化：{summary.get('plants_changed')}", ""]
    lines += ["## 本报告不证明", "",
              "- 真进程 fork、快照恢复或并行执行：四条轨迹都是冷启动录制。",
              "- 十次冷启动、两旗 strict 门槛或数据管理器（目录、发布、筛选、去重、划分、reward）。",
              "- 铲子干预只有 RNG 一条作用路径：音效历史与调用计数同时改变，报告如实列出。",
              "- 单根结论可推广到其它 seed、场景、音频模式或构建。", "",
              "## 复现命令", "",
              "```powershell",
              "$env:PYTHONPATH = 'src'",
              "python tools/issue99_shovel_fork.py run `",
              "  --control-plan experiments/plans/issue99-shovel-control.json `",
              "  --intervention-plan experiments/plans/issue99-shovel-intervene.json `",
              "  --prefix issue99-shovel --skip-build",
              "```", ""]
    return "\n".join(lines)


def write_reports(report: dict, *, json_path, markdown_path) -> None:
    Path(json_path).parent.mkdir(parents=True, exist_ok=True)
    Path(json_path).write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                               encoding="utf-8", newline="\n")
    Path(markdown_path).write_text(markdown_report(report), encoding="utf-8", newline="\n")


def seal(tree_directory, *, reports, nodes) -> dict:
    """Bind one packaged tree to the reports that explain it."""
    record = {"schema": "lvz.issue99-shovel-fork-seal.v1",
              "tree": str(Path(tree_directory)),
              "tree_id": nodes["tree_id"],
              "reports": [{"path": str(Path(path)), "sha256": sha256_file(path)} for path in reports],
              "nodes": [{"key": node["key"], "branch_id": node["branch_id"], "trunk": node["trunk"],
                         "parent_key": node["parent_key"], "trajectory_id": node["trajectory_id"],
                         "manifest_sha256": sha256_file(Path(tree_directory) / "nodes" / node["key"] / "trajectory.json")}
                        for node in nodes["nodes"]]}
    return record


def evaluate(plan, output, *, skip_build: bool, log_directory) -> None:
    """Run one suite through the public evaluation entry, exactly as documented.

    The entry exits 2 for "the suite ran but is not strict-ready", which is the
    expected result of a short-window smoke suite (and of ``--skip-build``), so
    the exit code alone cannot tell success from failure. What is checked here
    is the artifact: the suite must be sealed and the source run must carry a
    verified trajectory.
    """
    command = [sys.executable, "-m", "llm_vs_zombies.evaluation", "run", str(plan), "--output", str(output)]
    if skip_build:
        command.append("--skip-build")
    log_directory = Path(log_directory)
    log_directory.mkdir(parents=True, exist_ok=True)
    log = log_directory / f"{Path(output).name}.log"
    environment = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    print(f"$ {' '.join(command)}  (log: {log})", flush=True)
    with log.open("w", encoding="utf-8", newline="\n") as sink:
        finished = subprocess.run(command, cwd=ROOT, env=environment, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True)
        sink.write(finished.stdout)
    if finished.returncode not in (0, 2):
        raise SystemExit(f"{output}: evaluation entry failed with exit code {finished.returncode} (log: {log})")
    source = Path(f"{output}-s42-c0")
    manifest = source / "manifest.json"
    if not manifest.is_file() or not (source / "trajectory/trajectory.json").is_file():
        raise SystemExit(f"{output}: the suite did not seal a source run (log: {log})")
    record = read_json(manifest)
    if record.get("status") != "finalized":
        raise SystemExit(f"{output}: source run is not finalized (log: {log})")


def cmd_run(args) -> int:
    runs = Path(args.runs)
    # control/control-rerun -> -a/-b of the control plan; intervention likewise.
    suites = {"control": runs / f"{args.prefix}-control-a",
              "control-rerun": runs / f"{args.prefix}-control-b",
              "intervention": runs / f"{args.prefix}-intervention-a",
              "intervention-rerun": runs / f"{args.prefix}-intervention-b"}
    plans = {"control": Path(args.control_plan), "control-rerun": Path(args.control_plan),
             "intervention": Path(args.intervention_plan), "intervention-rerun": Path(args.intervention_plan)}
    for role in ROLES:
        if suites[role].exists():
            raise SystemExit(f"{suites[role]} already exists; this entry never overwrites a sealed suite")
    for role in ROLES:
        evaluate(plans[role], suites[role], skip_build=args.skip_build, log_directory=args.log_directory)
    report = compare_suites(suites, limit=args.limit)
    write_reports(report, json_path=args.report_json, markdown_path=args.report_markdown)
    build_tree(suites, args.tree_output)
    loaded = read_tree(args.tree_output)
    record = seal(args.tree_output, reports=[args.report_json, args.report_markdown], nodes=loaded)
    Path(args.seal).write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                               encoding="utf-8", newline="\n")
    print(json.dumps({"verdict": report["verdict"], "tree": loaded}, ensure_ascii=False, indent=2))
    return 0


def cmd_compare(args) -> int:
    suites = {"control": args.control, "control-rerun": args.control_rerun,
              "intervention": args.intervention, "intervention-rerun": args.intervention_rerun}
    report = compare_suites(suites, limit=args.limit)
    write_reports(report, json_path=args.report_json, markdown_path=args.report_markdown)
    print(json.dumps(report["verdict"], ensure_ascii=False, indent=2))
    return 0


def cmd_tree(args) -> int:
    suites = {"control": args.control, "control-rerun": args.control_rerun,
              "intervention": args.intervention, "intervention-rerun": args.intervention_rerun}
    build_tree(suites, args.output)
    loaded = read_tree(args.output)
    record = seal(args.output, reports=tuple(Path(path) for path in args.report), nodes=loaded)
    Path(args.seal).write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                               encoding="utf-8", newline="\n")
    print(json.dumps(loaded, ensure_ascii=False, indent=2))
    return 0


def cmd_read(args) -> int:
    print(json.dumps(read_tree(args.tree), ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_suite_arguments(sub):
        sub.add_argument("--control", type=Path, required=True, help="control suite directory")
        sub.add_argument("--control-rerun", type=Path, required=True, help="control rerun suite directory")
        sub.add_argument("--intervention", type=Path, required=True, help="intervention suite directory")
        sub.add_argument("--intervention-rerun", type=Path, required=True, help="intervention rerun suite directory")

    run = subparsers.add_parser("run", help="drive the four suites, then compare and package them")
    run.add_argument("--control-plan", type=Path, required=True)
    run.add_argument("--intervention-plan", type=Path, required=True)
    run.add_argument("--prefix", default="issue99-shovel")
    run.add_argument("--runs", type=Path, default=ROOT / "experiments/runs")
    run.add_argument("--tree-output", type=Path, default=ROOT / "experiments/trees/issue99-shovel-fork")
    run.add_argument("--report-json", type=Path, default=ROOT / "work/issue99-shovel-fork.json")
    run.add_argument("--report-markdown", type=Path, default=ROOT / "work/issue99-shovel-fork.md")
    run.add_argument("--seal", type=Path, default=ROOT / "work/issue99-shovel-fork-seal.json")
    run.add_argument("--log-directory", type=Path, default=ROOT / "work/issue99-logs")
    run.add_argument("--limit", type=int, default=8)
    run.add_argument("--skip-build", action="store_true", help="reuse the DLL the runs must bind")
    run.set_defaults(handler=cmd_run)

    compare = subparsers.add_parser("compare", help="compare four sealed runs read-only")
    add_suite_arguments(compare)
    compare.add_argument("--report-json", type=Path, required=True)
    compare.add_argument("--report-markdown", type=Path, required=True)
    compare.add_argument("--limit", type=int, default=8)
    compare.set_defaults(handler=cmd_compare)

    tree = subparsers.add_parser("tree", help="package four sealed runs into one evidence tree")
    add_suite_arguments(tree)
    tree.add_argument("--output", type=Path, required=True)
    tree.add_argument("--seal", type=Path, required=True)
    tree.add_argument("--report", type=Path, action="append", default=[])
    tree.set_defaults(handler=cmd_tree)

    read = subparsers.add_parser("read", help="read-only loader for a packaged tree")
    read.add_argument("--tree", type=Path, required=True)
    read.set_defaults(handler=cmd_read)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
