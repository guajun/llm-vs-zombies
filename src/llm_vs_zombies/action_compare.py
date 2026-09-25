"""Strict action/request/result comparison for the Stage-D arms.

The action evidence is read with the repository's established strict
trajectory reader (``engine_replay._trace_steps``); the native request/result
pairs are cross-checked with ``engine_replay._native_steps`` when the audit
carries engine-call control events. No second generic parser is introduced.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import audit_compare, engine_replay

IDENTITY_KEYS = {"request_id", "run_id", "branch_id", "session_id", "recorded_at"}


class ActionCompareError(ValueError):
    """The action evidence cannot be read or does not satisfy its contract."""


def _normalize(value):
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in sorted(value.items())
                if key not in IDENTITY_KEYS}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


def load_action_transcript(run: str | Path) -> tuple[dict, list[dict]]:
    """Read one run's trace with the strict engine-replay reader."""
    run = Path(run)
    trace = run / "decisions" / "evaluation.jsonl"
    if not trace.is_file():
        raise ActionCompareError(f"action trace is missing: {trace}")
    try:
        initial, steps = engine_replay._trace_steps(trace)
    except engine_replay.EvidenceError as exc:
        raise ActionCompareError(f"action trace is invalid: {exc}") from exc
    marker = run / "replay-initial.json"
    if marker.is_file():
        try:
            declared = json.loads(marker.read_bytes())
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ActionCompareError(f"captured initial marker is unreadable: {exc}") from exc
        declared_version = (declared.get("observation") or {}).get("version") \
            if isinstance(declared.get("observation"), dict) else None
        trace_version = (initial.get("observation") or {}).get("version") \
            if isinstance(initial.get("observation"), dict) else None
        if declared_version != trace_version:
            raise ActionCompareError("action trace initial boundary does not match the captured marker")
    audit_dir = run / "audit"
    if (audit_dir / "manifest.json").is_file():
        try:
            audit = audit_compare.AuditLog(audit_dir, require_closed=True)
        except (audit_compare.EvidenceError, OSError, UnicodeError, ValueError) as exc:
            raise ActionCompareError(f"audit evidence is unreadable: {exc}") from exc
        request_events = [event for event in audit.control_events
                          if event.get("kind") in ("request_started", "request_completed")]
        if request_events:
            native = engine_replay._native_steps(audit)
            source = [step for step in steps
                      if step["request"].get("method") in engine_replay.STEP_METHODS]
            if native != source:
                raise ActionCompareError("native authoritative requests/results differ from the source trace")
    return initial, steps


def compare_action_steps(left_steps: list[dict], right_steps: list[dict]) -> dict:
    """Compare request order/method/params and results semantically."""
    if len(left_steps) != len(right_steps):
        return {"equal": False, "reason": "action_count", "left": len(left_steps),
                "right": len(right_steps),
                "scope": "request order/method/params and results"}
    for index, (left, right) in enumerate(zip(left_steps, right_steps)):
        if left["request"].get("method") != right["request"].get("method"):
            return {"equal": False, "reason": "action_method", "index": index,
                    "left": left["request"].get("method"), "right": right["request"].get("method"),
                    "scope": "request order/method/params and results"}
        difference = audit_compare.first_difference(_normalize(left), _normalize(right))
        if difference:
            return {"equal": False, "reason": "action_or_result", "index": index,
                    "difference": difference, "scope": "request order/method/params and results"}
    return {"equal": True, "steps": len(left_steps),
            "scope": "request order/method/params and results"}


def _outcome(run: Path, initial: dict) -> dict:
    value = {"initialization": _normalize(initial.get("initialization")),
             "observation": _normalize(initial.get("observation"))}
    end = run / "experiment-end.json"
    if end.is_file():
        try:
            value["experiment_end"] = _normalize(json.loads(end.read_bytes()))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ActionCompareError(f"executed endpoint is unreadable: {exc}") from exc
    return value


def compare_actions(left_run: str | Path, right_run: str | Path) -> dict:
    """Compare the two runs' actions, request order/results and outcomes."""
    left_run, right_run = Path(left_run), Path(right_run)
    left_initial, left_steps = load_action_transcript(left_run)
    right_initial, right_steps = load_action_transcript(right_run)
    report = compare_action_steps(left_steps, right_steps)
    if not report["equal"]:
        return report
    difference = audit_compare.first_difference(_outcome(left_run, left_initial),
                                                _outcome(right_run, right_initial))
    if difference:
        return {"equal": False, "reason": "outcome", "difference": difference,
                "scope": "initial observation/recipe and executed endpoint"}
    report["outcome"] = "initial observation/recipe and executed endpoint compared"
    return report
