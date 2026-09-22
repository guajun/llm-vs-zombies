"""Tick-driven virtual audio device and offline soundtrack (N9 / issue #37).

Model level only: synthetic event streams, no game process, no device, no
wall-clock dependency. Three claims are under test.

1. Every device answer (``is_playing`` / ``current_position``) is a pure
   function of the game tick and the sound's own duration.
2. The declared ``observe_only`` wiring leaves the simulation frames identical
   to ``sound_effects_allocation_none_v1`` while still producing audible voices,
   and the original-like ``device_feedback`` wiring provably does not (the 036 /
   #12 mechanism).
3. A rendered track's duration and sample coverage match the tick grid it
   claims, including pause cuts, pitch scaling and overlapping voices.
"""
from __future__ import annotations

import json
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from llm_vs_zombies import virtual_audio as va

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

TICK = va.SAMPLES_PER_TICK


def fixture_soundscape() -> va.Soundscape:
    """Five sounds and four Foley types covering every declared flag."""
    sounds = {
        101: va.SoundResource(id=101, name="splat-a", duration_samples=20 * TICK, frequency_hz=440.0),
        102: va.SoundResource(id=102, name="splat-b", duration_samples=20 * TICK, frequency_hz=660.0,
                              base_volume=0.5),
        103: va.SoundResource(id=103, name="groan", duration_samples=40 * TICK, frequency_hz=220.0),
        104: va.SoundResource(id=104, name="ambient", duration_samples=5 * TICK, frequency_hz=330.0),
        105: va.SoundResource(id=105, name="hint", duration_samples=3 * TICK, frequency_hz=880.0, pan=-4000),
    }
    types = {
        1: va.FoleyType(type_id=1, name="splat", resources=(101, 102), flags=frozenset({"dont_repeat"})),
        2: va.FoleyType(type_id=2, name="groan", resources=(103,),
                        flags=frozenset({"one_at_a_time", "mute_on_pause"})),
        3: va.FoleyType(type_id=3, name="ambient", resources=(104,), flags=frozenset({"loop"})),
        4: va.FoleyType(type_id=4, name="hint", resources=(105,),
                        flags=frozenset({"mute_on_pause", "uses_music_volume"})),
    }
    return va.Soundscape(sounds=sounds, types=types)


def play(seq: int, tick: int, type_id: int, resource: int, **extra) -> va.AudioEvent:
    return va.AudioEvent(seq=seq, tick=tick, kind="play", type_id=type_id, resource=resource, **extra)


def frames_of(path: Path) -> list[int]:
    with wave.open(str(path), "rb") as handle:
        raw = handle.readframes(handle.getnframes())
    return list(struct.unpack("<%dh" % (len(raw) // 2), raw))


class TimeBaseTests(unittest.TestCase):
    def test_one_tick_is_ten_milliseconds(self):
        self.assertEqual(va.SAMPLES_PER_TICK, 441)
        self.assertEqual(va.SAMPLE_RATE // va.TICKS_PER_SECOND, va.SAMPLES_PER_TICK)
        self.assertEqual(va.tick_to_frame(7), 7 * TICK)
        self.assertEqual(va.tick_to_frame(19, start_tick=4), 15 * TICK)
        with self.assertRaises(va.AudioModelError):
            va.tick_to_frame(3, start_tick=4)

    def test_soundscape_requires_an_exact_tick_grid(self):
        with self.assertRaises(va.AudioModelError):
            va.Soundscape(sounds={1: va.SoundResource(id=1, name="x", duration_samples=10)},
                          sample_rate=22050, ticks_per_second=100)
        with self.assertRaises(va.AudioModelError):
            va.Soundscape(sounds={1: va.SoundResource(id=1, name="x", duration_samples=10)},
                          types={2: va.FoleyType(type_id=2, resources=(99,))})


class MixerSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.space = fixture_soundscape()
        self.mixer = va.TickMixer(self.space)

    def test_is_playing_and_position_follow_tick_and_duration(self):
        voice = self.mixer.play(tick=10, type_id=1, resource=101)
        self.assertEqual(self.mixer.natural_end_tick(voice), 30)
        self.assertFalse(self.mixer.is_playing(9, voice))
        for tick in range(10, 30):
            self.assertTrue(self.mixer.is_playing(tick, voice), tick)
            self.assertEqual(self.mixer.current_position(tick, voice), (tick - 10) * TICK)
        self.assertFalse(self.mixer.is_playing(30, voice))
        self.assertEqual(self.mixer.current_position(30, voice), 20 * TICK)
        self.assertEqual(self.mixer.position_bytes(10, voice), 0)
        self.assertEqual(self.mixer.position_bytes(11, voice), TICK * va.BLOCK_ALIGN_BYTES)

    def test_pitch_changes_the_end_tick_but_keeps_the_duration(self):
        rate = va.pitch_rate(1.0)
        self.assertNotEqual(rate, (1, 1))
        voice = self.mixer.play(tick=0, type_id=1, resource=101, rate=rate, pitch_steps=1.0)
        self.assertEqual(voice.duration_samples, 20 * TICK)
        numerator, denominator = rate
        expected = next(tick for tick in range(0, 100)
                        if (tick * TICK * numerator) // denominator >= 20 * TICK)
        self.assertEqual(self.mixer.natural_end_tick(voice), expected)
        self.assertTrue(self.mixer.is_playing(expected - 1, voice))
        self.assertFalse(self.mixer.is_playing(expected, voice))
        self.assertGreater(va.frequency_hz(rate), va.DEFAULT_BASE_FREQUENCY)
        self.assertLess(va.frequency_hz(rate), int(va.DEFAULT_BASE_FREQUENCY * 1.1))
        self.assertEqual(va.pitch_rate(0.0), (1, 1))

    def test_looping_voice_never_ends_and_wraps_its_position(self):
        voice = self.mixer.play(tick=0, type_id=3, resource=104)
        self.assertTrue(voice.loop, "the type's loop flag must reach the voice")
        self.assertIsNone(self.mixer.natural_end_tick(voice))
        self.assertTrue(self.mixer.is_playing(10000, voice))
        self.assertEqual(self.mixer.current_position(2, voice), 2 * TICK)
        self.assertEqual(self.mixer.current_position(6, voice), (6 * TICK) % (5 * TICK))

    def test_stop_resets_the_cursor_and_release_retires_the_voice(self):
        voice = self.mixer.play(tick=0, type_id=1, resource=101)
        self.mixer.stop(tick=5, type_id=1)
        self.assertFalse(self.mixer.is_playing(6, voice))
        self.assertEqual(self.mixer.current_position(6, voice), 0)
        self.assertTrue(voice.stopped)
        self.assertIsNone(self.mixer.stop(tick=7, type_id=1))
        second = self.mixer.play(tick=8, type_id=1, resource=102)
        self.mixer.release(tick=9, voice_id=second.voice_id)
        self.assertTrue(second.released)
        self.assertFalse(self.mixer.is_playing(10, second))

    def test_pause_reads_the_position_and_resume_continues_from_it(self):
        voice = self.mixer.play(tick=0, type_id=2, resource=103)
        self.mixer.pause(tick=5)
        self.assertEqual(voice.pause_offset, 5 * TICK)
        self.assertEqual(self.mixer.counters["pause_offset"], 1)
        self.assertFalse(self.mixer.is_playing(5, voice))
        self.assertEqual(self.mixer.current_position(5, voice), 0)
        self.mixer.resume(tick=8)
        self.assertEqual(self.mixer.current_position(8, voice), 5 * TICK)
        self.assertEqual(self.mixer.current_position(9, voice), 6 * TICK)
        self.assertEqual(self.mixer.natural_end_tick(voice), 8 + 35)
        self.assertTrue(self.mixer.is_playing(42, voice))
        self.assertFalse(self.mixer.is_playing(43, voice))

    def test_set_position_and_set_rate_keep_the_cursor_continuous(self):
        voice = self.mixer.play(tick=0, type_id=1, resource=101)
        self.mixer.set_position(tick=2, voice=voice, offset_samples=10 * TICK)
        self.assertEqual(self.mixer.current_position(2, voice), 10 * TICK)
        self.assertEqual(self.mixer.natural_end_tick(voice), 12)
        self.mixer.set_rate(tick=3, voice=voice, rate=(2, 1))
        self.assertEqual(self.mixer.current_position(3, voice), 11 * TICK)
        self.assertEqual(self.mixer.natural_end_tick(voice), 8)

    def test_queries_are_pure_functions_of_tick(self):
        voice = self.mixer.play(tick=0, type_id=1, resource=101)
        forward = [(tick, self.mixer.is_playing(tick, voice), self.mixer.current_position(tick, voice))
                   for tick in range(0, 40)]
        backward = [(tick, self.mixer.is_playing(tick, voice), self.mixer.current_position(tick, voice))
                    for tick in range(39, -1, -1)]
        self.assertEqual(forward, list(reversed(backward)))

    def test_identical_streams_produce_identical_state(self):
        def build() -> va.TickMixer:
            mixer = va.TickMixer(fixture_soundscape())
            mixer.play(tick=0, type_id=1, resource=101, variation=0)
            mixer.play(tick=3, type_id=2, resource=103, variation=0)
            mixer.pause(tick=5)
            mixer.resume(tick=9)
            return mixer
        self.assertEqual(build().snapshot(12), build().snapshot(12))

    def test_invalid_device_usage_is_rejected(self):
        with self.assertRaises(va.AudioModelError):
            self.mixer.play(tick=0, type_id=1, resource=999)
        with self.assertRaises(va.AudioModelError):
            self.mixer.play(tick=0, type_id=1, resource=101, rate=(0, 1))
        silent = va.AllocationNoneDevice()
        with self.assertRaises(va.AudioModelError):
            silent.is_playing(None, 0)

    def test_manager_channel_table_exhaustion_returns_no_instance(self):
        mixer = va.TickMixer(self.space, wiring="device_feedback")
        device = va.TickVirtualDevice(mixer)
        intents = [va.PlayIntent(seq=index, tick=0, type_id=1, variation=0, resource=101, pitch=0.0,
                                 loop=False, rate=(1, 1)) for index in range(va.MAX_MANAGER_CHANNELS)]
        self.assertEqual([device.allocate(0, intent) for intent in intents].count(None), 0)
        self.assertIsNone(device.allocate(0, va.PlayIntent(seq=99, tick=0, type_id=1, variation=0,
                                                           resource=101, pitch=0.0, loop=False, rate=(1, 1))))
        mixer.release(tick=0, voice_id=0)
        self.assertIsNotNone(device.allocate(0, va.PlayIntent(seq=100, tick=0, type_id=1, variation=0,
                                                              resource=101, pitch=0.0, loop=False, rate=(1, 1))))


class TrackTests(unittest.TestCase):
    def setUp(self):
        self.space = fixture_soundscape()
        self.directory = Path(tempfile.mkdtemp(prefix="lvz-audio-"))

    def write(self, track: va.Track, name: str = "track") -> Path:
        path = self.directory / f"{name}.wav"
        va.write_wav(path, track)
        return path

    def test_track_length_and_sample_coverage_match_the_tick_grid(self):
        track = va.render_track([play(0, 10, 1, 101)], self.space)
        self.assertEqual(track.manifest["start_tick"], 0)
        self.assertEqual(track.manifest["end_tick"], 30)
        self.assertEqual(track.frames, 30 * TICK)
        self.assertAlmostEqual(track.seconds, 0.3, places=6)
        coverage = track.manifest["coverage"]
        self.assertEqual(coverage["first_frame"], 10 * TICK)
        self.assertEqual(coverage["last_frame"], 30 * TICK)
        self.assertEqual(coverage["union_frames"], 20 * TICK)
        self.assertEqual(coverage["silent_frames"], 10 * TICK)
        samples = frames_of(self.write(track))
        self.assertEqual(len(samples) // 2, track.frames)
        self.assertEqual(set(samples[: 10 * TICK * 2]), {0})
        self.assertTrue(any(value for value in samples[10 * TICK * 2:]))

    def test_explicit_end_tick_keeps_trailing_silence(self):
        track = va.render_track([play(0, 10, 1, 101)], self.space, end_tick=40)
        self.assertEqual(track.frames, 40 * TICK)
        self.assertEqual(track.manifest["coverage"]["silent_frames"], 20 * TICK)
        samples = frames_of(self.write(track))
        self.assertEqual(set(samples[30 * TICK * 2:]), {0})

    def test_overlapping_voices_report_union_not_sum(self):
        events = [play(0, 0, 1, 101), play(1, 5, 2, 103)]
        track = va.render_track(events, self.space)
        coverage = track.manifest["coverage"]
        self.assertEqual(track.manifest["end_tick"], 45)
        self.assertEqual(coverage["sum_voice_frames"], (20 + 40) * TICK)
        self.assertEqual(coverage["union_frames"], 45 * TICK)
        self.assertLess(coverage["union_frames"], coverage["sum_voice_frames"])
        self.assertEqual(coverage["silent_frames"], 0)

    def test_pause_cuts_the_voice_and_resume_restarts_from_the_offset(self):
        events = [play(0, 0, 2, 103), va.AudioEvent(seq=1, tick=5, kind="pause", type_id=2),
                  va.AudioEvent(seq=2, tick=20, kind="resume", type_id=2)]
        track = va.render_track(events, self.space, end_tick=60)
        voice = track.manifest["voices"][0]
        self.assertEqual([segment["frames"] for segment in voice["segments"]], [5 * TICK, 35 * TICK])
        self.assertEqual(voice["frames"], 40 * TICK)
        self.assertEqual(voice["segments"][0]["offset_samples"], 0)
        self.assertEqual(voice["segments"][1]["offset_samples"], 5 * TICK)
        self.assertEqual(voice["end_tick"], 55)
        self.assertTrue(va.verify_track(self.write(track), track.manifest)["ok"])

    def test_admission_original_reproduces_the_device_gates(self):
        events = [play(0, 0, 1, 101), play(1, 3, 1, 101), play(2, 12, 1, 101), play(3, 30, 1, 101)]
        original = va.render_track(events, self.space, admission="original")
        every = va.render_track(events, self.space, admission="all")
        self.assertEqual([entry["reason"] for entry in original.manifest["dropped"]], ["recent"])
        self.assertEqual(original.manifest["dropped"][0]["tick"], 3)
        self.assertEqual(len(original.manifest["voices"]), 3)
        self.assertEqual(len(every.manifest["voices"]), 4)
        self.assertEqual(every.manifest["dropped"], [])
        self.assertEqual(original.manifest["events"]["play"], every.manifest["events"]["play"])
        self.assertGreater(original.manifest["mixer_counters"]["is_playing"], 0)

    def test_one_at_a_time_gate_uses_tick_based_play_state(self):
        events = [play(0, 0, 2, 103), play(1, 12, 2, 103), play(2, 45, 2, 103)]
        track = va.render_track(events, self.space, admission="original", end_tick=100)
        self.assertEqual([entry["reason"] for entry in track.manifest["dropped"]], ["reuse"])
        self.assertEqual(track.manifest["dropped"][0]["tick"], 12)
        self.assertEqual(len(track.manifest["voices"]), 2)

    def test_looping_voice_needs_an_explicit_horizon(self):
        events = [play(0, 0, 3, 104)]
        with self.assertRaises(va.AudioModelError):
            va.render_track(events, self.space)
        track = va.render_track(events, self.space, end_tick=30)
        self.assertEqual(track.frames, 30 * TICK)
        self.assertEqual(track.manifest["coverage"]["union_frames"], 30 * TICK)

    def test_render_reads_no_wall_clock(self):
        with mock.patch("time.monotonic", side_effect=AssertionError("clock read")), \
                mock.patch("time.time", side_effect=AssertionError("clock read")):
            track = va.render_track([play(0, 0, 1, 101)], self.space)
        self.assertEqual(track.frames, 20 * TICK)

    def test_verify_track_accepts_clean_and_rejects_tampered_evidence(self):
        track = va.render_track([play(0, 3, 1, 101)], self.space)
        path = self.write(track)
        self.assertTrue(va.verify_track(path, track.manifest)["ok"])
        for name, mutate in (
                ("frames", lambda value: value.update(total_frames=value["total_frames"] + 1)),
                ("hash", lambda value: value.update(pcm_sha256="0" * 64)),
                ("start", lambda value: value["voices"][0].update(start_frame=0)),
                ("coverage", lambda value: value["coverage"].update(union_frames=0)),
                ("counted", lambda value: value["events"].update(play=3))):
            manifest = json.loads(json.dumps(track.manifest))
            mutate(manifest)
            report = va.verify_track(path, manifest)
            self.assertFalse(report["ok"], name)
            self.assertTrue(report["failures"], name)
        short = self.directory / "short.wav"
        with wave.open(str(short), "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(va.SAMPLE_RATE)
            handle.writeframes(track.pcm[:-200])
        self.assertFalse(va.verify_track(short, track.manifest)["ok"])


class FoleyEquivalenceTests(unittest.TestCase):
    """The model-level proof behind the issue's acceptance criterion."""

    ACTION_STREAM = (
        ("play", 0, 1, 0.0), ("play", 0, 1, 0.0), ("play", 3, 1, 0.0), ("play", 12, 1, 1.0),
        ("play", 30, 1, 0.0), ("play", 31, 1, 0.0), ("play", 0, 2, 0.0), ("play", 12, 2, 0.0),
        ("play", 3, 4, 0.0), ("stop", 25, 2), ("play", 40, 2, 0.0), ("play", 8, 3, 0.0),
        ("play", 9, 3, 0.0), ("pause", 6, True), ("pause", 14, False), ("is_playing", 5, 1),
        ("is_playing", 20, 2), ("is_playing", 45, 1), ("cancel_paused", 60),
    )

    def setUp(self):
        self.space = fixture_soundscape()

    def silent(self, ticks=80, app_clock=None):
        return va.run_stream(self.space, self.ACTION_STREAM, ticks=ticks,
                             device=va.AllocationNoneDevice(), app_clock=app_clock)

    def observe_only(self, ticks=80, app_clock=None, admission="original"):
        mixer = va.TickMixer(self.space, wiring="observe_only")
        observer = va.MixerObserver(mixer, admission=admission)
        trace = va.run_stream(self.space, self.ACTION_STREAM, ticks=ticks,
                              device=va.AllocationNoneDevice(), observer=observer, app_clock=app_clock)
        return trace, observer

    def feedback(self, ticks=80, app_clock=None):
        mixer = va.TickMixer(self.space, wiring="device_feedback")
        return va.run_stream(self.space, self.ACTION_STREAM, ticks=ticks,
                             device=va.TickVirtualDevice(mixer), app_clock=app_clock)

    def test_observe_only_is_frame_identical_to_allocation_none(self):
        silent = self.silent()
        declared, observer = self.observe_only()
        comparison = va.compare_traces(silent, declared)
        self.assertTrue(comparison["equal"], comparison["first_difference"])
        self.assertEqual(len(silent.frames), 80)
        self.assertEqual([frame["tick"] for frame in declared.frames], list(range(80)))
        self.assertEqual(silent.counters["rng_draws"], declared.counters["rng_draws"])
        self.assertEqual(silent.events, declared.events)
        self.assertEqual(silent.queries, declared.queries)
        self.assertEqual(silent.counters["device_counters"], declared.counters["device_counters"])
        # The same frames still carry audible voices, and the mixer answers were
        # produced by the tick model only.
        self.assertGreater(declared.counters["voices"], 0)
        self.assertGreater(observer.mixer.counters["is_playing"], 0)
        self.assertGreater(observer.mixer.counters["pause_offset"], 0)
        self.assertEqual(len([entry for entry in observer.mixer.log if entry["kind"] == "play"]),
                         len([event for event in observer.events if event.kind == "play"]))
        for query in declared.frames[-1]["slots"].values():
            self.assertEqual({slot[0] for slot in query}, {0})

    def test_observer_never_consumes_randomness_even_with_every_intent_rendered(self):
        silent = self.silent()
        declared, observer = self.observe_only(admission="all")
        self.assertEqual(silent.counters["rng_draws"], declared.counters["rng_draws"])
        self.assertEqual([slot for slot in silent.events], [slot for slot in declared.events])
        self.assertEqual(len(observer.dropped), 0)
        self.assertEqual(len(observer.mixer.voices),
                         len([event for event in observer.events if event.kind == "play"]))

    def test_device_feedback_reproduces_the_036_divergence(self):
        silent = self.silent()
        feedback = self.feedback()
        comparison = va.compare_traces(silent, feedback)
        self.assertFalse(comparison["equal"])
        self.assertEqual(comparison["first_difference"]["tick"], 0)
        self.assertIn(comparison["first_difference"]["key"], {"slots", "last_variation", "rng_draws"})
        self.assertEqual(silent.counters["rng_draws"],
                         sum(1 for action in self.ACTION_STREAM if action[0] == "play"))
        self.assertLess(feedback.counters["rng_draws"], silent.counters["rng_draws"])
        self.assertEqual([event["outcome"] for event in silent.events[:2]], ["no_instance", "no_instance"])
        self.assertEqual([event["outcome"] for event in feedback.events[:2]], ["played", "recent"])
        self.assertEqual(feedback.events[1]["tick"], 0)
        self.assertEqual([query["result"] for query in silent.queries], [False, False, False])
        self.assertEqual([query["result"] for query in feedback.queries], [True, True, True])

    def test_frozen_app_counter_makes_the_pause_window_visible(self):
        app_clock = lambda tick: min(tick, 20)
        actions = (("play", 0, 2, 0.0), ("pause", 5, True), ("play", 8, 2, 0.0),
                   ("pause", 40, False), ("play", 41, 2, 0.0), ("play", 45, 2, 0.0))
        silent = va.run_stream(self.space, actions, ticks=60, device=va.AllocationNoneDevice(),
                               app_clock=app_clock)
        mixer = va.TickMixer(self.space)
        observer = va.MixerObserver(mixer)
        declared = va.run_stream(self.space, actions, ticks=60, device=va.AllocationNoneDevice(),
                                 observer=observer, app_clock=app_clock)
        self.assertTrue(va.compare_traces(silent, declared)["equal"])
        plays = sum(1 for action in actions if action[0] == "play")
        self.assertEqual(silent.counters["rng_draws"], plays, "silent mode never rejects a play")
        self.assertEqual([segment.end_tick for segment in mixer.voices[0].segments], [5, None])
        feedback = va.run_stream(self.space, actions, ticks=60,
                                 device=va.TickVirtualDevice(va.TickMixer(self.space)), app_clock=app_clock)
        self.assertLess(feedback.counters["rng_draws"], silent.counters["rng_draws"])

    def test_declared_run_can_render_a_track_from_its_own_intents(self):
        trace, observer = self.observe_only(ticks=80)
        self.assertGreater(len(observer.dropped), 0, "the audible gate must have dropped something")
        events = [va.AudioEvent(seq=index, tick=event.tick, kind=event.kind, type_id=event.type_id,
                                resource=event.resource, variation=event.variation, rate=event.rate,
                                volume=event.volume, pan=event.pan, loop=event.loop,
                                pitch_steps=event.pitch_steps)
                  for index, event in enumerate(observer.events)]
        track = va.render_track(events, self.space, admission="original", end_tick=80)
        self.assertEqual(track.manifest["start_tick"], 0)
        self.assertEqual(track.manifest["end_tick"], 80)
        self.assertEqual(track.frames, 80 * TICK)
        self.assertTrue(va.verify_track(self._write(track), track.manifest)["ok"])
        played = len([event for event in observer.events if event.kind == "play"])
        self.assertEqual(track.manifest["events"]["play"], played)
        self.assertEqual(track.manifest["dropped"], [], "admitted intents must replay without new drops")
        self.assertEqual(len(track.manifest["voices"]), len(observer.mixer.voices))

    def _write(self, track: va.Track) -> Path:
        path = Path(tempfile.mkdtemp(prefix="lvz-audio-")) / "intents.wav"
        va.write_wav(path, track)
        return path


class FoleyTraceAdapterTests(unittest.TestCase):
    def setUp(self):
        self.space = fixture_soundscape()

    @staticmethod
    def rows() -> list[dict]:
        return [
            {"schema": "lvz.foley-event.v1", "ordinal": 1, "kind": "foley_enter", "invocation_id": 7,
             "foley_type": 1, "pitch_bits": 0, "resource_ids": [101, 102, None],
             "version": {"epoch": 1, "tick": 4, "revision": 0}},
            {"kind": "is_playing_begin", "invocation_id": 7},
            {"kind": "variation_return", "invocation_id": 7, "selected_variation": 1, "returned_index": 0},
            {"kind": "sound_instance_return", "invocation_id": 7, "returned_instance": None},
            {"kind": "recent_return", "invocation_id": 8, "foley_type": 1},
            {"kind": "foley_enter", "ordinal": 2, "kind2": None, "invocation_id": 9, "foley_type": 1,
             "pitch_bits": struct.unpack("<I", struct.pack("<f", 4.0))[0],
             "resource_ids": [101, 102, None], "version": {"epoch": 1, "tick": 7, "revision": 0}},
            {"kind": "variation_return", "invocation_id": 9, "selected_variation": 0},
            {"kind": "foley_return_full", "invocation_id": 9},
            {"kind": "foley_return_common", "invocation_id": 7},
        ]

    def test_adapter_uses_the_drawn_variation_and_ignores_rejected_calls(self):
        events = va.events_from_foley_trace(self.rows(), self.space)
        self.assertEqual([(event.tick, event.variation, event.resource) for event in events],
                         [(4, 1, 102), (7, 0, 101)])
        self.assertEqual([event.seq for event in events], [0, 1])
        self.assertEqual(events[0].rate, (1, 1))
        self.assertGreater(events[1].rate[0], events[1].rate[1], "four semitones must speed the cursor up")
        self.assertFalse(events[0].loop)
        track = va.render_track(events, self.space, admission="all", end_tick=40)
        path = Path(tempfile.mkdtemp(prefix="lvz-audio-")) / "trace.wav"
        va.write_wav(path, track)
        self.assertTrue(va.verify_track(path, track.manifest)["ok"])
        self.assertEqual(len(track.manifest["voices"]), 2)

    def test_event_stream_round_trip_is_strict(self):
        events = va.events_from_foley_trace(self.rows(), self.space)
        path = Path(tempfile.mkdtemp(prefix="lvz-audio-")) / "events.jsonl"
        path.write_text(va.dump_events(events), encoding="utf-8")
        self.assertEqual(va.load_events(path), events)
        path.write_text(va.dump_events(list(reversed(events))), encoding="utf-8")
        with self.assertRaises(va.AudioModelError):
            va.load_events(path)
        broken = Path(tempfile.mkdtemp(prefix="lvz-audio-")) / "broken.jsonl"
        broken.write_text('{"schema":"lvz.audio-event.v1","seq":0,"tick":0,"kind":"play"}\n', encoding="utf-8")
        with self.assertRaises(va.AudioModelError):
            va.load_events(broken)

    def test_soundscape_round_trip(self):
        value = va.soundscape_to_json(self.space)
        path = Path(tempfile.mkdtemp(prefix="lvz-audio-")) / "soundscape.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        loaded = va.load_soundscape(path)
        self.assertEqual(loaded, self.space)


class OfflineSoundtrackToolTests(unittest.TestCase):
    """The tool only adds file handling; the model tests above carry the claims."""

    def setUp(self):
        import offline_soundtrack
        self.tool = offline_soundtrack
        self.directory = Path(tempfile.mkdtemp(prefix="lvz-soundtrack-"))
        self.resources = self.directory / "soundscape.json"
        self.resources.write_text(json.dumps(va.soundscape_to_json(fixture_soundscape())), encoding="utf-8")
        self.events = self.directory / "events.jsonl"
        self.events.write_text(va.dump_events([play(0, 3, 1, 101), play(1, 10, 2, 103)]), encoding="utf-8")
        self.wav = self.directory / "track.wav"
        self.manifest = self.directory / "track.audio.json"

    def build(self, *extra: str) -> str:
        import contextlib
        import io
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = self.tool.main(["build", "--events", str(self.events), "--resources", str(self.resources),
                                   "--wav", str(self.wav), *extra])
        self.assertEqual(code, 0)
        return stream.getvalue()

    def test_build_then_verify_reports_coverage(self):
        report = json.loads(self.build("--end-tick", "60"))
        self.assertEqual(report["frames"], 60 * TICK)
        self.assertEqual(report["seconds"], "0.600000")
        self.assertTrue(self.wav.exists() and self.manifest.exists())
        value = json.loads(self.manifest.read_text(encoding="utf-8"))
        # Voice one covers ticks 3..23, voice two ticks 10..50; the union is
        # 10..50 for the second one plus 3..10 of the first.
        self.assertEqual(value["coverage"]["union_frames"], 47 * TICK)
        self.assertEqual(value["coverage"]["silent_frames"], 13 * TICK)
        self.assertEqual([voice["start_frame"] for voice in value["voices"]], [3 * TICK, 10 * TICK])
        self.assertEqual(self.verify(), 0)
        value["coverage"]["union_frames"] += 1
        self.manifest.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(self.verify(), 1)

    def verify(self) -> int:
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            return self.tool.main(["verify", "--wav", str(self.wav), "--manifest", str(self.manifest)])

    def test_existing_artifacts_and_missing_arguments_are_rejected(self):
        self.build("--end-tick", "60")
        with self.assertRaises(SystemExit):
            self.tool.main(["build", "--events", str(self.events), "--resources", str(self.resources),
                            "--wav", str(self.wav), "--end-tick", "60"])
        with self.assertRaises(SystemExit):
            self.tool.main(["build", "--events", str(self.events), "--resources", str(self.resources),
                            "--wav", str(self.directory / "other.wav"), "--end-tick", "60", "--mux", "video.mp4"])

    def test_video_manifest_alignment_is_checked(self):
        video = self.directory / "sample.video.json"
        video.write_text(json.dumps({"schema": 1, "timing_basis": "game_tick", "ticks_per_second": 100,
                                     "epoch": 3, "start_tick": 0}), encoding="utf-8")
        report = json.loads(self.build("--end-tick", "60", "--video-manifest", str(video)))
        self.assertTrue(report["alignment"]["checked"])
        self.assertEqual(report["alignment"]["tick_offset"], 0)
        video.write_text(json.dumps({"schema": 1, "timing_basis": "game_tick", "ticks_per_second": 100,
                                     "epoch": 3, "start_tick": 5}), encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.tool.main(["build", "--events", str(self.events), "--resources", str(self.resources),
                            "--wav", str(self.directory / "offset.wav"), "--end-tick", "60",
                            "--video-manifest", str(video), "--mux", "video.mp4", "--output", "out.mp4"])

    def test_foley_trace_input_uses_the_drawn_variation(self):
        trace = self.directory / "foley-events.jsonl"
        trace.write_text("\n".join(json.dumps(row) for row in FoleyTraceAdapterTests.rows()) + "\n",
                         encoding="utf-8")
        report = json.loads(self.build("--events", str(trace), "--from-foley-trace", "--end-tick", "40",
                                       "--admission", "all"))
        self.assertEqual(report["voices"], 2)
        self.assertEqual(report["dropped"], 0)


if __name__ == "__main__":
    unittest.main()
