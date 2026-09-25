"""Offline checker for issue #111 candidate native-path evidence.

The candidate table in ``docs/issue111-原生候选证据.json`` records static
disassembly facts about the locked PvZ 1.0.0.1051 image. This tool never loads
the game, never writes to the executable and never installs a hook:

* ``--check`` validates the evidence document's structure and repository
  anchors. It fails if a candidate claims ``established`` without a named
  maintainer review.
* ``verify --exe PATH`` re-hashes the locked executable, maps each candidate
  RVA through the PE section table and compares the recorded bytes. The
  executable is the user's local, git-ignored copy; the repository only stores
  its SHA-256.

Exit codes: 0 = clean, 1 = evidence problems/mismatch, 2 = unreadable input.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "issue111-原生候选证据.json"
SCHEMA = "lvz.native-candidates.v1"
CANDIDATE_STATUSES = ("candidate_review_required", "established")
_HEX = re.compile(r"^0x[0-9a-f]+$")


class CandidateError(ValueError):
    """The candidate evidence document cannot be read."""


def load(path: Path = EVIDENCE) -> dict:
    try:
        return json.loads(Path(path).read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CandidateError(f"candidate evidence unreadable: {exc}") from exc


def _parse_hex(value, label, problems):
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        problems.append(f"{label} must be a 0x-prefixed lowercase hex value")
        return None
    return int(value, 16)


def check(doc: dict, root: Path | None = None) -> list[str]:
    problems: list[str] = []
    if not isinstance(doc, dict):
        return ["candidate evidence must be an object"]
    if doc.get("schema") != SCHEMA:
        problems.append(f"schema must be {SCHEMA!r}")
    for key in ("issue", "generated", "status", "method", "protocol_notes", "candidates",
                "interception_plan", "residual_questions"):
        if key not in doc:
            problems.append(f"missing top-level key: {key}")
    if doc.get("status") not in ("candidate_review_required", "reviewed"):
        problems.append("top-level status must be candidate_review_required or reviewed")
    target = doc.get("target")
    if not isinstance(target, dict):
        problems.append("target must be an object")
        target = {}
    image_base = _parse_hex(target.get("image_base"), "target.image_base", problems)
    sha = target.get("sha256")
    if not (isinstance(sha, str) and len(sha) == 64 and all(c in "0123456789abcdef" for c in sha)):
        problems.append("target.sha256 must be a 64-character lowercase hex digest")
    avz = target.get("avz_commit")
    if not (isinstance(avz, str) and len(avz) == 40 and all(c in "0123456789abcdef" for c in avz)):
        problems.append("target.avz_commit must be a 40-character lowercase hex commit")
    if not isinstance(doc.get("protocol_notes"), list) or not doc.get("protocol_notes"):
        problems.append("protocol_notes must be a non-empty list")
    if root is not None:
        evidence_path = Path(root) / "determinism" / "evidence.json"
        if evidence_path.is_file():
            evidence = json.loads(evidence_path.read_bytes())
            if evidence.get("extracted_executable_sha256") != sha:
                problems.append("target.sha256 does not match determinism/evidence.json extracted_executable_sha256")
        lock_path = Path(root) / "dependencies.lock.json"
        if lock_path.is_file():
            lock = json.loads(lock_path.read_bytes())
            if lock.get("avz_commit") != avz:
                problems.append("target.avz_commit does not match dependencies.lock.json")

    candidates = doc.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        problems.append("candidates must be a non-empty list")
        return problems
    seen_ids: set[str] = set()
    seen_vas: set[str] = set()
    any_established = False
    required = ("id", "status", "entry_va", "rva", "function_start_va", "bytes32", "abi", "semantics",
                "observed", "callers", "candidate_classification", "coverage_limits", "open_questions",
                "board_preview", "semantic_gate", "state_transition", "exit_effects", "caller_classes")
    for index, candidate in enumerate(candidates):
        label = f"candidates[{index}]"
        if not isinstance(candidate, dict):
            problems.append(f"{label} must be an object")
            continue
        for key in required:
            if key not in candidate:
                problems.append(f"{label} missing key: {key}")
        candidate_id = candidate.get("id")
        if not isinstance(candidate_id, str) or not candidate_id:
            problems.append(f"{label} id must be a non-empty string")
        elif candidate_id in seen_ids:
            problems.append(f"duplicate candidate id: {candidate_id}")
        else:
            seen_ids.add(candidate_id)
        status = candidate.get("status")
        if status not in CANDIDATE_STATUSES:
            problems.append(f"{label} status must be one of {CANDIDATE_STATUSES}")
        if status == "established":
            any_established = True
            if not isinstance(candidate.get("review"), str) or not candidate["review"].strip():
                problems.append(f"{label} is established without a maintainer review note")
        entry = _parse_hex(candidate.get("entry_va"), f"{label}.entry_va", problems)
        rva = _parse_hex(candidate.get("rva"), f"{label}.rva", problems)
        start = _parse_hex(candidate.get("function_start_va"), f"{label}.function_start_va", problems)
        if entry is not None and rva is not None and image_base is not None and rva != entry - image_base:
            problems.append(f"{label}: rva must equal entry_va - image_base")
        if start is not None and entry is not None and start > entry:
            problems.append(f"{label}: function_start_va must not be greater than entry_va")
        if entry is not None:
            value = candidate.get("entry_va")
            if value in seen_vas:
                problems.append(f"duplicate candidate entry_va: {value}")
            seen_vas.add(value)
        payload = candidate.get("bytes32")
        if not (isinstance(payload, str) and len(payload) == 64 and all(c in "0123456789abcdef" for c in payload)):
            problems.append(f"{label}.bytes32 must be 32 bytes of lowercase hex")
        for key in ("abi", "semantics", "candidate_classification", "board_preview", "semantic_gate",
                    "state_transition"):
            if not isinstance(candidate.get(key), str) or not candidate[key].strip():
                problems.append(f"{label}.{key} must be a non-empty string")
        for key in ("observed", "callers", "coverage_limits", "open_questions", "exit_effects"):
            value = candidate.get(key)
            if not isinstance(value, list) or not value:
                problems.append(f"{label}.{key} must be a non-empty list")
                continue
            for item in value:
                if not isinstance(item, str) or not item.strip():
                    problems.append(f"{label}.{key} entries must be non-empty strings")
        for caller in candidate.get("callers") or []:
            if isinstance(caller, str) and not _HEX.fullmatch(caller):
                problems.append(f"{label} caller must be 0x-prefixed hex: {caller!r}")
    if any_established and not (isinstance(doc.get("reviewed_by"), str) and doc["reviewed_by"].strip()):
        problems.append("established candidates require a reviewed_by maintainer name")
    plan = doc.get("interception_plan")
    if not isinstance(plan, list) or not plan:
        problems.append("interception_plan must be a non-empty list")
    else:
        for index, strategy in enumerate(plan):
            label = f"interception_plan[{index}]"
            if not isinstance(strategy, dict):
                problems.append(f"{label} must be an object")
                continue
            for key in ("fact", "strategy", "rationale", "abi", "board_filter", "rejected_alternatives"):
                if key not in strategy:
                    problems.append(f"{label} missing key: {key}")
            for key in ("fact", "strategy", "rationale", "abi", "board_filter"):
                if isinstance(strategy.get(key), str) and not strategy[key].strip():
                    problems.append(f"{label}.{key} must not be empty")
            if not isinstance(strategy.get("rejected_alternatives"), list) or not strategy["rejected_alternatives"]:
                problems.append(f"{label}.rejected_alternatives must be a non-empty list")
    questions = doc.get("residual_questions")
    if not isinstance(questions, list) or not questions or any(
            not isinstance(item, str) or not item.strip() for item in questions):
        problems.append("residual_questions must be a non-empty list of strings")
    return problems


def pe_sections(data: bytes) -> list[tuple[int, int, int, int]]:
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise CandidateError("not a PE image (missing MZ)")
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if data[e_lfanew:e_lfanew + 4] != b"PE\0\0":
        raise CandidateError("not a PE image (missing PE signature)")
    coff = e_lfanew + 4
    number_of_sections, = struct.unpack_from("<H", data, coff + 2)
    size_of_optional_header, = struct.unpack_from("<H", data, coff + 16)
    section_table = coff + 20 + size_of_optional_header
    sections = []
    for index in range(number_of_sections):
        offset = section_table + index * 40
        if offset + 40 > len(data):
            raise CandidateError("truncated PE section table")
        virtual_size, virtual_address, size_of_raw, pointer_to_raw = struct.unpack_from("<IIII", data, offset + 8)
        sections.append((virtual_address, virtual_size, pointer_to_raw, size_of_raw))
    return sections


def bytes_at(data: bytes, rva: int, length: int) -> bytes:
    for virtual_address, virtual_size, pointer_to_raw, size_of_raw in pe_sections(data):
        if virtual_address <= rva < virtual_address + max(virtual_size, size_of_raw):
            offset = pointer_to_raw + (rva - virtual_address)
            if offset + length > len(data):
                raise CandidateError(f"rva 0x{rva:x} extends past the image")
            return data[offset:offset + length]
    raise CandidateError(f"rva 0x{rva:x} is not inside a PE section")


def verify(doc: dict, exe: Path) -> dict:
    problems = check(doc)
    if problems:
        raise CandidateError("candidate evidence invalid: " + "; ".join(problems))
    try:
        data = Path(exe).read_bytes()
    except OSError as exc:
        raise CandidateError(f"executable unreadable: {exc}") from exc
    digest = hashlib.sha256(data).hexdigest()
    target_sha = doc["target"]["sha256"]
    results = []
    for candidate in doc["candidates"]:
        entry = int(candidate["entry_va"], 16)
        expected = bytes.fromhex(candidate["bytes32"])
        try:
            actual = bytes_at(data, entry - int(doc["target"]["image_base"], 16), len(expected))
        except CandidateError as exc:
            results.append({"id": candidate["id"], "status": "error", "detail": str(exc)})
            continue
        results.append({"id": candidate["id"], "status": "match" if actual == expected else "mismatch",
                        "expected": expected.hex(), "actual": actual.hex()})
    return {"schema": SCHEMA, "exe": str(exe), "sha256": digest, "sha256_match": digest == target_sha,
            "results": results,
            "ok": digest == target_sha and all(item["status"] == "match" for item in results)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check/verify #111 candidate native-path evidence offline")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--evidence", type=Path, default=EVIDENCE)
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="validate the evidence document and repository anchors")
    verify_parser = sub.add_parser("verify", help="compare recorded bytes with the locked executable")
    verify_parser.add_argument("--exe", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        doc = load(args.evidence)
        if args.command == "check":
            problems = check(doc, root=args.root)
            report = {"schema": SCHEMA, "ok": not problems, "problems": problems,
                      "candidates": len(doc.get("candidates") or [])}
            code = 0 if report["ok"] else 1
        else:
            report = verify(doc, args.exe)
            code = 0 if report["ok"] else 1
    except CandidateError as exc:
        report = {"schema": SCHEMA, "ok": False, "problems": [str(exc)]}
        code = 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not args.json and code:
        for problem in report.get("problems", []):
            print(problem, file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
