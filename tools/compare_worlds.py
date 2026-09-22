"""Read-only offline diff of two recorded worlds (evaluation runs).

This tool never launches a game and never writes inside a run directory. It
consumes a run's sealed ``audit/`` evidence through ``EvidenceStore`` (which
also resolves gzip containers) and answers three separate questions that must
not be conflated:

* *within-world* reproducibility: source run versus its cold replay;
* *cross-world* consistency: world C versus world D at the same boundary;
* where the first differences actually are, as canonical JSON Pointers.

``checksums.jsonl`` carries both component digests and the audit ``payload``.
The plain bytes therefore also change when only a request-id label changes, so
the digest columns and the plain SHA-256 are reported separately instead of
being collapsed into one "equal" flag.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from llm_vs_zombies.audit_compare import patch
from llm_vs_zombies.evidence_codec import EvidenceStore

STATE_FILE = "state-deltas.jsonl"
CHECKSUM_FILE = "checksums.jsonl"

# Zombie fields used by the dance-phase clock check. Offsets are the struct
# offsets the audit writes as keys; names come from the AvZ layout the project
# follows (``mZombieType`` +0x24, ``mZombiePhase`` +0x28).
ZOMBIE_TYPE = "00000024"
ZOMBIE_PHASE = "00000028"
DANCER_TYPES = {8, 9}          # ZOMBIE_DANCER, ZOMBIE_BACKUP_DANCER
DANCE_FRAME_LENGTH = 20
DANCE_FRAME_COUNT = 23         # period 460 ticks
DANCE_PERIOD = DANCE_FRAME_LENGTH * DANCE_FRAME_COUNT
DANCE_PHASES = {44: "PHASE_DANCER_DANCING_LEFT", 45: "PHASE_DANCER_WALK_TO_RAISE",
                46: "PHASE_DANCER_RAISE_LEFT_1", 47: "PHASE_DANCER_RAISE_RIGHT_1",
                48: "PHASE_DANCER_RAISE_LEFT_2", 49: "PHASE_DANCER_RAISE_RIGHT_2"}
# ZombiePhaseWanted(aFrame) from the candidate decompilation: frames 0..11 keep
# DANCING_LEFT, 12 is the single-frame WALK_TO_RAISE, then two-frame steps.
DANCE_PHASE_BY_FRAME = ((11, 44), (12, 45), (15, 47), (18, 46), (21, 49), (23, 48))


def dance_frame(clock: int) -> int:
    """``(mAppCounter % (20 * 23)) / 20`` as the candidate decompilation states."""
    return (clock % DANCE_PERIOD) // DANCE_FRAME_LENGTH


def dance_phase(clock: int) -> int:
    frame = dance_frame(clock)
    for limit, phase in DANCE_PHASE_BY_FRAME:
        if frame <= limit:
            return phase
    return 48


def read_rows(store: EvidenceStore, name: str):
    """Yield one decoded JSON object per line, refusing duplicate keys."""
    with store.open(name) as stream:
        for number, raw in enumerate(stream, 1):
            text = raw.decode("utf-8")
            if not text.endswith("\n"):
                raise ValueError(f"{name}: line {number} is not newline terminated")
            yield json.loads(text, object_pairs_hook=_unique_pairs)


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def iter_states(store: EvidenceStore, name: str = STATE_FILE, *, in_place: bool = False):
    """Replay the JSON Patch stream, yielding one fully expanded state."""
    state = None
    for index, row in enumerate(read_rows(store, name)):
        if "initial" in row:
            if state is not None:
                raise ValueError("more than one initial state")
            state = row["initial"]
        else:
            if state is None:
                raise ValueError("patch precedes the initial state")
            state = patch(state, row["patch"], in_place=in_place)
        yield {"index": index, "row": row, "state": state}


MISSING = object()


def value_at(document, pointer: str):
    """Resolve a JSON Pointer, returning MISSING when it is not present."""
    if pointer == "":
        return document
    node = document
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict):
            if token not in node:
                return MISSING
            node = node[token]
        elif isinstance(node, list):
            if not token.isdigit() or int(token) >= len(node):
                return MISSING
            node = node[int(token)]
        else:
            return MISSING
    return node


def refresh(differences: dict, left, right, path: str) -> None:
    """Recompute one subtree of the difference set after a patch touched it."""
    for key in [name for name in differences if name == path or name.startswith(path + "/")]:
        del differences[key]
    a, b = value_at(left, path), value_at(right, path)
    if a is MISSING and b is MISSING:
        return
    if a is MISSING or b is MISSING:
        differences[path] = {"path": path, "a": None if a is MISSING else a,
                             "b": None if b is MISSING else b, "reason": "missing_field",
                             "missing": "a" if a is MISSING else "b"}
        return
    for item in all_differences(a, b, path):
        differences[item["path"]] = item


def all_differences(a, b, path=""):
    """Every leaf difference between two expanded states as JSON Pointers."""
    if type(a) is not type(b):
        yield {"path": path, "a": a, "b": b, "reason": "type"}
        return
    if isinstance(a, dict):
        for key in sorted(set(a) | set(b)):
            child = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in a or key not in b:
                yield {"path": child, "a": a.get(key), "b": b.get(key), "reason": "missing_field"}
            else:
                yield from all_differences(a[key], b[key], child)
    elif isinstance(a, list):
        for index, (left, right) in enumerate(zip(a, b)):
            yield from all_differences(left, right, f"{path}/{index}")
        if len(a) != len(b):
            yield {"path": path, "a": len(a), "b": len(b), "reason": "array_length"}
    elif a != b:
        yield {"path": path, "a": a, "b": b, "reason": "value"}


def plain_sha256(store: EvidenceStore, name: str) -> str:
    """Digest of the decompressed bytes, verified against the codec receipt."""
    entry = store.entry(name + ".gz")
    if entry is not None:
        return store.plain_sha256(name + ".gz")
    digest = hashlib.sha256()
    with store.open(name) as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def digest_columns(row: dict) -> dict:
    return row.get("digests", {})


def label_columns(row: dict) -> dict:
    """Everything in a checksum row that is not a component digest."""
    return {key: value for key, value in row.items() if key != "digests"}


def compare_checksums(store_a: EvidenceStore, store_b: EvidenceStore, *, limit: int = 20):
    rows_a, rows_b = list(read_rows(store_a, CHECKSUM_FILE)), list(read_rows(store_b, CHECKSUM_FILE))
    result = {"a_lines": len(rows_a), "b_lines": len(rows_b),
              "plain_sha256_a": plain_sha256(store_a, CHECKSUM_FILE),
              "plain_sha256_b": plain_sha256(store_b, CHECKSUM_FILE),
              "plain_equal": None, "stored_sha256_a": store_a.stored_sha256(CHECKSUM_FILE + ".gz"),
              "stored_sha256_b": store_b.stored_sha256(CHECKSUM_FILE + ".gz"),
              "first_digest_difference": None, "digest_difference_count": 0,
              "first_label_difference": None, "label_difference_count": 0, "label_differences": []}
    result["plain_equal"] = result["plain_sha256_a"] == result["plain_sha256_b"]
    for index, (left, right) in enumerate(zip(rows_a, rows_b)):
        if digest_columns(left) != digest_columns(right):
            result["digest_difference_count"] += 1
            if result["first_digest_difference"] is None:
                names = sorted({key for key in set(digest_columns(left)) | set(digest_columns(right))
                                if digest_columns(left).get(key) != digest_columns(right).get(key)})
                result["first_digest_difference"] = {
                    "line": index, "seq": left.get("seq"), "kind": left.get("kind"),
                    "version": left.get("version"),
                    "components": {name: {"a": digest_columns(left).get(name), "b": digest_columns(right).get(name)}
                                   for name in names}}
        if label_columns(left) != label_columns(right):
            result["label_difference_count"] += 1
            if len(result["label_differences"]) < limit:
                first = next(iter(all_differences(label_columns(left), label_columns(right))))
                result["label_differences"].append({"line": index, "seq": left.get("seq"),
                                                   "kind": left.get("kind"), "pointer": first["path"],
                                                   "a": first["a"], "b": first["b"]})
            if result["first_label_difference"] is None:
                result["first_label_difference"] = result["label_differences"][0]
    return result


def _group(pointer: str) -> str:
    parts = pointer.split("/")
    if pointer.startswith("/reanimations/nodes/") and len(parts) > 4:
        return "/".join(parts[:4])
    if pointer.startswith("/zombies/slots/") and len(parts) > 4:
        return "/".join(parts[:4])
    return "/".join(parts[:3]) if len(parts) > 3 else pointer


def compare_states(store_a: EvidenceStore, store_b: EvidenceStore, *, baseline: int = 0, limit: int = 20,
                   verify_every: int = 0):
    """Diff every expanded boundary; separate the B(baseline) set from later ones.

    Only the subtrees a JSON Patch actually touched are recomputed, so a full
    4,000-boundary walk stays linear in the patch volume instead of re-diffing
    the whole state. ``verify_every`` cross-checks that shortcut against a
    brute-force diff of the complete state on every Nth boundary.
    """
    result = {"boundaries": 0, "baseline": None, "first_difference": None,
              "first_divergence_beyond_baseline": None, "boundaries_with_extra_differences": 0,
              "boundaries_with_any_difference": 0, "later_samples": [], "verification": {"checked": 0,
              "mismatches": []}, "extra_groups": {}}
    differences: dict[str, dict] = {}
    base_pointers: dict[str, dict] = {}
    left_stream = iter_states(store_a, in_place=True)
    right_stream = iter_states(store_b, in_place=True)
    for index, (left, right) in enumerate(zip(left_stream, right_stream)):
        result["boundaries"] += 1
        if index == 0:
            differences = {item["path"]: item for item in all_differences(left["state"], right["state"])}
        else:
            touched = sorted(set(refresh_paths(left["row"].get("patch", []), left["state"], right["state"]))
                             | set(refresh_paths(right["row"].get("patch", []), left["state"], right["state"])))
            for path in touched:
                refresh(differences, left["state"], right["state"], path)
        if verify_every and index % verify_every == 0:
            brute = {item["path"]: item for item in all_differences(left["state"], right["state"])}
            result["verification"]["checked"] += 1
            if set(brute) != set(differences):
                result["verification"]["mismatches"].append(
                    {"index": index, "only_brute": sorted(set(brute) - set(differences))[:8],
                     "only_incremental": sorted(set(differences) - set(brute))[:8]})
        if not differences:
            continue
        result["boundaries_with_any_difference"] += 1
        summary = {"index": index, "seq": left["row"].get("seq"), "kind": left["row"].get("kind"),
                   "version": left["row"].get("version"),
                   "pointers": [{"path": item["path"], "a": item["a"], "b": item["b"]}
                                for item in sorted(differences.values(), key=lambda value: value["path"])]}
        if result["first_difference"] is None:
            result["first_difference"] = summary
        if index == baseline:
            base_pointers = dict(differences)
            result["baseline"] = summary
            continue
        extra = {name: item for name, item in differences.items() if name not in base_pointers}
        if extra:
            result["boundaries_with_extra_differences"] += 1
            for item in extra.values():
                group = _group(item["path"])
                entry = result["extra_groups"].setdefault(
                    group, {"group": group, "boundaries": 0, "first_index": index, "first_version": None,
                            "last_index": index, "pointers": set()})
                entry["boundaries"] += 1
                entry["last_index"] = index
                entry["pointers"].add(item["path"])
                if entry["first_version"] is None:
                    entry["first_version"] = left["row"].get("version")
            if result["first_divergence_beyond_baseline"] is None:
                result["first_divergence_beyond_baseline"] = {
                    "index": index, "seq": left["row"].get("seq"), "kind": left["row"].get("kind"),
                    "version": left["row"].get("version"),
                    "pointer_count": len(extra),
                    "groups": sorted({_group(item["path"]) for item in extra.values()}),
                    "pointers": [{"path": item["path"], "a": item["a"], "b": item["b"]}
                                 for item in sorted(extra.values(), key=lambda value: value["path"])]}
        if len(result["later_samples"]) < limit:
            result["later_samples"].append({"index": index, "seq": left["row"].get("seq"),
                                           "kind": left["row"].get("kind"),
                                           "groups": sorted({_group(name) for name in differences}),
                                           "pointer_count": len(differences)})
    if base_pointers:
        result["baseline_pointer_count"] = len(base_pointers)
        result["later_only_baseline_pointers"] = result["boundaries_with_extra_differences"] == 0
    for entry in result["extra_groups"].values():
        entry["pointer_count"] = len(entry["pointers"])
        entry["pointers"] = sorted(entry["pointers"])[:50]
    return result


def world(path: Path) -> dict:
    store = EvidenceStore(Path(path) / "audit")
    return {"path": str(Path(path)), "compressed": store.compressed,
            "audit_files": sorted(store.entries)}


def refresh_paths(operations, left, right):
    """Paths whose subtree must be re-diffed after one side's patch list.

    An ``add``/``remove`` inside a JSON array shifts every later element, so the
    whole parent array is re-diffed instead of only the touched index.
    """
    paths = set()
    for operation in operations:
        path = operation["path"]
        if path == "":
            paths.add("")
            continue
        parent = path.rsplit("/", 1)[0]
        if isinstance(value_at(left, parent), list) or isinstance(value_at(right, parent), list):
            paths.add(parent)
        else:
            paths.add(path)
    return sorted(paths)


def within_world(run_a: Path, run_b: Path, *, limit: int = 20, verify_every: int = 0) -> dict:
    store_a, store_b = EvidenceStore(run_a / "audit"), EvidenceStore(run_b / "audit")
    return {"a": world(run_a), "b": world(run_b),
            "checksums": compare_checksums(store_a, store_b, limit=limit),
            "states": compare_states(store_a, store_b, limit=limit, verify_every=verify_every)}


def dancer_transitions(store: EvidenceStore, *, limit: int = 40):
    """Every dance/backup-dance phase change with the app counter that caused it."""
    previous: dict[str, int] = {}
    events, mismatches = [], []
    observed = matched = 0
    for item in iter_states(store, in_place=True):
        clock = item["state"]["app"]["mj_clock"]
        version = item["row"].get("version") or {}
        for slot, entry in item["state"]["zombies"]["slots"].items():
            fields = entry.get("fields") or {}      # free pool slots carry only a free-list link
            if fields.get(ZOMBIE_TYPE) not in DANCER_TYPES:
                continue
            phase = fields.get(ZOMBIE_PHASE)
            if phase in DANCE_PHASES:
                observed += 1
                if dance_phase(clock) == phase:
                    matched += 1
                elif len(mismatches) < limit:
                    # Immobilised zombies report frame 0; everything else should follow the clock.
                    mismatches.append({"slot": slot, "tick": version.get("tick"), "clock": clock,
                                       "expected": dance_phase(clock), "actual": phase,
                                       "actual_name": DANCE_PHASES.get(phase, str(phase))})
            if previous.get(slot) != phase:
                events.append({"slot": slot, "index": item["index"], "tick": version.get("tick"),
                               "kind": item["row"].get("kind"), "clock": clock, "frame": dance_frame(clock),
                               "from": previous.get(slot), "to": phase,
                               "name": DANCE_PHASES.get(phase, str(phase))})
                previous[slot] = phase
    steps = [item for item in events if item["to"] in DANCE_PHASES and item["from"] in DANCE_PHASES]
    starts = [item for item in events if item["to"] in DANCE_PHASES and item["from"] not in DANCE_PHASES]
    return {"cycle_observations": observed, "cycle_matches": matched,
            "cycle_mismatch_count": observed - matched,
            "cycle_step_transitions": len(steps), "cycle_start_transitions": len(starts),
            "cycle_step_counters": sorted({item["clock"] for item in steps}),
            "cycle_start_counters": sorted({item["clock"] for item in starts}),
            "cycle_step_ticks": sorted({item["tick"] for item in steps}),
            "step_counters_on_frame_boundary": all(item["clock"] % DANCE_FRAME_LENGTH == 0 for item in steps),
            "step_phases_predicted": all(dance_phase(item["clock"]) == item["to"] for item in steps),
            "samples": [item for item in steps if item["slot"] in ("8", "51")][:limit],
            "phase_mismatches": mismatches[:limit]}


def dancer_clock(run_a: Path, run_b: Path, *, limit: int = 40) -> dict:
    result = {"a": str(run_a), "b": str(run_b), "period": DANCE_PERIOD,
              "frame_length": DANCE_FRAME_LENGTH}
    for key, run in (("a", run_a), ("b", run_b)):
        result[key + "_dancers"] = dancer_transitions(EvidenceStore(run / "audit"), limit=limit)
    counters_a = result["a_dancers"]["cycle_step_counters"]
    counters_b = result["b_dancers"]["cycle_step_counters"]
    result["step_counters_identical"] = counters_a == counters_b
    offset = None
    ticks_a, ticks_b = result["a_dancers"]["cycle_step_ticks"], result["b_dancers"]["cycle_step_ticks"]
    if ticks_a and ticks_b:
        offset = ticks_b[0] - ticks_a[0]
    result["first_step_tick_offset_b_minus_a"] = offset
    if offset is not None:
        shifted = {tick - offset for tick in ticks_b}
        result["step_ticks_equal_after_offset"] = shifted == set(ticks_a)
        result["step_ticks_only_a"] = sorted(set(ticks_a) - shifted)[:10]
        result["step_ticks_only_b_shifted"] = sorted(shifted - set(ticks_a))[:10]
    return result


def self_test() -> int:
    checks = []

    def check(name, condition):
        checks.append((name, bool(condition)))

    document = {"app": {"ui": 3, "counts": [1, 2]}, "board": {"x": 1}}
    check("patch replace", patch(document, [{"op": "replace", "path": "/app/ui", "value": 4}])["app"]["ui"] == 4)
    check("patch add", patch(document, [{"op": "add", "path": "/app/k", "value": 9}])["app"]["k"] == 9)
    check("patch remove", "k" not in patch(document, [{"op": "remove", "path": "/board/x"}])["board"])
    check("patch escapes pointer", patch({"a/b": 1}, [{"op": "replace", "path": "/a~1b", "value": 2}])["a/b"] == 2)

    left = {"app": {"ui": 3, "mj_clock": 1}, "sound_effects": {"calls": 5}}
    right = {"app": {"ui": 3, "mj_clock": 2}, "sound_effects": {"calls": 6}}
    found = sorted(item["path"] for item in all_differences(left, right))
    check("diff finds both leaves", found == ["/app/mj_clock", "/sound_effects/calls"])
    check("diff finds no difference", list(all_differences(left, left)) == [])

    missing = list(all_differences({"a": 1}, {"b": 1}))
    check("diff reports missing field", missing[0]["reason"] == "missing_field")
    check("diff reports array length", any(item["reason"] == "array_length"
                                           for item in all_differences({"a": [1]}, {"a": [1, 2]})))
    check("diff respects types", list(all_differences({"a": 1}, {"a": "1"}))[0]["reason"] == "type")

    document = {"app": {"ui": 3, "counts": [1, 2]}, "board": {"x": 1}}
    check("patch does not mutate input", document["app"]["ui"] == 3)

    # Incremental diff must agree with a brute-force diff of the whole state.
    left = {"app": {"clock": 1}, "zombies": {"slots": {"0": {"fields": {"00000028": 44}}}}}
    right = {"app": {"clock": 34}, "zombies": {"slots": {"0": {"fields": {"00000028": 44}}}}}
    differences = {item["path"]: item for item in all_differences(left, right)}
    touched = [{"op": "replace", "path": "/zombies/slots/0/fields/00000028", "value": 45}]
    right = patch(right, touched)
    for path in refresh_paths(touched, left, right):
        refresh(differences, left, right, path)
    brute = {item["path"]: item for item in all_differences(left, right)}
    check("incremental diff matches brute force", set(differences) == set(brute))
    check("incremental diff keeps the new leaf", "/zombies/slots/0/fields/00000028" in differences)
    check("array insert refreshes the whole array",
          refresh_paths([{"op": "add", "path": "/counts/-", "value": 3}],
                        {"counts": [1, 2]}, {"counts": [1, 2, 3]}) == ["/counts"])

    check("dance frame is the app counter modulo 460 over 20", dance_frame(2540) == 12)
    check("frame 12 is PHASE_DANCER_WALK_TO_RAISE", dance_phase(2540) == 45)
    check("frame 0 is PHASE_DANCER_DANCING_LEFT", dance_phase(2300) == 44)
    check("one tick before the boundary still dances left", dance_phase(2539) == 44)

    import tempfile
    from llm_vs_zombies.evidence_codec import compress_evidence

    def synthetic(root: Path, clock: int, request: str):
        audit = root / "audit"
        audit.mkdir(parents=True)
        state = {"app": {"game_mode": 13, "mj_clock": clock, "ui": 3},
                 "zombies": {"slots": {"0": {"fields": {"00000028": 44}}}}}
        rows = [{"kind": "pre_step", "seq": 21, "version": {"epoch": 3, "revision": 5, "tick": 0}, "initial": state}]
        for tick in (1, 2):
            rows.append({"kind": "post_step", "seq": 21 + tick * 4,
                         "version": {"epoch": 3, "revision": 0, "tick": tick},
                         "patch": [{"op": "replace", "path": "/app/mj_clock", "value": clock + tick}]})
        (audit / STATE_FILE).write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
        (audit / CHECKSUM_FILE).write_text("".join(json.dumps(
            {"seq": 21 + index, "kind": "post_step", "version": {"epoch": 3, "revision": 0, "tick": index},
             "digests": {"app": "a" * 16, "all": "b" * 16}, "payload": {"request_id": request}}
        ) + "\n" for index in range(3)), encoding="utf-8")
        compress_evidence(audit)
        return root

    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        same = within_world(synthetic(base / "same-a", 100, "r1"), synthetic(base / "same-b", 100, "r2"))
        cross = within_world(synthetic(base / "cross-a", 100, "r1"), synthetic(base / "cross-b", 133, "r1"))
        check("identical worlds have identical digests", same["checksums"]["digest_difference_count"] == 0)
        check("identical worlds differ only in the request label",
              same["checksums"]["first_label_difference"]["pointer"] == "/payload/request_id")
        check("identical worlds still differ in plain bytes", same["checksums"]["plain_equal"] is False)
        check("identical worlds have no state difference", same["states"]["boundaries_with_any_difference"] == 0)
        baseline = cross["states"]["baseline"]["pointers"]
        check("cross-world B0 difference is the app clock",
              [item["path"] for item in baseline] == ["/app/mj_clock"])
        check("cross-world baseline keeps both raw values",
              [item["a"] for item in baseline] == [100] and [item["b"] for item in baseline] == [133])
        check("cross-world without new divergence reports none",
              cross["states"]["first_divergence_beyond_baseline"] is None
              and cross["states"]["later_only_baseline_pointers"] is True)

    failures = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(("ok   " if ok else "FAIL ") + name)
    print(f"{len(checks) - len(failures)}/{len(checks)} checks passed")
    return 1 if failures else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--a", type=Path, help="first run directory (world A)")
    parser.add_argument("--b", type=Path, help="second run directory (world B)")
    parser.add_argument("--limit", type=int, default=20, help="samples to retain in the report")
    parser.add_argument("--verify-every", type=int, default=0,
                        help="brute-force re-diff the whole state every Nth boundary as a cross-check")
    parser.add_argument("--json", type=Path, help="write the full report here")
    parser.add_argument("--self-test", action="store_true", help="run the offline self test and exit")
    parser.add_argument("--dancer-clock", action="store_true",
                        help="check the dance-phase clock mapping instead of the full diff")
    options = parser.parse_args(argv)
    if options.self_test:
        return self_test()
    if not options.a or not options.b:
        parser.error("--a and --b are required unless --self-test is used")
    if options.dancer_clock:
        report = dancer_clock(options.a, options.b, limit=options.limit)
    else:
        report = within_world(options.a, options.b, limit=options.limit, verify_every=options.verify_every)
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if options.json:
        options.json.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
