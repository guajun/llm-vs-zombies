# `#101` 整改：拟议 issue 正文修订稿（2026-09-26）

本文件是**未同步的拟议稿**，供父任务 review 后用 `gh issue edit --body-file` 同步到 GitHub。本轮不编辑任何 issue、不发评论。
- 原则：**保留历史报告/评论与不相关章节**；对当前状态与待办**直接更新**，与代码/既有评论冲突的过时块明确标注已被 2026-09-26 状态取代；错误推断直接修正。
- 所有拟议文字均附来源（PR/commit/评论/文档）。事实状态总表见 [issue101-整改矩阵](issue101-整改矩阵.md)。
- **完整新正文同步预览**（工作区外生成，不提交仓库；本仓不记录个人绝对路径）：`issue-bodies/issue-<N>.md`；来源摘要与 `updatedAt` 记录在同目录 `sources.json` / `sources.md`。本文件的替换/追加块与这些完整正文逐字一致。
- 建议同步顺序：#50 → #100 → #99 → #97 → #98 → #84 → #72 → #101（父任务自行决定）。

---

## 1. `#50`（P1：状态与代码/评论冲突）

### 1.1 替换「## 背景」末句

**原文：**

> Epic #39 的十件套（N1–N10）已全部合入 main，但**全部是合成/夹具级验证**：真实游戏从未跑过 a2 快照恢复，launcher 也还没接 `LVZ_BRANCH_ID`（#32 的未做项 3）。本 issue 是训练数据管线从"能力齐备"到"真实可用"的门槛。

**拟议替换为：**

> Epic #39 的十件套（N1–N10）已全部合入 main。截至 2026-09-24，真实游戏已跑过 a2 R0 的"抓取 → 回灌 → 再抓取"（`m1-r0-001` 连续两次、`m1-live-r0-01` 一次），launcher 分支身份已由 [PR #96](https://github.com/guajun/llm-vs-zombies/pull/96) 接通并有真机记录（见下方评论；代码在 `launcher/native_launcher.cpp` 与 `src/llm_vs_zombies/launcher.py`）。本 issue 现在收窄为 headless 扰动探针 A0/A1 的焦点/光标档与 A2/A3；R0/R1–R3 档位与硬限制以 [`docs/同源分支自检.md` §6](https://github.com/guajun/llm-vs-zombies/blob/389803f10bef8d1b2abdd7b1cee0b9328ac26332/docs/同源分支自检.md) 为准。

**来源：** [PR #96](https://github.com/guajun/llm-vs-zombies/pull/96)（merge `c4d7e7387a99e27380dd70479d7c4f1d72d9c674`）；#50 评论 [5815535379](https://github.com/guajun/llm-vs-zombies/issues/50#issuecomment-5815535379)。

### 1.2 替换「## 状态（2026-09-24 复核）」整节

**拟议替换为：**

> ## 状态（2026-09-26 整改后复核）
>
> 按 #101 review 的四列口径重排（实现 / 离线验证 / 真机验证 / 证据可取回）：
>
> | 交付物 | 实现 | 离线验证 | 真机验证 | 证据可取回 |
> |---|---|---|---|---|
> | 1. launcher 分支身份：注入 `LVZ_BRANCH_ID`，运行时身份与证据树 branch 名一致 | ✅ PR #96：建进程前校验并 `SetEnvironmentVariableW`，主线程恢复前写 receipt；`launcher.py` 缺省分支名 = run 目录名 | ✅ PR #96 断言 + [PR #108](https://github.com/guajun/llm-vs-zombies/pull/108) isolation fixture 核验 | ✅ `m1-live-a-s42-c0`（2026-09-24）：`launcher.json.branch_id` = 目录名 = `native-receipt.json.branch_id` = `hello.branch` = `manifest.branch`；冷重放报告 `source_branch_id=c0`/`runtime_branch_id=c1` | 评论 [5815535379](https://github.com/guajun/llm-vs-zombies/issues/50#issuecomment-5815535379)；本地 run（未入库） |
> | 2. 真实游戏 a2 R0："抓取 → 回灌 → 再抓取"逐字节相等 | ✅ `tools/process_snapshot.py`（PR #42）；档位结论 PR #96 | ✅ 夹具级 R0（PR #42 与 `tests/test_process_snapshot.py`） | ✅ `m1-r0-001` 连续两次 `r0=true`（2026-09-22）；`m1-live-r0-01`（2026-09-24，PID 108208、190 区间、177,958,912 B、`r0=true`，挂起前后 `version`/`state_sha256` 一致） | 评论 [5778554737](https://github.com/guajun/llm-vs-zombies/issues/50#issuecomment-5778554737) / [5815535379](https://github.com/guajun/llm-vs-zombies/issues/50#issuecomment-5815535379)；本地 run；原始 `--r0` JSON 未入库 |
> | 3. 同源两分支实测 | ✅ | ✅ | ✅ [`docs/平行世界S(A)=S(B)验收.md`](https://github.com/guajun/llm-vs-zombies/blob/389803f10bef8d1b2abdd7b1cee0b9328ac26332/docs/平行世界S(A)=S(B)验收.md)（4000/4000）、[`docs/巨人分叉验收.md`](https://github.com/guajun/llm-vs-zombies/blob/389803f10bef8d1b2abdd7b1cee0b9328ac26332/docs/巨人分叉验收.md)（前缀 802）；#99 四条真机轨迹同分支复跑一致 | 入库 docs；#99 run 本地 |
> | 4. headless A0/A1 扰动探针 | ✅ `suspend_probes.py` + `Plan.pause_perturbations`（PR #96；文档修正 PR #109） | ✅ `tests/test_suspend_probes.py` 18 项 | ◑ `wall` 档通过；`focus`/`cursor` 施加失败（winerror 0 / 5），`verdict=unverified`。**扰动没施加成功，既不能判有影响，也不能判无影响** | 评论 [5815535379](https://github.com/guajun/llm-vs-zombies/issues/50#issuecomment-5815535379)；本地 `m1-live-a0-01`、`a1-pause-002`；[`docs/挂起扰动探针A0A1.md`](https://github.com/guajun/llm-vs-zombies/blob/389803f10bef8d1b2abdd7b1cee0b9328ac26332/docs/挂起扰动探针A0A1.md) |
>
> 四列之外的旧结论保持不变：跑过之后再灌回失败（`VirtualProtectEx` winerror 487）与部分恢复导致进程死亡仍以 2026-09-22 评论原文为准。

### 1.3 替换「## 剩余子项（本 issue 的唯一未完成部分）」整节

**拟议替换为：**

> ## 剩余子项（2026-09-26）
>
> 1. **A0/A1 焦点/光标档**：自动化不在交互式桌面，`SetForegroundWindow`/`GetCursorPos` 无法施加。需要在交互式桌面会话重跑到 `applied=true`，或由维护者明确把本 issue 缩减为 `wall` 档并接受结论边界。**不以此阻塞迁移**：[#103](https://github.com/guajun/llm-vs-zombies/issues/103) 首版允许冷重放 Arena，不要求无窗口/独立桌面。
> 2. **A2/A3（≥300 s 长挂起、冻结挂起）**：未做，需要放宽 pause 上界与 `process_snapshot --hold-seconds`。
> 3. **文档两处修正**：已由 [PR #109](https://github.com/guajun/llm-vs-zombies/pull/109) 合入（§5.2 命令补 B(0) 归一化、assess 路径改为 `seed-42-replay-1`）。
> 4. **档位结论**：R0 只在"同一未前进挂起窗口"内成立；R1–R3 当前不可达；冷重放同根验证不依赖 a2 R1 或真 fork（详见 `docs/同源分支自检.md` §6 与 #99 文档 §2）。

---

## 2. `#97`（P1：实施状态补记）

**动作：(a) 在正文顶部（标题下方）加当前状态指针，并标明「现象/影响/建议修法/验收」为 2026-09-24 历史问题描述；(b) 文末追加「实施状态」段**（旧字段/旧报告按原文保留）：

> ## 实施状态（2026-09-26 补记）
>
> - **已实现（旧仓 runner，[PR #106](https://github.com/guajun/llm-vs-zombies/pull/106)，merge `1de71fedcd46a246d5518c3e28469dbeb569d2f5`）**：`flags_to_complete` 改名 `rounds_to_complete`（缺省 1、范围 1..100）；旧名保留为**过渡只读别名**，两键同时出现时 `Plan.load` 直接拒绝；runner/CLI 只写新键；`full_cycle` 门槛仍固定为"至少一个 round"，不随圆数声明变化；`context.target` 改为 `complete_declared_rounds`；`docs/evaluation.md`、`docs/runtime-protocol.md`、`runtime/README.md`、`experiments/scenarios/jingdian12/README.md` 与 `experiments/configs/*` 统一写明 **1 round = 2 flag = 20 波**。
> - **跨轮拒绝（同一 PR）**：源局第一次动作前按 `capabilities.card_resubmit_mid_run`（当前 runtime 声明 `false`）拒绝 `rounds_to_complete>1`，给出明确错误并以退出码 2 结束，不静默早停；负例见 `tests/test_scenarios.py`、`tests/test_evaluation_lifecycle.py`；本机拒绝 run `eval-97-live-refuse2`（本地）。
> - **跨仓收口**：公共结果合同（计划目标 / 实际完成量 / 一个完整周期 / 达到声明目标 / 终止原因 / 截断原因 / 验证状态相互独立，禁止从 `full_cycle` 推断目标完成；旧字段保持历史 round 语义；新旧矛盾拒绝）由 [trajectory-core#3](https://github.com/pvz-agent-lab/trajectory-core/issues/3) 定义；[PR #4](https://github.com/pvz-agent-lab/trajectory-core/pull/4) **未合并**（2026-09-26 核对；权威状态以链接为准），env/rollout 运行时对接未验证；接入回链 [pvz-env#1](https://github.com/pvz-agent-lab/pvz-env/issues/1) 与 [#105](https://github.com/guajun/llm-vs-zombies/issues/105)。该合同是 [#104](https://github.com/guajun/llm-vs-zombies/issues/104) 的待满足条件，不阻塞旧仓 runner 与各仓独立开发。
> - **不改历史**：旧 plan 与旧报告不改写；适配器带输入版本与证据范围，旧报告不被覆盖。

---

## 3. `#100`（P1：纠正错误推断 + 登记路线矩阵要求）

### 3.1 替换候选方案 1 的「好处」句

**原文：**

> - 好处：窗口相关全局量（前台/焦点/光标/可见性）彻底与进程无关。

**拟议替换为：**

> - **待验证假设**：不创建窗口只保证"窗口对象不存在"，**不**自动证明代码不再读取前台/焦点/光标/可见性等全局输入——这类读取仍可能发生并返回失败值/常量，仍可能进入执行路径。只有当这些读取点被逐点定位、控制，并给出声明、回执与判等证据后，才可声明它们与轨迹无关。

### 3.2 替换候选方案 2 的「好处」句

**原文：**

> - 好处：仍是真窗口、真渲染（视频 demo 可能保住），但**我们的自动化天然扰动不到它**，别的程序也抢不到那个桌面上的前台。

**拟议替换为：**

> - **待验证假设**：仍是真窗口、真渲染（视频 demo 可能保住），但"我们的自动化扰动不到它"只是隔离假设，不等于"没有影响"，也不等于"进程不在交互桌面"（DirectDraw/音频、注入线程桌面绑定与 IPC 均需实测）。必须有声明、能力模式与观测覆盖才可据此替换任何门槛。

### 3.3 追加到正文末尾

> ## Review 更正与补充要求（2026-09-26，来自 [#101](https://github.com/guajun/llm-vs-zombies/issues/101)）
>
> 1. **不得由"没有窗口"或"扰动没施加成功"推断无关**：A0/A1 真机 `verdict=unverified` 的原因是扰动没施加成功（`SetForegroundWindow` winerror 0、`GetCursorPos` winerror 5），它既不能判"有影响"，也不能判"无影响"。
> 2. **路线矩阵逐项声明**：每条候选路线必须列出：仍存在哪些外部输入（前台/焦点/光标/可见性/桌面、墙钟、主机负载、IPC、音频设备等）、如何控制、如何证明干预实际发生（而非仅"计划施加"）、以及观测覆盖边界。
> 3. **门槛替换前置条件**：任何取消或替换旧门槛（窗口观察 25 ms/250 ms、`private_launch` 可见性、A0/A1、A2/A3）都必须附对应的新证据、能力模式与失败判据，并保留旧模式报告，不删除或改写既有失败档。
> 4. **范围与归属**：本项是 RFC 与路线验证，**不阻塞**基于现有模式的短程串行迁移（[#102](https://github.com/guajun/llm-vs-zombies/issues/102) 已明确 #100 是可选 headless 路线）；具体实现主要归 [pvz-env#1](https://github.com/pvz-agent-lab/pvz-env/issues/1)，通用钩子才进 [avz#1](https://github.com/pvz-agent-lab/avz/issues/1)；若模式变化，由 [#103](https://github.com/guajun/llm-vs-zombies/issues/103) 触发对应补验。
> 5. **磁盘未改 ≠ 运行时未变**："不修改游戏本体" 只表示磁盘上的 EXE/DLL/PAK 与玩家存档不被改写；注入与 overlay 会改变**进程内存中的执行路径**（钩子、跳转、常量替换、导出替换）。两者必须分开声明与取证：磁盘文件摘要未变不能当作运行时行为未变，内存执行路径的每项改变都要进入声明/回执/判等体系。

### 3.4 替换「## 明确不做」第 2 条

**原文：**

> - 不动游戏本体与 AvZ 上游；所有改动都走我们已有的 bootstrap/overlay + 声明/回执体系。

**拟议替换为：**

> - 不改写磁盘上的游戏 EXE/DLL/PAK 与玩家存档；注入与 overlay 仍会改变进程内存中的执行路径（钩子、跳转、常量替换、导出替换），两者必须分开声明、分别取证。
> - 不动 AvZ 上游；所有改动都走我们已有的 bootstrap/overlay + 声明/回执体系。

---

## 4. `#99`（P1/P2：#99 自身无 P1，但需去可选措辞并登记新旧证据口径）

### 4.1 原文行内替换

| 位置 | 原文 | 拟议替换 |
|---|---|---|
| 首行状态 | 状态：待实施的方案验证任务；范围已确认，铲子机制预检已完成。代码核对基线：main@c4d7e7387a99e27380dd70479d7c4f1d72d9c674。 | 状态（2026-09-26）：**部分完成**——受控铲子动作、四条真机轨迹、薄封存与只读加载基础能力已验证（见文末「实施状态与证据口径」）；**未通过**：首次击杀正向门槛、strict/完整两旗、正式生产者封口（trajectory-core#3 待交付）；原冻结计划、历史证据与旧报告不改。原「待实施」是 2026-09-24 历史状态。代码核对基线（原审查）：main@c4d7e7387a99e27380dd70479d7c4f1d72d9c674。 |
| 目标段 | 并建议将结果封存为一棵可被独立加载器读取的树 | 并将结果封存为一棵可被独立加载器读取的树（已确认纳入关闭范围） |
| 交付物段 | 树封包及离线加载样例（若纳入范围） | 树封包及离线加载样例（已确认纳入，必交） |

### 4.2 追加到正文末尾

> ## 实施状态与证据口径（2026-09-26 补记）
>
> 本节不修改原计划、验收清单与旧报告，只登记截至 2026-09-26 的事实状态。
>
> - **正式四轨迹已完成**（[PR #107](https://github.com/guajun/llm-vs-zombies/pull/107)，merge `346e495aaabd5ccb1b7d52ef75ee95c8b4f4b82a`）：对照/干预各原跑与复跑，`branches_reproduce=true`；共同前缀 101 个边界；RNG 首差异 tick 101；首个语义差异 tick 118；第一波内容差异 tick 601；窗口内无结局差异。工具与文档：`tools/issue99_shovel_fork.py`、[`docs/issue99-铲子同根分叉.md`](https://github.com/guajun/llm-vs-zombies/blob/389803f10bef8d1b2abdd7b1cee0b9328ac26332/docs/issue99-铲子同根分叉.md)。
> - **薄封存/只读加载是必须验收项，且基础能力已完成复验**：规范证据树为 `experiments/trees/issue99-fc2-shovel-fork`（`tree_id=ce770e531a225234b53ac3b810bc3209e4417b257410605f105bc38b0bc90220`，4 节点），seal 为 `work/issue99-fc2-seal.json`。复验记录：`tools/issue99_shovel_fork.py verify`（2026-09-25 复核，见 [#110](https://github.com/guajun/llm-vs-zombies/issues/110) 正文）、#110 对 164 份输入的全量摘要核对、trajectory-core [PR #2](https://github.com/pvz-agent-lab/trajectory-core/pull/2) 对真实 seal 的 `verify_seal(..., verify_nodes=False)`（4/4 节点绑定、两份报告摘要相符）。**正式"无活动写入者"的生产者封口合同**仍由 [trajectory-core#3](https://github.com/pvz-agent-lab/trajectory-core/issues/3) 交付（PR #4 未合并，2026-09-26 核对；状态以链接为准），不代表正式封包已通过。
> - **新旧版本证据不同，必须区分**：PR #107 文档中的 `issue99-jd12r` 运行与 `tree_id=529a2f4f…e447f` 是开发期证据（其树与报告未入库，原实验检出当前只保留 `fc2` 树）；正式验收使用 `issue99-fc2` 树（`ce770e…`，运行目录 `experiments/runs/issue99-fc2-*`，报告摘要 `05d83bc3…`/`68495027…`）。两者的结论一致（共同前缀 101、RNG 首差异 101、语义首差异 118、`gameplay_fork_demonstrated=true`），但运行身份、DLL/构建哈希、树 ID 与报告摘要不同，**不得互相充当复跑或封存证据**；旧报告保持原样。
> - **首次击杀门槛不因 #110/#111 关闭而提升**：`first_removal`（tick 941 / 33 槽位）不是击杀；#110 离线分析确认 tick 940 有 5 个进入死亡阶段，但同边界 33 个直接消失原因未知，无法证明全窗口绝对首次死亡；#111 在其新四条真机轨迹上证明了完整首杀（exact-store capture），但**不替代**本 issue 与 [#105](https://github.com/guajun/llm-vs-zombies/issues/105) 的正向首杀门槛。
> - **跨仓归属**：根/计划执行入口归 agent-rollout（[#105](https://github.com/guajun/llm-vs-zombies/issues/105)）；游戏动作与原生证据归 env（[#103](https://github.com/guajun/llm-vs-zombies/issues/103)）；格式、校验与只读加载归 trajectory-core（[#104](https://github.com/guajun/llm-vs-zombies/issues/104)）。本 issue 不复制各仓验收标准。
> - **负例**：旧工具与 `tests/test_issue99_shovel_fork.py`、`tests/test_tree_evidence.py`、`tests/test_evidence_codec.py` 已覆盖缺边界、被替换 manifest/报告、损坏父引用、篡改/缺文件；篡改字节、未封口轨迹、未知 schema、错误父边界等跨仓负例由 trajectory-core PR #2（125 项）与 #104 跟踪，目标/终止合同负例继续由 core#3 补充。

---

## 5. `#98`（P2：测量前置与边界冻结）

**动作：直接更新当前待办与验收**——原「要做的事」第 2、3 条与「验收」由下方文本取代（旧文本保留在 GitHub 编辑历史）；「为什么现在就要做」的数据表与「疑点」保留为 2026-09-24 历史观察，历史运行报告不改。完整新正文见同步预览 `issue-98.md`。

> ### 要做的事（2026-09-26 修订）
>
> 1. **基准对照跑**（同一 plan/seed，串行）：A 现状；B 跳过/关闭受控绘制（若协议支持）；C 降审计采样率或关掉粒子 shake 等非判决性流。每个配置分别绑定模式身份——`headless_mode`、绘制模式、审计流配置、窗口观察配置，加上构建/plan/seed——并**各自给出复跑/重放结论**（同配置冷重放的通过范围与同动作同轨迹结论）；性能档不得表述成原模式逐字段等价，只报告该配置证据面下的吞吐。
> 2. **热点拆分**：先盘点现有 audit 时间戳能覆盖 引擎 update / 受控绘制 / IPC / 审计序列化 / 窗口观察 中的哪几块；不能直接测量的成本报告为 `unknown/estimated`，必要时单独增加插桩，不强行输出精确占比。
> 3. **两套配置的提议**：训练档与计分档分别声明模式身份、证据覆盖与自身重放结论；训练档即使关闭绘制/审计流，也必须能给出"同动作同轨迹"的证据，只是证据面变窄并声明清楚。
> 4. **红线（不变）**：不许为了让基准好看而放宽 `S(A)==S(B)` 的判据（B0 归一化、逐边界 digests、冷重放）。
> 5. **非迁移前置**：性能验收独立于首轮结构迁移（[#102](https://github.com/guajun/llm-vs-zombies/issues/102)），不阻塞 core/env/rollout 的短程串行闭环；"几千条轨迹"是方向描述，不写成没有预算依据的完成条件。
>
> ### 验收（2026-09-26 修订）
>
> - 一张含 A/B/C 的对照表（模式身份、命令、plan、seed、tick 数、墙钟、tick/s、字节数、run 路径），每个配置附自己的复跑/重放结论。
> - 一条"瓶颈在哪"的结论（哪几块可测、占比多少；不可测项明确标 `unknown/estimated`），以及"训练档能把 tick/s 提到多少"的量级估计。
> - 测量前冻结起始边界、终点、tick 硬上限、墙钟预算与重复次数并报告波动；结论附原始证据；失败档保留。
> - 既有 `jd12-2flags-01` 平均速率（10.6 tick/s / 7 tick/s）是单配置历史观测（本地 run，未入库），只作待复核输入，不是已完成的 A/B/C 对照。

---

## 6. `#84`（P2：代码与真机证据分列）

**动作：直接更新当前状态**——(a) 替换正文「3. … 这是本 issue 的 open item」末句；(b) 用下方内容替换整个「## Open items」；(c) 限缩离线边界句（见下）。离线验证与历史结论保留；完整新正文见同步预览 `issue-84.md`。

**(a) 行内替换**

原文：`所以托管炮击目前**没有审计记录**——这是本 issue 的 open item。`

拟议替换：`该审计缺口已由 PR #89 的托管炮击审计补齐（只加审计调用、不改引擎语义；真机 digest 对账修复 PR #92），真机证据见下方 Open items 状态更新。`

**(b) 替换整个「## Open items」**

> ## Open items（2026-09-26 更新）
>
> 1. ~~托管动作未进审计~~ **已完成**：[PR #89](https://github.com/guajun/llm-vs-zombies/pull/89)（`9b00d703…`）在 `AAsm::Fire` 后只加一次审计调用，不改引擎调用序列/参数/顺序/返回值；真机 digest 对账修复 [PR #92](https://github.com/guajun/llm-vs-zombies/pull/92)（`40893d76…`）。见 [`docs/avz-script-hosting.md` §8](https://github.com/guajun/llm-vs-zombies/blob/389803f10bef8d1b2abdd7b1cee0b9328ac26332/docs/avz-script-hosting.md)。
> 2. ~~真机验收~~ **已核验**：#99 四条正式轨迹（[PR #107](https://github.com/guajun/llm-vs-zombies/pull/107)）每条 4 发 `hosted_fire`（tick 567、1168 各两发），严格 count/digest 对账与同分支复跑通过；#111 阶段 D 四条 `hosted-v2` 真机轨迹（[PR #117](https://github.com/guajun/llm-vs-zombies/pull/117)，merge `11917a78…`）沿用同一托管脚本完成严格审计、生命周期与封存验证。
> 3. ~~`ASetZombies`/`ASelectCards` 与 runtime `initialize` 重叠~~ **已决定**：托管副本保留 `ASetZombies`（出怪列表没有其他路径），移除 `ASelectCards`（runtime `initialize` 拥有选卡）；见 `logger/avz/hosted/jing_dian_12.cpp` 顶部说明。
> 4. **剩余（协议扩展，不在原范围）**：托管动作不是一等 `fire` 动作——`hosted_fire` 记录不能作为请求动作再次提交或重放（无 request journal、无 `ordinal`）；冷重放通过**重新执行托管脚本**产生炮击并核对审计，不得既重跑脚本又重放日志造成重复发炮。要变成可重放的一等动作需要新协议 op + journal + replay 支持。
> 5. **结论口径**：#88 已关闭（根因 [PR #90](https://github.com/guajun/llm-vs-zombies/pull/90)），但真机结论只认绑定构建哈希的运行与报告，不从 issue 关闭自动推断。

**(c) 限缩离线边界句**

原文：

> - **明确不证明**：真实引擎行为、真实 `AAsm::Fire`、"十二炮能打赢两旗"——那必须真机

拟议替换：

> - **明确不证明（2026-09-23 离线边界）**：真实引擎行为、真实 `AAsm::Fire`、"十二炮能打赢两旗"——当时必须真机。后续 #99 四条轨迹与 #111 阶段 D 在**冻结短程窗口**内核验了托管脚本执行与炮击审计（每条 4 发 `hosted_fire`、同分支原跑=复跑），**不证明完整两旗、strict 或"十二炮能打赢两旗"**；完整两旗仍以 #99/#105 的 strict 门槛为准。

---

## 7. `#72`（P2：事实状态与 ADR 引用）

**动作：重排完整正文**——原正文的架构/方案/接口/待办块**整体**（不局部编辑）移入带日期的历史快照；正文前部新增「当前状态（2026-09-26，以此为准）」。旧块中的"只知道 2 个字段""完整性检查产出全集""不可测试不可评审"及 P1/P3 待做只作历史规划读。完整新正文见同步预览 `issue-72.md`。

**(a) 正文前部新增当前事实块**

> ## 当前状态（2026-09-26，以此为准）
>
> - **仓库事实**：组织 fork [pvz-agent-lab/avz](https://github.com/pvz-agent-lab/avz) 已建；`guajun/AsmVsZombies` 分支 `lvz/l1-determinism-primitives@e266e18aa447b2732113ff994aa81f32b70d2214`（基线 `c42676c2…`）。"fork 已建"成立；"主仓采用"未做（旧仓仍 pin `c42676c2…`，未切换、未做等价验证）；"上游 PR 已提"否（相关补丁未创建上游 PR）。
> - **治理交付**：[avz PR #2](https://github.com/pvz-agent-lab/avz/pull/2)（merge `c9f841d01d4d1f79c570c77ab01b33007bca04af`）交付 5 份文档：`docs/lvz/adr-0001-l1-ownership.md`（ADR：env/core/rollout 边界遵循 #102）、`docs/lvz/patch-registry.md`（F1–F5/O1–O11 逐项登记）、`docs/lvz/upstream-and-pins.md`（上游基线 SHA 与组织 fork 消费 SHA 两个固定点；采用/升级/回退六处一致性，旧归档不可改写）、`docs/lvz/README.md`、README 链接。**文档合入不等于采用 fork 或等价验证通过**；[avz#1](https://github.com/pvz-agent-lab/avz/issues/1) 保持 OPEN。
> - **P1 [#74](https://github.com/guajun/llm-vs-zombies/issues/74) 已关闭（2026-09-23）**：`b0_normalization` 统一实现（单表 + 单回执 + 双向集合差）已交付。**捕获范围不是完整游戏状态**：检查只证明"已捕获状态内没有未分类字段"，`coverage.complete_game_state=false`（[`docs/b0-normalization-native.md` §5](https://github.com/guajun/llm-vs-zombies/blob/389803f10bef8d1b2abdd7b1cee0b9328ac26332/docs/b0-normalization-native.md)）。
> - **P3 [#82](https://github.com/guajun/llm-vs-zombies/issues/82) 已关闭（2026-09-23）**：独立检出可一键准备并自检；"并行多世界"仍归 #19/#105。
> - **P2（strict：十次冷启动 + 完整两旗）仍未完成**：当前证据是 #99 冻结短程窗口与 #111 阶段 D 短程真机；strict 未请求。
> - **P4**：launcher 注入 `LVZ_BRANCH_ID` 已完成（PR #96）并有真机记录；A0/A1 已有实现与离线夹具、真机 `wall` 档通过，焦点/光标档因自动化不在交互式桌面保持 `unverified`（#50 剩余项）。
> - **角色**：本 issue 保留架构来源；迁移的仓库归属、版本组合与总体状态由 [#102](https://github.com/guajun/llm-vs-zombies/issues/102) 集中维护；后续采用/等价/上游动作由 [avz#1](https://github.com/pvz-agent-lab/avz/issues/1) 跟踪，本 issue 不重复盘点。
>
> 以下内容为 **2026-09-23 历史规划快照**（架构分层、方案比较、接口清单与当时的下一步），已整体被上方当前状态取代，保留用于溯源，不再作为当前指导。

**(b) 历史快照**

完整正文把原 `#72` 正文（从「## 目的」起至末尾）整体放入 `<details><summary>历史规划快照（2026-09-23，已被 2026-09-26 当前状态取代）</summary>…</details>`；不改写其中任何历史结论。

---

## 8. `#101` 自身：建议追加的收口说明

**动作：待父任务确认后追加到 #101 正文末尾（或作为评论发布）**（同步时把两处 `<本 PR 合并 SHA>` 替换为实际合并提交 SHA，或改用 `main` 的等效链接）：

> ## 整改登记（2026-09-26）
>
> 逐项事实状态、四列（实现/离线验证/真机验证/证据可取回）口径、证据链接、未完项与唯一责任 issue 已登记在仓库文档 [`docs/issue101-整改矩阵.md`](https://github.com/guajun/llm-vs-zombies/blob/<本 PR 合并 SHA>/docs/issue101-整改矩阵.md)；配套拟议正文见 [`docs/issue101-issue修订稿.md`](https://github.com/guajun/llm-vs-zombies/blob/<本 PR 合并 SHA>/docs/issue101-issue修订稿.md)。
>
> - P1：#50 的状态冲突已由 PR #96/#108/#109 与真机记录对齐；#97 的 runner 部分已由 PR #106 落实，版本化公共结果合同由 trajectory-core#3（PR #4 未合并，2026-09-26 核对；以链接为准）承接；#100 已纠正"不建窗口＝窗口无关"的推断，路线矩阵与门槛替换要求已登记，且不阻塞基础迁移。
> - P2 治理与状态：AvZ 治理文档已由 [avz PR #2](https://github.com/pvz-agent-lab/avz/pull/2)（merge `c9f841d01d4d1f79c570c77ab01b33007bca04af`）交付（ADR/差异表/pin 说明）；**fork 采用、等价验证与上游提交仍未完成**（[avz#1](https://github.com/pvz-agent-lab/avz/issues/1) 保持 OPEN）。#84 的离线边界与当前状态、#99 的部分完成状态、#97 的历史问题标记均见配套修订稿。
> - 验收口径：矩阵把"可并行开发"与"可完成验收/最终切换"分开登记——公共合同与生产者封口是 #104 的待满足条件，首次击杀是 #99/#105 的待满足条件；它们不阻塞各仓独立开发，但不因此视为已满足。
> - 未提升项：#110/#111 已关闭但**不自动提升**首次击杀或迁移验收；#99/#105 的正向首杀门槛仍未通过；#102 迁移本体未完成。
> - 历史结论（#50 的 winerror 487 失败档、A0/A1 `unverified`、#110 全窗口首杀 `unverified`、#99 开发期 `jd12r` 证据）均原样保留。
