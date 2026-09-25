# #111 阶段 D 冻结实验计划（待维护者确认后执行）

日期：2026-09-26。机器可读计划：[`issue111-阶段D计划.json`](issue111-阶段D计划.json)。
本计划**冻结**，本轮不启动游戏、不安装未经审查的死亡/移除/回收 hook。执行前必须通过
`python tools/issue111_stage_d.py doctor --root . --require-game`，并在 PR/issue 中记录结论。

## 1. 目的与边界

验证已合并的初始化生命周期测量（出生探针 + `LVZ_LIFECYCLE_RECORDING` 落盘 + 原子关闭回执）
在经典十二炮短窗口内：采集机制完整、观测不扰动的已验证范围、同一模式两次冷启动可复现，并
为 tick 940 的未知消失路径给出边界坐标与覆盖限制。死亡/移除/回收的生产事实仍取决于维护者
对 [`issue111-原生候选证据.md`](issue111-原生候选证据.md) 的审查；审查未通过时，D 只使用初始化
探针，捕获级死亡/移除结论输出 `unavailable`，不得用 D 结果替代审查。

## 2. 窗口与停止规则（沿用 #99，不改写）

| 项 | 冻结值 |
|---|---|
| 场景 | `jingdian12` |
| seed | 42 |
| 端点定义 | `stop_when.wave_at_least=2`（第一次观测到第 2 波的战斗边界） |
| 模拟 tick 硬上限 | 2000（计划执行期间不得延长） |
| 历史参考 | tick 1201 时 `wave=2`；只作参考，不为复现该数字调整停止规则 |
| 暂停探针 | tick 1000 处 1 秒墙钟暂停（与 #99 相同） |
| #99 计划锚点 | `experiments/plans/issue99-shovel-control.json`；`tools/issue111_stage_d.py check` 会逐字段复核 |

## 3. 模式开关与运行矩阵

同一构建（同一个 `build/recorder.dll` 哈希，绑定到每条 run 的 manifest）下，用环境变量
`LVZ_LIFECYCLE_RECORDING` 切换：

| 运行 | 模式 | 用途 |
|---|---|---|
| `issue111-d-off-a` | `0` | 插桩关闭对照 A（manifest 声明 enabled=false，无 lifecycle 文件） |
| `issue111-d-off-b` | `0` | 对照复跑 |
| `issue111-d-on-a` | `1` | 生命周期记录冷启动 A |
| `issue111-d-on-b` | `1` | 生命周期记录冷启动 B |

比较：

1. `off-a` vs `off-b`：对照可复现；
2. `on-a` vs `on-b`：新模式可复现；
3. `off-a` vs `on-a`：插桩不扰动对照——比较共同可观测边界状态、动作、RNG 与结果；报告首个
   分叉及比较范围，不能用事件流不同本身判失败，也不能声称所有隐藏状态无扰动；
4. tick 940：在声明窗口内扫描“无死亡阶段即消失/释放”的实体，逐条给坐标；不得预设 33 个对象。

共同证据比较排除模式元数据（`lifecycle_recording` capability 块、lifecycle 文件/回执）。

## 4. 资源与隔离

- 单个游戏进程串行执行；执行前 `doctor` 必须报告无运行中的 `PlantsVsZombies.exe`。
- 磁盘空闲 ≥ 4 GiB；`experiments/runs/issue111-d-*` 必须不存在（全新目录），不得覆盖 #99 四轨迹、
  报告、seal 或 manifest。
- 游戏文件必须与 `dependencies.lock.json` 哈希一致；`--require-game` 时缺失/变化即失败。
- 输出新目录：run 证据在 `experiments/runs/<name>`，派生报告在 `work/issue111-d/`；旧目录只读。

## 5. 命令（本轮只冻结，不执行）

```powershell
$env:PYTHONPATH='src'
python tools/issue111_stage_d.py check
python tools/issue111_stage_d.py doctor --root . --require-game
python tools/issue111_lifecycle_experiment.py prepare --name issue111-d-on-a --plan experiments/plans/issue99-shovel-control.json --mode on
python tools/issue111_lifecycle_experiment.py run --name issue111-d-on-a --plan experiments/plans/issue99-shovel-control.json --mode on
# 其余 off-a/off-b/on-b 同法，串行执行
python tools/issue111_lifecycle_report.py experiments/runs/issue111-d-on-a --out work/issue111-d/on-a.json
python tools/issue111_lifecycle_experiment.py seal --run experiments/runs/issue111-d-on-a
```

`prepare` 只创建全新 run 目录并打印带 `LVZ_LIFECYCLE_RECORDING` 的启动命令，不启动游戏；
`run` 设置环境变量后调用既有 `python -m llm_vs_zombies.evaluation run` 入口；`seal` 先校验再
用现有 evidence codec 压缩并写绑定摘要。

## 6. 判定与失败处理

分别判定四项，不互相顶替：

| 判定 | 通过条件 | 失败处理 |
|---|---|---|
| 采集机制完整性 | lifecycle 严格校验 valid（含关闭回执）、审计严格读取 closed、计数平衡、无探针故障 | 保留失败现场，报告故障坐标，不封存为合格证据 |
| 观测不扰动（已验证范围） | off/on 在声明范围内共同证据逐边界一致，或首个分叉被定位并解释；范围外不声称 | 报告首个分叉与比较范围；不因事件流差异本身判失败 |
| 两次重复性 | on-a == on-b（同一模式、声明范围内） | 定位环境/审计噪声；不挑选时段 |
| 首次击杀证据充分性 | 由 `tools/issue111_lifecycle_report.py` 判定；`first_kill_proven=false` 时如实输出缺口 | 不勾选 #99/#105 正向门槛；建立最小后续任务 |

## 7. 执行前的维护者关卡

1. 确认 `issue111-原生候选证据.md` 的死亡阶段权威写入点、移除/回收 hook 位置与去重键；未确认
   则 D 只跑初始化探针，tick 940 的捕获级结论保持 `unavailable`。
2. 确认本轮新增的 C 阶段工具（分析命令、实验入口、消费示例）已在冷读路径通过测试。
3. 确认资源与排期后，由执行者在独立 session 串行运行上述四条；本轮不运行。
