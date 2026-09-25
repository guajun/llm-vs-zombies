# #111 阶段 B：最小测量公共层与共享 capture_sequence（首个原生 PR）

日期：2026-09-25。承接 [issue #111](https://github.com/guajun/llm-vs-zombies/issues/111)
阶段 A（PR #112）的合同，实现**不依赖死亡/移除/回收地址证据**的公共测量层部分，不新增任何生产
hook，不启动游戏。死亡、未分类移除、槽位回收三个 `review_required` 捕获点仍等待维护者审查，
本 PR 不实现它们。

## 1. 交付边界

本 PR 做：

- 新增 `determinism/measurement.{hpp,cpp}`：宿主拥有的进程内共享 `capture_sequence` 分配器、
  单一宿主批次台账与健康/关闭回执（合同 `data_contract.counters` 的 8 个字段）。
- 出生探针接入共享顺序域：`capture_sequence` 在捕获点入口分配、出口入队前落定，drain/边界/epoch
  不重置；旧 `ordinal` 保持单探针历史语义（Install 重置），不冒充共享序号。
- `DrainSpawnBatch()`：单一破坏性 drain，同时产出旧 `lvz.spawn.v1` 投影与新的
  `lvz.lifecycle-event.v1` 投影；两次 drain 第二次为空，杜绝第二个消费者抢记录。
- 离线夹具：`tests/determinism_measurement.cpp`（纯公共层）与扩展的
  `tests/determinism_spawn_hook.cpp`（跨类型交错、嵌套 invocation/parent、drain 不重置、计数器与
  关闭回执）。

本 PR 不做：

- 不安装死亡/移除/回收生产 hook；不跑游戏；不新增实验。
- 不修改 `determinism/audit.cpp`、不修改 `logger/schemas/event.schema.json`、不改任何现有写入器的
  字段语义。`events.jsonl` 的 `zombie_initialized` 信封与 `lvz.spawn.v1` 载荷保持逐字段不变，旧轨迹
  继续可读、旧严格读者不回退。
- 不把 `lvz.lifecycle-event.v1` 落盘：磁盘写入、运行清单绑定与离线校验属下一 PR（本 PR 只交付
  合同允许的“小型内部 C++ 接口”及其夹具）。
- 不实现 Agent、奖励、在线消费者、until API。

## 2. 实现对照（阶段 B 清单）

| 清单项 | 本 PR 状态 |
|---|---|
| 复用出生探针与 runtime 宿主，抽公共部分 | 公共顺序域/台账抽到 `lvz::measurement`，出生探针接入；无泛型 hook 生成器/动态注册/脚本 |
| 公共上下文（身份/槽位代次/调用号/tick/相位） | `lvz.lifecycle-event.v1` 投影含 `entity{id,slot,generation}`、`version`、`version_phase`、`engine_call_id`、`probe`、`invocation`、`classification`、`complete` |
| 同一顺序域、嵌套保留 invocation/parent | `capture_sequence` 入口分配；嵌套调用保留 `invocation{depth,parent_capture_sequence}` |
| hook 内只做有界复制 | 复用既有出生探针约束；新字段只做定长复制，无堆分配/序列化/文件 I/O/回调 |
| 单一宿主 drain 与批次所有权 | `DrainSpawnBatch()` 单次破坏性 drain；`MeasurementHost` 记录 delivered |
| 计数与关闭回执 | 8 个合同计数器全部实现；`Close()` 置 `close_receipt_present` |
| 安装验证/回滚/所有权/卸载 | 复用既有 `Install`/`Remove`；`Open()` 只在安装成功路径调用 |
| 新模式显式启用 + 旧轨迹可读 | 本 PR 不落盘；`lvz.spawn.v1` 保持兼容，缺 `capture_sequence` 的旧轨迹由未来读取器标 unavailable |
| 实验入口独立闭环 | 属下一 PR（需落盘与 schema/读取器配套） |

## 3. 语义要点

- `capture_sequence` 在**入口**分配（`Enter` 内、入队前），因此父调用先于子调用拿到更小序号，
  `parent_capture_sequence` 可指向父调用的入口序号；fault 会留下缺口，但单调性不变。
- 无受控边界的初始化事件：`version=null`、`version_phase=initialization`、`engine_call_id=null`，
  不伪造身份。
- `classification.cause` 恒为 `unknown`；未观测到死亡阶段不等于已确认非死亡，本层不引入
  `non_death_removal`。
- `persisted` 只由记录适配器在写盘成功后递增；探针与 drain 只负责 `captured`/`delivered`。
- `wrong_thread`/`nesting_mismatch`/`incomplete_events`/`overflow` 逐项计数，健康以既有探针口径
  fail closed。

## 4. 校验

```powershell
$env:PYTHONPATH='src'
& ./tools/build-avz.ps1 -Jobs 6
ctest --test-dir build/cmake -R 'determinism_measurement|determinism_spawn_hook' --output-on-failure
python tools/issue111_capture_points.py --check
python -m unittest tests.test_issue111_capture_points tests.test_audit_compare -q
```

本机结果：

- `build-avz.ps1` 全量原生构建通过（含 recorder.dll）。
- `determinism_measurement`、`determinism_spawn_hook` 两个夹具通过（无游戏、无 AvZ）。
- 阶段 A 合同校验 `ok=true`、`problems=[]`。
- `test_issue111_capture_points` + `test_audit_compare` 共 53 个测试通过；全量 Python
  discover 717 通过（5 跳过）。

## 5. 待维护者确认 / 下一 PR

1. 确认入口分配 `capture_sequence` 的口径是否符合“捕获点内、入队前分配”与嵌套配对要求。
2. 死亡/移除/回收三个捕获点的函数、VA/RVA、ABI 与唯一签名（阶段 A `review_checklist`）仍待审查；
   审查通过前不实现生产 hook。
3. 下一 PR 建议：`audit.cpp` 接入 `DrainSpawnBatch().lifecycle`，按
   `lvz.lifecycle-event.v1` 落盘并绑定运行清单，配套离线读取器把缺 `capture_sequence` 的旧轨迹
   标 `unavailable`；随后补实验入口 enable→推进→drain→最后 drain→健康/关闭→封存。
