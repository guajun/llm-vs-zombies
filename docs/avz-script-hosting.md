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
* The same build is wrapped by `tools/build-hosted.ps1`, which also records the
  DLL's SHA256 for the launcher (section 7).
* The named file must define `ACoroutine lvz::hosted::Script()`; define
  `void lvz::hosted::Observe(std::string&)` in it as well if the live run
  should report the script's state (section 7; a weak default publishes
  nothing). Two are shipped:
  * `logger/avz/hosted/atime_probe.cpp` - three `co_await ATime(1, ...)` waits and
    a counter, used by the offline test and the live observability probe.
  * `logger/avz/hosted/jing_dian_12.cpp` - the P2 script, body verbatim from the
    tutorial file with only the entry renamed. It has no state of its own, so it
    publishes nothing; its run file holds the activation line alone.
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
   The hosted build now writes what the script publishes into the run directory
   (section 7), so that confirmation can be read out of a live run instead of
   only asserted offline; running the game itself is still the acceptance step.
2. Audit policy for hosted `Fire` (see section 4).
3. The `INFO`-logger fault above, which blocks using AvZ logs as evidence in the
   offline harness.

## 7. 真机运行 / live run

托管构建把脚本编进 `recorder.dll`，真机流程与默认录制相同，只差一处：这个 DLL 的
SHA256 与默认构建不同，而 run 的 `manifest.json` 绑定的是建 run 时
`build/recorder.dll` 的哈希（`tools/inject-recorder.ps1`、`launcher` 都会复核），所以
**先构建托管 DLL，再建 run**；DLL 换过一次就换新 run。

### 7.1 构建

```powershell
.\tools\build-hosted.ps1 -Script logger\avz\hosted\atime_probe.cpp
```

* 默认产出 `build\recorder.dll`；`-OutputDirectory build\hosted` 写到别处，脚本会打印
  需要补的 `Copy-Item ... build\recorder.dll`。
* `-BuildDirectory` 默认 `build\hosted-<脚本名>`，不碰默认构建树 `build\cmake`；`-Jobs`
  控制并行度。
* 脚本打印并落盘 DLL 的 SHA256：`<输出目录>\recorder.sha256`（sha256sum 格式）和
  `<输出目录>\hosted-build.json`（脚本源码路径及其 SHA256、DLL SHA256、构建树、CMake
  参数）。launcher 校验的就是 `manifest.implementation.recorder_sha256` 的那一个哈希。

### 7.2 启动

```powershell
.\launcher\build.ps1                                   # 需要时先构建 launcher 助手
.\tools\launch-experiment.ps1 -Name hosted-atime-01    # 建 run：复制私有环境、注入、初始化
```

每次换新 `-Name`；`-NoInitialize` 只启动不初始化。结束实验照常按 7 停止录制。

### 7.3 观测文件

`<run>\decisions\hosted-script.jsonl`：每行一个事件信封（与 `events.jsonl` 同一形状），
`phase` 为 `hosted_script`。放在 `decisions/` 而不是运行根目录，是因为封存策略
（`src/llm_vs_zombies/records.py` 的 `archive_policy`）只收白名单目录和运行根 `*.json`：
运行根下的 `*.jsonl` 会静默落在封存包外，`decisions/` 下的文件则随封存包一起被哈希。

```json
{"schema_version":1,"run_id":"hosted-atime-01","seq":0,"segment":0,"tick":0,"phase":"hosted_script","kind":"hosted_script_active","payload":{"script":"atime_probe.cpp"}}
{"schema_version":1,"run_id":"hosted-atime-01","seq":1,"segment":0,"tick":1,"phase":"hosted_script","kind":"hosted_script_state","payload":{"script":"atime_probe.cpp","started":1,"resumes":0,"resume_wave":0,"resume_time":0,"finished":0,"clock_at_finish":0}}
```

* `seq` 只数本文件的行；`script` 是编译进 DLL 的源文件名（`kind:hosted_script_active`
  行证明跑的是哪个托管源）。
* `tick` 是**产生该状态的那一帧**：采样发生在 AvZ `RunTotal()` 的 AfterTick，也就是
  `RunScript()` 处理完这一帧之后，所以 `co_await ATime(...)` 在某一帧恢复，其 tick 就是
  那一帧的 tick，不是下一帧。
* 只在脚本发布的文本发生变化时写行；相同的帧不重复写。被 controller 停住（暂停、未授予
  帧）时 `RunTotal()` 根本不会运行，因此不会有行——文件不长就说明没有被推动。
* 写入走与 `events.jsonl` 同一个 `BufferedWriter`，但**每写一行就 flush**（#88 之前是 64 KiB
  批量、只在段边界落盘，真机崩溃时留下的是 0 字节文件）。不新增线程、不引入新的同步或时序
  依赖；代价是 game 线程每帧最多一次小写入，只在状态变化时发生。
* 默认构建（没有 `LVZ_AVZ_HOSTED_SCRIPT`）里这条路径整条不存在：`hosted_observation.cpp`
  不参与编译，AfterTick 采样钩子也不注册。

### 7.4 atime_probe 怎么读

`logger/avz/hosted/atime_probe.cpp` 的三次等待固定为 `ATime(1, -599)`、`-549`、`-499`：

* `resume_time` 就是每次完成的等待偏移，`resumes` 依次变成 1、2、3（同一行里的 `tick`
  就是那次恢复所在的帧），`resume_wave` 恒为 1。
* 脚本跑完时 `finished=1`，`clock_at_finish` 是那一刻的 `GameClock()`。
* 离线测试看到的绝对 tick（2/52/102）来自假图里 wave-1 刷新时间 601；真机上第一波刷新
  时间由实际游戏决定，因此要看的是顺序、`resume_time` 序列和 `finished/clock_at_finish`，
  不是那三个具体数字。

```powershell
Get-Content <run>\decisions\hosted-script.jsonl | Select-String 'hosted_script_state'
```

### 7.6 首异常记录与协程等待修复（#88）

托管构建还会写 `<run>\decisions\hosted-crash-probe.log`：`logger/avz/hosted_crash_probe.cpp`
在 DLL 加载时注册一个**向量化异常处理器（VEH）**，把每个 first-chance 异常的 code、地址、
模块+RVA、线程号、以及 recorder 最近一帧的 `tick/segment` 与脚本发布状态写进去，每行
`WriteFile` + `FlushFileBuffers`。处理器只观测、不处理（永远 `EXCEPTION_CONTINUE_SEARCH`），
游戏与 AvZ 的处理路径与之前逐字节相同；`tests/hosted_crash_probe_tests.cpp` 用一个真的触发
访问违例的子进程验证"行已落盘 + 进程仍以同一个异常码退出"，默认构建则验证不生成任何文件。

用这份证据定位到的根因（#88）：`avz_coroutine.cpp` 的 `__AWait::await_suspend()` 结尾是
`if (!AConnect(_time, func)) func();`，而 `__AOpQueueManager::Push()` 在自己判定"时间已到"
时**已经运行过一次**该操作并返回 `nullopt`，于是同一次 `co_await` 会恢复协程两次：一次在
`await_suspend` 内部（此时 `AWaitForFight()` 正把游戏从选卡界面推进战斗，`ANowTime` 从
UNINIT 变成"已到"），一次在它返回之后。第二次恢复让协程跑过下一个 `co_await`，而那个等待
的操作仍留在队列里；协程体结束时栈帧被销毁（`final_suspend()` = `suspend_never`），残留操作
到期后对已释放的协程帧调用 `resume()` → 0xc0000005，随后 AvZ 自己的 `ASeh::DoHandleDebugEvent`
也崩在里面（所以没有 crash.txt）。

`runtime/avz_overlay.cmake` 因此对 `avz_coroutine.cpp` 做了一处 overlay（子模块保持原样、
哈希仍然钉住）：在 `await_suspend` 里先判一次"是否已到"，已到就只运行一次 `func()`，否则
才走 `AConnect`，失败再运行一次。真机验证：修前 `hosted-c1` 在 tick 3251 崩、观测是
`resumes 0→2→3`；修后 `hosted-c5`（源跑 + 冷重放）都在 3151/3201/3251 各恢复一次
（`resumes 1→2→3`、`finished=1, clock_at_finish=3251`），crash-probe 里只有游戏自己的
`0x40010006`（OutputDebugString）记录，没有访问违例。

### 7.5 验证默认构建不受影响

```powershell
.\tools\build-avz.ps1
Test-Path <run>\decisions\hosted-script.jsonl     # 默认构建录制后必须为 False
& third_party\llvm-mingw-20260908-ucrt-x86_64\bin\llvm-nm.exe build\recorder.dll |
    Select-String hosted                           # 默认构建必须没有 hosted 符号
```

离线证据（不需要游戏）：

* `ctest -R hosted_observation_default` —— 与托管构建同一份 `hosted_observation.cpp`，按
  默认构建方式编译：`Open/Sample/Flush/Close` 全是空操作，run 目录连 `decisions/` 都不会多。
* `ctest -R hosted_observation` —— 托管模式下的信封、`kind`、`tick`/`seq`、只写变化。
* `ctest -R avz_hosted_script` —— 帧归属与 `Observe` 发布内容（同时证明脚本里的强定义
  覆盖 `observe_default.cpp` 的弱定义）。
* `python -m unittest discover -s tests -p test_avz_hosted_script.py` —— `recorder.cpp` 里对
  观测文件的调用、CMake 里新增的源文件与测试目标都必须落在 `LVZ_AVZ_HOSTED_SCRIPT` 开关内。
