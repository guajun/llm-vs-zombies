"""Offline verifier for the issue #111 exact-store patch windows.

The native lifecycle probes patch whole instruction windows at locked
addresses. This tool proves, without loading the game, that:

* the site bytes in ``docs/issue111-patch-windows.json`` match the locked
  executable;
* the window is exactly covered by whole instructions (the continuation is an
  instruction boundary);
* no external branch (jmp/jcc/call) targets an address strictly inside the
  window, so no caller can enter patched bytes in the middle;
* the object class and store description are declared.

``--check`` validates the document only. ``--verify --exe ... --objdump ...``
re-disassembles the locked image and re-checks the byte signatures and branch
targets. Exit codes: 0 = ok, 1 = problems, 2 = unreadable input.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "issue111-patch-windows.json"
SCHEMA = "lvz.patch-windows.v1"
REQUIRED_SITE = ("id", "function", "va", "bytes", "window_bytes", "instructions", "continuation_va",
                 "register", "store", "object_class", "external_branch_targets")
_HEX = re.compile(r"^0x[0-9a-f]+$")


class WindowError(ValueError):
    pass


def load(path: Path = EVIDENCE) -> dict:
    try:
        return json.loads(Path(path).read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WindowError(f"patch-window evidence unreadable: {exc}") from exc


def check(doc: dict) -> list[str]:
    problems: list[str] = []
    if not isinstance(doc, dict):
        return ["evidence must be an object"]
    if doc.get("schema") != SCHEMA:
        problems.append(f"schema must be {SCHEMA!r}")
    if not isinstance(doc.get("patch_rule"), str) or not doc["patch_rule"]:
        problems.append("patch_rule must describe the replace-and-replay mechanism")
    target = doc.get("target")
    if not isinstance(target, dict) or target.get("image_base") != "0x400000":
        problems.append("target.image_base must be 0x400000")
    elif not (isinstance(target.get("sha256"), str) and len(target["sha256"]) == 64):
        problems.append("target.sha256 must be a lock digest")
    sites = doc.get("sites")
    if not isinstance(sites, list) or not sites:
        return problems + ["sites must be a non-empty list"]
    seen_ids, seen_vas = set(), set()
    for index, site in enumerate(sites):
        label = f"sites[{index}]"
        if not isinstance(site, dict):
            problems.append(f"{label} must be an object")
            continue
        for key in REQUIRED_SITE:
            if key not in site:
                problems.append(f"{label} missing key: {key}")
        site_id = site.get("id")
        if not isinstance(site_id, str) or not site_id:
            problems.append(f"{label} id must be a non-empty string")
        elif site_id in seen_ids:
            problems.append(f"duplicate site id: {site_id}")
        else:
            seen_ids.add(site_id)
        va = site.get("va")
        if not isinstance(va, str) or not _HEX.fullmatch(va):
            problems.append(f"{label}.va must be 0x hex")
            continue
        if va in seen_vas:
            problems.append(f"duplicate site va: {va}")
        seen_vas.add(va)
        payload = site.get("bytes")
        if not isinstance(payload, str) or not payload or len(payload) % 2 or not all(
                c in "0123456789abcdef" for c in payload):
            problems.append(f"{label}.bytes must be lowercase hex")
            continue
        window = site.get("window_bytes")
        if window != len(payload) // 2:
            problems.append(f"{label}.window_bytes must equal the byte length")
        continuation = site.get("continuation_va")
        if not isinstance(continuation, str) or not _HEX.fullmatch(continuation):
            problems.append(f"{label}.continuation_va must be 0x hex")
        elif site.get("store", {}).get("kind") != "free_commit":
            if int(continuation, 16) != int(va, 16) + window:
                problems.append(f"{label}: continuation must equal va + window_bytes")
        if not isinstance(site.get("instructions"), list) or not site["instructions"]:
            problems.append(f"{label}.instructions must list the replaced instructions")
        if site.get("object_class") not in ("zombie", "zombie_pool"):
            problems.append(f"{label}.object_class must be zombie or zombie_pool")
        if not isinstance(site.get("external_branch_targets"), list):
            problems.append(f"{label}.external_branch_targets must be a list")
        if site.get("store", {}).get("kind") not in ("immediate_phase", "register_phase", "mdead_byte",
                                                     "guard_compare", "free_commit"):
            problems.append(f"{label}.store.kind is unsupported")
    return problems


def _objdump_range(objdump: Path, exe: Path, start: int, stop: int) -> list[tuple[int, bytes, str]]:
    output = subprocess.run([str(objdump), "-d", f"--start-address=0x{start:x}",
                             f"--stop-address=0x{stop:x}", str(exe)],
                            capture_output=True, text=True, timeout=120)
    if output.returncode:
        raise WindowError(f"objdump failed: {output.stderr.strip()}")
    rows = []
    for line in output.stdout.splitlines():
        match = re.match(r"^\s*([0-9a-f]{6,8}):\s+((?:[0-9a-f]{2} )+)\s*(.*)$", line)
        if match:
            rows.append((int(match.group(1), 16), bytes.fromhex(match.group(2).strip()), match.group(3).strip()))
    return rows


def verify(doc: dict, exe: Path, objdump: Path) -> dict:
    problems = check(doc)
    if problems:
        raise WindowError("evidence invalid: " + "; ".join(problems))
    digest = hashlib.sha256(Path(exe).read_bytes()).hexdigest()
    if digest != doc["target"]["sha256"]:
        raise WindowError("executable SHA-256 does not match the locked target")
    # Disassemble the whole .text once to collect every branch target.
    with tempfile.TemporaryDirectory() as temp:
        full = subprocess.run([str(objdump), "-d", str(exe)], capture_output=True, text=True, timeout=1200)
        if full.returncode:
            raise WindowError("objdump full disassembly failed")
    branch_targets: set[int] = set()
    for line in full.stdout.splitlines():
        match = re.match(r"^\s*([0-9a-f]{6,8}):\s+(?:[0-9a-f]{2} )+\s*(j\w+|calll?)\s+0x([0-9a-f]+)", line)
        if match:
            branch_targets.add(int(match.group(3), 16))
    results = []
    for site in doc["sites"]:
        va = int(site["va"], 16)
        window = site["window_bytes"]
        rows = _objdump_range(objdump, Path(exe), va, va + window)
        actual = b"".join(row[1] for row in rows)
        expected = bytes.fromhex(site["bytes"])
        covered = sum(len(row[1]) for row in rows)
        inside = sorted(target for target in branch_targets if va < target < va + window)
        status = []
        if actual[:window] != expected:
            status.append("byte_mismatch")
        if covered != window:
            status.append("window_not_instruction_aligned")
        if inside:
            status.append("external_branch_into_window:" + ",".join(hex(t) for t in inside))
        results.append({"id": site["id"], "status": "match" if not status else "problem",
                        "problems": status, "actual": actual.hex()})
    ok = all(item["status"] == "match" for item in results)
    return {"schema": SCHEMA, "exe": str(exe), "sha256": digest, "ok": ok, "sites": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check/verify #111 exact-store patch windows")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--evidence", type=Path, default=EVIDENCE)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--exe", type=Path, required=True)
    verify_parser.add_argument("--objdump", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        doc = load(args.evidence)
        if args.command == "check":
            problems = check(doc)
            report = {"schema": SCHEMA, "ok": not problems, "problems": problems, "sites": len(doc.get("sites") or [])}
            code = 0 if report["ok"] else 1
        else:
            report = verify(doc, args.exe, args.objdump)
            code = 0 if report["ok"] else 1
    except WindowError as exc:
        report = {"schema": SCHEMA, "ok": False, "problems": [str(exc)]}
        code = 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
