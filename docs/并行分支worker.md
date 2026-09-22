# 并行分支 worker

日期：2026-09-22。对应 issue [#38](https://github.com/guajun/llm-vs-zombies/issues/38)（`[N10]`），Epic [#39](https://github.com/guajun/llm-vs-zombies/issues/39)。实现：`src/llm_vs_zombies/branch_workers.py`；命令：`tools/branch_workers.py`；测试：`tests/test_branch_workers.py`。

范围声明：本文交付**训练数据管线**的并行分支 worker（同源分叉的执行侧），只使用合成夹具验证调度、隔离、失败处理与 P1 逐帧比较；**不启动游戏、不跑长局、不动 `experiments/` 与 `game/`**。计分管线的冷重放门槛（#19 / `cold_workers.py` / `evaluation.py`）一条都没有被复用、放宽或改写，见 §1 与 §6。本文不提升任何既有实验结论，也不把"未观察到"写成"已证伪"。

---

## 0. 摘要

- 一条命令按**一棵树的一个根**并行跑多个分支：每个分支一个 worker 进程、一份用户档、一份日志、一份产物与证据目录；分支之间不共享运行目录。
- 与 #19 相反：**分支失败不停止派发**。失败分支就地封存（保留 `task.json`、`stderr.log`、执行器原始证据、`receipt.json` 或父侧 `failure.json`），其余分支继续跑完。
- P1（同源分叉决定论）只在计划**显式声明可比**的分支之间检查，且要求同根 + 同动作序列；动作摘要保留 `method`/`params`/`expect`，剔除每次运行都不同的 `request_id`。
- 隔离是"进程级 + 目录级 + 设备租约"三段：进程与 Job 私有一份；用户档/日志/产物/证据按分支落目录；音频设备用跨进程互斥租约串行化并留下可复核的租约日志。**没有虚拟化设备**，因此 C32 仍是"待定"（§4）。
- 交叉写入会被发现：worker 在封存前记录自己目录的证据摘要，父进程在 worker 退出后和整轮结束时各复核一次；任何第三方写入都会使该分支的证据作废，并写进报告的 `isolation.interference`。
- 报告显式声明 `pipeline.scoring_cold_replay = "not_applicable"`；计划里出现计分管线的键（`cold_starts`/`cold_workers`/`strict`…）会被直接拒绝。

## 1. 与 #19（并行冷重放）的边界

| 维度 | #19 并行冷重放（计分管线） | #38 并行分支 worker（训练管线） |
|---|---|---|
| 目的 | 用两个独立冷重放验证**同一条源轨迹**的可重复性 | 同根**分叉**跑多个不同分支，产出训练树节点 |
| 输入 | 已封存源档案 + 官方 replay 请求流 | 一棵封包树（N3）或显式内联动作序列 |
| 并发单位 | 同一种子的 cold attempt（`repeat-<n>`） | 分支（`branch_id`），各自一个 worker 进程 |
| 门槛 | 每分支逐条核验 `LIVE_GATES`：私有启动、窗口、归档完整性、引擎重放、关闭 health… | **不适用任何冷重放门槛**；只核验证据身份、帧契约、目录不可变与可比声明 |
| 失败策略 | 首次观测到 cold 失败即**停止派发**尚未开始的任务（计分需要全部有效） | **失败隔离**：失败分支就地封存，同批与后续分支继续运行 |
| 重试/重算 | 受预算与源轨迹约束，不重算策略 | 不重试；失败档原样保留，训练侧决定是否重建分支 |
| 根/动作要求 | 同源单主干，禁止分叉 | 同根 + 同动作序列才可比；不同根不比较（P3） |
| 设备 | 未单独讨论共享音频设备（C32 待定） | `exclusive_lease` 串行化设备窗口并留日志，或 `shared_declared` 显式接受共享 |
| 代码 | `src/llm_vs_zombies/cold_workers.py`、`evaluation.py` | `src/llm_vs_zombies/branch_workers.py` |

两条管线共用控制面风格（PID/创建时间绑定、自有 Job、只读回执、证据不可覆盖），但**门槛定义与失败策略分开写**；`branch_workers` 不导入 `evaluation`/`cold_workers`（`tests/test_branch_workers.py` 用子进程检查这一点）。

## 2. 计划格式

`schema: lvz.branch-workers.v1`（`Plan.from_dict`，`Plan.load` 读文件）。字段全部显式，未知键一律拒绝。

| 键 | 含义 |
|---|---|
| `executor` | 可选；`module:function` 或 `path/to/executor.py:function`。命令行 `--executor` 可覆盖。文件路径按**调度器的工作目录**解析（不是按分支目录） |
| `max_workers` | 1–4，且不超过分支数；缺省 `min(2, 分支数)` |
| `timeout_seconds` | 每个分支墙钟上限（含 arm 与执行），默认 900，上限 86400 |
| `tree` | 可选；封包树目录（N3 产物）。给了 `tree` 时根身份必须取自树上（整树重算 `validate_tree`），`root_identity` 若同时给出必须逐字段相等；工作区只引用该目录，不复制树 |
| `root_identity` | 无 `tree` 时必须给出：`game` / `artifacts` / `init_recipe` / 可选 `image_sha256`（`evidence_tree.normalize_identity` 口径） |
| `branches[]` | `branch_id`（目录名，`[A-Za-z0-9][A-Za-z0-9._-]{0,63}`）、`node`（树节点键，`tree` 模式）或 `steps`（内联动作序列）、可选 `env` |
| `comparisons[]` | `{name, branches}`；成员必须在同一根下**动作序列摘要相同**，否则计划被拒（"只能比较同根同动作"） |
| `device` | `policy`：`exclusive_lease` 或 `shared_declared`；`shared_declared` 必须同时给 `accept_shared: true` 与 `reason`；可选 `lease_timeout_seconds` |
| `env` | 计划级环境变量（字符串），同名分支级 `env` 覆盖；`LVZ_*` 是保留前缀，计划不得占用；`PYTHONPATH` 追加在 `src` 之后 |

计划身份 `plan_id` 由根身份、树 id、分支（含动作摘要）与设备策略规范化后的 SHA-256 派生；写进工作区的 `plan.json` 内的 `plan_id` 会被重新校验，不一致即拒绝。

动作摘要（`steps_digest` / `action_sequence`）：每个请求保留 `method`、`params` 与预状态 `expect`，**剔除 `request_id`**。否则同一动作的两份录制永远不相等（每次录制的 request id 都不同）——这一点在树模式下实测过（两个同动作分支的完整 `steps` 记录不同、动作摘要相同）。

## 3. 隔离方案

| 资源 | 隔离方式 | 证据 |
|---|---|---|
| 进程 | 每分支一个 worker 进程（`python -m llm_vs_zombies.branch_workers --worker <task.json>`）；Windows 下每分支一个自有匿名 Job（`KILL_ON_JOB_CLOSE`），POSIX 下 `start_new_session` | 回执的 `owner`（PID + 创建时间）、`arm.job_assigned`、报告 `host_process` |
| 进程绑定 | 父进程先 assign Job、后写 `armed.json`；worker 在收到 arm 回执前**不加载执行器、不执行任何分支工作** | `wait_for_arm`、`tests/test_branch_workers.py` 的"未 arm 不执行"用例 |
| 用户档 | 每分支 `<branch>/profile` 作为 appdata 沙箱（经 `LVZ_BRANCH_PROFILE` 交给执行器，由执行器传给启动器的 appdata 参数），PopCap 注册表键仍由启动器按目标进程播种 | 回执 `environment`；`docs/launcher.md` §3/§5 |
| 运行目录 | worker 以**自己的分支目录**为 cwd，相对路径写入落在自己目录内 | 回执 `environment.LVZ_BRANCH_DIR` 与回执自身路径一致 |
| 日志 | `<branch>/logs`、`<branch>/stdout.log`、`<branch>/stderr.log` 每分支独立 | 报告 `branches[].host_process`、`manifest` |
| 产物与证据 | `<branch>/artifacts`、`<branch>/evidence`；帧证据 `<branch>/frames.json` | 报告 `frames`（路径 + sha256 + tick 范围） |
| 设备 | `exclusive_lease`：命名互斥体（Windows）/ 锁文件（POSIX）+ 追加式租约日志 `device-lease.jsonl`；`shared_declared`：显式接受共享 | 报告 `isolation.device.lease`（配对、区间、重叠、未闭合、父侧封口） |
| 证据不可覆盖 | 工作区必须不存在；分支目录不存在；从不改写、从不删除既有文件 | 失败即拒绝运行（`workspace already exists`） |

租约日志的写序即持有序：worker **先**追加 `release`（仍持锁）**再**放锁；被杀死而没有释放的窗口由父进程在**回收进程之后**追加 `abandoned`（`by: parent`），记录"释放时刻未知及原因"，绝不伪造成 worker 的 release。

## 4. 仍然共享的资源与风险

条目与 [`docs/跨界耦合清单.md`](跨界耦合清单.md) §3.5（C28–C34）逐条对应，状态词表沿用该文 §1.2（已固定 / 已证伪 / 待定）。报告把整表写进 `isolation.shared_resources`，因此每份报告都自带当时的风险声明。

| ID | 资源 | 状态 | N10 的处理与残留风险 |
|---|---|---|---|
| C28 | 单实例互斥体 | 已固定 | 启动器按目标进程命名，分支之间与用户手动开的原版都不撞车 |
| C29 | 用户档 / PopCap 注册表键 | 已固定 | 每分支独立 `profile/` 与每 run 的注册表种子 |
| C30 | 运行目录 | 已固定 | 每 worker 以自己的分支目录为 cwd |
| C31 | 进程 / Job 归属 | 已固定 | 每分支自有 Job；终止一个分支不影响兄弟分支（逐 Job 隔离，不共用 Job） |
| C32 | 物理音频设备、DirectSound 对象、BASS 音乐通道 | **待定** | **未虚拟化**（N9 未落地）：`sound_effects_allocation_none_v1` 只切 Foley 分配；`exclusive_lease` 让设备窗口串行化并留证据，`shared_declared` 是显式接受风险。设备**状态**跨分支是否残留仍未验收 |
| C33 | 磁盘 / CPU / 内存竞争 | **待定** | 不预留容量：只有每分支墙钟上限与真实失败保留；并发下的采样完整性仍按 #19/#21 的经验处理，不因 N10 通过 |
| C34 | 显示设备与窗口 | 已固定 | 启动器隐藏窗口，N10 不改显示路径，也不声称已去除绘制 |
| N10-1 | 设备租约日志（控制面） | 已固定 | 追加式写入，仅在持锁期间由 worker 写、父进程只在回收后封口；日志本身是共享文件，不承载设备语义 |
| N10-2 | worker 宿主源码（磁盘上的包） | 已固定 | 每个 worker 重新计算被派发的包摘要，源码变化即本分支失败 |

另外两条**不声称已解决**的风险：

1. **分支目录的写入互斥不由操作系统强制**。harness 检测"worker 退出后目录被改动"（`receipt.evidence` 与整轮 `manifest` 两处复核），但一个分支在**执行期间**被外部写进目录时，那些文件会被当作该分支自己的证据。真正的写入互斥需要按目录 ACL/沙箱落地，属后续工作。
2. **训练侧的并发通过不等于计分侧的门槛通过**。本工具完全不覆盖窗口覆盖度、归档封口、关闭 health 等门槛；需要这些结论时走 #19 的管线。

## 5. 生命周期与失败隔离

1. **准备**：父进程创建 `<workspace>/branches/<branch_id>/`（存在即拒绝）、`profile/` `logs/` `artifacts/` `evidence/`，写 `task.json`（含动作摘要、根身份、超时、设备与父进程身份）。
2. **派发**：Windows 下先建自有 Job，再 `CreateProcess`（`CREATE_NO_WINDOW`），assign 后写 `armed.json`；worker 校验包摘要、父进程身份与 arm 回执后才继续。
3. **执行**：worker 解析执行器 `executor(task, directory) -> {mode, real_game, frames, identity?, restore?, note?}`，校验帧契约（首帧 tick 0、tick 非递减、末帧不早于节点 `end`）、校验 `identity` 与根身份（声明 identity 时必须是同根），把帧写入 `frames.json` 并记摘要。
4. **封存**：worker 在写回执前记录 `receipt.evidence`（分支自有证据摘要）；回执包含 `status`、错误详情、arm/租约信息、观测到的 `LVZ_*` 环境与宿主身份。
5. **收集**：父进程等待真实退出、确认自有 Job 清空、读取并复核回执（身份、环境、退出码一致性、帧证据摘要与派发动作一致、证据摘要未变）。
6. **失败隔离**：执行器异常 → 回执 `status=fail` + 原始证据保留；worker 崩溃 → 父侧 `failure.json` 记录；超时 → 关闭该分支 Job（只杀本分支树）后收集并保留残档。**任何一条失败都不停止其它分支的派发**（与 #19 的"首败停派"相反）。
7. **不可变性**：所有分支退出后父进程再取一次目录摘要，与收集时的摘要比较；差异写入 `isolation.interference` 并使该分支作废（`error.stage = "immutability"`）。
8. **设备封口**：失败/被杀分支如果留下未闭合租约窗口，父进程追加 `abandoned` 记录后 `verify_lease_journal` 复核配对与非重叠。
9. **复核**：`verify` 子命令离线重算——报告 id、`plan.json` 摘要、分支目录摘要（对照 `manifests/<branch>.json`）、租约日志、以及从 `frames.json` 重跑的可比组结论，全部必须与报告一致。

## 6. 命令与报告

```powershell
$env:PYTHONPATH = 'src'
# 运行：计划 + 新工作区；工作区必须不存在
python tools/branch_workers.py run work/branch-plan.json work/branch-run-001 --max-workers 2
# 离线复核：不启动任何进程
python tools/branch_workers.py verify work/branch-run-001
```

退出码：`0` 通过，`1` 失败或被拒（计划非法、工作区已存在、回执不符等），`2` 参数错误。

产物：`<workspace>/plan.json`（解析后的计划）、`<workspace>/report.json`（`schema: lvz.branch-workers-report.v1`）、`<workspace>/branches/<branch_id>/…`（每分支证据）、`<workspace>/manifests/<branch_id>.json`（完成时/最终目录摘要与差异）、`<workspace>/device-lease.jsonl`（`exclusive_lease` 时）。

报告要点：

| 键 | 内容 |
|---|---|
| `plan_id` / `plan` | 计划身份与 `plan.json` 摘要、执行器、`max_workers`、分支顺序 |
| `pipeline` | `name: training-branch`、`fork: required`、`scoring_cold_replay: not_applicable` 与排除项 |
| `root_identity` | 本轮全部分支共享的根身份（P2 的输入，由树或计划给出） |
| `branches[]` | 分支绑定（`tree`：`tree_id`/`node`/`branch_id`/`trajectory_id`/`chain_sha256`/`fork`/`end`）、动作摘要、`status`、回执（含错误详情）、帧证据、`host_process`、目录摘要与 `mutation`、父侧失败封印 |
| `comparisons[]` | 声明可比组：逐帧比较（帧序、tick、规范化状态）与首个差异（JSON Pointer）、两侧帧证据摘要 |
| `isolation` | 布局、进程模型、设备策略与租约复核、共享资源清单、干扰检测结论 |
| `verdict` | `passed`/`failed` 与失败分支、失败组、各分支状态 |
| `limitations` | `real_game_run`、`original_engine_verified`（恒为 false）、说明、前置条件清单 |

## 7. 验证（合成夹具，离线）

```powershell
$env:PYTHONPATH = 'src'
python -m unittest discover -s tests -p 'test_branch_workers.py' -v
```

夹具执行器在**真实子进程**里驱动 `tests/test_engine_replay.py` 的合成引擎（与 N8 同一夹具），覆盖：

- 两个分支（同一动作序列，封包树里两个同动作节点）并行运行：逐帧相等、进程 PID 不同、目录/用户档/日志独立、租约日志成对且非重叠、`verify` 通过；
- 单分支失败：该分支回执 `fail` 且原始证据保留，同批与后续分支照常完成，总判定失败但 `isolation` 不变；
- 单分支挂死：超过墙钟上限后只终止该分支 Job，`failure.json` 保留，兄弟分支通过；
- 交叉写入：另一分支往已完成分支写入后被发现（`isolation.interference.mutations` 记录新增文件，被写分支作废）；
- 未 arm 的 worker 不加载执行器、不产生任何分支证据；
- 计划校验（未知键、计分键、重复标签、路径逃逸、可比组不同动作）；
- 边界检查（报告不含 `LIVE_GATES` 键、`scoring_cold_replay = not_applicable`、导入 `branch_workers` 不导入 `evaluation`）。

**这不是真实游戏证据**：所有报告均为 `real_game_run: false`、`original_engine_verified: false`。合成夹具上通过只说明 harness 的调度、隔离与失败语义成立，不说明真实游戏上并行分叉已经成立。

## 8. 未做 + 前置条件

未做（本次范围内明确不做）：

1. 未启动游戏、未注入、未跑长局；没有真实两分支并行分叉的逐帧对照。
2. 未落地 a2 快照/恢复（N5）：分支执行的"从根开始"目前由执行器自行负责。
3. 未做虚拟音频设备（N9）：设备只做租约串行，未虚拟化（C32 仍待定）。
4. 未做 R4 类扰动（挂起间隔、主机负载、并行度变化对轨迹的影响）。
5. 未做目录写入的强制互斥（仅检测），未改 `experiments/` 与 `game/`。

真实运行的前置条件：

1. `--executor` 指向的实机执行器：启动受控目标（launcher + 固定运行时构建 + 私有用户档）、把树根恢复到目标、按分支动作执行、返回逐 tick 审计状态，并在 `identity` 里给出与树根相同的 game/artifacts。
2. 封包树（N3）已通过整树重算（`evidence_tree.validate_tree`），且根一致性检查（N7）通过；否则分支不是同一根。
3. 设备策略明确：`exclusive_lease`（默认建议）或 `shared_declared` + 理由；并把 C32/C33 的残留风险写进实验记录。
4. 需要"原版逐位确定性"或计分结论时走 #19 管线；训练管线的门槛（配置级一次冷启动 + 抽样自检，N8）与它分开。

## 9. 明确不做

1. 不把冷重放门槛搬进训练管线，也不在计分管线放开分叉。
2. 不把"两个分支逐帧相等"升级为"引擎确定性已证明"：那只是同根同动作的两个样本。
3. 不覆盖或改写任何失败档：工作区存在即拒绝运行，失败档原样保留。
4. 不声称完成了设备隔离（设备仅串行化，未虚拟化）。
