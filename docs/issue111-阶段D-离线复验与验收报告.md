# issue #111 Stage-D 离线复验与验收报告

运行身份
- hosted recorder.dll sha256 `a6e7dcf06060a4d3c3a87545b5e3044f2a47786b09cc2a293a42c9deeb9fb8ad`
- 宿主脚本 `logger/avz/hosted/jing_dian_12.cpp` sha256 `ff7f049a0607c6518e0a755fe7a81c5823035e03a822d409a6a97be8647cd171`
- 冻结计划 `experiments/plans/issue99-shovel-control.json`（seed42 / jingdian12 / B0 1500·1340 / audio sound_effects_allocation_none_v1 / stop_when wave>=2 / tick cap 2000 / pause 1000）
- build commit（运行产物来源）：main b156937（**不含** PR117 的 f7f06e73 原生 close fix——该 fix 只在 PR117 分支）；tool commit（离线验证实现）：PR117 `895f170` 及其后的本轮提交。

## 四个 v2 单冷臂（同一 DLL）

原始 `evaluation.json` 逐字节保留，未改写状态：

| 臂 | 原始状态 | 终点 | 严格离线复验 | first_kill |
|---|---|---|---|---|
| off-a | completed | stop_condition_reached (wave 2) | ok（AuditLog/verify_files + 严格 lifecycle + _trace_steps/_validate_steps） | 不适用（探针关） |
| on-a | failed（旧读取器打包） | stop_condition_reached (wave 2) | ok | true |
| off-b | completed | stop_condition_reached (wave 2) | ok | 不适用 |
| on-b | failed（旧读取器打包） | stop_condition_reached (wave 2) | ok | true |

每个臂均通过：`AuditLog(require_closed=True)` + `verify_files()`、`lifecycle_events.validate(require_close=True)`（valid）、`engine_replay._trace_steps` + `_validate_steps` 原生交叉校验；复验记录与只读封存见 `experiments/runs/issue111-d-hosted-v2-*/revalidation.json` 与 `revalidation-seal.json`，来源摘要见 `experiments/reports/issue111-d-provenance-v2.json`。

## 比较（保留证据，scope-aware）

- `--scope common` off-a vs on-a：`equal=true`（audit state/render/engine-call/particle + 严格 action/request/result + initial/final outcome）。
- `--scope both` off-a vs off-b 与 on-a vs on-b：`equal=true`（含完整 v2 语义序列与健康）。
- 原始首差异是 `request.branch`（运行身份）；已做仅信封层的窄归一化（不递归归一化参数/结果中的同名字段），并有正反测试。

## 全窗口首杀（on-a；on-b 同形）

- `first_kill.proven=true`，prerequisites 全部 true（receipt / probe capability / initialization / full window / health）。
- 首个确认事实：entity `3749838848`，capture_sequence `95`，`death_path=applyburn_diewithloot_dienoloot`（首次 mDead 0→1 + locked callsite `0x532FC2/0x5302FA` + 帧链 `0x5302FF/0x532FC7`）。
- 窗口 `{"epoch":3,"tick":0,"revision":5}` → `{"epoch":3,"tick":1201,"revision":0}`，anchor index 0。
- 事实汇总：137 条（104 gameplay + 33 preview），38 条确认死亡阶段，116 条初始化事实，0 unknown predecessor/onset；validated ApplyBurn 链 33 条、foreign/未知保持 unknown。

## 旧复验失败根因与修复

旧 `AuditLog` 只支持 7 个关闭事件页脚与全量计数 receipt，拒绝 merged writer 的
`recording_closed → engine_call_closed → fp → sound → draw → particle → spawn_hook_closed → lifecycle_probes_closed`
（尾部容量 8）以及 compact `{persisted}` 计数形状。离线层现严格支持两种形状，并新增缺失/重复/乱序/提前/版本不匹配/不健康/未声明/legacy7 负例；原生 DLL、记录与旧报告均未改动。

## 可复现命令

```
python tools/issue111_lifecycle_experiment.py --root . revalidate --run issue111-d-hosted-v2-<arm>
python tools/issue111_lifecycle_experiment.py --root . seal-revalidation --run issue111-d-hosted-v2-<arm>
python tools/issue111_lifecycle_experiment.py --root . verify-revalidation --run issue111-d-hosted-v2-<arm>
python tools/issue111_lifecycle_compare.py --scope common experiments/runs/issue111-d-hosted-v2-off-a-s42-c0 experiments/runs/issue111-d-hosted-v2-on-a-s42-c0
python tools/issue111_lifecycle_compare.py --scope both experiments/runs/issue111-d-hosted-v2-on-a-s42-c0 experiments/runs/issue111-d-hosted-v2-on-b-s42-c0
python tools/issue111_lifecycle_report.py experiments/runs/issue111-d-hosted-v2-on-a-s42-c0 --plan experiments/plans/issue99-shovel-control.json --out work/report-on-a.json
```

D 与 #111 仍未关闭，等待 principal 终审。


## 严格 seal 与只读 verify（评审第 5 轮）

- `revalidation_document` 为纯计算：不写文件；`revalidate` 显式写 `revalidation.json`；`seal_revalidation`
  仅当纯计算 ok=true 且与已存记录逐字节一致时封存；`verify_revalidation` 只读，独立重算并比较
  存储记录与 seal 哈希/ID，缺失或篡改一律失败且不重建。旧的弱 seal 以
  `revalidation-weak-895f170.json` / `revalidation-seal-weak-895f170.json` 原样保留。
- 复验绑定：prepared plan/raw binding、expected build、`_child_facts`（run manifest/replay-initial/
  experiment-end/audit/lifecycle/模式）、源码 trace + `_validate_steps`、冻结窗口/终点的
  `report_for_run`（无 full_window/binding problems）、suite gate 政策（仅允许已识别的旧读取器
  packaging 失败与 smoke 未验收项，其余失败拒绝）、以及工具身份（git head + 相关源码摘要）。


## 最终提交与提交后只读验证（评审第 6 轮）

- 实现提交（validator implementation）：`6f04b8798003946a497f47a64bdd544f3f1f3582`；证据提交：`06b6a859144dfffb109c208647047bf62505250d`（PR117 head）。
- 四个臂的严格 seal 记录 implementation commit `6f04b87…` 与完整 validator 源码指纹；证据副本随仓库发布：
  `experiments/reports/issue111-d-revalidation/<arm>-{revalidation,seal}.json`，摘要 `experiments/reports/issue111-d-provenance-v2.json`。
- 最终 push 之后执行 `verify-revalidation`：off-a/on-a/off-b/on-b 全部 exit 0（只读，无写盘）。
- 更早的弱 seal 作为历史保留：`*-weak-895f170.json`、`*-weak-f910aba.json`。

### 已知剩余缺口

- 未能提交一个“合成正例完整轨迹直接经过 `_child_revalidation`”的端到端测试：构造通过 `_validate_steps` 的合成 audit/trace 需要引擎调用审计写入器，本轮预算内未完成；生产 `_child_revalidation` 已在四条真实保留轨迹上完整执行并通过，门禁/seal/verify 的合成变异矩阵已覆盖。该正例测试是唯一未完成的请求项。
