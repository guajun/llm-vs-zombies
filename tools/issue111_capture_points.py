"""Validate the #111 lifecycle capture-point table and its repository anchors.

This is an offline contract check: it never starts the game, loads the native
runtime or reads sealed evidence. It keeps two properties honest:

1. table self-consistency -- required keys, status vocabulary, four fact
   classes, ``capture_sequence``/``seq`` separation, and the phase-A promise
   that no hook and no game run was added;
2. repository anchors -- the established ``zombie_initialized`` capture point
   still matches ``determinism/spawn_hook.cpp``, and the claims the table
   makes about ``determinism/audit.cpp`` and the event envelope still hold.

A ``review_required`` row is only allowed to describe missing evidence: it may
not carry an address, byte string or covered path. That is the junior execution
gate of issue #111 in executable form.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "docs" / "issue111-捕获点表.json"
SCHEMA = "lvz.capture-points.v1"
STATUSES = ("established", "review_required", "not_covered")
FACTS = ("initialization", "confirmed_death_stage", "non_death_removal", "slot_recycle")
CONTRACT_FIELDS = ("schema", "kind", "capture_sequence", "version", "version_phase",
                   "engine_call_id", "invocation", "entity", "before_after",
                   "classification", "probe", "complete")
CONTRACT_COUNTERS = ("captured", "delivered", "persisted", "overflow")
TOP_KEYS = ("schema", "generated", "issue", "delivery", "target", "upstream_recheck",
            "vocabulary", "fact_classes", "capture_points", "data_contract",
            "schema_compatibility", "open_questions", "review_checklist", "phase_plan")
POINT_KEYS = ("id", "event", "fact", "status", "phase", "phase_note", "caller_branch_basis",
              "abi", "ordering", "coverage", "failure_semantics")


class ContractError(ValueError):
    """The capture-point table cannot be read at all."""


def load(path: Path = TABLE) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"capture table unreadable: {exc}") from exc


def _points(table: dict) -> list[dict]:
    value = table.get("capture_points")
    return value if isinstance(value, list) else []


def _find(table: dict, point_id: str) -> dict:
    for row in _points(table):
        if isinstance(row, dict) and row.get("id") == point_id:
            return row
    return {}


def _address_claims(value, path: str) -> list[str]:
    """Any non-null RVA/address/bytes under ``abi`` is an unconfirmed claim."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and ("rva" in key.lower() or "address" in key.lower()
                                          or "bytes" in key.lower()):
                found.append(f"{path}.{key}")
            else:
                found.extend(_address_claims(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_address_claims(item, f"{path}[{index}]"))
    return found


def problems(table: dict, root: Path = ROOT) -> list[str]:
    """Return every self-consistency problem in the parsed table."""
    out: list[str] = []
    if not isinstance(table, dict):
        return ["capture table is not a JSON object"]
    for key in TOP_KEYS:
        if key not in table:
            out.append(f"missing top-level key: {key}")
    if table.get("schema") != SCHEMA:
        out.append(f"schema must be {SCHEMA!r}, got {table.get('schema')!r}")

    delivery = table.get("delivery") or {}
    if delivery.get("hooks_added") != 0:
        out.append("delivery.hooks_added must be 0 for phase A")
    if delivery.get("game_runs") != 0:
        out.append("delivery.game_runs must be 0 for phase A")

    facts = table.get("fact_classes") or {}
    if not set(FACTS).issubset(set(facts)):
        out.append(f"fact_classes must cover {list(FACTS)}")
    for name, entry in facts.items():
        if not isinstance(entry, dict) or not entry.get("definition"):
            out.append(f"fact_classes.{name} lacks a definition")
        if not isinstance(entry.get("current_capture"), list):
            out.append(f"fact_classes.{name}.current_capture must be a list")

    seen: set[str] = set()
    for index, row in enumerate(_points(table)):
        label = f"capture_points[{index}]"
        if not isinstance(row, dict):
            out.append(f"{label} is not an object")
            continue
        for key in POINT_KEYS:
            if key not in row:
                out.append(f"{label} missing key: {key}")
        point_id = row.get("id")
        if not point_id:
            out.append(f"{label} has no id")
        elif point_id in seen:
            out.append(f"duplicate capture point id: {point_id}")
        else:
            seen.add(point_id)
        status = row.get("status")
        if status not in STATUSES:
            out.append(f"{point_id}: status {status!r} outside {list(STATUSES)}")
        if row.get("fact") not in facts:
            out.append(f"{point_id}: fact {row.get('fact')!r} not in fact_classes")
        abi = row.get("abi")
        if not isinstance(abi, dict):
            out.append(f"{point_id}: abi must be an object")
            abi = {}
        coverage = row.get("coverage") or {}
        if not isinstance(coverage.get("covered_paths"), list) or not isinstance(coverage.get("unknown_paths"), list):
            out.append(f"{point_id}: coverage must list covered_paths and unknown_paths")
        if not row.get("failure_semantics"):
            out.append(f"{point_id}: failure_semantics must not be empty")
        if status == "established":
            if abi.get("kind") in (None, "unknown"):
                out.append(f"{point_id}: established row needs a known ABI kind")
            evidence = abi.get("evidence")
            if not isinstance(evidence, list) or not evidence:
                out.append(f"{point_id}: established row needs abi.evidence")
            else:
                for item in evidence:
                    if not isinstance(item, dict) or not item.get("path"):
                        out.append(f"{point_id}: evidence entry lacks path")
                        continue
                    if item.get("availability") == "local":
                        continue
                    if not (Path(root) / item["path"]).exists():
                        out.append(f"{point_id}: evidence path does not exist: {item['path']}")
            if not coverage.get("covered_paths"):
                out.append(f"{point_id}: established row needs covered_paths")
        elif status == "review_required":
            if abi.get("kind") != "unknown":
                out.append(f"{point_id}: review_required row must declare abi.kind=unknown")
            claims = _address_claims(abi, f"{point_id}.abi")
            if claims:
                out.append(f"{point_id}: review_required row claims addresses: {', '.join(claims)}")
            if not row.get("open_questions"):
                out.append(f"{point_id}: review_required row needs open_questions")
            if not row.get("required_payload"):
                out.append(f"{point_id}: review_required row needs required_payload")
            if coverage.get("covered_paths"):
                out.append(f"{point_id}: review_required row must not claim covered_paths")

    needed = {point_id for entry in facts.values()
              for point_id in (entry.get("needed_capture") or [])}
    for point_id in sorted(needed):
        if point_id not in seen:
            out.append(f"fact_classes reference missing capture point: {point_id}")
    for entry in facts.values():
        for point_id in entry.get("current_capture") or []:
            if point_id not in seen:
                out.append(f"fact_classes reference missing capture point: {point_id}")

    contract = table.get("data_contract") or {}
    names = [field.get("name") for field in contract.get("fields") or [] if isinstance(field, dict)]
    for name in CONTRACT_FIELDS:
        if name not in names:
            out.append(f"data_contract.fields missing {name}")
    for name in CONTRACT_COUNTERS:
        if name not in (contract.get("counters") or []):
            out.append(f"data_contract.counters missing {name}")
    policy = contract.get("seq_policy") or {}
    if policy.get("seq_is_write_order") is not True or policy.get("seq_is_capture_order") is not False:
        out.append("data_contract.seq_policy must keep seq as write order, not capture order")
    if policy.get("capture_sequence_is_write_order") is not False:
        out.append("capture_sequence must not be a write-order alias")
    if policy.get("capture_sequence_reset_on_drain") is not False:
        out.append("capture_sequence must survive drain")
    if "unavailable" not in str(policy.get("missing", "")):
        out.append("data_contract.seq_policy.missing must mark missing capture_sequence unavailable")
    if not (contract.get("batch_ownership") or {}).get("no_second_drain"):
        out.append("data_contract.batch_ownership.no_second_drain is required")
    if not contract.get("failure_semantics"):
        out.append("data_contract.failure_semantics must not be empty")

    if not table.get("open_questions"):
        out.append("top-level open_questions must not be empty")
    if not (table.get("phase_plan") or {}).get("gate"):
        out.append("phase_plan.gate must state the junior execution gate")
    schema_compat = table.get("schema_compatibility") or {}
    for key in ("approach", "reader_rule", "versioned_migration"):
        if not schema_compat.get(key):
            out.append(f"schema_compatibility missing {key}")

    contract_doc = Path(root) / "docs" / "issue111-生命周期测量合同.md"
    if contract_doc.exists():
        text = contract_doc.read_text(encoding="utf-8")
        for token in sorted(set(re.findall(r"`(zombie-[a-z][a-z-]*)`", text))):
            if token not in seen:
                out.append(f"contract document references unknown capture point id: {token}")
    return out


def _hex_pairs(literal: str) -> str:
    return "".join(value.lower() for value in re.findall(r"0x([0-9a-fA-F]{2})", literal))


def _table_bytes(row: dict, key: str) -> str:
    raw = str(row.get("abi", {}).get(key, ""))
    return "".join(token for token in raw.lower().split() if re.fullmatch(r"[0-9a-f]{2}", token))


def check_sources(table: dict, root: Path = ROOT) -> list[str]:
    """Cross-check the table's established facts against repository anchors."""
    out: list[str] = []
    root = Path(root)
    spawn = root / "determinism" / "spawn_hook.cpp"
    try:
        source = spawn.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"cannot read {spawn}: {exc}"]
    row = _find(table, "zombie-initialize-exit")
    match = re.search(r"kEntry\s*=\s*0x([0-9a-fA-F]+)\s*,\s*kExit\s*=\s*0x([0-9a-fA-F]+)", source)
    if not match:
        out.append("determinism/spawn_hook.cpp: kEntry/kExit constants not found")
    else:
        entry = f"0x{match.group(1).lower()}"
        exit_ = f"0x{match.group(2).lower()}"
        if row.get("abi", {}).get("entry_rva") != entry:
            out.append(f"table entry_rva {row.get('abi', {}).get('entry_rva')!r} != spawn_hook {entry}")
        if row.get("abi", {}).get("exit_epilogue_rva") != exit_:
            out.append(f"table exit_epilogue_rva {row.get('abi', {}).get('exit_epilogue_rva')!r} != spawn_hook {exit_}")
    entry_match = re.search(r"originalStart\[\]\s*=\s*\{([^}]*)\}", source)
    end_match = re.search(r"uint8_t end\[\]\s*=\s*\{([^}]*)\}", source)
    if not entry_match or _hex_pairs(entry_match.group(1)) != _table_bytes(row, "entry_bytes"):
        out.append("table entry_bytes do not match spawn_hook originalStart bytes")
    if not end_match or _hex_pairs(end_match.group(1)) != _table_bytes(row, "epilogue_bytes"):
        out.append("table epilogue_bytes do not match spawn_hook end bytes")
    if not any(item.get("path") == "tests/determinism_spawn_hook.cpp"
               for item in row.get("abi", {}).get("evidence", [])):
        out.append("established row must cite tests/determinism_spawn_hook.cpp")
    for anchor in ("0x522580", "0x524035"):
        if anchor not in spawn.read_text(encoding="utf-8"):
            out.append(f"documented ABI anchor missing from spawn_hook.cpp: {anchor}")

    docs = root / "docs" / "determinism-spawn-hook.md"
    try:
        text = docs.read_text(encoding="utf-8")
    except OSError as exc:
        out.append(f"cannot read {docs}: {exc}")
    else:
        for anchor in ("0x522580", "0x524035", "ret 0x14"):
            if anchor not in text:
                out.append(f"determinism-spawn-hook.md no longer documents {anchor}")

    audit = root / "determinism" / "audit.cpp"
    try:
        audit_text = audit.read_text(encoding="utf-8")
    except OSError as exc:
        out.append(f"cannot read {audit}: {exc}")
    else:
        if '"exact_spawn_hook",false' not in audit_text:
            out.append("audit.cpp no longer declares coverage.exact_spawn_hook=false")

    envelope = root / "logger" / "schemas" / "event.schema.json"
    try:
        schema = json.loads(envelope.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        out.append(f"cannot read {envelope}: {exc}")
    else:
        if "seq" not in schema.get("required", []):
            out.append("event.schema.json no longer requires seq; revisit the compatibility plan")
        if schema.get("additionalProperties") is not False:
            out.append("event.schema.json no longer rejects additional properties; revisit the compatibility plan")
    return out


def summary(table: dict) -> dict:
    rows = [row for row in _points(table) if isinstance(row, dict)]
    return {
        "schema": table.get("schema"),
        "capture_points": len(rows),
        "established": sum(row.get("status") == "established" for row in rows),
        "review_required": sum(row.get("status") == "review_required" for row in rows),
        "fact_classes": sorted((table.get("fact_classes") or {}).keys()),
        "hooks_added": (table.get("delivery") or {}).get("hooks_added"),
        "game_runs": (table.get("delivery") or {}).get("game_runs"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", type=Path, default=TABLE)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--check", action="store_true",
                        help="validate the table and its repository anchors (default action)")
    args = parser.parse_args(argv)
    try:
        table = load(args.table)
    except ContractError as exc:
        print(json.dumps({"ok": False, "problems": [str(exc)]}, ensure_ascii=False, indent=2))
        return 1
    found = problems(table, args.root) + check_sources(table, args.root)
    report = {"ok": not found, **summary(table), "problems": found}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not found else 1


if __name__ == "__main__":
    raise SystemExit(main())
