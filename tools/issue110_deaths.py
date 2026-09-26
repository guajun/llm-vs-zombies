"""Read-only death-stage analysis of the four sealed issue #99 fc2 runs.

No launcher, client or game imports. Outputs are exclusive-create derivatives.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections import Counter
from itertools import zip_longest
from pathlib import Path

from compare_worlds import EvidenceStore, iter_states, read_rows
from issue99_shovel_fork import ROOT, read_json, read_tree, sha256_file, verify_seal

RULE = "lvz.issue110-death-stage.v2"
BASELINE = "346e495aaabd5ccb1b7d52ef75ee95c8b4f4b82a"
AVZ = "c42676c269b5b482a1eb9203a5b979e9d8a2a5c7"
TREE_ID = "ce770e531a225234b53ac3b810bc3209e4417b257410605f105bc38b0bc90220"
OLD_REPORTS = {
    "issue99-fc2-report.json": "05d83bc30ca952aa568b077a97b3bebe1994202ed7aaca0ec89a33705970c639",
    "issue99-fc2-report.md": "68495027150d4f296ed28bbfe67ea2aa4e357dd0675695b57a27974f8e4fd7e6",
}
RUNS = {"control-a": "root", "control-b": "issue99-control-b",
        "intervention-a": "issue99-intervention-a", "intervention-b": "issue99-intervention-b"}
FIELDS = {"type": "00000024", "state": "00000028", "hp": "000000c8",
          "armor1": "000000d0", "armor2": "000000dc", "disappeared": "000000ec",
          "at_wave": "0000006c"}
DEAD = {1: "falling", 2: "ash", 3: "mower_death_stage"}
# Codes cross-checked against the local, archived candidate ConstEnums.h.
# Names are navigation aids, not independent binary verification. The actual
# death predicate is the pinned AvZ IsDead(), which is false for these codes.
# Deliberately do not accept arbitrary integers as a known live predecessor.
KNOWN_NONDEAD = {0, 11, 15, 20, 69, 70, 71, 73, 76}
SEMANTIC_SOURCES = {
    "avz": {"commit": AVZ, "path": "inc/avz_pvz_struct.h", "lines": [714, 900],
            "local_sha256": "9501250bd1305ee81c524cbc22e1ad2ebb27cb80e94ded96a094ed98fb91a4cb"},
    "capture": {"commit": BASELINE, "path": "determinism/audit.cpp", "lines": [143, 230],
                "local_sha256": "35378247fbcdc6ed4c6a198e086c5addc8755d7b42b37d67caf653dcca43dc83"},
    "phase_names_secondary": {"repository": "ruslan831/PlantsVsZombies-decompilation",
        "declared_archive_commit": "8a2d121899ba5cb4df644cd7d2e4c1aaf88dd238",
        "path": "ConstEnums.h", "local_path": "work/replay-research/ConstEnums.h", "lines": [1216, 1314],
        "local_sha256": "2601042777012223c0a638f3265fc75f9c27451db50d35058623be92c531c175",
        "status": "candidate source only; not independent disassembly of the locked executable",
        "codes": {0: "normal", 11: "polevaulter_pre_vault", 15: "jack_running", 20: "pogo_bouncing",
                  69: "gargantuar_throwing", 70: "gargantuar_smashing", 71: "imp_getting_thrown",
                  73: "balloon_flying", 76: "ladder_carrying"}},
}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def signed(word):
    return word - 2**32 if word >= 2**31 else word


def coordinate(item):
    row = item["row"]
    call = row.get("payload", {}).get("engine_call", {})
    return {"line": item["index"] + 1, "seq": row.get("seq"), "kind": row.get("kind"),
            "version": row.get("version"), "engine_call_id": call.get("engine_call_id"),
            "native_clock_before": call.get("native_clock_before"),
            "native_clock_after": call.get("native_clock_after")}


def scan(items, initial, final):
    """Streaming state machine; copies only entity facts from in-place patches."""
    previous, retired, confirmed = {}, set(), set()
    deaths, entries, removals, uncertain, initial_objects, problems = [], [], [], [], [], []
    phases = Counter()
    last_coord = first_coord = None
    pending = None
    next_call = 1
    count = 0
    for item in items:
        coord = coordinate(item)
        row = item["row"]
        call = row.get("payload", {}).get("engine_call", {})
        if first_coord is None:
            first_coord = coord
            if coord["version"] != initial:
                problems.append({"reason": "initial_boundary_mismatch", "coordinate": coord})
        if type(coord["seq"]) is not int or (last_coord and
                (type(last_coord["seq"]) is not int or coord["seq"] <= last_coord["seq"])):
            problems.append({"reason": "non_increasing_seq", "coordinate": coord})
        if coord["kind"] == "pre_step":
            if pending is not None or coord["engine_call_id"] != next_call:
                problems.append({"reason": "missing_or_reordered_native_boundary", "coordinate": coord})
            pending = coord
        elif coord["kind"] == "post_step":
            if pending is None or pending["engine_call_id"] != coord["engine_call_id"]:
                problems.append({"reason": "unpaired_post", "coordinate": coord})
            elif (call.get("pre_version") != pending["version"] or
                  call.get("engine_call_completed") is not True or
                  call.get("board_identity_preserved") is not True or
                  call.get("native_tick_delta") != 1 or
                  coord["version"] != {"epoch": pending["version"]["epoch"],
                                       "tick": pending["version"]["tick"] + 1, "revision": 0}):
                problems.append({"reason": "native_transition_not_verified", "coordinate": coord})
            pending = None
            next_call = (coord["engine_call_id"] or 0) + 1
        else:
            problems.append({"reason": "unexpected_boundary_kind", "coordinate": coord})
        if last_coord and coord["kind"] == "pre_step":
            a, b = last_coord["version"], coord["version"]
            if b["epoch"] != a["epoch"] or b["tick"] != a["tick"] or b["revision"] < a["revision"]:
                problems.append({"reason": "interrupted_version_chain", "coordinate": coord})
        pool = item["state"].get("zombies")
        if not isinstance(pool, dict) or not isinstance(pool.get("slots"), dict):
            problems.append({"reason": "missing_zombie_pool", "coordinate": coord})
            pool = {"slots": {}}
        current = {}
        for slot, entry in sorted(pool["slots"].items(), key=lambda pair: int(pair[0])):
            identity = entry.get("id_or_free_next")
            fields = entry.get("fields")
            allocated = type(identity) is int and 0 <= identity <= 0xffffffff and identity >> 16 != 0
            if type(identity) is int and 0 <= identity <= 0xffff and fields is None:
                continue
            if not allocated or identity & 0xffff != int(slot) or not isinstance(fields, dict):
                problems.append({"reason": "invalid_identity_or_missing_fields", "slot": slot, "coordinate": coord})
                continue
            raw = {name: fields.get(offset) for name, offset in FIELDS.items()}
            valid = all(type(v) is int and 0 <= v <= 0xffffffff for v in raw.values())
            valid = valid and raw["disappeared"] in (0, 1)
            fact = {"id": identity, "generation": identity >> 16, "slot": int(slot),
                    "raw": raw, "coordinate": coord, "valid": valid,
                    "pointer": f"/zombies/slots/{slot}"}
            current[identity] = fact
            if not valid:
                problems.append({"reason": "missing_or_invalid_field", "fact": fact})
                continue
            phases[raw["state"]] += 1
            prior = previous.get(identity)
            if identity in retired:
                problems.append({"reason": "full_identity_reappeared", "fact": fact})
            if count == 0:
                classification = ("preexisting_disappeared" if raw["disappeared"] else
                                  "preexisting_death" if raw["state"] in DEAD else "initial_live_or_unknown")
                initial_objects.append({"classification": classification, "fact": fact})
            if raw["state"] in DEAD and (prior is None or prior["raw"]["state"] not in DEAD):
                event = {"before": prior, "after": fact, "stage": DEAD[raw["state"]], "cause": "unverified"}
                deaths.append(event)
                if (prior and prior["valid"] and prior["raw"]["state"] in KNOWN_NONDEAD and
                        prior["raw"]["disappeared"] == 0 and signed(prior["raw"]["at_wave"]) >= 0 and
                        signed(raw["at_wave"]) >= 0 and not problems):
                    entries.append(event)
                    confirmed.add(identity)
                else:
                    uncertain.append({"reason": "death_observed_without_verified_live_predecessor", **event})
            if prior and prior["valid"] and prior["raw"]["disappeared"] == 0 and raw["disappeared"] == 1 and identity not in confirmed:
                uncertain.append({"reason": "disappeared_without_confirmed_death", "before": prior, "after": fact})
        for identity, prior in previous.items():
            if identity not in current:
                retired.add(identity)
                classification = "release_after_confirmed_death" if identity in confirmed else "unknown_removal"
                if any(x["fact"]["id"] == identity and x["classification"] == "preexisting_disappeared" for x in initial_objects):
                    classification = "release_of_preexisting_disappeared_object"
                event = {"classification": classification, "before": prior, "after_coordinate": coord,
                         "replacement": next((v for v in current.values() if v["slot"] == prior["slot"]), None)}
                removals.append(event)
                if classification == "unknown_removal":
                    uncertain.append({"reason": "removed_without_confirmed_death", **event})
        previous = current
        last_coord = coord
        count += 1
    if pending is not None or last_coord is None or last_coord["version"] != final or last_coord["kind"] != "post_step":
        problems.append({"reason": "incomplete_declared_window", "expected_final": final, "actual": last_coord})
    first = entries[0]["after"]["coordinate"] if entries else None
    first_events = [e for e in entries if e["after"]["coordinate"] == first]
    early = [u for u in uncertain if (u.get("after", {}).get("coordinate") or u.get("after_coordinate"))["line"] <= (first["line"] if first else float("inf"))]
    return {"status": "failed" if problems else "analyzed", "boundaries": count,
            "first_boundary": first_coord, "last_boundary": last_coord, "problems": problems,
            "phase_observations": dict(sorted(phases.items())),
            "unclassified_non_death_codes": sorted(set(phases) - set(DEAD) - KNOWN_NONDEAD),
            "initial_objects": initial_objects, "death_observations": deaths,
            "confirmed_entries": entries, "first_confirmed_entries": first_events,
            "removals": removals, "uncertain_events": uncertain, "earlier_or_same_boundary_uncertainty": early,
            "first_observed_death_stage": deaths[0] if deaths else None,
            "first_window_kill": "unverified",
            "limits": ["Boundary snapshots cannot exclude birth and immediate removal entirely inside one native call.",
                       "Same-boundary entities have no observed intra-call ordering; all earliest candidates are retained.",
                       "No damage-source evidence: plant, mower, self explosion and friendly damage attribution unverified."]}


def audit_items(directory):
    store = EvidenceStore(directory)
    # Bind source positions and labels to both companion streams, without tick merging.
    streams = (iter_states(store, in_place=True), read_rows(store, "checksums.jsonl"),
               read_rows(store, "engine-call-raw.jsonl"))
    for item, checksum, native in zip_longest(*streams):
        if item is None or checksum is None or native is None:
            raise ValueError(f"{directory}: state/checksum/native stream length mismatch")
        row = item["row"]
        for key in ("seq", "kind", "version"):
            if row.get(key) != checksum.get(key) or row.get(key) != native.get(key):
                raise ValueError(f"{directory}: companion coordinate mismatch at line {item['index'] + 1}")
        if row["payload"]["engine_call"]["engine_call_id"] != native.get("engine_call_id"):
            raise ValueError(f"{directory}: native call ID mismatch")
        yield item


def inventory(paths):
    files = set()
    for path in paths:
        if not path.exists():
            raise FileNotFoundError(str(path))
        if path.is_dir():
            files.update(p for p in path.rglob("*") if p.is_file())
        else:
            files.add(path)
    return {str(p.resolve()): sha256_file(p) for p in sorted(files)}


def event_signature(result):
    """Compare every event, identity, field and coordinate, not aggregate counts."""
    return {k: result[k] for k in ("first_boundary", "last_boundary", "initial_objects", "death_observations",
            "confirmed_entries", "removals", "uncertain_events", "phase_observations", "problems")}


def markdown(report):
    delivery = "离线分析交付完成" if report["delivery_status"] == "complete" else "离线分析失败"
    lines = ["# Issue #110 离线死亡阶段派生报告", "",
             f"{delivery}；全窗口首次击杀门槛 **未验证**。死亡阶段和伤害来源分开判定。", ""]
    if report["delivery_status"] != "complete":
        lines += ["失败轨迹及原因：", ""]
        for role, run in report["runs"].items():
            if run["status"] != "failed":
                continue
            for problem in run["problems"] or [{"reason": "未提供具体原因"}]:
                coord = (problem.get("coordinate") or problem.get("fact", {}).get("coordinate")
                         or problem.get("actual"))
                location = f"；证据坐标 `{canonical(coord)}`" if coord is not None else ""
                lines.append(f"- {role}：`{problem['reason']}`{location}")
        lines.append("")
    lines += [f"规则 `{RULE}`；输入 tree_id `{report['tree_id']}`。", "",
             "| 轨迹 | 完整边界数 | 首次确认进入死亡阶段 | 同边界实体数 | 状态 |", "|---|---:|---|---:|---|"]
    for role, run in report["runs"].items():
        first = run["first_confirmed_entries"]
        coord = first[0]["after"]["coordinate"] if first else None
        lines.append(f"| {role} | {run['boundaries']} | `{canonical(coord)}` | {len(first)} | {run['status']}；首次击杀未验证 |")
    lines += ["", "## 判据与版本依据", "", *[f"- {s}" for s in report["rules"]], "",
              "## 同组复跑事件比较", "", f"```json\n{json.dumps(report['pairs'], ensure_ascii=False, indent=2)}\n```", ""]
    for role, run in report["runs"].items():
        lines += [f"## {role}", "", f"源 `{run['source']}`；状态/校验/原生调用 JSONL 行号均从 1 起（gzip 解压后的逻辑行）。",
                  f"完整身份 `{run['trajectory_id']}`；未知非死亡语义状态码 `{run['unclassified_non_death_codes']}`。", "",
                  f"确认死亡阶段事件 {len(run['confirmed_entries'])}；槽位释放 {len(run['removals'])}；不确定事件 {len(run['uncertain_events'])}。",
                  "首次同边界候选如下（不按槽位排序虚构先后）。所有事件与初态残留见 JSON。", "",
                  "```json", json.dumps(run["first_confirmed_entries"], ensure_ascii=False, indent=2), "```", "",
                  "早于或同于首次确认边界的不确定事件：", "", "```json",
                  json.dumps(run["earlier_or_same_boundary_uncertainty"], ensure_ascii=False, indent=2), "```", ""]
    lines += ["## 未验证范围与最小补录建议", "", *[f"- {x}" for x in report["minimum_capture"]], "",
              "## 输入保护与分析器身份", "", f"分析前后摘要一致：{report['source_unchanged']}；旧 seal：{report['seal_after']['matches']}。",
              f"分析器身份：`{canonical(report['analyzer'])}`。完整输入逐文件 SHA256 在 JSON 的 inputs 中。", "",
              "## 复现", "", "```powershell", "python tools/issue110_deaths.py --output work/issue110-fc2-deaths-new", "```", "",
              "输出目录必须不存在。该命令只读已有输入，不启动游戏。", "",
              "关联：[#99](https://github.com/guajun/llm-vs-zombies/issues/99)、[#105](https://github.com/guajun/llm-vs-zombies/issues/105)、",
              "[PR #107](https://github.com/guajun/llm-vs-zombies/pull/107)、[#110](https://github.com/guajun/llm-vs-zombies/issues/110)。", ""]
    return "\n".join(lines)


def analyze(root):
    tree = root / "experiments/trees/issue99-fc2-shovel-fork"
    seal_path = root / "work/issue99-fc2-seal.json"
    runs = {role: root / f"experiments/runs/issue99-fc2-{role}-s42-c0" for role in RUNS}
    paths = [tree, seal_path, *(root / "work" / name for name in OLD_REPORTS)]
    for role, run in runs.items():
        paths += [run / "audit", run / "trajectory", run / "manifest.json", run / "experiment-end.json",
                  root / f"experiments/runs/issue99-fc2-{role}/plan.json"]
    before = inventory(paths)
    for name, digest in OLD_REPORTS.items():
        if sha256_file(root / "work" / name) != digest:
            raise ValueError(f"issue #110 input identity mismatch: {name}")
    old_report = read_json(root / "work/issue99-fc2-report.json")
    print("Validating sealed tree and all four native audit streams...", flush=True)
    # The legacy seal stores relative report paths; run its verifier at input root.
    import os
    cwd = Path.cwd()
    try:
        os.chdir(root)
        loaded = read_tree(tree)
        verified = verify_seal(tree, seal_path, loaded=loaded)
    finally:
        os.chdir(cwd)
    if not verified["matches"] or verified["tree_id"] != TREE_ID or verified["nodes"] != 4:
        raise ValueError(f"seal/tree identity rejected: {verified}")
    report = {"schema": RULE, "tree_id": TREE_ID, "seal_before": verified, "inputs": before,
              "semantic_sources": SEMANTIC_SOURCES,
              "rules": [
                  f"AvZ {AVZ} inc/avz_pvz_struct.h: State +0x28 int32; IsDead iff 1/2/3 (倒地/灰烬/小推车阶段).",
                  "HP +0xc8 / 饰品 +0xd0,+0xdc 为 int32；审计存 uint32 原始字，负数按二补码解释；HP<=0 不作死亡判据。",
                  "IsDisappeared +0xec 为 uint8/bool；不是死亡判据。State=2 时本体 HP 仍可为正。",
                  "完整 ID +0x158 为 uint32：低16位槽位，高16位代次；audit id_or_free_next 高16位为0表示空闲链。",
                  f"{BASELINE} determinism/audit.cpp Entity/Pool/Observe 覆盖上述字段和每次 pre/post；复用 iter_states/EvidenceStore。",
                  "确认进入死亡阶段需同 ID、有效前后字段、前态为已识别非死亡状态、未消失、AtWave>=0。已核状态码0/11/15/20/69/70/71/73/76按 AvZ IsDead 均为非死亡；其余码标未分类。",
                  "非死亡状态名称辅助参照本机封存候选 ConstEnums.h（摘要见 semantic_sources），不冒充逐项独立反汇编；死亡定义本身只用锁定 AvZ 的1/2/3判据。",
                  "初态已死亡/已消失单列；无死亡证据的消失或释放归原因未知。植物/小推车/自爆/友军均不凭时机归因。",
                  "这里的确认死亡只指 AvZ 死亡阶段；若将击杀定义为伤害导致死亡，则缺少来源/生命周期事件时门槛未验证。"],
              "runs": {}, "pairs": {}, "first_kill_gate": "unverified",
              "minimum_capture": [
                  "缺失的是同一原生调用内部的死亡/消失/回收原因与先后，而非已有 state-deltas/checksums/engine-call-raw 文件。",
                  "现有 audit coverage.exact_spawn_hook=false；不能排除两边界间创建并立即删除的实体，不能证明绝对首次死亡。",
                  "若需要证明门槛，先核验锁定二进制的死亡分支和无伤害回收路径，再记录完整ID/代次、原生call ID、调用内序号、前后state/HP/disappeared及原因。通用清理函数不足。",
                  "攻击者归因另需伤害事件（攻击者/投射物ID、伤害种类、目标ID），死亡阶段2不能唯一指向某一炮。",
                  "最小新采集仍覆盖原冻结根至既有终点，以排除更早遗漏；可先单分支验证插桩，再由用户决定是否补同组复跑/其余分支。不自动跑四局、不扩大窗口。"]}
    for role, run in runs.items():
        source = read_json(run / "trajectory/trajectory.json")
        node = read_json(tree / "nodes" / RUNS[role] / "trajectory.json")
        if any(source[k] != node[k] for k in ("initial", "steps", "files")):
            raise ValueError(f"{role}: run does not match sealed node contents")
        for name, digest in node["files"].items():
            if sha256_file(run / "trajectory" / name) != digest:
                raise ValueError(f"{role}: bound trajectory file changed: {name}")
            if name.startswith("audit/") and sha256_file(run / name) != digest:
                raise ValueError(f"{role}: analyzed audit not bound to tree: {name}")
        identity = source["initial"]["identity"]
        if (identity["build"]["avz_commit"] != AVZ or identity["build"]["pointer_bits"] != 32 or
                identity["game"]["target"] != "pvz-1.0.0.1051-en" or not identity["game"]["loaded_signatures_match"]):
            raise ValueError(f"{role}: unsupported native identity")
        if identity["artifacts"]["input_hashes"].get("game/local-engine/PlantsVsZombies.exe") != "f9669af338964787a3785a7895791297d599295b8bb669b0db49443f736a1322":
            raise ValueError(f"{role}: unsupported game binary digest")
        initial = source["initial"]["observation"]["version"]
        final = source["steps"][-1]["result"]["observation"]["version"]
        ending = read_json(run / "experiment-end.json")
        plan = read_json(root / f"experiments/runs/issue99-fc2-{role}/plan.json")
        legacy_role = role[:-2] + ("-rerun" if role.endswith("-b") else "")
        if plan != old_report["plans"][legacy_role]:
            raise ValueError(f"{role}: frozen plan differs from the sealed report")
        if ending["final_observation"]["version"] != final:
            raise ValueError(f"{role}: end declaration mismatch")
        if (final["tick"] > plan["tick_budget"] or ending["outcome"] != "stop_condition_reached" or
                ending["final_observation"]["wave"] < plan["stop_when"]["wave_at_least"]):
            raise ValueError(f"{role}: frozen stop condition not verified")
        result = scan(audit_items(run / "audit"), initial, final)
        result.update(source=str(run / "audit/state-deltas.jsonl.gz"),
                      declared_plan=plan, outcome=ending["outcome"], final_wave=ending["final_observation"]["wave"],
                      trajectory_id=source["trajectory_id"], sealed_node_trajectory_id=node["trajectory_id"],
                      identity=identity, coverage=read_json(run / "audit/manifest.json")["coverage"])
        report["runs"][role] = result
        print(f"{role}: {result['boundaries']} boundaries, {len(result['confirmed_entries'])} confirmed entries", flush=True)
    for group in ("control", "intervention"):
        a, b = (report["runs"][f"{group}-{suffix}"] for suffix in ("a", "b"))
        left, right = event_signature(a), event_signature(b)
        report["pairs"][group] = {"events_equal": left == right,
                                   "a_sha256": hashlib.sha256(canonical(left).encode()).hexdigest(),
                                   "b_sha256": hashlib.sha256(canonical(right).encode()).hexdigest(),
                                   "scope": "all entity events and raw fields, seq, line, version, native call; paths/request labels excluded"}
    after = inventory(paths)
    report["source_unchanged"] = before == after
    if before != after:
        raise ValueError("source evidence changed during analysis")
    # Validated tree bytes are identical; re-check seal and old reports without repeating expensive decoding.
    try:
        os.chdir(root)
        report["seal_after"] = verify_seal(tree, seal_path, loaded=loaded)
    finally:
        os.chdir(cwd)
    report["analyzer"] = {"rule": RULE, "base_commit": BASELINE,
        "checkout_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_sha256": sha256_file(__file__),
        "dependencies": {name: sha256_file(ROOT / name) for name in
                         ("tools/compare_worlds.py", "tools/issue99_shovel_fork.py",
                          "src/llm_vs_zombies/audit_compare.py", "src/llm_vs_zombies/evidence_codec.py",
                          "src/llm_vs_zombies/evidence_tree.py")}}
    report["delivery_status"] = "complete" if all(r["status"] == "analyzed" for r in report["runs"].values()) else "failed"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=ROOT / "work/issue110-fc2-deaths")
    args = parser.parse_args(argv)
    for protected in (args.root / "experiments/runs", args.root / "experiments/trees"):
        if args.output.resolve().is_relative_to(protected.resolve()):
            parser.error("output must be outside original run/tree evidence directories")
    # Exclusive directory creation protects old reports, even on failed analysis.
    args.output.mkdir(parents=True, exist_ok=False)
    try:
        report = analyze(args.root.resolve())
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError, RuntimeError, SystemExit) as exc:
        failure = {"schema": RULE, "delivery_status": "failed", "first_kill_gate": "unverified",
                   "error": str(exc), "remedy": "Restore the named sealed input/coverage; do not substitute a new game run."}
        (args.output / "failure.json").write_text(json.dumps(failure, ensure_ascii=False, indent=2), encoding="utf-8")
        print(canonical(failure))
        return 1
    for name, content in (("report.json", json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"),
                          ("report.md", markdown(report))):
        with (args.output / name).open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
    hashes = {name: sha256_file(args.output / name) for name in ("report.json", "report.md")}
    (args.output / "SHA256SUMS.json").write_text(json.dumps(hashes, indent=2) + "\n", encoding="utf-8")
    print(canonical({"delivery_status": report["delivery_status"], "first_kill_gate": "unverified", "sha256": hashes}))
    return 0 if report["delivery_status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
