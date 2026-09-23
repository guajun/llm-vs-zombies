"""Recipe completeness: two-way set difference over captured B(0) leaves.

Every leaf of the captured B(0) state must be claimed by exactly one of five
classes -- declared, recorded, derived, the normalization table, or explicitly
uncovered -- and every claim must land on a real leaf. These are two set
differences:

* holes ``H = U \\ C``: the state really holds the leaf, no class claims it;
* dangling ``D = C \\ U``: something claims a leaf this B(0) does not have,
  which is normally a typo or a version mismatch.

Any nonempty difference is a strict evidence failure, never a warning. The
check understands only the captured state: ``complete_game_state=false`` is
unchanged, and a clean report means "no unclassified field **inside the
captured state**", not that the capture is a complete game state.

Claims carry their own provenance. A ``derived`` claim without a recompute or
mapping implementation location is a hole by definition, and an ``uncovered``
claim must name one of the runtime's own ``coverage.uncovered`` strings, so a
field that is really written cannot be laundered into "not covered".
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from .audit_compare import EvidenceError, read_json
from . import b0_normalization as b0

SCHEMA = "lvz.b0-coverage.v1"
CLASSES = ("declared", "recorded", "derived", "normalization", "uncovered")
SAMPLE_LIMIT = 50


def fail(message):
    raise EvidenceError("B(0) coverage: " + message)


def leaves(document, prefix: str = ""):
    """Every leaf of one captured state as a JSON Pointer.

    Objects and arrays expand to their scalars; an empty object or array is its
    own leaf. Array indices are decimal, and ``~``/``/`` are escaped exactly as
    in ``tools/compare_worlds.py`` (the same expansion the world comparison
    uses).
    """
    if isinstance(document, dict):
        if not document:
            yield prefix
            return
        for key in sorted(document):
            child = prefix + "/" + key.replace("~", "~0").replace("/", "~1")
            yield from leaves(document[key], child)
    elif isinstance(document, list):
        if not document:
            yield prefix
            return
        for index, item in enumerate(document):
            yield from leaves(item, f"{prefix}/{index}")
    else:
        yield prefix


def _segments(pointer: str) -> tuple[str, ...]:
    if pointer == "":
        return ()
    if not pointer.startswith("/"):
        fail(f"invalid JSON Pointer pattern: {pointer!r}")
    return tuple(part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/"))


def matches(pointer: str, pattern: str) -> bool:
    """A claim covers one leaf: ``*`` matches exactly one segment, and a pattern
    that matches a prefix covers the whole subtree below it (``/zombies/*``
    claims every pool leaf without enumerating hundreds of addresses)."""
    wanted, actual = _segments(pattern), _segments(pointer)
    return (len(wanted) <= len(actual)
            and all(expect == "*" or expect == value for expect, value in zip(wanted, actual)))


def covers_leaf(pointer: str, pattern: str) -> bool:
    """A claim also accounts for an optional structure that is empty or null.

    ``challenge`` may be captured as ``null`` (its own leaf pointer); a claim on
    ``/challenge/completed_rounds`` still accounts for that leaf, because there
    is nothing beneath it to classify.
    """
    if matches(pointer, pattern):
        return True
    wanted, actual = _segments(pattern), _segments(pointer)
    return (len(actual) < len(wanted)
            and all(expect == "*" or expect == value for expect, value in zip(wanted, actual)))


# The built-in claims are deliberately conservative: each one names the actual
# writer/reader/verifier that makes the field comparable across worlds. Fields
# this table does not claim are reported as holes with their real leaf count.
DEFAULT_CLAIMS = {
    "declared": [
        {"pattern": "/schema", "source": "audit_compare.py SCHEMA constant lvz.audit.v1"},
        {"pattern": "/rng/schema", "source": "native RNG capture schema (determinism/audit.cpp)"},
        {"pattern": "/rng/target", "source": "game target declared by the runtime identity"},
        {"pattern": "/rng/complete_game_rng", "source": "manifest rng_scope declaration"},
        {"pattern": "/rng/instances", "source": "rng_seed declares the actual seeded instances (initialization.verify_seeded_rng)"},
        {"pattern": "/fp_environment/x87_control", "source": "fixed_owner_fp_v1 constant 0x027f (fp_environment.py:13)"},
        {"pattern": "/fp_environment/mxcsr_control", "source": "fixed_owner_fp_v1 constant 0x1f80 (fp_environment.py:13)"},
        {"pattern": "/draw_schedule/mode", "source": "deterministic_draw_schedule_v1 declaration (initialization.py:241)"},
        {"pattern": "/draw_schedule/warm_frames", "source": "declared warm/step counters (initialization.py:262)"},
        {"pattern": "/draw_schedule/step_frames", "source": "declared warm/step counters (initialization.py:262)"},
        {"pattern": "/app/game_mode", "source": "scenario configuration (launcher.py:202)"},
        {"pattern": "/app/ui", "source": "paused ready fight requirement (launcher.py:277-279)"},
        {"pattern": "/particle_shake/mode", "source": "declared controlled particle-shake mode (audit_compare.particle_semantics)"},
    ],
    "recorded": [
        {"pattern": "/board/00005568", "source": "clock_restore writes the recorded GameClock (determinism/audit.cpp:415)"},
        {"pattern": "/board/0000556c", "source": "clock_restore writes the recorded EffectCounter (determinism/audit.cpp:415)"},
        {"pattern": "/board/00005594", "source": "mZombieHealthToNextWave round-trips with the save (docs/机制读取点表.json D2-3)"},
        {"pattern": "/board/00005598", "source": "mZombieHealthWaveStart round-trips with the save (docs/机制读取点表.json D2-3)"},
        {"pattern": "/board/0000559c", "source": "mZombieCountDown round-trips with the save (docs/机制读取点表.json D2-2)"},
        {"pattern": "/board/000055a0", "source": "mZombieCountDownStart round-trips with the save (docs/机制读取点表.json D2-2)"},
        {"pattern": "/challenge/completed_rounds", "source": "recorded round counter (determinism/audit.cpp:458)", "required": False},
        {"pattern": "/seeds/conveyor_counter", "source": "reference save layout (cli.py:29-33; records.py:59-78)", "required": False},
        {"pattern": "/seeds/count", "source": "card order mapping (launcher.py:277-289)"},
        {"pattern": "/seeds/slots/*", "source": "card order mapping (launcher.py:277-289)", "required": False},
        {"pattern": "/zombies/*", "source": "reference save pool layout; manifest coverage.pool_slot_and_free_list"},
        {"pattern": "/plants/*", "source": "reference save pool layout; manifest coverage.pool_slot_and_free_list"},
        {"pattern": "/projectiles/*", "source": "reference save pool layout; manifest coverage.pool_slot_and_free_list"},
        {"pattern": "/coins/*", "source": "reference save pool layout; manifest coverage.pool_slot_and_free_list"},
        {"pattern": "/mowers/*", "source": "reference save pool layout; manifest coverage.pool_slot_and_free_list"},
        {"pattern": "/grid_items/*", "source": "reference save pool layout; manifest coverage.pool_slot_and_free_list"},
        {"pattern": "/sound_effects/*", "source": "recipe binds the whole component as sound_effects.b0_state_sha256 (initialization.py:345)", "required": False},
    ],
    "derived": [
        {"pattern": "/sound_effects/calls", "source": "counter origin rebase (docs/engine-replay.md:281,287-289)",
         "implementation": "sound_counter.py + sound_effects.state(counter_scope)"},
        {"pattern": "/sound_effects/counter_scope", "source": "counter origin rebase (docs/engine-replay.md:281,287-289)",
         "implementation": "sound_counter.receipt", "required": False},
        {"pattern": "/particle_shake/controlled_calls", "source": "controlled digest recomputed from verified seeds",
         "implementation": "audit_compare._ParticleEvidence/_particle_digest", "required": False},
        {"pattern": "/particle_shake/controlled_digest", "source": "controlled digest recomputed from verified seeds",
         "implementation": "audit_compare._ParticleEvidence/_particle_digest", "required": False},
        {"pattern": "/reanimations/schema", "source": "semantic animation identity mapping (docs/engine-replay.md:209-213)",
         "implementation": "audit_compare._AnimationDecoder", "required": False},
        {"pattern": "/reanimations/nodes/*", "source": "semantic animation identity mapping (docs/engine-replay.md:209-213)",
         "implementation": "audit_compare._AnimationDecoder + normalized_reference lookup rules", "required": False},
        {"pattern": "/reanimations/valid", "source": "semantic animation identity mapping (docs/engine-replay.md:209-213)",
         "implementation": "audit_compare._AnimationDecoder", "required": False},
        {"pattern": "/reanimations/issues", "source": "semantic animation identity mapping (docs/engine-replay.md:209-213)",
         "implementation": "audit_compare._AnimationDecoder", "required": False},
    ],
    "normalization": [],
    "uncovered": [],
}

# Runtime-declared boundaries. Every entry must cite one exact string of the
# native manifest's ``coverage.uncovered`` list. Most of these boundaries name
# state the capture deliberately never records, so they claim zero leaves; that
# is recorded and auditable instead of being silently dropped.
UNCOVERED_MAP = [
    {"manifest": "UI/input state", "pattern": None},
    {"manifest": "particle/effect pools", "pattern": None},
    {"manifest": "Challenge state except completed rounds", "pattern": None},
    {"manifest": "reanimation allocations entirely between sampled boundaries", "pattern": None},
    {"manifest": "image/font/text override identity", "pattern": None},
    {"manifest": "attachment effect graphs", "pattern": None},
    {"manifest": "PottedPlant coin specification", "pattern": None},
    {"manifest": "grid motion trails", "pattern": None},
    {"manifest": "time-source interception", "pattern": None},
    {"manifest": "RNG calls/instances on other threads", "pattern": None},
    {"manifest": "MT instances on the stack between boundaries", "pattern": None},
    {"manifest": "unreferenced animations and attachment graphs", "pattern": None},
    {"manifest": "unreferenced animation semantics", "pattern": None},
    {"manifest": "particle/trail pools", "pattern": None},
]


def default_claims():
    return copy.deepcopy(DEFAULT_CLAIMS)


def validate_claims(value) -> dict:
    optional = {"boundaries", "scope"}
    if (not isinstance(value, dict) or not set(CLASSES) <= set(value)
            or set(value) - set(CLASSES) - optional):
        fail(f"a claim set requires exactly the classes {list(CLASSES)}")
    result = {name: [] for name in CLASSES}
    for key in optional & set(value):
        result[key] = copy.deepcopy(value[key])
    for name in CLASSES:
        if not isinstance(value[name], list):
            fail(f"{name} claims must be an array")
        for claim in value[name]:
            if not isinstance(claim, dict) or not isinstance(claim.get("pattern"), str) or not claim["pattern"].startswith("/"):
                fail(f"{name} claim requires a JSON Pointer pattern")
            if not isinstance(claim.get("source"), str) or not claim["source"]:
                fail(f"{name} claim {claim['pattern']} requires a provenance string")
            if "required" in claim and type(claim["required"]) is not bool:
                fail(f"{name} claim {claim['pattern']} has a non-boolean required flag")
            if name == "derived" and (not isinstance(claim.get("implementation"), str) or not claim["implementation"]):
                # A derived claim without a recompute/mapping implementation is
                # a hole by definition; it is reported instead of being counted.
                fail(f"derived claim {claim['pattern']} lacks its implementation location")
            if name == "uncovered" and (not isinstance(claim.get("manifest"), str) or not claim["manifest"]):
                fail(f"uncovered claim {claim['pattern']} must cite a manifest coverage.uncovered entry")
            result[name].append(dict(claim))
    return result


def _manifest_uncovered(manifest) -> set[str]:
    if manifest is None:
        return set()
    coverage = manifest.get("coverage")
    if not isinstance(coverage, dict):
        fail("native audit manifest has no coverage section")
    found = set()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "uncovered":
                    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                        fail("native manifest coverage.uncovered must be a list of strings")
                    found.update(value)
                else:
                    walk(value)
    walk(coverage)
    if not found:
        fail("native manifest declares no coverage.uncovered boundaries")
    return found


def claims_for(recipe, manifest=None, extra=None) -> dict:
    """Built-in claims plus this run's own declarations.

    The normalization class always comes from the recipe table (unified form or
    the legacy per-field blocks), and the uncovered class must cite the native
    manifest's own boundary list.
    """
    result = validate_claims(extra) if extra is not None else default_claims()
    result["normalization"] = [{"pattern": item["field"],
                                "source": f"b0_normalization entry ({item.get('reason', 'no reason')})"}
                               for item in b0.recipe_entries(recipe)]
    declared = _manifest_uncovered(manifest)
    uncovered = []
    for claim in result["uncovered"]:
        # A caller-supplied boundary is only admissible when the runtime's own
        # manifest already declares that boundary.
        if claim["manifest"] not in declared:
            fail(f"runtime manifest does not declare the boundary: {claim['manifest']}")
        uncovered.append(dict(claim, source="caller claim bound to the native manifest"))
    for item in UNCOVERED_MAP:
        if item["manifest"] not in declared:
            fail(f"runtime manifest does not declare the boundary: {item['manifest']}")
        if item["pattern"]:
            uncovered.append({"pattern": item["pattern"], "manifest": item["manifest"],
                              "source": "native manifest coverage.uncovered", "required": False})
    result["uncovered"] = uncovered
    result["boundaries"] = sorted(declared)
    result = validate_claims(result)
    result["uncovered"] = uncovered
    result["boundaries"] = sorted(declared)
    return result


def _type_at(document, pointer: str):
    node = document
    for token in _segments(pointer):
        if isinstance(node, dict):
            if token not in node:
                return None
            node = node[token]
        elif isinstance(node, list):
            if not token.isascii() or not token.isdecimal() or int(token) >= len(node):
                return None
            node = node[int(token)]
        else:
            return None
    return node


def check(state: dict, claims: dict) -> dict:
    """The two-way set difference plus the class-conflict rules."""
    claims = validate_claims(claims)
    universe = set(leaves(state))
    covered, class_report, dangling = set(), {}, []
    for name in CLASSES:
        matched, claims_report = set(), []
        for claim in claims[name]:
            hits = {pointer for pointer in universe if covers_leaf(pointer, claim["pattern"])}
            if claim["pattern"] in universe:
                hits.add(claim["pattern"])
            claims_report.append({key: claim[key] for key in ("pattern", "source", "required", "implementation", "manifest")
                                  if key in claim} | {"leaves": len(hits)})
            if not hits and claim.get("required", True):
                dangling.append({"class": name, "pattern": claim["pattern"],
                                 "reason": "no captured leaf", "source": claim["source"]})
                continue
            matched |= hits
        class_report[name] = {"claims": claims_report, "leaves": len(matched)}
        covered |= matched
    holes = sorted(universe - covered)
    conflicts = []
    normalization = {claim["pattern"] for claim in claims["normalization"]}
    uncovered_patterns = {claim["pattern"] for claim in claims["uncovered"]}
    for pointer in sorted(normalization & uncovered_patterns):
        conflicts.append({"kind": "normalized_field_marked_uncovered", "field": pointer})
    for claim in claims["uncovered"]:
        # A boundary claim may never cover a field the table really writes.
        for pointer in sorted(normalization):
            if covers_leaf(pointer, claim["pattern"]):
                conflicts.append({"kind": "normalized_field_marked_uncovered", "field": pointer,
                                  "manifest": claim["manifest"]})
    for claim in claims["normalization"]:
        value = _type_at(state, claim["pattern"])
        if type(value) is not int:
            conflicts.append({"kind": "normalization_target_is_not_one_integer_leaf",
                              "field": claim["pattern"], "actual_type": type(value).__name__})
    report = {"schema": SCHEMA,
              "leaf_count": len(universe),
              "classes": {name: {"claims": len(class_report[name]["claims"]),
                                 "leaves": class_report[name]["leaves"],
                                 "detail": class_report[name]["claims"]} for name in CLASSES},
              "holes": {"count": len(holes), "sample": holes[:SAMPLE_LIMIT],
                        "by_top_level": _histogram(holes)},
              "dangling": {"count": len(dangling),
                           "sample": [item["pattern"] for item in dangling][:SAMPLE_LIMIT],
                           "detail": dangling[:SAMPLE_LIMIT]},
              "runtime_declared_boundaries": copy.deepcopy(claims.get("boundaries", [])),
              "conflicts": conflicts,
              "coverage_complete": not holes and not dangling and not conflicts,
              "scope": "captured B(0) state only; complete_game_state=false is unchanged"}
    return report


def _histogram(pointers) -> dict:
    """Group holes by component; raw-address and index segments stay out of the key."""
    import re
    raw = re.compile(r"(?:[0-9a-f]{8}|[0-9]+)$")
    counts = {}
    for pointer in pointers:
        segments = _segments(pointer)
        key = "/" + "/".join(segments[:2]) if len(segments) > 1 and not raw.fullmatch(segments[1]) else (
            "/" + segments[0] if segments else "/")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def require_complete(report: dict) -> dict:
    """Strict failure: any nonempty difference rejects the recipe."""
    if report.get("coverage_complete") is not True:
        holes = report["holes"]["count"]
        dangling = report["dangling"]["count"]
        conflicts = len(report["conflicts"])
        fail(f"recipe completeness failed: holes U\\C={holes}, dangling C\\U={dangling}, conflicts={conflicts}")
    return report


def projection(recipe) -> list:
    """The ordered (field, target) table projection used for merge checks."""
    return [[item["field"], item["target"]] for item in b0.recipe_entries(recipe)]


def merge_check(left_recipe, right_recipe) -> dict:
    """§5.5: the two worlds merge only when the projections are identical."""
    left, right = projection(left_recipe), projection(right_recipe)
    return {"left": left, "right": right, "equal": left == right,
            "scope": "reason text is printed for review and never part of this judgement"}


def report_for_run(run: Path, *, claims=None, compare: Path | None = None) -> dict:
    """Read one sealed run's B(0) and classify it without starting a game."""
    run = Path(run)
    from .records import read_json as read_run_json
    recipe = read_run_json(run / "initialization-recipe.json")
    manifest = read_json(run / "audit/manifest.json")
    state = read_run_json(run / "observations/initial-audit.json")["state"]
    observation = read_run_json(run / "observations/initial.json")
    report = check(state, claims_for(recipe, manifest, claims))
    report["run"] = str(run.resolve())
    report["b0_version"] = observation.get("version")
    report["normalization_projection"] = projection(recipe)
    if compare is not None:
        report["merge_check"] = merge_check(recipe, read_run_json(Path(compare) / "initialization-recipe.json"))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="sealed run directory (read-only)")
    parser.add_argument("--compare", type=Path, default=None,
                        help="second sealed run for the §5.5 (field, target) merge check")
    parser.add_argument("--claims", type=Path, default=None,
                        help="optional claim set JSON replacing the built-in classification")
    parser.add_argument("--report", type=Path, default=None, help="write the full report here")
    args = parser.parse_args(argv)
    try:
        claims = read_json(args.claims) if args.claims else None
        report = report_for_run(args.run, claims=claims, compare=args.compare)
        if args.report:
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"coverage_complete": report["coverage_complete"], "leaf_count": report["leaf_count"],
                          "holes": report["holes"]["count"], "dangling": report["dangling"]["count"],
                          "conflicts": len(report["conflicts"]),
                          "holes_by_top_level": report["holes"]["by_top_level"],
                          "normalization_projection": report["normalization_projection"],
                          "merge_check": report.get("merge_check")}, ensure_ascii=False, indent=2))
        require_complete(report)
        return 0
    except Exception as error:
        parser.exit(1, f"coverage: {type(error).__name__}: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
