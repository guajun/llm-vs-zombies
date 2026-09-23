# 平行世界验收：S(A) == S(B) 成立

日期：2026-09-23。状态：**本机实测通过**（范围见末节"不声称什么"）。

## 判据

```
S(root + Action) → S(A)
S(root + Action) → S(B)
S(A) == S(B)
```

A、B 是**同一个 root、同一个 Action** 的两个平行世界（不是两个不同动作的分叉）。比较方式是**逐边界的 `digests` 内容摘要**，不是整文件哈希——`checksums.jsonl` 含审计信封序号（`seq`），被拒请求会多写记录，整文件哈希天然会不同。

## 结果

| 项 | 结果 |
|---|---|
| 边界数 | 4000 vs 4000 |
| 首个 digests 不同的边界 | **无** |
| `S(A) == S(B)` | **True** |
| world-e5：冷重放 vs 自己 source | 相同 |
| world-f5：冷重放 vs 自己 source | 相同 |
| world-e5 B(0) `/app/mj_clock` | **1340** |
| world-f5 B(0) `/app/mj_clock` | **1340** |
| world-e5 B(0) `/sound_effects/app_update_count` | **1500** |
| world-f5 B(0) `/sound_effects/app_update_count` | **1500** |

两个值都等于**实验声明的共同目标**，而不是各自观测到的当场值。

## 环境与参数

| 项 | 值 |
|---|---|
| 模式 | strict（`sound_effects_allocation_none_v1`） |
| 种子 / 预算 | 42 / 2000 tick |
| 冷启动 | 每世界 1 次 |
| 策略 | `examples/liangyi_gargantuar_a.py` |
| 计划声明 | `--mj-clock 1340 --app-update-count 1500` |
| 执行方式 | **主检出串行**跑两个世界（独立检出那条路当天不可用，见 `docs/并行实验约定.md` §2.5） |
| 验收入口 | `python -m llm_vs_zombies.evaluation run work/m1-world5.json --output experiments/runs/world-{e5,f5}` |

## 结论链（怎么走到这一步的）

| 阶段 | 首分歧 | 处理 |
|---|---|---|
| C vs D（PR #59） | B(0) 的 `app` 组件：`/app/mj_clock` 1307 vs 1340；`/sound_effects/app_update_count` 同为该值对 | 定位到唯一未锚定字段 |
| e4 vs f4 | B(0) 只剩 `sound_effects`：`/sound_effects/app_update_count` 1395 vs 1422 | #61（`mj_clock` 固定目标）已生效，`app` 组件转为相同 |
| **e5 vs f5（本次）** | **无** | #64（`app_update_count` 声明式固定目标）生效 |

机制侧的支撑：`Zombie::GetDancerFrame()` = `(mApp->mAppCounter % 460) / 20`——B(0) 计数不统一时，舞王僵尸相位会在 tick 1200 起整体错开（C/D 期间实测，95,960 个观测点零反例）。所以这两个计数必须归一化，而不是"记录当场值"。

## 不声称什么

- **不等于十次冷启动 / 完整两旗门槛**：本次是 smoke 档，主动排除了 `full_cycle` 与 `ten_cold_starts`
- **不等于覆盖全部内部状态**：审计 schema 有已声明的未覆盖字段（见 `docs/determinism.md`）
- **不等于"不同 Action 的分叉也一致"**：#51 的 A/B（分叉后多一株小喷菇）因那条动作被引擎拒绝（`card_not_usable`）而未真正分叉，那份对照只证明"未分叉时两世界相同"
- **不等于纯无窗口服务**：headless 仍 = 隐藏窗口 + 原版 DirectDraw 初始化

## 证据位置

- `experiments/runs/world-e5`（含 `evaluation.md`、`seed-42-replay-1`）
- `experiments/runs/world-e5-s42-c0` / `-c1`
- `experiments/runs/world-f5` / `-s42-c0` / `-c1`
- 计划：`work/m1-world5.json`

## 统一形状（P1）后的复验（2026-09-23）

同一结论在**声明式统一形状**下重做一遍：两条锚定不再各占一个标量参数，而是由一张表完成。

| 项 | 值 |
|---|---|
| 计划 | `work/m1-world6.json`（schema `lvz.evaluation-plan.v2`） |
| 声明表 | `b0_normalization`：`/sound_effects/app_update_count=1500` 先、`/app/mj_clock=1340` 后（顺序被强制，对应 counter origin → App anchor → MJ clock → warm 的链） |
| 命令 | `--b0-normalize '<JSON 对象>'`（可重复；`--mj-clock` / `--app-update-count` 是过渡别名） |
| 两个世界 | `world-e10` / `world-f10` |
| 四个关键门槛 | `build_and_tests` / `private_launch` / `archive_integrity` / `scenario` / `engine_replay` **全 pass** |
| `S(A) == S(B)` | **4000/4000 边界 digests 全同，首分歧无** |
| 各自冷重放 vs 自己 source | 两份都相同 |
| 两世界 B(0) | `mj_clock = 1340`、`app_update_count = 1500`（都等于**声明的共同目标**） |

与旧标量形状（`world-e5` / `world-f5`）结论一致；差别在于现在由**一张声明表 + 单一回执**完成，且配方完整性检查可运行：

```
holes U\C = 1565, dangling C\U = 0, conflicts = 0
```

1565 条全部是 `/board/<原始地址>`（除 GameClock / EffectCounter / 波表阈值那几个已知的）——即这些 Board 字段目前没有逐字段契约，只被整体状态摘要与共享参考存档兜底。这份清单是给 AvZ 上游提"RNG/时钟原语"时的输入（见 issue #72 的 L1↔L2）。

### 过程中的接线缺陷（已修）

统一形状落地时暴露了 4 处同类缺陷——同一条"两种形状选哪种"的判据在 4 个层次各写了一遍且彼此不一致：

| # | 层 | 修复 |
|---|---|---|
| 1 | Python 初始化（用 runtime 能力判断形状排他） | PR #76 |
| 2 | Python 初始化（统一表已发、legacy 路径仍执行） | PR #77 |
| 3 | 原生 `prepare_render` warm 门（无条件要求 legacy 锚） | PR #78 |
| 4 | 归档读取器/校验器（统一归档仍走 legacy 校验） | PR #80 |

根因是**离线测试从未用过真实的能力组合**（runtime 同时声明统一表 + 两个 legacy 能力）。PR #80 已补"三能力同时声明"的夹具用例，并在原生侧加了 `prepare_render` 的形状用例。

### 本次证据位置

- `experiments/runs/world-e10` / `-s42-c0` / `-c1`
- `experiments/runs/world-f10` / `-s42-c0` / `-c1`
- 计划：`work/m1-world6.json`
