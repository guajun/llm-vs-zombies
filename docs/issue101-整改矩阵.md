# `#101` 整改矩阵与交接（2026-09-26）

审查对象：[#101 迁移前 Review：对齐现有 issue 状态、验收语义与跨仓库责任](https://github.com/guajun/llm-vs-zombies/issues/101)。
本文件在版本管理中逐项登记 #101 checklist 的**事实状态、证据来源、未完项与唯一责任 issue**，供父任务 review 后同步 issue。拟议 issue 正文见 [issue101-issue修订稿](issue101-issue修订稿.md)。

- 基线：本 worktree 的 `origin/main@389803f10bef8d1b2abdd7b1cee0b9328ac26332`（2026-09-26）；#101 原审查基线为 [`main@c4d7e7387a99e27380dd70479d7c4f1d72d9c674`](https://github.com/guajun/llm-vs-zombies/tree/c4d7e7387a99e27380dd70479d7c4f1d72d9c674)。
- 本文件只做登记与链接：**不启动游戏、不重跑昂贵测试、不改代码/生产行为、不覆盖或重写任何旧报告与失败档**。
- 本文件**不关闭 #101**；**不宣称 #102 迁移完成**；`#110`/`#111` 已关闭**不自动提升**首次击杀或迁移验收。
- 真机结论均转抄自既有带日期的运行记录/评论/文档，本文件不产生新的真机通过结论。
- 列含义：`实现`＝代码/合同在版本管理中；`离线验证`＝无游戏夹具/测试/静态检查；`真机验证`＝真实游戏运行记录；`证据可取回`＝证据位置，分为 `入库`（版本管理内）、`本地 run`（gitignored 的原实验检出运行目录，按运行名可找到）、`评论/报告留档`（数字已留档，原始文件未入库）。
- 状态词汇：`已落实` / `进行中`（有 owner 与在途 PR）/ `待实施` / `有理由延期`。
- 验收口径：**不阻塞独立开发 ≠ 满足验收/最终切换条件**。core#3 的公共结果合同与生产者封口是 #104 的待满足条件，#99/#105 的首次击杀是各自验收的待满足条件；它们不阻塞 core 提取与各仓独立开发，但不能用窗口化/真 fork 的非前置政策豁免，也不因相应 issue 创建或关闭而视为已满足。

## 0. 结论摘要

**P1**

- **#50**：launcher 分支身份（PR #96）、a2 R0（真机 `m1-r0-001`/`m1-live-r0-01`）、同源两分支（平行世界/巨人分叉/#99 四轨迹）均已实现并有真机证据；A0/A1 实现与离线夹具通过，真机 `wall` 档通过、`focus`/`cursor` 因自动化不在交互式桌面而保持 `unverified`——**不能判有影响，也不能判无影响**；A2/A3 未做。R0 只是"同一未前进挂起窗口"档，R1–R3 当前不可达；**冷重放同根验证不依赖 a2 R1 或真 fork**。
- **#97**：旧仓 runner 的改名、过渡别名、矛盾拒绝与跨轮显式拒绝已由 PR #106 落实并离线验证；**版本化公共结果合同**（周期完成/声明目标/终止与截断/验证状态分离，禁止用 `full_cycle` 推断目标完成）由 [trajectory-core#3](https://github.com/pvz-agent-lab/trajectory-core/issues/3) 承接，PR [#4](https://github.com/pvz-agent-lab/trajectory-core/pull/4) 在途。P1 没有"以后再说"的空洞延期。
- **#100**：RFC 未实施，本整改只纠正"不创建窗口＝窗口无关"的错误推断并登记路线矩阵、门槛替换与失败判据要求；#102 已明确它**不阻塞**基于现有模式的短程串行迁移。

**P2**：#72 的补丁盘点与 ADR 归 [pvz-agent-lab/avz#1](https://github.com/pvz-agent-lab/avz/issues/1)（另一任务执行，本文件不重复盘点）；#84 的代码实现与真机证据已可由 #99/#111 的运行绑定，真机结论只认带构建哈希的运行；#99 的薄封存/只读加载为**必须验收项且基础能力已复验**，新旧版本证据不同（开发期 `jd12r` 树 vs 正式 `fc2` 树）必须区分；#98 是无实现代码的性能研究，本整改只登记模式身份、测量未知项、固定窗口与非迁移前置四项判据。

## 1. 来源与证据清单

| 内容 | 位置 | 日期 / 标识 |
|---|---|---|
| #101 原审查基线 | `main@c4d7e7387a99e27380dd70479d7c4f1d72d9c674` | 2026-09-24 |
| 本文件基线 | `origin/main@389803f10bef8d1b2abdd7b1cee0b9328ac26332` | 2026-09-26 |
| launcher 分支身份 + A0/A1 实现 | [PR #96](https://github.com/guajun/llm-vs-zombies/pull/96)，merge `c4d7e738…` | 2026-09-24 |
| #50 真机复核评论（R0/launcher/A0/A1） | [#50 comment 5815535379](https://github.com/guajun/llm-vs-zombies/issues/50#issuecomment-5815535379) | 2026-09-24 |
| a2 R0 首次真机通过（连续两次） | [#50 comment 5778554737](https://github.com/guajun/llm-vs-zombies/issues/50#issuecomment-5778554737) | 2026-09-22 |
| R0 范围更正与跑过后恢复失败 | [#50 comment 5778657897](https://github.com/guajun/llm-vs-zombies/issues/50#issuecomment-5778657897) | 2026-09-22 |
| #97 runner 整改 | [PR #106](https://github.com/guajun/llm-vs-zombies/pull/106)，merge `1de71fedcd46a246d5518c3e28469dbeb569d2f5` | 2026-09-24 |
| #99 四轨迹与薄数据闭环 | [PR #107](https://github.com/guajun/llm-vs-zombies/pull/107)，merge `346e495aaabd5ccb1b7d52ef75ee95c8b4f4b82a` | 2026-09-24 |
| 托管炮击审计 | [PR #89](https://github.com/guajun/llm-vs-zombies/pull/89)（`9b00d703…`）、真机对账修复 [PR #92](https://github.com/guajun/llm-vs-zombies/pull/92)（`40893d76…`） | 2026-09-23 |
| #110 离线死亡判定 | [PR #118](https://github.com/guajun/llm-vs-zombies/pull/118)，merge `389803f10bef8d1b2abdd7b1cee0b9328ac26332` | 2026-09-26 |
| #111 阶段 D 真机验收 | [PR #117](https://github.com/guajun/llm-vs-zombies/pull/117)，merge `11917a78ed4f13f90f070e05d8330f9e5c40384a` | 2026-09-26 |
| trajectory-core 离线包 | [PR #2](https://github.com/pvz-agent-lab/trajectory-core/pull/2)，merge `afb770ee36fdacbc06df6ef150c8258179b7dc73` | 2026-09-26 |
| trajectory-core 目标/封口合同 | [core#3](https://github.com/pvz-agent-lab/trajectory-core/issues/3)；[PR #4](https://github.com/pvz-agent-lab/trajectory-core/pull/4) OPEN，**未合并**（2026-09-26 09:10Z 观察 head `c62f308e…`，此前 `e68e092e…`；head 持续更新，以 PR 页面为准）；本地 190 项与 8 个 CI job 通过；env/rollout 运行时对接未验证 | 2026-09-26 |
| 迁移总任务与三个验收 issue | [#102](https://github.com/guajun/llm-vs-zombies/issues/102) / [#103](https://github.com/guajun/llm-vs-zombies/issues/103) / [#104](https://github.com/guajun/llm-vs-zombies/issues/104) / [#105](https://github.com/guajun/llm-vs-zombies/issues/105) | 2026-09-24 起 |
| AvZ 薄 fork 治理 | [pvz-agent-lab/avz#1](https://github.com/pvz-agent-lab/avz/issues/1)（另一任务；本文件只引用）；[avz PR #2](https://github.com/pvz-agent-lab/avz/pull/2) OPEN，按 review 修复中，**未合并** | OPEN |

## 2. P1 项

### 2.1 #50：状态四列统一与 R0 语义

`#50` 的四项交付物按 #101 要求的四列重排如下（owner：[#50](https://github.com/guajun/llm-vs-zombies/issues/50)；迁移对应验收 [#103](https://github.com/guajun/llm-vs-zombies/issues/103)）：

| 交付物 | 实现 | 离线验证 | 真机验证 | 证据可取回 |
|---|---|---|---|---|
| 1. launcher 分支身份 | ✅ PR #96：`launcher/native_launcher.cpp` 建进程前校验并 `SetEnvironmentVariableW(L"LVZ_BRANCH_ID")`、主线程恢复前写 receipt；`src/llm_vs_zombies/launcher.py` 缺省分支名 = run 目录名 | ✅ PR #96 断言；[PR #108](https://github.com/guajun/llm-vs-zombies/pull/108) 的 isolation fixture 核验 `LVZ_BRANCH_ID` 注入 | ✅ `m1-live-a-s42-c0`（2026-09-24）五处一致：`launcher.json.branch_id` = 目录名 = `sandbox/native-receipt.json.branch_id` = `hello.branch.branch_id` = `manifest.branch.branch_id`；冷重放报告 `source_branch_id=c0`/`runtime_branch_id=c1` | 评论 [5815535379](https://github.com/guajun/llm-vs-zombies/issues/50#issuecomment-5815535379)；本地 run `work/hosted-live/experiments/runs/m1-live-a-s42-c0`（未入库） |
| 2. a2 R0（抓取→回灌→再抓取逐字节相等） | ✅ `tools/process_snapshot.py`（[PR #42](https://github.com/guajun/llm-vs-zombies/pull/42)，`d931be43…`）；档位结论 PR #96 | ✅ 夹具级 R0（PR #42 + `tests/test_process_snapshot.py`） | ✅ `m1-r0-001` 连续两次 `r0=true`（2026-09-22）；`m1-live-r0-01`（2026-09-24，PID 108208，`region_count=190`、177,958,912 B，`r0=true`，挂起前后 `version`/`state_sha256` 一致） | 评论 [5778554737](https://github.com/guajun/llm-vs-zombies/issues/50#issuecomment-5778554737) / [5815535379](https://github.com/guajun/llm-vs-zombies/issues/50#issuecomment-5815535379)；本地两处 run；`--r0` 原始 JSON 输出未入库 |
| 3. 同源两分支实测 | ✅ | ✅ | ✅ [平行世界S(A)=S(B)验收](平行世界S(A)=S(B)验收.md)（4000/4000 边界 digests 全同、P3 独立检出复验）、[巨人分叉验收](巨人分叉验收.md)（前缀 802 全同、差异只在分叉后）；#99 四条真机轨迹同分支复跑一致 | 入库 docs；#99 run 本地（gitignored） |
| 4. headless 约束实测（A0/A1） | ✅ `src/llm_vs_zombies/suspend_probes.py` + `Plan.pause_perturbations`（PR #96；文档修正 PR #109） | ✅ `tests/test_suspend_probes.py` 18 项（扰动失败判 `unverified`，不静默） | ◑ `wall` 档通过；`focus` 恒被拒（`SetForegroundWindow` winerror 0）、`cursor` access denied（`GetCursorPos` winerror 5），A0/A1 `verdict=unverified`（2026-09-24）。**扰动没施加成功，既不能判有影响也不能判无影响** | 评论 [5815535379](https://github.com/guajun/llm-vs-zombies/issues/50#issuecomment-5815535379)；本地 `m1-live-a0-01`、`a1-pause-002`；[挂起扰动探针A0A1](挂起扰动探针A0A1.md) |

| R# | #101 检查项 | 状态与证据 | 未完项 / 延期 | 唯一责任 |
|---|---|---|---|---|
| R1 | 将状态统一为"实现/离线验证/真机验证/证据可取回"四列；只补缺失证据和验证，不重写已存在功能 | ✅ 上表即四列重排；PR #96、#108、#109 已把代码与文档补到与评论一致 | 无（本矩阵与拟议 #50 正文提供可同步文本） | #50（迁移验收 #103） |
| R2 | 明确 R0 不是续跑恢复，冷重放同根验证不依赖 a2 R1 或真 fork | ✅ [同源分支自检](同源分支自检.md) §6：R0 只在"抓取后未再前进"的同一挂起窗口成立；跑过再灌回会撞 `VirtualProtectEx` winerror 487；R1–R3 不可达。[#99 文档](issue99-铲子同根分叉.md) §2 明确"同根"由冷重放到同一根并验证，不是进程克隆；#102 依赖方向也明确 a2 恢复/真 fork/并行/strict 不作为短程串行骨架前置 | R1–R3 与真 fork 属新实现（不在 #101 范围）；不改写 #50 comment 1 的失败档 | #50（档位结论）；#102（依赖方向） |

### 2.2 #97：计划目标与完成判据分离

| R# | #101 检查项 | 状态与证据 | 未完项 / 延期 | 唯一责任 |
|---|---|---|---|---|
| R3 | 新合同分别报告"完成一个完整周期""达到声明目标""终止/截断原因"，禁止迁移验收只读取 `full_cycle` | ◑ 旧仓 runner：`full_cycle` 门槛语义已写清（至少一个 round，不随 `rounds_to_complete` 变化，见 [evaluation.md](evaluation.md)）；新结果合同由 [core#3](https://github.com/pvz-agent-lab/trajectory-core/issues/3) 定义（计划目标/实际完成量/周期/目标/终止/截断/验证状态分离）；core PR [#4](https://github.com/pvz-agent-lab/trajectory-core/pull/4) 在途 | 等 core#3 合入后由 env/rollout 接入（[pvz-env#1](https://github.com/pvz-agent-lab/pvz-env/issues/1)、[#105](https://github.com/guajun/llm-vs-zombies/issues/105)）。P1 有明确 owner、步骤与验收，不是空洞延期 | trajectory-core#3 |
| R4 | 旧字段沿用历史 round 语义，不默默除以二；新旧字段同时出现且矛盾时拒绝 | ✅ PR #106：`flags_to_complete` → `rounds_to_complete`（缺省 1，范围 1..100）；旧名是**过渡只读别名**，两键同时出现 `Plan.load` 直接报错；CLI 只写新键；[evaluation.md](evaluation.md) 明写 1 round = 2 flag = 20 波 | 无 | PR #106（旧仓；合同侧 core#3） |
| R5 | 保留旧 plan 和旧报告；以版本化适配器解释，不覆写历史证据 | ✅ PR #106 `Plan.load` 读旧键并映射；旧 plan/历史报告不改写；unknown schema fail closed（另见 trajectory-core PR #2 的 legacy 闭包与旧格式兼容） | 无 | PR #106（旧仓）；trajectory-core#1（读取器） |
| R6 | 跨多 round 未支持时，执行前明确拒绝或声明能力限制；新增目标未达到的负例 | ✅ PR #106：源局第一次动作前按 `capabilities.card_resubmit_mid_run`（当前 runtime 为 `false`）拒绝 `rounds_to_complete>1`，退出码 2 并保留原因；负例 `tests/test_scenarios.py`、`tests/test_evaluation_lifecycle.py`；本机拒绝 run `eval-97-live-refuse2`（本地） | 运行中重新提交卡片是新能力，不在 #101 整改范围；core#3 继续补"目标未达到"负例 | PR #106（旧仓）；trajectory-core#3 |

### 2.3 #100：隔离假设与轨迹无关证明

| R# | #101 检查项 | 状态与证据 | 未完项 / 延期 | 唯一责任 |
|---|---|---|---|---|
| R7 | 路线矩阵逐项声明仍有哪些外部输入、如何控制、如何证明干预实际发生、观测覆盖边界 | ⏸ 未实施。本整改在拟议 #100 正文中把"不建窗口＝窗口相关全局量彻底无关""自动化扰动不到独立桌面"标为**待验证假设**，并要求路线矩阵逐项声明：外部输入清单、控制方式、干预发生证据、观测覆盖边界 | 归 #100 的 RFC 结论；实现归 [pvz-env#1](https://github.com/pvz-agent-lab/pvz-env/issues/1)；通用钩子才进 [avz#1](https://github.com/pvz-agent-lab/avz/issues/1)。延期理由：#102 明确 #100 是可选 headless 路线，不阻塞基础迁移 | #100（RFC）；pvz-env#1（实现） |
| R8 | 取消/替换旧门槛须有新证据、能力模式与失败判据；保留旧模式报告 | ⏸ 当前没有任何门槛被取消/替换（#100 正文声明不改代码、不删失败档）。拟议正文登记替换前置条件，[#103](https://github.com/guajun/llm-vs-zombies/issues/103) 明确"#100 路线只在选定模式变化后触发相应补验" | 若未来采纳某路线并按新能力模式替换窗口验收，需另开证据/门槛变更任务 | #100；#103（模式变化后的补验触发） |
| R9 | 本项仍是 RFC 与路线验证，不阻塞短程串行迁移；实现主要归环境仓库，通用钩子才进 AvZ fork | ✅ 文档化：#102 正文"#100 是可选 headless 路线，不把未决的窗口化或真实 fork 强加为基础迁移阻塞"；#103 正文允许首版冷重放 Arena，a2 R0/真 fork/独立桌面/无窗口均为独立能力声明 | 无 | #102（依赖方向）；#100（研究范围） |

## 3. P2 项

### 3.1 #72：事实状态、原语归属与 ADR

| R# | #101 检查项 | 状态与证据 | 未完项 / 延期 | 唯一责任 |
|---|---|---|---|---|
| R10 | 更新事实状态并修复 fork 分支/提交/差异表链接；"fork 已建""主仓采用""上游 PR 已提"分别报告 | ◑ [#102 正文](https://github.com/guajun/llm-vs-zombies/issues/102) 已记录：组织 fork `pvz-agent-lab/avz` 已建；既有 `guajun/AsmVsZombies` 分支 `lvz/l1-determinism-primitives@e266e18aa447b2732113ff994aa81f32b70d2214`（基线 `c42676c2`）；默认 master 不表示采用；**未提上游 PR**。#72 正文与损坏链接待同步 | 补丁清单、差异表与上游节奏归 [avz#1](https://github.com/pvz-agent-lab/avz/issues/1)（另一任务），本文件不重复盘点；#72 正文修订见拟议稿 | pvz-agent-lab/avz#1（盘点/ADR）；#72（架构来源） |
| R11 | 原语清单注明捕获范围；双向集合差检查通过不得写成完整游戏状态覆盖 | ✅ [b0-normalization-native](b0-normalization-native.md) §5：检查范围是**已捕获状态**，`coverage.complete_game_state=false` 不变 | #72 正文需链接该边界；本整改不重写原语清单 | #72（正文）；avz#1（清单本体） |
| R12 | 用一条 ADR 修订"所有游戏地址原语一律进 AvZ"：通用原语/钩子归薄 fork；环境专用原生适配可在环境仓库集中维护；明确为新架构决定，不追溯改写历史 | ⏸ 未写。已登记到 avz#1 的治理范围（"只承接通用游戏访问原语、版本适配及必要生命周期钩子；LVZ 初始化目标、证据判等、IPC、窗口沙箱政策仍在 env"） | 由 avz#1 的独立任务执行；延期理由：盘点与 ADR 需要 AvZ fork 上下文，避免两个任务并行产出竞争结论 | pvz-agent-lab/avz#1 |
| R13 | #72 保留架构来源角色；迁移本体集中维护仓库归属与版本组合，不再另设竞争总体状态表 | ✅ 文档化：#102 正文为跨仓唯一总跟踪；本文件只记录 #101 review 状态，不承担迁移完成状态；#72 不再维护总体状态表 | 版本组合清单本身是 #102 关闭条件（未完成），由 #102 主跟踪 | #102 |

### 3.2 #84：托管脚本与炮击审计

| R# | #101 检查项 | 状态与证据 | 未完项 / 延期 | 唯一责任 |
|---|---|---|---|---|
| R14 | 代码已实现与真机证据已核验分别列出；检查历史运行能否满足真实托管与动作审计证据，缺什么补什么 | ✅ **代码实现**：托管脚本 [PR #85](https://github.com/guajun/llm-vs-zombies/pull/85)（`1b019acb…`）、状态落盘 [PR #86](https://github.com/guajun/llm-vs-zombies/pull/86)（`23dfe15d…`）、托管炮击审计 [PR #89](https://github.com/guajun/llm-vs-zombies/pull/89)（`9b00d703…`）、digest 真机对账修复 [PR #92](https://github.com/guajun/llm-vs-zombies/pull/92)（`40893d76…`）、协程双重恢复修复 [PR #90](https://github.com/guajun/llm-vs-zombies/pull/90)（`bad0ce2b…`）。**真机证据**：#99 四条正式轨迹（PR #107）每条 4 发 `hosted_fire`（tick 567、1168 各两发），同分支复跑一致；[[avz-script-hosting](avz-script-hosting.md) §8](avz-script-hosting.md) 记录严格 count/digest 对账 | 详见 R15。托管动作不是一等 `fire` 动作：`hosted_fire` 记录不能作为请求动作再次提交或重放（无 request journal、无 `ordinal`）；冷重放通过**重新执行托管脚本**产生炮击并核对审计，不得既重跑脚本又重放日志造成重复发炮。这是请求动作重放层面的限制（`docs/avz-script-hosting.md` §8.6），属协议扩展议题，不在 #84 原文范围 | #84（合并登记）；#99（运行证据） |
| R15 | 在 #99 正式四轨迹中验收脚本动作覆盖与复跑；绝不因 #88 关闭自动推断所有真机测试通过 | ✅ #99 四条轨迹：对照/干预各原跑=复跑（`branches_reproduce=true`），`hosted_fire_count=4`/条；`hosted_fire`（脚本发炮）与 `action`（实验干预）在审计里分开记录，轨迹里只有后者是请求动作（[issue99-铲子同根分叉](issue99-铲子同根分叉.md) §6/§8）；冷重放按"重新执行托管脚本 + 核对 `hosted_fire` 记录"通过，不把日志当请求动作重放。#88 已关闭（根因 PR #90），但本项真机结论只以上述绑定构建哈希的运行与 #111 阶段 D（PR #117）为准 | 无（后续托管协议扩展另案） | #84；#99（证据）；#111（原生验收） |

### 3.3 #99：薄数据闭环与验收口径

| R# | #101 检查项 | 状态与证据 | 未完项 / 延期 | 唯一责任 |
|---|---|---|---|---|
| R16 | 去掉可选措辞；保持薄证据层，不扩展到 reward、数据集运营和训练 | ◑ 正文主体已是"已确认纳入"，但**交付物仍残留"（若纳入范围）"**；拟议稿给出替换。薄边界在 [issue99-铲子同根分叉](issue99-铲子同根分叉.md) §7 明确：只有轨迹、根/父身份、摘要校验、只读加载与复跑报告 | 正文同步由父任务执行；reward/数据集管理是明确非目标（#102/#111 同口径） | #99（正文）；#104（薄合同范围） |
| R17 | 迁移验收补充篡改、缺文件、未封口轨迹、错误父边界、未知 schema 版本的负例及预期错误 | ◑ 旧仓：`tests/test_issue99_shovel_fork.py`（缺边界、被替换 manifest/报告）、`tests/test_tree_evidence.py`（损坏父引用/缺父节点）、`tests/test_evidence_codec.py`（篡改/缺文件）；跨仓：trajectory-core PR #2 的 125 项含路径越界、篡改、坏 schema、竞争写入等负例 | "未封口/无活动写入者"的**生产者封口合同**与目标/终止合同负例由 [core#3](https://github.com/pvz-agent-lab/trajectory-core/issues/3)（PR #4 在途）交付；#104 保持 OPEN | trajectory-core#3（正式封口）；#104（验收） |
| R18 | 根/计划执行入口归 rollout；游戏动作与原生证据归 env；格式/校验/只读加载归 core；迁移本体关联，不复制 #99 四轨迹标准 | ✅ 文档化：#102 责任表 + [#103](https://github.com/guajun/llm-vs-zombies/issues/103)/[#104](https://github.com/guajun/llm-vs-zombies/issues/104)/[#105](https://github.com/guajun/llm-vs-zombies/issues/105) 分工；#105 明确"实验规格以 #99 为唯一来源，避免另复制漂移的计划" | 无（实现接入另行跟踪） | #102（归属）；agent-rollout#1 / pvz-env#1 / trajectory-core#1 |
| R19 | 分开报告"复现与封存基础通过"和"玩法语义分叉成功演示"；窗口内无语义差异不能冒充后者，也不抹掉已通过的基础能力 | ✅ 正式 `fc2` 报告 `verdict`：`branches_reproduce=true`、`shared_prefix_ticks=101`、`first_rng_difference.tick=101`、`first_simulation_difference.tick=118`、`gameplay_fork_demonstrated=true`；同时保留"窗口内无结局差异"的限制（[issue99-铲子同根分叉](issue99-铲子同根分叉.md) §6.3/§8） | 首次击杀门槛不在此提升：`first_removal`（tick 941/33 槽位）不是击杀；#110 离线分析只能确认 tick 940 有 5 个进入死亡阶段，33 个同边界直接消失原因未知，**全窗口绝对首次击杀仍 unverified**；#111 在新真机轨迹上证明完整首杀，但不替代 #99/#105 的正向门槛 | #99（口径）；#110/#111（补证，已关闭不自动提升） |

### 3.4 #98：性能测量的模式语义与窗口

| R# | #101 检查项 | 状态与证据 | 未完项 / 延期 | 唯一责任 |
|---|---|---|---|---|
| R20 | 先盘点可支持配置与时间戳覆盖；无法直接测量的成本报告为未知/估计，必要时单独增加插桩，不强行输出精确占比 | ⏸ 未实施（仓库无 A/B/C 测量文档；现有 `jd12-2flags-01` 速率是**单配置历史观测**，本地 run，未入库）。拟议正文要求：先盘点时间戳覆盖，未知成本标 `unknown/estimated`，必要时另加插桩 | 归 #98 本体；延期理由：#102 把 #98 列为独立性能研究，不是基础迁移前置 | #98 |
| R21 | A/B/C 分别绑定模式身份、证据覆盖和自身重放结论；性能档不能冒充原模式逐字段等价 | ⏸ 未实施。拟议正文要求每个配置绑定 `headless_mode`/绘制/审计流/窗口观察配置与构建/plan/seed，并声明证据面差异 | 同上 | #98 |
| R22 | 固定测量窗口、终点和重复次数，给出吞吐波动；性能验收独立于首轮结构迁移，不把"几千条轨迹"写成没有预算依据的完成条件 | ⏸ 未实施。拟议正文要求冻结窗口（起点/终点/tick 上限/墙钟预算）、重复次数与波动口径，并明确独立于 #102 | 同上 | #98 |

## 4. #101 关闭条件对账

| C# | 关闭条件 | 状态 | 说明 |
|---|---|---|---|
| C1 | 每项发现有修订链接、实现 issue 或有理由的延期决定；P1 不可仅以"以后再说"关闭 | ✅ 本矩阵 26 项逐项给出证据/owner；三条 P1 全部有在途或已落实的收敛路径（#50 证据已足、#97 core#3/PR #4、#100 修正推断+非阻塞登记） | 待父任务同步 issue 正文后由维护者判定关闭 |
| C2 | 实现/未验证/证据缺失/假设明确区分；旧实验结论不被覆盖 | ✅ 四列 + 状态词汇；A0/A1 `unverified`、#110 全窗口首杀 `unverified`、#100 路线均标为假设；本 PR 未改任何历史报告 | — |
| C3 | 迁移本体与各验收 issue 反向引用本 issue；每项代码整改只有一个主负责 issue | ✅ #102/#103/#104/#105 正文均含 `migration-links-v1` 反向链接 #101；每个整改项在上表给出唯一 owner（#50/#97/#100/#72/#84/#99/#98 + core#3/avz#1） | #101 自身正文追加收口说明见拟议稿 |
| C4 | 代码合同变化附行为测试；仅文案同步无需为形式新增测试 | ✅ 合同/行为变化均有测试：PR #106（`test_scenarios`/`test_evaluation_lifecycle`）、PR #107（`test_issue99_shovel_fork`）、core PR #2（125 项）；本 PR 为纯文档，未新增形式测试 | — |

## 5. 未完成项与延期理由（汇总）

| 未完成项 | 责任 | 延期/在途理由 | 是否阻塞独立开发 | 是否阻塞对应验收/最终切换 |
|---|---|---|---|---|
| A0/A1 焦点/光标档真机通过 | #50 | 需要交互式桌面会话才能让 `SetForegroundWindow`/`GetCursorPos` 成功；当前自动化不在交互桌面。可重跑取 `applied=true`，或由 #50 维护者明确收窄为 `wall` 档并接受结论边界 | 否 | 否——不在 #102/#103 已声明关闭条件内；#103 只要求在模式/隔离声明中不把窗口不可见等同于窗口无关（该推断已由 #100 更正） |
| A2/A3（≥300 s 长挂起、冻结挂起） | #50 | 需要放宽 pause 上界与 `process_snapshot --hold-seconds`；未实现 | 否 | 否——未列入 #102/#103 门槛，属 #50 自身收窄项 |
| 版本化公共结果合同 | trajectory-core#3 | [PR #4](https://github.com/pvz-agent-lab/trajectory-core/pull/4) OPEN，**未合并**（2026-09-26 观察 head `c62f308e…`、持续更新；本地 190 项与 8 个 CI job 通过）；env/rollout 运行时对接尚未验证 | 否（不阻塞 core 提取与各仓独立开发） | **是**——#104 的公共 schema/结果合同待满足；#102 关闭条件要求三项迁移验收有匹配版本证据 |
| 正式生产者封口协议（无活动写入者） | trajectory-core#3 | 同 PR #4；core 只验证生产者回执，不伪造运行时事实 | 否 | **是**——#104 的正式封存生产者协议待满足，属 #102 关闭条件 |
| #100 路线矩阵与门槛替换证据 | #100 / pvz-env#1 / avz#1 | RFC 研究，未选定路线；#102 明确其不是基础迁移前置 | 否 | 否——但若选定某路线并替换窗口验收门槛，#103 要求触发对应补验 |
| AvZ 补丁盘点与 ADR | avz#1 | [avz PR #2](https://github.com/pvz-agent-lab/avz/pull/2) OPEN，按 review 修复中，**未合并**；本文件只回链，不重复盘点 | 否 | **是（最终切换条件）**——#102 要求四仓责任与版本组合明确；env 固定 AvZ 提交属 #103 验收内容 |
| #99/#105 正向首次击杀门槛 | #99 / #105 | #110 四轨迹离线无法证明全窗口绝对首杀（33 个未知直接消失）；#111 在新轨迹上已证明完整首杀，但那是 #111 自身验收，且 #102/#111 明确 core 提取不等待首杀补证 | 否（#110/#111 与 core 提取已独立推进） | **是**——#99/#105 的正向门槛待满足，不能由 #110/#111 关闭替代，也不能用窗口化/真 fork 的非前置政策豁免 |
| #98 A/B/C 性能对照与固定窗口 | #98 | 独立 PERF 研究；先盘点时间戳覆盖，不能直接测量的成本不得强行给占比 | 否 | 否——#102 明确非基础迁移前置 |
| 跨仓版本组合与旧入口退出 | #102 | #102 关闭条件之一，未完成 | 否（各仓可独立开发并逐步固定版本） | **是**——即 #102 的最终切换条件 |

## 6. 本文件不改变的历史结论

- #50 comment 1（2026-09-22）记录的"跑过之后再灌回失败（winerror 487）+ 部分恢复导致进程死亡"保持原文，仍是 R0 硬限制的来源。
- #50 comment 3（2026-09-24）的 A0/A1 `unverified` 结论保持，不被本文件提升。
- #110 正文与报告"全窗口绝对首次击杀仍未验证"保持；#111 的全部结论只绑定其自身四条 `hosted-v2` 真机轨迹与构建哈希。
- #99 开发期 `issue99-jd12r` 文档与正式 `issue99-fc2` 证据**并存但不同**：运行身份、DLL/构建哈希、树 ID、报告摘要均不同，不得互相充当复跑或封存证据（详见拟议 #99 正文与 [issue99-铲子同根分叉](issue99-铲子同根分叉.md) 顶部补记）。
- #101 原审查文本、所有 issues 原文与评论均不改写；拟议新状态附日期 2026-09-26 与出处链接。
