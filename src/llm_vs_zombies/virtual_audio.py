"""Tick-driven virtual audio device and offline soundtrack model (N9, issue #37).

Three requirements pull the same audio path in different directions:

* the policy must not read audio, so audio state can never be part of the
  compared simulation state;
* the video needs an audible track;
* a real device query enters the simulation's control flow and changes RNG
  consumption (036 / #12), so the answer cannot come from a sound card.

This module answers all three with one tick-driven mixer: every device answer
(``is_playing`` / ``current_position``) is a pure function of the game tick and
the sound's own duration, so no sound card and no wall clock are involved. The
module also carries the *declared wiring* that keeps those answers out of the
simulation path (``observe_only``) next to the original-like wiring
(``device_feedback``) that this issue deliberately does not enable.

``docs/虚拟音频设备与离线音轨.md`` is the design of record: tick semantics,
declared semantic changes, native hook landing points and required interfaces.

This file never starts a process, opens a device, or reads a clock. It is a
host-side model plus the offline renderer used to produce a tick-aligned WAV.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import struct
import sys
import wave
from array import array
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

MODE = "virtual_audio_tick_v1"
EVENT_SCHEMA = "lvz.audio-event.v1"
RESOURCE_SCHEMA = "lvz.audio-resources.v1"
TRACK_SCHEMA = "lvz.audio-track.v1"
TRACE_SCHEMA = "lvz.audio-foley-trace.v1"

SAMPLE_RATE = 44100
TICKS_PER_SECOND = 100
SAMPLES_PER_TICK = SAMPLE_RATE // TICKS_PER_SECOND
BLOCK_ALIGN_BYTES = 4

# Declared mirror of the original constants: `SoundSystemHasFoleyPlayedTooRecently`
# compares against 10 centiseconds, `MAX_FOLEY_INSTANCES` is 8 per Foley type and
# the DirectSound manager owns 32 channels.
TOO_RECENT_UPDATES = 10
MAX_FOLEY_INSTANCES = 8
MAX_MANAGER_CHANNELS = 32

WIRINGS = ("observe_only", "device_feedback")
ADMISSION_POLICIES = ("all", "original")
EVENT_KINDS = ("play", "stop", "pause", "resume")
FLAGS = ("loop", "one_at_a_time", "dont_repeat", "mute_on_pause", "uses_music_volume")

# 1.0594630943592952645618252949463 == 2 ** (1 / 12): one semitone, the exact
# constant in DSoundInstance::AdjustPitch. The model declares float64 arithmetic
# and the original integer-Hz `SetFrequency` rounding step.
PITCH_STEP = 1.0594630943592952645618252949463
DEFAULT_BASE_FREQUENCY = 44100
MIN_FREQUENCY = 100
MAX_FREQUENCY = 100000

TONE_AMPLITUDE = 0.5
MAX_TRACK_TICKS = 60000  # ten minutes of tick-aligned audio per call


class AudioModelError(ValueError):
    """Rejected input for the tick-driven audio model."""


def fail(message: str) -> AudioModelError:
    return AudioModelError("virtual audio: " + message)


# --------------------------------------------------------------------------- #
# time base and validation helpers
# --------------------------------------------------------------------------- #


def tick_to_frame(tick: int, *, start_tick: int = 0, samples_per_tick: int = SAMPLES_PER_TICK) -> int:
    """Absolute sample frame of the first sample of ``tick``'s block."""
    _integer(tick, "tick")
    _integer(start_tick, "start_tick")
    _integer(samples_per_tick, "samples_per_tick", 1)
    if tick < start_tick:
        raise fail(f"tick {tick} precedes the track start {start_tick}")
    return (tick - start_tick) * samples_per_tick


def _integer(value: Any, name: str, minimum: int = 0, maximum: int | None = None) -> int:
    if type(value) is not int:
        raise fail(f"{name} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        limit = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
        raise fail(f"{name} must be {limit}")
    return value


def _boolean(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise fail(f"{name} must be a boolean")
    return value


def _number(value: Any, name: str) -> float:
    if type(value) not in (int, float):
        raise fail(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise fail(f"{name} must be finite")
    return number


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise fail(f"{name} must be a non-empty string")
    return value


def _keys(value: Any, required: Iterable[str], optional: Iterable[str], name: str) -> None:
    if not isinstance(value, dict):
        raise fail(f"{name} must be an object")
    required, optional = set(required), set(optional)
    if not required <= set(value) or not set(value) <= required | optional:
        raise fail(f"{name} has missing or unexpected keys: {sorted(set(value) ^ (required | optional))}")


# --------------------------------------------------------------------------- #
# resources, Foley types and the soundscape table
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SoundResource:
    """One source buffer plus the level the manager installs for it.

    ``source_kind`` is ``tone`` (a deterministic placeholder signal, used by the
    model tests and by assets that have not been prepared yet) or ``file`` (a
    16-bit PCM WAV at the track's sample rate). Levels are declared, not derived
    from the original mixer: the model claims sample coverage and tick
    alignment, not byte-exact loudness.
    """

    id: int
    name: str
    duration_samples: int
    source_kind: str = "tone"
    frequency_hz: float = 440.0
    path: str | None = None
    base_volume: float = 1.0
    pan: int = 0
    loop: bool = False

    def __post_init__(self) -> None:
        _integer(self.id, "sound id")
        _text(self.name, "sound name")
        _integer(self.duration_samples, "duration_samples", 1)
        if self.source_kind not in ("tone", "file"):
            raise fail(f"unsupported sound source kind {self.source_kind!r}")
        if self.source_kind == "tone":
            if not _number(self.frequency_hz, "frequency_hz") > 0.0:
                raise fail("frequency_hz must be positive")
        else:
            _text(self.path, "sound file path")
        volume = _number(self.base_volume, "base_volume")
        if not 0.0 <= volume <= 1.0:
            raise fail("base_volume must be within 0..1")
        _integer(self.pan, "pan", -10000, 10000)
        _boolean(self.loop, "loop")


@dataclass(frozen=True)
class FoleyType:
    """The declared part of the original ``FoleyParams`` table the model needs."""

    type_id: int
    name: str = ""
    resources: tuple[int | None, ...] = ()
    flags: frozenset[str] = frozenset()
    pitch_range: float = 0.0

    def __post_init__(self) -> None:
        _integer(self.type_id, "foley type id")
        if not isinstance(self.name, str):
            raise fail("foley type name must be a string")
        if len(self.resources) > 10:
            raise fail("a Foley type has at most 10 variations")
        for resource in self.resources:
            if resource is not None:
                _integer(resource, "variation resource id")
        if not isinstance(self.flags, frozenset) or not set(self.flags) <= set(FLAGS):
            raise fail(f"foley flags must be a subset of {list(FLAGS)}")
        _number(self.pitch_range, "pitch_range")

    @property
    def loop(self) -> bool:
        return "loop" in self.flags

    @property
    def one_at_a_time(self) -> bool:
        return "one_at_a_time" in self.flags


@dataclass(frozen=True)
class Soundscape:
    """Sounds plus Foley type flags: the whole declared audio-side input."""

    sounds: dict[int, SoundResource]
    types: dict[int, FoleyType] = field(default_factory=dict)
    sample_rate: int = SAMPLE_RATE
    ticks_per_second: int = TICKS_PER_SECOND

    def __post_init__(self) -> None:
        _integer(self.sample_rate, "sample_rate", 1)
        _integer(self.ticks_per_second, "ticks_per_second", 1)
        if self.sample_rate % self.ticks_per_second:
            raise fail("sample_rate must be an exact multiple of ticks_per_second")
        if not self.sounds:
            raise fail("a soundscape needs at least one sound")
        for key, sound in self.sounds.items():
            if key != sound.id:
                raise fail(f"sound table key {key} differs from its id")
        for key, entry in self.types.items():
            if key != entry.type_id:
                raise fail(f"foley type table key {key} differs from its id")
            for resource in entry.resources:
                if resource is not None and resource not in self.sounds:
                    raise fail(f"foley type {key} names unknown resource {resource}")

    @property
    def samples_per_tick(self) -> int:
        return self.sample_rate // self.ticks_per_second

    def resource(self, resource_id: int) -> SoundResource:
        try:
            return self.sounds[resource_id]
        except KeyError as error:
            raise fail(f"unknown resource {resource_id}") from error

    def type(self, type_id: int) -> FoleyType:
        try:
            return self.types[type_id]
        except KeyError as error:
            raise fail(f"unknown foley type {type_id}") from error


def sound_from_json(entry: Any, name: str = "sound") -> SoundResource:
    _keys(entry, ("id", "name", "duration_samples"),
          ("source", "base_volume", "pan", "loop", "frequency_hz", "path"), name)
    source = entry.get("source", {"kind": "tone"})
    _keys(source, ("kind",), ("frequency_hz", "path"), f"{name}.source")
    kind = source["kind"]
    if kind == "tone":
        frequency = source.get("frequency_hz", entry.get("frequency_hz", 440.0))
        return SoundResource(id=entry["id"], name=entry["name"], duration_samples=entry["duration_samples"],
                             source_kind="tone", frequency_hz=_number(frequency, f"{name}.frequency_hz"),
                             base_volume=entry.get("base_volume", 1.0), pan=entry.get("pan", 0),
                             loop=entry.get("loop", False))
    if kind == "file":
        return SoundResource(id=entry["id"], name=entry["name"], duration_samples=entry["duration_samples"],
                             source_kind="file", path=source.get("path", entry.get("path")),
                             base_volume=entry.get("base_volume", 1.0), pan=entry.get("pan", 0),
                             loop=entry.get("loop", False))
    raise fail(f"{name}.source kind {kind!r} is unsupported; prepare a 16-bit PCM WAV first")


def type_from_json(entry: Any, name: str = "type") -> FoleyType:
    _keys(entry, ("type",), ("name", "resources", "flags", "pitch_range"), name)
    resources = entry.get("resources", [])
    if not isinstance(resources, list):
        raise fail(f"{name}.resources must be a list")
    flags = entry.get("flags", [])
    if not isinstance(flags, list):
        raise fail(f"{name}.flags must be a list")
    return FoleyType(type_id=entry["type"], name=entry.get("name", ""), resources=tuple(resources),
                     flags=frozenset(flags),
                     pitch_range=_number(entry.get("pitch_range", 0.0), f"{name}.pitch_range"))


def soundscape_from_json(value: Any) -> Soundscape:
    _keys(value, ("schema", "sounds"), ("types", "sample_rate", "ticks_per_second"), "soundscape")
    if value["schema"] != RESOURCE_SCHEMA:
        raise fail(f"soundscape schema must be {RESOURCE_SCHEMA}")
    if not isinstance(value["sounds"], list):
        raise fail("soundscape.sounds must be a list")
    sounds = [sound_from_json(entry, f"sounds[{index}]") for index, entry in enumerate(value["sounds"])]
    table = {sound.id: sound for sound in sounds}
    if len(table) != len(sounds):
        raise fail("soundscape contains duplicate sound ids")
    raw_types = value.get("types", [])
    if not isinstance(raw_types, list):
        raise fail("soundscape.types must be a list")
    types = [type_from_json(entry, f"types[{index}]") for index, entry in enumerate(raw_types)]
    type_table = {entry.type_id: entry for entry in types}
    if len(type_table) != len(types):
        raise fail("soundscape contains duplicate foley type ids")
    soundscape = Soundscape(sounds=table, types=type_table,
                            sample_rate=value.get("sample_rate", SAMPLE_RATE),
                            ticks_per_second=value.get("ticks_per_second", TICKS_PER_SECOND))
    for sound in soundscape.sounds.values():
        if sound.source_kind == "file":
            frames, channels = wav_frames(sound.path)
            if frames != sound.duration_samples:
                raise fail(f"sound {sound.id} declares {sound.duration_samples} samples but {sound.path} has {frames}")
            if channels not in (1, 2):
                raise fail(f"sound {sound.id} must be mono or stereo")
    return soundscape


def load_soundscape(path: str | Path) -> Soundscape:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise fail(f"soundscape is not valid JSON: {error}") from error
    return soundscape_from_json(value)


def soundscape_to_json(soundscape: Soundscape) -> dict:
    sounds = []
    for sound in sorted(soundscape.sounds.values(), key=lambda item: item.id):
        source = {"kind": sound.source_kind}
        if sound.source_kind == "tone":
            source["frequency_hz"] = sound.frequency_hz
        else:
            source["path"] = sound.path
        sounds.append({"id": sound.id, "name": sound.name, "duration_samples": sound.duration_samples,
                       "source": source, "base_volume": sound.base_volume, "pan": sound.pan, "loop": sound.loop})
    types = [{"type": entry.type_id, "name": entry.name, "resources": list(entry.resources),
              "flags": sorted(entry.flags), "pitch_range": entry.pitch_range}
             for entry in sorted(soundscape.types.values(), key=lambda item: item.type_id)]
    return {"schema": RESOURCE_SCHEMA, "sample_rate": soundscape.sample_rate,
            "ticks_per_second": soundscape.ticks_per_second, "sounds": sounds, "types": types}


def wav_frames(path: str | Path | None) -> tuple[int, int]:
    try:
        with wave.open(str(path), "rb") as handle:
            if handle.getsampwidth() != 2:
                raise fail(f"{path} must be 16-bit PCM")
            if handle.getcomptype() != "NONE":
                raise fail(f"{path} must be uncompressed PCM")
            return handle.getnframes(), handle.getnchannels()
    except (OSError, wave.Error) as error:
        raise fail(f"cannot read {path}: {error}") from error


# --------------------------------------------------------------------------- #
# simulated Foley event stream
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AudioEvent:
    """One entry of ``lvz.audio-event.v1``: what the game asked the device to do.

    ``play`` carries the resource, the variation the game actually drew, the
    pitch steps that produced the playback rate, and the level/pan the manager
    installed. ``stop`` / ``pause`` / ``resume`` mirror ``TodFoley::StopFoley``
    and ``TodFoley::GamePause`` and act on the first live voice of ``type_id``.
    """

    seq: int
    tick: int
    kind: str
    type_id: int
    resource: int | None = None
    variation: int | None = None
    rate: tuple[int, int] = (1, 1)
    volume: float = 1.0
    pan: int = 0
    loop: bool = False
    voice: int | None = None
    pitch_steps: float = 0.0

    def __post_init__(self) -> None:
        _integer(self.seq, "event seq")
        _integer(self.tick, "event tick")
        if self.kind not in EVENT_KINDS:
            raise fail(f"event kind {self.kind!r} is not one of {list(EVENT_KINDS)}")
        _integer(self.type_id, "event type id")
        if self.kind == "play":
            _integer(self.resource, "event resource")
            if self.variation is not None:
                _integer(self.variation, "event variation", 0, 9)
            if not isinstance(self.rate, tuple) or len(self.rate) != 2:
                raise fail("event rate must be a (num, den) tuple")
            _integer(self.rate[0], "event rate numerator", 1)
            _integer(self.rate[1], "event rate denominator", 1)
            volume = _number(self.volume, "event volume")
            if not 0.0 <= volume <= 1.0:
                raise fail("event volume must be within 0..1")
            _integer(self.pan, "event pan", -10000, 10000)
            _boolean(self.loop, "event loop")
            _number(self.pitch_steps, "event pitch_steps")
        elif self.resource is not None:
            raise fail(f"{self.kind} events must not carry a resource")
        if self.voice is not None:
            _integer(self.voice, "event voice")

    def as_json(self) -> dict:
        value = asdict(self)
        value["rate"] = list(self.rate)
        return value


def event_from_json(value: Any, name: str = "event") -> AudioEvent:
    _keys(value, ("seq", "tick", "kind", "type_id"),
          ("resource", "variation", "rate", "volume", "pan", "loop", "voice", "pitch_steps"), name)
    rate = value.get("rate", (1, 1))
    if isinstance(rate, list):
        rate = tuple(rate)
    return AudioEvent(seq=value["seq"], tick=value["tick"], kind=value["kind"], type_id=value["type_id"],
                      resource=value.get("resource"), variation=value.get("variation"), rate=rate,
                      volume=value.get("volume", 1.0), pan=value.get("pan", 0), loop=value.get("loop", False),
                      voice=value.get("voice"), pitch_steps=value.get("pitch_steps", 0.0))


def load_events(path: str | Path) -> list[AudioEvent]:
    """Strict JSONL reader: schema, sequence and tick monotonicity are required."""
    events: list[AudioEvent] = []
    previous = -1
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise fail(f"event line {number} is not valid JSON: {error}") from error
        if value.get("schema", EVENT_SCHEMA) != EVENT_SCHEMA:
            raise fail(f"event line {number} has an unknown schema")
        value.pop("schema", None)
        event = event_from_json(value, f"event line {number}")
        if event.seq <= previous:
            raise fail(f"event line {number} does not advance seq")
        previous = event.seq
        if events and event.tick < events[-1].tick:
            raise fail(f"event line {number} moves backwards in tick")
        events.append(event)
    if not events:
        raise fail("event stream is empty")
    return events


def dump_events(events: Sequence[AudioEvent]) -> str:
    lines = []
    for event in events:
        value = {"schema": EVENT_SCHEMA}
        value.update(event.as_json())
        lines.append(json.dumps(value, sort_keys=True, allow_nan=False))
    return "\n".join(lines) + "\n"


def pitch_rate(pitch_steps: float, *, base_frequency: int = DEFAULT_BASE_FREQUENCY) -> tuple[int, int]:
    """Declared pitch mapping: ``DSoundInstance::AdjustPitch`` then integer Hz.

    The original multiplies ``mDefaultFrequency`` by ``1.059463...**steps`` and
    hands ``(DWORD)aNewFrequency`` to ``SetFrequency``. The model keeps that
    integer rounding step, so the rate is a rational number, not a float.
    """
    _integer(base_frequency, "base_frequency", 1)
    steps = _number(pitch_steps, "pitch_steps")
    if steps == 0.0:
        return (1, 1)
    frequency = int(base_frequency * math.pow(PITCH_STEP, steps))
    frequency = min(max(frequency, MIN_FREQUENCY), MAX_FREQUENCY)
    divisor = math.gcd(frequency, base_frequency)
    return (frequency // divisor, base_frequency // divisor)


def frequency_hz(rate: tuple[int, int], *, base_frequency: int = DEFAULT_BASE_FREQUENCY) -> int:
    return rate[0] * base_frequency // rate[1]


# --------------------------------------------------------------------------- #
# the tick-driven mixer
# --------------------------------------------------------------------------- #


@dataclass
class Segment:
    """One continuous audible stretch inside a voice (play or resume to cut).

    Playback parameters live here rather than on the voice so that a mid-flight
    ``SetFrequency`` / ``SetVolume`` / ``SetPan`` is exactly a segment boundary,
    which is what the cursor rules above need to stay a pure function of tick.
    """

    start_tick: int
    offset_samples: int = 0
    rate_num: int = 1
    rate_den: int = 1
    volume: float = 1.0
    pan: int = 0
    end_tick: int | None = None


@dataclass
class Voice:
    """A virtual ``TodDSoundInstance``: one source buffer plus a play cursor."""

    voice_id: int
    type_id: int
    resource: int
    duration_samples: int
    loop: bool = False
    variation: int | None = None
    seq: int = 0
    pitch_steps: float = 0.0
    frequency_hz: int = DEFAULT_BASE_FREQUENCY
    segments: list[Segment] = field(default_factory=list)
    stopped: bool = False
    paused: bool = False
    released: bool = False
    pause_offset: int = 0
    pause_count: int = 0

    @property
    def segment(self) -> Segment | None:
        return self.segments[-1] if self.segments else None

    @property
    def open(self) -> bool:
        segment = self.segment
        return segment is not None and segment.end_tick is None and not self.stopped and not self.paused


class TickMixer:
    """A deterministic virtual device: no hardware, no clock, no hidden state.

    Device writes (``play`` / ``stop`` / ``pause`` / ``resume`` / ``set_*``) and
    device queries (``is_playing`` / ``current_position``) are the whole
    interface, and every query depends only on ``(tick, current state)``.
    """

    def __init__(self, soundscape: Soundscape, *, wiring: str = "observe_only"):
        if wiring not in WIRINGS:
            raise fail(f"unknown wiring {wiring!r}")
        self.soundscape = soundscape
        self.wiring = wiring
        self.samples_per_tick = soundscape.samples_per_tick
        self.voices: list[Voice] = []
        self.log: list[dict] = []
        self.counters = {"is_playing": 0, "current_position": 0, "pause_offset": 0}

    # -- device writes ------------------------------------------------------ #

    def play(self, *, tick: int, type_id: int, resource: int, variation: int | None = None,
             rate: tuple[int, int] = (1, 1), volume: float = 1.0, pan: int = 0, loop: bool = False,
             seq: int = 0, pitch_steps: float = 0.0) -> Voice:
        _integer(tick, "tick")
        _integer(type_id, "type_id")
        sound = self.soundscape.resource(resource)
        entry = self.soundscape.types.get(type_id)
        if entry is not None and entry.loop:
            loop = True
        gain = min(max(sound.base_volume * _number(volume, "volume"), 0.0), 1.0)
        combined_pan = min(max(sound.pan + _integer(pan, "pan", -10000, 10000), -10000), 10000)
        steps = _number(pitch_steps, "pitch_steps")
        rate = (_integer(rate[0], "rate numerator", 1), _integer(rate[1], "rate denominator", 1))
        voice = Voice(voice_id=len(self.voices), type_id=type_id, resource=resource,
                      duration_samples=sound.duration_samples, loop=loop, variation=variation, seq=seq,
                      pitch_steps=steps, frequency_hz=frequency_hz(rate))
        voice.segments.append(Segment(start_tick=tick, offset_samples=0, rate_num=rate[0], rate_den=rate[1],
                                      volume=gain, pan=combined_pan))
        self.voices.append(voice)
        self.log.append({"kind": "play", "tick": tick, "voice": voice.voice_id, "type_id": type_id,
                         "resource": resource, "variation": variation, "loop": loop, "volume": gain,
                         "pan": combined_pan, "rate": [rate[0], rate[1]], "pitch_steps": steps,
                         "frequency_hz": voice.frequency_hz})
        return voice

    def stop(self, *, tick: int, type_id: int | None = None, voice_id: int | None = None) -> Voice | None:
        """``DSoundInstance::Stop``: stop the buffer and reset the cursor to 0.

        The instance stays usable: ``Play`` after a ``Stop`` starts a new
        segment, which is what the pause path and any reuse needs.
        """
        voice = self._first(tick, type_id=type_id, voice_id=voice_id)
        if voice is None:
            return None
        self._cut(voice, tick)
        voice.stopped = True
        voice.paused = False
        self.log.append({"kind": "stop", "tick": tick, "voice": voice.voice_id})
        return voice

    def play_from(self, *, tick: int, voice: Voice, offset_samples: int = 0) -> Voice:
        """``SoundInstance::Play``: (re)start the buffer at ``offset_samples``."""
        _integer(tick, "tick")
        _integer(offset_samples, "offset_samples")
        previous = voice.segment
        self._cut(voice, tick)
        voice.segments.append(Segment(start_tick=tick, offset_samples=offset_samples,
                                      rate_num=previous.rate_num if previous else 1,
                                      rate_den=previous.rate_den if previous else 1,
                                      volume=previous.volume if previous else 1.0,
                                      pan=previous.pan if previous else 0))
        voice.stopped = False
        voice.paused = False
        self.log.append({"kind": "play", "tick": tick, "voice": voice.voice_id,
                         "offset_samples": offset_samples, "kind_detail": "play_from"})
        return voice

    def release(self, *, tick: int, type_id: int | None = None, voice_id: int | None = None) -> Voice | None:
        """``SoundInstance::Release``: stop and retire the instance."""
        voice = self.stop(tick=tick, type_id=type_id, voice_id=voice_id)
        if voice is not None:
            voice.released = True
            self.log.append({"kind": "release", "tick": tick, "voice": voice.voice_id})
        return voice

    def pause(self, *, tick: int, type_id: int | None = None, voice_id: int | None = None) -> list[Voice]:
        """``TodFoley::GamePause(true)``: read ``GetSoundPosition`` then stop."""
        targets = self._all(tick, type_id=type_id, voice_id=voice_id)
        for voice in targets:
            voice.pause_offset = self.current_position(tick, voice)
            self.counters["pause_offset"] += 1
            self._cut(voice, tick)
            voice.stopped = True
            voice.paused = True
            voice.pause_count += 1
            self.log.append({"kind": "pause", "tick": tick, "voice": voice.voice_id,
                             "pause_offset": voice.pause_offset})
        return targets

    def resume(self, *, tick: int, type_id: int | None = None, voice_id: int | None = None) -> list[Voice]:
        """``TodFoley::GamePause(false)``: ``Play`` then ``SetSoundPosition(offset)``."""
        targets = [voice for voice in self.voices
                   if voice.paused and not voice.released
                   and (voice_id is None or voice.voice_id == voice_id)
                   and (type_id is None or voice.type_id == type_id)]
        for voice in targets:
            voice.paused = False
            voice.stopped = False
            previous = voice.segment
            voice.segments.append(Segment(start_tick=tick, offset_samples=voice.pause_offset,
                                          rate_num=previous.rate_num if previous else 1,
                                          rate_den=previous.rate_den if previous else 1,
                                          volume=previous.volume if previous else 1.0,
                                          pan=previous.pan if previous else 0))
            self.log.append({"kind": "resume", "tick": tick, "voice": voice.voice_id,
                             "offset_samples": voice.pause_offset})
        return targets

    def set_rate(self, *, tick: int, voice: Voice, rate: tuple[int, int]) -> None:
        """``SetFrequency`` keeps the cursor: close the segment and reopen it."""
        segment = self._segment_of(voice, tick)
        if segment is None:
            return
        cursor = self._cursor(voice, segment, tick)
        rate = (_integer(rate[0], "rate numerator", 1), _integer(rate[1], "rate denominator", 1))
        self._cut(voice, tick)
        voice.frequency_hz = frequency_hz(rate)
        voice.segments.append(Segment(start_tick=tick, offset_samples=cursor, rate_num=rate[0], rate_den=rate[1],
                                      volume=segment.volume, pan=segment.pan))

    def set_position(self, *, tick: int, voice: Voice, offset_samples: int) -> None:
        """``SetCurrentPosition``: move the cursor without stopping playback."""
        _integer(offset_samples, "offset_samples")
        segment = self._segment_of(voice, tick)
        if segment is None:
            return
        self._cut(voice, tick)
        voice.segments.append(Segment(start_tick=tick, offset_samples=offset_samples,
                                      rate_num=segment.rate_num, rate_den=segment.rate_den,
                                      volume=segment.volume, pan=segment.pan))

    def set_volume(self, *, tick: int, voice: Voice, volume: float) -> None:
        self._reparameterise(voice, tick, volume=min(max(_number(volume, "volume"), 0.0), 1.0))

    def set_pan(self, *, tick: int, voice: Voice, pan: int) -> None:
        self._reparameterise(voice, tick, pan=_integer(pan, "pan", -10000, 10000))

    # -- device queries ----------------------------------------------------- #

    def is_playing(self, tick: int, voice: Voice) -> bool:
        """``IDirectSoundBuffer::GetStatus & DSBSTATUS_PLAYING`` from tick+duration."""
        _integer(tick, "tick")
        self.counters["is_playing"] += 1
        segment = voice.segment
        if segment is None or not voice.open or tick < segment.start_tick:
            return False
        if voice.loop:
            return True
        end = self.natural_end_tick(voice, segment)
        return end is None or tick < end

    def current_position(self, tick: int, voice: Voice) -> int:
        """``IDirectSoundBuffer::GetCurrentPosition``: source samples consumed."""
        _integer(tick, "tick")
        self.counters["current_position"] += 1
        segment = voice.segment
        if segment is None or not voice.open or tick < segment.start_tick:
            return 0
        if not voice.loop:
            end = self.natural_end_tick(voice, segment)
            if end is not None and tick >= end:
                return voice.duration_samples
        cursor = self._cursor(voice, segment, tick)
        if voice.loop:
            return cursor % voice.duration_samples
        return min(cursor, voice.duration_samples)

    def position_bytes(self, tick: int, voice: Voice) -> int:
        """16-bit stereo block align; this is the offset the game stores."""
        return self.current_position(tick, voice) * BLOCK_ALIGN_BYTES

    def natural_end_tick(self, voice: Voice, segment: Segment | None = None) -> int | None:
        """First tick whose first sample is past the end of a non-looping buffer."""
        segment = segment or voice.segment
        if segment is None or voice.loop:
            return None
        remaining = voice.duration_samples - segment.offset_samples
        if remaining <= 0:
            return segment.start_tick
        ticks = -(-(remaining * segment.rate_den) // (segment.rate_num * self.samples_per_tick))
        return segment.start_tick + ticks

    def playing(self, tick: int) -> list[Voice]:
        return [voice for voice in self.voices if self.is_playing(tick, voice)]

    def snapshot(self, tick: int) -> dict:
        """Observable device state; used by tests and by the track manifest."""
        voices = [{"voice_id": voice.voice_id, "type_id": voice.type_id, "resource": voice.resource,
                   "variation": voice.variation, "loop": voice.loop,
                   "playing": self.is_playing(tick, voice), "position": self.current_position(tick, voice),
                   "stopped": voice.stopped, "paused": voice.paused, "released": voice.released,
                   "pause_offset": voice.pause_offset,
                   "gains": [segment.volume for segment in voice.segments]} for voice in self.voices]
        return {"tick": tick, "voices": voices, "counters": dict(self.counters)}

    # -- internals ---------------------------------------------------------- #

    def _cursor(self, voice: Voice, segment: Segment, tick: int) -> int:
        elapsed_samples = (tick - segment.start_tick) * self.samples_per_tick
        return segment.offset_samples + (elapsed_samples * segment.rate_num) // segment.rate_den

    def _cut(self, voice: Voice, tick: int) -> None:
        segment = voice.segment
        if segment is not None and segment.end_tick is None and tick >= segment.start_tick:
            segment.end_tick = tick

    def _segment_of(self, voice: Voice, tick: int) -> Segment | None:
        segment = voice.segment
        if segment is None or segment.end_tick is not None or voice.stopped or voice.paused:
            return None
        if tick < segment.start_tick:
            raise fail(f"tick {tick} precedes voice {voice.voice_id}'s current segment")
        return segment

    def _reparameterise(self, voice: Voice, tick: int, *, volume: float | None = None,
                        pan: int | None = None) -> None:
        segment = self._segment_of(voice, tick)
        if segment is None:
            return
        cursor = self._cursor(voice, segment, tick)
        self._cut(voice, tick)
        voice.segments.append(Segment(start_tick=tick, offset_samples=cursor, rate_num=segment.rate_num,
                                      rate_den=segment.rate_den,
                                      volume=segment.volume if volume is None else volume,
                                      pan=segment.pan if pan is None else pan))

    def _first(self, tick: int, *, type_id: int | None, voice_id: int | None) -> Voice | None:
        for voice in self._all(tick, type_id=type_id, voice_id=voice_id):
            return voice
        return None

    def _all(self, tick: int, *, type_id: int | None, voice_id: int | None) -> list[Voice]:
        matched = []
        for voice in self.voices:
            if voice_id is not None and voice.voice_id != voice_id:
                continue
            if type_id is not None and voice.type_id != type_id:
                continue
            segment = voice.segment
            if segment is None or segment.start_tick > tick or not voice.open:
                continue
            matched.append(voice)
        return matched


# --------------------------------------------------------------------------- #
# Foley admission machine (the simulation side of the model)
# --------------------------------------------------------------------------- #


@dataclass
class Slot:
    """Declared mirror of ``FoleyInstance`` (instance, refcount, paused, start, offset)."""

    refcount: int = 0
    paused: bool = False
    start_count: int = 0
    instance: Voice | None = None
    pause_offset: int = 0

    def signature(self) -> tuple:
        return (self.refcount, self.paused, self.start_count, self.pause_offset)


@dataclass(frozen=True)
class PlayIntent:
    seq: int
    tick: int
    type_id: int
    variation: int
    resource: int
    pitch: float
    loop: bool
    rate: tuple[int, int]
    volume: float = 1.0
    pan: int = 0


class ModelRandom:
    """Declared stand-in for ``Sexy::MTRand``; only draw counts matter here."""

    def __init__(self, seed: int = 1):
        self.state = _integer(seed, "seed") & 0xFFFFFFFF
        self.draws = 0

    def rand(self, count: int) -> int:
        _integer(count, "rand count", 1)
        self.draws += 1
        self.state = (1103515245 * self.state + 12345) & 0xFFFFFFFF
        return self.state % count


class AllocationNoneDevice:
    """``sound_effects_allocation_none_v1``: every allocation fails with null."""

    name = "allocation_none"

    def __init__(self):
        self.counters = {"allocate": 0}

    def allocate(self, tick: int, intent: PlayIntent) -> Voice | None:
        self.counters["allocate"] += 1
        return None

    def is_playing(self, instance: Voice, tick: int) -> bool:
        raise fail("allocation-none device has no instances to query")

    def position(self, instance: Voice, tick: int) -> int:
        raise fail("allocation-none device has no instances to query")

    def release(self, instance: Voice, tick: int) -> None:
        raise fail("allocation-none device has no instances to release")

    def play(self, instance: Voice, looping: bool, tick: int) -> None:
        raise fail("allocation-none device has no instances to play")

    def stop(self, instance: Voice, tick: int) -> None:
        raise fail("allocation-none device has no instances to stop")

    def set_position(self, instance: Voice, tick: int, offset: int) -> None:
        raise fail("allocation-none device has no instances to reposition")

    def adjust_pitch(self, instance: Voice, steps: float, tick: int) -> None:
        raise fail("allocation-none device has no instances to pitch")

    def set_volume(self, instance: Voice, volume: float, tick: int) -> None:
        raise fail("allocation-none device has no instances to retune")


class TickVirtualDevice:
    """Wiring ``device_feedback``: the game sees live tick-model voices.

    This is the original-like wiring and the reason N9 declares a different one:
    the answers below enter ``SoundSystemReleaseFinishedInstances`` and
    ``SoundSystemHasFoleyPlayedTooRecently``, which is exactly the 036 / #12
    mechanism. It is modelled here so the model can demonstrate the divergence;
    the mode implemented by this issue does not enable it.
    """

    name = "tick_virtual_feedback"

    def __init__(self, mixer: TickMixer):
        self.mixer = mixer

    def allocate(self, tick: int, intent: PlayIntent) -> Voice | None:
        """``DSoundManager::GetSoundInstance``: a full channel table returns NULL.

        ``DSoundManager::Update`` frees a channel once its instance is released,
        so "released" is the only condition that returns a channel here.
        """
        if len([voice for voice in self.mixer.voices if not voice.released]) >= MAX_MANAGER_CHANNELS:
            return None
        return self.mixer.play(tick=tick, type_id=intent.type_id, resource=intent.resource,
                               variation=intent.variation, rate=intent.rate, volume=intent.volume,
                               pan=intent.pan, loop=intent.loop, seq=intent.seq, pitch_steps=intent.pitch)

    def is_playing(self, instance: Voice, tick: int) -> bool:
        return self.mixer.is_playing(tick, instance)

    def position(self, instance: Voice, tick: int) -> int:
        return self.mixer.current_position(tick, instance)

    def release(self, instance: Voice, tick: int) -> None:
        self.mixer.release(tick=tick, voice_id=instance.voice_id)

    def play(self, instance: Voice, looping: bool, tick: int) -> None:
        self.mixer.play_from(tick=tick, voice=instance)

    def stop(self, instance: Voice, tick: int) -> None:
        self.mixer.stop(tick=tick, voice_id=instance.voice_id)

    def set_position(self, instance: Voice, tick: int, offset: int) -> None:
        self.mixer.set_position(tick=tick, voice=instance, offset_samples=offset)

    def adjust_pitch(self, instance: Voice, steps: float, tick: int) -> None:
        self.mixer.set_rate(tick=tick, voice=instance, rate=pitch_rate(steps))

    def set_volume(self, instance: Voice, volume: float, tick: int) -> None:
        self.mixer.set_volume(tick=tick, voice=instance, volume=volume)


class MixerObserver:
    """Wiring ``observe_only``: mirror play intents into the mixer, read nothing back.

    The machine never receives a value from this object, so no mixer answer can
    reach a branch or an RNG draw. ``admission`` selects the *audible* policy:
    ``all`` renders every admitted intent, ``original`` re-derives the original
    gates (too recently played, one-at-a-time reuse, eight live slots) from
    tick-based ``is_playing`` answers.
    """

    def __init__(self, mixer: TickMixer, *, admission: str = "original"):
        if admission not in ADMISSION_POLICIES:
            raise fail(f"unknown admission policy {admission!r}")
        self.mixer = mixer
        self.admission = admission
        self.events: list[AudioEvent] = []
        self.dropped: list[dict] = []

    def _gate(self, tick: int, type_id: int) -> str | None:
        if self.admission == "all":
            return None
        entry = self.mixer.soundscape.types.get(type_id)
        playing = [voice for voice in self.mixer.voices
                   if voice.type_id == type_id and self.mixer.is_playing(tick, voice)]
        if not playing:
            return None
        if entry is not None and entry.loop:
            return None
        if max(voice.segment.start_tick for voice in playing if voice.segment) > tick - TOO_RECENT_UPDATES:
            return "recent"
        if entry is not None and entry.one_at_a_time:
            return "reuse"
        if len(playing) >= MAX_FOLEY_INSTANCES:
            return "no_slot"
        return None

    def intent(self, intent: PlayIntent) -> Voice | None:
        reason = self._gate(intent.tick, intent.type_id)
        if reason is not None:
            self.dropped.append({"seq": intent.seq, "tick": intent.tick, "type_id": intent.type_id,
                                 "reason": reason})
            return None
        event = AudioEvent(seq=intent.seq, tick=intent.tick, kind="play", type_id=intent.type_id,
                           resource=intent.resource, variation=intent.variation, rate=intent.rate,
                           volume=intent.volume, pan=intent.pan, loop=intent.loop, pitch_steps=intent.pitch)
        self.events.append(event)
        return self.mixer.play(tick=intent.tick, type_id=intent.type_id, resource=intent.resource,
                               variation=intent.variation, rate=intent.rate, volume=intent.volume,
                               pan=intent.pan, loop=intent.loop, seq=intent.seq, pitch_steps=intent.pitch)

    def stop(self, tick: int, type_id: int) -> None:
        self.mixer.stop(tick=tick, type_id=type_id)
        self.events.append(AudioEvent(seq=len(self.events), tick=tick, kind="stop", type_id=type_id))

    def game_pause(self, tick: int, entering: bool) -> None:
        """Mirror ``GamePause``: only ``mute_on_pause`` types leave the device."""
        muted = [type_id for type_id, entry in self.mixer.soundscape.types.items()
                 if "mute_on_pause" in entry.flags]
        for type_id in muted:
            if entering:
                self.mixer.pause(tick=tick, type_id=type_id)
            else:
                self.mixer.resume(tick=tick, type_id=type_id)
        self.events.append(AudioEvent(seq=len(self.events), tick=tick,
                                      kind="pause" if entering else "resume", type_id=0))


class FoleyMachine:
    """The original Foley bookkeeping, with the device behind an interface.

    Only the parts that can feed back into the simulation are modelled: release
    of finished instances, the ten-update "played too recently" gate, the
    one-at-a-time reuse path, the eight-instance-per-type limit, the variation
    draw, ``StopFoley`` and ``GamePause``.
    """

    def __init__(self, soundscape: Soundscape, *, device: Any, seed: int = 1,
                 observer: MixerObserver | None = None,
                 app_clock: Callable[[int], int] | None = None):
        self.soundscape = soundscape
        self.device = device
        self.observer = observer
        self.rng = ModelRandom(seed)
        self.app_clock = app_clock or (lambda tick: tick)
        self.slots: dict[int, list[Slot]] = {type_id: [Slot() for _ in range(MAX_FOLEY_INSTANCES)]
                                             for type_id in soundscape.types}
        self.last_variation = {type_id: -1 for type_id in soundscape.types}
        self.outcomes: list[dict] = []

    # -- original helpers --------------------------------------------------- #

    def release_finished(self, tick: int) -> None:
        for slots in self.slots.values():
            for slot in slots:
                if slot.refcount == 0 or slot.paused:
                    continue
                assert slot.instance is not None
                if not self.device.is_playing(slot.instance, tick):
                    self.device.release(slot.instance, tick)
                    slot.instance = None
                    slot.refcount = 0

    def played_too_recently(self, tick: int, type_id: int) -> bool:
        now = self.app_clock(tick)
        return any(slot.refcount != 0 and now - slot.start_count < TOO_RECENT_UPDATES
                   for slot in self.slots[type_id])

    def find_instance(self, type_id: int) -> Slot | None:
        for slot in self.slots[type_id]:
            if slot.refcount > 0:
                return slot
        return None

    def free_slot(self, type_id: int) -> Slot | None:
        for slot in self.slots[type_id]:
            if slot.refcount == 0:
                return slot
        return None

    def is_foley_playing(self, tick: int, type_id: int) -> bool:
        """``TodFoley::IsFoleyPlaying``: the reader the store dialogue uses."""
        self.release_finished(tick)
        return self.find_instance(type_id) is not None

    # -- original entry points --------------------------------------------- #

    def play(self, tick: int, type_id: int, pitch: float = 0.0) -> str:
        entry = self.soundscape.type(type_id)
        self.release_finished(tick)
        if self.played_too_recently(tick, type_id) and not entry.loop:
            return self._outcome(tick, type_id, "recent")
        if entry.one_at_a_time:
            slot = self.find_instance(type_id)
            if slot is not None:
                slot.refcount += 1
                slot.start_count = self.app_clock(tick)
                return self._outcome(tick, type_id, "reuse")
        slot = self.free_slot(type_id)
        if slot is None:
            return self._outcome(tick, type_id, "no_slot")
        candidates = []
        for index, resource in enumerate(entry.resources):
            if resource is None:
                break
            if "dont_repeat" in entry.flags and self.last_variation[type_id] == index:
                continue
            candidates.append(index)
        if not candidates:
            raise fail(f"foley type {type_id} has no admissible variation")
        variation = candidates[self.rng.rand(len(candidates))]
        self.last_variation[type_id] = variation
        intent = PlayIntent(seq=len(self.outcomes), tick=tick, type_id=type_id, variation=variation,
                            resource=entry.resources[variation], pitch=pitch, loop=entry.loop,
                            rate=pitch_rate(pitch))
        if self.observer is not None:
            self.observer.intent(intent)
        instance = self.device.allocate(tick, intent)
        if instance is None:
            return self._outcome(tick, type_id, "no_instance", variation=variation)
        slot.instance = instance
        slot.refcount = 1
        slot.start_count = self.app_clock(tick)
        slot.paused = False
        slot.pause_offset = 0
        if pitch != 0.0:
            self.device.adjust_pitch(instance, pitch, tick)
        if "uses_music_volume" in entry.flags:
            self.device.set_volume(instance, 1.0, tick)
        self.device.play(instance, entry.loop, tick)
        return self._outcome(tick, type_id, "played", variation=variation)

    def stop(self, tick: int, type_id: int) -> None:
        """``TodFoley::StopFoley``: drop one reference, release at zero."""
        self.release_finished(tick)
        if self.observer is not None:
            self.observer.stop(tick, type_id)
        slot = self.find_instance(type_id)
        if slot is None:
            return
        slot.refcount -= 1
        if slot.refcount == 0:
            assert slot.instance is not None
            self.device.release(slot.instance, tick)
            slot.instance = None
        self.outcomes.append({"tick": tick, "type_id": type_id, "outcome": "stop", "variation": None})

    def game_pause(self, tick: int, entering: bool) -> None:
        """``TodFoley::GamePause``, which only touches ``mute_on_pause`` types."""
        self.release_finished(tick)
        if self.observer is not None:
            self.observer.game_pause(tick, entering)
        for type_id, slots in self.slots.items():
            entry = self.soundscape.types[type_id]
            if "mute_on_pause" not in entry.flags:
                continue
            for slot in slots:
                if slot.refcount == 0:
                    continue
                assert slot.instance is not None
                if entering:
                    slot.paused = True
                    slot.pause_offset = self.device.position(slot.instance, tick)
                    self.device.stop(slot.instance, tick)
                elif slot.paused:
                    slot.paused = False
                    self.device.play(slot.instance, entry.loop, tick)
                    self.device.set_position(slot.instance, tick, slot.pause_offset)
        self.outcomes.append({"tick": tick, "type_id": None, "outcome": "pause" if entering else "unpause",
                              "variation": None})

    def cancel_paused(self, tick: int) -> None:
        self.release_finished(tick)
        for slots in self.slots.values():
            for slot in slots:
                if slot.refcount != 0 and slot.paused:
                    assert slot.instance is not None
                    self.device.release(slot.instance, tick)
                    slot.instance = None
                    slot.refcount = 0

    def snapshot(self, tick: int) -> dict:
        """Non-mutating frame: slot bookkeeping, RNG cursor and the reader view."""
        return {"tick": tick, "app_update_count": self.app_clock(tick), "rng_draws": self.rng.draws,
                "slots": {type_id: [slot.signature() for slot in slots]
                          for type_id, slots in sorted(self.slots.items())},
                "last_variation": dict(sorted(self.last_variation.items())),
                "playing": {type_id: any(slot.refcount != 0 for slot in slots)
                            for type_id, slots in sorted(self.slots.items())}}

    def _outcome(self, tick: int, type_id: int, outcome: str, *, variation: int | None = None) -> str:
        self.outcomes.append({"tick": tick, "type_id": type_id, "outcome": outcome, "variation": variation})
        return outcome


@dataclass(frozen=True)
class FoleyTrace:
    frames: tuple[dict, ...]
    events: tuple[dict, ...]
    intents: tuple[dict, ...]
    queries: tuple[dict, ...]
    counters: dict

    def as_dict(self) -> dict:
        return {"schema": TRACE_SCHEMA, "frames": [dict(frame) for frame in self.frames],
                "events": [dict(event) for event in self.events],
                "intents": [dict(intent) for intent in self.intents],
                "queries": [dict(query) for query in self.queries], "counters": dict(self.counters)}


def run_stream(soundscape: Soundscape, actions: Sequence[tuple], *, ticks: int, device: Any,
               observer: MixerObserver | None = None, seed: int = 1,
               app_clock: Callable[[int], int] | None = None) -> FoleyTrace:
    """Run one synthetic action sequence and return every frame boundary.

    Actions are ``("play", tick, type_id, pitch)``, ``("stop", tick, type_id)``,
    ``("pause", tick, True|False)``, ``("is_playing", tick, type_id)`` or
    ``("cancel_paused", tick)``. The frame at tick ``t`` is recorded after every
    action scheduled at ``t``, and only ``(tick, state)`` decides the frame.
    """
    _integer(ticks, "ticks", 1)
    schedule: dict[int, list[tuple]] = {}
    for action in actions:
        if not action:
            raise fail("an action must not be empty")
        tick = action[1]
        _integer(tick, "action tick")
        if tick >= ticks:
            raise fail(f"action tick {tick} is outside 0..{ticks - 1}")
        schedule.setdefault(tick, []).append(action)
    machine = FoleyMachine(soundscape, device=device, seed=seed, observer=observer, app_clock=app_clock)
    frames = []
    queries: list[dict] = []
    for tick in range(ticks):
        for action in schedule.get(tick, []):
            kind = action[0]
            if kind == "play":
                machine.play(tick, action[2], action[3] if len(action) > 3 else 0.0)
            elif kind == "stop":
                machine.stop(tick, action[2])
            elif kind == "pause":
                machine.game_pause(tick, bool(action[2]))
            elif kind == "is_playing":
                queries.append({"tick": tick, "type_id": action[2],
                                "result": machine.is_foley_playing(tick, action[2])})
            elif kind == "cancel_paused":
                machine.cancel_paused(tick)
            else:
                raise fail(f"unknown action {kind!r}")
        frames.append(machine.snapshot(tick))
    counters = {"rng_draws": machine.rng.draws, "outcomes": len(machine.outcomes)}
    if observer is not None:
        counters["mixer_queries"] = dict(observer.mixer.counters)
        counters["voices"] = len(observer.mixer.voices)
        counters["dropped"] = len(observer.dropped)
    device_counters = getattr(device, "counters", None)
    if device_counters is not None:
        counters["device_counters"] = dict(device_counters)
    return FoleyTrace(frames=tuple(frames), events=tuple(machine.outcomes),
                      intents=tuple(intent.as_json() for intent in (observer.events if observer else [])),
                      queries=tuple(queries), counters=counters)


def compare_traces(left: FoleyTrace, right: FoleyTrace) -> dict:
    """Frame-by-frame comparison of two runs of the same action sequence."""
    for index, (first, second) in enumerate(zip(left.frames, right.frames)):
        if first != second:
            key = next(key for key in sorted(set(first) | set(second)) if first.get(key) != second.get(key))
            return {"equal": False, "frames": min(len(left.frames), len(right.frames)),
                    "first_difference": {"index": index, "tick": first["tick"], "key": key,
                                         "left": first.get(key), "right": second.get(key)}}
    if len(left.frames) != len(right.frames):
        return {"equal": False, "frames": min(len(left.frames), len(right.frames)),
                "first_difference": {"index": min(len(left.frames), len(right.frames)), "tick": None,
                                     "key": "frame_count", "left": len(left.frames),
                                     "right": len(right.frames)}}
    if left.events != right.events:
        for index, (first, second) in enumerate(zip(left.events, right.events)):
            if first != second:
                return {"equal": False, "frames": len(left.frames),
                        "first_difference": {"index": index, "tick": first["tick"], "key": "outcome",
                                             "left": first, "right": second}}
        return {"equal": False, "frames": len(left.frames),
                "first_difference": {"index": min(len(left.events), len(right.events)), "tick": None,
                                     "key": "event_count", "left": len(left.events),
                                     "right": len(right.events)}}
    if left.queries != right.queries:
        for index, (first, second) in enumerate(zip(left.queries, right.queries)):
            if first != second:
                return {"equal": False, "frames": len(left.frames),
                        "first_difference": {"index": index, "tick": first["tick"], "key": "query_result",
                                             "left": first, "right": second}}
        return {"equal": False, "frames": len(left.frames),
                "first_difference": {"index": min(len(left.queries), len(right.queries)), "tick": None,
                                     "key": "query_count", "left": len(left.queries),
                                     "right": len(right.queries)}}
    return {"equal": True, "frames": len(left.frames), "first_difference": None}


# --------------------------------------------------------------------------- #
# offline soundtrack
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Track:
    pcm: bytes
    frames: int
    channels: int
    sample_rate: int
    start_tick: int
    end_tick: int
    samples_per_tick: int
    manifest: dict

    @property
    def seconds(self) -> float:
        return self.frames / self.sample_rate


def _source_pcm(sound: SoundResource, *, sample_rate: int, cache: dict[int, array]) -> array:
    """Interleaved stereo ``array('h')`` for one resource, decoded once."""
    if sound.id in cache:
        return cache[sound.id]
    if sound.source_kind == "tone":
        samples = array("h")
        step = 2.0 * math.pi * sound.frequency_hz / sample_rate
        for index in range(sound.duration_samples):
            value = int(round(TONE_AMPLITUDE * 32767 * math.sin(step * index)))
            samples.append(value)
            samples.append(value)
    else:
        _frames, channels = wav_frames(sound.path)
        with wave.open(str(sound.path), "rb") as handle:
            if handle.getframerate() != sample_rate:
                raise fail(f"sound {sound.id} must be {sample_rate} Hz; convert {sound.path} first")
            raw = handle.readframes(handle.getnframes())
        source = array("h")
        source.frombytes(raw)
        if sys.byteorder != "little":
            source.byteswap()
        if channels == 1:
            samples = array("h")
            for value in source:
                samples.append(value)
                samples.append(value)
        else:
            samples = source
    cache[sound.id] = samples
    return samples


def pan_gains(pan: int) -> tuple[float, float]:
    """Declared linear pan law; not the original hundredths-of-dB law."""
    position = pan / 10000.0
    left = 1.0 if position <= 0 else 1.0 - position
    right = 1.0 if position >= 0 else 1.0 + position
    return left, right


def voice_end_tick(mixer: TickMixer, voice: Voice, *, limit: int | None = None) -> int | None:
    """Last tick the voice can still be heard at (exclusive).

    An open looping voice has no natural end; ``limit`` (the track end) then
    decides how far it is rendered, and ``None`` means "not decidable yet".
    """
    end = voice.segments[0].start_tick if voice.segments else 0
    undecided = False
    for segment in voice.segments:
        natural = mixer.natural_end_tick(voice, segment)
        stops = [tick for tick in (segment.end_tick, natural) if tick is not None]
        if not stops:
            undecided = True
            continue
        end = max(end, min(stops))
    if undecided:
        return limit
    return end


def render_track(events: Sequence[AudioEvent], soundscape: Soundscape, *, start_tick: int = 0,
                 end_tick: int | None = None, admission: str = "original",
                 wiring: str = "observe_only") -> Track:
    """Render the tick-aligned PCM track for one admitted event stream.

    ``admission='all'`` renders every play event. ``admission='original'``
    re-derives the original audible gates (too recently played, one-at-a-time
    reuse, eight live slots per type) from tick-based ``is_playing`` answers.
    Neither policy changes the event stream itself.
    """
    if admission not in ADMISSION_POLICIES:
        raise fail(f"unknown admission policy {admission!r}")
    _integer(start_tick, "start_tick")
    if end_tick is not None:
        _integer(end_tick, "end_tick", 1)
        if end_tick <= start_tick:
            raise fail("end_tick must be after start_tick")
        if end_tick - start_tick > MAX_TRACK_TICKS:
            raise fail(f"track longer than {MAX_TRACK_TICKS} ticks")
    mixer = TickMixer(soundscape, wiring=wiring)
    observer = MixerObserver(mixer, admission=admission)
    voices: list[Voice] = []
    counts = {kind: 0 for kind in EVENT_KINDS}
    for event in events:
        if event.tick < start_tick:
            raise fail(f"event {event.seq} precedes the track start {start_tick}")
        counts[event.kind] += 1
        if event.kind == "play":
            intent = PlayIntent(seq=event.seq, tick=event.tick, type_id=event.type_id,
                                variation=event.variation if event.variation is not None else 0,
                                resource=event.resource, pitch=event.pitch_steps, loop=event.loop,
                                rate=event.rate, volume=event.volume, pan=event.pan)
            voice = observer.intent(intent)
            if voice is not None:
                voices.append(voice)
        elif event.kind == "stop":
            if event.voice is not None:
                mixer.stop(tick=event.tick, voice_id=event.voice)
            else:
                mixer.stop(tick=event.tick, type_id=event.type_id)
        elif event.kind == "pause":
            mixer.pause(tick=event.tick)
        elif event.kind == "resume":
            mixer.resume(tick=event.tick)
    if not voices:
        raise fail("no play event produced an audible voice")
    samples_per_tick = soundscape.samples_per_tick
    last_event = max(event.tick for event in events)
    if end_tick is None:
        ends = [voice_end_tick(mixer, voice) for voice in voices]
        if any(end is None for end in ends):
            raise fail("a looping voice is still open; pass an explicit end_tick")
        end_tick = max(max(ends), last_event + 1)
    if end_tick - start_tick > MAX_TRACK_TICKS:
        raise fail(f"track longer than {MAX_TRACK_TICKS} ticks")
    total_frames = (end_tick - start_tick) * samples_per_tick

    left = array("i", bytes(4 * total_frames))
    right = array("i", bytes(4 * total_frames))
    cache: dict[int, array] = {}
    rendered = []
    intervals = []
    for voice in voices:
        source = _source_pcm(soundscape.resource(voice.resource), sample_rate=soundscape.sample_rate, cache=cache)
        frames = 0
        first_frame = None
        last_frame = None
        segments = []
        for segment in voice.segments:
            natural = mixer.natural_end_tick(voice, segment)
            stop = min(tick for tick in (segment.end_tick, natural, end_tick) if tick is not None)
            if stop < segment.start_tick:
                continue
            base = (segment.start_tick - start_tick) * samples_per_tick
            gains = pan_gains(segment.pan)
            written = 0
            for index in range((stop - segment.start_tick) * samples_per_tick):
                position = segment.offset_samples + (index * segment.rate_num) // segment.rate_den
                if voice.loop:
                    position %= voice.duration_samples
                elif position >= voice.duration_samples:
                    break
                left[base + written] += int(source[2 * position] * segment.volume * gains[0])
                right[base + written] += int(source[2 * position + 1] * segment.volume * gains[1])
                written += 1
            segments.append({"start_tick": segment.start_tick, "end_tick": stop,
                             "offset_samples": segment.offset_samples, "frames": written,
                             "start_frame": base if written else None,
                             "rate": [segment.rate_num, segment.rate_den], "volume": segment.volume,
                             "pan": segment.pan})
            frames += written
            if written:
                first_frame = base if first_frame is None else min(first_frame, base)
                last_frame = base + written if last_frame is None else max(last_frame, base + written)
                intervals.append((base, base + written))
        rendered.append({"voice_id": voice.voice_id, "seq": voice.seq, "type_id": voice.type_id,
                         "resource": voice.resource, "variation": voice.variation, "loop": voice.loop,
                         "pitch_steps": voice.pitch_steps, "frequency_hz": voice.frequency_hz,
                         "duration_samples": voice.duration_samples,
                         "start_tick": voice.segments[0].start_tick if voice.segments else None,
                         "end_tick": voice_end_tick(mixer, voice, limit=end_tick), "frames": frames,
                         "start_frame": first_frame, "end_frame": last_frame, "segments": segments})
    pcm = array("h")
    clipped = 0
    for index in range(total_frames):
        for accumulator in (left, right):
            value = accumulator[index]
            clamped = -32768 if value < -32768 else 32767 if value > 32767 else value
            if clamped != value:
                clipped += 1
            pcm.append(clamped)
    if sys.byteorder != "little":
        pcm.byteswap()
    payload = pcm.tobytes()
    union = union_frames(intervals)
    manifest = {
        "schema": TRACK_SCHEMA, "mode": MODE, "wiring": wiring, "admission": admission,
        "sample_rate": soundscape.sample_rate, "ticks_per_second": soundscape.ticks_per_second,
        "samples_per_tick": samples_per_tick, "channels": 2, "sample_format": "s16le",
        "start_tick": start_tick, "end_tick": end_tick, "total_frames": total_frames,
        "duration_seconds": f"{total_frames / soundscape.sample_rate:.6f}",
        "events": counts, "voices": rendered, "dropped": list(observer.dropped),
        "coverage": {"sum_voice_frames": sum(voice["frames"] for voice in rendered), "union_frames": union,
                     "silent_frames": total_frames - union, "clipped_values": clipped,
                     "first_frame": min((voice["start_frame"] for voice in rendered
                                         if voice["start_frame"] is not None), default=None),
                     "last_frame": max((voice["end_frame"] for voice in rendered
                                        if voice["end_frame"] is not None), default=None)},
        "mixer_counters": dict(mixer.counters), "pcm_sha256": hashlib.sha256(payload).hexdigest()}
    return Track(pcm=payload, frames=total_frames, channels=2, sample_rate=soundscape.sample_rate,
                 start_tick=start_tick, end_tick=end_tick, samples_per_tick=samples_per_tick,
                 manifest=manifest)


def union_frames(intervals: Sequence[tuple[int, int]]) -> int:
    """Number of distinct frames covered by half-open intervals."""
    total = 0
    current_end: int | None = None
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if current_end is None or start > current_end:
            total += end - start
            current_end = end
        elif end > current_end:
            total += end - current_end
            current_end = end
    return total


def wav_bytes(track: Track) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(track.channels)
        handle.setsampwidth(2)
        handle.setframerate(track.sample_rate)
        handle.writeframes(track.pcm)
    return buffer.getvalue()


def write_wav(path: str | Path, track: Track) -> None:
    Path(path).write_bytes(wav_bytes(track))


def verify_track(wav_path: str | Path, manifest: dict) -> dict:
    """Re-check duration and sample coverage of a rendered track."""
    failures: list[str] = []
    if manifest.get("schema") != TRACK_SCHEMA:
        failures.append("manifest schema mismatch")
    try:
        with wave.open(str(wav_path), "rb") as handle:
            channels, width, rate, frames = (handle.getnchannels(), handle.getsampwidth(),
                                             handle.getframerate(), handle.getnframes())
            payload = handle.readframes(frames)
    except (OSError, wave.Error) as error:
        return {"ok": False, "failures": [f"cannot read track: {error}"], "checks": {}}
    if channels != manifest.get("channels"):
        failures.append("channel count differs from the manifest")
    if width != 2:
        failures.append("track is not 16-bit PCM")
    if rate != manifest.get("sample_rate"):
        failures.append("sample rate differs from the manifest")
    if frames != manifest.get("total_frames"):
        failures.append("frame count differs from the manifest")
    samples_per_tick = manifest.get("samples_per_tick")
    start_tick, end_tick = manifest.get("start_tick"), manifest.get("end_tick")
    if not all(type(value) is int for value in (samples_per_tick, start_tick, end_tick)):
        failures.append("manifest tick grid is incomplete")
        return {"ok": False, "failures": failures, "checks": {"frames": frames}}
    expected = (end_tick - start_tick) * samples_per_tick
    if expected != frames:
        failures.append(f"tick grid implies {expected} frames but the track has {frames}")
    intervals = []
    for voice in manifest.get("voices", []):
        parts = voice.get("segments", [])
        segment_frames = sum(part.get("frames", 0) for part in parts)
        if voice.get("frames") != segment_frames:
            failures.append(f"voice {voice['voice_id']} frame count differs from its segments")
        if voice.get("start_frame") is None:
            continue
        expected_start = tick_to_frame(voice["start_tick"], start_tick=start_tick,
                                       samples_per_tick=samples_per_tick)
        if voice["start_frame"] != expected_start:
            failures.append(f"voice {voice['voice_id']} does not start on its tick boundary")
        if voice["end_frame"] > frames:
            failures.append(f"voice {voice['voice_id']} exceeds the track")
        written: list[tuple[int, int]] = []
        for part in parts:
            if part["end_tick"] < part["start_tick"]:
                failures.append(f"voice {voice['voice_id']} has an inverted segment")
            span = (part["end_tick"] - part["start_tick"]) * samples_per_tick
            if part["frames"] > span:
                failures.append(f"voice {voice['voice_id']} writes more frames than one segment spans")
            if not part["frames"]:
                continue
            part_start = tick_to_frame(part["start_tick"], start_tick=start_tick,
                                       samples_per_tick=samples_per_tick)
            if part.get("start_frame") not in (None, part_start):
                failures.append(f"voice {voice['voice_id']} segment is not on its tick boundary")
            written.append((part_start, part_start + part["frames"]))
        if written:
            if voice["start_frame"] != min(start for start, _end in written):
                failures.append(f"voice {voice['voice_id']} start frame differs from its segments")
            if voice["end_frame"] != max(end for _start, end in written):
                failures.append(f"voice {voice['voice_id']} end frame differs from its segments")
            intervals.extend(written)
    union = union_frames(intervals)
    coverage = manifest.get("coverage", {})
    if coverage.get("union_frames") != union:
        failures.append("union coverage differs from the manifest")
    if coverage.get("silent_frames") != frames - union:
        failures.append("silence accounting differs from the manifest")
    if coverage.get("sum_voice_frames") != sum(voice.get("frames", 0) for voice in manifest.get("voices", [])):
        failures.append("summed voice frames differ from the manifest")
    counts = manifest.get("events", {})
    if counts.get("play") != len(manifest.get("voices", [])) + len(manifest.get("dropped", [])):
        failures.append("play events are not all accounted for")
    if manifest.get("pcm_sha256") != hashlib.sha256(payload).hexdigest():
        failures.append("track payload differs from the manifest hash")
    checks = {"frames": frames, "union_frames": union, "silent_frames": frames - union,
              "voices": len(manifest.get("voices", [])), "dropped": len(manifest.get("dropped", []))}
    return {"ok": not failures, "failures": failures, "checks": checks}


# --------------------------------------------------------------------------- #
# adapter: real ``lvz.foley-event.v1`` rows to audio events
# --------------------------------------------------------------------------- #


def events_from_foley_trace(rows: Iterable[dict], soundscape: Soundscape) -> list[AudioEvent]:
    """Derive play events from a ``foley_branch_trace_v1`` JSONL stream.

    One event is emitted per invocation that drew a variation and named a
    resource. Allocation success is deliberately *not* required: allocation-none
    fails every allocation after the draw, and the point of the offline track is
    to render what the game asked for. ``StopFoley`` / ``GamePause`` have no
    hook site in that diagnostic today, so a stream that used them needs the
    extra hook sites listed in the design document.
    """
    pending: dict[Any, dict] = {}
    derived: list[tuple[int, int, AudioEvent]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise fail("foley trace rows must be objects")
        kind = row.get("kind")
        invocation = row.get("invocation_id")
        if kind == "foley_enter":
            pending[invocation] = {"enter": row}
        elif kind == "variation_return":
            pending.setdefault(invocation, {})["variation"] = row
        elif kind == "recent_return":
            pending.pop(invocation, None)
        elif kind in ("foley_return_full", "foley_return_reuse", "foley_return_common"):
            item = pending.pop(invocation, None)
            if item is None or "variation" not in item or "enter" not in item:
                continue
            enter, variation = item["enter"], item["variation"]
            index = variation.get("selected_variation")
            if index is None:
                index = variation.get("returned_index")
            resources = enter.get("resource_ids") or []
            if not isinstance(index, int) or not 0 <= index < len(resources) or resources[index] is None:
                continue
            version = enter.get("version") or {}
            if type(version.get("tick")) is not int:
                raise fail("foley trace row lacks a tick")
            bits = (enter.get("pitch_bits") or 0) & 0xFFFFFFFF
            pitch = struct.unpack("<f", struct.pack("<I", bits))[0]
            entry = soundscape.types.get(enter.get("foley_type"))
            event = AudioEvent(seq=0, tick=version["tick"], kind="play", type_id=enter["foley_type"],
                               resource=resources[index], variation=index, rate=pitch_rate(pitch),
                               volume=1.0, pan=0, loop=bool(entry.loop) if entry else False,
                               pitch_steps=pitch)
            derived.append((event.tick, enter.get("ordinal", 0), event))
    derived.sort(key=lambda item: (item[0], item[1]))
    return [AudioEvent(seq=seq, tick=event.tick, kind=event.kind, type_id=event.type_id,
                       resource=event.resource, variation=event.variation, rate=event.rate,
                       volume=event.volume, pan=event.pan, loop=event.loop, pitch_steps=event.pitch_steps)
            for seq, (_tick, _ordinal, event) in enumerate(derived)]
