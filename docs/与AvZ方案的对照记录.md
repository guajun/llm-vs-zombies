# 与"仅 AvZ"方案的对照记录

日期：2026-09-23。范围：issue #51（场景脚本与文档部分）。对照对象是**仅 AvZ 方案**：AvZ 固定提交
`c42676c2`（完整 SHA `c42676c269b5b482a1eb9203a5b979e9d8a2a5c7`，本仓库子模块 `avz/framework`），
以及仓库内 PvZ 1.0.0.1051 反编译副本 `work/replay-research/`（既有文档统一把它当"源码"证据源）。

标注约定：每条结论行末尾带一个方括号标记，内容为"已引证"或"待核实"。"已引证" = 本文给出了
可复核的源码或归档位置；"待核实" = 本次没有核到的部分，不能当结论用。本文不启动游戏、不新跑实验，
引用的实测数据都来自既有归档。AvZ 侧路径相对 `avz/framework/`；本仓库结论引 `docs/`、`src/`、
`experiments/`、`work/replay-research/`。

## 0. 摘要

AvZ 的回放与接管是**存档级**能力：它靠"每 N 帧存一次整块 Board 存档、回放时把它读回来"实现，
存读档不覆盖全局随机流、CRT 随机数状态、`mNextKey` 与 Board 之外的 app 级状态，而且读档动作本身
会消耗随机数。本仓库的分支方案把"同根 + 同动作序列 ⇒ 逐帧同轨迹"写成 P1 合同，用**从根重算 +
全程审计**替代读档快进，并在既有运行时验证里拿到 `equal=true`。两边不是同一种等价性：
AvZ 给的是"阵型看起来回去了"，本方案给的是"这一步之后谁读了什么都可复核"。

## 1. AvZ `AReplay` 的存读档语义

### 1.1 默认参数

- 默认每 10 帧记录一次：`StartRecord(int interval = 10, int64_t startIdx = 0)`，成员默认
  `int _recordInterval = 10;`，教程同文写"默认每 10 帧记录一次"（`inc/avz_replay.h:114`、`:215`、
  `tutorial/28_replay.md:12`）。 `[已引证]`
- 默认最多保留 2000 帧存档：`int64_t _maxSaveCnt = 2000;`，`SetMaxSaveCnt` 注释写
  "如果不使用此函数，回放对象的默认值为 2000"，教程重复一次（`inc/avz_replay.h:154-155`、`:217`、
  `tutorial/28_replay.md:52`）。 `[已引证]`
- 存档按 100 帧一个包组织，跨包文件名是 `(_endIdx % _maxSaveCnt) / _packTickCnt` 与
  `_endIdx % _maxSaveCnt % _packTickCnt`，超出上限后 `_startIdx = _endIdx - _maxSaveCnt + 1`
  环形覆盖旧帧（`inc/avz_replay.h:160-161`、`src/avz_replay.cpp:231-234`）。 `[已引证]`
- 存储成本由教程给出参考值："1GB 大概可以容纳 1600 帧存档，如果 10 帧一存"（`tutorial/28_replay.md:201`）。 `[已引证]`

### 1.2 实现方式：反复存档读档，不是内存快照

- 教程原文："因为回放功能本身是借助疯狂存档读档实现的"（`tutorial/28_replay.md:184`）。 `[已引证]`
- 记录侧：`AReplay::_RecordTick()` 在 `clock % _recordInterval == 0` 时删除同名 `.dat` 后调用
  `AAsm::SaveGame(filePath)`，最后 `++_endIdx`（`src/avz_replay.cpp:206-258`，存盘调用在 `:246`）。 `[已引证]`
- 播放侧：`StartPlay` 先 `SavePvzState()`（`src/avz_replay.cpp:493-513`），`_PlayTick` 每
  `_playInterval` 帧调一次 `ShowOneTick(tmpPlayIdx)`，`ShowOneTick` 最终调用
  `AAsm::LoadGame(filePath)` 把那一帧的 `.dat` 读回来（`src/avz_replay.cpp:402-443`、
  `ShowOneTick` `:296-339`、`_LoadPvzState` `:673-690`；读盘调用在 `:325`、`:335`、`:681`）。 `[已引证]`
- 存档文件之外还有一份外部输入表：`_RecordTick` 同时记录鼠标坐标/按下类型、`MjClock` 与女仆秘籍
  相位到 `tickInfo.dat`（`src/avz_replay.cpp:206-230`、`_SaveTickInfo` `:627`、`_LoadTickInfo` `:644`），
  `_ShowTickInfo` 再用它写回 `MjClock`/相位并重放鼠标按下与抬起事件（`src/avz_replay.cpp:446-460`）。
  说明 **`.dat` 单靠自己不足以复现输入**。 `[已引证]`
- `SetInterpolate` 默认开：`bool _isInterpolate = true;`（`inc/avz_replay.h:225`）。补帧开启时，
  两次落盘帧之间的 tick 不读档，而是让**真实游戏继续跑** `AAsm::GameTotalLoop()`，并手改
  `RefreshCountdown`（`src/avz_replay.cpp:412-425`）。这正是"补帧 → 数据不一致"的机制来源。 `[已引证]`
- 警告原文（头文件与教程同文）："使用补帧，游戏更加流畅但是可能会导致数据不一致的现象"
  （`inc/avz_replay.h:131`、`tutorial/28_replay.md:31`）。 `[已引证]`
- 教程没有说明"数据不一致"的具体观测样本，也没有给出不一致率或一致性判据。 `[待核实]`
- `Pause()/GoOn()` 只暂停/恢复回放自己的 `_tickRunner`，不改游戏本身的暂停语义
  （`src/avz_replay.cpp:542-556`、`IsPaused()` `:546`）；因此在"不补帧"或暂停状态下游戏侧
  状态如何与记录帧对齐，需要另行证明。 `[待核实]`

### 1.3 与本方案的差别

- 本方案不把"存档帧"当状态来源：数据管线的节点是"根 + 动作序列"，恢复靠从 B(0) 重算前缀，
  `docs/分支数据合同与实施计划.md:146-152` 把重算以外的恢复手段列为 R0–R4 阶梯。 `[已引证]`
- 本方案对"记录必须完整"的要求落在证据侧：每条请求、每次边界与完整状态快照都进审计，
  而不是"每 10 帧存一次盘"（`docs/runtime-protocol.md`、`docs/engine-replay.md:84-121`）。 `[已引证]`

## 2. `SaveGame` / `LoadGame` 覆盖什么、不覆盖什么

### 2.1 AvZ 调用的就是引擎自己的 Board 存档

- `AAsm::SaveGame` 把 `[0x6a9ec0+0x768]`（Board 指针）交给引擎 `0x4820D0`，并先改写小丑僵尸的
  `+0x104` 字段（`src/avz_asm.cpp:713-731`）。 `[已引证]`
- `AAsm::LoadGame` 的顺序是：先 `MakeNewBoard()`，再尝试 `LawnLoadGame 0x481FE0`；若场景变了则用
  `LoadGame 0x408DE0` 重来一次；随后调用 `continue board 0x4127A0` 与
  `__aOpQueueManager.UpdateRefreshTime(true)`（`src/avz_asm.cpp:645-704`）。 `[已引证]`
- 引擎侧对应实现：`LawnSaveGame` `0x4820D0` = 写头 + `SyncBoard`，`LawnLoadGame` `0x481FE0` =
  读头 + `SyncBoard` + `FixBoardAfterLoad` + 场景置 `SCENE_PLAYING`
  （`work/replay-research/Lawn_System_SaveGame.cpp:498-545`）；`Board::LoadGame 0x408DE0` 只是再补
  `LoadBackgroundImages/ClearUpdateBacklog/ResetFPSStats/UpdateLayers`
  （`work/replay-research/Lawn_Board.cpp:347-356`）。 `[已引证]`

### 2.2 入档的内容

- Board 自 `mPaused`（+0x164）到结构末尾的整块字节：包含波表 `mZombiesInWave`（+0x6B4）、
  行选择数组 `mRowPickingArray`（+0x654）、`mBoardRandSeed`（+0x561C）、出怪与刷新倒计时等
  （`work/replay-research/Lawn_System_SaveGame.cpp:364-365`、`work/replay-research/Lawn_Board.h:124-185`）。 `[已引证]`
- 12 个池的内容与空闲链表：僵尸、植物、投射物、金币、割草机、场地物、粒子系统、发射器、粒子、
  动画、拖尾、附着，每个池按 `mFreeListHead` + `mMaxUsedCount` + `mSize` + `mBlock` 同步
  （`work/replay-research/Lawn_System_SaveGame.cpp:355-361`、`:366-379`）。 `[已引证]`
- 另有 CursorObject、CursorPreview、Advice(MessageWidget)、SeedBank、Challenge、Music 六块单独同步
  （`work/replay-research/Lawn_System_SaveGame.cpp:403-408`）。 `[已引证]`
- 本仓库既有结论与此一致：入档 = 对象块 + 空闲链表 + 波表/阈值/倒计时 + `mBoardRandSeed`
  （`docs/机制读取点表.md:328`），且"已存在对象的槽位与空闲链表随对象字节入档"
  （`docs/社区约定与读档等价性调研.md:23`）。 `[已引证]`

### 2.3 不入档的内容

- 全局 MT 与它的游标：MT 是引擎里的全局实例（本仓库地址与实现说明见 `docs/determinism.md:11-12`），
  `SyncBoard` 全文不涉及；既有文档把它列在"读档还会丢什么"表首行
  （`docs/阵型运转影响评估.md:209-221`），`docs/机制读取点表.md:287-289` 记录 `MTRand::mt[624]`/`mti`
  与 `Sexy::Rand` 是玩法与表现共用的同一实例。 `[已引证]`
- 游戏线程 CRT `rand` 状态：`rand=0x61E087`、`srand=0x61E07A`，状态在 `_getptd` 返回对象的 `+0x14`
  （`docs/determinism.md:13`）；存档路径不写它，既有文档列入"读档还会丢什么"
  （`docs/阵型运转影响评估.md:216`）。 `[已引证]`
- `DataArray::mNextKey`：ID 由 `(mNextKey++ << 16) | slot` 生成（`work/replay-research/Sexy.TodLib_DataArray.h:142-143`），
  字段在 DataArray 头部（同文件 `:34`），`DataArrayInitialize` 把它复位成 1001（`:59`）；
  `SyncDataArray` 只同步 `mFreeListHead/mMaxUsedCount/mSize/mBlock`（`work/replay-research/Lawn_System_SaveGame.cpp:355-361`），
  而 Board 整块字节从 `mPaused`（+0x164）起，六个 DataArray 头部占 +0x90~+0x138 区间，**不在这块范围内**
  （`work/replay-research/Lawn_Board.h:107-125`）。即三条独立理由都指向"不入档"。 `[已引证]`
- Board 之外的状态同样不入档：app 级字段、音乐播放位置、avZ 层操作队列等
  （`docs/阵型运转影响评估.md:209-221` 表、`docs/社区约定与读档等价性调研.md:128-130`）。 `[已引证]`
- 因此"读档后新对象 ID 从 1001 重新起算"是源码结论；它造成的 **ABA 别名后果未实测**，
  仓库既有文档同样只标到推断（`docs/机制读取点表.md:15`、`docs/阵型运转影响评估.md:287`）。 `[待核实]`

### 2.4 读档自身消耗随机数的证据

- 链条第一段（AvZ）：`AAsm::LoadGame` 在读档前先调 `AAsm::MakeNewBoard()`，最坏情况循环体最多跑两次
  （`src/avz_asm.cpp:645-693`；`MakeNewBoard` 本体 `:567-584`）。 `[已引证]`
- 链条第二段（引擎）：`AAsm::MakeNewBoard()` 调 `LawnApp::MakeNewBoard 0x44F5F0`，实现是
  `KillBoard()` + `mBoard = new Board(this)`
  （`work/replay-research/LawnApp.cpp:419-428`）。 `[已引证]`
- 链条第三段（消耗点）：`Board::Board 0x407B50` 里对全部格子做外观初始化，每格调用 3 次全局
  `Rand`：`mGridCelLook[i][j] = Rand(20)`、`mGridCelOffset[i][j][0/1] = Rand(10) - 5`
  （`work/replay-research/Lawn_Board.cpp:70-79`；`MAX_GRID_SIZE_X=9`、`MAX_GRID_SIZE_Y=6`
  见 `work/replay-research/Lawn_Board.h:18-19`）。 `[已引证]`
- 按逐行计数：9 × 6 = 54 格 × 3 次 = **162 次**全局 `Rand` 调用；这些值随后会被 `.dat` 里的
  `mGridCelLook`/`mGridCelOffset`（+0x240/+0x318，位于 `mPaused` 之后的同步区间）覆盖，
  但**消耗已经发生且不回滚**。 `[已引证]`
- 仓库既有文档记的是"45×6 循环 / ≈800 次"（`docs/阵型运转影响评估.md:222`）与"≈810 次"
  （`docs/机制读取点表.md:300`）。本次逐行核对的循环边界是 9×6，两处旧数字与源码不符；
  `Board` 构造内是否还有其他随机消耗、以及一次完整读档的**实际**总消耗次数，均未实测。 `[待核实]`
- 引擎自身的读档路径同样如此：`LawnApp::TryLoadGame` 也是 `MakeNewBoard()` 之后才
  `mBoard->LoadGame(...)`（`work/replay-research/LawnApp.cpp:446-457`）。 `[已引证]`
- 附带两个会改状态的怪癖（源码结论，未实测）：保存前会改写小丑僵尸字段（`src/avz_asm.cpp:714-716`）；
  读档不是内存覆盖而是摧毁重建，读档后靠 `FixBoardAfterLoad` 重新挂回 `mApp/mBoard` 指针
  （`work/replay-research/Lawn_System_SaveGame.cpp:427-494`）。 `[已引证]`

### 2.5 与本方案的差别

- 本方案没有"读存档文件"这一步：`docs/分支数据合同与实施计划.md:49-56` 把"读档 / 接管"限定为
  训练数据管线的建树手段，并要求计分运行管线在证据里**证明未发生**读档。 `[已引证]`
- 本方案对随机流的处理是显式接口而不是隐含副作用：`rng_seed`/`rng_restore` 在暂停边界内
  只动被声明的 RNG 实例并记事件（`docs/runtime-protocol.md:26-28`）；`prepare_render` 在
  预热绘制**之前**独立核对 624 个 MT 字、游标与游戏线程 CRT 是否等于最后一次显式播种
  （`docs/runtime-protocol.md:29`）。 `[已引证]`
- 本方案不声称覆盖全部随机源：独立 MT 的调用点只被识别、未被覆盖，`complete_game_rng=false`
  （`docs/determinism.md:14`、`:76`）。任何"完全恢复随机流"的说法在本仓库口径下都不成立。 `[已引证]`

## 3. AvZ 的随机数相关设施（逐条）

1. `ARandom` / 全局实例 `aRandom`：AvZ 自带的 C++ 随机数类，可 `SetSeed`，默认用
   `std::random_device()` 播种（`inc/avz_global.h:103-237`）。能固定的：**AvZ 自己**的随机选择；
   不触及引擎全局 MT 或它的游标，因此不改变游戏自身的出怪/掉落/动画随机流；与游戏随机数不是
   同一个对象（游戏侧实例位置见 `docs/determinism.md:11`）。 `[已引证]`
2. `ACreateRandomTypeList`：用 `aRandom` 生成满足"必出/不出"约束的僵尸类型表
   （`inc/avz_memory.h:163-169`、`src/avz_memory.cpp:269-330`，随机调用在 `:297`、`:325`）。
   它固定的是**类型表这个结果**，不是随机流；结果要通过下一步写进游戏才生效。 `[已引证]`
3. `ASetZombies` / `ASetWaveZombies`：直接写 `ZombieTypeList()` 与 `ZombieList()`
   （`src/avz_memory.cpp:188-266`，写入点 `:201`、`:220`、`:254`），即直接写 Board 内、**会入档**的
   出怪类型表。能固定：这一局的出怪类型；不能固定：出生行、速度、动画速率、下一波刷新等仍由
   全局 MT 决定（机制表见 `docs/阵型运转影响评估.md:226-232`、`docs/机制读取点表.md:14`）。 `[已引证]`
4. `AAsm::PickRandomSeeds`：调用引擎 `0x4859B0`（参数取 `[0x6a9ec0+0x774]`），注释写明用途是
   "以随机选卡填充卡槽"（`src/avz_asm.cpp:889-900`、`inc/avz_asm.h:231-232`）。它是随机流的
   **消费者/使用者**，不是拦截器；是否消耗全局 MT 未逐行核对。 `[待核实]`
5. DSL 层的随机用法：`inc/dsl/shorthand_240205.h:633`、`inc/dsl/shorthand_240713.h:921`、`:944` 用 `aRandom`
   （`Shuffle`/`Choice`）决定行号与出怪类型；同样只影响 AvZ 自己的随机实例。 `[已引证]`
6. 通用内存写入能力：仓库内 `MPtr`/`AMRef` 一族与内联汇编直调允许 AvZ 读写任意引擎地址
   （例如 `src/avz_replay.cpp:661-668` 直接改写 `_FALLING_SUN_ADDR`/`_ZOMBIE_SPAWN_ADDR` 两个
   代码字节）。理论上可以手工写 MT 的 624 字与游标（地址见 `docs/determinism.md:11`），
   但固定提交里**没有任何 API、示例或测试**这样做；能否据此做到逐位一致的随机流接管、
   以及中断后游标是否可达，本文无法核实。 `[待核实]`
7. 中途执行接管：`ATickRunner`、`AStateHook` 的 `BeforeTick/AfterTick`、按键 `AConnect` 都允许在
   任意 tick 运行代码（`inc/avz_tick_runner.h`、`inc/avz_state_hook.h:55-63`、
   `tutorial/28_replay.md` 的手动 TAS 示例）。可接管的是**控制流**，不是随机流；
  接管期间 MT 与游标继续原样前进。 `[已引证]`
8. 搜索负结果（可作为"AvZ 没有随机数恢复设施"的直接证据）：在固定提交的 `inc/` 与 `src/` 下
   检索 `MTRand`、`mBoardRandSeed`、`0x75A910`、`0x5A9930` 均无命中；`rand` 命中只覆盖
   `ARandom`、`ACreateRandomTypeList`、`PickRandomSeeds`、以及 MinHook 反汇编器里的 `operand`。 `[已引证]`

## 4. 本方案的做法与已测证据

### 4.1 合同

- 三条性质：P1 同源分叉决定论（同根 + 同动作序列 ⇒ 逐帧相同轨迹）、P2 根一致性（根是合法引擎
  状态且取自真实分布）、P3 跨树独立（不同根的轨迹不互相比较）
  （`docs/分支数据合同与实施计划.md:20-26`）。 `[已引证]`
- 两条管线分开：训练数据管线"允许读档/接管作为建树手段"，计分运行管线"禁止，且须在证据里
  证明未发生"（`docs/分支数据合同与实施计划.md:47-56`）。 `[已引证]`
- 恢复阶梯 R0–R4 把"恢复是否恒等"写成可判分的自检（恢复后重抓逐字节相等 → 不推进 → 推进
  1 tick → 推进 N tick → 改恢复间隔/负载）（`docs/分支数据合同与实施计划.md:142-152`）。 `[已引证]`
- 中途接管的语义：从 B(0) 重算前缀，seek 停在**第一次** B(t)，接管回调在 initializer 上下文内
  运行并把本次 trace 标成 `replay_takeover` 父分支（`docs/engine-replay.md:127-153`、`:169`）。 `[已引证]`
- 分支身份：runtime 声明自己的作用域，请求带 `branch` 字段，去重键是
  `(branch_id, epoch, request_id)`，克隆进程忘记改写身份会在 `hello` 阶段失败
  （`docs/client.md` 分支作用域一节；`hello.branch` 校验见 `src/llm_vs_zombies/client.py:42-81`）。 `[已引证]`

### 4.2 已测证据（`experiments/runs/m1-two-cold`，500 tick / 2 冷启动）

- 计划：`tier=smoke`、`seeds=[42]`、`cold_starts=2`、`tick_budget=500`、策略为
  `examples/liangyi_baseline.py`（`experiments/runs/m1-two-cold/plan.json`）。 `[已引证]`
- 两次冷启动都成功并在 tick 500 结束：`seed-42-case.json` 里 `cold_starts` 长度 2、
  `status=completed`、`outcome=tick_budget_exhausted`、`final_observation.version.tick=500`、
  `decisions=12`、`failed_actions=0`（`experiments/runs/m1-two-cold/seed-42-case.json`）。 `[已引证]`
- 两次冷启动的 B(0) 相同：`experiments/runs/m1-two-cold-s42-c1/initial-comparison.json` 为
  `{"equal": true, "difference": null}`。 `[已引证]`
- 从 B(0) 重算的引擎重放 `equal=true`：`seed-42-replay-1/replay-report.json` 里
  `mode="cold_start_recompute"`、`equal=true`、`reached_version={epoch:3,tick:500,revision:0}`、
  `engine_calls` 记 `returned_calls_compared=500` / `clock_steps_compared=500`。 `[已引证]`
- 本轮验收结论：`engine_replay=pass`；同时 `ten_cold_starts=fail`（只请求 2 次）、
  `full_cycle=fail`（500 tick 内 `maximum_wave=0`），即这是 smoke 范围的证据，不是长局成功
  （`experiments/runs/m1-two-cold/evaluation.json`、`evaluation.md`）。 `[已引证]`
- 该证据不含出生路径：`replay-report.json` 记 `spawn_exercised=false`、`spawn_events_compared=0`；
  #51/#52 新增的 `spawn` op 需要自己的运行证据。 `[已引证]`
- `spawn` op 本身的实现与协议语义（1-based 行列、0-based 引擎格换算、失败码、消耗全局 RNG）
  见 `docs/runtime-protocol.md`「Spawn action」一节（`:36-63`）。 `[已引证]`

### 4.3 尚未具备的证据

- 本仓库尚未实测"AvZ 读档路径消耗多少次随机数"这一具体数字，现有数字与源码不符（见 §2.4）。 `[待核实]`
- 本方案是否覆盖了 AvZ-only 方案里由读档副作用引入的全部差异（独立 MT、app 级字段、
  动画/音效共享随机流）也还没有对照实验；`complete_game_rng=false` 就是这条限制的书面口径
  （`docs/determinism.md:76`）。 `[待核实]`
- 本次交付的两个 #51 场景脚本（`examples/liangyi_gargantuar_a.py`、`_b.py`）只做离线契约验证：
  锤子索敌与砸击时机、`APPROACH_TICKS`/`SMASH_TICKS` 是否真的落在索敌窗口与砸击事件上，
  必须由主 agent 的真机运行决定。 `[待核实]`

## 5. 结论表

| 问题 | 仅 AvZ 方案能做到什么 | 本仓库能做到什么 |
|---|---|---|
| 同一问题：同一起点 + 同一动作序列，是否得到同一状态 | 不保证。回放靠读 Board 存档，而全局 MT 与游标、CRT 状态、`mNextKey`、app 级状态都不入档（§2.2/§2.3）；补帧模式下两次落盘之间游戏继续真跑（§1.2），官方文档自己写"可能会导致数据不一致"（§1.1）。 | 训练数据管线按 P1 断言"同根 + 同动作 ⇒ 逐帧同轨迹"（§4.1）；已测证据：500 tick / 2 冷启动、同一 B(0)、引擎重放 `equal=true`（§4.2）。 |
| 能否中途接管 | 能接管**控制流**：`StartPlay/ShowOneTick/Pause/GoOn` 跳到指定帧重放，`ATickRunner`/`BeforeTick`/`AfterTick`/`AConnect` 可在任意 tick 跑代码（§3.7、§1.2）。但接管到的是 Board 存档语义，随机流不会回到那一刻（§2.4）。 | 能：从 B(0) 重算前缀、seek 到第一次 B(t)、`on_takeover` 回调把后续写成带父分支身份的独立分支（§4.1）；恢复等价性用 R0–R4 阶梯判分（§4.1）。 |
| 读档是否改变随机流 | 会。一是不入档（§2.3），二是读档路径先 `MakeNewBoard()` 而 Board 构造至少消耗 162 次全局 `Rand` 且不回滚（§2.4）；具体总消耗未实测。 | 管线里没有读档这一步（§2.5）；`rng_seed`/`rng_restore` 覆盖全局 MT 的 624 字与游标以及游戏线程 CRT（§2.5），但独立 MT 未覆盖（§4.3）。 |
