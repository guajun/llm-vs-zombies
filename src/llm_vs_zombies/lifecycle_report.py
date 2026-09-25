"""Offline boundary-observation report for issue #111 stage C.

This module consumes an already-audited run (``audit/checksums.jsonl`` +
``audit/state-deltas.jsonl`` through the strict :class:`AuditLog` reader, and
optionally the lifecycle recording) and derives the facts a single offline
command must expose:

* first confirmed death-stage observation (same full slot+generation id, a
  valid live predecessor, then State in {1,2,3});
* first removal observation (disappeared 0->1 or slot disappearance/reuse);
* unknown removals (no death stage observed first) and their coordinates;
* whether a full-window first kill is provable.

The death/removal classification follows the conservative rule registered in
issue #110 (``lvz.issue110-death-stage.v2``): HP<=0, a disappeared flag, a
recycle function or a nearby cannon shot never prove a kill by themselves.
Every derived fact is labelled a **boundary observation**, not production
capture evidence: the three capture points remain ``review_required`` in
``docs/issue111-捕获点表.json``, so ``capture_level`` is always
``unavailable`` in this delivery and ``first_kill.proven`` is always false.

No game, no Win32 and no Agent import: the reader contract lives in
``audit_compare``; this module only interprets reconstructed states.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import lifecycle_events

ANALYSIS_SCHEMA = "lvz.lifecycle-analysis.v1"
# Same conservative predecessor set and stage names as issue #110's rule v2.
FIELDS = {"type": "00000024", "state": "00000028", "hp": "000000c8",
          "armor1": "000000d0", "armor2": "000000dc", "disappeared": "000000ec",
          "at_wave": "0000006c"}
DEAD_STAGES = {1: "falling", 2: "ash", 3: "mower_death_stage"}
KNOWN_NONDEAD = {0, 11, 15, 20, 69, 70, 71, 73, 76}
UNIMPLEMENTED_FACT_CLASSES = ("confirmed_death_stage", "removal_unclassified", "slot_recycle")
# Sites whose store transitions are proven death stages in the locked binary.
# DropLoot is deliberately absent: it is not kill evidence (issue #111 review).
DEATH_CONFIRMING_SITES = frozenset({"phase-dienoloot", "phase-diewithloot", "phase-mowdown", "phase-burn",
                                   "phase-playdeathanim", "phase-zamboni", "phase-catapult"})
NONDEATH_SITES = frozenset({"phase-drop-loot"})
# Health counters that invalidate first-kill proof for the whole stream.
BLOCKING_COUNTERS = ("overflow", "wrong_thread", "faults", "refused_recycle", "unmatched_commits",
                     "pair_mismatch", "overwritten_pending", "incomplete_records")


def analyze_capture_facts(records: list[dict], *, counters: dict | None = None) -> dict:
    """Classify the v2 exact-store facts without dropping any observation.

    The function never folds repeated phase stores and never treats DropLoot as
    kill evidence. First-kill provability is derived from the raw order, so an
    earlier unclassified removal blocks a later claim instead of being hidden.
    """
    events: list[dict] = []
    for record in records:
        event = record.get("event") if isinstance(record, dict) and isinstance(record.get("event"), dict) else record
        if isinstance(event, dict) and event.get("schema") == lifecycle_events.PROBE_EVENT_SCHEMA:
            events.append(event)
    entities: dict[int, dict] = {}
    facts: list[dict] = []

    def entity(identifier: int) -> dict:
        return entities.setdefault(identifier, {"id": identifier, "confirmed_death_stage": False,
                                               "removal_without_death": False, "removal": False,
                                               "recycle_states": [], "phase_transitions": []})

    for event in events:
        identifier = event.get("entity", {}).get("id") if isinstance(event.get("entity"), dict) else None
        if not isinstance(identifier, int):
            continue
        state = entity(identifier)
        kind = event.get("kind")
        sequence = event.get("capture_sequence")
        if kind == "zombie_phase_transition":
            phase = event.get("phase", {})
            site = phase.get("site")
            transition = {"capture_sequence": sequence, "entity": identifier, "kind": kind, "site": site,
                          "before": phase.get("before"), "after": phase.get("after")}
            transitions = state["phase_transitions"]
            transitions.append(transition)
            if site in DEATH_CONFIRMING_SITES and phase.get("before") != phase.get("after"):
                transition["class"] = "confirmed_death_stage"
                state["confirmed_death_stage"] = True
            elif site in NONDEATH_SITES:
                transition["class"] = "nondeath"
            else:
                transition["class"] = "phase_unclassified"
            facts.append(transition)
        elif kind == "zombie_removal_marked":
            removal = {"capture_sequence": sequence, "entity": identifier, "kind": kind,
                       "source": event.get("removal", {}).get("source"),
                       "before": event.get("removal", {}).get("before"),
                       "after": event.get("removal", {}).get("after")}
            state["removal"] = True
            if state["confirmed_death_stage"]:
                removal["class"] = "removal_after_death"
            else:
                removal["class"] = "removal_unclassified"
                state["removal_without_death"] = True
            facts.append(removal)
        elif kind == "zombie_slot_recycle_candidate":
            state["recycle_states"].append("candidate")
            facts.append({"capture_sequence": sequence, "entity": identifier, "kind": kind,
                          "class": "slot_recycle_candidate"})
        elif kind == "zombie_slot_recycle_commit":
            state["recycle_states"].append("committed")
            facts.append({"capture_sequence": sequence, "entity": identifier, "kind": kind,
                          "class": "slot_recycle_commit"})

    confirmed = [fact for fact in facts if fact.get("class") == "confirmed_death_stage"]
    unknown = [fact for fact in facts if fact.get("class") in ("removal_unclassified", "phase_unclassified")]
    counters = counters or {}
    unhealthy = {name: counters.get(name) for name in BLOCKING_COUNTERS
                 if isinstance(counters.get(name), int) and counters.get(name) > 0}
    reasons: list[str] = []
    blocking: list[dict] = []
    first: dict | None = None
    if not confirmed:
        reasons.append("no capture fact shows a death-confirming phase store")
    else:
        first = confirmed[0]
        blocking = [fact for fact in unknown if fact["capture_sequence"] < first["capture_sequence"]]
        if blocking:
            reasons.append("an earlier unclassified fact blocks first-kill proof")
    if unhealthy:
        reasons.append("probe health counters are not clean: " + ", ".join(sorted(unhealthy)))
    proven = bool(first) and not blocking and not unhealthy
    return {
        "facts": facts,
        "entities": {identifier: state for identifier, state in sorted(entities.items())},
        "summary": {
            "fact_count": len(facts),
            "confirmed_death_stages": len(confirmed),
            "unclassified_facts": len(unknown),
            "removals": sum(1 for fact in facts if fact["kind"] == "zombie_removal_marked"),
            "recycle_candidates": sum(1 for fact in facts if fact["kind"] == "zombie_slot_recycle_candidate"),
            "recycle_commits": sum(1 for fact in facts if fact["kind"] == "zombie_slot_recycle_commit"),
        },
        "first_kill": {
            "proven": proven,
            "entity": None if not (proven and first) else first["entity"],
            "capture_sequence": None if not (proven and first) else first["capture_sequence"],
            "blocking_facts": [{"capture_sequence": fact["capture_sequence"], "class": fact["class"],
                                "entity": fact["entity"]} for fact in blocking],
            "reasons": reasons,
        },
        "unknown_facts": unknown,
    }


def first_kill_assessment(coverage: dict | None) -> dict:
    """Report only the guarantees this delivery actually has.

    Boundary samples can never prove a full-window first kill unless the
    initialization capture is installed *and* death/removal/recycle capture
    points exist; call-internal ordering is missing in every case.
    """
    coverage = coverage or {}
    capture = coverage.get("initialization_capture", "not declared")
    reasons: list[str] = []
    if capture == "installed":
        reasons.append("ZombieInitialize exit capture is installed and validated for initialization facts, "
                       "but no death/removal/recycle capture point is installed, so removals are inferred "
                       "only at boundary resolution")
    else:
        reasons.append(f"initialization exit capture is {capture}: objects created and destroyed between "
                       "boundary samples are not excluded")
    reasons.append("multiple changes inside one boundary cannot be ordered (no call-internal ordering)")
    reasons.append("boundary observations cannot exclude an earlier unobserved removal before the first "
                   "sampled boundary")
    reasons.append("death-stage boundary observations are not production capture facts")
    return {"gate": "unverified", "proven": False, "reasons": reasons,
            "initialization_capture": capture}


class ReportError(ValueError):
    """The audit evidence cannot be read for a report."""


def signed(word: int) -> int:
    return word - 2**32 if word >= 2**31 else word


def snapshot_from_frame(frame, line: int) -> dict:
    """Turn one AuditFrame into a compact snapshot the analyzer understands."""
    state = frame.state if isinstance(frame.state, dict) else {}
    pool = state.get("zombies") if isinstance(state, dict) else None
    slots = pool.get("slots") if isinstance(pool, dict) else None
    zombies: dict[str, dict] = {}
    problems: list[str] = []
    if not isinstance(slots, dict):
        problems.append("missing_zombie_pool")
    else:
        for slot, entry in sorted(slots.items(), key=lambda pair: int(pair[0])):
            if not isinstance(entry, dict):
                problems.append("invalid_slot_entry")
                continue
            identity = entry.get("id_or_free_next")
            fields = entry.get("fields")
            allocated = type(identity) is int and identity >> 16 != 0
            if not allocated:
                continue
            if not isinstance(fields, dict) or identity & 0xFFFF != int(slot):
                problems.append("invalid_identity_or_missing_fields")
                continue
            raw = {name: fields.get(offset) for name, offset in FIELDS.items()}
            zombies[str(slot)] = {"id": identity, "slot": int(slot), "generation": identity >> 16,
                                  "raw": raw, "pointer": f"/zombies/slots/{slot}"}
    payload = frame.payload if isinstance(frame.payload, dict) else {}
    call = payload.get("engine_call") if isinstance(payload.get("engine_call"), dict) else {}
    coordinate = {"line": line, "seq": frame.seq, "kind": frame.kind, "version": frame.version,
                  "request_id": payload.get("request_id"),
                  "engine_call_id": call.get("engine_call_id"),
                  "native_clock_before": call.get("native_clock_before"),
                  "native_clock_after": call.get("native_clock_after")}
    return {"coordinate": coordinate, "zombies": zombies, "problems": problems}


def snapshots_from_audit(directory: str | Path, *, require_closed: bool = True):
    """Strictly read an audit directory and produce ordered snapshots."""
    from . import audit_compare
    directory = Path(directory)
    try:
        audit = audit_compare.AuditLog(directory, require_closed=require_closed)
    except (audit_compare.EvidenceError, OSError, UnicodeError, ValueError) as exc:
        raise ReportError(f"audit evidence is not readable: {exc}") from exc
    snapshots = [snapshot_from_frame(frame, index + 1) for index, frame in enumerate(audit.frames)]
    return audit.manifest, snapshots


def _fact(entity: dict, coordinate: dict, before: dict | None, after: dict | None, **extra) -> dict:
    value = {"kind": "boundary_observation", "entity": {"id": entity["id"], "slot": entity["slot"],
                                                        "generation": entity["generation"]},
             "coordinates": [coordinate], "before": before, "after": after}
    value.update(extra)
    return value


def _state(entry: dict | None):
    if not isinstance(entry, dict):
        return None
    raw = entry.get("raw")
    if not isinstance(raw, dict) or any(type(value) is not int for value in raw.values()):
        return None
    if raw["disappeared"] not in (0, 1):
        return None
    return raw


def analyze_snapshots(snapshots: list[dict], *, coverage: dict | None = None) -> dict:
    """Derive first-death/first-removal/unknown facts from ordered snapshots.

    One pass, no retention of full states: only the previous boundary's entity
    facts are kept. Coordinates are the audit envelope coordinates of the
    boundary where the change was observed.
    """
    if not isinstance(snapshots, list):
        raise ReportError("snapshots must be a list")
    previous: dict[int, dict] = {}
    retired: set[int] = set()
    confirmed: set[int] = set()
    initial_ids: set[int] = set()
    deaths: list[dict] = []
    confirmed_deaths: list[dict] = []
    removals: list[dict] = []
    removal_events: list[dict] = []
    unknown_removals: list[dict] = []
    disappearance_flags: list[dict] = []
    slot_reuse: list[dict] = []
    initial_residue: list[dict] = []
    problems: list[str] = []
    phase_counts: dict[str, int] = {}
    first_coordinate = None
    last_coordinate = None
    for index, snapshot in enumerate(snapshots):
        if not isinstance(snapshot, dict):
            problems.append("snapshot must be an object")
            continue
        coordinate = snapshot.get("coordinate") if isinstance(snapshot.get("coordinate"), dict) else {}
        if first_coordinate is None:
            first_coordinate = coordinate
        last_coordinate = coordinate
        for problem in snapshot.get("problems") or []:
            problems.append(f"boundary {index + 1}: {problem}")
        zombies = snapshot.get("zombies") if isinstance(snapshot.get("zombies"), dict) else {}
        current: dict[int, dict] = {}
        for entry in zombies.values():
            if not isinstance(entry, dict) or type(entry.get("id")) is not int:
                continue
            identity = entry["id"]
            raw = _state(entry)
            current[identity] = entry
            if raw is None:
                problems.append(f"boundary {index + 1}: invalid raw fields for entity {identity}")
                continue
            phase_counts[str(raw["state"])] = phase_counts.get(str(raw["state"]), 0) + 1
            prior = previous.get(identity)
            prior_raw = _state(prior) if prior else None
            if identity in retired:
                problems.append(f"boundary {index + 1}: retired entity {identity} reappeared")
            if index == 0:
                classification = ("preexisting_disappeared" if raw["disappeared"]
                                  else "preexisting_death" if raw["state"] in DEAD_STAGES
                                  else "initial_live_or_unknown")
                if raw["disappeared"] or raw["state"] in DEAD_STAGES or signed(raw["at_wave"]) < 0:
                    initial_ids.add(identity)
                    initial_residue.append(_fact(entry, coordinate, None, raw,
                                                 classification=classification,
                                                 reason="present at the first sampled boundary; not a window event"))
            if raw["state"] in DEAD_STAGES and (prior_raw is None or prior_raw["state"] not in DEAD_STAGES):
                fact = _fact(entry, coordinate, prior_raw, raw, stage=DEAD_STAGES[raw["state"]],
                             cause="unverified")
                deaths.append(fact)
                if (prior_raw is not None and prior_raw["state"] in KNOWN_NONDEAD and prior_raw["disappeared"] == 0
                        and signed(prior_raw["at_wave"]) >= 0 and signed(raw["at_wave"]) >= 0):
                    fact["confirmed"] = True
                    confirmed_deaths.append(fact)
                    confirmed.add(identity)
                else:
                    fact["confirmed"] = False
                    fact["reason"] = "death stage observed without a verified live predecessor"
            if (prior_raw is not None and prior_raw["disappeared"] == 0 and raw["disappeared"] == 1
                    and identity not in confirmed):
                fact = _fact(entry, coordinate, prior_raw, raw, channel="disappeared_flag",
                             classification="without_confirmed_death",
                             reason="disappeared flag set without a prior death-stage observation")
                disappearance_flags.append(fact)
                removal_events.append(fact)
                unknown_removals.append(fact)
            elif prior_raw is not None and prior_raw["disappeared"] == 0 and raw["disappeared"] == 1:
                removal_events.append(_fact(entry, coordinate, prior_raw, raw, channel="disappeared_flag",
                                            classification="after_confirmed_death",
                                            reason="disappeared flag set after a confirmed death stage"))
        for identity, before in previous.items():
            if identity in current:
                continue
            retired.add(identity)
            before_raw = _state(before)
            if identity in initial_ids:
                initial_residue.append(_fact(before, coordinate, before_raw, None,
                                             classification="initial_residue_release",
                                             reason="cold-start residue leaving the pool; not a window removal"))
                continue
            classification = "release_after_confirmed_death" if identity in confirmed else "unknown_removal"
            fact = _fact(before, coordinate, before_raw, None, channel="slot_release",
                         classification=classification,
                         reason="slot no longer held this full identity at the boundary")
            removals.append(fact)
            removal_events.append(fact)
            if classification == "unknown_removal":
                unknown_removals.append(fact)
        # Same slot with a different full id is a reuse we did not observe end-to-end.
        previous_by_slot = {entry["slot"]: entry for entry in previous.values()}
        for entry in current.values():
            prior = previous_by_slot.get(entry["slot"])
            if prior is not None and prior["id"] != entry["id"]:
                slot_reuse.append(_fact(entry, coordinate,
                                        _state(prior), _state(entry),
                                        classification="unobserved_reuse",
                                        previous_identity=prior["id"],
                                        reason="slot changed generation between sampled boundaries"))
        previous = current
    first_confirmed = confirmed_deaths[0] if confirmed_deaths else None
    first_removal = removal_events[0] if removal_events else None
    first_unknown = unknown_removals[0] if unknown_removals else None
    return {
        "schema": ANALYSIS_SCHEMA,
        "window": {"boundaries": len(snapshots), "first_coordinate": first_coordinate,
                   "last_coordinate": last_coordinate},
        "facts": {
            "first_confirmed_death": first_confirmed or {"available": False,
                                                         "reason": "no confirmed death-stage observation in the sampled window"},
            "first_removal": first_removal or {"available": False,
                                               "reason": "no removal observed in the sampled window"},
            "unknown_removals": {"count": len(unknown_removals), "first": first_unknown,
                                 "rule": "disappeared 0->1 or slot release without a prior confirmed "
                                         "death-stage observation"},
            "disappeared_without_confirmed_death": {
                "count": len(disappearance_flags), "first": disappearance_flags[0] if disappearance_flags else None,
                "rule": "disappeared 0->1 without a prior death-stage observation"},
            "removal_events": {"count": len(removal_events),
                               "channels": {"disappeared_flag": len(disappearance_flags),
                                            "slot_release": len(removals)}},
            "death_stage_observations": deaths,
            "removals": removals,
            "slot_reuse": {"count": len(slot_reuse), "first": slot_reuse[0] if slot_reuse else None},
            "initial_residue": initial_residue,
        },
        "capture_level": {
            "death": "unavailable", "removal": "unavailable", "recycle": "unavailable",
            "reason": "zombie-death-stage-enter / zombie-removal-unclassified / zombie-slot-recycled "
                      "are review_required in docs/issue111-捕获点表.json; this report is boundary observation only",
            "unimplemented_fact_classes": list(UNIMPLEMENTED_FACT_CLASSES),
        },
        "first_kill": first_kill_assessment(coverage),
        "coverage": coverage or {},
        "state_phase_counts": phase_counts,
        "problems": problems,
    }


def report_for_run(run: str | Path, *, require_closed: bool = True,
                   lifecycle: bool = True) -> dict:
    """Read a run (or audit) directory and return the full report."""
    run = Path(run)
    audit_directory = run / "audit" if (run / "audit" / "manifest.json").is_file() else run
    manifest, snapshots = snapshots_from_audit(audit_directory, require_closed=require_closed)
    spawn_hook = manifest.get("spawn_hook") if isinstance(manifest.get("spawn_hook"), dict) else {}
    coverage = {"audit_manifest_target": manifest.get("target"),
                "source": "boundary samples",
                "initialization_capture": "installed" if spawn_hook.get("installed") is True else "not installed"}
    report = analyze_snapshots(snapshots, coverage=coverage)
    report["source"] = {"directory": str(audit_directory), "target": manifest.get("target"),
                        "boundaries": len(snapshots)}
    if lifecycle:
        from . import lifecycle_events
        try:
            lifecycle_report = lifecycle_events.validate(audit_directory, manifest=manifest,
                                                         require_close=False)
        except lifecycle_events.LifecycleError as exc:
            lifecycle_report = {"schema": lifecycle_events.VALIDATION_SCHEMA, "status": "invalid",
                                "problems": [str(exc)]}
        report["lifecycle"] = {"schema": lifecycle_report.get("schema"),
                               "status": lifecycle_report.get("status"),
                               "records": (lifecycle_report.get("records") or {}).get("count"),
                               "close_receipt_present": (lifecycle_report.get("close_receipt") or {}).get("present"),
                               "problems": lifecycle_report.get("problems") or []}
        report["coverage"]["lifecycle_recording"] = lifecycle_report.get("status")
        if lifecycle_report.get("status") == "failed":
            report["problems"].extend(f"lifecycle: {problem}" for problem in lifecycle_report["problems"])
    return report


def markdown_report(report: dict) -> str:
    """Render the derived report for review; numbers are copied, not summarized away."""
    facts = report.get("facts") or {}
    death = facts.get("first_confirmed_death") or {}
    removal = facts.get("first_removal") or {}
    unknown = facts.get("unknown_removals") or {}
    first_kill = report.get("first_kill") or {}
    lines = ["# #111 lifecycle boundary report", "",
             f"- schema: `{report.get('schema')}`",
             f"- source: `{(report.get('source') or {}).get('directory')}`",
             f"- boundaries: {(report.get('window') or {}).get('boundaries')}",
             f"- first confirmed death: {json.dumps(death, ensure_ascii=False, sort_keys=True)}",
             f"- first removal: {json.dumps(removal, ensure_ascii=False, sort_keys=True)}",
             f"- unknown removals: count={unknown.get('count')}",
             f"- first kill proven: {first_kill.get('proven')}",
             f"- capture level: {json.dumps(report.get('capture_level'), ensure_ascii=False, sort_keys=True)}",
             f"- lifecycle: {json.dumps(report.get('lifecycle'), ensure_ascii=False, sort_keys=True)}", ""]
    if report.get("problems"):
        lines.append("## Problems")
        lines.extend(f"- {problem}" for problem in report["problems"])
        lines.append("")
    lines.append("All facts are boundary observations; death/removal/recycle capture points remain "
                 "review_required and first_kill remains unverified.")
    return "\n".join(lines) + "\n"


def write_report(report: dict, *, json_path: str | Path | None = None,
                 markdown_path: str | Path | None = None) -> None:
    if json_path is not None:
        Path(json_path).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if markdown_path is not None:
        Path(markdown_path).write_text(markdown_report(report), encoding="utf-8")
