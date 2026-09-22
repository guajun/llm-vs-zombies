"""Self-test for tools/giant_fork_diff.py on synthetic JSON Patch streams.

Nothing here starts a game or reads a run directory: the audit evidence is
fabricated with the same envelope the native recorder writes (``initial`` first,
then one ``pre_step``/``post_step`` pair per tick), so the replay, the giant
selection, the resample detection and the divergence scan are all exercised
offline.
"""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SPEC = importlib.util.spec_from_file_location("giant_fork_diff", ROOT / "tools" / "giant_fork_diff.py")
giant_fork_diff = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = giant_fork_diff          # dataclasses resolve cls.__module__ through sys.modules
SPEC.loader.exec_module(giant_fork_diff)

from llm_vs_zombies.audit_compare import digests, patch as apply_patch
from llm_vs_zombies.evidence_codec import compress_evidence

SPAWN_VELX = 0x3E9704C6          # 0.294958293
RESAMPLED_VELX = 0x3E915324      # 0.283837438
BODY = "zombies/df000b/body#0"
BODY_POINTER = "/reanimations/nodes/zombies~1df000b~1body#0/state"
MT_CURSOR = 5


def _undo_right_shift(value, shift):
    result = value
    for _ in range(32 // shift + 1):
        result = value ^ (result >> shift)
    return result & 0xFFFFFFFF


def _undo_left_shift(value, shift, mask):
    result = value
    for _ in range(32 // shift + 1):
        result = value ^ ((result << shift) & mask)
    return result & 0xFFFFFFFF


def untemper(value):
    """Invert MT19937 tempering, so a crafted mt[] word yields a chosen draw."""
    value = _undo_right_shift(value, 18)
    value = _undo_left_shift(value, 15, 0xEFC60000)
    value = _undo_left_shift(value, 7, 0x9D2C5680)
    return _undo_right_shift(value, 11)


def bits_of(value):
    import struct
    return struct.unpack("<I", struct.pack("<f", value))[0]


def draw_for_velocity(target_bits):
    """The MT draw whose RandRangeFloat(0.23f, 0.37f) is ``target_bits``."""
    estimate = int(round((giant_fork_diff.float32(target_bits) - 0.23) / 0.14 * 0x7FFFFFFF))
    for word in range(max(estimate - 4, 0), estimate + 5):
        if bits_of(giant_fork_diff.velocity_from_draw(word)) == target_bits:
            return word
    raise AssertionError(f"no draw reproduces {target_bits:#010x}")


RESAMPLE_DRAW = draw_for_velocity(RESAMPLED_VELX)
MT_WORDS = [(0x12345678 + 7919 * index) & 0xFFFFFFFF for index in range(624)]
MT_WORDS[MT_CURSOR] = untemper(RESAMPLE_DRAW)


def zombie_slot(zombie_type, phase=0, velx=SPAWN_VELX, x=0x445A0000, hp=3000):
    return {"id_or_free_next": 0x00DF000B, "fields": {
        "00000008": 897,
        "00000020": 0x00DF000B,
        "00000024": zombie_type,
        "00000028": phase,
        "0000002c": x,
        "00000034": velx,
        "000000c8": hp,
        "00000118": {"node": BODY, "status": "live"},
    }}


def plant_slot(plant_type, row, col, hp=300, disappeared=0, crushed=0):
    return {"id_or_free_next": 0x00DC0041, "fields": {
        "00000008": col * 80 - 42,
        "00000018": 1,
        "0000001c": row,
        "00000024": plant_type,
        "00000028": col,
        "00000040": hp,
        "00000044": 300,
        "00000141": disappeared,
        "00000142": crushed,
    }}


def animation(loop_count=0, loop_type=0, frame_count=49, anim_time=0x3A926B31):
    return {"owners": ["zombies/df000b/body"], "state": {
        "anim_rate_bits": 1085221052, "anim_time_bits": anim_time, "dead": False,
        "frame_count": frame_count, "loop_count": loop_count, "loop_type": loop_type}}


def base_state():
    return {"schema": "lvz.audit.v1", "app": {"mj_clock": 0, "game_mode": 13},
            "board": {"00000164": 0},
            "rng": {"instances": {"global_mt": {"algorithm": "sexy_mt19937_31",
                                                "cursor": MT_CURSOR, "words": list(MT_WORDS)}}},
            "zombies": {"capacity": 1024, "used": 2, "slots": {
                "0": zombie_slot(0, velx=0x3E880000), "11": zombie_slot(23)}},
            "plants": {"capacity": 1024, "used": 97, "slots": {"65": {"id_or_free_next": 0}}},
            "reanimations": {"valid": True, "issues": [], "nodes": {BODY: animation()}}}


def frame_record(seq, kind, tick, *, initial=None, patch=None):
    record = {"schema": "lvz.audit.v1", "seq": seq, "kind": kind,
              "version": {"epoch": 3, "tick": tick, "revision": 0}, "payload": {"request_id": "r"}}
    if initial is not None:
        record["initial"] = initial
    else:
        record["patch"] = patch or []
    return record


def synthetic_stream():
    """Four ticks: spawn, walk, smash entry, loop-count flip, walk resume."""
    state = base_state()
    del state["zombies"]["slots"]["11"]          # pre_step(0) is before the spawn
    records = [frame_record(0, "pre_step", 0, initial=state)]
    steps = [
        ("post_step", 1, [{"op": "add", "path": "/zombies/slots/11", "value": zombie_slot(23)}]),
        ("pre_step", 1, [{"op": "add", "path": "/plants/slots/65", "value": plant_slot(8, 5, 6)}]),
        ("post_step", 2, [{"op": "replace", "path": "/zombies/slots/11/fields/00000028", "value": 70},
                          {"op": "replace", "path": BODY_POINTER + "/loop_type", "value": 3},
                          {"op": "replace", "path": BODY_POINTER + "/frame_count", "value": 33}]),
        ("pre_step", 2, [{"op": "replace", "path": "/plants/slots/65/fields/00000142", "value": 1}]),
        ("post_step", 3, [{"op": "replace", "path": BODY_POINTER + "/loop_count", "value": 1},
                          {"op": "replace", "path": BODY_POINTER + "/anim_time_bits", "value": 0x3F800000}]),
        ("pre_step", 3, [{"op": "remove", "path": "/plants/slots/65/fields/00000142"},
                         {"op": "add", "path": "/plants/slots/65/fields/00000141", "value": 1}]),
        ("post_step", 4, [{"op": "replace", "path": "/zombies/slots/11/fields/00000028", "value": 0},
                          {"op": "replace", "path": "/zombies/slots/11/fields/00000034", "value": RESAMPLED_VELX},
                          {"op": "replace", "path": BODY_POINTER + "/loop_count", "value": 0},
                          {"op": "replace", "path": BODY_POINTER + "/loop_type", "value": 0},
                          {"op": "replace", "path": BODY_POINTER + "/frame_count", "value": 49}]),
        # The MT cursor moves after the resample: the reported cursor_before
        # must stay the pre-resample value, never a live reference to this.
        ("pre_step", 4, [{"op": "replace", "path": "/rng/instances/global_mt/cursor",
                          "value": MT_CURSOR + 1}]),
    ]
    for index, (kind, tick, patch) in enumerate(steps, start=1):
        records.append(frame_record(20 + index, kind, tick, patch=patch))
    return records


def write_evidence(directory, records, *, compress=False):
    audit = Path(directory) / "audit"
    audit.mkdir(parents=True, exist_ok=True)
    (audit / "state-deltas.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    rows, state = [], None
    for number, record in enumerate(records):
        state = record["initial"] if number == 0 else apply_patch(state, record["patch"], in_place=True)
        rows.append(json.dumps(dict(record, digests=digests(state))) + "\n")
    (audit / "checksums.jsonl").write_text("".join(rows), encoding="utf-8")
    if compress:
        compress_evidence(audit)
    return Path(directory)


class ReplayTest(unittest.TestCase):
    def test_replay_rebuilds_every_frame(self):
        # Frames share one in-place state, so every frame is asserted as it is
        # yielded rather than after the whole stream has been consumed.
        seen = []
        for frame in giant_fork_diff.replay(iter(synthetic_stream())):
            zombies = frame.state["zombies"]["slots"]
            plants = frame.state["plants"]["slots"]
            # Every frame shares one in-place state, so values are copied out now.
            seen.append({"index": frame.index, "kind": frame.kind, "giant": "11" in zombies,
                         "giant_fields": dict(zombies.get("11", {}).get("fields", {})),
                         "plant": "fields" in plants.get("65", {}),
                         "plant_fields": dict(plants.get("65", {}).get("fields", {}))})
        self.assertEqual([(frame["index"], frame["kind"]) for frame in seen],
                         [(index, "pre_step" if index % 2 == 0 else "post_step") for index in range(9)])
        self.assertEqual([(frame["giant"], frame["plant"]) for frame in seen],
                         [(False, False), (True, False), (True, True), (True, True), (True, True),
                          (True, True), (True, True), (True, True), (True, True)])
        self.assertEqual(seen[3]["giant_fields"]["00000028"], 70)
        self.assertEqual(seen[4]["plant_fields"]["00000142"], 1)
        self.assertEqual(seen[6]["plant_fields"]["00000141"], 1)
        self.assertNotIn("00000142", seen[6]["plant_fields"])          # removed op frees the flag
        self.assertEqual(seen[7]["giant_fields"]["00000034"], RESAMPLED_VELX)

    def test_replay_rejects_a_missing_initial_state(self):
        with self.assertRaises(Exception):
            list(giant_fork_diff.replay(iter([frame_record(1, "pre_step", 1, patch=[])])))

    def test_float32_matches_the_field_convention(self):
        self.assertAlmostEqual(giant_fork_diff.float32(SPAWN_VELX), 0.294958293, places=9)
        self.assertAlmostEqual(giant_fork_diff.float32(RESAMPLED_VELX), 0.283837438, places=9)
        self.assertEqual(giant_fork_diff.hex_word(RESAMPLED_VELX), "0x3e915324")


class MTRandTest(unittest.TestCase):
    def test_mt_draws_match_the_published_mt19937_vector(self):
        # mt19937ar(5489) first five outputs, masked the way NextNoAssert() does.
        words = [5489]
        for index in range(1, 624):
            words.append((1812433253 * (words[index - 1] ^ (words[index - 1] >> 30)) + index) & 0xFFFFFFFF)
        self.assertEqual(giant_fork_diff.mt_draws(words, 624, 5),
                         [value & 0x7FFFFFFF for value in (3499211612, 581869302, 3890346734,
                                                           3586334585, 545404204)])

    def test_rand_range_float_formula_matches_the_engine(self):
        span = giant_fork_diff.round32(0.37) - giant_fork_diff.round32(0.23)
        self.assertEqual(giant_fork_diff.round32(span), giant_fork_diff.round32(0.14))
        self.assertEqual(giant_fork_diff.velocity_from_draw(0), giant_fork_diff.round32(0.23))
        self.assertEqual(giant_fork_diff.velocity_from_draw(0x7FFFFFFF), giant_fork_diff.round32(0.37))
        # The drawn value stays inside the float32 bounds the engine passes in.
        self.assertTrue(0.23 < giant_fork_diff.velocity_from_draw(RESAMPLE_DRAW) < 0.37)

    def test_crafted_state_reproduces_the_observed_resample_bits(self):
        self.assertEqual(giant_fork_diff.mt_draws(MT_WORDS, MT_CURSOR, 1)[0], RESAMPLE_DRAW)
        self.assertEqual(bits_of(giant_fork_diff.velocity_from_draw(RESAMPLE_DRAW)), RESAMPLED_VELX)
        self.assertAlmostEqual(giant_fork_diff.velocity_from_draw(RESAMPLE_DRAW), 0.283837438, places=9)

    def test_match_velocity_draw_finds_the_draw_index(self):
        snapshot = {"words": list(MT_WORDS), "cursor": MT_CURSOR}
        self.assertEqual(giant_fork_diff.match_velocity_draw(snapshot, RESAMPLED_VELX)["draw_index"], 0)
        self.assertEqual(giant_fork_diff.match_velocity_draw(snapshot, SPAWN_VELX)["draw_index"], None)
        self.assertIsNone(giant_fork_diff.match_velocity_draw(None, RESAMPLED_VELX))


class GiantTest(unittest.TestCase):
    def test_only_type_23_is_selected(self):
        state = base_state()
        self.assertEqual(sorted(giant_fork_diff.giants(state)), ["11"])
        state["zombies"]["slots"]["0"]["fields"]["00000024"] = 23
        self.assertEqual(sorted(giant_fork_diff.giants(state)), ["0", "11"])
        state["zombies"]["slots"]["11"]["fields"]["00000024"] = 4
        self.assertEqual(sorted(giant_fork_diff.giants(state)), ["0"])

    def test_analysis_reports_the_post_smash_resample_bits(self):
        with tempfile.TemporaryDirectory() as directory:
            write_evidence(directory, synthetic_stream())
            analysis = giant_fork_diff.analyze(directory, plant_type=8)
        self.assertEqual(analysis.giant["slot"], "11")
        self.assertEqual(analysis.giant["animation"], BODY)
        self.assertEqual(analysis.giants["11"]["id"], 0x00DF000B)
        self.assertEqual([(change["tick"], change["kind"], change["event"])
                          for change in analysis.giant["changes"]],
                         [(1, "post_step", "appear"), (2, "post_step", "change"), (4, "post_step", "change")])
        resample = analysis.giant["changes"][-1]
        self.assertEqual(resample["velx_bits"], RESAMPLED_VELX)
        self.assertEqual(resample["phase"], 0)
        self.assertEqual(resample["mt_draw"]["draw_index"], 0)
        self.assertEqual(resample["mt_draw"]["matches"], [0])
        self.assertEqual(resample["mt_draw"]["cursor_before"], MT_CURSOR)
        self.assertNotIn("mt_draw", analysis.giant["changes"][0])       # spawn speed is not a resample
        self.assertEqual(analysis.giant["last"]["velx_bits"], RESAMPLED_VELX)
        loop_events = [(event["tick"], event["event"]) for event in analysis.animation_events[BODY]]
        self.assertIn((3, "loop_count_changed"), loop_events)
        self.assertIn((4, "walk_animation_resumed"), loop_events)
        self.assertIn((2, "play_once_and_hold_start"), loop_events)
        plant = analysis.plants["65"]
        self.assertEqual((plant["first"]["tick"], plant["first"]["row"], plant["first"]["col"]), (1, 5, 6))
        self.assertEqual([change["crushed"] for change in plant["changes"]], [0, 1, None])

    def test_analysis_reads_a_compressed_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            write_evidence(directory, synthetic_stream(), compress=True)
            self.assertTrue((Path(directory) / "audit" / "state-deltas.jsonl.gz").is_file())
            analysis = giant_fork_diff.analyze(directory)
        self.assertEqual(analysis.frames, 9)
        self.assertTrue(str(analysis.evidence).endswith("state-deltas.jsonl.gz"))


class DivergenceTest(unittest.TestCase):
    def test_identical_streams_have_no_divergence(self):
        with tempfile.TemporaryDirectory() as left, tempfile.TemporaryDirectory() as right:
            write_evidence(left, synthetic_stream(), compress=True)
            write_evidence(right, synthetic_stream(), compress=True)
            digest_scan = giant_fork_diff.compare_digests(left, right)
            exact = giant_fork_diff.compare_states(left, right)
        self.assertEqual(digest_scan["compared"], 9)
        self.assertIsNone(digest_scan["state"])
        self.assertIsNone(digest_scan["envelope"])
        self.assertEqual(exact["compared"], 9)
        self.assertIsNone(exact["state"])

    def test_extra_plant_is_reported_with_its_pointer(self):
        branch = synthetic_stream()
        branch[2]["patch"] = branch[2]["patch"] + [
            {"op": "add", "path": "/plants/slots/64", "value": plant_slot(8, 5, 5)}]
        with tempfile.TemporaryDirectory() as left, tempfile.TemporaryDirectory() as right:
            write_evidence(left, synthetic_stream(), compress=True)
            write_evidence(right, branch, compress=True)
            digest_scan = giant_fork_diff.compare_digests(left, right)
            exact = giant_fork_diff.compare_states(left, right)
            confirmed = giant_fork_diff.confirm_digest_difference(
                left, right, digest_scan["state"]["a"]["index"])
        self.assertEqual(digest_scan["state"]["sections"], ["all", "plants"])
        self.assertEqual(exact["state"]["path"], "/plants/slots/64")
        self.assertEqual(confirmed["path"], "/plants/slots/64")

    def test_resampled_speed_difference_is_reported(self):
        branch = synthetic_stream()
        branch[7]["patch"] = branch[7]["patch"] + [
            {"op": "replace", "path": "/zombies/slots/11/fields/00000034", "value": 0x3E8F5C29}]
        with tempfile.TemporaryDirectory() as left, tempfile.TemporaryDirectory() as right:
            write_evidence(left, synthetic_stream())
            write_evidence(right, branch)
            exact = giant_fork_diff.compare_states(left, right)
        self.assertEqual(exact["state"]["b"]["tick"], 4)
        self.assertEqual(exact["state"]["path"], "/zombies/slots/11/fields/00000034")
        self.assertEqual(exact["state"]["difference"]["expected"], RESAMPLED_VELX)
        self.assertEqual(exact["state"]["difference"]["actual"], 0x3E8F5C29)

    def test_envelope_shift_without_state_change_is_separated(self):
        branch = synthetic_stream()
        branch[2]["seq"] += 3           # a rejected action still burns sequence numbers
        with tempfile.TemporaryDirectory() as left, tempfile.TemporaryDirectory() as right:
            write_evidence(left, synthetic_stream())
            write_evidence(right, branch)
            digest_scan = giant_fork_diff.compare_digests(left, right)
            exact = giant_fork_diff.compare_states(left, right)
        self.assertEqual(digest_scan["envelope"]["a"]["seq"], 22)
        self.assertEqual(digest_scan["envelope"]["b"]["seq"], 25)
        self.assertEqual(digest_scan["envelope"]["a"]["tick"], 1)
        self.assertIsNone(digest_scan["state"])
        self.assertIsNone(exact["state"])


class VerifyTest(unittest.TestCase):
    def test_digest_cross_check_matches_the_recorded_checksums(self):
        with tempfile.TemporaryDirectory() as directory:
            write_evidence(directory, synthetic_stream(), compress=True)
            analysis = giant_fork_diff.analyze(directory, verify=True)
        self.assertTrue(analysis.verify["available"])
        self.assertEqual(analysis.verify["checked"], 9)
        self.assertEqual(analysis.verify["mismatches"], [])
        self.assertEqual(analysis.verify["trailing_records"], 0)

    def test_digest_cross_check_detects_a_tampered_state(self):
        with tempfile.TemporaryDirectory() as directory:
            write_evidence(directory, synthetic_stream())
            path = Path(directory) / "audit" / "state-deltas.jsonl"
            rows = path.read_text(encoding="utf-8").splitlines()
            rows[3] = rows[3].replace('"value": 70}', '"value": 71}')   # diverges from checksums.jsonl
            path.write_text("\n".join(rows) + "\n", encoding="utf-8")
            analysis = giant_fork_diff.analyze(directory, verify=True)
        self.assertGreaterEqual(len(analysis.verify["mismatches"]), 1)
        first = analysis.verify["mismatches"][0]
        self.assertEqual(first["tick"], 2)               # the tampered patch lands at post_step(2)
        self.assertIn("all", first["sections"])
        self.assertIn("zombies", first["sections"])


class EvidenceTest(unittest.TestCase):
    def test_state_deltas_can_be_opened_directly_or_as_a_run_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            write_evidence(directory, synthetic_stream(), compress=True)
            direct = Path(directory) / "audit" / "state-deltas.jsonl.gz"
            direct_path, direct_plain = giant_fork_diff.resolve_state_deltas(direct)
            run_path, run_plain = giant_fork_diff.resolve_state_deltas(directory)
            audit_path, audit_plain = giant_fork_diff.resolve_state_deltas(Path(directory) / "audit")
        self.assertEqual(direct_path, direct)
        self.assertIsNone(direct_plain)
        self.assertEqual(run_path, direct)
        self.assertEqual(audit_path, direct)
        self.assertEqual(len(run_plain), 64)
        self.assertEqual(run_plain, audit_plain)

    def test_gzip_container_round_trips_the_records(self):
        import gzip
        with tempfile.TemporaryDirectory() as directory:
            write_evidence(directory, synthetic_stream(), compress=True)
            with gzip.open(Path(directory) / "audit" / "state-deltas.jsonl.gz", "rt", encoding="utf-8") as stream:
                rows = [json.loads(line) for line in stream]
        self.assertEqual(rows, synthetic_stream())


if __name__ == "__main__":
    unittest.main()
