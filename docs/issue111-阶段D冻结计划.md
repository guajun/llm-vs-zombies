# #111 阶段 D 冻结实验计划（待维护者确认后执行）

日期：2026-09-26。机器可读计划：[`issue111-阶段D计划.json`](issue111-阶段D计划.json)。
本计划**冻结**，本轮不启动游戏、不安装未经审查的死亡/移除/回收 hook。执行前必须通过
`python tools/issue111_stage_d.py --root . doctor --require-game`，并在 PR/issue 中记录结论。

## 1. 目的与边界

在经典十二炮短窗口内验证：生产 store 探针（`LVZ_LIFECYCLE_PROBES` 打开/关闭，同构建、持久化
`LVZ_LIFECYCLE_RECORDING=1` 在两个探针臂都开启）是否
引入可观测差异、同一模式的两次冷启动是否可复现，并为 tick 940 的未知消失路径给出边界坐标与
覆盖限制。

**重要语义修正（不得含糊）：**

- `LVZ_LIFECYCLE_PROBES` 只控制 5 个 phase store、mDead store、回收 guard/commit 的生产探针
  安装；`ZombieInitialize` v1 探针、legacy `lvz.spawn.v1` 投影和持久化在两条臂**都开启**。因此本对照测
  **持久化适配器增量开销**，不能当作“新增插桩关闭/开启”验收，也不能覆盖死亡/移除/回收。
- 这不等于“无插桩构建”对照：两条臂都含 recorder 其它部件。如需真正无插桩对照，仍需另一份构建身份；
  构建期开关，这属于缺口，待维护者决定是否为本 issue 增加第二种构建，而不是用本 switch 冒充。
- 死亡/移除/回收的生产事实取决于 [`issue111-原生候选证据.md`](issue111-原生候选证据.md) 的维护者
  审查。审查未通过时，D 只使用初始化探针，tick 940 的捕获级结论输出 `unavailable`。

## 2. 窗口与停止规则（沿用 #99，不改写）

| 项 | 冻结值 |
|---|---|
| 场景 | `jingdian12` |
| seed | 42 |
| 端点定义 | `stop_when.wave_at_least=2`（第一次观测到第 2 波的战斗边界） |
| 模拟 tick 硬上限 | 2000（执行期间不得延长） |
| 历史参考 | tick 1201 时 `wave=2`；只作参考，不为复现该数字调整停止规则 |
| 暂停探针 | tick 1000 处 1 秒墙钟暂停（与 #99 相同） |
| #99 计划锚点 | `experiments/plans/issue99-shovel-control.json`；`tools/issue111_stage_d.py check` 逐字段复核 |

## 3. 运行矩阵与实际子运行身份

四个名字是**四次单冷轨迹执行**（`prepare --single-cold`），不是四套 suite。每次执行由
`evaluation.run_suite(..., single_cold=True)` 只创建 `<suite>-s42-c0` 一条源轨迹；不创建
`-s42-c1` 重放或 `-s42-recovery` 恢复子运行，因此总共恰好四条轨迹：

| suite | 模式（prepare 元数据） | 用途 |
|---|---|---|
| `issue111-d-probe-off-a` | `--mode on --probes off --single-cold`，`LVZ_LIFECYCLE_RECORDING=1`、`LVZ_LIFECYCLE_PROBES=0` | 探针关闭对照冷启动 A（仅差探针安装） |
| `issue111-d-probe-off-b` | 同上 | 探针关闭对照冷启动 B（独立复跑） |
| `issue111-d-probe-on-a` | `--mode on --probes on --single-cold`，`LVZ_LIFECYCLE_RECORDING=1`、`LVZ_LIFECYCLE_PROBES=1` | 探针开启冷启动 A |
| `issue111-d-probe-on-b` | 同上 | 探针开启冷启动 B（独立复跑） |

四条臂都通过 `--expected-recorder-sha256` 固定同一个 recorder 构建身份；`check` 会拒绝任何子审计
声明不同构建的轨迹。子审计真实路径为 `experiments/runs/<suite>-s42-c0`（suite 目录本身没有 audit）。

比较：

1. `off-a` vs `off-b`：对照可复现；
2. `on-a` vs `on-b`：新模式可复现（每个子运行都须有有效回执）；
3. `off-a` vs `on-a`：**持久化适配器**共同证据比较；用 `tools/issue111_lifecycle_compare.py` 比较
   lifecycle 流（只归一化 run_id/branch_id/session_id，不剥离事实/计数/序号），并报告首个分叉
   与比较范围；不能用事件流不同本身判失败，也不能声称证明了所有隐藏状态无扰动；
4. tick 940：在声明窗口内扫描“无死亡阶段即消失/释放”的实体，逐条给坐标；不得预设 33 个对象；
   捕获级结论在 hook 审查通过前保持 `unavailable`。

共同证据比较排除模式元数据（`lifecycle_recording` capability 块、lifecycle 文件/回执）。

## 4. 资源与隔离

- 单个游戏进程串行执行；执行前 `doctor` 必须报告无运行中的 `PlantsVsZombies.exe`。
- 磁盘空闲 ≥ 4 GiB；四个 suite 目录与全部预期子目录必须不存在（全新目录），不得覆盖 #99 四轨迹、
  报告、seal 或 manifest。
- 游戏文件必须与 `dependencies.lock.json` 哈希一致；`--require-game` 时缺失/变化即失败。
- 输出：suite 证据在 `experiments/runs/<name>` 与其子运行目录，派生报告在 `work/issue111-d/`；
  旧目录只读。封存写 `<name>.lifecycle-seal.json`（绑定 plan、suite 报告、构建与每个子运行的
  run manifest/audit manifest/codec receipt 哈希）。

## 5. 命令（本轮只冻结，不执行；`prepare`/`check`/`seal` 为 dry-run 可解析命令，`run` 会启动游戏）

```powershell
$env:PYTHONPATH='src'
python tools/issue111_stage_d.py --root . check
python tools/issue111_stage_d.py --root . doctor --require-game
python tools/issue111_lifecycle_experiment.py --root . prepare --name issue111-d-on-a --plan experiments/plans/issue99-shovel-control.json --mode on --skip-build
python tools/issue111_lifecycle_experiment.py --root . run --name issue111-d-on-a
python tools/issue111_lifecycle_report.py experiments/runs/issue111-d-on-a --out work/issue111-d/on-a.json
python tools/issue111_lifecycle_compare.py experiments/runs/issue111-d-off-a experiments/runs/issue111-d-on-a
python tools/issue111_lifecycle_experiment.py --root . check --run issue111-d-on-a
python tools/issue111_lifecycle_experiment.py --root . seal --run issue111-d-on-a
python tools/issue111_lifecycle_experiment.py --root . verify --run issue111-d-on-a
```

`run` 在启动前强制执行冻结的 plan/资源合同（plan 身份、tick 上限、磁盘预留、单一游戏进程），
不依赖用户手动跑 doctor；`check`/`seal` 要求 suite 完成、每个子运行严格审计与模式一致；现有 seal
不会被静默替换，`verify` 可只读复核 seal 与当前证据是否一致。

`prepare` 只锁定 plan 字节/归一化身份、root、模式与预期子运行名，并写 suite 旁的
`<name>.lifecycle.json`；它不创建 suite 目录（`run_suite` 要求输出目录不存在）。`run` 校验锁定
身份后调用真实 `evaluation.run_suite`（与公开 CLI 同一函数）；`--skip-build` 只在 prepare 时显式
记录，绝不隐式跳过。`check`/`seal` 要求 suite 报告完成、每个预期子运行存在、每个子运行的
`AuditLog(require_closed=True)` 通过、且模式 capability 与开启/关闭一致。

## 6. 判定与失败处理

| 判定 | 通过条件 | 失败处理 |
|---|---|---|
| 采集机制完整性 | 每个子运行 lifecycle 严格校验 valid（含关闭回执）且审计严格读取 closed、计数平衡、无探针故障 | 保留失败现场，报告故障坐标，不封存为合格证据 |
| 探针安装效应（仅此范围） | probe off/on 在声明范围内共同证据逐边界一致，或首个分叉被定位并解释；不声称无插桩非扰动 | 报告首个分叉与比较范围；v2 事实只出现在 on 臂，属预期差异 |
| 两次重复性 | on-a 子运行 == on-b 子运行（同一模式、声明范围内） | 定位环境/审计噪声；不挑选时段 |
| 死亡/移除覆盖 | 仅在候选证据通过维护者审查并实现 hook 后判定；当前为 `unavailable` | 不勾选 #99/#105 正向门槛；建立最小后续任务 |
| 首次击杀证据充分性 | 由 `tools/issue111_lifecycle_report.py` 判定；当前 `first_kill_proven=false` | 如实输出缺口，不缩减标准 |

## 7. 执行前的维护者关卡

1. 确认 `issue111-原生候选证据.md` 的语义判定与 ABI-safe 拦截方案；未确认则 D 只跑初始化探针，
   tick 940 的捕获级结论保持 `unavailable`。
2. 确认是否要求“真正无插桩构建”的 off/on 对照；若需要，安排第二种构建身份并重冻计划，而不是把
   持久化 switch 当成插桩 switch。
3. 确认资源与排期后，由执行者在独立 session 串行运行四个 suite；本轮不运行。
