# #111 阶段 B：最小测量公共层与共享 capture_sequence（首个原生 PR）

日期：2026-09-25。承接 [issue #111](https://github.com/guajun/llm-vs-zombies/issues/111)
阶段 A（PR #112）的合同，实现**不依赖死亡/移除/回收地址证据**的公共测量层部分，不新增死亡/移除/
回收生产 hook，不启动游戏。三个 `review_required` 捕获点仍等待维护者审查。

## 1. 交付边界

本 PR 做：

- 新增 `determinism/measurement.{hpp,cpp}`：由 **runtime 宿主** 拥有的一次性测量会话
  `Open`/`Close`/`Abort`、进程内共享 `capture_sequence` 分配器与会话内共享 `invocation_id` 分配器、
  单一宿主批次台账与关闭/终止回执。
- 出生探针接入共享顺序域：`capture_sequence` 在**实际捕获点出口**分配、进入队列前落定；调用身份
  `invocation_id`/`parent_invocation_id` 在入口分配（会话内唯一，重装探针不重置），每条事件都带
  `invocation_id` 与 `depth`，仅顶层 `parent_invocation_id` 为 null；旧 `ordinal` 保持单探针历史语义。
- `DrainSpawnBatch()`：单一破坏性 drain，同时产出旧 `lvz.spawn.v1` 投影与新的
  `lvz.lifecycle-event.v1` 投影。
- `determinism/audit.cpp` 最小接入：`Initialize` 开启会话、`Shutdown` 用
  `RunMeasurementShutdown` 做异常安全收尾（drain/写盘/Foley 收尾任一失败也必达 Abort + 卸钩/关文件，
  首个错误在清理后传播）、`DrainAndCheckSpawns` 单次 drain 并记 delivered/persisted。
- 离线夹具：`tests/determinism_measurement.cpp`（会话/关闭/终止/关闭序列/负例）与扩展的
  `tests/determinism_spawn_hook.cpp`（出口捕获顺序、嵌套 invocation、跨类型交错、drain 不重置、
  重装不重置、关闭后拒绝采集）。

本 PR 不做：

- 不安装死亡/移除/回收生产 hook；不跑游戏；不新增实验。
- 不改 `logger/schemas/event.schema.json`、不改 `events.jsonl` 的 `zombie_initialized` 信封与
  `lvz.spawn.v1` 载荷（逐字段不变，旧轨迹继续可读）。
- 不把 `lvz.lifecycle-event.v1` 落盘；磁盘写入、运行清单绑定与离线校验属下一 PR。
- 不实现 Agent、奖励、在线消费者、until API。

## 2. 实现对照（阶段 B 清单）

| 清单项 | 本 PR 状态 |
|---|---|
| 复用出生探针与 runtime 宿主，抽公共部分 | 公共顺序域/会话台账抽到 `lvz::measurement`，出生探针绑定；无泛型 hook 生成器/动态注册/脚本 |
| 公共上下文 | `lvz.lifecycle-event.v1` 投影含 `entity{id,slot,generation}`、`version`、`version_phase`、`engine_call_id`、`probe`、`invocation`、`classification`、`complete` |
| 同一顺序域、嵌套 invocation/parent | `capture_sequence` 出口分配；`invocation{invocation_id,depth,parent_invocation_id}` 每条事件都输出、入口分配、会话内唯一，与事件序号分离 |
| hook 内只做有界复制 | 复用既有出生探针约束；新字段只做定长复制，无堆分配/序列化/文件 I/O/回调 |
| 单一宿主 drain 与批次所有权 | `DrainSpawnBatch()` 单次破坏性 drain；`MeasurementHost` 记录 delivered |
| 计数与关闭回执 | 8 个合同计数器全部实现；`Close()` 成功才置 `close_receipt_present`，失败用 `Abort()` 终止并保留证据；关闭后拒绝分配/采集/台账修改 |
| 安装验证/回滚/所有权/卸载 | 复用既有 `Install`/`Remove`；探针安装只 `BoundTo` 绑定已开启会话，不 reset 共享状态 |
| 新模式显式启用 + 旧轨迹可读 | `lvz.spawn.v1` 保持兼容；缺 `capture_sequence` 的旧轨迹由未来读取器标 unavailable |
| 实验入口独立闭环 | 会话 Open/Close 已由 `audit.cpp` 宿主接入；lifecycle 落盘与读取器属下一 PR |

## 3. 语义要点

- `capture_sequence` 在**出口**分配：嵌套初始化实际先捕获子出口、再捕获父出口，因此子序号 < 父序号，
  排序与事实发生顺序一致；将来初始化期间的伤害/移除事件也按真实出口顺序排入。
- `invocation_id`/`parent_invocation_id` 在**入口**分配（父先于子）且会话内唯一（重装探针不重置），
  只用于配对嵌套调用；每条事件都输出 `invocation_id`/`depth`，仅顶层 `parent_invocation_id=null`。
- 无受控边界的初始化事件：`version=null`、`version_phase=initialization`、`engine_call_id=null`。
- `classification.cause` 恒为 `unknown`；未观测到死亡阶段不等于已确认非死亡。
- `persisted` 只由记录适配器在写盘成功后递增；探针与 drain 只负责 `captured`/`delivered`。
- 会话生命周期（两阶段提交）：`Open` 拒绝活跃（open/closing）会话重复开启并递增 session 身份；
  `Close` 验证无 fault 且 `captured==delivered==persisted` 后进入 closing（尚未产生回执）；
  `Commit` 在清理/持久化成功后把 closing 提交为 closed 并产生成功回执；`Abort` 把 open/closing
  终止为 failed（不产生回执、保留故障与未交付计数）。`audit::Shutdown` 经
  `RunMeasurementShutdown(body, cleanup)` 保证：body 内 drain/写盘/Foley 收尾任一异常都保存为首个
  错误，会话仍被终止（清理失败会降级为 failed，不会留下成功回执），cleanup 必达（Foley 卸钩/关流、
  粒子/出生卸钩、flush、关文件、initialized=false），最后传播首个错误；有在途调用或 hook 所有权
  问题时保留 DLL 并明确报告。

## 4. 校验

```powershell
$env:PYTHONPATH='src'
& ./tools/build-avz.ps1 -Jobs 6
ctest --test-dir build/cmake -R 'determinism_measurement|determinism_spawn_hook' --output-on-failure
python tools/issue111_capture_points.py --check
python -m unittest tests.test_issue111_capture_points tests.test_audit_compare -q
```

本机结果：

- 全量原生构建通过（含 `recorder.dll`）。
- `determinism_measurement`、`determinism_spawn_hook` 通过（无游戏、无 AvZ）。
- 阶段 A 合同校验 `ok=true`、`problems=[]`。
- 阶段 A/严格读取器相关 Python 测试通过。

## 5. 待维护者确认 / 下一 PR

1. 死亡/移除/回收三个捕获点的函数、VA/RVA、ABI 与唯一签名仍待审查（阶段 A `review_checklist`）。
2. 下一 PR 建议：`audit.cpp` 接入 `DrainSpawnBatch().lifecycle` 按 `lvz.lifecycle-event.v1` 落盘并绑定
   运行清单，配套离线读取器对缺 `capture_sequence` 的旧轨迹标 `unavailable`；再补实验入口
   enable→推进→drain→最后 drain→健康/关闭→封存。
