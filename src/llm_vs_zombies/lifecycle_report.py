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
Boundary facts stay labelled **boundary observations**. When the run also
carries a validated exact-store lifecycle stream (v2 events from the installed
phase/mDead/recycle probes), ``capture_level`` reports those capture facts and
their limits instead of a blanket ``unavailable``; the first-kill gate is only
reported as proven with the close receipt, probe capability, initialization
stream, frozen full window and clean counters all verified.

No game, no Win32 and no Agent import: the reader contract lives in
``audit_compare``; this module only interprets reconstructed states.
"""
from __future__ import annotations

import hashlib
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
# The five frozen phase stores and the raw value each one writes. DropLoot is
# not a phase store and is never death evidence.
DEATH_PHASE_BY_SITE = {
    "phase-playdeathanim": 1, "phase-applyburn": 2, "phase-mowdown": 3,
    "phase-catapult": 1, "phase-zamboni": 1,
}
DEATH_STAGES = {1: "falling", 2: "ash", 3: "mower_death_stage"}
# Locked-f966 ApplyBurn -> DieWithLoot -> DieNoLoot chain: the two return
# addresses that a validated bounded frame fact must show. Generic mDead or a
# bare DieNoLoot/DieWithLoot observation is never a death by itself.
APPLYBURN_CHAIN_RETURNS = {"return_into_diewithloot": 0x5302FF,
                           "return_into_applyburn": 0x532FC7}
NONDEATH_SITES = frozenset({"phase-drop-loot"})
# Health counters that invalidate completeness and first-kill proof. live_skips
# is the expected live guard branch and stays benign; off-board facts are
# published (not dropped) and are never a read/classification failure.
BLOCKING_COUNTERS = ("overflow", "wrong_thread", "faults", "unmatched_commits", "pair_mismatch",
                     "overwritten_pending", "read_failed", "classify_refused", "inactive_suppressed")
BENIGN_COUNTERS = ("live_skips",)


def _attach_predecessor(fact: dict, initialization: dict, initial_entities: dict,
                        event: dict, *, removal: bool) -> None:
    """Correlate a captured fact with an earlier initialization or residue.

    The initialization map only contains records that appeared *earlier* in
    capture_sequence order, so a nested initializer captured after a death can
    never be used as prior known-live state. Initial residue carries the raw
    phase/dead state from the first sampled boundary, not merely membership.
    """
    identifier = fact.get("entity")
    init = initialization.get(identifier)
    if init is not None:
        fact["initialization"] = {"capture_sequence": init["capture_sequence"],
                                  "invocation_id": init["invocation_id"],
                                  "invocation_depth": init.get("invocation_depth"),
                                  "parent_invocation_id": init.get("parent_invocation_id"),
                                  "engine_call_id": init["engine_call_id"]}
        fact["predecessor"] = "initialization"
        call = event.get("engine_call_id")
        # Same-call lifetime is only meaningful for an actual removal.
        if removal and call is not None and init.get("engine_call_id") is not None \
                and call == init["engine_call_id"]:
            fact["same_call_lifetime"] = True
        return
    residue = initial_entities.get(identifier)
    if residue is not None:
        fact["initial_residue"] = dict(residue)
        state = residue.get("state")
        if residue.get("disappeared") or (isinstance(state, int) and state in DEATH_STAGES):
            fact["predecessor"] = "initial_residue_doomed"
        else:
            fact["predecessor"] = "initial_residue"
        return
    fact["predecessor"] = "unknown_predecessor"


def analyze_capture_facts(records: list[dict], *, counters: dict | None = None,
                          coverage: dict | None = None) -> dict:
    """Classify the v2 exact-store facts without dropping any observation.

    The function never folds repeated phase stores and never treats DropLoot as
    kill evidence. A first-kill claim additionally needs evidence that the
    capture itself was complete: a valid close receipt, the enabled probe
    capability, the initialization capture, a full frozen window and clean
    counters. Off-board preview facts are preserved and scoped out of the
    gameplay first-kill proof instead of being dropped or counted as blockers.
    """
    coverage = coverage or {}
    initial_entities = coverage.get("initial_entities") if isinstance(coverage, dict) else None
    initial_entities = initial_entities if isinstance(initial_entities, dict) else {}
    events: list[tuple[int, dict]] = []
    for index, record in enumerate(records):
        event = record.get("event") if isinstance(record, dict) and isinstance(record.get("event"), dict) else record
        if isinstance(event, dict):
            events.append((index, event))
    events.sort(key=lambda item: (item[1].get("capture_sequence") if isinstance(
        item[1].get("capture_sequence"), int) else item[0]))
    entities: dict[int, dict] = {}
    facts: list[dict] = []
    initialization: dict[int, dict] = {}

    def entity(identifier: int) -> dict:
        return entities.setdefault(identifier, {"id": identifier, "confirmed_death_stage": False,
                                               "removal_without_death": False, "removal": False,
                                               "recycle_states": [], "phase_transitions": []})

    for _, event in events:
        if event.get("schema") == lifecycle_events.EVENT_SCHEMA \
                and event.get("kind") == lifecycle_events.KIND_INITIALIZATION:
            data = event.get("entity") if isinstance(event.get("entity"), dict) else {}
            identifier = data.get("id")
            if isinstance(identifier, int):
                invocation = event.get("invocation") if isinstance(event.get("invocation"), dict) else {}
                initialization[identifier] = {
                    "capture_sequence": event.get("capture_sequence"),
                    "invocation_id": invocation.get("invocation_id"),
                    "invocation_depth": invocation.get("depth"),
                    "parent_invocation_id": invocation.get("parent_invocation_id"),
                    "engine_call_id": event.get("engine_call_id"),
                }
            continue
        if event.get("schema") != lifecycle_events.PROBE_EVENT_SCHEMA:
            continue
        data = event.get("entity") if isinstance(event.get("entity"), dict) else {}
        identifier = data.get("id")
        if not isinstance(identifier, int):
            continue
        state = entity(identifier)
        kind = event.get("kind")
        sequence = event.get("capture_sequence")
        obj = event.get("object") if isinstance(event.get("object"), dict) else {}
        scope = "gameplay" if obj.get("on_board") is not False else "preview"
        if kind == "zombie_phase_transition":
            phase = event.get("phase", {})
            site = phase.get("site")
            transition = {"capture_sequence": sequence, "entity": identifier, "kind": kind, "site": site,
                          "before": phase.get("before"), "after": phase.get("after"), "scope": scope}
            state["phase_transitions"].append(transition)
            expected = DEATH_PHASE_BY_SITE.get(site)
            before, after = phase.get("before"), phase.get("after")
            if expected is not None and after == expected and before not in DEATH_STAGES \
                    and before != after:
                transition["class"] = "confirmed_death_stage"
                transition["stage"] = DEATH_STAGES.get(expected)
                state["confirmed_death_stage"] = True
                _attach_predecessor(transition, initialization, initial_entities, event, removal=False)
            elif expected is not None and after == expected and before in DEATH_STAGES:
                transition["class"] = "already_dying"
            elif site in NONDEATH_SITES:
                transition["class"] = "nondeath"
            else:
                transition["class"] = "phase_unclassified"
            facts.append(transition)
        elif kind == "zombie_removal_marked":
            removal = {"capture_sequence": sequence, "entity": identifier, "kind": kind,
                       "source": event.get("removal", {}).get("source"),
                       "before": event.get("removal", {}).get("before"),
                       "after": event.get("removal", {}).get("after"), "scope": scope}
            state["removal"] = True
            _attach_predecessor(removal, initialization, initial_entities, event, removal=True)
            frame = event.get("removal", {}).get("frame") if isinstance(event.get("removal"), dict) else None
            chain_valid = (isinstance(frame, dict) and frame.get("callsite_bytes_match") is True
                           and frame.get("return_into_diewithloot")
                           == APPLYBURN_CHAIN_RETURNS["return_into_diewithloot"]
                           and frame.get("return_into_applyburn")
                           == APPLYBURN_CHAIN_RETURNS["return_into_applyburn"]
                           and removal["before"] == 0 and removal["after"] == 1 and scope == "gameplay")
            if state["confirmed_death_stage"]:
                removal["class"] = "removal_after_death"
            elif chain_valid:
                removal["class"] = "confirmed_death_path"
                removal["death_path"] = "applyburn_diewithloot_dienoloot"
                removal["death_stage"] = "charred_animation_via_applyburn"
                state["confirmed_death_stage"] = True
            else:
                removal["class"] = "removal_unclassified"
                state["removal_without_death"] = True
            facts.append(removal)
        elif kind == "zombie_slot_recycle_candidate":
            state["recycle_states"].append("candidate")
            facts.append({"capture_sequence": sequence, "entity": identifier, "kind": kind,
                          "class": "slot_recycle_candidate", "scope": scope})
        elif kind == "zombie_slot_recycle_commit":
            state["recycle_states"].append("committed")
            facts.append({"capture_sequence": sequence, "entity": identifier, "kind": kind,
                          "class": "slot_recycle_commit", "scope": scope})

    preview_facts = [fact for fact in facts if fact.get("scope") == "preview"]
    gameplay_facts = [fact for fact in facts if fact.get("scope") != "preview"]
    confirmed = [fact for fact in gameplay_facts
                 if fact.get("class") in ("confirmed_death_stage", "confirmed_death_path")]
    unknown = [fact for fact in gameplay_facts
               if fact.get("class") in ("removal_unclassified", "phase_unclassified")]
    unknown_predecessors = [fact for fact in confirmed
                            if fact.get("predecessor") in ("unknown_predecessor",
                                                           "initial_residue_doomed")]
    unknown += unknown_predecessors
    candidates = [fact for fact in confirmed
                  if fact.get("predecessor") in ("initialization", "initial_residue")]
    counters = counters or {}
    coverage = coverage or {}
    unhealthy = {name: counters.get(name) for name in BLOCKING_COUNTERS
                 if isinstance(counters.get(name), int) and counters.get(name) > 0}
    reasons: list[str] = []
    blocking: list[dict] = []
    first: dict | None = None
    if not coverage.get("receipt_valid"):
        reasons.append("no valid close receipt proves the capture stream was complete")
    if not coverage.get("probe_capability"):
        reasons.append("the exact-store probe capability is not proven installed and healthy")
    initialization_capture = bool(coverage.get("initialization_capture")) or bool(initialization)
    if not initialization_capture:
        reasons.append("the initialization capture is not proven installed")
    if not coverage.get("full_window"):
        reasons.append("a full frozen window with both endpoints is not proven")
    if coverage.get("health_clean") is not True:
        reasons.append("clean probe counters are not asserted by the evidence")
    if not confirmed:
        reasons.append("no capture fact shows a death-confirming phase store")
    else:
        first = candidates[0] if candidates else None
        if unknown_predecessors:
            reasons.append("a death fact has no earlier initialization or live initial-residue predecessor")
        if first is not None:
            blocking = [fact for fact in unknown
                        if isinstance(fact.get("capture_sequence"), int)
                        and isinstance(first.get("capture_sequence"), int)
                        and fact["capture_sequence"] < first["capture_sequence"]]
            if blocking:
                reasons.append("an earlier unclassified fact blocks first-kill proof")
    if unhealthy:
        reasons.append("probe health counters are not clean: " + ", ".join(sorted(unhealthy)))
    prerequisites = (coverage.get("receipt_valid") is True and coverage.get("probe_capability") is True
                     and initialization_capture and coverage.get("full_window") is True
                     and coverage.get("health_clean") is True)
    proven = bool(first) and not blocking and not unhealthy and prerequisites
    return {
        "facts": facts,
        "entities": {identifier: state for identifier, state in sorted(entities.items())},
        "summary": {
            "fact_count": len(facts),
            "gameplay_facts": len(gameplay_facts),
            "preview_facts": len(preview_facts),
            "confirmed_death_stages": len(confirmed),
            "unclassified_facts": len(unknown),
            "initialization_facts": len(initialization),
            "same_call_lifetimes": sum(1 for fact in facts if fact.get("same_call_lifetime")),
            "unknown_predecessors": len(unknown_predecessors),
            "removals": sum(1 for fact in facts if fact["kind"] == "zombie_removal_marked"),
            "recycle_candidates": sum(1 for fact in facts if fact["kind"] == "zombie_slot_recycle_candidate"),
            "recycle_commits": sum(1 for fact in facts if fact["kind"] == "zombie_slot_recycle_commit"),
        },
        "first_kill": {
            "proven": proven,
            "prerequisites": {"receipt_valid": bool(coverage.get("receipt_valid")),
                              "probe_capability": bool(coverage.get("probe_capability")),
                              "initialization_capture": initialization_capture,
                              "full_window": bool(coverage.get("full_window")),
                              "health_clean": bool(coverage.get("health_clean"))},
            "entity": None if not (proven and first) else first["entity"],
            "capture_sequence": None if not (proven and first) else first["capture_sequence"],
            "blocking_facts": [{"capture_sequence": fact["capture_sequence"], "class": fact["class"],
                                "entity": fact["entity"]} for fact in blocking],
            "reasons": reasons,
        },
        "preview_facts": preview_facts,
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


def load_capture_facts(audit_directory: str | Path) -> list[dict]:
    """Read the envelope records so the capture analyzer sees the real stream.

    The shared codec reader resolves plain and sealed (gzip) evidence, so a
    sealed run yields exactly the same facts as before compression. Decode
    errors propagate; they are never turned into an empty success.
    """
    from . import lifecycle_events
    return lifecycle_events.load_records(Path(audit_directory))


def _normalized_plan(plan_path: Path) -> dict | None:
    """Normalize a raw frozen plan through the repository's Plan contract."""
    try:
        from dataclasses import asdict

        from . import evaluation
        return json.loads(json.dumps(asdict(evaluation.Plan.load(plan_path))))
    except Exception:  # a plan outside the frozen contract stays unexplained
        return None


def _plan_binding_paths(run_directory: Path, suite_directory: Path,
                        explicit: str | Path | None) -> list[Path]:
    paths: list[Path] = []
    if explicit is not None:
        paths.append(Path(explicit))
    paths.append(suite_directory / "lifecycle-plan-binding.json")
    paths.append(run_directory / "lifecycle-plan-binding.json")
    return paths


def _full_window_evidence(run_directory: Path, audit_directory: Path, snapshots: list[dict],
                         plan: str | Path | None,
                         plan_binding: str | Path | None = None) -> tuple[bool, list[str]]:
    """Prove the frozen plan, the raw identity binding, the captured initial
    root and the executed endpoint all belong to the same closed run.

    A constant is never flipped here: every clause reads real evidence, and a
    missing binding stays an explicit blocker.
    """
    if plan is None:
        return False, ["no frozen plan was supplied to bind the window"]
    plan_path = Path(plan)
    if not plan_path.is_file():
        return False, [f"frozen plan is unreadable: {plan_path}"]
    try:
        plan_bytes = plan_path.read_bytes()
        plan_doc = json.loads(plan_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return False, [f"frozen plan is unreadable: {exc}"]
    normalized = _normalized_plan(plan_path)
    if normalized is None:
        return False, ["the frozen plan does not satisfy the evaluation Plan contract"]
    name = run_directory.name
    suite_name = name.rsplit("-s", 1)[0] if "-s" in name else name
    suite_directory = run_directory.parent / suite_name if "-s" in name else run_directory
    suite_plan = suite_directory / "plan.json"
    if not suite_plan.is_file() and (run_directory / "plan.json").is_file():
        suite_plan = run_directory / "plan.json"
    if not suite_plan.is_file():
        return False, [f"suite plan copy is missing: {suite_plan}"]
    try:
        suite_doc = json.loads(suite_plan.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return False, [f"suite plan copy is unreadable: {exc}"]
    if suite_doc != normalized:
        return False, ["the suite plan copy does not match the normalized frozen plan"]
    binding: dict | None = None
    for candidate in _plan_binding_paths(run_directory, suite_directory, plan_binding):
        if candidate.is_file():
            try:
                binding = json.loads(candidate.read_bytes())
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                return False, [f"plan identity binding is unreadable: {exc}"]
            binding["_path"] = str(candidate)
            break
    if binding is None:
        return False, ["the raw plan identity binding is missing"]
    digest = hashlib.sha256(plan_bytes).hexdigest()
    if binding.get("raw_plan_sha256") != digest:
        return False, ["the raw plan identity binding does not match the frozen plan bytes"]
    seed = None
    if "-s" in name:
        seed_text = name.rsplit("-s", 1)[1].split("-", 1)[0]
        try:
            seed = int(seed_text)
        except ValueError:
            return False, [f"the child run name has no usable seed: {name}"]
        if normalized.get("seeds") and seed not in normalized["seeds"]:
            return False, [f"the child run seed {seed} is not in the frozen plan"]
    manifest_path = run_directory / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_bytes())
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return False, [f"run manifest is unreadable: {exc}"]
        if manifest.get("run_id") != name:
            return False, ["the run manifest identity does not match the child directory"]
        pin = binding.get("recorder_sha256")
        implementation = manifest.get("implementation")
        build = implementation.get("recorder_sha256") if isinstance(implementation, dict) else None
        if pin and build != pin:
            return False, ["the run manifest recorder build does not match the pinned build"]
    initial_path = run_directory / "replay-initial.json"
    if not initial_path.is_file():
        return False, ["the run has no captured initial boundary"]
    try:
        initial = json.loads(initial_path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return False, [f"the captured initial boundary is unreadable: {exc}"]
    observation = initial.get("observation") if isinstance(initial.get("observation"), dict) else initial
    initial_version = observation.get("version") if isinstance(observation, dict) else None
    if not isinstance(initial_version, dict):
        return False, ["the captured initial boundary has no version"]
    if not snapshots:
        return False, ["no audited boundary exists"]
    first_version = (snapshots[0].get("coordinate") or {}).get("version")
    if first_version != initial_version:
        return False, ["the audited stream does not start at the captured initial boundary"]
    endpoint = run_directory / "experiment-end.json"
    if not endpoint.is_file():
        return False, ["executed endpoint evidence is missing"]
    try:
        ending = json.loads(endpoint.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return False, [f"executed endpoint is unreadable: {exc}"]
    final_version = (ending.get("final_observation") or {}).get("version") \
        if isinstance(ending.get("final_observation"), dict) else None
    if not isinstance(final_version, dict):
        return False, ["executed endpoint has no boundary version"]
    last_version = (snapshots[-1].get("coordinate") or {}).get("version")
    if last_version != final_version:
        return False, ["the audited boundary stream does not end at the executed endpoint"]
    try:
        if last_version.get("tick") < first_version.get("tick") \
                or last_version.get("epoch") < first_version.get("epoch"):
            return False, ["the audited interval is not ordered"]
    except (TypeError, AttributeError):
        return False, ["the audited boundary versions are malformed"]
    budget = normalized.get("tick_budget")
    if isinstance(budget, int) and last_version.get("tick", 0) > budget:
        return False, ["the executed endpoint exceeds the frozen tick budget"]
    stop = plan_doc.get("stop_when")
    if isinstance(stop, dict) and "wave_at_least" in stop:
        wave = ending.get("maximum_wave")
        if not isinstance(wave, int) or wave < stop["wave_at_least"]:
            return False, ["the executed endpoint does not satisfy the frozen stop condition"]
    return True, []


def report_for_run(run: str | Path, *, require_closed: bool = True,
                   lifecycle: bool = True, plan: str | Path | None = None,
                   plan_binding: str | Path | None = None) -> dict:
    """Read a run (or audit) directory and return the full report.

    Both documented input forms work: a child run directory (with its nested
    ``audit/``) and a bare audit directory (whose parent supplies the endpoint
    and suite plan binding).
    """
    run = Path(run)
    audit_directory = run / "audit" if (run / "audit" / "manifest.json").is_file() else run
    run_directory = run.parent if run.name == "audit" else run
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
        probes_block = manifest.get("lifecycle_probes")
        probes_enabled = isinstance(probes_block, dict) and probes_block.get("enabled") is True
        probes_healthy = isinstance(probes_block, dict) and probes_block.get("healthy") is True \
            and probes_block.get("pending_candidate") is False
        probe_counters = (probes_block or {}).get("probe_counters") if probes_enabled else None
        if isinstance(probe_counters, dict):
            probes_healthy = probes_healthy and not any(
                isinstance(probe_counters.get(name), int) and probe_counters.get(name) > 0
                for name in BLOCKING_COUNTERS)
        receipt = lifecycle_report.get("close_receipt") or {}
        try:
            capture_records = load_capture_facts(audit_directory)
        except lifecycle_events.LifecycleError as exc:
            raise ReportError(f"capture facts are unreadable: {exc}") from exc
        full_window, window_problems = _full_window_evidence(run_directory, audit_directory, snapshots,
                                                             plan, plan_binding)
        binding_mode_problems: list[str] = []
        suite_directory = run_directory.parent / (run_directory.name.rsplit("-s", 1)[0]
                                                   if "-s" in run_directory.name else run_directory.name)
        for candidate in _plan_binding_paths(run_directory, suite_directory, plan_binding):
            if not candidate.is_file():
                continue
            try:
                bound = json.loads(candidate.read_bytes())
            except (OSError, UnicodeError, json.JSONDecodeError):
                break
            expected_probes = bound.get("probes")
            if expected_probes in ("off", "on") and probes_enabled != (expected_probes == "on"):
                binding_mode_problems.append("the probe capability does not match the prepared probe arm")
            expected_mode = bound.get("mode")
            lifecycle_mode = lifecycle_events.mode(manifest)
            if expected_mode in ("off", "on") and lifecycle_mode != ("enabled" if expected_mode == "on"
                                                                     else "disabled"):
                binding_mode_problems.append("the lifecycle capability does not match the prepared mode arm")
            break
        initial_entities: dict[int, dict] = {}
        if snapshots:
            for entry in (snapshots[0].get("zombies") or {}).values():
                if not isinstance(entry, dict) or not isinstance(entry.get("id"), int):
                    continue
                raw = entry.get("raw") if isinstance(entry.get("raw"), dict) else {}
                state = raw.get("state")
                state = signed(state) if isinstance(state, int) else None
                initial_entities[entry["id"]] = {"state": state,
                                                 "disappeared": bool(raw.get("disappeared"))}
        report["coverage"]["frozen_plan"] = str(plan) if plan is not None else None
        report["coverage"]["full_window_problems"] = window_problems
        report["capture_facts"] = analyze_capture_facts(
            capture_records,
            counters=probe_counters,
            coverage={
                "receipt_valid": lifecycle_report.get("status") == "valid" and receipt.get("present") is True,
                "probe_capability": probes_enabled,
                "initialization_capture": spawn_hook.get("installed") is True,
                "full_window": full_window,
                "health_clean": probes_healthy,
                "initial_entities": initial_entities,
            })
        if binding_mode_problems:
            report["coverage"]["binding_problems"] = binding_mode_problems
            report["problems"].extend(f"binding: {problem}" for problem in binding_mode_problems)
            report["capture_facts"]["first_kill"]["proven"] = False
            report["first_kill"] = {"proven": False, "source": "exact-store capture facts",
                                    "reasons": binding_mode_problems,
                                    "prerequisites": report["capture_facts"]["first_kill"]["prerequisites"],
                                    "boundary_gate": report.get("first_kill")}
        if window_problems:
            report["coverage"]["full_window_limit"] = window_problems
        capture_first_kill = report["capture_facts"]["first_kill"]
        report["first_kill"] = {
            "proven": capture_first_kill["proven"],
            "source": "exact-store capture facts",
            "reasons": capture_first_kill["reasons"],
            "prerequisites": capture_first_kill["prerequisites"],
            "boundary_gate": report.get("first_kill"),
        }
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
             f"- capture facts: {json.dumps((report.get('capture_facts') or {}).get('summary'), sort_keys=True)}",
             f"- full window problems: {json.dumps((report.get('coverage') or {}).get('full_window_problems'), sort_keys=True)}",
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
