# B(0) 归一化统一实现：单表 + 单回执

日期：2026-09-23。设计依据：[b0-归一化提案](b0-归一化提案.md)（#63，PR #67）。实现：#74。
本文只描述**已落地**的形状与检查；不宣称任何实机验证（`live_verified:false` 不变），不启动游戏，不改写任何既有验收结论。

## 1. 形状

```json
{
  "b0_normalization": [
    {"field": "/sound_effects/app_update_count", "target": 2048, "reason": "B(0) 前 App 循环圈数"},
    {"field": "/app/mj_clock", "target": 2048, "reason": "同一循环的绝对计数"}
  ]
}
```

| 层 | 形状 | 说明 |
|---|---|---|
| 计划 | `lvz.evaluation-plan.v2` 的 `b0_normalization` | 顶层不再有 `mj_clock`/`app_update_count` 标量 |
| CLI | `--b0-normalize '<JSON>'`（可重复，按命令行次序成表） | `evaluation plan`、`launcher start`、`repl` 都有 |
| 过渡别名 | `--mj-clock N` / `--app-update-count N` | 生成单表项，`reason` 标为 `legacy flag --…`；与 `--b0-normalize` 混用直接拒绝 |
| 配方 | `b0_normalization: {configuration, entries}` | `configuration` 与 runtime 声明逐字段相同，`entries` 与计划同序同文 |
| RPC | `b0_normalization`，事件 `b0_normalized` / 失败 `b0_normalization_failed` | 整表一次应用、**一个 revision** |
| 回执 | `lvz.b0-normalization.v1` | `requested` / `before` / `after` / `before_state` / `after_state` + 由 controller 补齐的 `before_version`/`after_version` |

字段集合由 runtime 声明（`hello.game.b0_normalization.fields`，封闭）：`/sound_effects/app_update_count`（`LawnApp+0x484`）与
`/app/mj_clock`（`LawnApp+0x838`）。声明序即执行序，`/app/mj_clock` 必须排在 `/sound_effects/app_update_count` 之后。

## 2. 缺省 fail-closed

| 情形 | 行为 |
|---|---|
| runtime 声明能力、计划未声明 | 在**发出任何初始化请求之前**拒绝，并列出未声明的字段 |
| 表为空数组 | 视为"未声明"，同样拒绝（不是"全部用观测值"） |
| `target` 为 `null`/布尔/浮点/越界 | 拒绝 |
| `field` 不在声明集合内、重复、顺序不满足依赖 | 拒绝 |
| `reason` 为空、超 200 字符、含控制字符 | 拒绝 |
| 旧计划 `mj_clock: null` | 按旧形状读作"未声明"，在第一个请求前拒绝 |

`app_update_count` 回退到"本 run 观测值"的路径在本实现中**不存在**：源侧与冷侧都必须给出同一个固定目标。真实 `before` 各世界可以不同，
`after` 必须等于声明目标。

## 3. 单一回执的读取规则

读取器（Python `b0_normalization.receipt`/`Evidence`，与 native 回执逐键相同）要求：

1. 键集合精确；`requested` 与配方 `entries` 逐项相等（`field`/`target`/`reason` 一字不差），长度 ≥ 1、无重复字段；
2. 每个 `target` 在 `0..2147483647` 且为整数；
3. 表内 `after[i].value == requested[i].target`，`before[i].value` 等于 `before_state` 在该指针处的真实值；
4. 表外字段**逐叶子**不变（展开成 JSON Pointer 后比较；不得用摘要相等代替）；
5. `tick == 0`、同 epoch、`after_version == before_version + 1`，warm 必须是回执的**紧邻下一 revision**；
6. `before_state.rng.instances` 与最近的 `rng_seeded` 逐项相符；
7. 前置状态：`app.ui == 3`、`draw_schedule == {warm_frames:0, step_frames:0}`、计数原点已绑定、无已进入的受控引擎调用；
8. 缺失/重复/晚到/失败事件、声明序不符、归一化之后再 `rng_seed`/`rng_restore`/`clock_restore` 都使严格证据失败。

`reason` 进配方（配方 SHA-256 绑在 manifest 上，事后改写即破坏封包校验），但**不进根身份**：构造根身份前把表投影成有序
`(field, target)` 对（`b0_normalization.identity_recipe`），因此同一目标只有措辞不同不会产生两个"根"。

## 4. 配方完整性检查

```powershell
python -m llm_vs_zombies.b0_coverage experiments/runs/<world> --compare experiments/runs/<other> --report work/b0-coverage.json
```

输入是**已封存 run 的 B(0)**（`observations/initial-audit.json` 与配方、`audit/manifest.json`），全程只读、不启动游戏：

1. 按 JSON Pointer 展开口径枚举叶子 `U`（与 `tools/compare_worlds.py` 同口径：对象/数组展开到标量，空对象/空数组记为自身）；
2. 读五类模式：声明 / 记录 / 派生 / 归一化表 / 显式不覆盖，展开成同一 pointer 空间的集合 `C`；
3. 双向集合差：漏洞 `H = U\C`、悬空 `D = C\U`，任一非空即严格失败（退出码 1）；
4. 冲突检查：`normalization ∩ uncovered = ∅`；归一化字段必须命中**恰好一个整数叶子**；`derived` 条目必须给出重算/映射实现位置；
   `uncovered` 条目必须逐条对上 `audit/manifest.json.coverage.uncovered` 的字符串——写了的字段不能被洗成"不覆盖"。

报告写 `coverage_complete`、B(0) 版本、`len(U)`、五类叶子计数、`H`/`D` 样本（每类最多 50 条 + 总数，只截断显示、不截断判定）、
`normalization_projection`（有序 `(field, target)`）与 `merge_check`（§5.5 的合并判定）。

内置分类是**保守**的：只认有实际写入/恢复/重算/映射位置的字段。`docs/b0-归一化提案.md` §4.2 把整个 Board 记为"记录"，
该读法放在仓库内的 `determinism/b0-claims-proposal.json`，用 `--claims` 传入即可；两读法的差别就是"Board 里除 GameClock/EffectCounter
与波表阈值外，其余地址还没有逐字段契约"这一条事实，报告照实列出。

## 5. 边界

- 检查范围是**已捕获状态**；`coverage.complete_game_state=false` 不变，干净的报告只说明"已捕获状态内没有未分类字段"。
- 旧的 `app_update_anchor`/`mj_clock_anchor` 读取器与旧归档原样保留；一个归档里混用新旧形状即严格失败，不存在"合并解读"。
- native 旧 RPC 仍在代码里，但声明统一能力的 runtime 会拒绝它们（`legacy_anchor_retired`），一次运行只走一条路径。
- 本实现不跑真实游戏，`live_verified:false` 不变；两个世界的复跑由主 agent 另做。
