# 经典十二炮同根分叉：铲子干预、轨迹复现与薄数据闭环（issue #99）

本文记录 [issue #99](https://github.com/guajun/llm-vs-zombies/issues/99) 的实施：在经典十二炮
托管键控下，用**一次拿起铲子再放回**的受控动作，从同一冷启动根生成对照与干预两条分支，各
复跑一次，比较首差异，并把四份真实运行封成一棵可离线读取的树。

范围已按 issue 确认：铲子操作是拿起后取消/放回，**不是**铲除植物，也不使用现有
`shovel(row,col)`（那是 `AShovel`，会把植物从草坪上铲掉）。实验用确定性的 AvZ 托管键控替代
LLM，以隔离模型输出的不确定性。

## 1. 冻结的实验计划

计划基线：`main@c4d7e7387a99e27380dd70479d7c4f1d72d9c674`。两个计划文件只差"声明的动作"这
一处（下表最后一行的对照/干预两列），其余字段完全一致。

| 项目 | 值（对照 / 干预） |
|---|---|
| 场景 | `jingdian12`（泳池无尽，`Board::Scene()==2`，十卡槽） |
| seed | 42 |
| 策略 | `null`（AvZ 托管脚本 `logger/avz/hosted/jing_dian_12.cpp` 驱动） |
| 音频模式 | `sound_effects_allocation_none_v1` |
| B(0) 归一化 | `/sound_effects/app_update_count=1500`、`/app/mj_clock=1340` |
| 模拟 tick 硬上限 | 2000（正式运行不为追求差异而延长） |
| 事件终点 | `stop_when.wave_at_least=2`：第一个观测到第 2 波的战斗边界 |
| 决策粒度 | `chunk_ticks=100`，公开入口首个策略决策在 B1 |
| 暂停探针 | tick 1000 处 1 秒墙钟暂停 + 已捕获状态不变证明 |
| rounds_to_complete | 1（短程窗口内不完成任何 round；`full_cycle` 门槛因此不通过，属预期） |
| 冷启动 | `cold_starts=1`：每条分支另有一次原版冷重放与一次断线恢复探针 |
| 声明边界 | `epoch 3 / tick 101 / revision 0` |
| 先决条件 | `wave=0`、`refresh_countdown=498`、`zombies=0`、`plants=52` |
| 动作 | 对照：`null`（只观测该边界）；干预：`{"op":"shovel_hold_cancel"}`（`advance_ticks` 100） |

计划文件（连同 `intervention` 与 `stop_when` 是 `lvz.evaluation-plan.v2` 的新声明字段）：

- `experiments/plans/issue99-shovel-control.json`
- `experiments/plans/issue99-shovel-intervene.json`

构建身份：托管 DLL 的 `recorder_sha256` 与托管脚本 SHA256 由
`tools/build-hosted.ps1` 落盘（`build/hosted-build.json`），每条运行的 `manifest.json` 绑定
同一次构建。**四份运行必须绑定同一个 DLL 哈希**；改代码就要重跑全部四条，本次实施期间因此
丢掉了一份只用于打通的准备运行（见 §7）。

注意：本机实测**同一份源码两次重建会得到不同的 `recorder.dll` 哈希**（LLVM-MinGW 链接产物
带非确定内容），所以 `recorder_sha256` 标识的是"这一份字节"，不是"这份源码必然产出同一份
字节"。要复现实验请记录并引用运行 manifest 里的哈希；重建后必须新建运行，不能拿旧运行
冒充新构建。

## 2. 共同根与干预边界

- **共同根**：seed 42 + 上面的 B(0) 归一化表 + 同一份参考存档与构建。四条轨迹都用公开入口
  冷启动到 B0，再按同一键控程序推进；首版"同根"由**冷重放到同一根并验证**实现并报告，
  **不是**进程克隆（issue 已确认）。
- **干预边界**：预检在 `epoch 3 / tick 100 / revision 0`、`wave=0`、`refresh_countdown=499`、
  场上无僵尸的稳定点上测过铲子动作。正式运行走公开入口时，第一个策略决策边界是 B1，之后
  每 100 tick 一个边界，因此同一"第一波刷新前"的点在正式计划里被冻结为
  `epoch 3 / tick 101 / revision 0`（`refresh_countdown=498`，离第一波刷新仍有 498 tick，
  第一波生成事件尚未发生）。这不是把预检结果搬过来，而是把边界冻结在公开入口真正能停住的
  坐标上：计划只声明坐标，**是否命中由运行时版本三元组逐字段判定**。
- **fail-closed**：边界被跳过（观测到的版本已经越过声明坐标）、先决条件不成立、动作回执
  `ok!=true`，三种情况都以 `intervention_not_reached` / `intervention_precondition_failed` /
  `intervention_failed` 结束该分支并保留运行，不用"晚一点的干预"顶替。

## 3. 受控动作：拿起铲子再放回

动作 `{"op":"shovel_hold_cancel"}` 在游戏线程上由普通 commit 执行器调用，走 AvZ 的原生场景
点击入口 `AAsm::ClickScene`（锁定引擎 `0x411f20` `Board::MouseDown`）：一次左键把铲子拿到
光标上，再按声明的 `cancel`（默认 `"right"`）放回。**不**发 OS 鼠标消息、不抢焦点（隐藏窗口
下同样成立）、**不**直接写随机源、**不**调用 `AShovel`。完整接口、前置条件、回执形状与失败码
见 [`docs/runtime-protocol.md`](runtime-protocol.md) 的 "Controlled shovel action"。

动作自己带三段证据（`before` / `picked` / `after`：光标类型、RNG 完整快照、状态摘要、音效摘要、
植物身份集合），并在 `changes` 里汇报**实测**副作用。它不声称"恰好消费一次随机数"：预检
实测的是 MT 游标前进 3 个输出位置、624 个词不变、游戏线程 CRT 不变，同时
`sound_effects/calls` 与历史 `9`、`75` 的 `last_variation` 改变；本轮真机值与预检一致
（见 §6）。

## 4. 历史 baseline 身份对照

issue 记录的历史运行 `<原实验主机>\work\hosted-live\experiments\runs\jd12-2flags-01`（及其
`-s42-c0`/`-s42-c1`）在本机同一检出的 `work/hosted-live` 下，本轮只读复核：

| 文件 | issue 记录 | 本机复算 |
|---|---|---|
| `jd12-2flags-01/plan.json` | `879860b40962c7d3dc3171a41c5b25d1141bbc50ae8560f63db904601dbdb226` | 一致 |
| `jd12-2flags-01/seed-42-replay-1/replay-report.json` | `fc47b738951af3ef6e21fb82df7402c5ae8dc42f6aedf0d7a2dabc0f7b148dc4` | 一致 |
| 游戏文件 | `game/original` 四件 | 与 `dependencies.lock.json` 的锁定哈希逐件一致 |

复用结论：历史档给出**参考轨迹与冷重放通过记录**，可以对照，但不能直接当作"新增铲子动作后
的同构对照"——两边不是同一个 DLL、不是同一个 tick 上限，也不是同一段窗口。本轮因此重新录制
四条轨迹，历史档只用于（a）确认经典十二炮真机可跑、（b）反推第一波刷新到下一波刷新的窗口
（历史档：tick 501 时 `wave=0, cd=98`，tick 601 已 `wave=1`；tick 1101 仍 `wave=1`，tick 1201
已 `wave=2`）。

口径提醒（#97）：字段在 #97 后统一叫 `rounds_to_complete`，单位是 round（1 round = 2 flag = 20 波）；
旧名 `flags_to_complete` 只剩 `Plan.load` 的过渡只读别名，历史档写的 `flags_to_complete=2` 是
**2 round = 4 面旗 = 40 波**，不是"两面旗"。本轮计划声明 1（短程窗口内一个 round 都打不完），
`full_cycle` 门槛交给既有定义，因此**不**通过该门槛。

## 5. 正式运行与复跑

四条轨迹都由同一公开入口产生：

```powershell
$env:PYTHONPATH = 'src'
python -m llm_vs_zombies.evaluation run experiments\plans\issue99-shovel-control.json `
  --output experiments\runs\issue99-jd12-control-a --skip-build
```

`tools/issue99_shovel_fork.py run` 就是把这四条（`-control-a`、`-control-b`、
`-intervention-a`、`-intervention-b`）串行跑完，再比较、封树、写报告。`--skip-build` 是必须的：
托管 DLL 必须**先**构建、由运行绑定，运行器自己重建默认 DLL 会换掉被绑定的哈希。

每个 suite 还包含该轨迹的原版冷重放（`-s42-c1`，同一份 manifest 绑定同一个 DLL）与断线恢复
探针；`engine_replay` 的门槛因此是真实执行结果，不是脚本声明。冷重放不能标记为 snapshot
restore 或 process fork；报告里"复跑"指重新产生轨迹的 `-b` suite。

## 6. 结果：首差异与复现

本节数字全部来自 `tools/issue99_shovel_fork.py` 生成的报告（`work/issue99-jd12r-report.json`，
由 `work/issue99-jd12r-seal.json` 绑定到树身份）。比较按 tick 对齐、逐边界摘要：每个 tick 取该
tick 最后一个 `pre_step` 状态，逐组件比较摘要值（`rng`、`sound_effects`、`board`、`zombies`、
`plants` …）。版本三元组里的 `revision` 单独报告，因为声明的动作会在自己的 tick 里把它加一。

### 6.1 四条轨迹

| 角色 | 运行 | 结局 | 结束边界 | 最大波次 | 托管炮击 | 实验动作 | 冷重放 |
|---|---|---|---|---|---|---|---|
| control | `issue99-jd12r-control-a-s42-c0` | stop_condition_reached | epoch 3 / tick 1201 / rev 0 | 2 | 4 发（567、1168 各两发） | 0 条 | equal=true |
| control-rerun | `issue99-jd12r-control-b-s42-c0` | stop_condition_reached | epoch 3 / tick 1201 / rev 0 | 2 | 4 发（567、1168 各两发） | 0 条 | equal=true |
| intervention | `issue99-jd12r-intervention-a-s42-c0` | stop_condition_reached | epoch 3 / tick 1201 / rev 0 | 2 | 4 发（567、1168 各两发） | 1 条 | equal=true |
| intervention-rerun | `issue99-jd12r-intervention-b-s42-c0` | stop_condition_reached | epoch 3 / tick 1201 / rev 0 | 2 | 4 发（567、1168 各两发） | 1 条 | equal=true |

四条运行的 `initial_version` 都是 `epoch 3 / tick 0 / revision 5`，`identity`（游戏/资源/存档/
模块摘要 + 初始化配方）逐字段相同，`recorder_sha256` 都是同一次托管构建——这是"同根"的可核对
部分。首个僵尸槽位离场（不是击杀计数：审计没有独立击杀标记）四条都出现在 tick 941、各 33 个
槽位；本条只是如实记录观察，不作为差异判据。

### 6.2 复现性（同分支两条轨迹）

| 比较 | 共同前缀 | 首个差异 |
|---|---|---|
| control vs control-rerun | 1201 个边界（全窗口） | 无（`identical=true`） |
| intervention vs intervention-rerun | 1201 个边界（全窗口） | 无（`identical=true`） |

### 6.3 首差异（对照 vs 干预）

| 项目 | 结果 |
|---|---|
| 干预前共同前缀 | 101 个边界（tick 1..100 逐组件摘要相同） |
| 首个差异 | tick 101：`revision 0→1`，组件 `rng`、`sound_effects`（`board` 未变） |
| 首个随机状态差异 | tick 101：tick 101 边界处 `/rng/instances/global_mt/cursor 604→607`（+3 个输出位置），624 词不变；`game_thread_crt` 42→42 不变 |
| 首个语义差异 | tick 118：`/plants/slots/18/fields/00000058` `286→293`（该差异边界处逐指针共 5 条，其余 3 条是 RNG 游标、音效调用计数与两条音效历史） |
| 第一波（tick 601 边界） | 逐指针 603 条差异：`/board/*`（含 `refresh_countdown` 2622 vs 2677）、50 个 `/zombies/slots/*`、`/reanimations/nodes/*`、`/coins/slots/*`：第一波出怪的具体内容不同（两端僵尸数量与炮击时刻仍相同） |
| 结局差异 | 窗口内无：两分支都是 4 发炮击、`plants=52`、无通关或失败 |

也就是说：**随机状态分叉与首波内容分叉都在冻结窗口内出现且可复现**，但窗口内还没有出现"胜负"
级别的结局差异——这与 issue 的判定表一致（不要求两局一胜一负）。

### 6.4 干预动作真机回执

（数字来自运行内 `action` 审计记录与 `experiments/runs/.../decisions/evaluation.jsonl` 的
`intervention` 事件；两次复跑一致。）

| 项目 | 值 |
|---|---|
| 声明/实际边界 | epoch 3 / tick 101 / revision 0（两分支相同） |
| 先决条件 | wave=0、refresh_countdown=498、zombies=0、plants=52 |
| 光标 | 0 → 6 → 0 |
| RNG | `instances/global_mt` 游标 `604 → 606 → 607`（拿起 +2、放回 +1），624 词不变；`instances/game_thread_crt` `42 → 42 → 42` |
| 音效副作用 | `/sound_effects/calls` 0→2；`histories/9`、`histories/75` 的 `last_variation` −1→0 |
| 植物 | 集合与数量不变（52→52） |
| tick 消耗 | 动作绑定同一 tick、`revision` 加一：不消耗模拟 tick；同一次 commit 仍按计划给两个分支相同的 100 tick 前进预算 |

两次干预运行的上述数字完全相同（`604 → 606 → 607`、`42 → 42 → 42`、`0 → 6 → 0`、同一组音效
变化），因此"动作副作用"本身也是可复现的，而不是一次偶然观察。

## 7. 数据闭环：一条入口、一棵树、一个离线读取器

`tools/issue99_shovel_fork.py` 有五个子命令：`run`（跑四条轨迹并串联后续步骤）、`compare`
（只读比较）、`tree`（只读封包）、`read`（离线读取）、`verify`（按已写出的 seal 重新核对树与报告）。
`run` 不自己启动游戏：它调用公开入口 `python -m llm_vs_zombies.evaluation run ...`，与人工执行逐字相同。

树身份 `tree_id = 529a2f4f1370a720a138fcbf90ffad5ce3e64dfe17d1ae00e178c6a7a97e447f`，结构
（`experiments/trees/issue99-jd12r-shovel-fork/`，`tree.json` + `nodes/<key>/`）：

| 节点 | 分支 id | 主干 | 父节点 | 出发边界 |
|---|---|---|---|---|
| control | `issue99-control-a` | 是（真实续跑基准） | — | 根身份（游戏/存档/模块摘要 + 初始化配方） |
| control-rerun | `issue99-control-b` | 否 | control | epoch 3 / tick 0 / revision 5（共同根） |
| intervention | `issue99-intervention-a` | 否 | control | epoch 3 / tick 101 / revision 0（声明的干预边界） |
| intervention-rerun | `issue99-intervention-b` | 否 | intervention | epoch 3 / tick 0 / revision 5（共同根） |

这棵树里**没有一条边声称是进程克隆**：parent 边界记录的是"该轨迹声明从哪一点离开父轨迹"，
而"两条冷启动轨迹在该点之前逐边界相同"这件事由 §6.3 的比较报告证明，树只绑定身份、父边界、
分支 id 与哈希链。`read` 子命令在无 AvZ、无游戏进程、无 launcher 的情况下重新加载每个节点
（含 `Trajectory.load` 的全部证据校验）、重算 chain 与 tree_id，并打印节点/父边界：

```powershell
$env:PYTHONPATH = 'src'
python tools/issue99_shovel_fork.py read --tree experiments\trees\issue99-jd12r-shovel-fork
```

`work/issue99-jd12r-seal.json` 把树身份与两份报告（JSON/Markdown）以及每个节点的
`trajectory_id`、`manifest.json` 摘要绑在一起。`read` 只重算树自己的哈希链，不打开 seal，所以
seal 与现场的一致性由第五个子命令核对：报告被替换、节点被换掉、树或报告摘要被改写，都会在
`verify` 里逐条列出（本轮的四个 `trajectory_id` 与报告摘要见该文件）：

```powershell
$env:PYTHONPATH = 'src'
python tools/issue99_shovel_fork.py verify `
  --tree experiments\trees\issue99-jd12r-shovel-fork `
  --seal work\issue99-jd12r-seal.json
```

`verify` 只读、不需要 AvZ 或游戏进程；`matches=false` 时以非零退出码结束并保留每条
`declared`/`actual`。**封存是薄证据基础**：只有轨迹、根与父节点身份、摘要校验、只读加载与关联
复跑报告；它不包含数据集目录服务、版本发布、样本筛选、去重、训练集划分、reward 或训练采样。

本轮封存现场实测（rebase 后的入口，2026-09-24）：`matches=true`、`tree_id=529a2f4f…e447f`、
4 个节点、两份报告（JSON/Markdown）的 SHA256 逐条相符，`problems` 为空。

## 8. 能力结论

对照 issue #99 的验收清单：

| 验收项 | 结论 | 依据 |
|---|---|---|
| 经典十二炮场景真机确认，来源与构建身份齐全 | ✅ | 运行 manifest 绑定 `recorder_sha256`；场景校验 `Scene()==2`、十卡槽、12 门炮 |
| 同根成立，干预前共同前缀相同 | ✅ | 101 个边界逐组件摘要相同 |
| 干预动作只按声明执行一次、没有铲植物，副作用有证据 | ✅ | 每条干预运行恰好 1 条 `action` 记录；`plants_changed=false`、光标归位、音效与 RNG 变化逐叶子列出 |
| 对照原跑 == 对照复跑；干预原跑 == 干预复跑 | ✅ | 两对比较 `identical=true`（各自冷重放 `equal=true`） |
| 首差异不早于干预；RNG 差异与语义差异分别定位 | ✅ | RNG 差异 tick 101；首个语义差异 tick 118；第一波内容差异 tick 601 |
| 报告指定 RNG 实例及真实状态变化，并记录音效副作用 | ✅ | `instances/global_mt` + `instances/game_thread_crt` 三段快照；音效三处变化逐项列出；不声称"恰好一次" |
| 冻结窗口内出现可复现语义差异 → 判定"成功演示游戏轨迹分叉" | ✅ | 首波出怪内容与波次计时在窗口内不同，且同分支两次一致 |
| 一条入口接受根、对照/干预计划并生成两分支树及关联复跑证据 | ✅ | `tools/issue99_shovel_fork.py run`（四份 suite + 比较 + 树 + seal） |
| 树中根、父边界、分支身份、动作、轨迹、审计与自检报告相互绑定，可校验 | ✅ | `tree.json` 索引 + 每节点 `tree` 段（哈希链）；seal 绑定报告摘要，`tools/issue99_shovel_fork.py verify` 逐条重算并列出不符项 |
| 无 AvZ、无游戏进程的只读加载器能读取两条轨迹并重建根与分支关系 | ✅ | `tools/issue99_shovel_fork.py read` |
| 最小样本区分环境脚本动作与实验干预动作 | ✅ | `hosted_fire`（托管脚本发炮，形态同 #84）与 `action`（实验干预，含 `request_id`/`ordinal`）在 `audit/events.jsonl` 里分开记录，轨迹里只有后者是请求动作 |
| 失败/缺证据轨迹不能冒充已验证训练数据 | ✅ | 只读加载器与比较器在缺边界、缺证据时返回失败/未验证，不自动通过 |

同时明确**未**通过或未涵盖的门槛：本实验按 issue 范围只做短程 smoke 级的受控实验，`strict`
未请求、`build_and_tests` 因托管 DLL 必须先于运行构建而标为未验证、十次冷启动与两旗
`full_cycle` 均未执行。报告因此把"能力结论"限定在本场景、本根、本构建。

## 9. 判定与失败处理对照

| issue 观测 | 本次实际 | 处理 |
|---|---|---|
| 干预前就分叉 | 未发生（前缀 101 个边界相同） | — |
| 铲子动作失败或 UI 状态未还原 | 未发生（光标 0→6→0） | 若发生：`intervention_failed` 结束该分支并保留运行 |
| 动作成功但 RNG 不变 | 未发生（MT 游标 +3） | 若发生：保留负结果，不暗中补随机 |
| RNG 改变但不是一次 | 适用：前进 3 个输出位置 | 报告真实次数与实例，不改 RNG |
| 两组各自不可复现 | 未发生（两对逐边界一致） | — |
| 两组可复现但窗口内无语义差异 | 未发生（tick 118 起出现语义差异） | — |
| 分叉与树加载均通过 | 通过 | 结论不外推为通用就绪 |

## 10. 不由本任务证明

- LLM 的决策质量或 LLM 对局组件：本实验用确定性托管键控替代 LLM。
- 模型训练有效、reward 合理、数据集规模化管理：封存只是薄证据基础。
- 真进程 fork、运行后快照恢复或并行执行：四条轨迹都是冷启动。
- 十次冷启动、完整两旗 strict 门槛：除本窗口外的验收未执行。
- 捕获游戏全部状态，或铲子干预只有 RNG 一条作用路径：音效历史与调用计数同时改变，已如实列出。
- 单根结论可推广到其它 seed、场景、音频模式或构建。

## 11. 复现命令

```powershell
# 1) 构建绑定到运行的托管 DLL（先构建，后建 run；改代码就要重跑全部四条）
.\tools\build-hosted.ps1 -Script logger\avz\hosted\jing_dian_12.cpp

# 2) 跑四条轨迹 + 比较 + 封树 + 写报告
$env:PYTHONPATH = 'src'
python tools\issue99_shovel_fork.py run `
  --control-plan experiments\plans\issue99-shovel-control.json `
  --intervention-plan experiments\plans\issue99-shovel-intervene.json `
  --prefix issue99-jd12r --skip-build `
  --tree-output experiments\trees\issue99-jd12r-shovel-fork `
  --report-json work\issue99-jd12r-report.json `
  --report-markdown work\issue99-jd12r-report.md `
  --seal work\issue99-jd12r-seal.json

# 3) 离线读取（不需要 AvZ、不需要游戏进程）
python tools\issue99_shovel_fork.py read --tree experiments\trees\issue99-jd12r-shovel-fork

# 4) 按 seal 重新核对树与两份报告（matches=false 时非零退出）
python tools\issue99_shovel_fork.py verify `
  --tree experiments\trees\issue99-jd12r-shovel-fork `
  --seal work\issue99-jd12r-seal.json

# 5) 离线自检
ctest --test-dir build\cmake -R "runtime_shovel_action|runtime_spawn_action"
python -m unittest tests.test_issue99_shovel_fork -v
```

准备记录（**不是**本轮四条轨迹，保留在 `experiments/runs/` 下以备审阅）：`issue99-shovel-intervene-a`
（旧 DLL：动作回执的 RNG 摘要有缺陷，见提交说明）与 `issue99-jd12-control-a`（运行期间操作者
修改了 `src/llm_vs_zombies/*.py`，suite 的 `host_identity` 门槛因此失败；该次源轨迹本身
`archive_sealed=true`、`trajectory_verified=true`，但没有冷重放与恢复探针，故不作为本轮对照）。
