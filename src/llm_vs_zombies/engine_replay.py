"""Execute recorded actions through the normal runtime Client; never invoke an LLM.

This is verified scheduling and captured-state comparison, not a declaration
that every original-engine source of nondeterminism has been controlled.
"""
from __future__ import annotations
from . import sound_effects
from . import b0_normalization as b0
from . import app_update_anchor
from . import mj_clock_anchor
from . import sound_counter
from . import fp_environment
from . import evidence_codec

import argparse
import base64
import copy
import hashlib
import json
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .audit_compare import (AuditLog, AuditTail, EvidenceError, SCHEMA as AUDIT_SCHEMA, canonical,
                            audit_files, digests, file_hash, first_difference, hash_backend, jsonl, particle_semantics,
                            spawn_semantics, read_json, version, DRAW_SCHEDULE_MODE, draw_mode, render_semantics,
                            ENGINE_CALL_MODE, engine_call_mode, engine_call_semantics, validate_engine_origin)
from .client import Client, OutcomeUnknown, ProtocolError, RemoteError, declared_branch_scope
from .evidence_tree import TreePlacement, attach_tree, content_identity, validate_embedded_tree

SCHEMA = "lvz.engine-replay.v1"
READ_ONLY = {"hello", "observe", "status", "audit_snapshot"}
STEP_METHODS = {"commit", "advance"}
INTERVENTIONS = STEP_METHODS | {"capture_frame", "pause"}


def _write(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def _artifacts(value: Any) -> None:
    if not isinstance(value, dict) or not value:
        raise EvidenceError("nonempty artifact SHA-256 identity is required")
    for item in value.values():
        if isinstance(item, dict):
            _artifacts(item)
        elif not isinstance(item, str) or re.fullmatch(r"[0-9a-f]{64}", item) is None:
            raise EvidenceError("artifact identity must contain lowercase SHA-256 values")


def identity_from_launcher(hello: dict, launcher: dict) -> dict:
    """Bind game/runtime/profile assets as well as runtime-advertised identity."""
    artifacts = {key: launcher[key] for key in ("input_hashes", "module_hashes", "profile_hashes")}
    if "resource_hashes" in launcher:
        artifacts["resource_hashes"] = launcher["resource_hashes"]
    _artifacts(artifacts)
    if not isinstance(hello.get("build"), dict) or not isinstance(hello.get("game"), dict):
        raise EvidenceError("hello lacks build/game identity")
    sound_effects.negotiate(hello)
    b0.negotiate(hello)
    app_update_anchor.negotiate(hello)
    mj_clock_anchor.negotiate(hello)
    sound_counter.negotiate(hello)
    fp_environment.negotiate(hello)
    return {"build": copy.deepcopy(hello["build"]), "game": copy.deepcopy(hello["game"]),
            "artifacts": copy.deepcopy(artifacts)}


def capture_initial(client: Client, *, identity: dict, initialization: dict) -> dict:
    """Call after initialization, before the first experiment action.

    The marker and state are captured from the live client. The caller supplies
    checked launcher artifact hashes; these must never be copied from a desired
    trajectory when describing a newly launched process.
    """
    hello = client.hello()
    sound_effects.negotiate(hello)
    b0.negotiate(hello)
    app_update_anchor.negotiate(hello)
    mj_clock_anchor.negotiate(hello)
    sound_counter.negotiate(hello)
    fp_environment.negotiate(hello)
    _validate_identity(identity)
    if hello.get("build") != identity["build"] or hello.get("game") != identity["game"]:
        raise EvidenceError("launcher/runtime identity mismatch")
    if hello.get("capabilities", {}).get("audit_snapshot") is not True:
        raise EvidenceError("runtime does not provide audit_snapshot")
    observation = client.observe()
    snapshot = client.request("audit_snapshot")
    if snapshot.get("version") != observation["version"]:
        raise EvidenceError("initial observation and state are from different boundaries")
    marker = {"schema": SCHEMA, "identity": copy.deepcopy(identity),
              "initialization": copy.deepcopy(initialization), "observation": observation,
              "state": snapshot["state"]}
    branch = declared_branch_scope(hello)
    if branch is not None:
        # The runtime's own scope, so a later replay can prove whether it
        # continued this branch or started a different one.
        marker["branch"] = copy.deepcopy(branch)
    if engine_call_mode(identity["game"]):
        marker["engine_call_origin"] = copy.deepcopy(snapshot.get("engine_call"))
    fp_evidence = fp_environment.evidence(snapshot, identity["game"])
    if fp_evidence is not None:
        marker["fixed_fp_origin"] = copy.deepcopy(fp_evidence)
    _validate_initial(marker)
    client.trace.emit("replay_initial", marker)
    return marker


def _validate_identity(identity: dict) -> None:
    if not isinstance(identity, dict) or set(identity) != {"build", "game", "artifacts"}:
        raise EvidenceError("replay identity requires build, game, and artifacts")
    _artifacts(identity["artifacts"])
    if not identity["build"] or identity["build"].get("runtime_protocol") != 1:
        raise EvidenceError("unsupported runtime build identity")
    game = identity["game"]
    if game.get("schema") != AUDIT_SCHEMA or game.get("loaded_signatures_match") is not True:
        raise EvidenceError("unsupported game target identity")
    draw_mode(game)
    engine_call_mode(game)
    sound_effects.artifacts(game, identity["artifacts"])
    b0.mode(game)
    app_update_anchor.mode(game)
    mj_clock_anchor.mode(game)
    sound_counter.mode(game)
    fp_environment.mode(game)


def _validate_initial(initial: dict) -> None:
    if initial.get("schema") != SCHEMA:
        raise EvidenceError("initial marker schema mismatch")
    _validate_identity(initial.get("identity"))
    if "branch" in initial:
        try:
            branch = declared_branch_scope({"branch": initial["branch"]})
        except ProtocolError as error:
            raise EvidenceError(f"initial marker branch scope is invalid: {error}") from error
        if branch is None:
            raise EvidenceError("initial marker branch scope is empty")
    observation = initial.get("observation", {})
    if version(observation.get("version"))["tick"] != 0:
        raise EvidenceError("replay must begin at controller tick zero; this is not snapshot restore")
    if observation.get("game_ui") != 3 or observation.get("scene") is None:
        raise EvidenceError("initial marker must describe a ready fight board")
    if not isinstance(initial.get("initialization"), dict) or not initial["initialization"]:
        raise EvidenceError("initialization recipe is required")
    if not isinstance(initial.get("state"), dict) or initial["state"].get("schema") != AUDIT_SCHEMA:
        raise EvidenceError("initial full audit_snapshot is required")
    if initial["state"].get("rng", {}).get("target") != initial["identity"]["game"].get("target"):
        raise EvidenceError("initial RNG/game target mismatch")
    mode = draw_mode(initial["identity"]["game"])
    if mode:
        expected = {"mode": mode, "warm_frames": 1, "step_frames": 0}
        if (initial["initialization"].get("execution_mode") != mode
                or initial["initialization"].get("draw_schedule") != initial["identity"]["game"]["draw_schedule"]
                or observation.get("render_prepared") is not True
                or first_difference(expected, initial["state"].get("draw_schedule"))):
            raise EvidenceError("initial marker must declare the prepared controlled draw mode")
    elif ("draw_schedule" in initial["state"]
          or initial["initialization"].get("execution_mode") == DRAW_SCHEDULE_MODE):
        raise EvidenceError("initial controlled drawing lacks an explicit engine identity")
    if engine_call_mode(initial["identity"]["game"]):
        validate_engine_origin(initial.get("engine_call_origin"))
    elif "engine_call_origin" in initial:
        raise EvidenceError("engine call origin lacks an explicit engine identity")
    sound_effects.initial(initial)
    b0.initial(initial)
    app_update_anchor.initial(initial)
    mj_clock_anchor.initial(initial)
    sound_counter.initial(initial)
    fp_environment.initial(initial)
    digests(initial["state"])


def _validate_capture_mode(response, before, after, mode):
    result = response.get("result", {})
    if not mode:
        if result.get("mode") == DRAW_SCHEDULE_MODE or result.get("method") == "cached_controlled_engine_frame":
            raise EvidenceError("cached controlled capture lacks an explicit engine identity")
        return
    if after != before:
        raise EvidenceError("cached capture changed the simulation boundary")
    if response.get("ok") is not True:
        return  # An explicit RPC failure has no invented frame receipt.
    if result.get("forced_render") is not False:
        raise EvidenceError("controlled capture must read its cache without drawing")
    if result.get("capture_ok"):
        if (result.get("mode") != mode or result.get("method") != "cached_controlled_engine_frame"
                or result.get("frame_version") != before or result.get("version") != before
                or result.get("known_rng_unchanged") is not True
                or type(result.get("game_clock_before")) is not int
                or result.get("game_clock_before") != result.get("game_clock_after")
                or result.get("width") != 800 or result.get("height") != 600 or result.get("pixel_format") != "bgr24"):
            raise EvidenceError("cached capture receipt is stale or lacks its read-only guards")


def _capture_response(response: dict) -> tuple[dict, dict | None]:
    """Normalize capture evidence without treating image equality as state equality.

    SessionTrace may retain only a raw-pixel hash/length, while the Client's live
    response still contains base64. Both produce the same metadata contract.
    """
    if response.get("ok") is False:
        error = response.get("error")
        if not isinstance(error, dict) or not all(isinstance(error.get(k), str) for k in ("code", "message")):
            raise EvidenceError("capture error response is malformed")
        return {"ok": False, "error": copy.deepcopy(error)}, None
    result = response.get("result")
    if response.get("ok") is not True or not isinstance(result, dict) or type(result.get("capture_ok")) is not bool:
        raise EvidenceError("capture response must explicitly report capture_ok")
    metadata = copy.deepcopy(result)
    raw = metadata.pop("pixels_base64", None)
    evidence = metadata.pop("pixels_evidence", None)
    metadata.pop("trace_metadata_only", None)
    if raw is not None:
        if not isinstance(raw, str):
            raise EvidenceError("capture pixel payload must be base64 text")
        try:
            pixels = base64.b64decode(raw, validate=True)
        except ValueError as error:
            raise EvidenceError("capture pixel payload is invalid base64") from error
        decoded = {"sha256": hashlib.sha256(pixels).hexdigest(), "byte_length": len(pixels)}
        if evidence is not None and evidence != decoded:
            raise EvidenceError("capture pixel payload and hash evidence disagree")
        evidence = decoded
    if evidence is not None:
        if (not isinstance(evidence, dict) or set(evidence) != {"sha256", "byte_length"}
                or not isinstance(evidence["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", evidence["sha256"]) is None
                or type(evidence["byte_length"]) is not int or evidence["byte_length"] < 0):
            raise EvidenceError("invalid capture pixel hash/length evidence")
    if metadata["capture_ok"] and evidence is None:
        raise EvidenceError("successful capture needs pixels or lightweight pixel evidence")
    version(metadata.get("version"))
    for name in ("forced_render", "used_3d", "known_rng_unchanged", "state_checked"):
        if name in metadata and type(metadata[name]) is not bool:
            raise EvidenceError(f"capture {name} must be boolean")
    if metadata.get("forced_render") is True:
        if type(metadata.get("known_rng_unchanged")) is not bool or any(
                type(metadata.get(name)) is not int for name in ("game_clock_before", "game_clock_after")):
            raise EvidenceError("forced render is missing known-RNG/clock guard evidence")
    return {"ok": True, "result": metadata}, copy.deepcopy(evidence)


def _step_end_version(step: dict) -> dict:
    return step["after_version"] if step["request"]["method"] == "capture_frame" else step["result"]["observation"]["version"]


def _trace_steps(path: Path) -> tuple[dict, list[dict]]:
    initial, pending, steps = None, None, []
    stopped = False
    ids = set()
    capture_errors = set()
    last_capture = None
    last_pause = None
    for seq, event in enumerate(jsonl(path)):
        if type(event.get("schema")) is not int or event["schema"] != 1 or type(event.get("seq")) is not int or event["seq"] != seq:
            raise EvidenceError("SessionTrace schema/sequence mismatch")
        kind, data = event.get("kind"), event.get("data")
        if kind == "replay_initial":
            if initial is not None or pending:
                raise EvidenceError("exactly one initial marker at a completed boundary is required")
            _validate_initial(data)
            initial = data
        elif kind == "request":
            if pending is not None:
                raise EvidenceError("overlapping requests are not supported by sequential replay")
            if not isinstance(data, dict) or type(data.get("protocol")) is not int or data["protocol"] != 1:
                raise EvidenceError("invalid request protocol")
            request_id = data.get("request_id")
            if not isinstance(request_id, str) or not request_id or request_id in ids:
                raise EvidenceError("missing/duplicate source request ID")
            ids.add(request_id)
            if initial is not None and data.get("method") not in READ_ONLY | INTERVENTIONS | {"stop_recording"}:
                raise EvidenceError(f"unsupported intervening mutation: {data.get('method')}")
            if initial is not None and stopped and data.get("method") not in READ_ONLY:
                raise EvidenceError("mutation after recording was closed")
            if data.get("method") in INTERVENTIONS | {"stop_recording"}:
                if last_pause is not None and "state_after" not in last_pause:
                    raise EvidenceError("pause requires a post-control audit snapshot before any later mutation")
                last_pause = None
                last_capture = None
            pending = data
        elif kind == "response":
            if pending is None or not isinstance(data, dict) or data.get("request_id") != pending["request_id"]:
                raise EvidenceError("response has no matching request")
            if type(data.get("protocol")) is not int or data["protocol"] != 1:
                raise EvidenceError("response protocol mismatch")
            if initial is not None:
                if pending["method"] == "capture_frame":
                    capture_response, pixels = _capture_response(data)
                    step = {"request": pending, "capture_response": capture_response, "pixels_evidence": pixels}
                    if capture_response["ok"]:
                        step["after_version"] = copy.deepcopy(capture_response["result"]["version"])
                    else:
                        capture_errors.add(pending["request_id"])
                    steps.append(step)
                    last_capture = step
                elif data.get("ok") is not True or not isinstance(data.get("result"), dict):
                    raise EvidenceError("source request did not complete successfully; action failures must be in action_results")
                elif pending["method"] in STEP_METHODS:
                    steps.append({"request": pending, "result": data["result"]})
                elif pending["method"] == "pause":
                    if (not steps or steps[-1]["request"]["method"] not in STEP_METHODS
                            or steps[-1]["result"].get("executed_ticks", 0) <= 0):
                        raise EvidenceError("pause replay requires an already completed advancement")
                    last_pause = {"request": pending, "result": data["result"]}
                    steps.append(last_pause)
                elif pending["method"] == "stop_recording":
                    if data["result"].get("closed") is not True:
                        raise EvidenceError("source recording did not close")
                    stopped = True
                elif last_capture is not None and pending["method"] in {"observe", "audit_snapshot"}:
                    result = data["result"]
                    actual_version = version(result.get("version"))
                    if "after_version" in last_capture and last_capture["after_version"] != actual_version:
                        raise EvidenceError("capture response and subsequent boundary disagree")
                    last_capture["after_version"] = copy.deepcopy(actual_version)
                    if pending["method"] == "observe":
                        last_capture.setdefault("observation_after", result)
                    else:
                        if not isinstance(result.get("state"), dict) or result["state"].get("schema") != AUDIT_SCHEMA:
                            raise EvidenceError("capture post-state snapshot is malformed")
                        last_capture.setdefault("state_after", result["state"])
                elif last_pause is not None and pending["method"] == "audit_snapshot":
                    result = data["result"]
                    if (result.get("version") != last_pause["result"].get("observation", {}).get("version")
                            or not isinstance(result.get("state"), dict) or result["state"].get("schema") != AUDIT_SCHEMA):
                        raise EvidenceError("pause post-state snapshot is missing its unchanged boundary")
                    if "state_after" in last_pause and last_pause["state_after"] != result["state"]:
                        raise EvidenceError("game state changed while paused")
                    last_pause["state_after"] = result["state"]
            pending = None
        elif initial is not None and kind in {"invalid_response", "exception"}:
            if (kind == "exception" and isinstance(data, dict) and data.get("request_id") in capture_errors
                    and data.get("type") == "RemoteError" and data.get("outcome_unknown") is False):
                capture_errors.remove(data["request_id"])
                continue
            raise EvidenceError("source trace contains an uncertain or rejected outcome")
    if initial is None or pending is not None or not stopped:
        raise EvidenceError("source needs initial marker, completed requests, and stop_recording")
    return initial, steps


def _native_steps(audit: AuditLog) -> list[dict]:
    steps, pending = [], None
    for event in audit.control_events:
        payload = event["payload"]
        if event["kind"] == "request_started":
            if pending is not None:
                raise EvidenceError("overlapping native requests")
            pending = payload["request"]
            if pending.get("request_id") != payload.get("request_id"):
                raise EvidenceError("native request identity mismatch")
        elif event["kind"] == "request_completed":
            if pending is None or pending["request_id"] != payload.get("request_id"):
                raise EvidenceError("unmatched native request completion")
            steps.append({"request": pending, "result": payload["result"]})
            pending = None
    if pending:
        raise EvidenceError("unfinished native request")
    return steps


def _terminal_event(audit: AuditLog, request_id: str, *, required: bool) -> dict | None:
    request_events = audit.request_events(request_id)
    events = [event for event in request_events if event["kind"] == "terminal_transition"]
    if not required:
        if events:
            raise EvidenceError("terminal transition without scene_changed result")
        return None
    if len(events) != 1:
        raise EvidenceError("scene_changed requires exactly one terminal_transition")
    event = events[0]
    payload = event["payload"]
    zero = (engine_call_mode(audit.manifest) and payload.get("transition_kind") == "terminal_zero_clock_update"
            and payload.get("terminal_call_verified") is True and payload.get("clock_delta_measured") is True)
    if (payload.get("tick_delta_verified") is not (False if zero else True)
            or payload.get("board_identity_preserved") is not True
            or type(payload.get("native_tick_delta")) is not int or payload["native_tick_delta"] != (0 if zero else 1)):
        raise EvidenceError("terminal transition cannot prove one actual step on the original Board")
    frames = audit.request_headers(request_id)
    completions = [item for item in request_events if item["kind"] == "request_completed"]
    if (not frames or len(completions) != 1 or frames[-1].kind != "post_step"
            or not frames[-1].seq < event["seq"] < completions[0]["seq"]
            or event["version"] != frames[-1].version):
        raise EvidenceError("terminal transition is missing its final post_step/completion boundary")
    return event


def _validate_native_details(audit: AuditLog, request: dict, result: dict) -> None:
    rid, before = request["request_id"], request["expect"]
    selected = audit.request_events(rid)
    starts = [event for event in selected if event["kind"] == "request_started"]
    ends = [event for event in selected if event["kind"] == "request_completed"]
    recorded = starts[0]["payload"].get("request") if len(starts) == 1 else None
    if (len(starts) != 1 or len(ends) != 1 or starts[0]["version"] != before
            or not isinstance(recorded, dict) or _request_content(recorded) != _request_content(request)
            or ends[0]["payload"].get("result") != result
            or ends[0]["version"] != result["observation"]["version"]):
        raise EvidenceError("native request/completion identity, result, or version mismatch")
    actions = [event for event in selected if event["kind"] == "action"]
    if len(actions) != len(result["action_results"]):
        raise EvidenceError("native action evidence count mismatch")
    for ordinal, (event, outcome) in enumerate(zip(actions, result["action_results"])):
        wanted = {"request_id": rid, "ordinal": ordinal,
                  "action": request["params"]["actions"][ordinal], "result": outcome}
        expected_version = dict(before, revision=before["revision"] + ordinal + 1)
        if (event["payload"] != wanted or event["version"] != expected_version
                or not starts[0]["seq"] < event["seq"] < ends[0]["seq"]):
            raise EvidenceError("native action attempts, outcomes, or frame order mismatch")


def _request_content(request: dict) -> dict:
    """The branch scope is a namespace claim, never part of request content."""
    return {key: value for key, value in request.items() if key != "branch"}


def _validate_steps(initial: dict, steps: list[dict], audit: AuditLog) -> None:
    if not steps:
        raise EvidenceError("trajectory has no executed requests")
    if audit.manifest != initial["identity"]["game"]:
        raise EvidenceError("native audit target/coverage identity differs from hello")
    audit.validate_draw_initial(initial)
    audit.validate_audio_initial(initial)
    audit.validate_app_anchor_initial(initial)
    audit.validate_sound_counter_initial(initial)
    audit.validate_fp_initial(initial)
    mode = draw_mode(audit.manifest)
    call_mode = engine_call_mode(audit.manifest)
    if _native_steps(audit) != [step for step in steps if step["request"]["method"] in STEP_METHODS]:
        raise EvidenceError("native authoritative requests/results differ from source trajectory")
    current = initial["observation"]["version"]
    request_ids = set()
    pause_state_checks = {}
    for step_index, step in enumerate(steps):
        req, result = step["request"], step.get("result")
        request_id = req.get("request_id")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 256 or request_id in request_ids:
            raise EvidenceError("native request ID is missing, oversized or duplicated")
        request_ids.add(request_id)
        if (type(req.get("protocol")) is not int or req["protocol"] != 1
                or req.get("method") not in INTERVENTIONS):
            raise EvidenceError("trajectory skipped a mutation or has stale request version")
        if req["method"] != "pause" or "expect" in req:
            version(req.get("expect"))
        params = req.get("params")
        if not isinstance(params, dict):
            raise EvidenceError("request params must be an object")
        method = req["method"]
        if method == "pause":
            before = steps[step_index - 1] if step_index else None
            if (params != {} or ("expect" in req and req["expect"] != current) or before is None
                    or before["request"]["method"] not in STEP_METHODS or before["result"]["executed_ticks"] <= 0
                    or not isinstance(result, dict) or result.get("state") != "paused_at_boundary"
                    or result.get("observation") != before["result"]["observation"]
                    or result["observation"].get("version") != current or not isinstance(step.get("state_after"), dict)):
                raise EvidenceError("pause is not a proven no-op at a completed paused boundary")
            if audit.request_headers(request_id) or audit.request_events(request_id):
                raise EvidenceError("no-op pause unexpectedly created native actions or steps")
            previous_frames = audit.request_headers(before["request"]["request_id"])
            if not previous_frames or previous_frames[-1].kind != "post_step":
                raise EvidenceError("pause is missing the previous completed audit boundary")
            pause_state_checks[previous_frames[-1].seq] = step["state_after"]
            continue
        if method == "capture_frame":
            response = step["capture_response"]
            after = version(step.get("after_version"))
            _validate_capture_mode(response, current, after, mode)
            if (after["epoch"] != current["epoch"] or after["tick"] != current["tick"]
                    or after["revision"] not in {current["revision"], current["revision"] + 1}):
                raise EvidenceError("capture is missing an actual bounded post-response boundary")
            if response["ok"]:
                if req["expect"] != current or response["result"]["version"] != after:
                    raise EvidenceError("completed capture used a stale or inconsistent version")
                if response["result"]["capture_ok"] and after != current:
                    raise EvidenceError("successful capture silently changed the boundary")
            if "observation_after" in step and step["observation_after"]["version"] != after:
                raise EvidenceError("capture post-observation disagrees with actual boundary")
            if "state_after" in step:
                digests(step["state_after"])
            if audit.request_headers(request_id):
                raise EvidenceError("capture cannot be represented as simulated game steps")
            current = after
            continue
        if req["expect"] != current:
            raise EvidenceError("trajectory skipped a mutation or has stale request version")
        count = params.get("max_ticks" if method == "advance" else "advance_ticks")
        if (type(count) is not int or not 0 <= count <= 100000
                or type(result.get("requested_ticks")) is not int or result["requested_ticks"] != count):
            raise EvidenceError("requested tick count mismatch")
        executed = result.get("executed_ticks")
        if type(executed) is not int or not 0 <= executed <= count:
            raise EvidenceError("invalid executed tick count")
        outcomes, actions = result.get("action_results"), params.get("actions", [])
        if not isinstance(outcomes, list) or not isinstance(actions, list) or len(outcomes) > len(actions):
            raise EvidenceError("invalid action outcome count")
        if method == "advance" and (actions or outcomes):
            raise EvidenceError("advance cannot contain actions")
        failed = False
        for ordinal, outcome in enumerate(outcomes):
            if (not isinstance(outcome, dict) or type(outcome.get("ok")) is not bool
                    or outcome.get("ordinal") != ordinal or outcome.get("action") != actions[ordinal] or failed):
                raise EvidenceError("action order/identity/result mismatch")
            failed = outcome["ok"] is False
        if failed:
            if executed or result.get("stop_reason") != "action_failed":
                raise EvidenceError("failed action must stop before advancement")
        elif len(outcomes) != len(actions):
            raise EvidenceError("successful trajectory is missing action outcomes")
        if not failed and result.get("stop_reason") not in {"budget_exhausted", "wave_changed", "scene_changed"}:
            raise EvidenceError("source terminated for an unsupported external condition")
        terminal = result.get("stop_reason") == "scene_changed"
        _terminal_event(audit, req["request_id"], required=terminal)
        terminal_zero = call_mode and result.get("terminal_zero_clock_calls") == 1
        if terminal and (step_index != len(steps) - 1 or (executed == 0 and not terminal_zero)
                         or result.get("observation", {}).get("game_ui") == 3):
            raise EvidenceError("verified scene transition must end the final request after an actual step")
        if result.get("stop_reason") == "budget_exhausted" and executed != count:
            raise EvidenceError("budget was not actually exhausted")
        after = version(result.get("observation", {}).get("version"))
        wanted = {"epoch": current["epoch"], "tick": current["tick"] + executed,
                  "revision": 0 if executed else current["revision"] + len(outcomes)}
        if after != wanted:
            raise EvidenceError("actual source version does not match execution")
        _validate_native_details(audit, req, result)
        frames = audit.request_headers(req["request_id"])
        call_count = result.get("executed_engine_calls") if call_mode else executed
        if type(call_count) is not int or call_count != executed + int(bool(terminal_zero)) or len(frames) != call_count * 2:
            raise EvidenceError("source lacks pre/post evidence for every executed tick")
        progressed = 0
        for index, frame in enumerate(frames):
            if frame.kind == "post_step":
                progressed += frame.payload["native_tick_delta"]
            expected_version = {"epoch": current["epoch"], "tick": current["tick"] + progressed,
                                "revision": current["revision"] + len(outcomes) if progressed == 0 else 0}
            if (frame.version != expected_version
                    or frame.payload.get("executed_ticks") != progressed):
                raise EvidenceError("source audit ticks do not follow request")
            if frame.kind == "pre_step" and frame.payload.get("requested_ticks") != count:
                raise EvidenceError("source audit requested budget differs from request")
        native_actions = [event["payload"] for event in audit.request_events(req["request_id"]) if event["kind"] == "action"]
        expected_actions = [{"request_id": req["request_id"], "ordinal": i, "action": actions[i], "result": outcome}
                            for i, outcome in enumerate(outcomes)]
        if native_actions != expected_actions:
            raise EvidenceError("native action attempts/results were lost or reordered")
        current = after
    if pause_state_checks:
        # Pauses are uncommon control probes. One sequential pass proves their
        # post-snapshots equal the actual prior post-step without random seeks
        # or equating a non-cryptographic digest with the full reconstructed state.
        for frame in audit._frames(reuse_state=True):
            if frame.seq in pause_state_checks and frame.canonical_state != canonical(pause_state_checks[frame.seq]):
                raise EvidenceError("pause changed the captured game state")


@dataclass
class Trajectory:
    directory: Path
    manifest: dict
    audit: AuditLog

    @property
    def initial(self):
        return self.manifest["initial"]

    @property
    def steps(self):
        return self.manifest["steps"]

    @classmethod
    def load(cls, path: str | Path) -> "Trajectory":
        path = Path(path)
        if path.is_dir():
            path = path / "trajectory.json"
        manifest = read_json(path)
        if manifest.get("schema") != SCHEMA or manifest.get("trajectory_id") != content_identity(manifest):
            raise EvidenceError("trajectory schema/content identity mismatch")
        validate_embedded_tree(manifest)
        audit_directory = path.parent / "audit"
        store = evidence_codec.EvidenceStore(audit_directory, error=EvidenceError)
        expected_files = {"audit/" + store.stored_path(name).name
                          for name in audit_files(audit_directory, read_json(audit_directory / "manifest.json"))}
        if store.compressed:
            expected_files.add(f"audit/{evidence_codec.RECEIPT}")
        if manifest.get("source") == "session_trace":
            expected_files.add("session.jsonl")
        elif manifest.get("source") == "native":
            expected_files.add("initial.json")
        else:
            raise EvidenceError("unknown trajectory source")
        if set(manifest.get("files", {})) != expected_files:
            raise EvidenceError("trajectory evidence file set is incomplete")
        for name, checksum in manifest["files"].items():
            candidate = (path.parent / name).resolve()
            if not candidate.is_relative_to(path.parent.resolve()) or not candidate.is_file() or file_hash(candidate) != checksum:
                raise EvidenceError(f"evidence SHA-256 mismatch: {name}")
        audit = AuditLog(path.parent / "audit", require_closed=True)
        if manifest["source"] == "session_trace":
            initial, steps = _trace_steps(path.parent / "session.jsonl")
        else:
            initial, steps = read_json(path.parent / "initial.json"), _native_steps(audit)
            _validate_initial(initial)
        if manifest.get("initial") != initial or manifest.get("steps") != steps:
            raise EvidenceError("trajectory contents do not match authoritative evidence")
        _validate_steps(initial, steps, audit)
        return cls(path.parent, manifest, audit)


def build_trajectory(trace: str | Path | None, audit_directory: str | Path,
                     output_directory: str | Path, *, initial: str | Path | None = None,
                     tree: TreePlacement | None = None) -> Trajectory:
    """Package a closed recording. Exactly one source is required.

    Native-only input still requires a live-captured initial.json identity/state.
    It cannot recover an omitted initial state from later observations.
    An optional tree placement adds one manifest section; it never changes the
    content identity of the recording and is verified separately.
    """
    if (trace is None) == (initial is None):
        raise ValueError("provide exactly one of SessionTrace or initial metadata")
    audit = AuditLog(audit_directory, require_closed=True)
    if trace is not None:
        marker, steps = _trace_steps(Path(trace))
    else:
        marker, steps = read_json(Path(initial)), _native_steps(audit)
        _validate_initial(marker)
    _validate_steps(marker, steps, audit)
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=False)
    (output / "audit").mkdir()
    filenames = []
    for name in audit.evidence_files:
        # Copy the stored form (plain or a verified gzip container) and keep the
        # codec receipt with it so the packaged trajectory stays self-describing.
        stored = audit.store.stored_path(name)
        dest = f"audit/{stored.name}"
        shutil.copyfile(stored, output / dest)
        filenames.append(dest)
    if audit.store.compressed:
        shutil.copyfile(Path(audit_directory) / evidence_codec.RECEIPT, output / "audit" / evidence_codec.RECEIPT)
        filenames.append(f"audit/{evidence_codec.RECEIPT}")
    source_name = "session.jsonl" if trace is not None else "initial.json"
    shutil.copyfile(Path(trace if trace is not None else initial), output / source_name)
    filenames.append(source_name)
    manifest = {"schema": SCHEMA, "source": "session_trace" if trace is not None else "native",
                "initial": marker, "steps": steps,
                "files": {name: file_hash(output / name) for name in filenames},
                "scope": "captured state only; no completeness or original-engine determinism certification"}
    manifest["trajectory_id"] = content_identity(manifest)
    if tree is not None:
        manifest["tree"] = attach_tree(manifest, tree)
    _write(output / "trajectory.json", manifest)
    return Trajectory.load(output)


@dataclass
class ReplaySession:
    """Returned by a cold-start initializer; identity must describe this process."""
    client: Client
    identity: dict
    audit_directory: Path


class ReplayDivergence(RuntimeError):
    def __init__(self, report: dict):
        self.report = report
        super().__init__(json.dumps(report, ensure_ascii=False))


def replay(trajectory: Trajectory | str | Path, initializer: Callable, output_directory: str | Path,
           *, target_tick: int | None = None, on_takeover: Callable[[ReplaySession, dict], None] | None = None) -> dict:
    """Cold-start, execute every preceding action, compare, optionally hand off.

    initializer(trajectory, output_directory) must return a context manager
    yielding ReplaySession. Cleanup, game launching and isolation are its job.
    target_tick stops at the first B(t) boundary, before later same-tick actions.
    on_takeover runs while the session is alive. No arbitrary snapshot is restored.
    """
    trajectory = Trajectory.load(trajectory.directory) if isinstance(trajectory, Trajectory) else Trajectory.load(trajectory)
    end_tick = _step_end_version(trajectory.steps[-1])["tick"]
    if target_tick is not None and (type(target_tick) is not int or not 0 <= target_tick <= end_tick):
        raise ValueError("target_tick must be inside the recorded trajectory")
    last_simulation = next((step for step in reversed(trajectory.steps) if step["request"]["method"] in STEP_METHODS), None)
    terminal_zero_end = (engine_call_mode(trajectory.audit.manifest) and last_simulation is not None
                         and last_simulation["result"].get("terminal_zero_clock_calls") == 1)
    if (on_takeover is not None and last_simulation is not None and last_simulation["result"]["stop_reason"] == "scene_changed"
            and (target_tick is None or (target_tick == end_tick and not terminal_zero_end))):
        raise ValueError("a terminal scene transition cannot hand off a fight; seek an earlier stable boundary")
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=False)
    report = {"schema": SCHEMA, "parent_trajectory_id": trajectory.manifest["trajectory_id"],
              "branch_id": uuid.uuid4().hex, "requested_target_tick": target_tick,
              "mode": "cold_start_recompute", "original_engine_replay_verified": False,
              "scope": "captured state, actual action outcomes, and controlled initializer-exit spawns",
              "requests": [], "equal": False, "spawn_events_compared": 0, "spawn_exercised": False}
    report["audit_hash_backend"] = hash_backend()
    controlled_draw = draw_mode(trajectory.audit.manifest)
    controlled_calls = engine_call_mode(trajectory.audit.manifest)
    audio_mode = sound_effects.mode(trajectory.audit.manifest)
    declared_anchor_mode = app_update_anchor.mode(trajectory.audit.manifest)
    declared_mj_clock_mode = mj_clock_anchor.mode(trajectory.audit.manifest)
    unified_owner = b0.unified_owns_fields(trajectory.audit.manifest)
    b0_mode = b0.MODE if unified_owner else None
    if b0.mixed_shapes(trajectory.audit.manifest, trajectory.initial["initialization"]):
        raise EvidenceError("one archive cannot mix the unified B(0) normalization with a legacy per-field anchor")
    # The declared unified table owns the two fields, so this archive must be
    # replayed with the table alone: the legacy declarations stay validated as
    # identity (one DLL advertises all three shapes), but nothing here may demand
    # or compare a legacy receipt the unified run never wrote.
    anchor_mode = None if unified_owner else declared_anchor_mode
    mj_clock_mode = None if unified_owner else declared_mj_clock_mode
    counter_mode = sound_counter.mode(trajectory.audit.manifest)
    fp_mode = fp_environment.mode(trajectory.audit.manifest)
    report["fixed_fp"] = {"mode": fp_mode or "not_declared", "activation_compared": False,
        "raw_boundaries_verified": 0, "final_health_verified": False,
        "source_health": trajectory.audit.fp_health, "state_normalized": False,
        "raw_status_thread_and_initial_input_compared": False}
    report["sound_counter"] = {"mode": counter_mode or "bootstrap_lifetime", "receipt_compared": False,
        "native_state_compared": False, "raw_origins_compared": False, "state_normalized_by_comparator": False}
    report["app_update_anchor"] = {"mode": anchor_mode or (
        f"superseded_by_{b0.MODE}" if declared_anchor_mode else "not_declared"), "receipt_compared": False,
        "original_engine_bitwise_unmodified": False if anchor_mode else None,
        "target_policy": app_update_anchor.TARGET_POLICY if anchor_mode else None,
        "before_values_compared": False, "subsequent_state_normalized": False}
    report["mj_clock_anchor"] = {"mode": mj_clock_mode or (
        f"superseded_by_{b0.MODE}" if declared_mj_clock_mode else "not_declared"),
        "original_engine_bitwise_unmodified": False if mj_clock_mode else None,
        "receipt_compared": False, "before_values_compared": False, "subsequent_state_normalized": False,
        "target_policy": mj_clock_anchor.SPEC["target_policy"] if mj_clock_mode else None}
    report["b0_normalization"] = {"mode": b0_mode or "not_declared",
        "original_engine_bitwise_unmodified": False if b0_mode else None,
        "ordering": b0.ORDERING if b0_mode else None,
        "declared_field_set": list(b0.FIELD_NAMES) if b0_mode else [],
        "entries": copy.deepcopy(b0.recipe_entries(trajectory.initial["initialization"])) if b0_mode else [],
        "receipt_compared": False, "before_values_compared": False, "subsequent_state_normalized": False,
        "source_actual_value_reused_as_target": False}
    report["sound_effects"] = {"mode": audio_mode or "original", "state_compared": False,
        "original_engine_bitwise_unmodified": False if audio_mode else None,
        "activation_evidence_verified": False, "final_health_verified": False,
        "source_health": trajectory.audit.sound_effects_health,
        "raw_activation_addresses_compared": False, "scope": "allocation-none SFX; music/exhaustive determinism unverified"}
    report["engine_calls"] = {"mode": controlled_calls or "not_declared", "returned_calls_compared": 0,
        "clock_steps_compared": 0, "terminal_zero_calls_compared": 0, "last_source_call_id": None,
        "last_actual_call_id": None, "final_health_verified": False, "source_health": trajectory.audit.engine_call_health,
        "raw_board_addresses_compared": False}
    report["draw_schedule"] = {"mode": controlled_draw or "legacy_autonomous_draw_schedule",
        "original_engine_bitwise_unmodified": False if controlled_draw else None,
        "warm_receipts_compared": 0, "step_receipts_compared": 0, "terminal_skips_compared": 0,
        "final_health_verified": False, "automatic_draw_counters_compared": False,
        "source_health": trajectory.audit.draw_health}
    report["particle_shake"] = {"mode": trajectory.audit.manifest.get("particle_shake", {}).get("mode", "not_declared"),
                                "original_engine_bitwise_unmodified": False if trajectory.audit.manifest.get("particle_shake") else None,
                                "semantic_seed_calls_compared": 0, "raw_pointer_seeds_compared": False,
                                "final_health_verified": False}
    source_captures = [step for step in trajectory.steps if step["request"]["method"] == "capture_frame"]
    report["capture_interventions"] = {
        "source": "session_trace" if trajectory.manifest["source"] == "session_trace" else "unknown_native_only",
        "recorded": len(source_captures) if trajectory.manifest["source"] == "session_trace" else None,
        "attempted": 0, "executed": 0, "compared": 0, "reproduced": False, "unknown_outcome": False,
        "scope": "executed_prefix" if target_tick is not None else "entire_trajectory",
        "pixel_content_compared": False,
    }
    report["pause_controls"] = {"recorded": sum(step["request"]["method"] == "pause" for step in trajectory.steps),
                                "executed": 0, "verified_noop": 0}
    report["spawn_comparison"] = {
        "scope": "executed_prefix" if target_tick is not None else "entire_trajectory",
        "hook_declared": trajectory.audit.manifest.get("spawn_hook", {}).get("installed") is True,
        "source_controlled_total": trajectory.audit.birth_counts["controlled"],
        "source_initialization_history": trajectory.audit.birth_counts["initialization"],
        "actual_initialization_history": 0, "initialization_history_compared": False,
        "initial_state_compared": False, "final_health_verified": False,
        "excluded": ["global ordinal (includes unassigned preview history)",
                     "raw animation handles after verified local semantic mapping"],
    }

    def require_equal(expected, actual, stage):
        difference = first_difference(expected, actual)
        if difference:
            raise ReplayDivergence({"stage": stage, "difference": difference})

    source_frame_stream = actual_frames = None
    try:
        with initializer(trajectory, output) as session:
            if not isinstance(session, ReplaySession):
                raise TypeError("initializer must yield ReplaySession")
            client = session.client
            _validate_identity(session.identity)
            require_equal(trajectory.initial["identity"], session.identity, "identity")
            hello = client.hello()
            sound_effects.negotiate(hello)
            b0.negotiate(hello)
            app_update_anchor.negotiate(hello)
            mj_clock_anchor.negotiate(hello)
            sound_counter.negotiate(hello)
            fp_environment.negotiate(hello)
            require_equal(session.identity["build"], hello.get("build"), "live_build")
            require_equal(session.identity["game"], hello.get("game"), "live_game")
            # Branch scope: the live runtime owns the namespace that actually
            # produced these frames. A pre-branch runtime keeps the legacy
            # controller-side label and is reported as unscoped.
            try:
                runtime_branch = declared_branch_scope(hello)
            except ProtocolError as error:
                raise EvidenceError(f"live hello branch scope is invalid: {error}") from error
            source_branch = trajectory.initial.get("branch")
            if not isinstance(source_branch, dict):
                source_branch = None
            report["branch_scope"] = {
                "mode": "runtime_declared" if runtime_branch and runtime_branch["branch_id"] else "runtime_unscoped",
                "request_label": report["branch_id"],
                "runtime_branch_id": None if runtime_branch is None else runtime_branch["branch_id"],
                "runtime_origin": None if runtime_branch is None else runtime_branch["origin"],
                "runtime_parent_branch_id": None if runtime_branch is None else runtime_branch["parent_branch_id"],
                "dedup_key": None if runtime_branch is None else runtime_branch["dedup_key"],
                "source_branch_id": None if source_branch is None else source_branch.get("branch_id"),
                "relation": ("unknown_source_branch" if source_branch is None or runtime_branch is None
                             else "same_branch" if source_branch.get("branch_id") == runtime_branch["branch_id"]
                             else "other_branch"),
                "scope": "a version triple is only comparable inside one branch scope",
            }
            if runtime_branch is not None and runtime_branch["branch_id"]:
                report["branch_id"] = runtime_branch["branch_id"]
                require_equal(report["branch_id"], client.branch_id, "live_branch_scope")
            required = {"observe", "audit_snapshot"} | {step["request"]["method"] for step in trajectory.steps if step["request"]["method"] in STEP_METHODS}
            if controlled_draw:
                required.update({"prepare_render", DRAW_SCHEDULE_MODE})
            if controlled_calls:
                required.add(ENGINE_CALL_MODE)
            if audio_mode:
                required.add(sound_effects.MODE)
            if anchor_mode:
                required.update({app_update_anchor.MODE, app_update_anchor.METHOD})
            if mj_clock_mode:
                required.update({mj_clock_anchor.MODE, mj_clock_anchor.METHOD})
            if b0_mode:
                required.update({b0.MODE, b0.METHOD})
            if counter_mode:
                required.update({sound_counter.MODE, sound_counter.METHOD})
            if fp_mode:
                required.add(fp_environment.MODE)
            if report["pause_controls"]["recorded"]:
                # Protocol v1 implements these controller methods, but historic
                # hello manifests omit their capability keys. Probe status and
                # execute pause normally; explicit denial still fails closed.
                if any(hello.get("capabilities", {}).get(name) is False for name in ("pause", "status")):
                    raise EvidenceError("runtime explicitly denies pause/status control")
            if any(step["capture_response"]["ok"] and step["capture_response"]["result"]["capture_ok"] for step in source_captures):
                required.add("capture_frame")
            for capability in required:
                if hello.get("capabilities", {}).get(capability) is not True:
                    raise EvidenceError(f"missing runtime capability: {capability}")
            observation = client.observe()
            source_version = trajectory.initial["observation"]["version"]
            live_version = version(observation.get("version"))
            if live_version["tick"] != 0:
                raise EvidenceError("initializer did not supply a fresh tick-zero boundary")
            report["epoch_mapping"] = {"source": source_version["epoch"], "actual": live_version["epoch"],
                                        "initial_revision_offset": live_version["revision"] - source_version["revision"]}

            def mapped_version(value):
                value = copy.deepcopy(version(value))
                if value["epoch"] != source_version["epoch"]:
                    raise EvidenceError("source trajectory changed epoch")
                value["epoch"] = live_version["epoch"]
                if value["tick"] == 0:
                    value["revision"] += report["epoch_mapping"]["initial_revision_offset"]
                return value

            def mapped_observation(value):
                result = copy.deepcopy(value)
                result["version"] = mapped_version(result["version"])
                return result

            require_equal(mapped_observation(trajectory.initial["observation"]), observation, "initial_observation")
            snapshot = client.request("audit_snapshot")
            require_equal(live_version, snapshot.get("version"), "initial_snapshot_boundary")
            require_equal(trajectory.initial["state"], snapshot.get("state"), "initial_state")
            if controlled_calls:
                validate_engine_origin(snapshot.get("engine_call"))
                require_equal(trajectory.initial["engine_call_origin"], snapshot["engine_call"], "initial_engine_call_origin")
            report["spawn_comparison"]["initial_state_compared"] = True
            # The actual trace remains independently packageable, including a
            # later branch continuation. It starts at actual B(0), not at the
            # seek target. Preserve the parent link without forging a snapshot.
            actual_marker = {"schema": SCHEMA, "identity": copy.deepcopy(session.identity),
                "initialization": copy.deepcopy(trajectory.initial["initialization"]),
                "observation": copy.deepcopy(observation), "state": copy.deepcopy(snapshot["state"]),
                "parent": {"trajectory_id": trajectory.manifest["trajectory_id"],
                           "branch_id": report["branch_id"], "target_tick": target_tick,
                           "epoch_mapping": copy.deepcopy(report["epoch_mapping"])}}
            if runtime_branch is not None:
                # The actual node's own scope, so a packaged continuation carries
                # the namespace that produced it.
                actual_marker["branch"] = copy.deepcopy(runtime_branch)
            if controlled_calls:
                actual_marker["engine_call_origin"] = copy.deepcopy(snapshot["engine_call"])
            fp_evidence = fp_environment.evidence(snapshot, session.identity["game"])
            if fp_evidence is not None:
                actual_marker["fixed_fp_origin"] = copy.deepcopy(fp_evidence)
            client.trace.emit("replay_initial", actual_marker)
            actual_ids = {}
            reached_version = client.version
            source_frame_stream = trajectory.audit._frames(reuse_state=True)
            actual_audit = AuditTail(session.audit_directory)
            require_equal(trajectory.audit.manifest, actual_audit.manifest, "audit_manifest")
            actual_audit.validate_audio_initial(actual_marker)
            report["sound_effects"].update(state_compared=bool(audio_mode), activation_evidence_verified=bool(audio_mode))
            if controlled_draw:
                # Consume initialization evidence before executing any recorded
                # request. Warm rendering belongs to B0, never to tick one.
                if any(actual_audit.read_request(None)):
                    raise EvidenceError("initializer executed an audited update before B0")
                actual_audit.validate_draw_initial({"observation": observation, "state": snapshot["state"]})
                require_equal(render_semantics(trajectory.audit.warm_render, map_version=mapped_version),
                              render_semantics(actual_audit.warm_render), "initial_warm_render")
                report["draw_schedule"]["warm_receipts_compared"] = 1
            actual_audit.validate_app_anchor_initial(actual_marker)
            actual_audit.validate_mj_clock_initial(actual_marker)
            actual_audit.validate_b0_normalization_initial(actual_marker)
            actual_audit.validate_sound_counter_initial(actual_marker)
            actual_audit.validate_fp_initial(actual_marker)
            if fp_mode:
                require_equal(fp_environment.semantics(trajectory.audit.fp_activation),
                              fp_environment.semantics(actual_audit.fp_activation), "initial_fixed_fp")
                report["fixed_fp"]["activation_compared"] = True
            if counter_mode:
                expected_counter, actual_counter = trajectory.audit.sound_counter_receipt, actual_audit.sound_counter_receipt
                require_equal(sound_counter.semantics(expected_counter, map_version=mapped_version),
                              sound_counter.semantics(actual_counter), "initial_sound_counter")
                report["sound_counter"].update(receipt_compared=True, native_state_compared=True,
                    source_origin_raw_calls=expected_counter["origin_raw_calls"],
                    actual_origin_raw_calls=actual_counter["origin_raw_calls"])
            if anchor_mode:
                expected_anchor, actual_anchor = trajectory.audit.app_anchor_receipt, actual_audit.app_anchor_receipt
                pending_mj_clock = mj_clock_mode is not None
                require_equal(app_update_anchor.semantics(expected_anchor, map_version=mapped_version,
                                                          pending_mj_clock=pending_mj_clock),
                              app_update_anchor.semantics(actual_anchor, pending_mj_clock=pending_mj_clock),
                              "initial_app_update_anchor")
                report["app_update_anchor"].update(receipt_compared=True, source_before=expected_anchor["before"],
                    actual_before=actual_anchor["before"], requested=expected_anchor["requested"],
                    common_target=expected_anchor["requested"],
                    source_after=expected_anchor["after"], actual_after=actual_anchor["after"])
            if mj_clock_mode:
                expected_mj, actual_mj = trajectory.audit.mj_clock_receipt, actual_audit.mj_clock_receipt
                require_equal(mj_clock_anchor.semantics(expected_mj, map_version=mapped_version),
                              mj_clock_anchor.semantics(actual_mj), "initial_mj_clock_anchor")
                report["mj_clock_anchor"].update(receipt_compared=True, source_before=expected_mj["before"],
                    actual_before=actual_mj["before"], requested=expected_mj["requested"],
                    common_target=expected_mj["requested"], source_after=expected_mj["after"],
                    actual_after=actual_mj["after"])
            if b0_mode:
                expected_b0, actual_b0 = trajectory.audit.b0_normalization_receipt, actual_audit.b0_normalization_receipt
                require_equal(b0.semantics(expected_b0, map_version=mapped_version),
                              b0.semantics(actual_b0), "initial_b0_normalization")
                report["b0_normalization"].update(receipt_compared=True, before_values_recorded=True,
                    source_before=[item["value"] for item in expected_b0["before"]],
                    actual_before=[item["value"] for item in actual_b0["before"]],
                    common_targets=[item["target"] for item in expected_b0["requested"]],
                    source_after=[item["value"] for item in expected_b0["after"]],
                    actual_after=[item["value"] for item in actual_b0["after"]])
            for ordinal, step in enumerate(trajectory.steps):
                if target_tick is not None and client.version["tick"] >= target_tick:
                    break
                req, expected = step["request"], step.get("result")
                source_before = trajectory.initial["observation"]["version"] if ordinal == 0 else _step_end_version(trajectory.steps[ordinal - 1])
                require_equal(mapped_version(source_before), client.version, f"request[{ordinal}].before")
                params = copy.deepcopy(req["params"])
                partial = (req["method"] in STEP_METHODS and target_tick is not None
                    and (expected["observation"]["version"]["tick"] > target_tick
                         or (controlled_calls and expected.get("terminal_zero_clock_calls") == 1
                             and expected["observation"]["version"]["tick"] == target_tick)))
                if partial:
                    params["max_ticks" if req["method"] == "advance" else "advance_ticks"] = target_tick - client.version["tick"]
                actual_id = f"replay-{report['branch_id']}-{ordinal}"
                actual_ids[req["request_id"]] = actual_id
                if req["method"] == "pause":
                    def paused_status(label):
                        status = client.request("status")
                        if (not {"state", "fault", "version", "pending_request_id", "executed_ticks"} <= set(status)
                                or status.get("state") != "paused_at_boundary" or status["pending_request_id"] is not None
                                or type(status["executed_ticks"]) is not int or status["executed_ticks"] != 0 or status["fault"] is not None):
                            raise EvidenceError("pause control requires an already paused runtime without pending work")
                        require_equal(mapped_version(source_before), status.get("version"), label)
                        return status
                    status_before = paused_status(f"pause[{ordinal}].before_status")
                    before = client.request("audit_snapshot")
                    require_equal(client.version, before.get("version"), f"pause[{ordinal}].before_snapshot_version")
                    actual = client.request("pause", params, expect=mapped_version(req["expect"]) if "expect" in req else None,
                                            request_id=actual_id)
                    execution = {"ordinal": ordinal, "source_request_id": req["request_id"],
                                 "actual_request_id": actual_id, "method": "pause", "params": params, "result": actual,
                                 "partial": False, "status_before": status_before}
                    report["requests"].append(execution)
                    report["pause_controls"]["executed"] += 1
                    wanted = copy.deepcopy(expected)
                    wanted["observation"] = mapped_observation(expected["observation"])
                    require_equal(wanted, actual, f"pause[{ordinal}].result")
                    execution["status_after"] = paused_status(f"pause[{ordinal}].after_status")
                    after = client.request("audit_snapshot")
                    require_equal(before.get("version"), after.get("version"), f"pause[{ordinal}].snapshot_version")
                    require_equal(before.get("state"), after.get("state"), f"pause[{ordinal}].state_unchanged")
                    require_equal(step["state_after"], after.get("state"), f"pause[{ordinal}].source_state")
                    execution["verified_noop"] = True
                    report["pause_controls"]["verified_noop"] += 1
                    reached_version = client.version
                    continue
                if req["method"] == "capture_frame":
                    report["capture_interventions"]["attempted"] += 1
                    capture_before = client.request("audit_snapshot") if controlled_draw else None
                    if capture_before is not None:
                        require_equal(client.version, capture_before.get("version"), f"capture[{ordinal}].before_boundary")
                    try:
                        actual_result = client.request("capture_frame", params, expect=mapped_version(req["expect"]), request_id=actual_id)
                        actual_response = {"ok": True, "result": actual_result}
                    except RemoteError as error:
                        actual_response = error.response
                    except OutcomeUnknown:
                        report["capture_interventions"]["unknown_outcome"] = True
                        raise
                    actual_metadata, actual_pixels = _capture_response(actual_response)
                    execution = {"ordinal": ordinal, "source_request_id": req["request_id"],
                                 "actual_request_id": actual_id, "method": "capture_frame", "params": params,
                                 "capture_response": actual_metadata, "pixels_evidence": actual_pixels, "partial": False}
                    report["requests"].append(execution)
                    report["capture_interventions"]["executed"] += 1
                    wanted = copy.deepcopy(step["capture_response"])
                    if wanted["ok"]:
                        wanted["result"]["version"] = mapped_version(wanted["result"]["version"])
                        if "frame_version" in wanted["result"]:
                            wanted["result"]["frame_version"] = mapped_version(wanted["result"]["frame_version"])
                    require_equal(wanted, actual_metadata, f"capture[{ordinal}].metadata")
                    require_equal(step["pixels_evidence"] is not None, actual_pixels is not None, f"capture[{ordinal}].pixel_evidence_present")
                    if actual_pixels is not None:
                        require_equal(step["pixels_evidence"]["byte_length"], actual_pixels["byte_length"], f"capture[{ordinal}].pixel_byte_length")
                    # Read the actual current observation even after an explicit
                    # capture failure; never synthesize it from expected values.
                    observation = client.observe()
                    _validate_capture_mode(actual_metadata, mapped_version(source_before), observation["version"], controlled_draw)
                    require_equal(mapped_version(step["after_version"]), observation["version"], f"capture[{ordinal}].after_version")
                    if "observation_after" in step:
                        require_equal(mapped_observation(step["observation_after"]), observation, f"capture[{ordinal}].observation")
                    if "state_after" in step or controlled_draw:
                        snapshot = client.request("audit_snapshot")
                        require_equal(client.version, snapshot.get("version"), f"capture[{ordinal}].snapshot_boundary")
                        if "state_after" in step:
                            require_equal(step["state_after"], snapshot.get("state"), f"capture[{ordinal}].state")
                        if capture_before is not None:
                            require_equal(capture_before.get("state"), snapshot.get("state"), f"capture[{ordinal}].read_only_state")
                    execution["observation_after"] = observation
                    execution["post_capture_state_compared"] = "state_after" in step
                    report["capture_interventions"]["compared"] += 1
                    reached_version = client.version
                    continue
                actual_request = {"protocol": 1, "request_id": actual_id, "method": req["method"],
                                  "params": copy.deepcopy(params), "expect": client.version}
                actual = client.request(req["method"], params, expect=client.version, request_id=actual_id)
                # Persist actual evidence immediately, before any comparison can fail.
                execution = {"ordinal": ordinal, "source_request_id": req["request_id"],
                             "actual_request_id": actual_id, "params": params, "result": actual, "partial": partial}
                report["requests"].append(execution)
                if partial:
                    count = params["max_ticks" if req["method"] == "advance" else "advance_ticks"]
                    require_equal(count, actual.get("requested_ticks"), "partial.requested_ticks")
                    require_equal(count, actual.get("executed_ticks"), "partial.executed_ticks")
                    if controlled_calls:
                        require_equal(count, actual.get("executed_engine_calls"), "partial.executed_engine_calls")
                        require_equal(0, actual.get("terminal_zero_clock_calls"), "partial.terminal_zero_clock_calls")
                    require_equal("budget_exhausted", actual.get("stop_reason"), "partial.stop_reason")
                    require_equal(expected["action_results"], actual.get("action_results"), "partial.action_results")
                    require_equal({"epoch": live_version["epoch"], "tick": target_tick, "revision": 0},
                                  actual.get("observation", {}).get("version"), "partial.boundary")
                else:
                    wanted = copy.deepcopy(expected)
                    wanted["observation"] = mapped_observation(wanted["observation"])
                    require_equal(wanted, actual, f"request[{ordinal}].result")
                source_count = actual["executed_engine_calls" if controlled_calls else "executed_ticks"] * 2
                actual_frames = actual_audit.read_request(actual_id)
                actual_count = 0
                last_source_frame = None
                for index, right in enumerate(actual_frames):
                    left = next(source_frame_stream)
                    actual_count += 1
                    require_equal(req["request_id"], left.payload.get("request_id"), "source_frame_request")
                    require_equal(left.kind, right.kind, f"audit[{ordinal}:{index}].phase")
                    require_equal(mapped_version(left.version), right.version, f"audit[{ordinal}:{index}].version")
                    require_equal(engine_call_semantics(left.payload.get("engine_call"), map_version=mapped_version),
                                  engine_call_semantics(right.payload.get("engine_call")), f"audit[{ordinal}:{index}].engine_call")
                    if controlled_calls and right.kind == "post_step":
                        delta = right.payload["native_tick_delta"]
                        report["engine_calls"]["returned_calls_compared"] += 1
                        report["engine_calls"]["clock_steps_compared"] += delta
                        report["engine_calls"]["terminal_zero_calls_compared"] += int(delta == 0)
                        report["engine_calls"]["last_source_call_id"] = left.payload["engine_call"]["engine_call_id"]
                        report["engine_calls"]["last_actual_call_id"] = right.payload["engine_call"]["engine_call_id"]
                    require_equal(render_semantics(left.payload.get("render"), map_version=mapped_version),
                                  render_semantics(right.payload.get("render")), f"audit[{ordinal}:{index}].render")
                    if controlled_draw and right.kind == "post_step":
                        key = "terminal_skips_compared" if right.payload["render"]["phase"] == "terminal" else "step_receipts_compared"
                        report["draw_schedule"][key] += 1
                    require_equal([particle_semantics(event, map_version=mapped_version) for event in left.particle_seeds],
                                  [particle_semantics(event) for event in right.particle_seeds],
                                  f"audit[{ordinal}:{index}].particle_shake")
                    report["particle_shake"]["semantic_seed_calls_compared"] += len(right.particle_seeds)
                    # A birth's initializer-exit state can differ and later
                    # converge before post_step. Compare it independently of
                    # frame-state equality, in exact local occurrence order.
                    for birth_index in range(max(len(left.spawn_events), len(right.spawn_events))):
                        source_birth = left.spawn_events[birth_index] if birth_index < len(left.spawn_events) else None
                        actual_birth = right.spawn_events[birth_index] if birth_index < len(right.spawn_events) else None
                        wanted_birth = spawn_semantics(source_birth, left, map_version=mapped_version) if source_birth else None
                        measured_birth = spawn_semantics(actual_birth, right) if actual_birth else None
                        difference = first_difference(wanted_birth, measured_birth)
                        if difference:
                            raise ReplayDivergence({"stage": f"audit[{ordinal}:{index}].spawn[{birth_index}]",
                                "tick": right.version["tick"], "phase": right.kind,
                                "controlled_ordinal": report["spawn_events_compared"],
                                "source_seq": source_birth["seq"] if source_birth else None,
                                "actual_seq": actual_birth["seq"] if actual_birth else None,
                                "difference": difference})
                        report["spawn_events_compared"] += 1
                        report["spawn_exercised"] = True
                    # Both canonical byte strings were reconstructed from the
                    # actual JSON Patches and checked against every native
                    # digest. Compare the bytes themselves, not only FNV hashes.
                    if left.canonical_state != right.canonical_state:
                        require_equal(left.state, right.state, f"audit[{ordinal}:{index}].state")
                    if fp_mode:
                        report["fixed_fp"]["raw_boundaries_verified"] += 1
                    last_source_frame = left
                require_equal(source_count, actual_count, f"request[{ordinal}].frame_count")
                _validate_native_details(actual_audit, actual_request, actual)
                terminal = not partial and expected["stop_reason"] == "scene_changed"
                actual_terminal = _terminal_event(actual_audit, actual_id, required=terminal)
                reached_version = client.version
                if terminal:
                    source_terminal = _terminal_event(trajectory.audit, req["request_id"], required=True)
                    wanted_payload = copy.deepcopy(source_terminal["payload"])
                    wanted_payload["request_id"] = actual_id
                    if "engine_call" in wanted_payload:
                        wanted_payload["engine_call"] = engine_call_semantics(wanted_payload["engine_call"], map_version=mapped_version)
                    require_equal(wanted_payload, actual_terminal["payload"], "terminal_transition.payload")
                    require_equal(mapped_version(source_terminal["version"]), actual_terminal["version"], "terminal_transition.version")
                    report["terminal_transition"] = copy.deepcopy(actual_terminal)
                    # Controller Boundary may already have advanced the epoch.
                    # Keep the measured terminal version, refresh cleanup's expect.
                    client.observe()
                elif last_source_frame is not None:
                    snapshot = client.request("audit_snapshot")
                    require_equal(client.version, snapshot.get("version"), "final_snapshot_boundary")
                    require_equal(last_source_frame.state, snapshot.get("state"), "post_request_state")
            # Seek normally leaves the source suffix unread. Its byte binding
            # must be checked synchronously before publishing success; relying
            # on generator GC would discard a close-time evidence exception.
            source_frame_stream.close()
            source_frame_stream = None
            trajectory.audit.verify_files()
            require_equal(report["spawn_events_compared"], actual_audit.birth_counts["controlled"],
                          "spawn.uncompared_actual_events")
            if target_tick is None:
                require_equal(trajectory.audit.birth_counts["controlled"], report["spawn_events_compared"],
                              "spawn.uncompared_source_events")
            report["spawn_comparison"]["actual_initialization_history"] = actual_audit.birth_counts["initialization"]
            report["equal"] = True
            expected_captures = sum(step["request"]["method"] == "capture_frame"
                                    for step in trajectory.steps[:len(report["requests"])] )
            report["capture_interventions"]["expected_in_scope"] = expected_captures
            report["capture_interventions"]["reproduced"] = (
                report["capture_interventions"]["executed"] == report["capture_interventions"]["compared"] == expected_captures
                if trajectory.manifest["source"] == "session_trace" else None)
            report["reached_version"] = reached_version
            report["current_runtime_version"] = client.version
            report["request_mapping"] = actual_ids
            report["takeover"] = {"parent_trajectory_id": trajectory.manifest["trajectory_id"],
                                   "parent_tick": reached_version["tick"], "parent_requests_consumed": len(report["requests"]),
                                   "actual_version": client.version, "epoch_mapping": report["epoch_mapping"],
                                   "branch_id": report["branch_id"], "branch_scope": report["branch_scope"]}
            if controlled_calls:
                report["takeover"].update(parent_engine_calls_consumed=report["engine_calls"]["returned_calls_compared"],
                    last_source_call_id=report["engine_calls"]["last_source_call_id"],
                    last_actual_call_id=report["engine_calls"]["last_actual_call_id"],
                    terminal_consumed="terminal_transition" in report,
                    partial_request_ordinal=next((request["ordinal"] for request in report["requests"] if request.get("partial")), None))
            client.trace.emit("replay_takeover" if on_takeover else "replay_completed", report["takeover"])
            # A live consumer can continue with the exact returned epoch/version.
            if on_takeover is not None:
                on_takeover(session, copy.deepcopy(report["takeover"]))
        particle_required = trajectory.audit.manifest.get("particle_shake") is not None
        spawn_required = report["spawn_comparison"]["hook_declared"] or trajectory.audit.birth_counts["controlled"] > 0
        if particle_required or spawn_required or controlled_draw or audio_mode or fp_mode:
            if on_takeover is None:
                actual_audit.verify_closed()
                closed_audit = actual_audit
            else:
                closed_audit = AuditLog(session.audit_directory, require_closed=True)
                if fp_mode:
                    closed_audit.validate_fp_initial(actual_marker)
            report["particle_shake"]["final_health_verified"] = particle_required
            report["spawn_comparison"]["final_health_verified"] = spawn_required
            if controlled_draw:
                report["draw_schedule"].update(final_health_verified=True, actual_health=closed_audit.draw_health,
                    health_scope="full_branch" if on_takeover else "executed_prefix")
            if controlled_calls:
                report["engine_calls"].update(final_health_verified=True, actual_health=closed_audit.engine_call_health,
                    health_scope="full_branch" if on_takeover else "executed_prefix")
            if fp_mode:
                report["fixed_fp"].update(final_health_verified=True, actual_health=closed_audit.fp_health,
                    health_scope="full_branch" if on_takeover else "executed_prefix")
                if on_takeover is None:
                    require_equal(report["fixed_fp"]["raw_boundaries_verified"], closed_audit.fp_health["raw_frames"],
                                  "fixed_fp.uncompared_closed_boundaries")
            if audio_mode:
                report["sound_effects"].update(final_health_verified=True, actual_health=closed_audit.sound_effects_health,
                    health_scope="full_branch" if on_takeover else "executed_prefix")
                if target_tick is None and on_takeover is None:
                    require_equal(sound_counter.health_semantics(trajectory.audit.sound_effects_health, trajectory.audit.manifest),
                                  sound_counter.health_semantics(closed_audit.sound_effects_health, closed_audit.manifest),
                                  "sound_effects.close")
                if counter_mode:
                    report["sound_counter"].update(final_health_verified=True,
                        source_raw_calls_end=trajectory.audit.sound_effects_health["raw_calls"],
                        actual_raw_calls_end=closed_audit.sound_effects_health["raw_calls"],
                        source_experiment_calls_end=trajectory.audit.sound_effects_health["calls"],
                        actual_experiment_calls_end=closed_audit.sound_effects_health["calls"],
                        health_scope="full_branch" if on_takeover else "executed_prefix")
            if on_takeover is None:
                require_equal(report["spawn_events_compared"], actual_audit.birth_counts["controlled"],
                              "spawn.uncompared_close_events")
                report["spawn_comparison"]["actual_initialization_history"] = actual_audit.birth_counts["initialization"]
    except Exception as error:
        for stream in (actual_frames, source_frame_stream):
            if stream is not None:
                try:
                    stream.close()
                except Exception as close_error:
                    report.setdefault("evidence_close_failures", []).append(str(close_error))
        report["equal"] = False
        report["failure"] = error.report if isinstance(error, ReplayDivergence) else {"type": type(error).__name__, "message": str(error)}
        _write(output / "replay-report.json", report)
        raise
    _write(output / "replay-report.json", report)
    return report


def demo(output_directory: str | Path) -> dict:
    """Run a tiny explicitly synthetic counter model, without launching PvZ."""
    from contextlib import contextmanager
    from .session import SessionTrace

    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=False)
    game = {"schema": AUDIT_SCHEMA, "target": "synthetic-counter-demo", "loaded_signatures_match": True,
            "coverage": {"complete_game_state": False}, "original_engine_replay_verified": False}
    build = {"runtime_protocol": 1, "synthetic": True}
    identity = {"build": build, "game": game, "artifacts": {"synthetic_fixture": "0" * 64}}

    class CounterTransport:
        def __init__(self, path, epoch):
            self.path, self.epoch, self.tick, self.seq, self.previous = path, epoch, 0, 0, None
            path.mkdir(parents=True)
            _write(path / "manifest.json", game)
            self.files = {name: (path / name).open("x", encoding="utf-8", newline="\n")
                          for name in ("events.jsonl", "checksums.jsonl", "state-deltas.jsonl")}

        def version(self):
            return {"epoch": self.epoch, "tick": self.tick, "revision": 0}

        def observe(self):
            return {"version": self.version(), "game_ui": 3, "scene": 3, "game_clock": self.tick}

        def state(self):
            return {"schema": AUDIT_SCHEMA, "rng": {"target": game["target"]}, "counter": self.tick}

        def record(self, kind, payload):
            envelope = {"schema": AUDIT_SCHEMA, "seq": self.seq, "kind": kind,
                        "payload": payload, "version": self.version()}
            self.seq += 1
            rows = {"events.jsonl": envelope}
            if kind in {"pre_step", "post_step"}:
                state = self.state()
                delta = {"initial": state} if self.previous is None else {"patch": [{"op": "replace", "path": "/counter", "value": self.tick}]}
                rows = {"checksums.jsonl": dict(envelope, digests=digests(state)),
                        "state-deltas.jsonl": dict(envelope, **delta)}
                self.previous = state
            for name, value in rows.items():
                self.files[name].write(canonical(value).decode() + "\n")
                self.files[name].flush()

        def exchange(self, payload, timeout):
            request = json.loads(payload)
            rid, method = request["request_id"], request["method"]
            if method == "hello":
                result = {"build": build, "game": game,
                          "capabilities": {"observe": True, "audit_snapshot": True, "advance": True}}
            elif method == "observe":
                result = self.observe()
            elif method == "audit_snapshot":
                result = {"state": self.state(), "version": self.version()}
            elif method == "advance":
                if request["expect"] != self.version():
                    raise ValueError("synthetic counter received stale version")
                count = request["params"]["max_ticks"]
                self.record("request_started", {"request_id": rid, "request": request})
                for executed in range(count):
                    self.record("pre_step", {"request_id": rid, "requested_ticks": count, "executed_ticks": executed})
                    self.tick += 1
                    self.record("post_step", {"request_id": rid, "native_tick_delta": 1, "executed_ticks": executed + 1})
                result = {"action_results": [], "requested_ticks": count, "executed_ticks": count,
                          "stop_reason": "budget_exhausted", "observation": self.observe()}
                self.record("request_completed", {"request_id": rid, "result": result})
            elif method == "stop_recording":
                self.record("recording_closed", {"request_id": rid})
                result = {"closed": True, "observation": self.observe()}
            else:
                raise ValueError(f"unsupported synthetic method: {method}")
            return canonical({"protocol": 1, "request_id": rid, "ok": True, "result": result})

        def close(self):
            for stream in self.files.values():
                stream.close()

    with SessionTrace(output / "source.jsonl") as trace, Client(CounterTransport(output / "source-audit", 1), trace=trace) as client:
        capture_initial(client, identity=identity, initialization={"synthetic_counter": True})
        client.advance(2)
        client.advance(3)
        client.request("stop_recording", expect=client.version)
    trajectory = build_trajectory(output / "source.jsonl", output / "source-audit", output / "trajectory")

    @contextmanager
    def initialize(trajectory, run):
        with SessionTrace(run / "session.jsonl") as trace, Client(CounterTransport(run / "audit", 99), trace=trace) as client:
            yield ReplaySession(client, copy.deepcopy(identity), run / "audit")
            client.request("stop_recording", expect=client.version)

    report = replay(trajectory, initialize, output / "replayed")
    return {"synthetic": True, "launched_game": False, "report": str(output / "replayed/replay-report.json"),
            "equal": report["equal"], "original_engine_replay_verified": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pack = commands.add_parser("pack", help="package a closed SessionTrace and native audit")
    pack.add_argument("trace", type=Path)
    pack.add_argument("audit", type=Path)
    pack.add_argument("output", type=Path)
    native = commands.add_parser("pack-native", help="package authoritative native events with live initial metadata")
    native.add_argument("initial", type=Path)
    native.add_argument("audit", type=Path)
    native.add_argument("output", type=Path)
    inspect = commands.add_parser("inspect", help="verify evidence hashes, schemas and complete actual execution")
    inspect.add_argument("trajectory", type=Path)
    run = commands.add_parser("run", help="cold-start using an explicitly supplied initializer module:function")
    run.add_argument("trajectory", type=Path)
    run.add_argument("output", type=Path)
    run.add_argument("--initializer", required=True)
    run.add_argument("--target-tick", type=int)
    demonstration = commands.add_parser("demo", help="exercise packaging and replay with a synthetic counter; never starts PvZ")
    demonstration.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            print(json.dumps(demo(args.output), indent=2))
            return 0
        if args.command == "run":
            import importlib
            module, separator, name = args.initializer.partition(":")
            if not separator or not module or not name:
                raise EvidenceError("initializer must be module:function")
            initializer = getattr(importlib.import_module(module), name)
            print(json.dumps(replay(args.trajectory, initializer, args.output, target_tick=args.target_tick), indent=2))
            return 0
        if args.command == "pack":
            trajectory = build_trajectory(args.trace, args.audit, args.output)
        elif args.command == "pack-native":
            trajectory = build_trajectory(None, args.audit, args.output, initial=args.initial)
        else:
            trajectory = Trajectory.load(args.trajectory)
        print(json.dumps({"trajectory_id": trajectory.manifest["trajectory_id"], "requests": len(trajectory.steps),
                          "final_tick": _step_end_version(trajectory.steps[-1])["tick"],
                          "engine_call_health": trajectory.audit.engine_call_health,
                          "capture_interventions": sum(step["request"]["method"] == "capture_frame" for step in trajectory.steps),
                          "audit_frames": len(trajectory.audit.frames), "original_engine_replay_verified": False}, indent=2))
        return 0
    except (EvidenceError, OSError, KeyError, TypeError) as error:
        parser.exit(1, f"replay evidence rejected: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
