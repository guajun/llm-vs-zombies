"""Semantic comparison of two lifecycle recordings.

Two cold runs of the same plan have different ``run_id``/``branch_id`` by
design (and a per-process ``session_id``). This module compares the recorded
facts themselves and normalizes **only** those identity fields:

* every event (kind, capture_sequence, invocation, entity, version,
  classification, probe, completeness) must be deep-equal after removing the
  identity fields;
* receipts are compared on facts (record count, sequence bounds, counters,
  probe health, persistence) while their identity-bound digests remain
  verified individually by :mod:`lifecycle_events`;
* a difference reports the first differing record index and the first
  differing JSON path so a comparison cannot be confused with "both receipts
  are valid".

The audit ``compare_audits`` path remains the authority for boundary evidence;
this module adds the lifecycle stream to the comparison instead of ignoring it.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from . import evidence_codec, lifecycle_events
from .audit_compare import first_difference

IDENTITY_FIELDS = ("run_id", "branch_id", "session_id")
# The raw Board* is a process-local diagnostic pointer (ASLR differs per cold
# start). It is the only permitted normalization besides the run identity; the
# entity ids, slots, generations, waves and counts stay comparable.
DIAGNOSTIC_FIELDS = ("event.object.board",)
_RECEIPT_FACT_KEYS = ("schema", "event_schema", "envelope_schema", "sequence_domain", "records",
                      "first_capture_sequence", "last_capture_sequence", "counters", "probe_health",
                      "completed", "persistence")


class CompareError(ValueError):
    """The lifecycle evidence cannot be compared."""


def read_records(directory: str | Path) -> tuple[list[dict], dict]:
    directory = Path(directory)
    store = evidence_codec.EvidenceStore(directory, error=CompareError)
    if store.entry(lifecycle_events.EVENTS_FILE) is None and not (directory / lifecycle_events.EVENTS_FILE).is_file():
        raise CompareError(f"lifecycle events are missing: {directory}")
    records: list[dict] = []
    with store.open(lifecycle_events.EVENTS_FILE) as stream:
        for line in stream:
            if line.strip():
                records.append(json.loads(line))
    receipt = read_receipt(directory)
    return records, receipt


def read_receipt(directory: str | Path) -> dict:
    directory = Path(directory)
    store = evidence_codec.EvidenceStore(directory, error=CompareError)
    if store.entry(lifecycle_events.RECEIPT_FILE) is None and not (directory / lifecycle_events.RECEIPT_FILE).is_file():
        raise CompareError(f"lifecycle close receipt is missing: {directory}")
    with store.open(lifecycle_events.RECEIPT_FILE) as stream:
        return json.loads(stream.read())


def _validate(directory: Path) -> dict:
    try:
        report = lifecycle_events.validate(directory, require_close=True)
    except lifecycle_events.LifecycleError as exc:
        raise CompareError(f"lifecycle evidence is unreadable: {exc}") from exc
    if report["status"] != "valid":
        raise CompareError("lifecycle evidence is invalid: " + "; ".join(report["problems"]))
    return report


def normalize(record: dict) -> dict:
    """Remove only the permitted per-run identity and diagnostic fields."""
    value = copy.deepcopy(record)
    for key in IDENTITY_FIELDS:
        value.pop(key, None)
    if isinstance(value.get("event"), dict):
        obj = value["event"].get("object")
        if isinstance(obj, dict) and "board" in obj:
            obj["board"] = "<board-scope>"
    return value


SCOPED_NORMALIZATION = ("run_id", "branch_id", "session_id", "lifecycle_probes manifest",
                        "event.object.board")


def compare_scoped(left: str | Path, right: str | Path, *, scope: str = "both") -> dict:
    """Scope-aware comparison for the Stage-D arms.

    ``common`` compares only the shared gameplay/state/RNG/action/result
    evidence with the narrow declared instrumentation normalization; ``lifecycle``
    compares the exact-store stream semantically; ``both`` requires both.
    Each stream is still validated independently first.
    """
    if scope not in ("common", "lifecycle", "both"):
        raise CompareError(f"unsupported comparison scope: {scope}")
    left_run, right_run = Path(left), Path(right)
    left, right = left_run, right_run
    left = left / "audit" if (left / "audit" / "manifest.json").is_file() else left
    right = right / "audit" if (right / "audit" / "manifest.json").is_file() else right
    for directory in (left, right):
        try:
            report = lifecycle_events.validate(directory, require_close=True)
        except lifecycle_events.LifecycleError as exc:
            raise CompareError(f"lifecycle evidence is unreadable: {exc}") from exc
        if report["status"] == "failed":
            raise CompareError("lifecycle evidence is invalid: " + "; ".join(report["problems"]))
    report = {"schema": "lvz.lifecycle-scoped-compare.v1", "scope": scope,
              "normalized": list(SCOPED_NORMALIZATION), "equal": False,
              "requires": ["audit", "actions"] + (["lifecycle"] if scope != "common" else [])}
    if scope in ("common", "both"):
        try:
            from . import action_compare, audit_compare
            report["common"] = audit_compare.compare_common_audits(
                audit_compare.AuditLog(left, require_closed=True),
                audit_compare.AuditLog(right, require_closed=True))
            report["actions"] = action_compare.compare_actions(left_run, right_run)
        except (audit_compare.EvidenceError, action_compare.ActionCompareError,
                OSError, UnicodeError, ValueError) as exc:
            raise CompareError(f"common audit/action evidence is unreadable: {exc}") from exc
    if scope in ("lifecycle", "both"):
        report["lifecycle"] = compare_lifecycle(left, right)
    common_equal = report.get("common", {}).get("equal", True)
    actions_equal = report.get("actions", {}).get("equal", True)
    lifecycle_equal = report.get("lifecycle", {}).get("equal", True)
    report["equal"] = common_equal and actions_equal and lifecycle_equal
    return report


def compare_lifecycle(left: str | Path, right: str | Path) -> dict:
    left, right = Path(left), Path(right)
    left_report = _validate(left)
    right_report = _validate(right)
    left_records, right_records = read_records(left)[0], read_records(right)[0]
    if len(left_records) != len(right_records):
        return {"equal": False, "reason": "record_count",
                "left": len(left_records), "right": len(right_records),
                "normalized": list(IDENTITY_FIELDS)}
    for index, (a, b) in enumerate(zip(left_records, right_records)):
        na, nb = normalize(a), normalize(b)
        if na != nb:
            return {"equal": False, "reason": "record", "index": index,
                    "difference": first_difference(na, nb), "normalized": list(IDENTITY_FIELDS)}
    left_receipt, right_receipt = read_receipt(left), read_receipt(right)
    left_facts = {key: left_receipt.get(key) for key in _RECEIPT_FACT_KEYS}
    right_facts = {key: right_receipt.get(key) for key in _RECEIPT_FACT_KEYS}
    difference = first_difference(left_facts, right_facts)
    if difference:
        return {"equal": False, "reason": "receipt", "difference": difference,
                "normalized": list(IDENTITY_FIELDS)}
    return {"equal": True, "records": len(left_records),
            "normalized": list(IDENTITY_FIELDS),
            "sessions": [left_report["capability"].get("session_id"),
                         right_report["capability"].get("session_id")],
            "counters": left_facts.get("counters"),
            "first_capture_sequence": left_facts.get("first_capture_sequence"),
            "last_capture_sequence": left_facts.get("last_capture_sequence")}
