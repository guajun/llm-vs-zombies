"""Offline contract and validator for the #111 lifecycle recording stream.

This module is the file-contract side of the native measurement layer. It
never imports a game, a Win32 API or the resident runtime; the native recorder
writes ``audit/lifecycle-events.jsonl`` plus an atomic
``audit/lifecycle-close-receipt.jsonl``, and this reader proves what is on
disk.

Deliberate separation of meanings:

* A trajectory without the ``lifecycle_recording`` capability is
  ``unavailable``. That is not zero events and must never be read as success.
* A trajectory whose capability is explicitly disabled is ``disabled``; a
  lifecycle file that exists without an enabled capability is refused.
* An enabled trajectory is only ``valid`` when the close receipt exists, the
  events digest and byte count match the receipt, every record carries the
  receipt's run/session/branch identity, and the capture sequences are
  strictly increasing with no duplicates. Gaps are legitimate (nested
  initialization exists child-first, future capture points share the domain)
  and are reported, never required to be contiguous.
* The receipt is written only after the events stream was flushed, closed and
  checked, so its absence is the fail-closed answer for a crash, a failed
  write or a failed close. An in-memory ``closed`` flag is not evidence.

Only the initialization capture point exists in this delivery. The validator
reports that limit instead of inventing death/removal/recycle facts, and it
never claims the full-window first-kill gate.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import evidence_codec

MODE = "lvz.lifecycle-recording.v1"
ENVELOPE_SCHEMA = "lvz.lifecycle-record.v1"
EVENT_SCHEMA = "lvz.lifecycle-event.v1"
RECEIPT_SCHEMA = "lvz.lifecycle-close-receipt.v1"
VALIDATION_SCHEMA = "lvz.lifecycle-validation.v1"
EVENTS_FILE = "lifecycle-events.jsonl"
RECEIPT_FILE = "lifecycle-close-receipt.jsonl"
SEQUENCE_DOMAIN = "lvz.measurement.capture-sequence"
PROBE_NAME = "zombie-initialize-exit"
PROBE_SCHEMA = "lvz.spawn.v1"
KIND_INITIALIZATION = "zombie_initialized"
CAPABILITY_KEY = "lifecycle_recording"
_MAX_RECORD_BYTES = 32 << 20

# The three capture points that would be needed to conclude anything about
# deaths, removals or recycling are still review_required in the phase-A
# capture table. They are listed here so every report states the limit.
_UNIMPLEMENTED_FACT_CLASSES = ("confirmed_death_stage", "removal_unclassified", "slot_recycle")


class LifecycleError(ValueError):
    """The lifecycle evidence cannot be read or does not satisfy its contract."""


def mode(manifest: dict) -> str:
    """Classify one audit manifest: enabled, disabled or unavailable.

    ``unavailable`` means the trajectory predates the capability and says
    nothing about event counts. A malformed declaration is an error: it must
    not be silently downgraded to unavailable.
    """
    if not isinstance(manifest, dict):
        raise LifecycleError("audit manifest must be an object")
    block = manifest.get(CAPABILITY_KEY)
    if block is None:
        return "unavailable"
    if not isinstance(block, dict):
        raise LifecycleError("lifecycle_recording capability must be an object")
    if block.get("mode") != MODE:
        raise LifecycleError("unsupported lifecycle recording mode")
    enabled = block.get("enabled")
    if type(enabled) is not bool:
        raise LifecycleError("lifecycle_recording.enabled must be a boolean")
    if not enabled:
        return "disabled"
    if block.get("event_schema") != EVENT_SCHEMA or block.get("envelope_schema") != ENVELOPE_SCHEMA:
        raise LifecycleError("lifecycle recording schema declaration mismatch")
    if block.get("receipt_schema") != RECEIPT_SCHEMA:
        raise LifecycleError("lifecycle close-receipt schema declaration mismatch")
    if block.get("sequence_domain") != SEQUENCE_DOMAIN:
        raise LifecycleError("unsupported lifecycle sequence domain")
    session = block.get("session_id")
    if type(session) is not int or session <= 0:
        raise LifecycleError("lifecycle session identity must be a positive integer")
    probe = block.get("probe")
    if (not isinstance(probe, dict) or probe.get("name") != PROBE_NAME or probe.get("schema") != PROBE_SCHEMA
            or probe.get("event_kind") != KIND_INITIALIZATION):
        raise LifecycleError("lifecycle probe declaration mismatch")
    files = block.get("files")
    if (not isinstance(files, dict) or files.get("events") != EVENTS_FILE
            or files.get("close_receipt") != RECEIPT_FILE):
        raise LifecycleError("lifecycle evidence file declaration mismatch")
    build = block.get("build")
    if not _valid_build(build):
        raise LifecycleError("lifecycle build identity declaration mismatch")
    return "enabled"


def _valid_build(build) -> bool:
    return (isinstance(build, dict) and isinstance(build.get("module"), str) and build["module"]
            and _valid_sha256(build.get("sha256")))


def _valid_sha256(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _present(store: evidence_codec.EvidenceStore, name: str) -> bool:
    if store.compressed:
        return store.entry(name) is not None
    return (store.directory / name).is_file()


def _read_plain(store: evidence_codec.EvidenceStore, name: str) -> bytes:
    with store.open(name) as stream:
        return stream.read()


def _scan_probe_events(store: evidence_codec.EvidenceStore) -> dict:
    """Best-effort scan of the existing audit events for explicit probe faults.

    The scan only reads the envelope ``kind`` and, for a closed hook, whether
    its payload was healthy. It is diagnosis, not a second authority: the
    lifecycle receipt remains the success barrier.
    """
    report = {"scanned": False, "fault_events": 0, "closed_unhealthy": False}
    if not _present(store, "events.jsonl"):
        return report
    try:
        with store.open("events.jsonl") as stream:
            while line := stream.readline(_MAX_RECORD_BYTES + 1):
                if len(line) > _MAX_RECORD_BYTES or not line.endswith(b"\n"):
                    break
                try:
                    value = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    break
                if not isinstance(value, dict):
                    break
                kind = value.get("kind")
                if kind in {"spawn_hook_fault", "particle_shake_fault"}:
                    report["fault_events"] += 1
                elif kind == "spawn_hook_closed" and isinstance(value.get("payload"), dict):
                    if value["payload"].get("healthy") is not True:
                        report["closed_unhealthy"] = True
        report["scanned"] = True
    except (OSError, LifecycleError, evidence_codec.CodecError):
        return report
    return report


def _decode_records(data: bytes) -> tuple[list[dict], list[str]]:
    problems: list[str] = []
    records: list[dict] = []
    if data and not data.endswith(b"\n"):
        problems.append("lifecycle events file is truncated: the last record has no trailing newline")
    for number, line in enumerate(data.splitlines(), 1):
        if not line.strip():
            problems.append(f"line {number}: empty lifecycle record")
            continue
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            problems.append(f"line {number}: lifecycle record is not valid JSON: {exc}")
            continue
        if not isinstance(value, dict):
            problems.append(f"line {number}: lifecycle record must be an object")
            continue
        records.append(value)
    return records, problems


def _check_envelope(record: dict, index: int, capability: dict) -> list[str]:
    problems: list[str] = []
    label = f"line {index + 1}"
    if record.get("schema") != ENVELOPE_SCHEMA:
        problems.append(f"{label}: envelope schema is not {ENVELOPE_SCHEMA}")
    if type(record.get("file_seq")) is not int or record["file_seq"] != index:
        problems.append(f"{label}: file_seq must be the written record index {index}")
    for key in ("run_id", "branch_id"):
        value = record.get(key)
        if not isinstance(value, str) or not value:
            problems.append(f"{label}: {key} must identify this run")
    if record.get("session_id") != capability.get("session_id"):
        problems.append(f"{label}: session_id does not match the declared session identity")
    if record.get("sequence_domain") != SEQUENCE_DOMAIN:
        problems.append(f"{label}: sequence_domain does not match the declared session identity")
    event = record.get("event")
    if not isinstance(event, dict):
        problems.append(f"{label}: event must be an object")
        return problems
    _check_event(event, label, problems)
    return problems


def _check_event(event: dict, label: str, problems: list[str]) -> None:
    if event.get("schema") != EVENT_SCHEMA:
        problems.append(f"{label}: event schema is not {EVENT_SCHEMA}")
    if type(event.get("kind")) is not str or not event["kind"]:
        problems.append(f"{label}: event kind must be a non-empty string")
    sequence = event.get("capture_sequence")
    if type(sequence) is not int or sequence <= 0:
        problems.append(f"{label}: capture_sequence must be a positive integer")
    if event.get("complete") is not True:
        problems.append(f"{label}: event is not marked complete")
    probe = event.get("probe")
    if (not isinstance(probe, dict) or probe.get("name") != PROBE_NAME or probe.get("schema") != PROBE_SCHEMA
            or probe.get("sequence_domain") != SEQUENCE_DOMAIN):
        problems.append(f"{label}: event probe declaration does not match the capability")
    phase = event.get("version_phase")
    version = event.get("version")
    call_id = event.get("engine_call_id")
    if phase == "initialization":
        if version is not None or call_id is not None:
            problems.append(f"{label}: initialization event must carry an explicitly unassigned version and engine_call_id")
    elif phase == "controlled_boundary":
        if (not isinstance(version, dict)
                or any(type(version.get(key)) is not int or version[key] < 0 for key in ("epoch", "tick", "revision"))):
            problems.append(f"{label}: controlled event must carry a non-negative version object")
        if call_id is not None and (type(call_id) is not int or call_id <= 0):
            problems.append(f"{label}: engine_call_id must be null or a positive integer")
    else:
        problems.append(f"{label}: unsupported version_phase {phase!r}")
    invocation = event.get("invocation")
    if not isinstance(invocation, dict):
        problems.append(f"{label}: invocation must be an object")
    else:
        invocation_id = invocation.get("invocation_id")
        depth = invocation.get("depth")
        parent = invocation.get("parent_invocation_id")
        if type(invocation_id) is not int or invocation_id <= 0:
            problems.append(f"{label}: invocation_id must be a positive integer")
        if type(depth) is not int or depth < 0:
            problems.append(f"{label}: invocation depth must be a non-negative integer")
        elif depth == 0:
            if parent is not None:
                problems.append(f"{label}: a top-level invocation must have a null parent_invocation_id")
        else:
            if type(parent) is not int or parent <= 0:
                problems.append(f"{label}: a nested invocation must reference its parent")
            elif type(invocation_id) is int and parent >= invocation_id:
                problems.append(f"{label}: parent_invocation_id must be allocated before its child")
    entity = event.get("entity")
    if not isinstance(entity, dict) or type(entity.get("id")) is not int or entity["id"] <= 0:
        problems.append(f"{label}: entity id must be a positive integer")
    else:
        identifier = entity["id"]
        if entity.get("slot") != (identifier & 0xFFFF) or entity.get("generation") != (identifier >> 16):
            problems.append(f"{label}: entity slot/generation do not decompose its id")
    classification = event.get("classification")
    if (not isinstance(classification, dict) or classification.get("class") != "initialization"
            or classification.get("cause") != "unknown"):
        problems.append(f"{label}: this delivery only records initialization facts with cause=unknown")


def _cross_check_receipt(receipt: dict, capability: dict, records: list[dict], events_bytes: bytes,
                         manifest_bytes: bytes | None, problems: list[str]) -> None:
    def require(condition: bool, message: str) -> None:
        if not condition:
            problems.append(message)

    require(receipt.get("schema") == RECEIPT_SCHEMA, f"close receipt schema is not {RECEIPT_SCHEMA}")
    require(receipt.get("completed") is True, "close receipt is not marked completed")
    require(receipt.get("envelope_schema") == ENVELOPE_SCHEMA, "close receipt envelope schema mismatch")
    require(receipt.get("event_schema") == EVENT_SCHEMA, "close receipt event schema mismatch")
    require(receipt.get("sequence_domain") == SEQUENCE_DOMAIN, "close receipt sequence domain mismatch")
    require(receipt.get("session_id") == capability.get("session_id"),
            "close receipt session identity does not match the capability")
    build = receipt.get("build")
    require(_valid_build(build) and build == capability.get("build"),
            "close receipt build identity does not match the capability")
    receipt_probe = receipt.get("probe")
    require(isinstance(receipt_probe, dict) and receipt_probe.get("name") == PROBE_NAME
            and receipt_probe.get("schema") == PROBE_SCHEMA,
            "close receipt probe declaration mismatch")
    require(receipt.get("records") == len(records),
            f"close receipt records={receipt.get('records')!r} does not match {len(records)} parsed records")
    require(receipt.get("bytes") == len(events_bytes), "close receipt byte count does not match the events file")
    digest = hashlib.sha256(events_bytes).hexdigest()
    require(receipt.get("sha256") == digest, "close receipt SHA-256 does not match the events file bytes")
    if manifest_bytes is not None:
        manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
        require(receipt.get("manifest_sha256") == manifest_digest,
                "close receipt manifest_sha256 does not match audit/manifest.json")
    else:
        require(_valid_sha256(receipt.get("manifest_sha256")), "close receipt must bind the audit manifest digest")

    sequences = [record.get("event", {}).get("capture_sequence") for record in records]
    first = sequences[0] if sequences else None
    last = sequences[-1] if sequences else None
    require(receipt.get("first_capture_sequence") == first, "close receipt first capture_sequence mismatch")
    require(receipt.get("last_capture_sequence") == last, "close receipt last capture_sequence mismatch")

    counters = receipt.get("counters")
    if not isinstance(counters, dict):
        problems.append("close receipt must carry the final measurement counters")
    else:
        for key in ("captured", "delivered", "persisted"):
            require(counters.get(key) == len(records), f"close receipt counter {key} does not match the record count")
        for key in ("overflow", "wrong_thread", "nesting_mismatch", "incomplete_events"):
            require(counters.get(key) == 0, f"close receipt counter {key} must be zero for a completed session")
    health = receipt.get("probe_health")
    if not isinstance(health, dict):
        problems.append("close receipt must carry the final probe health")
    else:
        for key in ("faults", "overflow", "wrong_thread_calls"):
            require(health.get(key) == 0, f"close receipt probe health {key} must be zero")
        require(health.get("active_initializers") == 0, "close receipt probe still has an active initializer")
        require(health.get("queued") == 0, "close receipt probe still has queued records")
        require(health.get("healthy") is True, "close receipt probe health is not healthy")
        require(health.get("captured") == len(records), "close receipt probe captured count does not match the record count")


def _identity_problems(records: list[dict], capability: dict, receipt: dict | None,
                       *, check_capability: bool = True) -> list[str]:
    problems: list[str] = []
    first: dict | None = None
    for index, record in enumerate(records):
        identity = {key: record.get(key) for key in ("run_id", "branch_id", "session_id", "sequence_domain")}
        if first is None:
            first = identity
        elif identity != first:
            problems.append(f"line {index + 1}: lifecycle record mixes run/session/branch identities")
            break
    if first is not None:
        if check_capability and first["session_id"] != capability.get("session_id"):
            problems.append("lifecycle records carry a session id that does not match the capability")
        if receipt is not None:
            if first["run_id"] != receipt.get("run_id"):
                problems.append("lifecycle records carry a run id that does not match the close receipt")
            if first["branch_id"] != receipt.get("branch_id"):
                problems.append("lifecycle records carry a branch id that does not match the close receipt")
            if first["sequence_domain"] != receipt.get("sequence_domain"):
                problems.append("lifecycle records carry a sequence domain that does not match the close receipt")
    return problems


def _sequence_problems(records: list[dict]) -> tuple[list[str], dict]:
    problems: list[str] = []
    previous: int | None = None
    previous_index: int | None = None
    first = last = None
    for index, record in enumerate(records):
        sequence = record.get("event", {}).get("capture_sequence")
        if type(sequence) is not int:
            continue
        if first is None:
            first = sequence
        last = sequence
        if previous is not None and sequence <= previous:
            reason = "duplicated" if sequence == previous else "out of order"
            problems.append(f"line {index + 1}: capture_sequence {sequence} is {reason} "
                            f"(after {previous} on line {previous_index + 1})")
        previous, previous_index = sequence, index
    gaps = None if first is None else (last - first + 1) - len(records)
    unique = len({record.get("event", {}).get("capture_sequence") for record in records})
    return problems, {"first_capture_sequence": first, "last_capture_sequence": last,
                      "unique_sequences": unique, "gaps": gaps,
                      "rule": "strictly increasing within one run/session/sequence_domain; gaps allowed, "
                              "duplicates and decreases are refused"}


def _parent_problems(records: list[dict]) -> list[str]:
    problems: list[str] = []
    invocations: dict[int, int] = {}
    for index, record in enumerate(records):
        invocation = record.get("event", {}).get("invocation")
        if isinstance(invocation, dict) and type(invocation.get("invocation_id")) is int:
            invocations[invocation["invocation_id"]] = index
    for index, record in enumerate(records):
        invocation = record.get("event", {}).get("invocation")
        if not isinstance(invocation, dict) or type(invocation.get("depth")) is not int or invocation["depth"] <= 0:
            continue
        parent = invocation.get("parent_invocation_id")
        if type(parent) is int and parent not in invocations:
            problems.append(f"line {index + 1}: parent invocation {parent} has no event in this session")
    return problems


def validate(directory: str | Path, *, manifest: dict | None = None,
             recorder: str | Path | None = None) -> dict:
    """Validate one audit directory and return a JSON-serializable report.

    ``status`` is ``valid``, ``unavailable``, ``disabled`` or ``failed``. A
    malformed capability/file set raises :class:`LifecycleError` instead of
    returning a report, because that is a contract error rather than a
    classification of a trajectory.
    """
    directory = Path(directory)
    if manifest is None:
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            raise LifecycleError(f"audit manifest is missing: {manifest_path}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LifecycleError(f"audit manifest is unreadable: {exc}") from exc
    classification = mode(manifest)
    store = evidence_codec.EvidenceStore(directory, error=LifecycleError)
    block = manifest.get(CAPABILITY_KEY) or {}
    report = {
        "schema": VALIDATION_SCHEMA,
        "status": classification,
        "directory": str(directory),
        "capability": {
            "declared": CAPABILITY_KEY in manifest,
            "mode": block.get("mode"),
            "enabled": block.get("enabled"),
            "session_id": block.get("session_id"),
            "sequence_domain": block.get("sequence_domain"),
            "probe": block.get("probe"),
            "build": block.get("build"),
            "files": block.get("files"),
        },
        "records": {"count": None, "kinds": {}, "identities": [],
                    "first_capture_sequence": None, "last_capture_sequence": None,
                    "unique_sequences": None, "gaps": None, "rule": None},
        "close_receipt": {"present": False, "records": None, "sha256": None},
        "probe_events": {"scanned": False, "fault_events": 0, "closed_unhealthy": False},
        "problems": [],
        "claims": {
            "capture_table": "only zombie_initialize_exit is installed",
            "initialization_facts_only": True,
            "unimplemented_fact_classes": list(_UNIMPLEMENTED_FACT_CLASSES),
            "first_kill_gate": "unverified",
            "first_kill_proven": False,
            "note": "No death, removal or slot-recycle capture point exists yet, and call-internal "
                    "ordering beyond initialization is not measured. This validator reports "
                    "initialization persistence facts and never claims the full-window first kill.",
        },
    }
    if classification == "unavailable":
        stray = [name for name in (EVENTS_FILE, RECEIPT_FILE) if _present(store, name)]
        if stray:
            report["status"] = "failed"
            report["problems"].append("lifecycle evidence exists but the manifest does not declare the capability: "
                                      + ", ".join(stray))
            return report
        report["claims"]["note"] = ("This trajectory predates (or does not declare) the lifecycle "
                                    "recording capability. Its lifecycle state is unavailable: not zero "
                                    "events and not an implicit success. ") + report["claims"]["note"]
        return report
    if classification == "disabled":
        stray = [name for name in (EVENTS_FILE, RECEIPT_FILE) if _present(store, name)]
        if stray:
            report["status"] = "failed"
            report["problems"].append("lifecycle recording is disabled but lifecycle evidence exists: "
                                      + ", ".join(stray))
        return report

    events_present = _present(store, EVENTS_FILE)
    receipt_present = _present(store, RECEIPT_FILE)
    if not events_present:
        report["status"] = "failed"
        report["problems"].append("declared lifecycle recording has no lifecycle-events.jsonl")
        return report
    events_bytes = _read_plain(store, EVENTS_FILE)
    records, problems = _decode_records(events_bytes)
    problems += [problem for index, record in enumerate(records) for problem in _check_envelope(record, index, block)]
    problems += _identity_problems(records, block, None)
    sequence_problems, sequence_summary = _sequence_problems(records)
    problems += sequence_problems
    problems += _parent_problems(records)

    report["records"] = {
        "count": len(records),
        "kinds": _kind_counts(records),
        "identities": sorted({(record.get("run_id"), record.get("branch_id"), record.get("session_id"))
                              for record in records}, key=repr),
        **sequence_summary,
    }
    report["probe_events"] = _scan_probe_events(store)

    manifest_bytes = None
    manifest_path = store.directory / "manifest.json"
    if manifest_path.is_file():
        manifest_bytes = manifest_path.read_bytes()

    if not receipt_present:
        report["status"] = "failed"
        report["problems"] = problems + [
            "lifecycle close receipt is missing: the events stream was not proven flushed and closed "
            "(crash, failed write, failed close or an aborted session)"]
        return report
    receipt_bytes = _read_plain(store, RECEIPT_FILE)
    receipt_records, receipt_decode_problems = _decode_records(receipt_bytes)
    if receipt_decode_problems:
        problems += [f"close receipt: {problem}" for problem in receipt_decode_problems]
    if len(receipt_records) != 1:
        problems.append(f"close receipt must contain exactly one record, found {len(receipt_records)}")
        receipt = receipt_records[0] if receipt_records else {}
    else:
        receipt = receipt_records[0]
    report["close_receipt"] = {"present": True, "records": receipt.get("records"),
                               "sha256": receipt.get("sha256")}
    _cross_check_receipt(receipt, block, records, events_bytes, manifest_bytes, problems)
    problems += _identity_problems(records, block, receipt, check_capability=False)
    problems += _run_identity_bindings(directory, manifest, block, receipt, recorder)
    report["problems"] = problems
    report["status"] = "valid" if not problems else "failed"
    return report


def _kind_counts(records: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for record in records:
        kind = record.get("event", {}).get("kind")
        if isinstance(kind, str):
            counts[kind] = counts.get(kind, 0) + 1
    return counts


def _run_identity_bindings(directory: Path, manifest: dict, capability: dict, receipt: dict,
                           recorder: str | Path | None) -> list[str]:
    """Bind the lifecycle identity to the run and build evidence beside it.

    The run manifest and launcher receipt already exist for experiments; when
    present they must agree with the lifecycle identity. A bare audit
    directory (offline fixture) is still validated on its own bytes.
    """
    problems: list[str] = []
    run_manifest_path = directory.parent / "manifest.json"
    if run_manifest_path.is_file():
        try:
            run_manifest = json.loads(run_manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            run_manifest = None
        if isinstance(run_manifest, dict):
            run_id = run_manifest.get("run_id")
            if isinstance(run_id, str) and run_id and run_id != receipt.get("run_id"):
                problems.append("run manifest run_id does not match the lifecycle close receipt")
            implementation = run_manifest.get("implementation")
            if isinstance(implementation, dict) and isinstance(implementation.get("recorder_sha256"), str):
                if implementation["recorder_sha256"] != capability.get("build", {}).get("sha256"):
                    problems.append("run manifest implementation.recorder_sha256 does not match the "
                                    "declared lifecycle build identity")
    launcher_path = directory.parent / "launcher.json"
    if launcher_path.is_file():
        try:
            launcher = json.loads(launcher_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            launcher = None
        if isinstance(launcher, dict) and isinstance(launcher.get("branch_id"), str):
            if launcher["branch_id"] != receipt.get("branch_id"):
                problems.append("launcher branch identity does not match the lifecycle close receipt")
    if recorder is not None:
        path = Path(recorder)
        if not path.is_file():
            problems.append(f"recorder binary is missing: {path}")
        else:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != capability.get("build", {}).get("sha256"):
                problems.append("recorder binary does not match the declared lifecycle build identity")
    return problems
