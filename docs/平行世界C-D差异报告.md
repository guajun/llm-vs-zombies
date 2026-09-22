# 平行世界 C / D 差异报告（离线只读）

日期：2026-09-23。范围：对两个已封存 `evaluation run` 世界做纯离线对比。**未启动游戏、未写入任何 run 目录、未改动根仓库**。

对象（绝对路径，只读）：

| 世界 | source | 冷重放 |
|---|---|---|
| C | `F:\llm-vs-zombies\experiments\runs\m1-par-c2-s42-c0` | `…\m1-par-c2-s42-c1` |
| D | `F:\llm-vs-zombies\experiments\runs\m1-par-d-s42-c0` | `…\m1-par-d-s42-c1` |

证据来源一律用「文件 / JSON Pointer / 命令」指到具体位置；无法核实的写 **[待核实]**。

---

## 0. 结论摘要

1. **B(0)（`seq=21`、`pre_step`、epoch 3 / revision 5 / tick 0）两侧状态只差两个字段**：`/app/mj_clock` 与 `/sound_effects/app_update_count`，C 都是 1307、D 都是 1340（差值恒为 33）。`/app` 组件内**唯一**不同的字段就是 `/app/mj_clock`。
2. 分类：`/sound_effects/app_update_count` 属于**已显式锚定**的初始化计数；`/app/mj_clock` 属于**已记录、已随冷重放恢复、但从未被跨运行统一/锚定**的字段 → 归入第二类（真问题）。
3. **会传播**：首个 tick 级分歧出现在 **tick 1200（post_step，`state-deltas` 第 2399 条）**，落在 28 只舞者/伴舞僵尸的 body 动画与僵尸槽位上；随后 RNG 在 tick 1242、植物/粒子在 1244、board/coins 在 1655 依次分叉。
4. 机制已机械化验证：舞者相位是「绝对 App 计数」的纯函数（`(mAppCounter % 460) / 20`）。两侧计数相差 33 → **同一批相位切换在 D 早 33 tick 触发**；触发用到的计数集合两边完全相同。
5. **世界内可复现**（C0≡C1、D0≡D1，4000/4000 边界状态全等）；**跨世界不一致**（从 B(0) 起就不同）。这两件事互不矛盾，但「平行世界逐 tick 可比」这个说法目前**不成立**。

---

## 1. 方法与工具

- 读容器：仓库自身的 `src/llm_vs_zombies/evidence_codec.py`（`EvidenceStore`），它会校验 `audit/evidence-codec.json` 回执并把 gzip 容器解成原始 JSONL 字节；`state-deltas.jsonl` 用 `audit_compare.patch()`（RFC 6902 的 `add/remove/replace`）逐条展开。
- 辅助工具：`tools/compare_worlds.py`（本次交付），三种模式：
  - 默认模式：两侧 `checksums.jsonl` 逐行比对（**组件摘要**与**非摘要载荷**分开统计）+ 逐边界展开状态并定位差异。为避免重复整状态 diff，只重算补丁触碰到的子树，并在每第 N 个边界用暴力全量 diff 交叉校验（`--verify-every`，本报告各次比对用 400/500/1000，均**无 mismatch**；跨世界那次的抽查点覆盖了分歧发生之后）。
  - `--dancer-clock`：验证「舞者相位 = 绝对 App 计数的函数」这条机制。
  - `--self-test`：24 项离线自检（补丁语义、指针转义、差异枚举、缺字段/数组长度/类型、输入不被就地修改、增量 diff 与暴力全量 diff 一致、数组插入刷新整段、舞者相位映射，以及一组用 `compress_evidence()` 造出来的合成世界端到端比对）。

```powershell
# 自检
python tools/compare_worlds.py --self-test
# B(0) 差集 + 传播轨迹（世界内或跨世界都可跑）
python tools/compare_worlds.py --a <runA> --b <runB> --verify-every 400 --json report.json
# 机制验证
python tools/compare_worlds.py --a F:\llm-vs-zombies\experiments\runs\m1-par-c2-s42-c0 `
                              --b F:\llm-vs-zombies\experiments\runs\m1-par-d-s42-c0 --dancer-clock
```

---

## 2. B(0) 差异字段表（精确 JSON Pointer + 原始值 + 分类 + 证据）

`state-deltas.jsonl` 第 0 条即 B(0) 完整状态（`{"initial": …}`，配 `checksums.jsonl` 第 0 行 `seq=21 / pre_step / epoch 3 / revision 5 / tick 0`）。对 C0 与 D0 展开后逐叶子比对，整个状态下**只有两处**不同：

| # | JSON Pointer | 底层字段 | C 原值 | D 原值 | 所属组件摘要 | 分类 | 证据来源 |
|---|---|---|---|---|---|---|---|
| 1 | `/app/mj_clock` | `LawnApp+0x838`（本项目命名 `mj_clock`） | `1307` | `1340` | `app`（摘要不同） | **未锚定（真问题）**：已在初始化配方中记录、冷重放用 `clock_restore` 恢复，但**没有任何跨运行统一/锚定** | `determinism/audit.cpp:391`（读 `app+0x838`）；`initialization-recipe.json` 的 `clock_anchor.mj_clock` / `postwarm_clock.mj_clock`；`audit/events.jsonl` 的 `clocks_restored`（epoch3 rev2 tick0，snapshot `mj_clock`） |
| 2 | `/sound_effects/app_update_count` | `LawnApp+0x484`（`mUpdateCount`） | `1307` | `1340` | `sound_effects`（摘要不同） | **已显式锚定**（`initial_app_update_anchor_v1`，每 epoch 一次性写入） | `docs/app-update-anchor-native.md`；`determinism/app_update_anchor.cpp:8,59-72`；`initialization-recipe.json` 的 `app_update_anchor.app_update_count`；`events.jsonl` 的 `app_update_anchored`（epoch3 rev4 tick0，`after=1307/1340`）；`seed-42-replay-1/replay-report.json` 的 `app_update_anchor` 块（`source_after == actual_after`） |

同一条边界上其余组件摘要逐一相同（`checksums.jsonl` 第 0 行 `digests` 逐键对比）：

`board` / `challenge` / `coins` / `draw_schedule` / `fp_environment` / `grid_items` / `mowers` / `particle_shake` / `plants` / `projectiles` / `reanimations` / `rng` / `schema` / `seeds` / `zombies` 全部相同；只有 `app`、`sound_effects` 与聚合 `all` 不同（`app` 因 #1、`sound_effects` 因 #2、`all` 因前两者）。

> 两个字段数值相同（1307/1340）不是巧合：两者都按一次 App 更新循环 +1，因此 B(0) 处的差值恒等于「两侧从启动到 B(0) 各跑了多少圈」之差 = **33**。

---

## 3. 分类判定与证据

### 3.1 `app_update_count`：已被显式锚定（不是问题）

- 文档语义：`docs/app-update-anchor-native.md` 开头即写明该能力「source 显式把自己实际值写一次；冷初始化显式把 source 记录的目标值写进去」，且「There is no subtraction of an offset in the state comparator」——即靠**真实内存写入**把两侧拉平，而不是比较器旁路。
- 本批次实际值：C 配方 `app_update_count = 1307`，D 为 `1340`；两条 run 的 `app_update_anchored` 事件均在 `epoch 3 / revision 4 / tick 0`，`after` 与各自配方一致。
- 冷重放侧：`seed-42-replay-1/replay-report.json` 的 `app_update_anchor` 显示 `source_after == actual_after == requested`（C 1307、D 1340），`before_values_compared=false`（锚定之前的历史值天然不同，不参与比较）。
- **注意范围**：锚定只把「同一个世界内 source 与冷重放」拉平；两个**不同世界**各自锚到自己的实际值，所以跨世界比较时它依然差 33。这一点由 `docs/sound-counter-origin-native.md` 的「Cross-run complete state comparison takes place after the App anchor and at B0, when the App count has actually been made equal」界定。

### 3.2 `mj_clock`：已记录、已恢复，但**未跨运行统一** → 真问题

支持「属于已声明初始化输入」的证据：

- `initialization-recipe.json` 明确记录三个时钟：C `{"game_clock":3151,"effect_clock":53166,"mj_clock":1307}`，D 为 `…,"mj_clock":1340`（`clock_anchor` 与 `postwarm_clock` 两处一致）。
- `audit/events.jsonl` 中四个 run 都有 `clocks_restored`（`epoch 3 / revision 2 / tick 0`），snapshot 里的 `mj_clock` 分别是 1307（C0、C1）/1340（D0、D1）——冷重放是**按记录值恢复**的，不是重跑出来的。
- `docs/determinism.md`（「接口与时序」一节）：三个时钟「实验应明确记录在 B(0) 统一设定的三个值，并在重放用相同初始化流程恢复它们」。这条只约束「记录 + 重放恢复」。

说明它**不属于**「已声明可跨运行不同」的证据：

- 仓库里没有任何能力、配方字段或门槛声明「平行世界的 `mj_clock` 可以不同」。与 `app_update_count` 的对比最能说明问题：后者有专门能力把被比较双方**真正改成相等**，前者没有对应步骤。
- 它**没有**被比较器归一掉：`/app/mj_clock` 属于 `app` 组件摘要，所以在 C0–D0 的 4000/4000 行摘要里 `app` 始终不同（见 §4.1）。
- 它**会被原版引擎当绝对计数读取**，因此不是「无害的诊断量」（见 §5）。

**判定**：`/app/mj_clock` 落在第二类——**未被覆盖/未被锚定**。它使两个世界的「同一 Action」实际上不是同一时间基准，是本次差异链的唯一起点。

---

## 4. 是否传播

### 4.1 组件级首现时间（C0 vs D0，4000 条边界 = tick 0…2000）

| 组件摘要 | 首次不同 | 不同边界数 | 说明 |
|---|---|---|---|
| `all` / `app` / `sound_effects` | 第 0 行（B(0)） | 4000 / 4000 | 由 §2 两个计数造成，全程持续 |
| `reanimations` / `zombies` | 第 2399 行（tick 1200） | 1601 | 首个 tick 级分歧 |
| `rng` | 第 2483 行（tick 1242） | 1517 | MT 实例状态开始不同 |
| `particle_shake` / `plants` | 第 2487 行（tick 1244） | 1513 | |
| `board` / `coins` | 第 3309 行（tick 1655） | 691 | |
| `challenge` / `draw_schedule` / `fp_environment` / `grid_items` / `mowers` / `projectiles` / `schema` / `seeds` | 从不 | 0 | |

### 4.2 首个 tick 级分歧的精确内容（`state-deltas` 第 2399 行，tick 1200，`post_step`）

第 2399 行两侧共有 4499 个叶子差异，其中 2 个是 B(0) 的两个计数（§2），**新增** 4497 个；形状只有 58 种，集中在 28 个节点 + 28 个槽位：

**a) 28 个僵尸 body 动画节点**（`reanimations` 组件，路径模式 `/reanimations/nodes/zombies~1<id>~1body#0/state/…`），逐节点字段同构：

| JSON Pointer（以 `zombies/df7c0008/body#0` 为例） | C 原值 | D 原值 | 浮点解码 |
|---|---|---|---|
| `…/state/anim_rate_bits` | `1098414524` | `1099956224` | 15.52972 → 18.0 |
| `…/state/anim_time_bits` | `1053586882` | `1058916866` | 0.39933592 → 0.61636364 |
| `…/state/last_frame_time_bits` | `1053338743` | `1058642330` | 0.39194080 → 0.6 |
| `…/state/frame_start` | `46` | `67` | |
| `…/state/frame_count` | `21` | `11` | |
| `…/state/track_scalars/<组>/<下标>` | 各自旧值 | 各自新值 | 4301 个叶子，占新增差异的绝大部分 |

**b) 同一批僵尸的逻辑字段**（`zombies` 组件，槽位 `0,7,8,13,23,26,50…71`，共 28 个）：

| JSON Pointer | 字段 | C 原值 | D 原值 |
|---|---|---|---|
| `/zombies/slots/<n>/fields/00000028` | `mZombiePhase` | `44` | `45` |
| `/zombies/slots/<n>/fields/00000124` | `mOriginalAnimRate` | `1103101952`（30.0；伴舞槽位为 `0`） | `1099956224`（18.0） |

偏移名依据：`docs/机制读取点表.md`（D6-2 `+0x28` = `mZombiePhase`；§3 冷启动差异字段表把 `Zombie+0x124` 标为 `mOriginalAnimRate`）。

这 28 个槽位的僵尸类型为 `8`（`ZOMBIE_DANCER`，7 只：槽 `0,7,8,13,23,26,50`）与 `9`（`ZOMBIE_BACKUP_DANCER`，21 只：槽 `51…71`）；**没有**任何其他槽位出现相位 45。

### 4.3 冷重放侧完全复现

C1 vs D1 得到**同样**的结果：首个额外分歧同样是第 2399 行 tick 1200，同样 1601 条边界有额外差异，同样的组件首现顺序。也就是说，跨世界分叉是**可复现的确定性后果**，不是某一次运行的抖动。

---

## 5. 机制：为什么「33 的计数差」会变成玩法分歧

### 5.1 原版代码直接把这个绝对计数当玩法时钟

候选反编译（提交 `8a2d121899ba5cb4df644cd7d2e4c1aaf88dd238`，本地副本在根仓库 `work/replay-research/`，未纳入版本控制）：

```cpp
// Lawn_Zombie.cpp:6071-6096  Zombie::GetDancerFrame()
int aFrameLength = 20, aFramesCount = 23;            // 相位周期 = 460
…
return (mApp->mAppCounter % (aFrameLength * aFramesCount)) / aFrameLength;

// Lawn_Zombie.cpp:6099-6109  Zombie::GetDancerPhase()
//   frame<=11 → PHASE_DANCER_DANCING_LEFT(44)
//   frame==12 → PHASE_DANCER_WALK_TO_RAISE(45)
//   frame<=15 → RAISE_RIGHT_1(47) … frame<=21 → RAISE_RIGHT_2(49) else RAISE_LEFT_2(48)
```

`ConstEnums.h:1216-1267` 给出 `PHASE_DANCER_DANCING_LEFT = 44`、`PHASE_DANCER_WALK_TO_RAISE = 45`（`ConstEnums.h:1315-1343` 给出 `ZOMBIE_DANCER = 8`、`ZOMBIE_BACKUP_DANCER = 9`）。`Lawn_Zombie.cpp:2907-2910`（舞者本体）与 `2992-2995`（伴舞）在进入 `PHASE_DANCER_WALK_TO_RAISE` 时正好执行 `PlayZombieReanim("anim_armraise", REANIM_LOOP, 10, 18.0f); …->mAnimTime = 0.6f;` —— 与 §4.2 观察到的 `anim_rate = 18.0`、`last_frame_time = 0.6`、`frame_start/frame_count = 67/11` 完全吻合（随后一帧按 `mAnimTime += 0.01*rate/frameCount` 推进到 `0.6163636`）。

### 5.2 算术核对

| 量 | C | D |
|---|---|---|
| B(0) `mj_clock` | 1307 | 1340 |
| tick 1200 时 `mj_clock` | 2507 | 2540 |
| `mj_clock % 460` / 帧号 | 207 / 10 → 相位 44 | 240 / **12** → 相位 **45** |
| 该相位切换实际发生的 tick | **1233**（计数 2540） | **1200**（计数 2540） |

### 5.3 机械化验证（`--dancer-clock`）

对两条 source 全程逐边界检查「类型为 8/9 且相位属于 44–49 的僵尸，其相位是否等于 `dance_phase(mj_clock)`」：

| 检查 | C0 | D0 |
|---|---|---|
| 观测样本数 / 命中 / 反例 | 48053 / 48053 / **0** | 47907 / 47907 / **0** |
| 相位切换（舞者相位之间）次数 | 321 | 327 |
| 切换点计数是否都落在 20 的整数倍（帧边界） | 是 | 是 |
| 切换后相位是否等于预测相位 | 是 | 是 |
| 切换点计数集合（前 12 个） | `2540,2560,2620,2680,2740,2760,3000,3020,3080,3140,3200,3220…` | **完全相同** |
| 切换 tick | 1233,1253,1313,… | 1200,1220,1280,… |

两者关系：`D 的切换 tick = C 的切换 tick − 33`（脚本输出 `first_step_tick_offset_b_minus_a = -33`、`step_ticks_equal_after_offset = true`、`step_counters_identical = true`）。

### 5.4 这不等于「整体时间平移」

两个世界并不是彼此的平移副本：舞者相位、出怪/金币等后续事件被钉在**绝对 App 计数**上，而两侧的绝对计数起点不同，所以同一 tick 上两侧处于不同调度位置。差异随后通过「动画创建消耗共享 MT RNG」这类通道回到模拟（`docs/动画耦合与可重复性取证.md` §2），表现为 tick 1242 起 `/rng/instances` 分叉。因此**不能**用「后面只是动画差一点」来收尾。

---

## 6. 世界内可复现 vs 跨世界一致（分开陈述）

### 6.1 世界内：source vs 冷重放 —— **相等**

| 证据 | C（c0 vs c1） | D（d0 vs d1） |
|---|---|---|
| `evaluation.md` 的 `engine_replay` 门槛 | `pass` | `pass` |
| `seed-42-replay-1/replay-report.json` 的 `equal` | `true`（SHA-256 `5612a6c7…`） | `true`（SHA-256 `0ae68c3f…`） |
| 组件摘要逐行比对（4000 行） | 不同行数 **0** | 不同行数 **0** |
| 展开状态逐边界比对（4000 条） | 有差异的边界 **0** | 有差异的边界 **0** |
| `checksums.jsonl.gz` 的 `plain_sha256` | `ed5dc801…` vs `0b42c3aa…` **不同** | `1f37db88…` vs `5c4fd0ea…` **不同** |
| `plain_sha256` 不同的原因 | 4000/4000 行只差 `/payload/request_id`（源 `ae2e42b7…` → 重放 `replay-m1-par-c2-s42-c1-64920-0`） | 同样只差 `/payload/request_id` |

即：**「plain_sha256 不等」不等于状态不同**。`checksums.jsonl` 每行既带组件摘要又带审计载荷，冷重放会给每个请求换一个新 request id，于是原始字节必然不同；而 18 个组件摘要**没有一个**变化。世界内可复现性成立（在已声明覆盖范围内）。

附注：C 的 `evaluation.json` 里 `cases[0].cold_starts[1].passed = false`，D 为 `true`。这不是状态比较失败：`evaluation.py:732` 要求 `equal and infrastructure_passed`，而 C1 的 `evaluation-runtime-windows.json` 是 `foreground_check_status = "unverified"`（D0/D1 为 `pass`），窗口观测门槛未通过导致 `infrastructure_passed = false`。`engine_replay` 门槛本身在 C 侧同样是 `pass`。

### 6.2 跨世界：C vs D —— **不一致**

| 证据 | 结果 |
|---|---|
| B(0) 状态差集 | 恰好 2 个指针（§2），其中 `/app/mj_clock` 为 33 计数差 |
| `checksums.jsonl.gz` `plain_sha256` | `ed5dc801…` vs `1f37db88…`，不同 |
| 组件摘要不同的行数 | `all`/`app`/`sound_effects` 4000/4000；其余见 §4.1 |
| 展开状态有差异的边界 | 4000/4000（其中 1601 条超出 B(0) 那两个计数） |
| 冷重放对（C1 vs D1） | 同样的首现 tick、同样的 1601 条边界 → 可复现，非抖动 |

结论：**跨世界一致性不成立，而且不是「引擎不确定」造成的**——两个世界的初始绝对 App 计数不同，原版引擎按该计数调度事件，于是同一 Action 在两个世界里执行在不同的时间基准上。要在比较前修掉它，或者把「逐 tick 可比」的说法降级。

---

## 7. 下一步建议

1. **先加门槛，不要先改判据**：把 `/app/mj_clock` 纳入平行世界的进入条件——两边 B(0) 该值不等时，直接标注「逐 tick 不可比」。`tools/compare_worlds.py` 的默认输出已经能直接给出这 2 个指针，可直接当门槛输入。
2. **在 B(0) 真正统一 App 计数**（推荐方向）：现有 `initial_app_update_anchor_v1` 只写 `LawnApp+0x484`，而舞者路径读的是 `LawnApp+0x838`。可行做法是扩展该能力（或新增一个能力）同时把 `+0x838` 钉到配方给定值，并在平行分支的 `clock_anchor.mj_clock` 上要求两侧相同、由启动流程强校验。注意这属于「改变初始化输入」，需要与现有锚定一样给出真实内存 before/after 证据。
3. **若接受改原版**（须标 `original_engine_bitwise_unmodified=false` 并重新验收）：候选反编译里 `Zombie::GetDancerFrame()` 已有 `DO_FIX_BUGS` 分支把它换成 `mBoard->mMainCounter`（局内计数），这正是「把事件从绝对 App 计数改为局内 tick」的修法。改动后必须重跑完整验收，不能沿用本文档的一致/不一致结论。
4. **不要做的事**：不要把 `mj_clock`、舞者相位或 `anim_time_bits` 从严格比较里删掉来「让比较通过」。`docs/determinism.md` 明确禁止为了适应不同等待时长而删字段，`docs/动画耦合与可重复性取证.md` §1/§6 也已证明这一族字段是玩法时钟。这类改动必须走 §7.3 的显式声明路线。

---

## 8. 未做 / 待核实

- 未启动游戏，未写入任何 run 目录；两个 run 目录全程只读（`EvidenceStore` 仅读；无 `chmod`/改文件操作）。
- `LawnApp+0x838` 的**规范字段名**（`mAppCounter` 还是别的）未在目标二进制上独立反汇编核实 **[待核实]**；但「该字段是舞者相位的输入」已由两次运行共 95960 个观测点零反例 + 切换计数集合完全一致证明。
- 未穷举所有读取该计数的原版调用点：本文只验证舞者/伴舞路径（首个分歧点即在此），其余读取点是否还有「绝对计数敏感」的事件未做清单。
- 未做 `≥10` 次冷启动、`full_cycle`、recovery 等门槛；两个世界的 `evaluation.md` 均为 `experiment_ready = 否`（`unmet_gates` 含 `ten_cold_starts`、`full_cycle`、`runtime_windows` 等）。
- 未分析「若两侧 App 计数被统一，剩余分歧是否为零」——那需要一次新的真实运行（本次明确不启动游戏）。

---

## 附录 A 复现命令

```powershell
cd <clone>\tools
# 1) B(0) 差集 + 传播轨迹（跨世界）
python compare_worlds.py --a F:\llm-vs-zombies\experiments\runs\m1-par-c2-s42-c0 `
                         --b F:\llm-vs-zombies\experiments\runs\m1-par-d-s42-c0 `
                         --verify-every 400 --json c0-vs-d0.json
# 2) 世界内（source vs 冷重放）
python compare_worlds.py --a …\m1-par-c2-s42-c0 --b …\m1-par-c2-s42-c1 --verify-every 1000 --json c0-vs-c1.json
# 3) 机制验证
python compare_worlds.py --a …\m1-par-c2-s42-c0 --b …\m1-par-d-s42-c0 --dancer-clock
# 4) 自检
python compare_worlds.py --self-test
```

## 附录 B 首个分歧（tick 1200）涉及的 28 个对象

动画节点（`/reanimations/nodes/…`）：`zombies/df7c0008/body#0`、`df7d0007`、`df840000`、`df86000d`、`df900017`、`df93001a`、`dfab0032`、`dfac0033`、`dfad0034`、`dfae0035`、`dfaf0036`、`dfb00037`、`dfb10038`、`dfb20039`、`dfb3003a`、`dfb4003b`、`dfb5003c`、`dfb6003d`、`dfb7003e`、`dfb8003f`、`dfb90040`、`dfba0041`、`dfbb0042`、`dfbc0043`、`dfbd0044`、`dfbe0045`、`dfbf0046`、`dfc00047`（均带 `/body#0` 后缀）。

僵尸槽位：`0, 7, 8, 13, 23, 26, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70, 71`。
