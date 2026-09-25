# #111 原生测量合同与僵尸生命周期捕获点表（阶段 A）

日期：2026-09-25。对应 issue [#111](https://github.com/guajun/llm-vs-zombies/issues/111)，承接
[#110](https://github.com/guajun/llm-vs-zombies/issues/110) 发现的证据缺口，并回链
[#99](https://github.com/guajun/llm-vs-zombies/issues/99)、[#105](https://github.com/guajun/llm-vs-zombies/issues/105)
与 [PR #107](https://github.com/guajun/llm-vs-zombies/pull/107)。

本 PR 是阶段 A（首个独立 PR）：提交捕获点表、事实分类、最小数据合同与失败语义、模块依赖与
schema 兼容办法。机器可读表在 [`docs/issue111-捕获点表.json`](issue111-捕获点表.json)，由
`tools/issue111_capture_points.py` 与 `tests/test_issue111_capture_points.py` 校验。

**Junior 执行关卡状态：死亡、未分类移除、槽位回收三个候选捕获点缺少可靠的地址、ABI 与调用路径
依据，等待熟悉引擎的维护者审查；审查通过前不实现未经确认的生产 hook、不跑游戏。**
本文档通过只表示合同与已有路径证据成立，不代表任何原生语义已经真机验收。

## 1. 交付边界

本 PR：

- 只读复核 #110 的报告与来源身份，不修改原报告、seal、tree 或旧结论。
- 把初始化、确认死亡、未分类移除、槽位回收四类事实分开定义，没有依据的原因显式标 `unknown`。
- 登记全部候选捕获点：事件名、入口/出口相位、原始字段、版本/目标字节、ABI、调用者/分支依据、
  已覆盖与未知路径。
- 写出最小数据合同与失败语义、模块依赖方向、schema 兼容与迁移办法。

本 PR 不做：

- 不安装任何新的生产 hook；`delivery.hooks_added=0`。
- 不启动游戏、不新增游戏实验、不打扰其他正在运行的任务；`delivery.game_runs=0`。
- 不实现 Agent、奖励、在线条件等待或在线消费者。
- 不把未确认地址作为实现前提；不猜地址、不伪造通过、不默默换实验规格。
- 不改 `logger/schemas/event.schema.json`、不改任何现有写入器的字段语义。

## 2. #110 复核（只读）

复核命令与结果（在本机旧检出根执行；`work/`、`experiments/` 属于不随 clone 提供的本机证据）：

```powershell
$env:PYTHONPATH='src'
python tools/issue99_shovel_fork.py verify --tree experiments/trees/issue99-fc2-shovel-fork --seal work/issue99-fc2-seal.json
python tools/issue110_deaths.py --output <新目录>/issue111-recheck110
```

- tree 校验：`matches=true`、4 个节点、`problems=[]`；`tree_id =
  ce770e531a225234b53ac3b810bc3209e4417b257410605f105bc38b0bc90220`。
- 旧报告摘要与 #110 声明一致：report.json `05d83bc3…`、report.md `68495027…`。
- 重新分析：`delivery_status=complete`、`first_kill_gate=unverified`；四条轨迹各 2402 个边界，
  每条 5 个确认死亡阶段候选。重跑报告（去掉 analyzer 提交/源码摘要后）与
  `work/issue110-fc2-final/report.json` 逐字段语义相等。
- #110 的工具分支 `codex/issue110-offline-deaths` 截至 2026-09-25 仍是本地分支，未合并、未推送；本 PR 不携带其提交。

**已有证据（复核后维持）：**

| 事实 | 值 |
|---|---|
| 首次观测到进入死亡阶段 | epoch 3 / tick 940 / revision 0，post_step，engine_call 940，原生时钟 55091→55092；四条轨迹一致 |
| 同边界死亡候选 | 5 个，type=16、AtWave=0、state 73→2（灰烬）、HP 270→270、disappeared 0→0；不按槽位虚造先后 |
| 同边界未知消失 | 33 个 disappeared 0→1，无死亡阶段；tick 941 槽位释放 |
| 初始化残留 | 每条 11 个初态已消失对象在 tick 1 释放，AtWave=-2，不是窗口内击杀 |
| 槽位释放总数 | 每条 44 次（11 残留 + 33 未知），不能与 5 次确认死亡相加当击杀数 |
| 不确定事件 | 66 条 = 33 次消失 + 33 次释放两种观测，不是 66 个不同实体 |

**缺口（原样保留，不伪装通过）：**

- `coverage.exact_spawn_hook=false`：两次边界采样之间创建并立即删除的对象无法排除。
- 同一边界内没有调用内顺序：5 个死亡候选与 33 个消失/释放候选谁先谁后未知。
- 33 个消失与释放的原因未知；不得以 HP<=0、消失标志、回收函数或炮击时间接近判定击杀。
- 没有伤害来源证据：不把 State=2 归因于某一发炮，也不区分植物、小推车、自爆、友军与非伤害离场。
- 首次击杀门槛仍未通过。

## 3. 事实分类

四类事实分开定义，互不冒充；捕获点表 `fact_classes` 是机器可读版本。

| 事实 | 定义 | 现状 | 需要的捕获点 |
|---|---|---|---|
| 初始化 `initialization` | ZombieInitialize 在共享 epilogue 返回调用者之前完成的对象创建；不声称调用者完成最终放置 | 已有出口探针（`zombie_initialized`） | 无（复用） |
| 确认死亡路径 `confirmed_death_stage` | 同一完整 ID 从已识别非死亡前态进入 AvZ State 1/2/3；只表示进入死亡阶段 | 只有 pre/post 边界观测 | `zombie-death-stage-enter`（未定位） |
| 未分类移除 `removal_unclassified` | 没有观测到死亡阶段的对象消失/槽位移除；中性记录，不声称对象未死亡，原因保持 unknown；只有独立路径证据确认后才允许 `non_death_removal` | 只有边界观测 | `zombie-removal-unclassified`（未定位） |
| 回收事实 `slot_recycle` | 槽位释放与空闲链更新；可与死亡或未分类移除配对，但不等于原因 | 只有边界观测 | `zombie-slot-recycled`（未定位） |

要点：`ZombieInitialize` 出口不能命名为调用者完成最终放置；没有依据的原因显式 `unknown`；
没有观测到死亡阶段不等于已确认非死亡，移除默认 `removal_unclassified`；槽位释放不是击杀计数；
初态残留不是窗口内事件。机器可读的 `fact_classes` 另含
`boundary_observation`，表示既有 pre/post 采样通道而不是生命周期事实类别。

## 4. 捕获点表

完整行见 [`docs/issue111-捕获点表.json`](issue111-捕获点表.json)。摘要：

| 捕获点 | 事件 | 相位 | 状态 | 地址/ABI 依据 |
|---|---|---|---|---|
| `zombie-initialize-exit` | `zombie_initialized` | exit（共享 epilogue 前） | established | 入口/出口 VA `0x522580`/`0x524035`（基址 `0x400000`），锁定字节 + MinHook |
| `zombie-death-stage-enter` | `zombie_death_stage_enter` | internal_write | review_required | 未定位，无地址 |
| `zombie-removal-unclassified` | `zombie_removed` | internal_write | review_required | 未定位，无地址 |
| `zombie-slot-recycled` | `zombie_slot_recycled` | internal_write | review_required | 未定位，无地址 |
| `boundary-state-sample` | `pre_step`/`post_step` 快照 + `zombie_first_boundary_observed` | boundary_sample | established | 不安装 hook；`determinism/audit.cpp` 只读采样 |

已建立捕获点（`zombie-initialize-exit`）的关键事实：

- ABI：row 在 EAX，栈上依次为返回地址、this、type、variant、parent、wave；入口与共享 epilogue
  两处精确字节校验（VA，基址 `0x400000`），`0x522580..0x524040` 内唯一正常返回 `ret 0x14`。
- hook 内预算：固定 1024 条队列、32 层嵌套，只做定长复制；无堆分配、无序列化、无文件 I/O。
- 状态保存：GPR、EFLAGS、对齐 512 字节 FXSAVE（x87、MXCSR、XMM）、DF、LastError；不调用 RNG、不改游戏字段。
- 顺序：当前记录的 `ordinal` 是探针局部序号（在捕获时分配、drain 不重置，但 Install 时重置），
  不得直接当作全进程 `capture_sequence`；受控边界标签在 hook 进入时快照，调用外为 null。
- 未知路径：调用者返回后的位置/属性调整；两次采样之间创建并销毁的对象；尚未逐类核验的真机类别；
  异常展开/longjmp 不属于受支持轨迹。

未建立捕获点（三个 `review_required` 行）只登记**需要什么证据**，不登记地址：唯一性/完整性、ABI、
调用上下文、与其它写入的相对顺序、未知签名拒绝策略。每行的 `open_questions` 是给维护者的具体疑点。

## 5. 死亡/击杀判据与非覆盖

判据（与 #110 的保守口径一致）：

- `confirmed_death_stage_enter`：同一完整 ID（槽位+代次）在有效前态（未消失、已识别非死亡状态、
  AtWave>=0）之后进入 State 1/2/3；或由未来的直接捕获点在调用内记录同一写点的前后字段。
- `slot_release`：池槽位被释放（`id_or_free_next` 高位为零或池计数下降），带前后池头证据。
- `first_kill_gate`：**不通过**。只有当以上事实在声明窗口内覆盖到“不存在更早的创建/死亡/移除”时
  才能声明全窗口首次击杀；当前 `exact_spawn_hook=false` 与调用内顺序缺失使该门槛无法满足。

明确不覆盖：

- 攻击者/投射物/伤害类别归因；State=3 只证明小推车死亡阶段，State=2 不能唯一归因于炮击。
- 累计击杀统计、通用伤害来源链。
- 33 个未知消失的死亡判定；它们保持 `unknown`，不因 cause=unknown 或分类字段改称已确认非死亡。
- 旧轨迹与旧报告的重新解释；旧结论保持不变。

## 6. 最小数据合同与失败语义

合同名 `lvz.lifecycle-event.v1`（阶段 A 只定义，不落盘）。字段、计数与失败语义的机器可读版本在
捕获点表 `data_contract`。

**6.1 `capture_sequence` 与 `seq`**

- 现有信封、`state-deltas.jsonl`、`checksums.jsonl` 的 `seq` 是**唯一写者的写盘顺序**，保持原义不重定义；
  旧轨迹继续可读。
- 新字段 `capture_sequence` 表示**捕获顺序**：在捕获点内、进入队列前分配的进程内单调 `uint64`；
  同一游戏线程上的所有捕获点共享同一顺序域；drain、边界与 epoch 都不重置。
- 需要捕获顺序的分析只读 `capture_sequence`；缺少该字段的旧轨迹标 `unavailable`，不得用 `seq` 近似，
  也不得按零处理。现有出生探针的 `ordinal` 只是单探针局部序号（Install 时重置），不得直接当作
  全进程 `capture_sequence`；阶段 B 必须由宿主提供共享分配器，出生探针接入同一顺序域，并用
  不同事件类型交错发生的夹具验证。

**6.2 事件字段（最小集）**

`schema`、`kind`、`capture_sequence`、`version{epoch,tick,revision}`、`version_phase`
（`controlled_boundary`/`initialization`/`unknown`）、`engine_call_id`（受控调用内为真实 ID，否则必须
为 null，禁止伪造）、`invocation{depth,parent_capture_sequence}`、`entity{id,slot,generation}`、
`before_after`（`before`/`after` 子对象）原始字段子集、`classification{class,cause}`（无证据时 `unknown`）、`probe`、`complete`。
每个捕获点只复制自己声明的最小字段；浮点保存原始 32 位；指针不作为跨运行身份。

**6.3 批次所有权**

- 每个进程只有一个宿主 drain；记录适配器消费不可变批次。
- 未来在线消费者不得通过第二次破坏性 drain 抢走记录；缓冲复用前完成同步消费或显式复制，
  不暴露悬空指针。

**6.4 计数与关闭**

`captured`/`delivered`/`persisted` 分别表示捕获、交付记录器、成功落盘的数量；另有
`overflow`、`wrong_thread`、`nesting_mismatch`、`incomplete_events`、`close_receipt_present`。
关闭必须给出最终健康与回执；缺回执、截断或计数失配的轨迹由离线校验拒绝。

**6.5 失败语义**

- hook 内 fault（错误线程/容量溢出/嵌套失配/不可读/ID 变化）→ health unhealthy → 记录器让本轮显式失败
  并保留现场，不产出不完整记录。
- 写盘失败与采集失败分别传播，不静默生成合格证据。
- 未知签名拒绝安装；缺少能力标 `unavailable`，而不是零事件。
- 未匹配的嵌套调用与未完成事件在关闭时按 fault 处理。

## 7. 模块依赖方向

```text
版本适配/原生探针（determinism/*，env 侧）
   │  只做有界复制；不序列化、不写盘、不调用 Agent
   ▼
最小测量公共层（批次、capture_sequence、健康状态，env 侧）
   │  单一宿主 drain，消费不可变批次
   ▼
实验记录适配器（序列化、写盘、绑定运行清单，env 侧）
   │  JSONL/封存，现有方式
   ▼
文件合同与无游戏校验（trajectory-core；不含游戏地址、死亡规则、Win32）
   ▼
实验编排与首次死亡分析（rollout/分析工具；消费文件，不反向依赖原生层）
```

方向约束：采集层不依赖 Agent、奖励函数或实验脚本；分析结论去 rollout，不能反向成为原生测量层依赖；
未来在线消费者读取相同批次、可提出停止请求，但停止由 runtime 推进控制器执行，本期只写清接入位置。

## 8. schema 兼容与迁移办法

- 采用**新增字段 + 显式 schema 字符串版本化**；不改写已有 `seq` 语义，不原地重写旧轨迹。
- 旧读者可忽略未知字段；需要 `capture_sequence` 的新读者对缺失值 fail closed（`unavailable`）。
- 新模式显式启用并绑定游戏/构建/探针/schema/配置身份；旧轨迹不追溯获得新能力，缺新能力标
  `unavailable` 而不是零事件。
- `logger/schemas/event.schema.json` 当前要求 `seq` 且 `additionalProperties=false`：新生命周期记录必须走
  新的 schema 文档，或在同一文件内先版本化再扩展。本 PR 两者都不做，留待阶段 B 实施时按此规则落地。

## 9. 需要维护者确认的问题（Junior 关卡）

以下问题未回答前，阶段 B/C 不实现对应生产 hook：

1. 死亡路径：State 1/2/3 的写入点对应哪些原始函数/RVA？是否存在唯一死亡入口，还是多个分支？
   调用是否总在受控 update 内部？
2. 未分类移除：`+0xEC` 在哪些函数写入？是否总是伴随槽位释放？与死亡后的回收是否共用实现？
   独立路径证据确认非死亡后如何分层输出 `non_death_removal`？
3. 回收路径：槽位释放的函数/分支、ABI 与唯一签名是什么？如何与死亡/未分类移除配对？
4. 受控边界外的事件如何标注 `version`/`engine_call_id` 与 `capture_sequence` 顺序域而不伪造身份？
5. 同调用内多事件顺序以什么顺序域表达？嵌套调用如何配对？
6. 旧轨迹缺少 `capture_sequence` 时，哪些结论必须拒绝或标 `unavailable`？

完整的逐项清单在捕获点表 `review_checklist` 与每一行的 `open_questions`。

## 10. 后续阶段接入位置

- **阶段 B**：维护者审查通过后，复用出生探针与现有 runtime 宿主，抽出实际需要的公共部分；
  宿主提供共享顺序分配器（出生探针接入，旧 `ordinal` 保持单探针历史语义），用不同事件类型
  交错发生的夹具验证跨类型顺序；实验入口独立完成 `enable → 推进 → drain/记录 → 最后 drain → 健康检查/关闭 → 封存`。
  没有 Agent 服务、消费者或训练包仍正常工作。
- **阶段 C**：无游戏夹具覆盖同槽不同代次、同调用创建后移除、同 tick 多调用、死亡后延迟回收、
  未知移除、初态残留、嵌套调用与事件顺序；纯实验记录消费者跑全链路；输出单一离线命令。
- **阶段 D**：实施前冻结计划与排期；先做新增插桩开/关对照，再做两次独立冷启动 A/B，
  对照 #99 冻结窗口与历史 tick 1201 参照，输出新报告与封存。
- 后续在线消费者只读取相同批次并提出安全边界停止请求；不新增 until API、不自动扩展 `observe()`。

## 11. 校验与测试

```powershell
$env:PYTHONPATH='src'
python tools/issue111_capture_points.py --check
python -m unittest tests.test_issue111_capture_points tests.test_issue99_shovel_fork tests.test_audit_compare -q
```

校验器做两类检查：

1. 表内合同：schema/必填键、status 词表、`established` 行必须有存在证据与已覆盖路径、
   `review_required` 行必须无地址（含整数与数组形式）、必须带开放问题、四类事实齐全、
   `capture_sequence` 与 `seq` 分离且出生探针 `ordinal` 不得冒充共享序号、
   `delivery.hooks_added=0` 且 `delivery.game_runs=0`。
2. 锚点复核：`determinism/spawn_hook.cpp` 的 `kEntry/kExit` 与入口/出口字节、
   `docs/determinism-spawn-hook.md` 的 RVA、`determinism/audit.cpp` 的 `exact_spawn_hook=false`、
   `logger/schemas/event.schema.json` 仍要求 `seq`。任一锚点变化都会让校验失败，提醒复核合同。

行为夹具覆盖：合法表通过；`established` 缺证据、`review_required` 带字符串/整数/数组地址、
`seq` 或出生探针 `ordinal` 冒充 `capture_sequence`、重复 id、事实类别缺失、
hooks_added/game_runs 非零、CLI 失败退出等负例。这些都是无游戏、无 AvZ 的离线性测试，
不替代真机验收。
