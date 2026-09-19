"""Same-frame audio composition; synthetic fixtures do not prove live replay."""
import copy
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from llm_vs_zombies import sound_counter as counter, sound_effects as audio, app_update_anchor as anchor
from llm_vs_zombies.audit_compare import AuditFrame, EvidenceError
from test_draw_schedule import DRAW_SPEC
from test_sound_effects import SPEC, activation, sound_state


def fixture(*, scope=True):
    game = {'sound_effects': copy.deepcopy(SPEC)}
    if scope:
        game.update(sound_counter=copy.deepcopy(counter.SPEC), app_update_anchor=copy.deepcopy(anchor.SPEC),
                    draw_schedule=copy.deepcopy(DRAW_SPEC))
    a, c = audio.Evidence(game, activation()), counter.Evidence(game, activation())
    if scope:
        # Isolate the per-frame contract. Full initialization/event ordering is
        # separately exercised by test_sound_counter's real reader fixtures.
        c.origin, c.anchor, c.last_raw = {'origin_raw_calls': 14}, {}, 14
    sound = sound_state()
    if scope:
        sound['counter_scope'] = 'experiment'
    frame = AuditFrame(10, 'pre_step', {'epoch': 3, 'revision': 0, 'tick': 0},
        {'engine_call': {'engine_call_id': 1}}, {'sound_effects': sound}, {})
    raw = {'schema': 'lvz.sound-counter-raw.v1', 'seq': 10, 'kind': 'pre_step',
        'version': copy.deepcopy(frame.version), 'engine_call_id': 1,
        'raw_calls': 19, 'origin_raw_calls': 14, 'experiment_calls': 5}
    return a, c, frame, raw


class AudioFrameValidationTests(unittest.TestCase):
    def test_composed_frame_checks_full_state_once_and_all_raw_fields(self):
        a, c, frame, raw = fixture()
        with patch.object(audio, 'state', wraps=audio.state) as checked:
            c.frame_with_audio(a, frame, raw)
        self.assertEqual(checked.call_count, 1)
        self.assertEqual((a.calls, c.last_raw, c.last_calls, c.frames), (5, 19, 5, 1))

    def test_invalid_sound_with_valid_raw_rejected_by_both_entry_points(self):
        edits = [lambda s: s['histories'][109]['slots'][7].__setitem__(1, 1),
                 lambda s: s['channels'].__setitem__(31, False),
                 lambda s: s['parameters'][0]['sound_ids'].__setitem__(0, None),
                 lambda s: s.update(counter_scope='bootstrap_lifetime')]
        for index, edit in enumerate(edits):
            for composed in (False, True):
                a, c, frame, raw = fixture()
                edit(frame.state['sound_effects'])
                with self.subTest(edit=index, composed=composed), self.assertRaises(EvidenceError):
                    c.frame_with_audio(a, frame, raw) if composed else c.frame(frame, raw)
                self.assertEqual(c.frames, 0)

    def test_valid_sound_never_authorizes_corrupted_raw(self):
        edits = [lambda r: r.update(seq=11), lambda r: r.update(engine_call_id=2),
                 lambda r: r['version'].update(revision=1), lambda r: r.update(origin_raw_calls=15),
                 lambda r: r.update(raw_calls=20), lambda r: r.update(experiment_calls=True),
                 lambda r: r.update(raw_calls=0x100000000), lambda r: r.update(extra='unchecked')]
        for index, edit in enumerate(edits):
            a, c, frame, raw = fixture()
            edit(raw)
            with self.subTest(edit=index), self.assertRaises(EvidenceError):
                c.frame_with_audio(a, frame, raw)
            self.assertEqual(c.frames, 0)

    def test_mutable_next_frame_is_fully_revalidated(self):
        a, c, frame, raw = fixture()
        following = replace(frame, seq=11, kind='post_step', version=dict(frame.version, tick=1))
        following_raw = dict(raw, seq=11, kind='post_step', version=following.version)
        self.assertIs(following.state, frame.state)
        with patch.object(audio, 'state', wraps=audio.state) as checked:
            c.frame_with_audio(a, frame, raw)
            # Exactly how a decoded JSON patch can mutate a previously checked
            # subtree without changing its object identity or counter value.
            following.state['sound_effects']['histories'][109]['slots'][7][0] = 123
            with self.assertRaises(EvidenceError):
                c.frame_with_audio(a, following, following_raw)
        self.assertEqual(checked.call_count, 2)
        self.assertEqual(c.frames, 1)

    def test_valid_changed_history_is_not_a_cross_frame_cache(self):
        a, c, frame, raw = fixture()
        with patch.object(audio, 'state', wraps=audio.state) as checked:
            c.frame_with_audio(a, frame, raw)
            frame.state['sound_effects']['histories'][109]['last_variation'] = 7
            following = replace(frame, seq=11, kind='post_step', version=dict(frame.version, tick=1))
            c.frame_with_audio(a, following, dict(raw, seq=11, kind='post_step', version=following.version))
        self.assertEqual(checked.call_count, 2)
        self.assertEqual(c.frames, 2)

    def test_standalone_remains_strict_and_has_no_caller_authority_parameter(self):
        a, c, frame, raw = fixture()
        with patch.object(audio, 'state', wraps=audio.state) as checked:
            c.frame(frame, raw)
        self.assertEqual(checked.call_count, 1)
        with self.assertRaises(TypeError):
            c.frame(frame, raw, validated_sound=frame.state['sound_effects'])
        forged = SimpleNamespace(game=c.game, frame=lambda _: frame.state['sound_effects'])
        with self.assertRaises(EvidenceError):
            c.frame_with_audio(forged, frame, raw)
        unrelated = audio.Evidence(copy.deepcopy(c.game), activation())
        with self.assertRaises(EvidenceError):
            c.frame_with_audio(unrelated, frame, raw)

    def test_original_and_legacy_silent_modes_keep_their_checks(self):
        game = {}
        a, c = audio.Evidence(game), counter.Evidence(game)
        frame = AuditFrame(0, 'pre_step', {'epoch': 1, 'revision': 0, 'tick': 0}, {}, {}, {})
        c.frame_with_audio(a, frame, None)
        with self.assertRaises(EvidenceError):
            c.frame_with_audio(a, replace(frame, state={'sound_effects': sound_state()}), None)
        a, c, frame, _ = fixture(scope=False)
        c.frame_with_audio(a, frame, None)
        self.assertEqual(a.calls, 5)
        frame.state['sound_effects']['calls'] = 4
        with self.assertRaises(EvidenceError):
            c.frame_with_audio(a, frame, None)


if __name__ == '__main__':
    unittest.main()
