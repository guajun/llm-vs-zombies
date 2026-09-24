"""Own-process launcher with private files, process-local hooks and PID identity checks.

The headless option means a hidden ordinary game window, not a renderer-free
simulation. Live acceptance remains necessary for a particular game build.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import time

from .records import read_json, sha256, write_json
from . import sound_effects

DEFAULT_CARDS = [16, 30, 14, 63, 15, 2, 20, 17, 8, 27]
REFERENCE_LAYOUT = {
    42: {(2, 2), (5, 2), (3, 6), (4, 6)},
    41: {(2, 1), (5, 1), (3, 1), (3, 3), (4, 1), (4, 3)},
    37: {(3, 2), (4, 2)},
    30: {(2, 1), (2, 2), (3, 6), (4, 6), (5, 1), (5, 2)},
    16: {(3, 1), (3, 2), (3, 3), (3, 6), (4, 1), (4, 2), (4, 3), (4, 6)},
}
# 经典十二炮的卡序来自教程 jing_dian_12_co_await.cpp 的 ASelectCards，逐项按
# avz/framework/inc/avz_types.h 的 APlantType 取值换算成本仓库的观察 id：模仿者卡片
# 记作 49 + 被模仿的种子（AM_ICE_SHROOM = 14 -> 63），与观察里
# `type == 48 ? imitator_type + 49 : type` 的写法一致。
#   寒冰菇 AICE_SHROOM=14、模仿寒冰菇 AM_ICE_SHROOM=63、咖啡豆 ACOFFEE_BEAN=35、
#   毁灭菇 ADOOM_SHROOM=15、荷叶 ALILY_PAD=16、倭瓜 ASQUASH=17、
#   樱桃炸弹 ACHERRY_BOMB=2、三叶草 ABLOVER=27、南瓜头 APUMPKIN=30、小喷菇 APUFF_SHROOM=8
JINGDIAN12_CARDS = [14, 63, 35, 15, 16, 17, 2, 27, 30, 8]
# avz_types.h: ACOB_CANNON = 47（玉米加农炮）。
COB_CANNON = 47
COMMON_INPUTS = ("game/original/PlantsVsZombies.exe", "game/original/PlantsVsZombies.dat",
                 "game/original/main.pak", "game/original/bass.dll",
                 "game/local-engine/PlantsVsZombies.exe")


@dataclass(frozen=True)
class Scenario:
    """One loadable reference scenario: its save, its cards, and what "loaded" means.

    ``layout`` (if non-empty) is an exact, complete expectation per plant type;
    ``min_plants`` only requires at least N living plants of a type. Both are
    checked against the observation of the loaded board, never against the save
    file, and both are deliberately conservative for a scenario whose board has
    not been confirmed live yet.
    """

    name: str
    save: str
    config: str
    cards: tuple[int, ...]
    game_mode: int
    expected_scene: int
    scene_label: str
    layout: dict = field(default_factory=dict)
    min_plants: dict = field(default_factory=dict)
    note: str = ""

    def inputs(self) -> tuple[str, ...]:
        """The hash-locked local prerequisites of this scenario, in lock order."""
        return (*COMMON_INPUTS, self.save)


SCENARIOS = {
    "liangyi": Scenario(
        name="liangyi",
        save="experiments/scenarios/liangyi/game1_13.dat",
        config="experiments/configs/liangyi.json",
        cards=tuple(DEFAULT_CARDS),
        game_mode=13,
        expected_scene=3,
        scene_label="fog",
        layout=REFERENCE_LAYOUT,
        note="官方两仪参考存档：雾夜（Scene 3）、4 曾/6 花/2 伞 + 固定卡序"),
    "jingdian12": Scenario(
        name="jingdian12",
        save="experiments/scenarios/jingdian12/game1_13.dat",
        config="experiments/configs/jingdian12.json",
        cards=tuple(JINGDIAN12_CARDS),
        game_mode=13,
        expected_scene=2,
        scene_label="pool",
        min_plants={COB_CANNON: 12},
        note="AvZ 教程经典十二炮：泳池无尽（Board::Scene()==2，待真机确认）、至少 12 门玉米加农炮"),
}


def scenario_spec(name: str = "liangyi") -> Scenario:
    """Registry lookup; an unknown scenario fails closed with the known list."""
    try:
        return SCENARIOS[name]
    except (KeyError, TypeError):
        raise ValueError(f"unknown scenario {name!r}; known scenarios: {', '.join(sorted(SCENARIOS))}") from None


REQUIRED_INPUTS = scenario_spec("liangyi").inputs()
BRANCH_CHANNEL = "LVZ_BRANCH_ID"


def branch_identity(run: Path, requested: str | None = None) -> dict:
    """The single branch name this run's runtime and its evidence tree must share.

    The default is the run directory name, because that name is what a tree
    node is packaged and referenced as; deriving both from one string is what
    keeps "the branch the engine executed" and "the branch the evidence is
    filed under" from drifting apart. ``requested`` overrides it for explicit
    counterfactual branches and is validated with the same grammar as the
    protocol (``client.branch_scope_id``) and the evidence tree
    (``evidence_tree.branch_id``). An unusable value fails here, before any
    process exists: the runtime's own fallback is a process-instance label
    that no tree can be named after.
    """
    from .client import branch_scope_id
    value, source = (Path(run).name, "run-directory-name") if requested is None else (requested, "explicit")
    if not isinstance(value, str):
        raise ValueError("branch id must be a string")
    try:
        value = branch_scope_id(value)
    except ValueError as error:
        hint = "" if requested is not None else "; rename the run or pass --branch-id"
        raise ValueError(f"{source} branch id cannot identify this run: {error}{hint}") from error
    return {"branch_id": value, "branch_source": source}


def verify_inputs(root: Path, scenario: str = "liangyi") -> dict[str, str]:
    spec = scenario_spec(scenario)
    locked = {item["path"]: item for item in read_json(root / "dependencies.lock.json")["files"]}
    verified = {}
    for name in spec.inputs():
        if name not in locked:
            raise ValueError(f"required input is absent from dependencies.lock.json: {name}")
        path = root / name
        if not path.is_file():
            raise FileNotFoundError(f"missing local prerequisite: {path}; game files are not downloaded")
        digest = sha256(path)
        if digest != locked[name]["sha256"] or path.stat().st_size != locked[name]["size"]:
            raise ValueError(f"locked input hash/size mismatch: {name}")
        verified[name] = digest
    return verified


def synthetic_profile() -> dict[str, bytes]:
    """Deterministic single-user fixture, independent of the player's real saves.

    Formats derive from pinned ProfileMgr::SyncState / PlayerInfo::SyncDetails.
    The profile has the campaign completed, purchased plant upgrades and 10 slots.
    It does not certify an initial board or any RNG state.
    """
    name = b"Experiment"
    users = struct.pack("<IH", 14, 1) + struct.pack("<H", len(name)) + name + struct.pack("<II", 1, 1)
    values = [12, 50, 0, 1] + [0] * 100 + [0] * 80 + [0] * 21
    for index in range(9):
        values[104 + index] = 1
    values[104 + 21] = 4  # STORE_ITEM_PACKET_UPGRADE: six initial slots + four.
    for index in (8, 9, 14):  # unlocked minigames / puzzle / survival flags
        values[184 + index] = 1
    return {"users.dat": users, "user1.dat": struct.pack("<205I", *values)}


def prepare(root: Path, run: Path, *, audio_mode: str = "original", scenario: str = "liangyi",
            branch_id: str | None = None) -> dict:
    audio_mode = sound_effects.configured(audio_mode)
    spec = scenario_spec(scenario)
    root, run = root.resolve(), run.resolve()
    if not run.is_relative_to(root / "experiments/runs"):
        raise ValueError("experiment run must be under this project's experiments/runs")
    identity = branch_identity(run, branch_id)
    manifest = read_json(run / "manifest.json")
    if manifest["status"] != "recording" or (run / "events.jsonl").exists():
        raise ValueError("launcher requires a fresh recording run")
    # A run created from a scenario config records which save it was created
    # for; the sandbox must not stage a different scenario's board than the one
    # the archive claims.
    configuration = read_json(run / "config.json") if (run / "config.json").is_file() else {}
    declared = configuration.get("scenario_save")
    if declared and str(declared).replace("\\", "/") != spec.save:
        raise ValueError(f"run was created for scenario save {declared}, not {spec.save}; create a new run "
                         f"with experiments/configs/{spec.name}.json")
    inputs = verify_inputs(root, scenario)
    recorder = root / "build/recorder.dll"
    if not recorder.is_file() or sha256(recorder) != manifest["implementation"]["recorder_sha256"]:
        raise ValueError("recorder DLL differs from the build bound to this run; create a new run")
    helpers = [root / "build/launcher/lvz-launcher.exe", root / "build/launcher/lvz-bootstrap.dll"]
    for helper in helpers:
        if not helper.is_file():
            raise FileNotFoundError(f"missing launcher helper: {helper}; run launcher/build.ps1")
    sandbox = run / "sandbox"
    # The original game uses ANSI paths and MAX_PATH in its folder resolver.
    if len(str(sandbox)) > 200 or (os.name == "nt" and str(sandbox).encode("mbcs", "replace").decode("mbcs") != str(sandbox)):
        raise ValueError("sandbox path must be <= 200 characters and representable by the Windows ANSI code page")
    sandbox.mkdir(exist_ok=False)
    game = sandbox / "game"
    game.mkdir()
    for filename in ("main.pak", "bass.dll", "drm.xml", "drm.xml.sig", "updates.xml"):
        candidate = root / "game/original" / filename
        if candidate.is_file():
            shutil.copy2(candidate, game / filename)
    shutil.copytree(root / "game/original/properties", game / "properties")
    shutil.copy2(root / "game/local-engine/PlantsVsZombies.exe", game / "PlantsVsZombies.exe")
    (sandbox / "windows").mkdir()
    profiles = synthetic_profile()
    # Signed partner configuration chooses the product name. Both known product
    # paths and the old relative fallback are private directories.
    for userdata in (sandbox / "appdata/PopCap Games/PlantsVsZombies/userdata",
                     sandbox / "appdata/PopCap Games/PopCap/PlantsVsZombies/userdata", game / "userdata"):
        userdata.mkdir(parents=True)
        for name, content in profiles.items():
            (userdata / name).write_bytes(content)
        shutil.copy2(root / spec.save, userdata / "game1_13.dat")
    module = sandbox / "modules"
    module.mkdir()
    for helper in helpers:
        shutil.copy2(helper, module / helper.name)
    shutil.copy2(recorder, module / "recorder.dll")
    if sha256(module / "recorder.dll") != manifest["implementation"]["recorder_sha256"]:
        raise ValueError("recorder changed while copying; create a new run after the build finishes")
    if sha256(game / "PlantsVsZombies.exe") != inputs["game/local-engine/PlantsVsZombies.exe"]:
        raise ValueError("engine changed while copying")
    interval = manifest["configuration"].get("state_interval_ticks", 10)
    if type(interval) is not int or not 1 <= interval <= 1000:
        raise ValueError("state_interval_ticks must be an integer in 1..1000")
    (module / "recorder.cfg").write_text(f"{run}\n{interval}\n", encoding="utf-8")
    state = {"schema": 1, "run": str(run), "sandbox": str(sandbox), "engine": str(game / "PlantsVsZombies.exe"),
             "native_launcher": str(module / "lvz-launcher.exe"), "bootstrap": str(module / "lvz-bootstrap.dll"),
             "runtime": str(module / "recorder.dll"), "headless_mode": "hidden_window",
             "status": "prepared", "audio_mode": audio_mode, "scenario": spec.name, "input_hashes": inputs,
             **identity, "branch_channel": BRANCH_CHANNEL,
             "module_hashes": {p.name: sha256(p) for p in module.iterdir() if p.suffix in (".dll", ".exe")},
             "profile_hashes": {name: hashlib.sha256(content).hexdigest() for name, content in profiles.items()},
             "resource_hashes": {p.relative_to(game).as_posix(): sha256(p) for p in game.rglob("*") if p.is_file()},
             "scenario_verified": False}
    write_json(run / "launcher.json", state)
    return state


def _native(state: dict, *arguments: str) -> dict:
    if os.name != "nt":
        raise OSError("the original PvZ launcher requires Windows")
    helper = Path(state["native_launcher"])
    if sha256(helper) != state["module_hashes"][helper.name]:
        raise ValueError("launcher helper changed after preparation")
    result = subprocess.run([str(helper), *map(str, arguments)], capture_output=True, text=True,
                            timeout=35, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"native launcher failed: exit {result.returncode}")
    return json.loads(result.stdout)


def stop(run: Path) -> dict:
    state = read_json(run / "launcher.json")
    if "pid" not in state:
        receipt = Path(state["sandbox"]) / "native-receipt.json"
        if receipt.is_file():
            state.update(read_json(receipt))
    if "pid" not in state or state.get("status") == "stopped":
        raise ValueError("run does not own a currently launched process")
    result = _native(state, "stop", state["pid"], state["engine"], state["creation_time"], "unused")
    state["status"] = "stopped"
    write_json(run / "launcher.json", state)
    return result


def start(root: Path, run: Path, *, initialize: bool = True, timeout: float = 90.0, seed: int = 0,
          defer_preparation: bool = False, audio_mode: str = "original", mj_clock: int | None = None,
          app_update_count: int | None = None, b0_normalize: list | None = None,
          scenario: str = "liangyi", branch_id: str | None = None) -> dict:
    from .client import connect
    from .session import SessionTrace
    from . import b0_normalization as b0
    from . import fp_environment
    spec = scenario_spec(scenario)
    if type(seed) is not int or not 0 <= seed <= 0xFFFFFFFF:
        raise ValueError("seed must be uint32")
    if type(defer_preparation) is not bool:
        raise ValueError("defer_preparation must be boolean")
    # --mj-clock / --app-update-count stay readable as transitional aliases for
    # single-entry tables; an explicit --b0-normalize table never mixes with them.
    table = [b0.entry(item) for item in (b0_normalize or [])]
    if table and (mj_clock is not None or app_update_count is not None):
        raise ValueError("--b0-normalize cannot be mixed with the transitional "
                         "--app-update-count/--mj-clock aliases")
    if not table:
        table = b0.alias_entries(app_update_count=app_update_count, mj_clock=mj_clock, source="flag")
    # The CLI may pass a project-relative --run. Resolve the archive base once so
    # the session trace, launcher.json and the initialization recipe cannot bind
    # the same evidence to different paths.
    root, run = Path(root).resolve(), Path(run).resolve()
    audio_mode = sound_effects.configured(audio_mode)
    # One identity for this run: the runtime scope injected into the engine and
    # the branch name used for its evidence tree come from the same string.
    identity = branch_identity(run, branch_id)
    state = prepare(root, run, audio_mode=audio_mode, scenario=spec.name, branch_id=branch_id)
    state["audio_mode"] = audio_mode
    state["initialization_seed"] = seed
    state["preparation_deferred"] = defer_preparation
    sandbox = Path(state["sandbox"])
    try:
        receipt = _native(state, "launch", state["engine"], state["bootstrap"], state["sandbox"],
                          str(sandbox / "game"), str(sandbox / "native-receipt.json"),
                          state.get("branch_id", identity["branch_id"]),
                          *([audio_mode] if audio_mode != "original" else []))
        # The receipt is written before the primary thread resumes, so it is the
        # earliest proof of which scope the engine actually inherited. Compare it
        # before merging: the receipt must never be able to re-label this run.
        if state.get("branch_channel") == BRANCH_CHANNEL and receipt.get("branch_id") != state["branch_id"]:
            raise ValueError("native launcher receipt does not confirm the injected branch id; "
                             "the runtime would start in an unknown scope")
        state.update(receipt, status="started")
        state["branch_id"], state["branch_source"] = identity["branch_id"], identity["branch_source"]
        write_json(run / "launcher.json", state)
        _native(state, "inject", state["pid"], state["engine"], state["creation_time"], state["runtime"])
        state["endpoint"] = f"\\\\.\\pipe\\llm-vs-zombies-{state['pid']}"
        write_json(run / "launcher.json", state)
        with SessionTrace(run / "decisions/launcher.jsonl") as trace, connect(pid=state["pid"], trace=trace, timeout=timeout) as client:
            state["hello"] = client.hello_result
            state["runtime_branch"] = verify_branch_scope(state, client.hello_result)
            verify_audio_activation(state, client.hello_result)
            fp_mode = fp_environment.negotiate(client.hello_result)
            observation = client.observe()
            if initialize:
                deadline = time.monotonic() + timeout
                while not (observation["game_ui"] == 1 or
                           observation["game_ui"] == 0 and observation.get("loading_complete") is True):
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"game did not reach the main menu: game_ui={observation['game_ui']}")
                    time.sleep(0.1)
                    observation = client.observe()
                configured = client.request("initialize",
                                            {"game_mode": spec.game_mode, "cards": list(spec.cards), "seed": seed},
                                            expect=observation["version"])
                if fp_mode:
                    activation = fp_environment.activation(configured.get("fixed_fp"), client.hello_result["game"])
                    if activation["version"] != observation["version"]:
                        raise ValueError("fixed FP activation is not bound to this initialize request boundary")
                    state["fixed_fp_activation"] = activation
                while True:
                    observation = client.observe()
                    initialization = observation.get("initialization", {})
                    if initialization.get("state") == "error":
                        raise RuntimeError(f"scenario initialization failed: {initialization.get('error')}")
                    if initialization.get("state") == "ready":
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"scenario initialization timed out: {initialization}")
                    time.sleep(0.1)
                verify_scenario(observation, spec.name)
                if fp_mode:
                    ready_fp = fp_environment.evidence(client.request("audit_snapshot"), client.hello_result["game"])
                    if ready_fp["activation"] != state["fixed_fp_activation"]:
                        raise ValueError("fixed FP activation changed during asynchronous scene initialization")
                    state["fixed_fp_ready"] = ready_fp
                state["scenario_verified"] = True
                if not defer_preparation:
                    from .initialization import apply_recipe
                    state["initialization_recipe"] = apply_recipe(client, seed, run=run, scenario_verified=True,
                                                                  b0_normalization=table or None)
                    observation = client.observation
                    state["hello"] = client.hello_result
                else:
                    # This is scenario evidence only; apply_recipe will bind B0
                    # and initial.json after the caller's one-time warm draw.
                    write_json(run / "observations/scenario-ready.json", observation)
            state["status"] = "ready" if initialize else "connected"
            state["initial_observation"] = observation
            state["render_prepared"] = observation.get("render_prepared", False)
            write_json(run / "launcher.json", state)
            write_json(run / "observations/initial.json", observation)
        return state
    except BaseException as error:
        receipt_path = sandbox / "native-receipt.json"
        try:
            if "pid" not in state and receipt_path.is_file():
                state.update(read_json(receipt_path))
        except Exception as receipt_error:
            error.add_note(f"Cannot recover native process receipt: {receipt_error}")
        state.update(status="failed", error=f"{type(error).__name__}: {error}")
        # A disk failure must not prevent closing a process we already own.
        if "pid" in state:
            try:
                _native(state, "stop", state["pid"], state["engine"], state["creation_time"], "unused")
                state["process_cleaned_up"] = True
            except Exception as cleanup_error:
                state["cleanup_error"] = str(cleanup_error)
                error.add_note(f"Owned process cleanup failed: {cleanup_error}")
        try:
            write_json(run / "launcher.json", state)
        except Exception as persistence_error:
            error.add_note(f"Cannot persist launcher failure/cleanup evidence: {persistence_error}")
        raise


def verify_branch_scope(state: dict, hello: dict) -> dict | None:
    """The resident runtime must own exactly the branch this run declares.

    Returns the runtime's own ``hello.branch`` block so it can be recorded next
    to the requested id. A runtime that declares nothing, or declares a
    different id, fails the launch: the process would then deduplicate requests
    in a namespace that no evidence tree is named after, which is the
    confusion the branch-scope contract forbids.

    The check is bound to the prepared launcher state: ``prepare`` records
    ``branch_channel`` and ``branch_id`` together, and a state without the
    channel (a fixture, or a state dict written before this channel existed) is
    left exactly on its previous behavior.
    """
    from .client import declared_branch_scope
    if state.get("branch_channel") != BRANCH_CHANNEL:
        return None
    expected = state.get("branch_id")
    if expected is None:
        raise ValueError(f"{BRANCH_CHANNEL} state is missing its branch_id")
    declared = declared_branch_scope(hello)
    if declared is None:
        raise ValueError(f"runtime declared no branch scope although {BRANCH_CHANNEL} was injected")
    if declared["mode"] != "branch" or declared["branch_id"] != expected:
        raise ValueError(f"runtime branch scope {declared['branch_id']!r} differs from this run's branch {expected!r}")
    return declared


def verify_audio_activation(state: dict, hello: dict) -> None:
    """Bind requested mode to the suspended-launch receipt and resident code."""
    actual = sound_effects.negotiate(hello, expected=state["audio_mode"])
    if actual is None:
        if "audio_activation" in state:
            raise ValueError("original audio launch contains an unexpected activation receipt")
        return
    spec = hello["game"]["sound_effects"]
    if spec["bootstrap_sha256"] != state.get("module_hashes", {}).get("lvz-bootstrap.dll"):
        raise ValueError("sound effects bootstrap differs from the prepared launcher module")
    sound_effects.receipt(state.get("audio_activation"), spec, pre_resume=True)


def verify_scenario(observation: dict, scenario: str = "liangyi") -> None:
    """Check the loaded board against the selected scenario's registry entry.

    The card order and the fight scene come from the registry; the layout rules
    are per scenario and may be exact (liangyi) or deliberately loose while a
    board has not been confirmed live (jingdian12).
    """
    spec = scenario_spec(scenario)
    if observation.get("game_ui") != 3 or observation.get("scene") != spec.expected_scene:
        raise ValueError(f"loaded scenario is not an active Scene {spec.expected_scene} {spec.scene_label} fight")
    plants = observation.get("plants", [])
    for kind, required in spec.layout.items():
        matching = [p for p in plants if p.get("type") == kind]
        actual = {(p.get("row"), p.get("col")) for p in matching}
        if actual != required or len(matching) != len(required):
            raise ValueError(f"reference layout mismatch: plant type {kind} expected {sorted(required)}, observed {sorted(actual)}")
    for kind, minimum in spec.min_plants.items():
        matching = [p for p in plants if p.get("type") == kind]
        if len(matching) < minimum:
            raise ValueError(f"scenario requires at least {minimum} plant type {kind}, observed {len(matching)}")
    seeds = observation.get("seeds", [])
    selected = [s.get("imitator_type") + 49 if s.get("type") == 48 else s.get("type") for s in seeds]
    if selected != list(spec.cards):
        raise ValueError(f"selected card order differs from the experiment definition: {selected}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "start", "stop"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--no-initialize", action="store_true")
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--branch-id", default=None,
                        help="Branch scope for the launched runtime; default is the run directory "
                             "name, so the runtime identity and the evidence tree branch agree")
    parser.add_argument("--scenario", default="liangyi",
                        help="Reference scenario from the launcher registry (liangyi, jingdian12); "
                             "an unknown name is rejected before any process starts")
    parser.add_argument("--audio-mode", choices=("original", sound_effects.MODE), default="original",
                        help="Explicit SFX allocation semantics; original remains the default")
    parser.add_argument("--defer-preparation", action="store_true",
                        help="Leave seeded/warm boundary preparation to the evaluation or replay recipe")
    parser.add_argument("--app-update-count", type=int, default=None,
                        help="Transitional alias for --b0-normalize with field /sound_effects/app_update_count")
    parser.add_argument("--mj-clock", type=int, default=None,
                        help="Transitional alias for --b0-normalize with field /app/mj_clock")
    parser.add_argument("--b0-normalize", action="append", default=None, metavar="JSON",
                        help="Declared B(0) normalization entry as a JSON object {field, target, reason}; "
                             "repeatable, applied in command-line order")
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "stop":
            result = stop(arguments.run.resolve())
        elif arguments.command == "prepare":
            result = prepare(arguments.root, arguments.run, audio_mode=arguments.audio_mode,
                             scenario=arguments.scenario, branch_id=arguments.branch_id)
        else:
            result = start(arguments.root, arguments.run, initialize=not arguments.no_initialize,
                           timeout=arguments.timeout, seed=arguments.seed, defer_preparation=arguments.defer_preparation,
                           audio_mode=arguments.audio_mode, mj_clock=arguments.mj_clock,
                           app_update_count=arguments.app_update_count,
                           scenario=arguments.scenario, branch_id=arguments.branch_id,
                           b0_normalize=[json.loads(item) for item in (arguments.b0_normalize or [])])
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        parser.exit(1, f"launcher: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
