"""Shared seeded boundary preparation for launch, REPL, evaluation and replay.

The controlled draw consumes its real RNG values exactly once. B0 is captured
after it; no reseeding/rollback is permitted to conceal that consumption.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from . import sound_effects

DRAW_MODE = "deterministic_draw_schedule_v1"
LEGACY_DRAW_MODE = "legacy_autonomous_draw_schedule"


def draw_mode(hello: dict) -> str:
    """Negotiate the complete new contract; old runtimes remain explicitly old."""
    capabilities = hello.get("capabilities", {})
    spec = hello.get("game", {}).get("draw_schedule")
    advertised = capabilities.get("prepare_render") is True or capabilities.get(DRAW_MODE) is True
    if spec is None and not advertised:
        return LEGACY_DRAW_MODE
    if (not isinstance(spec, dict) or spec.get("mode") != DRAW_MODE
            or spec.get("installed") is not True
            or capabilities.get("prepare_render") is not True
            or capabilities.get(DRAW_MODE) is not True
            or spec.get("autonomous_fight_draws") is not False
            or spec.get("capture") != "cached_bgr24_only"
            or spec.get("rng_restore_after_draw") is not False):
        raise RuntimeError("incomplete or unsupported controlled draw schedule contract")
    return DRAW_MODE


def clock_anchor(snapshot: dict) -> dict:
    state = snapshot["state"]
    return {"schema": state["schema"], "target": state["rng"]["target"],
            "game_clock": state["board"]["00005568"], "effect_clock": state["board"]["0000556c"],
            "mj_clock": state["app"]["mj_clock"]}


def _rng_sha256(snapshot: dict) -> str:
    return hashlib.sha256(json.dumps(snapshot["state"]["rng"]["instances"],
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def verify_seeded_rng(snapshot: dict, seed: int) -> None:
    rng = snapshot["state"]["rng"]["instances"]
    words = [seed or 4357]
    for index in range(1, 624):
        previous = words[-1]
        words.append((1812433253 * (previous ^ (previous >> 30)) + index) & 0xFFFFFFFF)
    if (rng["global_mt"].get("words") != words or rng["global_mt"].get("cursor") != 624
            or rng["game_thread_crt"].get("state") != seed):
        raise RuntimeError("RNG readback differs from the requested seed")


def _persist(client, run: Path, hello: dict, recipe: dict, evidence: dict,
             snapshot: dict, observation: dict, scenario_verified: bool) -> None:
    from .records import read_json, record_initial_state, sha256, write_json
    run = Path(run)
    manifest = read_json(run / "manifest.json")
    if manifest.get("status") != "recording":
        raise ValueError("cannot rewrite initialization of a finalized run")
    (run / "observations").mkdir(exist_ok=True)
    write_json(run / "initialization-recipe.json", recipe)
    write_json(run / "initialization-evidence.json", evidence)
    write_json(run / "observations/initial.json", observation)
    write_json(run / "observations/initial-audit.json", snapshot)
    manifest = record_initial_state(run, observation_path=run / "observations/initial.json",
        hello=hello, scenario_verified=scenario_verified,
        audit_snapshot_path=run / "observations/initial-audit.json")
    manifest["initial_state"]["execution_mode"] = recipe["execution_mode"]
    manifest["initial_state"]["render_prepared"] = observation.get("render_prepared", False)
    manifest["initialization"] = {
        "execution_mode": recipe["execution_mode"],
        "recipe": {"path": "initialization-recipe.json", "sha256": sha256(run / "initialization-recipe.json")},
        "evidence": {"path": "initialization-evidence.json", "sha256": sha256(run / "initialization-evidence.json")},
        "draw_health": {"warm_preparation_checked": recipe["execution_mode"] == DRAW_MODE,
                        "final_health": "pending_native_draw_schedule_closed" if recipe["execution_mode"] == DRAW_MODE else "not_supported"},
    }
    write_json(run / "manifest.json", manifest)


def apply_recipe(client, seed: int, anchor: dict | None = None, *, run: Path | None = None,
                 scenario_verified: bool | None = None) -> dict:
    if type(seed) is not int or not 0 <= seed <= 0xFFFFFFFF:
        raise ValueError("seed must be uint32")
    hello = client.hello()
    capabilities = hello["capabilities"]
    if capabilities.get("rng_seed") is not True:
        raise RuntimeError("runtime has no implemented rng_seed capability; planned seed is not evidence")
    mode = draw_mode(hello)
    sound_mode = sound_effects.negotiate(hello)
    if (anchor is not None or mode == DRAW_MODE) and capabilities.get("clock_restore") is not True:
        raise RuntimeError("runtime lacks clock_restore needed by the recorded initialization recipe")
    observation = client.observe()
    if observation["version"]["tick"] != 0:
        raise RuntimeError("initialization must precede the first simulated tick")
    if mode == DRAW_MODE:
        if observation.get("render_prepared") is not False:
            raise RuntimeError("render preparation already completed or readiness flag is missing; do not reseed B0")
        if observation.get("game_ui") != 3 or client.status().get("state") != "paused_at_boundary":
            raise RuntimeError("render preparation requires a paused ready fight")
    run = run if run is not None else getattr(client, "_initialization_run", None)
    if run is not None:
        from .records import read_json
        if read_json(Path(run) / "manifest.json").get("status") != "recording":
            raise ValueError("cannot rewrite initialization of a finalized run")
    if mode == DRAW_MODE:
        # Both source and cold replay execute the same mutation sequence. A
        # source restores its own captured anchor once, not zero times.
        captured_anchor = clock_anchor(client.request("audit_snapshot"))
        if anchor is None:
            anchor = captured_anchor
    client.request("rng_seed", {"seed": seed}, expect=observation["version"])
    if anchor is not None:
        client.request("clock_restore", {"snapshot": anchor}, expect=client.version)
    seeded = client.request("audit_snapshot")
    verify_seeded_rng(seeded, seed)
    sound_effects.state(seeded["state"], hello.get("game", {}))
    before_clock = clock_anchor(seeded)
    if anchor is not None and before_clock != anchor:
        raise RuntimeError("clock readback differs from the initialization anchor")
    recipe = {"game_mode": 13, "seed": seed,
        "seed_phase": "before_enter_game_and_paused_after_scenario_load",
        "seed_scope": "global Sexy MT + game-thread CRT; initialization also seeds before scene creation; generated initial state is compared",
        "clock_anchor": before_clock, "execution_mode": mode}
    prepared = None
    snapshot = seeded
    if mode == DRAW_MODE:
        if seeded["state"].get("draw_schedule") != {"mode": DRAW_MODE, "warm_frames": 0, "step_frames": 0}:
            raise RuntimeError("draw schedule has already rendered a controlled frame before preparation")
        before_version = copy.deepcopy(client.version)
        if seeded.get("version") != before_version:
            raise RuntimeError("seeded audit snapshot is not the current initialization boundary")
        prepared = client.request("prepare_render", {}, expect=before_version)
        expected_version = {**before_version, "revision": before_version["revision"] + 1}
        rendered = prepared.get("render", {})
        if (prepared.get("prepared") is not True or client.version != expected_version
                or prepared.get("observation", {}).get("render_prepared") is not True
                or rendered.get("mode") != DRAW_MODE or rendered.get("phase") != "warm"
                or rendered.get("frame_version") != expected_version
                or rendered.get("rng_restored") is not False
                or rendered.get("clocks_before") != before_clock or rendered.get("clocks_after") != before_clock
                or rendered.get("counts") != {"mode": DRAW_MODE, "warm_frames": 1, "step_frames": 0}
                or rendered.get("seed_readback") != {"seed": seed, "global_mt_words": 624,
                    "global_mt_cursor": 624, "game_thread_crt": seed, "verified_before_draw": True}):
            raise RuntimeError("prepare_render did not establish the exact next warm boundary")
        snapshot = client.request("audit_snapshot")
        after_clock = clock_anchor(snapshot)
        if (snapshot.get("version") != expected_version
                or snapshot["state"].get("draw_schedule") != {"mode": DRAW_MODE, "warm_frames": 1, "step_frames": 0}
                or after_clock != before_clock):
            raise RuntimeError("postwarm audit snapshot has invalid clocks, counters or boundary")
        recipe.update(draw_schedule=copy.deepcopy(hello["game"]["draw_schedule"]),
            postwarm_clock=after_clock,
            render_preparation={"warm_frames": 1, "step_frames": 0, "verified_before_draw": True,
                "seeded_rng_sha256": _rng_sha256(seeded), "postwarm_rng_sha256": _rng_sha256(snapshot)})
    sound = sound_effects.state(snapshot["state"], hello.get("game", {}))
    if sound_mode:
        recipe["sound_effects"] = {"configuration": copy.deepcopy(hello["game"]["sound_effects"]),
                                   "b0_state_sha256": sound_effects.state_sha256(sound)}
    # A read-only observation checks that persistence binds the same final boundary.
    observation = client.observe()
    if snapshot.get("version") != observation["version"]:
        raise RuntimeError("initial observation changed after the audit snapshot")
    evidence = {"schema": "lvz.initialization-evidence.v1", "execution_mode": mode,
        "seeded_version": seeded.get("version"), "seeded_rng_sha256": _rng_sha256(seeded),
        "postwarm_rng_sha256": _rng_sha256(snapshot), "prepare_render": prepared,
        "initial_version": observation["version"], "final_draw_health": "pending_native_close" if mode == DRAW_MODE else "not_supported"}
    trace = getattr(client, "trace", None)
    if trace is not None:
        trace.emit("initialization_prepared", {"recipe": recipe, "evidence": evidence})
    if run is not None:
        verified = getattr(client, "_initialization_scenario_verified", False) if scenario_verified is None else scenario_verified
        _persist(client, Path(run), hello, recipe, evidence, snapshot, observation, verified)
    return recipe


def ensure_render_prepared(client, seed: int = 0) -> dict | None:
    """Prepare an attached ready new-mode game; reconnecting never reseeds it."""
    hello = client.hello_result if client.hello_result is not None else client.hello()
    sound_effects.negotiate(hello)
    if draw_mode(hello) != DRAW_MODE:
        return None
    observation = client.observe()
    if observation.get("render_prepared") is True:
        return None
    if observation.get("game_ui") != 3:
        return None  # The REPL can still inspect/loading-initialize the process.
    return apply_recipe(client, seed)
