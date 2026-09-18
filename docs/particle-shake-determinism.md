# 粒子抖动的确定性运行模式

`deterministic_particle_shake_v1` 修复原版 `FIELD_SHAKE` 使用堆地址播种 CRT 的不确定性。它会改变粒子的抖动序列及该调用后的 CRT 状态，manifest 明确标记 `original_engine_bitwise_unmodified=false`。它保留原版的两次播种、四次随机调用，并继续记录和比较由新种子产生的完整 CRT 状态；不能称为未修改原版的逐位重放，也未证明后续共享该 CRT 的其他调用完全不受影响。

## 首次冷重放分叉证据

`eval-headless-012-s42-c0` 与 `c1` 首个不同的边界为 epoch 3、tick 605、post_step。唯一不同字段是 `/rng/instances/game_thread_crt/state`：源运行 `3668862914`，重放 `1119315906`；全局 MT、植物、僵尸、动画及其他已审计字段均一致。

tick 604 两者 CRT 都从 42 变成 `505908858`，正好等于 `srand(0)` 后两次 MSVC LCG 更新。把 tick 605 的 CRT 状态逆推两步，得到种子 `0x0ca31108` 和 `0x0ca41108`；后续三个 tick 恰好分别是这两个数的 2、3、4 倍。这和两进程粒子地址相差 `0x10000` 完全吻合。

若误把重播种当作连续 rand，tick 604→605 的 LCG 距离分别为 6,875,208 与 3,380,734,024，不能解释为少执行几次 rand。函数和机器码进一步确认原因是地址播种，发生在 update 内。

## 锁定目标与布局

目标引擎 SHA256：`f9669af338964787a3785a7895791297d599295b8bb669b0db49443f736a1322`。

固定参考源码来自 [ruslan831/PlantsVsZombies-decompilation](https://github.com/ruslan831/PlantsVsZombies-decompilation/tree/8a2d121899ba5cb4df644cd7d2e4c1aaf88dd238)，使用 `Sexy.TodLib/TodParticle.cpp/.h` 与 `EffectSystem.h`，并逐项核对本地解包引擎的反汇编。源码本身不代替机器码验证。

| 地址/布局 | 确认含义 |
|---|---|
| `0x516820` | `TodParticleEmitter::UpdateParticleField`，ESI 为粒子，EAX 为 field index，ECX 为 field definition |
| `0x516f93` | `UpdateParticle(0x516f00)` 对上述方法的唯一直接调用 |
| `0x516b3c` | `srand(previous_age_factor * particle_address)`，原字节 `e8 39 75 10 00` |
| `0x516ba5` | `srand(age * particle_address)`，原字节 `e8 d0 74 10 00` |
| `0x61e07a` | 原版 CRT srand，原字节 `e8 be a9 00 00 8b 4c 24 04 89 48 14 c3` |
| 粒子 `+0,+4,+8,+0x20,+0x24,+0x38` | emitter、duration、age、position x/y、crossfade duration |
| `particle→emitter+4→system+8` | 粒子 holder；必须等于 `LawnApp+0x820→EffectSystem+0` |
| holder `+0x38` | 粒子 DataArray；容量上限 1024 |
| DataArray 元素 | 步长 `0xa0`，完整 generation/slot ID 位于 `+0x9c` |
| `0x518cb0`、`0x518d30` | 粒子分配与 ID 查询，确认上述步长、ID 偏移和代次检查 |

首个调用的因子为 `age == 0 ? duration - 1 : age - 1`，第二个为 `age`。新种子为 `uint32_t(factor * full_particle_id)`。不删除 generation，不把同槽位的不同生命周期视为相同粒子。原始种子必须等于 `uint32_t(factor * particle_address)`，否则拒绝转换并使审计失败。

通常 `age < duration`。但 `UpdateParticle` 在 `0x517039` 启动 CrossFade 成功后仍会继续执行当前帧，age 尚未被限制；此时只在 `crossfade_duration > 0` 的情况下接受达到或超过 duration 的 age。每次记录该标量，公式仍完全按原函数计算。

## Hook 与失败语义

只改写两个已签名验证的五字节 CALL；CRT srand 函数及全部 rand 调用不改写。包装器保存 GPR、EFLAGS、完整 x87/XMM/MXCSR 和 Win32 LastError，在固定容量队列中记录证据，然后恢复执行环境并尾调用原版 srand。包装器不分配内存、不输出 JSON、不读其他线程的游戏对象。

验证当前线程、真实 holder 归属、pool header、对象对齐和范围、ID 高位代次/低位槽位、age/duration/crossfade，以及原始地址种子。失败时保留原始调用参数，递增 sticky fault；下一审计边界立即报告失败并停止控制推进。8192 条队列溢出同样停止实验，不丢弃记录后冒充成功。卸载先检查两个位置的所有权，避免覆盖其他模块的修改。

## 记录合同

manifest 的 `particle_shake` 对象包含模式、installed、语义变化说明、`raw_evidence: "particle-shake-seeds.jsonl"` 及实机验收状态。

每次成功转换输出 `events.jsonl` 中的 `particle_shake_seed`，带 seq、version、`native_phase=before_srand`。payload 包含全局 ordinal、callsite RVA、完整 particle ID、slot、generation、age、duration、crossfade_duration、factor、canonical_seed、pool 标量、pool_verified 和 control_phase。初始化调用 version 为 null；受控调用使用对应 pre_step/action/request_started 的版本。

`particle-shake-seeds.jsonl` 使用 `lvz.particle-shake-raw.v1`，与语义事件共享 seq/version，payload 是其超集，额外保存原地址、关联对象地址、pool block 和 original_seed。地址只作为证据；跨运行比较实际 seed 输入因子、完整 ID、转换种子和调用序列。

每次完整 CaptureState 额外包含 `particle_shake.mode/controlled_calls/controlled_digest`。摘要为 FNV-1a 64，初值 `14695981039346656037`；每个受控调用按下列顺序将各值转换为 uint64，逐项追加 8 字节小端表示：callsite_rva、particle_id、factor、canonical_seed、uint32(age)、uint32(duration)、uint32(crossfade_duration)、pool.used、capacity、free_head、count、next_key。版本信息由事件映射校验，避免把会重映射的 epoch/revision 混入摘要。

关闭顺序为已有 recording_closed、particle_shake_closed、spawn_hook_closed。captured 必须等于所有 seed 事件数，controlled_calls 必须等于受控事件数，queued/faults/overflow/wrong_thread_calls 必须为零。原始完整 CRT 状态仍由既有 rng 组件采集和比较。

## 验证范围

独立 32 位 fixture 覆盖实际 CALL 包装器的寄存器/FX/LastError 保持、调用原函数、相同 ID 不同地址、相同槽位不同 generation、age=0 的 duration 回绕、合法首次 crossfade、错误 ID/holder/原种子、错误线程、队列溢出及 hook 所有权。

真实实验 016→019 已通过一次独立冷启动的 1,000 tick 比较：2,000 个前后边界语义状态、40,946 次转换调用相同，关闭健康通过。更长轨迹和十次冷启动仍需独立验收。若完整粒子 ID 本身在两次运行间不一致，比较应失败，不能无证据地去掉代次。此模式没有宣称覆盖所有粒子/附件状态，也不通过事后恢复 CRT 隐藏差异。
