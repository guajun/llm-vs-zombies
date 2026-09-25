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
校验器与 `AuditLog(require_closed=True)` 对启用轨迹要求 receipt；`AuditTail` 对进行中的录制不要求
（events 流在初始化时已存在），seal/轨迹打包会连同 receipt 一起复制。

receipt 关键字段：`records`、`bytes`、`sha256`（events 原文）、`manifest_sha256`
（`audit/manifest.json` 原文）、`run_id`/`branch_id`/`session_id`/`sequence_domain`、
`build{module,sha256}`、`counters`（captured/delivered/persisted 必须相等且等于 records，四类 fault
必须为 0）、`probe_health`（queued/active_initializers/faults/overflow/wrong_thread_calls 必须为 0，
healthy=true，captured=records）、`completed=true`。

## 5. 离线校验命令

```powershell
$env:PYTHONPATH='src'
python tools/issue111_lifecycle_check.py <run-or-audit-dir>
python tools/issue111_lifecycle_check.py <run-or-audit-dir> --recorder build/recorder.dll --json
```

- 接受实验运行目录（自动取 `<run>/audit`）或 audit 目录本身；使用现有 `EvidenceStore`，兼容
  seal 后的 gzip 容器，不引入 Win32 依赖。
- 退出码：`0` = valid/unavailable/disabled；`1` = failed；`2` = 合同不可读。
- 报告包含 `status`、`records{count,kinds,identities,first/last,gaps,rule}`、`close_receipt`、
  `probe_events`、`problems` 与 `claims`。
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
- Python 读取器：valid/unavailable/disabled、截断、重复/乱序、坏父引用、计数/摘要/manifest 绑定
  失配、错误 session/run/branch、探针故障事件、gzip seal 往返、CLI 退出码。

## 7. 仍待完成

- 死亡、未分类移除、槽位回收三个捕获点的地址/ABI/唯一签名审查与生产 hook（阶段 A
  `review_checklist`）。
- 真机短程验收（阶段 D）：插桩开/关对照、A/B 冷启动、#99 冻结窗口对照与首次击杀门槛。
- 实验编排读写 `LVZ_LIFECYCLE_RECORDING` 的便捷入口（可选，不阻塞本 PR）。
- 未做 fsync/掉电语义声明：receipt 证明写入者的 flush+close 成功与内容绑定，与现有 JSONL 证据
  同一持久化级别。
