"""Execute recorded actions through the normal runtime Client; never invoke an LLM.

This is verified scheduling and captured-state comparison, not a declaration
that every original-engine source of nondeterminism has been controlled.
"""
from __future__ import annotations

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
                            audit_files, digests, file_hash, first_difference, hash_backend, jsonl, particle_semantics, read_json, version)
from .client import Client, OutcomeUnknown, RemoteError

SCHEMA = "lvz.engine-replay.v1"
READ_ONLY = {"hello", "observe", "status", "audit_snapshot"}
STEP_METHODS = {"commit", "advance"}
INTERVENTIONS = STEP_METHODS | {"capture_frame", "pause"}


def _write(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


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
    return {"build": copy.deepcopy(hello["build"]), "game": copy.deepcopy(hello["game"]),
            "artifacts": copy.deepcopy(artifacts)}


def capture_initial(client: Client, *, identity: dict, initialization: dict) -> dict:
    """Call after initialization, before the first experiment action.

    The marker and state are captured from the live client. The caller supplies
    checked launcher artifact hashes; these must never be copied from a desired
    trajectory when describing a newly launched process.
    """
    hello = client.hello()
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


def _validate_initial(initial: dict) -> None:
    if initial.get("schema") != SCHEMA:
        raise EvidenceError("initial marker schema mismatch")
    _validate_identity(initial.get("identity"))
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
    digests(initial["state"])


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
    if (payload.get("tick_delta_verified") is not True
            or payload.get("board_identity_preserved") is not True
            or type(payload.get("native_tick_delta")) is not int or payload["native_tick_delta"] != 1):
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
    if (len(starts) != 1 or len(ends) != 1 or starts[0]["version"] != before
            or starts[0]["payload"].get("request") != request
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


def _validate_steps(initial: dict, steps: list[dict], audit: AuditLog) -> None:
    if not steps:
        raise EvidenceError("trajectory has no executed requests")
    if audit.manifest != initial["identity"]["game"]:
        raise EvidenceError("native audit target/coverage identity differs from hello")
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
        if terminal and (step_index != len(steps) - 1 or executed == 0
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
        if len(frames) != executed * 2:
            raise EvidenceError("source lacks pre/post evidence for every executed tick")
        for index, frame in enumerate(frames):
            expected_version = {"epoch": current["epoch"], "tick": current["tick"] + (index + 1) // 2,
                                "revision": current["revision"] + len(outcomes) if index == 0 else 0}
            if (frame.version != expected_version
                    or frame.payload.get("executed_ticks") != (index + 1) // 2):
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
        unsigned = {key: value for key, value in manifest.items() if key != "trajectory_id"}
        if manifest.get("schema") != SCHEMA or manifest.get("trajectory_id") != _hash(unsigned):
            raise EvidenceError("trajectory schema/content identity mismatch")
        audit_directory = path.parent / "audit"
        expected_files = {"audit/" + name for name in audit_files(audit_directory, read_json(audit_directory / "manifest.json"))}
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
                     output_directory: str | Path, *, initial: str | Path | None = None) -> Trajectory:
    """Package a closed recording. Exactly one source is required.

    Native-only input still requires a live-captured initial.json identity/state.
    It cannot recover an omitted initial state from later observations.
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
        dest = f"audit/{name}"
        shutil.copyfile(Path(audit_directory) / name, output / dest)
        filenames.append(dest)
    source_name = "session.jsonl" if trace is not None else "initial.json"
    shutil.copyfile(Path(trace if trace is not None else initial), output / source_name)
    filenames.append(source_name)
    manifest = {"schema": SCHEMA, "source": "session_trace" if trace is not None else "native",
                "initial": marker, "steps": steps,
                "files": {name: file_hash(output / name) for name in filenames},
                "scope": "captured state only; no completeness or original-engine determinism certification"}
    manifest["trajectory_id"] = _hash(manifest)
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
    if (on_takeover is not None and last_simulation is not None and last_simulation["result"]["stop_reason"] == "scene_changed"
            and (target_tick is None or target_tick == end_tick)):
        raise ValueError("a terminal scene transition cannot hand off a fight; seek an earlier stable boundary")
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=False)
    report = {"schema": SCHEMA, "parent_trajectory_id": trajectory.manifest["trajectory_id"],
              "branch_id": uuid.uuid4().hex, "requested_target_tick": target_tick,
              "mode": "cold_start_recompute", "original_engine_replay_verified": False,
              "scope": "captured state and actual action outcomes", "requests": [], "equal": False}
    report["audit_hash_backend"] = hash_backend()
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
            require_equal(session.identity["build"], hello.get("build"), "live_build")
            require_equal(session.identity["game"], hello.get("game"), "live_game")
            required = {"observe", "audit_snapshot"} | {step["request"]["method"] for step in trajectory.steps if step["request"]["method"] in STEP_METHODS}
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
            # The actual trace remains independently packageable, including a
            # later branch continuation. It starts at actual B(0), not at the
            # seek target. Preserve the parent link without forging a snapshot.
            client.trace.emit("replay_initial", {"schema": SCHEMA, "identity": copy.deepcopy(session.identity),
                "initialization": copy.deepcopy(trajectory.initial["initialization"]),
                "observation": copy.deepcopy(observation), "state": copy.deepcopy(snapshot["state"]),
                "parent": {"trajectory_id": trajectory.manifest["trajectory_id"],
                           "branch_id": report["branch_id"], "target_tick": target_tick,
                           "epoch_mapping": copy.deepcopy(report["epoch_mapping"])}})
            actual_ids = {}
            reached_version = client.version
            source_frame_stream = trajectory.audit._frames(reuse_state=True)
            actual_audit = AuditTail(session.audit_directory)
            require_equal(trajectory.audit.manifest, actual_audit.manifest, "audit_manifest")
            for ordinal, step in enumerate(trajectory.steps):
                if target_tick is not None and client.version["tick"] >= target_tick:
                    break
                req, expected = step["request"], step.get("result")
                source_before = trajectory.initial["observation"]["version"] if ordinal == 0 else _step_end_version(trajectory.steps[ordinal - 1])
                require_equal(mapped_version(source_before), client.version, f"request[{ordinal}].before")
                params = copy.deepcopy(req["params"])
                partial = req["method"] in STEP_METHODS and target_tick is not None and expected["observation"]["version"]["tick"] > target_tick
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
                    require_equal(wanted, actual_metadata, f"capture[{ordinal}].metadata")
                    require_equal(step["pixels_evidence"] is not None, actual_pixels is not None, f"capture[{ordinal}].pixel_evidence_present")
                    if actual_pixels is not None:
                        require_equal(step["pixels_evidence"]["byte_length"], actual_pixels["byte_length"], f"capture[{ordinal}].pixel_byte_length")
                    # Read the actual current observation even after an explicit
                    # capture failure; never synthesize it from expected values.
                    observation = client.observe()
                    require_equal(mapped_version(step["after_version"]), observation["version"], f"capture[{ordinal}].after_version")
                    if "observation_after" in step:
                        require_equal(mapped_observation(step["observation_after"]), observation, f"capture[{ordinal}].observation")
                    if "state_after" in step:
                        snapshot = client.request("audit_snapshot")
                        require_equal(client.version, snapshot.get("version"), f"capture[{ordinal}].snapshot_boundary")
                        require_equal(step["state_after"], snapshot.get("state"), f"capture[{ordinal}].state")
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
                    require_equal("budget_exhausted", actual.get("stop_reason"), "partial.stop_reason")
                    require_equal(expected["action_results"], actual.get("action_results"), "partial.action_results")
                    require_equal({"epoch": live_version["epoch"], "tick": target_tick, "revision": 0},
                                  actual.get("observation", {}).get("version"), "partial.boundary")
                else:
                    wanted = copy.deepcopy(expected)
                    wanted["observation"] = mapped_observation(wanted["observation"])
                    require_equal(wanted, actual, f"request[{ordinal}].result")
                source_count = actual["executed_ticks"] * 2
                actual_frames = actual_audit.read_request(actual_id)
                actual_count = 0
                last_source_frame = None
                for index, right in enumerate(actual_frames):
                    left = next(source_frame_stream)
                    actual_count += 1
                    require_equal(req["request_id"], left.payload.get("request_id"), "source_frame_request")
                    require_equal(left.kind, right.kind, f"audit[{ordinal}:{index}].phase")
                    require_equal(mapped_version(left.version), right.version, f"audit[{ordinal}:{index}].version")
                    require_equal([particle_semantics(event, map_version=mapped_version) for event in left.particle_seeds],
                                  [particle_semantics(event) for event in right.particle_seeds],
                                  f"audit[{ordinal}:{index}].particle_shake")
                    report["particle_shake"]["semantic_seed_calls_compared"] += len(right.particle_seeds)
                    # Both canonical byte strings were reconstructed from the
                    # actual JSON Patches and checked against every native
                    # digest. Compare the bytes themselves, not only FNV hashes.
                    if left.canonical_state != right.canonical_state:
                        require_equal(left.state, right.state, f"audit[{ordinal}:{index}].state")
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
                                   "actual_version": client.version, "epoch_mapping": report["epoch_mapping"]}
            client.trace.emit("replay_takeover" if on_takeover else "replay_completed", report["takeover"])
            # A live consumer can continue with the exact returned epoch/version.
            if on_takeover is not None:
                on_takeover(session, copy.deepcopy(report["takeover"]))
        if trajectory.audit.manifest.get("particle_shake") is not None:
            if on_takeover is None:
                actual_audit.verify_closed()
            else:
                AuditLog(session.audit_directory, require_closed=True)
            report["particle_shake"]["final_health_verified"] = True
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
                          "capture_interventions": sum(step["request"]["method"] == "capture_frame" for step in trajectory.steps),
                          "audit_frames": len(trajectory.audit.frames), "original_engine_replay_verified": False}, indent=2))
        return 0
    except (EvidenceError, OSError, KeyError, TypeError) as error:
        parser.exit(1, f"replay evidence rejected: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
