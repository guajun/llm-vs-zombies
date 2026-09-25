# #111 新增测量开发说明

本文说明如何在本仓库新增/复用生命周期测量能力：选择捕获点、定义载荷、接入批次、测试验证与
覆盖声明。目标是不要求未来事件重新实现缓冲、身份或封存。

## 1. 选择捕获点

- 已建立的唯一生产捕获点是 `zombie-initialize-exit`：`ZombieInitialize` 入口 `0x522580` 与其
  共享正常返回 epilogue `0x524035`（基址 `0x400000`，锁定字节见
  [`determinism-spawn-hook.md`](determinism-spawn-hook.md) 与 [`determinism/evidence.json`](../determinism/evidence.json)）。
- 为什么在共享 epilogue 出口而不是入口：出口能看到对象初始化后的原始字段（身份、出生位置、
  RNG 与时钟），且嵌套初始化按真实出口顺序排序；入口只用于分配调用身份与保存父关系。
- ABI：`row` 在 `EAX`，栈上依次为返回地址、`this`、`type`、`variant`、`parent`、`wave`；入口与
  出口都做字节校验，未知签名一律拒绝安装。
- 死亡/移除/回收的候选地址与疑点集中在 [`issue111-原生候选证据.md`](issue111-原生候选证据.md)，
  在维护者审查前只能停留在 `review_required`，不得实现生产 hook。

## 2. 定义载荷

- hook 内只做定长复制：把 `this` 指向的僵尸对象复制进栈上 `Record`，同时复制入口/出口的 MT 与
  游戏时钟；不堆分配、不序列化、不写文件、不调用 Agent、不调用 RNG、不改游戏字段。
- 投影分两个视图，来自同一批记录：
  - `legacy`：既有 `lvz.spawn.v1` 字段，逐字段不变（旧轨迹/读者兼容）；
  - `lifecycle`：`lvz.lifecycle-event.v1`，带共享 `capture_sequence`、`invocation{invocation_id,depth,parent_invocation_id}`、
    `entity{id,slot,generation}`、`version{epoch,tick,revision}`、`version_phase`、`engine_call_id`、
    `classification{class,cause=unknown}`、`probe`、`complete`。
- 字段语义与 schema：[`issue111-生命周期测量合同.md`](issue111-生命周期测量合同.md)、
  [`issue111-生命周期记录与离线校验.md`](issue111-生命周期记录与离线校验.md)、
  [`logger/schemas/lifecycle-record.schema.json`](../logger/schemas/lifecycle-record.schema.json)。

## 3. 接入批次与写盘

1. 宿主 `lvz::measurement::MeasurementHost` 拥有会话、`capture_sequence`/`invocation_id` 顺序域与
   8 项计数器；探针安装只 `BoundTo` 绑定已开启会话，不重置共享状态。
2. `DrainSpawnBatch()` 是唯一的破坏性 drain，返回不可变批次（`legacy` + `lifecycle` + `count`）。
   任何消费者都不得第二次 drain 抢走记录。
3. `determinism/audit.cpp` 是唯一记录适配器：在边界与关闭时 drain，写 `audit/events.jsonl`
   （legacy）与 `audit/lifecycle-events.jsonl`（lifecycle envelope），然后 `OnPersisted(count)`。
4. `LifecycleRecorder` 在显式 `LVZ_LIFECYCLE_RECORDING=1` 时开启；关闭时先 flush+close+校验
   events，再用临时文件 + `MoveFileExW` 原子写 `lifecycle-close-receipt.jsonl`。receipt 绑定
   run/session/branch、记录数、字节数、events SHA-256、`audit/manifest.json` SHA-256、计数与
   探针健康；没有 receipt 就没有成功。
5. 离线消费者只读文件：`src/llm_vs_zombies/lifecycle_events.py`、`tools/issue111_lifecycle_check.py`、
   `tools/issue111_lifecycle_report.py`、`examples/issue111_lifecycle_consume.py`；严格审计读取器
   （`AuditLog`/`AuditTail`）在同一校验入口拒绝不合法流。

## 4. 测试与验证

```powershell
$env:PYTHONPATH='src'
& ./tools/build-avz.ps1 -Jobs 6
ctest --test-dir build/cmake --output-on-failure
python tools/issue111_capture_points.py --check
python tools/issue111_native_candidates.py check
python tools/issue111_stage_d.py check
python -m unittest discover -s tests -q
```

原生行为夹具：`tests/determinism_spawn_hook.cpp`（签名拒绝、出口顺序、嵌套/调用身份、ABI 保存、
溢出、错误线程、部分失败、未完成调用与恢复、所有权）、`tests/determinism_measurement.cpp`
（会话/关闭/终止）、`tests/determinism_lifecycle.cpp`（写盘/关闭/receipt 故障注入与句柄释放）。
无游戏文件夹具：`tests/test_lifecycle_events.py`（合同与畸形输入）、`tests/test_lifecycle_report.py`
（同槽不同代次、边界内创建后移除、延迟回收、未知移除、初态残留、多变更边界）、
`tests/test_issue111_lifecycle_experiment.py`（实验入口/封存/消费示例）。

## 5. 覆盖声明

- 已覆盖：初始化出口事实、捕获顺序、调用嵌套、受控边界身份、健康计数、写盘/关闭回执、
  离线/严格读取校验、边界观测（死亡阶段、移除、槽位复用、未知原因）的诊断。
- 未覆盖（必须输出 unavailable/unknown）：死亡/移除/回收的生产捕获事实、调用内事件顺序、
  两次边界之间创建并销毁的对象、伤害来源归因、完整游戏状态与未拦截线程。
- 因此 `first_kill_proven` 恒为 false；#99/#105 的正向门槛仍不通过。D 阶段计划见
  [`issue111-阶段D冻结计划.md`](issue111-阶段D冻结计划.md)。

## 6. 新增未来捕获点的步骤

1. 在捕获点表登记事实类别、相位、ABI、字节与覆盖/未知路径；地址证据放入
   `docs/issue111-原生候选证据.json` 并过维护者审查后才升级状态。
2. 复用 `MeasurementHost` 的顺序域与台账：入口分配 invocation、出口分配 capture_sequence；
   只做有界复制，不新增独立缓冲区或订阅机制。
3. 在 `spawn_hook.cpp`/新探针中输出同一 `lvz.lifecycle-event.v1` 投影（新的 `kind` 用 schema
   版本化，不改旧字段）；由唯一 drain 进入同一批次。
4. 记录适配器写盘、计数与 receipt 沿用现有路径；`complete=false` 的事件不得进入合格证据。
5. 在 `tests/determinism_lifecycle.cpp` 或新的原生夹具里做行为负例；在
   `tests/test_lifecycle_events.py` 补文件合同负例；更新覆盖声明与离线报告规则。
