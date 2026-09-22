# libTAS + AvZ 方案调研：能替掉什么，还剩多少轮子要自己造

核查日期：2026-09-20。状态：**静态调研**，依据 libTAS 官方文档与源码、项目 issue、TASVideos 文档以及本仓库现有实现；**未在 Wine 下启动 PvZ，未注入，未验证任何确定性结论**。下文的“可替代/仍需自研”是能力层面的判断，不能当实机通过结论使用。

libTAS 基准：版本 1.4.8（2026-05-14），源码 `clementgallet/libTAS@49033fdabca8dee587ce1ed732ef7f3ccafbffe7`（2026-08-11）。

## 1. 结论

1. **libTAS 是 GNU/Linux 工具，不是 Windows 工具。** 它用 `LD_PRELOAD` 把 `libtas.so` 注入游戏进程，通过 Unix socket 与 GUI 进程通信；对 Windows 游戏只能“用 Wine 启动 `.exe`”。官方 FAQ 的原话是：能不能 TAS Windows 游戏？“Short answer: no, unless it has a Linux release.”；README 把 Wine 支持标注为 **limited and not being actively developed**。[S1][S2]
2. **在我们现在的 Windows 主线上，libTAS 基本用不上。** 它对 Wine 的干预靠补丁 Wine 自身的 `.so`（`ntdll`、`user32`、`kernel32`、`wined3d`），完全不 hook PE 内部。PvZ 引擎自己的 MT19937、静态 CRT 随机，以及我们 `determinism/` 已经覆盖的每帧状态，都在 PE 里面，libTAS 看不到。
3. **如果迁到 Linux + Wine，libTAS 能直接替换或显著节省 5 类轮子**：帧边界与逐帧推进、输入录制/回放（`.ltm`）、时间源虚拟化（`GetTickCount`/QPC 等）、音视频编码，以及一项我们**完全没有**的能力——同进程 savestate。
4. **它不能替换本项目的核心部分**：语义动作执行（AvZ）、逐帧完整状态审计与首个分叉定位、跨进程冷启动等价性、证据封存与身份绑定、评测门槛与报告。TAS 意义的“sync”（录像能播完、画面一致）不等于我们要求的“每帧规范化状态逐字节相等”。
5. **迁到 Wine 还会新增约 8 项适配轮子**（见第 5 节）：Wine/PvZ 可运行性、帧定义对齐、双层 hook 所有权、AvZ 注入方式、IPC 传输、外部控制器协议、并行 worker 隔离、savestate 正确性验证。其中帧定义、savestate 两项是高风险研究项，不是接线工作。
6. 综合建议：**保持现有 Windows 主线（方案 C）**，把 libTAS 当作三样东西用——时间 hook 的现成参考实现、帧边界与“快速转发必须绘制”的外部佐证、savestate 的设计参考。只有当“回档/秒级分支”成为硬需求、且 Wine + savestate 的 PoC 真的通过时，才值得考虑方案 A。

## 2. libTAS 实际能力（含出处）

### 2.1 运行位置与启动方式

- 结构是“GUI 程序 `libTAS` + 被注入的 `libtas.so`”，两者用 Unix socket 通信；游戏由它自己 `fork()`/`exec()` 启动，**没有 attach 到已运行进程的选项**。[S3]
- 帧边界握手时，游戏进程停在渲染调用里等对方回包（“Send inputs for next frame → Send end of frame → Return from Render() call”），所以**阻塞 libTAS 一侧就等于阻塞游戏线程**。[S3]
- 命令行已具备自动化入口：`-n/--non-interactive`（无交互，可 headless）、`-r/--read MOVIE`、`-w/--write MOVIE`、`-d/--dump FILE`、`-l/--lua FILE`、`-s/--set KEY=VALUE`。[S4] 这一项有价值：不需要自己写无头外壳。
- 配置在 `$XDG_DATA_HOME/libTAS` 或 `$HOME/libTAS`，[S9] 并行多实例可以用每 worker 独立的 `HOME`/`XDG_DATA_HOME` 隔离，但要实测。

### 2.2 “帧”的定义

libTAS 把**一次窗口呈现调用**当作一帧的结束，hook 点包括 `SDL_GL_SwapWindow`/`SDL_GL_SwapBuffers`、`SDL_RenderPresent`、`SDL_Flip`、`SDL_UpdateRect`、`SDL_UpdateWindowSurface`、`glXSwapBuffers`、`eglSwapBuffers`、`vkQueuePresentKHR`、`XShmPutImage`、`VdpPresentationQueueDisplay`，外加 `DeterministicTimer` 与忙等检测触发的**非绘制帧**。[S3][S5]

官方文档自己列的限制，对本项目全部成立：[S3]

- **非绘制帧**（靠 sleep/等时间推进出来的帧）在同一台机器上可能稳定，换系统、换 libc/驱动就不一致；
- **一个游戏逻辑帧 ≠ 一次绘制**，PvZ 用 `ASkipTick` 跳绘制时更明显；
- 快速转发默认跳过绘制，而本项目 #10 已实测“原版绘制会写入动画字段”，即**绘制调度本身影响模拟状态**。libTAS 有 `FF_RENDER_ALL` 一类选项可以强制不跳绘制，但这条耦合必须写进双方契约。

### 2.3 确定性手段与实际边界

- **时间**：主线程的 `clock_gettime`/`gettimeofday`/`time`，以及 **Wine 侧的 `GetTickCount`、`GetTickCount64`、`QueryPerformanceFrequency`、`QueryPerformanceCounter`** 都被接管，时间只在帧边界按 1/fps 推进；另有 sleep / wait / “时间追踪”（同一帧内时间查询次数到阈值就推进时间）三种补充机制。[S3][S8]
- **外部熵**：`/dev/urandom` 被换成由 `initial_time_sec` 播种的 xorshift64* 伪随机管道，`getrandom` 同源，`/proc/uptime` 等做假。[S3][S7]
- **未初始化内存**归零，对“同一进程多次启动结果不同”这类问题有效。
- **一个常见误解**：libTAS 的 `rand`/`srand`/`random`/`rand48` 包装**只记录日志，不改变返回值**（`randomwrappers.cpp` 里走的是 `RETURN_NATIVE`）。它让游戏自带 PRNG 可复现，靠的是掐掉外部熵源，不是替游戏决定随机数。PvZ 的全局 MT 与静态 CRT 状态需要我们自己捕获/恢复，这块 libTAS **不提供任何帮助**。
- **线程**：官方原话是线程是“最严重的不确定性来源，libTAS 基本无法解决”，只对 Unity 等特定引擎有同步 hook。[S3] 本项目 runtime 的“只有游戏线程碰游戏内存、通信线程不直接调用游戏函数”反而是更强约束。

### 2.4 输入

Linux 原生路径覆盖 SDL1/2/3、xlib、xcb、evdev、jsdev 等；**Wine 路径覆盖面很窄**，源码里只有 `user32.dll.so` 的 `GetCursorPos`、`ScreenToClient`、`GetAsyncKeyState` 三个补丁，加上键盘布局表。[S8] 已知缺陷 issue #249 直接写着“Wine 下输入处理不确定，会丢输入或保持按键按下”。[S10]

对我们的意义：**AvZ 的语义动作根本不走输入路径**，所以输入录制再完善，也录不到 `plant()`/`shovel()` 这类进程内调用。反过来，如果为了用 libTAS 改成纯鼠标键盘驱动，就等于放弃 AvZ 的动作/观察层，把已经验证过的失败语义、动作结果和版本校验全部重做。

### 2.5 savestate（我们唯一真正缺的能力）

机制是进程级内存快照：挂起其他线程（信号 + `getcontext` + altstack）、导出 `/proc/self/pagemap` 的内存段、记录打开的文件描述符与管道内容、导出内存中的虚拟存档文件。[S3]

官方明列的限制：

- **共享内存段不保存**（文档原文标注为 TODO）；不可写段、特殊段也跳过；
- X11 连接不能保存，只能在加载后“续上”，因此**跨进程重启恢复通常不可用**；
- 要让 GPU 状态可快照，必须开 “Force software rendering”（Mesa llvmpipe），性能大幅下降；
- 需要停止音频播放，否则驱动里残留的样本会影响存档。[S2][S3]

对 Wine 场景这些限制是叠加的：`wineserver` 是**另一个进程**，它的同步对象、句柄表、注册表/窗口状态都不在这份内存快照里。issue #278 就是 Wine 游戏在 savestate/音频路径上崩溃的实例。[S11] 所以“libTAS 给我们回档能力”这句话必须先用 PoC 证明，不能先写进设计。

### 2.6 Lua 与对外接口

Lua 运行在 **GUI 进程**里（不是游戏进程），用 `luaL_openlibs` 打开标准库，因此 `io`、`os`、`package` 可用；提供 `memory.read*`/`write*`、`memory.baseAddress`、`runtime.saveState/loadState/loadBranch`、`runtime.sleepMS`、`movie.currentFrame/frameCount/isDraw`、`input.set*`，以及 `callback.onStartup/onInput/onFrame/onPaint`。[S6][S12]

理论上可以：Lua 在 `onFrame` 里阻塞等外部文件/FIFO/自定义模块给出动作 → 读内存组装观察 → 写内存或注入输入 → 放行下一帧。**代价**是观察/动作的语义层要在 Lua 里重写（等于重写 AvZ + 我们的状态解码），阻塞会卡住 libTAS 主循环，且 libTAS 内部 socket 协议是私有且随版本变化的。[S6][S12]

### 2.7 视频与音频

在帧边界做窗口像素采集，交给 FFmpeg 编码，音频在内部混音后一起输出，`-d` 直接指定输出文件。[S3][S4] 这能替代我们 `recording/` + `src/llm_vs_zombies/video.py` 的采集编码链，但**采集对象不同**：我们抓的是引擎绘制表面（原画，已声明与桌面截图无关），libTAS 抓的是窗口呈现结果，OSD/HUD/窗口遮挡都会进去。这属于等价类变更，要写进 manifest，不能悄悄替换。

### 2.8 Wine 支持的实际覆盖面（源码级清单）

`master@49033fd` 中所有 Wine 相关补丁点如下：

| 模块 | 补丁的函数 | 触发条件 |
|---|---|---|
| `ntdll.dll.so` | `LdrGetProcedureAddress` | 总是 |
| `user32.dll.so` | `GetCursorPos`、`ScreenToClient`、`GetAsyncKeyState` | 总是 |
| `kernel32.dll.so` | `GetTickCount`、`GetTickCount64`、`QueryPerformanceFrequency`、`QueryPerformanceCounter` | 总是（`WaitForMultipleObjectsEx` 已注释停用） |
| `wined3d.dll.so` | `wined3d_texture_get_resource`、`wined3d_swapchain_present`、`wined3d_resource_map` | 仅当启用 `GC_SYNC_WITNESS` |

来源：`src/library/wine/*.cpp`。[S8] 清单里**没有任何 `ddraw`、`dinput`、`dsound` 补丁**：PvZ 1.0.0.1051 是本机锁定的 DirectDraw/DirectSound 32 位 PE，能否被 libTAS 认出帧边界，取决于 Wine 把 `ddraw` 的呈现最终落到哪个后端（落到 `wined3d` → OpenGL 会命中 `glXSwapBuffers`；若落到 GDI 路径，就只能靠“非绘制帧/时间追踪”推进，同步风险显著上升）。这是第一号必须实测的未知。

## 3. 与本仓库现状的对照

本仓库当前代码规模（本机统计，不含上游 AvZ 子模块的 `src/`）：

| 目录 | 文件 | 行数 | 职责 |
|---|---|---|---|
| `runtime/` | 12 | 23,884 | 常驻 IPC、原子动作、精确帧预算、去重、绘制调度、终局处理 |
| `src/llm_vs_zombies/` | 22 | 8,532 | 客户端、REPL、视频、引擎重放、评测、审计比较 |
| `tests/` | 48 | 9,217 | Python 与原生验收 |
| `determinism/` | 21 | 2,533 | 目标签名、RNG、每帧审计、出生钩子、粒子 |
| `launcher/` | 7 | 553 | 挂起启动、注入、用户档隔离、隐藏窗口 |
| `recording/` | 5 | 415 | 原版绘制表面采集 |
| `logger/` | 3 | 238 | AvZ 侧结构化日志 |

逐项判断：

| 能力 | libTAS | 本仓库现状 | 结论 |
|---|---|---|---|
| 帧边界与逐帧推进 | 有（定义在呈现调用） | `runtime/` 定义在原版更新前后 | 可替代，但需对齐 |
| 暂停等待外部决策 | 有（帧边界阻塞） | `pause`/`commit` 带版本与请求 ID | 部分替代 |
| 输入录像/回放 | 有（`.ltm`） | 轨迹 + 动作语义 + 请求 ID 映射 | 只覆盖输入驱动路径 |
| 确定性时间源 | 有（含 Wine 的 kernel32 四个函数） | 已恢复 Board/App 时钟，未接管 Win32 时间源 | 可直接借鉴/移植 |
| 游戏自身 RNG 状态 | 无 | MT19937 624 项 + 游标 + CRT 状态捕获/恢复 | 仍需自研 |
| 每帧状态审计与分叉 | 无 | `determinism/`、`audit_compare.py`、首差异 JSON Pointer | 仍需自研 |
| savestate/回档 | 有（同进程，限制多） | 无，只有“从起点重算”和 AvZ `.dat` 存档 | 唯一的大块净收益 |
| 视频/音频编码 | 有（窗口像素 + FFmpeg） | 引擎绘制表面 + FFmpeg 流式 | 可替代，等价类不同 |
| 证据封存/身份绑定/评测门槛 | 无 | `evidence_codec.py`、manifest、SHA-256 封包 | 仍需自研 |
| 语义动作执行 | 无 | AvZ + 受控更新 hook | 仍需自研 |
| 用户档隔离与隐藏窗口 | 无（Wine 前缀可部分替代） | 启动器 + 私有档 + 隐藏窗口 | 部分替代 |
| 并行冷启动 worker | 无 | `cold_workers.py`（Windows 专用） | 仍需自研 |

结论很直白：**libTAS 直接替掉的，按代码量算大致是 `recording/` + 部分 `runtime/` 帧推进 + 视频编码，约 5% 量级；另外三项（时间源、savestate、无头 CLI）是它替我们省下的“本来要新造”的东西。** 其余约 95% 是语义、审计、证据和评测，不会因为引入 libTAS 而消失。

## 4. 三种组合架构

### 方案 A：libTAS 当外层执行引擎（Linux + Wine + AvZ）

libTAS 负责启动、帧推进、时间虚拟化、录像、编码、可选 savestate；AvZ + 本项目 `runtime` 仍留在 PE 内做语义动作与观察；中间加一层 Linux 侧桥接。

必须最先解决的冲突是**帧所有权**。libTAS 的帧边界在呈现调用内部，此时游戏线程正停在 libTAS 代码里；我们的 `pre_step`/`post_step` 在原版更新边界，且 `deterministic_draw_schedule_v1` 已经把“一次原版绘制”算进受控更新。两者不能各自宣称“这一帧的边界”，否则动作执行点、暂停点、审计点会错位。可行做法是：以 libTAS 呈现点作为**外层门控**，以我们的更新钩子作为**内层语义边界**，并显式记录映射关系（类似现有 `epoch`/`revision` 的映射），而不是假设一一对应。

### 方案 B：完全改用 libTAS 的输入/内存/Lua

放弃 AvZ，用 Lua + 内存偏移直接读写游戏。等于重写语义层、状态解码、失败语义和目标校验，并失去现有审计能力。**不推荐**，除非目标改成“给 TASVideos 投稿”。

### 方案 C：不引入 libTAS，只借方案

保持 Windows 主线，把四项从 libTAS 搬到自己的 hook 里：

1. **时间源清单**：把 `GetTickCount`/`GetTickCount64`/`QueryPerformanceCounter`/`QueryPerformanceFrequency` 纳入受控时间（我们现在只处理 Board/App 时钟与浮点环境）；
2. **帧边界的判定经验**：呈现调用作为边界、非绘制帧显式标注、快速转发不得跳过绘制；
3. **savestate 设计参考**：如果要做 Windows 侧回档，参考“挂起线程 + 内存段快照 + 打开句柄/管道内容 + 虚拟存档文件”的清单；Windows 侧的同类成熟做法是 Hourglass 那一路（32 位原生游戏、帧推进、savestate、输入录像、AVI 导出），[S13] 值得单独评估；
4. **`.ltm` 的记录结构**：帧索引输入 + 环境元数据 + 分支注释，与我们轨迹格式的字段做一次对齐。

## 5. 仍需自研/移植的轮子清单

“新增”= 走 libTAS 路线上从零要做；“移植”= 本仓库已有、换环境后要重做接线与验收。

| # | 轮子 | 为什么 libTAS 不提供 | 规模 |
|---|---|---|---|
| 1 | Wine/Proton 环境与 PvZ 可运行性（ddraw→wined3d、窗口/焦点、音频、软件渲染） | 它只是 hook 层，不保证游戏能跑 | M（前置 gate） |
| 2 | 帧定义对齐（呈现边界 ↔ 原版更新边界、`ASkipTick`、非绘制帧、快速转发） | 它按自己的定义走 | M（高风险） |
| 3 | 双层 hook 的所有权与初始化顺序（PE 内 AvZ/runtime ↔ .so 侧 libTAS） | 不涉及 | M |
| 4 | AvZ 在 Wine 下的注入（`AppInit_DLLs`/注入器等候选；libTAS 自己 fork/exec，不能再挂起启动） | 它不注入第三方 PE DLL | M |
| 5 | 传输层改造：现有客户端明确拒绝非 Windows 命名管道 | 无关 | S/M（移植） |
| 6 | 外部控制器协议与帧门控（Lua 阻塞或文件/socket、请求 ID、超时、断连保持暂停） | 它只有 GUI/CLI/私有 socket | M |
| 7 | 逐帧状态审计与首分歧定位 | 它不做游戏语义审计 | L（移植 + 重新验收） |
| 8 | 目标身份与初始化配方（exe/DLL/存档/FPU/种子/时钟，再加 Wine/Mesa 版本） | 无关 | M（移植） |
| 9 | 隔离与并行 cold worker（`WINEPREFIX` + 每 worker `HOME`/`XDG_DATA_HOME`；libTAS 是 GUI 程序） | 无关 | M |
| 10 | 证据封存/评分门槛/报告 | 无关 | L（移植） |
| 11 | 视频语义（窗口像素 vs 引擎绘制表面、OSD、捕获扰动） | 采集对象不同 | S/M |
| 12 | savestate 正确性验证（跳共享内存段、`wineserver` 状态、音频、X 连接） | 这正是要验证的 | M/L（高风险） |
| 13 | 版本锁定与维护（libTAS/Wine/Mesa/内核 glibc；GPLv3 改动的开源义务） | 无关 | S（持续） |

合计：**可直接替换 5 项，仍需自研或移植 13 项**；其中 5 项属于把已有代码换环境（以移植成本为主），8 项是新工作，第 2、12 项决定这条路走不走得通。

## 6. 最小验证顺序（PoC）

每一步先写清通过/失败判据，失败就停，不要带着失败继续往上叠。

| 步骤 | 内容 | 通过判据 |
|---|---|---|
| P0 | WSL2/Ubuntu + `wine32` 直接跑 PvZ 1.0.0.1051，不用 libTAS | 窗口化进入雾夜场景，无崩溃，能加载参考存档 |
| P1 | `libTAS -n` 启动同一 exe，观察帧计数与逐帧推进 | 帧计数稳定递增；用 AvZ 或内存读确认“一帧 ↔ 一个 `GameClock` 步”的映射（含 `ASkipTick` 情况） |
| P2 | 时间源实验：`Uncontrolled time` 开关对比 | 关闭后时间查询不再影响模拟；两次运行的 RNG 消耗序列相同 |
| P3 | AvZ 注入与 IPC | AvZ hook 安装成功；确认现有命名管道**不能**被 Linux 侧访问（预期失败→决定改造传输层） |
| P4 | 同进程 savestate 10 次 | 每次加载后与连续运行的逐帧审计状态一致；无崩溃、无音频/X 连接错误 |
| P5 | 2 次冷启动 + 录像重放 | 全程逐帧状态相等（不是“能播完”） |
| P6 | 只有 P1–P5 全通过，才进入“是否迁移主线”的决策 | — |

在 P4/P5 通过之前，**任何“libTAS 帮我们省掉回档/重放系统”的说法都不成立**。

## 7. 已知风险

- Wine 路径官方标注“未在积极开发”，且明确不建议；已知输入不确定（#249）、崩溃（#278）。[S1][S10][S11]
- savestate 跳过共享内存段，`wineserver` 状态不在快照内；跨进程恢复官方也说不靠谱。[S2][S3]
- PvZ 的随机性在 PE 内部，libTAS 的 Linux 侧 RNG 包装覆盖不到；这部分我们的 `determinism/` 已经做了，迁移后要重新验证。
- libTAS 需要 X11/WSLg（或 Xvfb），而当前“隐藏窗口”的前提是保留原版 Win32/DirectDraw 初始化；Wine 下的窗口与隐藏语义重新洗牌。
- 内核/glibc 兼容成本是持续的：源码里就有为 Linux 7.0.10 的 `rseq` 问题打 `GLIBC_TUNABLES=glibc.pthread.rseq=0` 的 workaround。[S14]
- 若要改 libTAS 本体，GPLv3 修改需要开源，并跟着上游与内核变化走。本项目同为 GPL-3.0，许可上没有冲突。

## 8. 备选线索

Hourglass（`TASEmulators/hourglass-win32`）是面向 32 位 Windows 原生游戏的 TAS 工具，能力覆盖帧推进、savestate、输入录像、RAM watch、AVI 导出，[S13] 与本项目“Windows + 原版 PE + AvZ”的组合更贴近，也不引入 Wine。它同样不提供游戏语义审计；能否与 AvZ 的 hook 共存、savestate 是否覆盖我们 DLL 的状态，需要单独调研。本文只作线索列出，不作结论。

## 9. 来源

- [S1：libTAS README（平台、Wine 支持状态、构建方式）](https://github.com/clementgallet/libTAS/blob/master/README.md)
- [S2：libTAS FAQ（Windows 游戏、savestate 与重启恢复、软件渲染）](https://clementgallet.github.io/libTAS/faq/)
- [S3：How does it work（帧定义与限制、确定性、时间、输入、savestate 全流程）](https://clementgallet.github.io/libTAS/guides/how/)
- [S4：libTAS 命令行选项（`src/program/main.cpp` 的 usage）](https://github.com/clementgallet/libTAS/blob/master/src/program/main.cpp)
- [S5：帧边界调用点（`src/library/frame.cpp` 及各 wrappers）](https://github.com/clementgallet/libTAS/blob/master/src/library/frame.cpp)
- [S6：Lua 函数与回调](https://clementgallet.github.io/libTAS/guides/lua/)
- [S7：`src/library/fileio/URandom.cpp`、`src/library/general/randomwrappers.cpp`（熵源替换与 RNG 只记录不替换）](https://github.com/clementgallet/libTAS/blob/master/src/library/fileio/URandom.cpp)
- [S8：`src/library/wine/`（ntdll/user32/kernel32/wined3d 的全部补丁点）](https://github.com/clementgallet/libTAS/tree/master/src/library/wine)
- [S9：`src/program/Config.cpp`（配置目录 `$XDG_DATA_HOME/libTAS`、`$HOME/libTAS`）](https://github.com/clementgallet/libTAS/blob/master/src/program/Config.cpp)
- [S10：libTAS issue #249 —— Wine 输入处理不确定](https://github.com/clementgallet/libTAS/issues/249)
- [S11：libTAS issue #278 —— Wine 游戏 savestate/音频崩溃报告](https://github.com/clementgallet/libTAS/issues/278)
- [S12：`src/program/lua/Main.cpp`（`luaL_openlibs` 与注册的函数表）](https://github.com/clementgallet/libTAS/blob/master/src/program/lua/Main.cpp)
- [S13：TASVideos —— Hourglass（32 位 Windows 游戏的帧推进、savestate、输入录像、AVI 导出）](https://tasvideos.org/EmulatorResources/Hourglass)
- [S14：`src/program/GameThread.cpp`（Wine 启动参数与 `rseq` workaround）](https://github.com/clementgallet/libTAS/blob/master/src/program/GameThread.cpp)
- 本仓库：[原版确定性重放器提案](原版确定性重放器提案.md)、[PvZ 录制与回放方案](PvZ录制与回放方案.md)、[确定性审计适配器](determinism.md)、[原版动作重放](engine-replay.md)、[运行时协议](runtime-protocol.md)
