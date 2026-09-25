# #111 生命周期记录、终结协议与离线校验（阶段 C 前半）

日期：2026-09-25。承接 [issue #111](https://github.com/guajun/llm-vs-zombies/issues/111)
阶段 A（PR #112）合同与阶段 B（PR #113）公共测量层。本 PR 把
`DrainSpawnBatch().lifecycle` 的不可变批次真正落盘，并给出正式 schema、终结回执与无游戏离线校验；
不新增死亡/移除/回收生产 hook，不启动游戏，不实现 Agent 或在线消费者。

## 1. 交付边界

本 PR 做：

- **显式启用**：`LVZ_LIFECYCLE_RECORDING=1` 才开启生命周期记录；`=0` 在 manifest 显式声明
  disabled；未设置则完全不产生 capability 与文件，旧运行逐字节兼容。
- **原生落盘**：审计宿主的**唯一**破坏性 drain（`DrainSpawnBatch()`）同时产出 legacy 与
  lifecycle 投影；lifecycle 投影由 `determinism/lifecycle_record.{hpp,cpp}` 写入
  `audit/lifecycle-events.jsonl`。没有第二个消费者，也没有第二次 drain。
- **身份绑定**：每条记录带 `run_id`、`branch_id`、`session_id`、`sequence_domain`；审计 manifest
  的能力声明带 session、probe、schema、构建模块及其 SHA-256；运行 manifest 的
  `implementation.recorder_sha256` 与实际加载模块不符时初始化失败。
- **真实终结协议**：先 flush、close 并检查 events 流，之后才把 close receipt 写成临时文件并
  原子改名。receipt 绑定记录数、字节数、events 的 SHA-256、audit manifest 的 SHA-256、测量计数
  与探针健康。崩溃、写盘失败或 close 失败都不会留下 receipt；离线校验对缺 receipt 的启用轨迹
  fail closed。内存中的 `closed`/`Commit` 不是任何结论依据。
- **离线校验**：`tools/issue111_lifecycle_check.py` + `src/llm_vs_zombies/lifecycle_events.py`
  在不加载游戏、不引入 Win32 依赖的前提下校验重复/乱序、坏父引用、缺回执、截断、计数失配、
  错误 session/身份、探针故障；合法序号间隙只计数不拒绝。
- **无游戏测试**：`tests/determinism_lifecycle.cpp` 用真实 writer 覆盖写失败、close 失败、
  receipt 改名失败、打开失败、句柄释放，以及 `RunMeasurementShutdown` 的“receipt 后才 Commit /
  失败降级 Abort”语义；`tests/test_lifecycle_events.py` 覆盖读取器合同与旧轨迹 unavailable。

本 PR 不做：

- 不安装死亡、未分类移除、槽位回收 hook；三个 `review_required` 捕获点仍等待维护者审查。
- 不实现 Agent、奖励、在线条件等待、until API 或第二种生产事件。
- 不修改 `logger/schemas/event.schema.json`、`audit/events.jsonl` 的旧信封与 `lvz.spawn.v1` 字段语义。
- 不运行游戏、不新增实验、不触碰旧四轨迹、报告或 seal。
- 不把 `branch_id` 放进既有原生审计 manifest/状态摘要/draw/engine_call 证据（沿用
  launcher.md R7）：运行/分支身份只出现在新的可选 lifecycle 流与回执中。

## 2. 显式启用与能力声明

进程环境变量 `LVZ_LIFECYCLE_RECORDING`：

| 值 | 行为 |
|---|---|
| `1` | 打开 lifecycle-events/close-receipt，审计 manifest 写入 enabled capability |
| `0` | 不开文件，manifest 写入 `{"mode":"lvz.lifecycle-recording.v1","enabled":false}` |
| 未设置 | 不写 capability、不写文件；manifest 与旧版逐字节一致 |
| 其他 | 初始化失败（拼写错误不得退化为“没有记录”） |

`audit/manifest.json` 中 enabled capability 形如：

```json
"lifecycle_recording": {
  "mode": "lvz.lifecycle-recording.v1",
  "enabled": true,
  "event_schema": "lvz.lifecycle-event.v1",
  "envelope_schema": "lvz.lifecycle-record.v1",
  "receipt_schema": "lvz.lifecycle-close-receipt.v1",
  "sequence_domain": "lvz.measurement.capture-sequence",
  "session_id": 1,
  "probe": {"name": "zombie-initialize-exit", "schema": "lvz.spawn.v1", "event_kind": "zombie_initialized"},
  "build": {"module": "recorder.dll", "sha256": "…64 hex…"},
  "files": {"events": "lifecycle-events.jsonl", "close_receipt": "lifecycle-close-receipt.jsonl"},
  "live_validated": false
}
```

实验入口（`tools/launch-experiment.ps1` / launcher 链）通过在启动前设置环境变量启用，原生启动器
按现有隔离通道继承该变量；本 PR 不新增编排参数，避免把测量层耦合进实验脚本。

## 3. 文件合同

正式 schema：[`logger/schemas/lifecycle-record.schema.json`](../logger/schemas/lifecycle-record.schema.json)
与 [`logger/schemas/lifecycle-close-receipt.schema.json`](../logger/schemas/lifecycle-close-receipt.schema.json)。

`audit/lifecycle-events.jsonl` 每行一个 envelope：

```json
{"schema":"lvz.lifecycle-record.v1","file_seq":0,"run_id":"…","branch_id":"…","session_id":1,
 "sequence_domain":"lvz.measurement.capture-sequence","event":{"schema":"lvz.lifecycle-event.v1",
 "kind":"zombie_initialized","capture_sequence":1,"version":null,"version_phase":"initialization",
 "engine_call_id":null,"invocation":{"invocation_id":1,"depth":0,"parent_invocation_id":null},
 "entity":{"id":65537,"slot":1,"generation":1},"before_after":{"before":null,"after":{…}},
 "classification":{"class":"initialization","cause":"unknown"},
 "probe":{"name":"zombie-initialize-exit","schema":"lvz.spawn.v1",
 "sequence_domain":"lvz.measurement.capture-sequence"},"complete":true}}
```

唯一性规则：

- `capture_sequence` 在 `(run_id, branch_id, session_id, sequence_domain)` 内唯一且严格递增；
  **允许间隙**（嵌套初始化先子后父、未来捕获点共享同一顺序域），不要求等差连续。
- `file_seq` 是写盘顺序 0..n-1，只用于发现截断/重排，不冒充捕获顺序。
- `invocation_id` 在会话内定义调用身份；`parent_invocation_id` 允许指向**之后**才写出的父事件
  （父在入口获得 id，子在出口先落盘），读取器按整个会话解析，不按相邻行解析。
- 不同 session 的裸序号不得直接合并；读取器按上述四元组作用域比较序号，并用 receipt 的 session
  与全部记录逐条比对。
- 旧轨迹缺少 `sequence_domain`/`capture_sequence` 时标 `unavailable`，不是零事件，也不得用
  旧 `seq`/`ordinal` 近似。

## 4. 终结协议（close receipt）

`LifecycleRecorder::Finish(complete, counters, probe_health)`：

1. `close()` events 流（含 flush），检查 `fail()`；失败即抛错，不写 receipt。
2. 只有 `complete` 且 measurement `Close()` 已把会话置为 closing、且本次 cleanup 之前无错误时，
   才进入 receipt 写入。
3. receipt JSON 以 `lifecycle-close-receipt.jsonl.partial` 打开、写入、flush、close、检查，再用
   `MoveFileExW(.., MOVEFILE_REPLACE_EXISTING|MOVEFILE_WRITE_THROUGH)` 原子改名。任何一步失败都
   删除 partial 并抛错。
4. `audit::Shutdown` 的 cleanup 在 lifecycle 收尾**之前**完成 foleytrace/粒子/出生 hook 卸载、
   `Flush()`、`spawn_hook_closed`、其余文件 close 检查；lifecycle 收尾是 cleanup 的最后一个可失败
   步骤。cleanup 全部成功返回后 `RunMeasurementShutdown` 才 `Commit()` 内存会话。

因此磁盘上的成功证据是“events 字节 + receipt 摘要”而不是任何内存标志：`spawn_hook_closed` 的
measurement health 即使在 closing 状态下也如实记录，判断成功与否只看 receipt 是否成立。离线
校验器与 `AuditLog(require_closed=True)`、`AuditTail.verify_closed()` 通过**同一个**完整文件合同校验
（事件最小契约、精确键集与整数类型、LIFO 调用嵌套、序号规则、receipt 摘要/manifest/身份/计数/
persistence 断言、审计流探针故障），任何不合法都返回 `EvidenceError`；两者都支持 seal 后的 gzip
容器。`AuditLog(require_closed=False)` 与进行中的 `AuditTail` 允许 receipt 尚未出现的 `open`
状态，但已出现记录仍需满足同一合同；seal/轨迹打包会连同 receipt 一起复制。

receipt 关键字段：`records`、`bytes`、`sha256`（events 原文）、`manifest_sha256`
（`audit/manifest.json` 原文）、`run_id`/`branch_id`/`session_id`/`sequence_domain`、
`build{module,sha256}`、`counters`（captured/delivered/persisted 必须相等且等于 records，四类 fault
必须为 0）、`probe_health`（queued/active_initializers/faults/overflow/wrong_thread_calls 必须为 0，
healthy=true，captured=records）、`completed=true`。

## 5. 离线校验命令

校验（完整性/回执）：

```powershell
$env:PYTHONPATH='src'
python tools/issue111_lifecycle_check.py <run-or-audit-dir>
python tools/issue111_lifecycle_check.py <run-or-audit-dir> --recorder build/recorder.dll --json
```

边界观测报告（首次死亡阶段/首次移除/未知移除/首次击杀可证性）：

```powershell
python tools/issue111_lifecycle_report.py <run-or-audit-dir> --out work/report.json --markdown work/report.md
```

报告把派生事实标注为 `boundary_observation`；三个捕获点未安装时 `capture_level` 恒为
`unavailable`、`first_kill.proven` 恒为 false。实验入口与封存：

```powershell
python tools/issue111_lifecycle_experiment.py prepare --name <new-run> --plan experiments/plans/<plan>.json --mode on
python tools/issue111_lifecycle_experiment.py seal --run experiments/runs/<new-run>
```

```powershell
$env:PYTHONPATH='src'
python tools/issue111_lifecycle_check.py <run-or-audit-dir>
python tools/issue111_lifecycle_check.py <run-or-audit-dir> --recorder build/recorder.dll --json
```

- 接受实验运行目录（自动取 `<run>/audit`）或 audit 目录本身；使用现有 `EvidenceStore`，兼容
  seal 后的 gzip 容器，不引入 Win32 依赖。
- 退出码：`0` = valid/unavailable/disabled/open；`1` = failed；`2` = 合同不可读（含声明为 null/
  非对象的 capability）。
- 报告包含 `status`、`records{count,kinds,identities,first/last,gaps,rule}`、`close_receipt`、
  `probe_events{present,scanned,fault_events,closed_unhealthy,problems}`、`problems` 与 `claims`。
- 语义检查：事件/信封/receipt 的精确键集（禁止缺失、多余键），`kind=zombie_initialized`，
  `before_after.before=null` 且 `after` 为原生快照字段子集并与 `entity` 身份一致，
  `classification{class=initialization,cause=unknown}`，初始化事件的 `version`/`engine_call_id`
  显式 null，受控边界事件为版本对象；bool/float/字符串不得冒充整数。
- 嵌套检查：`invocation_id` 必须为连续会话计数且不重复；按出口顺序重建 LIFO 栈，逐条验证
  declared depth、parent 及“父先于子退出”不可能性（合法的先子后父退出与序号间隙保留）。
- 进行中的录制（无 receipt 且 `require_close=False`）：允许前缀语义——未完成祖先与尚未写出的
  invocation id 不报错，最后一条未 flush 的记录与 `audit/events.jsonl` 的未完成行也容忍；但重复
  ID、深度/父引用矛盾、乱序退出仍然拒绝。receipt 一旦存在（即使调用方传
  `require_close=False`）或按关闭读取时，仍要求连续 invocation、LIFO 收口与完整行。
- 存储/解码失败（manifest 非 UTF-8、gzip 截断/损坏、codec receipt 非法）在公共边界转换为
  `LifecycleError`，CLI `--json` 退 2 并输出合法 JSON，严格读取器转 `EvidenceError`；run/launcher
  身份文件“存在但损坏”报绑定失败，不再静默忽略。
- 探针故障：`audit/events.jsonl` 出现 `spawn_hook_fault` 或 `spawn_hook_closed.healthy!=true`，
  或该文件不可读/不完整，均 disqualify success；不会静默忽略。
- `claims.first_kill_proven` 恒为 false，`first_kill_gate=unverified`，并列出尚未实现的
  `confirmed_death_stage`/`removal_unclassified`/`slot_recycle` 三类事实——本 PR 只提供初始化事实，
  不声称任何全窗口首次击杀结论。
- 当 run 目录存在 `manifest.json`/`launcher.json` 时，读取器会额外核对 run_id、
  `implementation.recorder_sha256` 与 branch_id；缺失时仅按签名与内部一致性校验。

旧轨迹判定：manifest 无 `lifecycle_recording` → `unavailable`（不是零事件）。若旧 manifest 旁却
存在 lifecycle 文件，或其 capability 为 disabled 却存在文件，读取器与严格审计读取器都拒绝，
避免“事后追溯获得新能力”。

## 6. 测试与验证

```powershell
$env:PYTHONPATH='src'
& ./tools/build-avz.ps1 -Jobs 6
ctest --test-dir build/cmake --output-on-failure
python tools/issue111_capture_points.py --check
python -m unittest tests.test_lifecycle_events tests.test_issue111_capture_points tests.test_audit_compare -q
python -m unittest discover -s tests -q
```

无游戏夹具覆盖：

- 真实 writer：合法批次（含序号间隙）、空批次、写失败、close 失败、receipt 原子改名失败、
  打开失败、重复/递减序号拒绝、析构句柄释放；
- `RunMeasurementShutdown`：clean 时 receipt 先于 Commit，lifecycle 失败降级 Failed 且无 receipt，
  body 异常时不产生 receipt；
- Python 读取器：valid/unavailable/disabled/open、截断与残缺尾、重复/乱序、事件契约各字段的
  dict/list/null/bool/float 类型表（不崩溃）、LIFO 嵌套的重复 ID/深度失配/父先退/交叉嵌套、
  receipt 计数/摘要/manifest/身份/计数/健康/persistence 失配、run/branch/recorder 绑定、探针故障与
  审计流不可读、gzip seal 往返、CLI 退出码 0/1/2；
- live 前缀：子先退出的未完成前缀 → 父到达 → 写回执转严格 valid；矛盾前缀仍失败；无回执时容忍
  生命周期/审计流的未完成尾行，receipt 存在时即使 `require_close=False` 也严格拒绝；
- 存储/解码：非 UTF-8 manifest、非法 codec receipt、截断/损坏 gzip、损坏的 present run/launcher
  身份文件都结构化失败（CLI 退 2 或 failed），不泄漏 `UnicodeDecodeError`/`OSError`/`zlib.error`；
- 严格读取器集成：同一条 lifecycle stream 分别用 `AuditLog(require_closed=True)`、
  `AuditTail.verify_closed()` 验证合法（含压缩）、语义非法事件、摘要损坏、腐败 receipt 与探针故障
  都必须报 `EvidenceError`，而进行中的录制在 receipt 出现前保持可读。

## 7. 仍待完成

- 死亡、未分类移除、槽位回收三个捕获点的地址/ABI/唯一签名审查与生产 hook（阶段 A
  `review_checklist`）。
- 真机短程验收（阶段 D）：插桩开/关对照、A/B 冷启动、#99 冻结窗口对照与首次击杀门槛。
- 实验编排读写 `LVZ_LIFECYCLE_RECORDING` 的便捷入口（可选，不阻塞本 PR）。
- 未做 fsync/掉电语义声明：receipt 证明写入者的 flush+close 成功与内容绑定，与现有 JSONL 证据
  同一持久化级别。

## v2 生产探针记录（`lvz.lifecycle-event.v2`）

当 manifest 含有启用的 `lifecycle_probes` capability 时，同一个
`lifecycle-events.jsonl` 还包含 store 粒度的探针事实（envelope 仍为
`lvz.lifecycle-record.v1`）：

- `zombie_phase_transition`（5 个 phase store；记录 site、原始 before/after）；
- `zombie_removal_marked`（mDead store；removal 不是 kill）；
- `zombie_slot_recycle_candidate` / `zombie_slot_recycle_commit`（guard 与 free 提交配对，
  commit 记录 `candidate_capture_sequence` 与 free head/count）。

校验规则（`lifecycle_events.validate`）：v2 记录必须伴随
`lifecycle_probes.enabled=true`、`record_schema=lvz.lifecycle-event.v2`、probe set、session 与
build 身份；重复的 phase store 原样保留（不做 dedup）；candidate 必须有 commit、commit 必须
引用已知 candidate，否则文件判 failed。旧 v1 轨迹与 unavailable 判定保持兼容。

## v2 关闭回执与最终健康（评审修正）

探针开启时回执 schema 升级为 `lvz.lifecycle-close-receipt.v2`：`counters` 按来源分为
`initialization` / `probes` / `total`，其中 `probes.persisted` 与 v2 记录数严格相等；
`probe_health` 绑定卸载后的最终状态（`installed=false`、`pending_candidate=false`、
`healthy=true`、`queued=0`，所有故障计数为 0），并与 manifest 的最终
`lifecycle_probes.probe_counters`、`sites` 以及 audit `lifecycle_probes_closed` 事件逐项一致。
v2 receipt 的 `event_schemas` 同时声明 v1 与 v2；缺关闭事件、payload 不一致或任一故障计数
非零都判 failed。

## 计数器语义政策（评审修正）

- **良性**：`live_skips` 只是回收 guard 的存活分支，不计入失败。
- **丢失观测（使完整性/首杀证明失败）**：`read_failed`、`classify_refused`、`inactive_suppressed`、
  `unmatched_commits`、`pair_mismatch`、`overwritten_pending`、`faults`、`overflow`、`wrong_thread`。
  它们必须为 0 才会写入 `healthy=true` 的关闭回执；`analyze_capture_facts` 同样拒绝证明。
- **不丢失的筛选**：离板预览对象（`on_board=false`）照常发布事实，不是读/分类失败。
- `reader_protected=false` 且 `pending_callbacks=0` 表示 VEH 读取保护句柄已随补丁卸载而释放。

## 形式 schema（评审修正）

`logger/schemas/lifecycle-record.schema.json` 的信封 `event` 现在是 v1/v2 的 `oneOf`；
`lifecycle-close-receipt.schema.json` 顶层是 `receipt_v1`/`receipt_v2` 的 `oneOf`。
`tests/test_lifecycle_schemas.py` 用仓库内置的最小 draft-2020-12 子集校验器对原生混合
v1/v2 产物逐条验证信封与回执，不依赖第三方 `jsonschema`。
