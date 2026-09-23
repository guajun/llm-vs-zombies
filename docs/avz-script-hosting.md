# Hosting an AvZ script inside the resident runtime

P2 wants the two-flag / strict run to use `avz/framework/tutorial/scripts/jing_dian_12/`
(`game1_13.dat` + `jing_dian_12_co_await.cpp`). This note records what was
verified about that path, how to build it, and what the offline evidence does
and does not cover. Everything below is quoted from the pinned submodule
(`avz/framework` at the commit in `dependencies.lock.json`) or produced by the
offline test in `tests/avz_hosted_script_tests.cpp`.

## 1. How a user script reaches the DLL

AvZ scripts are **compiled into the injected DLL**; there is no runtime loader.

* `avz/framework/metadata.json` `compileOptions` compiles the script file itself
  and links it into the module that gets injected:
  `... -shared -o "bin/libavz.dll"` with `-D__SCRIPT__=R"(:__FILE_NAME__:)"`.
* `avz/framework/src/avz_script.cpp` calls the script at link time - there is no
  lookup, no `LoadLibrary`, no exported entry:
  `void AScript(); AScript();` inside `__AScriptManager::LoadScript()`.
* `avz/framework/inc/avz.h` *defines* that entry from the script source:
  `#define ACoScript() __ARealScript(); void AScript() { ACoLaunch(__ARealScript); } auto __ARealScript() -> decltype(__ARealScript())`.
* `__SCRIPT__` itself is only the script's own file name: `inc/libavz.h` sets it
  to `""` for editors, and `inc/dsl/verifier.h` uses it to name the `.seml`
  trace it writes. Nothing reads it to locate a script.
* `avz/framework/tools/injector/src/main.cpp` injects `bin/libavz.dll` - the
  script's own module.

Consequence for this repository: the runtime's DLL *is* that module, and it
already defines `AScript()` (`logger/avz/recorder.cpp`: run lock, `events.jsonl`,
stop key). An unmodified tutorial source cannot be linked next to it, because it
defines `AScript()` too. A hosted script therefore exposes only its coroutine
under one name - `ACoroutine lvz::hosted::Script()` - and the recorder launches
it from inside `AScript()` with `ACoLaunch` (`logger/avz/hosted_script.hpp`).

## 2. What advances a script coroutine while the runtime owns frames

The per-frame entry is AvZ's update hook (`src/avz_hook.cpp` writes
`&__AScriptHook` to game address `0x667bc0`; the game calls that slot from
`0x54BACD`), and the runtime's overlay rewrites its body
(`runtime/avz_overlay.cmake`):

```
void __AScriptManager::ScriptHook() {
    if (!lvz::runtime::BeforeFrame()) return;      // runtime gate: CheckThread, IPC drain, ShouldStep
    RunTotal();
    if (!lvz::runtime::AfterAvzRunTotal()) return;
    if (!__aGameControllor.isUpdateWindow) return;
    if (!lvz::runtime::RunOneEngineFrame()) return; // controller-owned original update
    if (lvz::runtime::Started()) return;           // the controller owns all frame budgets
    while (__aGameControllor.isSkipTick() && ...) { ... }
}
```

`RunTotal()` reaches the script through exactly one branch:
`if (isLoaded) RunScript(); else LoadScript();`.

* `co_await ATime(wave, time)` does **not** use a tick runner. `__AWait::await_suspend`
  (`src/avz_coroutine.cpp`) calls `AConnect(_time, ...)`, which pushes into
  `__AOpQueueManager` (`src/avz_time_queue.cpp`), and the coroutine resumes from
  `__AOpQueueManager::RunOperation()`.
* Predicate waits (`co_await pred`) create an `ATickRunner` in `ONLY_FIGHT` mode.
* Both `RunOperation()` and the tick queues (`GLOBAL`, `ONLY_FIGHT`) are called
  from `__AScriptManager::RunScript()`, and `RunScript()` is called from
  `RunTotal()`, which the gate above must let through.
* The fight clock that makes an `ATime` due only moves inside
  `RunOneEngineFrame()` (`AAsm::GameTotalLoop` at `0x452650`, wrapped by
  `controlled_engine_call_v1`).

So while `recorder.dll` is resident, a hosted coroutine advances **only on frames
the controller grants**, at exactly the `ATime` offsets it asked for - which is
the behaviour P2 wants. Frame budgets, pause, the request journal and the
engine-call boundary stay with the runtime.

## 3. Enabling a hosted script

```
cmake -S . -B build/hosted -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_SYSTEM_NAME=Windows \
  -DCMAKE_C_COMPILER=.../i686-w64-mingw32-clang.exe \
  -DCMAKE_CXX_COMPILER=.../i686-w64-mingw32-clang++.exe \
  -DLVZ_AVZ_HOSTED_SCRIPT=logger/avz/hosted/jing_dian_12.cpp
cmake --build build/hosted --target recorder
```

* `LVZ_AVZ_HOSTED_SCRIPT` is empty by default: the shipped `recorder.dll` is
  unchanged (`build/cmake` build) and contains no hosted-script symbols.
* The named file must define `ACoroutine lvz::hosted::Script()`. Two are shipped:
  * `logger/avz/hosted/atime_probe.cpp` - three `co_await ATime(1, ...)` waits and
    a counter, used by the offline test.
  * `logger/avz/hosted/jing_dian_12.cpp` - the P2 script, body verbatim from the
    tutorial file with only the entry renamed.
* Verified: building with `jing_dian_12.cpp` produces a `recorder.dll` whose
  symbol table contains `lvz::hosted::Script()` plus its coroutine
  `.resume`/`.destroy` frames; the default build contains none of them.

Two behaviours of that script need a decision before the live run:

* `ASetZombies`/`ASelectCards` also configure the level, while the runtime's
  `initialize` request selects cards itself and refuses a list whose size does
  not match the seed slots. Keep them identical or drop them from the hosted
  copy.
* `aCobManager.Fire` is not a runtime action (next section).

## 4. What `aCobManager.Fire` really is

`src/avz_cob_manager.cpp`: `Fire()` -> `_BasicFire()` -> `AAsm::ReleaseMouse()`,
`AGridToCoordinate()`, `AAsm::Fire(x, y, cobIdx)`, `AAsm::ReleaseMouse()`.

`src/avz_asm.cpp` shows what `AAsm::Fire` is: it loads the App pointer from
`0x6a9ec0`, the Board from `+0x768`, the plant array from `+0xac`, indexes
`rank * 0x14c` (332 = `sizeof(APlant)`) and calls the engine routine at
`0x466D50` (`4615504`) with `(x, y)`. `ReleaseMouse` calls `0x40CD80`.

So a cannon shot is a **direct in-process engine call on the game thread**, not a
mouse event and not a raw memory poke:

* It must run on the game thread with a live Board. AvZ callbacks (and therefore
  a hosted coroutine) already run inside `ScriptHook`, so this holds.
* It does not need window focus, message pumping or the mouse, so it does not
  conflict with the runtime's headless/hidden-window model.
* `RecoverFire` defers through `AConnect(ANowDelayTime(delay), ...)`, i.e. the
  same operation queue that resumes `ATime` waits.
* It **bypasses the runtime's action set**: `plant`/`shovel`/`spawn` go through
  `logger/avz/recorder.cpp` and the request journal, while a hosted `Fire` is a
  single engine call with no request, no `expect` version and no journal record.
  Audit coverage for hosted cannons is therefore an open item; the options are
  (a) audit-only instrumentation of `_BasicFire` beside the existing
  `avz_smart.cpp` overlay hook, or (b) a first-class `fire` action in the runtime
  protocol, which would also make the fire order expressible without AvZ script
  code.

## 5. Offline evidence: what it proves and what it does not

`ctest -R avz_hosted_script` runs `tests/avz_hosted_script_tests.cpp`, which links
the generated overlay of the pinned `avz_script.cpp` (the same file
`recorder.dll` is built from), AvZ's coroutine/time-queue/tick-runner/logger
translation units and the probe script, against a fake PvZ image: AvZ's real App
cell `0x6a9ec0` plus a fake `APvzBase`/`AMainObject` filled with the documented
offsets. The target doubles are the runtime frame gate and `avz_game_controllor`
state, and `AScript()` is called the way `LoadScript()` calls it.

It asserts:

* a closed gate (`BeforeFrame()` false) advances neither the script nor the fake
  clock and registers no time connection;
* with the gate open, `ScriptHook -> RunTotal -> RunScript` resumes the probe
  coroutine at exactly `ATime(1, -599)`, `-549` and `-499` (clocks 2, 52, 102
  with the wave-1 refresh at clock 601) and the body finishes there;
* every granted frame produces exactly one engine call, and a paused stretch
  produces none.

It does **not** prove, and deliberately does not fake:

* the load-frame hook set (`MemoryInit` + `BeforeScript` hooks). Those call PvZ
  code at fixed RVAs - `AFieldInfo::_BeforeScript` calls `AAsm::GridToOrdinate`
  which executes `call *0x41C740`, and `AAsm::CanSpawnZombies`/`IsNight`/
  `IsRoof`/`HasGrave` are the same shape - so they can only run inside the game
  process. The test therefore starts from the state a completed load leaves
  behind.
* real engine behaviour: a real Board, real cannons, real wave refresh, real
  `AAsm::Fire`. The live acceptance run has to cover those.
* whether the 12-cannon script *wins* two flags. Hosting proves the script is
  driven; strategy and outcome are a live result.

Known harness limitation, recorded rather than hidden: raising AvZ's logger to
`INFO` inside the fake image faults in `AAbstractLogger::_CreateHeader` while it
formats its `[wave, time]` header, so the offline test keeps the production level
(`{ERROR, WARNING}`) and cannot use AvZ's own log lines as a second witness. The
same formatting path runs against a real App in production.

## 6. Open items

1. Live confirmation that a hosted coroutine advances with granted frames inside
   the real process (the offline test covers the mechanism, not the process).
2. Audit policy for hosted `Fire` (see section 4).
3. The `INFO`-logger fault above, which blocks using AvZ logs as evidence in the
   offline harness.
