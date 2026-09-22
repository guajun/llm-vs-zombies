"""Offline A/B comparison of two gargantuar fork runs (issue #51, part A/B).

This tool never starts a game and never writes inside a run directory. It
rebuilds each run's board state from the sealed ``audit/state-deltas.jsonl``
evidence (one initial state followed by the RFC 6902 patch stream the native
recorder emits) and reports:

* the gargantuar's (zombie type 23) ``mVelX`` timeline -- field offset
  ``00000034``, i.e. ``Zombie +0x34``, handled as the raw 32-bit IEEE-754 word
  that ``docs/机制读取点表.json`` records for this field (AvZ
  ``AZombie::Speed()`` at ``0x34``);
* the same timeline for ``mZombiePhase`` (``+0x28``), the float abscissa
  (``+0x2c``) and the body reanimation's ``mLoopCount`` / ``mAnimTime``, so the
  post-smash resample can be bound to a tick and compared bit for bit;
* whether the recorded global MT state reproduces that resample (the audit
  stores ``rng.instances.global_mt`` as ``words[624]`` plus ``cursor``), which
  shows the value came from ``RandRangeFloat(0.23f, 0.37f)`` rather than from a
  coincidence;
* the first frame in which the two runs differ -- by native per-frame digests
  by default, or field by field with ``--exact`` -- so "the speeds match"
  cannot be confused with "the fork never reached the board".

Frames are streamed: only the current state and the extracted projections stay
resident, never the whole state history.

Timeline convention: a frame's ``tick`` is the native ``version.tick`` and
``kind`` is the native frame kind. The recorder writes ``post_step(k)`` after
the k-th engine update and ``pre_step(k)`` at the preparation boundary before
the (k+1)-th update, both carrying ``version.tick == k``; the stream order is
``pre_step(0)``, ``post_step(1)``, ``pre_step(1)``, ``post_step(2)``, ... The
``index`` field is the 0-based line number in the evidence file and orders
frames inside one tick without reinterpreting the native label.
"""
from __future__ import annotations

import argparse
import copy
import gzip
import json
import struct
import sys
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from llm_vs_zombies.audit_compare import EvidenceError, digests, first_difference, patch
from llm_vs_zombies.evidence_codec import EvidenceStore

STATE_DELTAS = "state-deltas.jsonl"
CHECKSUMS = "checksums.jsonl"
AUDIT_DIRECTORY = "audit"

# AvZ field offsets for the pinned engine (avz/framework/inc/avz_pvz_struct.h).
GIANT_TYPE = 23                     # AZombieType AGARGANTUAR
PHASE_GARGANTUAR_SMASHING = 70      # AZombie::State() while the hammer is up
ANIMATION_PLAY_ONCE_AND_HOLD = 3    # ReanimLoopType used by anim_smash
SMASH_TIMED_EVENT = 0.64            # Lawn_Zombie.cpp timed event that squishes the square

# Global MT19937-31 as the pinned engine implements it
# (work/replay-research/SexyAppFramework_MTRand.cpp; the audit records its
# ``words`` array and ``cursor`` as ``rng.instances.global_mt``).
MT_N, MT_M, MT_MATRIX_A = 624, 397, 0x9908B0DF
MT_UPPER_MASK, MT_LOWER_MASK = 0x80000000, 0x7FFFFFFF
VELOCITY_MIN, VELOCITY_MAX = 0.23, 0.37    # Zombie::PickRandomSpeed -> RandRangeFloat(0.23f, 0.37f)
MT_PROBE_DRAWS = 512                       # draws searched for one observed resample
ZOMBIE_TYPE = "00000024"
ZOMBIE_PHASE = "00000028"
ZOMBIE_X = "0000002c"
ZOMBIE_Y = "00000030"
ZOMBIE_VELX = "00000034"            # mVelX, raw IEEE-754 bits
ZOMBIE_ID = "00000020"
ZOMBIE_HP = "000000c8"
ZOMBIE_ANIMATION = "00000118"
PLANT_HURT_WIDTH = "00000010"
PLANT_ROW = "0000001c"
PLANT_TYPE = "00000024"
PLANT_COL = "00000028"
PLANT_HP = "00000040"
PLANT_HP_MAX = "00000044"
PLANT_ANIMATION = "00000094"
PLANT_DISAPPEARED = "00000141"
PLANT_CRUSHED = "00000142"


def float32(bits):
    """Decode a raw 32-bit word as the engine's IEEE-754 single."""
    return struct.unpack("<f", struct.pack("<I", int(bits) & 0xFFFFFFFF))[0]


def maybe_float32(bits):
    """Same decode, but a field the audit never recorded stays ``None``."""
    return float32(bits) if isinstance(bits, int) else None


def round32(value):
    """Round a Python double to the engine's 32-bit single precision."""
    return struct.unpack("<f", struct.pack("<f", value))[0]


def mt_twist(words):
    """One MT19937 reload of ``mt[]`` exactly as SexyAppFramework does it."""
    state = list(words)
    for kk in range(0, MT_N - MT_M):
        y = (state[kk] & MT_UPPER_MASK) | (state[kk + 1] & MT_LOWER_MASK)
        state[kk] = state[kk + MT_M] ^ (y >> 1) ^ (MT_MATRIX_A if y & 1 else 0)
    for kk in range(MT_N - MT_M, MT_N - 1):
        y = (state[kk] & MT_UPPER_MASK) | (state[kk + 1] & MT_LOWER_MASK)
        state[kk] = state[kk + (MT_M - MT_N)] ^ (y >> 1) ^ (MT_MATRIX_A if y & 1 else 0)
    y = (state[MT_N - 1] & MT_UPPER_MASK) | (state[0] & MT_LOWER_MASK)
    state[MT_N - 1] = state[MT_M - 1] ^ (y >> 1) ^ (MT_MATRIX_A if y & 1 else 0)
    return state


def mt_draws(words, cursor, count):
    """The next ``count`` values of ``MTRand::NextNoAssert() & 0x7FFFFFFF``."""
    state, index, values = list(words), int(cursor), []
    for _ in range(count):
        if index >= MT_N:
            state, index = mt_twist(state), 0
        y = state[index]
        index += 1
        y ^= y >> 11
        y ^= (y << 7) & 0x9D2C5680
        y ^= (y << 15) & 0xEFC60000
        y ^= y >> 18
        values.append(y & MT_LOWER_MASK)
    return values


def velocity_from_draw(word, minimum=VELOCITY_MIN, maximum=VELOCITY_MAX):
    """``RandRangeFloat(theMin, theMax) = Rand(theMax - theMin) + theMin`` in float32."""
    span = round32(round32(maximum) - round32(minimum))
    return round32(round32(word / MT_LOWER_MASK * span) + round32(minimum))


def match_velocity_draw(snapshot, bits, *, draws=MT_PROBE_DRAWS):
    """Search the MT draws after ``snapshot`` for the observed ``mVelX`` bits."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("words"), list):
        return None
    words, cursor = snapshot["words"], snapshot.get("cursor")
    if not isinstance(cursor, int) or len(words) < MT_N:
        return None
    matches = [index for index, word in enumerate(mt_draws(words, cursor, draws))
               if struct.unpack("<I", struct.pack("<f", velocity_from_draw(word)))[0] == bits]
    return {"cursor_before": cursor, "draws_searched": draws, "draw_index": matches[0] if matches else None,
            "matches": matches, "velocity": (velocity_from_draw(mt_draws(words, cursor, matches[0] + 1)[-1])
                                             if matches else None)}


def hex_word(bits):
    return "0x%08x" % (int(bits) & 0xFFFFFFFF)


@dataclass(frozen=True)
class Frame:
    """One rebuilt state plus the native envelope that produced it."""

    index: int
    seq: int | None
    kind: str
    epoch: int | None
    tick: int
    revision: int | None
    state: dict

    def stamp(self):
        return {"index": self.index, "seq": self.seq, "kind": self.kind, "tick": self.tick}


def audit_directory(target):
    path = Path(target)
    if path.is_dir() and (path / AUDIT_DIRECTORY).is_dir():
        return path / AUDIT_DIRECTORY
    return path


def resolve_evidence(target, name):
    """Resolve one logical audit evidence file inside a run or audit directory."""
    store = EvidenceStore(audit_directory(target), error=EvidenceError)
    return store.stored_path(name), store


def resolve_state_deltas(target):
    """Accept a run directory, an ``audit`` directory, or the JSONL file itself."""
    path = Path(target)
    if path.is_file():
        return path, None
    stored, store = resolve_evidence(target, STATE_DELTAS)
    if not stored.is_file():
        raise EvidenceError(f"missing audit evidence: {stored}")
    return stored, store.plain_sha256(STATE_DELTAS)


def read_records(path):
    """Stream one JSONL evidence file (plain or gzip container)."""
    with open_records(path) as stream:
        for number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise EvidenceError(f"{path}:{number}: object required")
            yield value


def open_records(path):
    """Open one JSONL evidence file for text reading (plain or gzip container)."""
    opener = gzip.open if Path(path).suffix == ".gz" else open
    return opener(path, "rt", encoding="utf-8")


def replay(records):
    """Yield one :class:`Frame` per record, rebuilding state with JSON Patch.

    The first record must carry the whole initial state; every later record must
    carry a patch.
    """
    state = None
    for index, record in enumerate(records):
        if index == 0:
            if "initial" not in record or "patch" in record or not isinstance(record["initial"], dict):
                raise EvidenceError("first audit record must carry the initial state")
            state = record["initial"]
        else:
            operations = record.get("patch")
            if not isinstance(operations, list):
                raise EvidenceError("every later audit record must carry a patch")
            state = patch(state, operations, in_place=True)
        version = record.get("version")
        if not isinstance(version, dict):
            raise EvidenceError("audit record lacks a version")
        kind = record.get("kind")
        if kind not in {"pre_step", "post_step"}:
            raise EvidenceError(f"unexpected audit frame kind: {kind!r}")
        yield Frame(index=index, seq=record.get("seq"), kind=kind, epoch=version.get("epoch"),
                    tick=version.get("tick"), revision=version.get("revision"), state=state)


def giants(state):
    """Every live gargantuar slot of one state, keyed by zombie pool slot index."""
    found = {}
    for key, entry in state.get("zombies", {}).get("slots", {}).items():
        fields = entry.get("fields")
        if not isinstance(fields, dict) or fields.get(ZOMBIE_TYPE) != GIANT_TYPE:
            continue
        found[key] = {
            "slot": key,
            "id": entry.get("id_or_free_next"),
            "id_field": fields.get(ZOMBIE_ID),
            "type": fields.get(ZOMBIE_TYPE),
            "phase": fields.get(ZOMBIE_PHASE),
            "x_bits": fields.get(ZOMBIE_X),
            "y_bits": fields.get(ZOMBIE_Y),
            "velx_bits": fields.get(ZOMBIE_VELX),
            "hp": fields.get(ZOMBIE_HP),
            "animation": copy.deepcopy(fields.get(ZOMBIE_ANIMATION)),
        }
    return found


def mt_snapshot(state):
    """A detached copy of the recorded global MT state.

    The rebuilt board state is patched in place, so a snapshot must not keep a
    reference to the live ``rng`` subtree.
    """
    snapshot = state.get("rng", {}).get("instances", {}).get("global_mt")
    if not isinstance(snapshot, dict):
        return None
    return {"cursor": snapshot.get("cursor"), "words": list(snapshot.get("words") or [])}


def giant_view(view, frame):
    return dict(view, **frame.stamp(), event="change", velx=float32(view["velx_bits"]),
                velx_hex=hex_word(view["velx_bits"]), x=float32(view["x_bits"]),
                y=maybe_float32(view["y_bits"]))


def animation_entry(state, node):
    entry = state.get("reanimations", {}).get("nodes", {}).get(node)
    if not isinstance(entry, dict):
        return None
    value = entry.get("state")
    return value if isinstance(value, dict) else None


def animation_view(value, node, frame):
    bits = value.get("anim_time_bits")
    return {"node": node, **frame.stamp(), "loop_count": value.get("loop_count"),
            "loop_type": value.get("loop_type"), "frame_count": value.get("frame_count"),
            "frame_start": value.get("frame_start"), "anim_time_bits": bits,
            "anim_time": float32(bits) if isinstance(bits, int) else None,
            "anim_rate_bits": value.get("anim_rate_bits"), "dead": value.get("dead")}


def plant_view(entry, fields):
    return {"id": entry.get("id_or_free_next"), "type": fields.get(PLANT_TYPE),
            "x": fields.get("00000008"), "y": fields.get("0000000c"),
            "hurt_width": fields.get(PLANT_HURT_WIDTH),
            "row": fields.get(PLANT_ROW), "col": fields.get(PLANT_COL),
            "hp": fields.get(PLANT_HP), "max_hp": fields.get(PLANT_HP_MAX),
            "visible": fields.get("00000018"), "animation": fields.get(PLANT_ANIMATION),
            "disappeared": fields.get(PLANT_DISAPPEARED), "crushed": fields.get(PLANT_CRUSHED)}


@dataclass
class Analysis:
    directory: str
    evidence: str
    plain_sha256: str | None
    frames: int
    first_frame: dict | None
    last_frame: dict | None
    giants: dict
    giant: dict
    plants: dict
    animations: dict
    animation_events: dict
    verify: dict | None = None

    def as_dict(self):
        return {"directory": self.directory, "evidence": self.evidence, "plain_sha256": self.plain_sha256,
                "frames": self.frames, "first_frame": self.first_frame, "last_frame": self.last_frame,
                "giants": self.giants, "giant": self.giant, "plants": self.plants,
                "animations": self.animations, "animation_events": self.animation_events,
                "verify": self.verify}


def analyze(target, *, plant_type=None, verify=False):
    """Rebuild one run in a single streaming pass and extract the projections."""
    path, plain = resolve_state_deltas(target)
    checksum_path = None
    if verify:
        try:
            checksum_path, _ = resolve_evidence(target, CHECKSUMS)
        except EvidenceError:
            checksum_path = None
    # The checksum stream is small (one digest dict per frame) and is read
    # eagerly so the evidence handle never outlives the analysis.
    checksum_records = list(read_records(checksum_path)) if checksum_path is not None else []
    checksum_position = 0
    verify_state = {"available": checksum_path is not None, "checked": 0, "mismatches": [],
                    "missing_records": 0, "trailing_records": 0}

    frames = 0
    first_frame = last_frame = None
    giant_slots = {}
    giant = {"slot": None, "animation": None, "changes": []}
    giant_signature = None
    giant_present = False
    plants = {}
    plant_signatures = {}
    animations = {}
    animation_events = {}
    animation_signature = None
    animation_probe = {}
    last_giant = None
    previous_mt = None

    for frame in _records_of(path):
        frames += 1
        first_frame = first_frame or frame.stamp()
        last_frame = frame.stamp()
        if verify:
            record = (checksum_records[checksum_position]
                      if checksum_position < len(checksum_records) else None)
            checksum_position += 1
            recorded = record.get("digests") if isinstance(record, dict) else None
            if not isinstance(recorded, dict):
                verify_state["missing_records"] += 1
            else:
                computed = digests(frame.state)
                verify_state["checked"] += 1
                if computed != recorded:
                    verify_state["mismatches"].append(
                        {"seq": frame.seq, "kind": frame.kind, "tick": frame.tick,
                         "sections": sorted(key for key in set(computed) | set(recorded)
                                            if computed.get(key) != recorded.get(key))})
        live = giants(frame.state)
        for key, view in live.items():
            record = giant_slots.setdefault(key, {"id": view["id"], "first": None, "last": None})
            record["first"] = record["first"] or frame.stamp()
            record["last"] = frame.stamp()
        if giant["slot"] is None and live:
            slot = sorted(live, key=int)[0]
            giant["slot"] = slot
            animation = live[slot]["animation"]
            giant["animation"] = animation.get("node") if isinstance(animation, dict) else None
        slot = giant["slot"]
        if slot is not None:
            view = live.get(slot)
            last_giant = dict(giant_view(view, frame), event="last") if view is not None else None
            previous_velocity = giant_signature[0] if giant_signature is not None else None
            if view is None:
                if giant_present:
                    giant["changes"].append(dict(frame.stamp(), slot=slot, event="gone"))
                giant_present, giant_signature = False, None
            else:
                signature = (view["velx_bits"], view["phase"], view["hp"])
                if not giant_present:
                    giant["changes"].append(dict(giant_view(view, frame), event="appear"))
                    giant_present, giant_signature = True, signature
                elif signature != giant_signature:
                    change = giant_view(view, frame)
                    if view["velx_bits"] != previous_velocity:
                        change["mt_draw"] = match_velocity_draw(previous_mt, view["velx_bits"])
                    giant["changes"].append(change)
                    giant_signature = signature
            node = giant["animation"]
            value = animation_entry(frame.state, node) if node is not None else None
            if value is not None:
                signature = (value.get("loop_count"), value.get("loop_type"), value.get("frame_count"))
                if signature != animation_signature:
                    animations.setdefault(node, []).append(animation_view(value, node, frame))
                animation_signature = signature
                probe = animation_probe.get(node)
                now = {"loop_type": value.get("loop_type"), "loop_count": value.get("loop_count")}
                bits = value.get("anim_time_bits")
                anim_time = float32(bits) if isinstance(bits, int) else None
                events = animation_events.setdefault(node, [])
                if probe is None or now["loop_type"] != probe["loop_type"]:
                    if now["loop_type"] == ANIMATION_PLAY_ONCE_AND_HOLD:
                        events.append(dict(animation_view(value, node, frame),
                                           event="play_once_and_hold_start"))
                    elif probe is not None and probe["loop_type"] == ANIMATION_PLAY_ONCE_AND_HOLD:
                        events.append(dict(animation_view(value, node, frame), event="walk_animation_resumed"))
                    probe = {"loop_type": now["loop_type"], "loop_count": now["loop_count"], "crossed": False}
                if now["loop_count"] != probe["loop_count"]:
                    events.append(dict(animation_view(value, node, frame), event="loop_count_changed",
                                       loop_count_from=probe["loop_count"], loop_count_to=now["loop_count"]))
                    probe["loop_count"] = now["loop_count"]
                if (now["loop_type"] == ANIMATION_PLAY_ONCE_AND_HOLD and not probe["crossed"]
                        and anim_time is not None and anim_time >= SMASH_TIMED_EVENT):
                    events.append(dict(animation_view(value, node, frame), event="anim_time_reached_0.64"))
                    probe["crossed"] = True
                animation_probe[node] = probe
        previous_mt = mt_snapshot(frame.state)
        if plant_type is not None:
            for key, entry in frame.state.get("plants", {}).get("slots", {}).items():
                fields = entry.get("fields")
                if not isinstance(fields, dict) or fields.get(PLANT_TYPE) != plant_type:
                    continue
                view = plant_view(entry, fields)
                record = plants.setdefault(key, {"slot": key, "first": None, "last": None, "changes": []})
                record["first"] = record["first"] or dict(view, **frame.stamp())
                record["last"] = dict(view, **frame.stamp())
                signature = (view["hp"], view["disappeared"], view["crushed"], view["visible"])
                if plant_signatures.get(key) != signature:
                    plant_signatures[key] = signature
                    record["changes"].append(dict(view, **frame.stamp()))
    if verify:
        verify_state["trailing_records"] = len(checksum_records) - checksum_position
    giant["last"] = last_giant
    return Analysis(directory=str(target), evidence=str(path), plain_sha256=plain, frames=frames,
                    first_frame=first_frame, last_frame=last_frame, giants=giant_slots, giant=giant,
                    plants=plants, animations=animations, animation_events=animation_events,
                    verify=verify_state if verify else None)


def compare_digests(target_a, target_b, *, limit=None):
    """Lockstep scan of the native per-frame digests of two runs.

    The native recorder hashes every rebuilt section per frame, so equal digest
    dicts mean equal canonical states at that frame. This scan is exact over the
    whole horizon and cheap; ``compare_states`` is the slower literal
    field-by-field confirmation of the same claim.
    """
    result = {"compared": 0, "state": None, "envelope": None, "frame_count": None}
    sentinel = object()
    left_stream, right_stream = digest_frames(target_a), digest_frames(target_b)
    try:
        for left, right in zip_longest(left_stream, right_stream, fillvalue=sentinel):
            if left is sentinel or right is sentinel:
                extra = right if left is sentinel else left
                result["frame_count"] = {"side": "b" if left is sentinel else "a", "frame": extra}
                break
            result["compared"] += 1
            if result["envelope"] is None and (left["seq"], left["kind"], left["tick"]) != (
                    right["seq"], right["kind"], right["tick"]):
                result["envelope"] = {"a": left, "b": right}
            if result["state"] is None and left["digests"] != right["digests"]:
                sections = sorted(key for key in set(left["digests"] or {}) | set(right["digests"] or {})
                                  if (left["digests"] or {}).get(key) != (right["digests"] or {}).get(key))
                result["state"] = {"a": left, "b": right, "sections": sections}
                break
            if limit is not None and result["compared"] >= limit:
                break
    finally:
        left_stream.close()
        right_stream.close()
    return result


def digest_frames(target):
    """Stream the native per-frame digests of one run, closing the file on exit."""
    path, _ = resolve_evidence(target, CHECKSUMS)
    second = 0
    with open_records(path) as stream:
        for line in stream:
            if not line.strip():
                continue
            record = json.loads(line)
            version = record.get("version") or {}
            yield {"index": second, "seq": record.get("seq"), "kind": record.get("kind"),
                   "tick": version.get("tick"), "digests": record.get("digests")}
            second += 1


def confirm_digest_difference(target_a, target_b, index):
    """Rebuild both streams up to ``index`` and name the first differing field."""
    if index is None:
        return None
    states = []
    for target in (target_a, target_b):
        stream = stream_of(target)
        try:
            for position, frame in enumerate(stream):
                if position == index:
                    states.append(frame)
                    break
            else:
                states.append(None)
        finally:
            stream.close()
    if states[0] is None or states[1] is None:
        return None
    left, right = states
    difference = first_difference(left.state, right.state)
    return {"a": left.stamp(), "b": right.stamp(),
            "path": difference.get("path") if difference else None, "difference": difference}


def compare_states(target_a, target_b, *, limit=None):
    """Exact canonical state comparison of two rebuilt streams.

    ``reason`` names the first frame whose rebuilt board state differs, with the
    JSON Pointer of the first differing field; ``envelope`` names the first
    frame whose native ``seq``/``kind``/``tick`` envelope differs (a rejected
    action still consumes recorder sequence numbers). Extra frames on one side
    are reported as ``frame_count`` so a short or long stream cannot hide
    behind the alignment. Either entry is ``None`` when it never happens.
    """
    result = {"state": None, "envelope": None, "compared": 0, "frame_count": None}
    sentinel = object()
    left_stream, right_stream = stream_of(target_a), stream_of(target_b)
    try:
        for left, right in zip_longest(left_stream, right_stream, fillvalue=sentinel):
            if left is sentinel or right is sentinel:
                extra = right if left is sentinel else left
                result["frame_count"] = {"side": "b" if left is sentinel else "a", "frame": extra.stamp()}
                break
            result["compared"] += 1
            if result["envelope"] is None and (left.seq, left.kind, left.tick) != (right.seq, right.kind, right.tick):
                result["envelope"] = {"a": left.stamp(), "b": right.stamp()}
            if result["state"] is None:
                difference = first_difference(left.state, right.state)
                if difference is not None:
                    result["state"] = {"a": left.stamp(), "b": right.stamp(),
                                       "path": difference.get("path"), "difference": difference}
            if result["state"] is not None and result["envelope"] is not None:
                break
            if limit is not None and result["compared"] >= limit:
                break
    finally:
        left_stream.close()
        right_stream.close()
    return result


def stream_of(target):
    """Stream rebuilt frames of one run, closing the evidence file on exit."""
    path, _ = resolve_state_deltas(target)
    yield from _records_of(path)


def read_records_from(stream):
    for line in stream:
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise EvidenceError("audit JSONL record must be an object")
        yield value


def _records_of(path):
    """Frames of a state-deltas file, with the evidence handle scoped to the loop."""
    with open_records(path) as stream:
        yield from replay(read_records_from(stream))


def brief(frame):
    """Frame identity without the digest payload."""
    if frame is None:
        return None
    return {key: frame.get(key) for key in ("index", "seq", "kind", "tick") if key in frame}


def format_analysis(analysis):
    lines = [f"run          {analysis.directory}",
             f"evidence     {analysis.evidence}",
             f"plain sha256 {analysis.plain_sha256}",
             f"frames       {analysis.frames}: {analysis.first_frame} .. {analysis.last_frame}"]
    if analysis.verify is not None:
        lines.append(f"digests      available={analysis.verify['available']} checked={analysis.verify['checked']} "
                     f"mismatches={len(analysis.verify['mismatches'])} "
                     f"trailing={analysis.verify['trailing_records']}")
    if analysis.giant["slot"] is None:
        lines.append("giant        no zombie of type 23 in the audited horizon")
    else:
        lines.append(f"giant        slot {analysis.giant['slot']} slots={analysis.giants} "
                     f"body reanim={analysis.giant['animation']}")
        for change in analysis.giant["changes"]:
            if change["event"] == "gone":
                lines.append(f"  gone       {change['tick']:>6} {change['kind']:<9}")
                continue
            lines.append(f"  {change['event']:<10} {change['tick']:>6} {change['kind']:<9} "
                         f"phase={change['phase']:<3} x={change['x']:.6f} "
                         f"mVelX={change['velx_hex']} {change['velx']:.9f} hp={change['hp']}")
            draw = change.get("mt_draw")
            if draw is not None:
                lines.append(f"  {'MT draw':<10} cursor_before={draw['cursor_before']} "
                             f"draw_index={draw['draw_index']} matches={draw['matches'][:4]} "
                             f"velocity={draw['velocity']}")
    for node, changes in analysis.animations.items():
        lines.append(f"reanimation {node}")
        for change in changes:
            lines.append(f"  {change['tick']:>6} {change['kind']:<9} loop_count={change['loop_count']} "
                         f"loop_type={change['loop_type']} frame_count={change['frame_count']} "
                         f"anim_time={change['anim_time']} bits={change['anim_time_bits']}")
        for event in analysis.animation_events.get(node, []):
            lines.append(f"  {event['tick']:>6} {event['kind']:<9} {event['event']:<26} "
                         f"loop_count={event['loop_count']} loop_type={event['loop_type']} "
                         f"anim_time={event['anim_time']} bits={event['anim_time_bits']}")
    for key in sorted(analysis.plants, key=int):
        record = analysis.plants[key]
        lines.append(f"plant slot {key} type {record['first']['type']} row {record['first']['row']} "
                     f"col {record['first']['col']} x {record['first']['x']} "
                     f"first {record['first']['tick']} {record['first']['kind']} "
                     f"last {record['last']['tick']} {record['last']['kind']}")
        for change in record["changes"]:
            lines.append(f"  {change['tick']:>6} {change['kind']:<9} hp={change['hp']} "
                         f"visible={change['visible']} disappeared={change['disappeared']} "
                         f"crushed={change['crushed']}")
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline A/B diff of two gargantuar fork audit streams.")
    parser.add_argument("--a", required=True, help="run directory or audit/state-deltas.jsonl of run A")
    parser.add_argument("--b", help="run directory or audit/state-deltas.jsonl of run B")
    parser.add_argument("--plant-type", type=int, default=None,
                        help="also track plants of this engine type (8 = puff-shroom)")
    parser.add_argument("--verify", action="store_true",
                        help="cross-check rebuilt states against audit/checksums.jsonl digests")
    parser.add_argument("--json", type=Path, default=None, help="write the full analysis as JSON")
    parser.add_argument("--exact", action="store_true",
                        help="also run the slow canonical field-by-field state comparison")
    parser.add_argument("--limit", type=int, default=None, help="compare at most this many frames")
    arguments = parser.parse_args(argv)

    analysis_a = analyze(arguments.a, plant_type=arguments.plant_type, verify=arguments.verify)
    print("\n".join(format_analysis(analysis_a)))
    payload = {"schema": "lvz.giant-fork-diff.v1", "a": analysis_a.as_dict()}
    if arguments.b:
        analysis_b = analyze(arguments.b, plant_type=arguments.plant_type, verify=arguments.verify)
        print()
        print("\n".join(format_analysis(analysis_b)))
        payload["b"] = analysis_b.as_dict()
        divergence = compare_digests(arguments.a, arguments.b, limit=arguments.limit)
        payload["digest_divergence"] = divergence
        print()
        print(f"frames compared: {divergence['compared']}")
        print("first envelope difference: none" if divergence["envelope"] is None
              else "first envelope difference: " + json.dumps({side: brief(divergence["envelope"][side])
                                                                for side in ("a", "b")}, ensure_ascii=False))
        print("first digest difference: none" if divergence["state"] is None
              else "first digest difference: " + json.dumps({side: brief(divergence["state"][side])
                                                              for side in ("a", "b")}, ensure_ascii=False)
              + " sections=" + json.dumps(divergence["state"]["sections"]))
        if divergence["frame_count"] is not None:
            print("frame count difference: " + json.dumps(divergence["frame_count"], ensure_ascii=False))
        if arguments.exact:
            exact = compare_states(arguments.a, arguments.b, limit=arguments.limit)
            payload["exact_divergence"] = exact
            print(f"exact scan over {exact['compared']} frames: "
                  + ("no state difference" if exact["state"] is None
                     else json.dumps(exact["state"], ensure_ascii=False)))
    if arguments.json is not None:
        arguments.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {arguments.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
