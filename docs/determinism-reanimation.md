# 动画句柄的语义关联审计

`determinism/reanimation_audit.hpp/.cpp` 比较经实际对象池验证的动画关联与动画状态。它保留原始句柄作为旁证，但**比较结果是动画身份的语义等价，不是原始 animation handle 的逐位相等**。它只读取游戏内存，不修改对象池、代次计数或动画进度；仍不宣称覆盖完整游戏状态。

## 已观察到的分叉与目标证据

本机冷启动验收 `007` 的初态仅有 12 项差异：已死亡的预览僵尸（类型 21、`mDead=1`）的 `+0x118` BodyReanimID，两个运行分别为类似 `0xD57B003B` 和 `0xD577003B` 的数值。僵尸本身代次 ID 一致，动画代次相差 `4<<16`。这些原始差异保留在私有验收数据中，不能改写为“原始状态完全相同”。

核查的是本项目锁定的 1.0.0.1051 实际引擎（SHA-256 `f9669af338964787a3785a7895791297d599295b8bb669b0db49443f736a1322`）的只读加载镜像；源码布局参考固定提交的 [Reanimator.h](https://github.com/ruslan831/PlantsVsZombies-decompilation/blob/8a2d121899ba5cb4df644cd7d2e4c1aaf88dd238/Sexy.TodLib/Reanimator.h) 与 [Reanimator.cpp](https://github.com/ruslan831/PlantsVsZombies-decompilation/blob/8a2d121899ba5cb4df644cd7d2e4c1aaf88dd238/Sexy.TodLib/Reanimator.cpp)。公开仓库只保存适配器与少量签名，不发布引擎或完整反编译文件。

| 目标位置 | 实际二进制证据与作用 |
|---|---|
| `Zombie::DieNoLoot 0x530510` | 通过动画对象池查询 `+0x118/+0x144/+0x150` 的完整代次，调用 ReanimationDie 后写 `zombie+0xEC=1`，没有将这些句柄清零。因此死亡不能直接等同于句柄已经失效。 |
| App `+0x820` → EffectSystem `+8` | ReanimationHolder/DataArray；本版本对象步长 `0xA0`、真实 ID 在 `+0x9C`。查找要求非零 ID、槽位小于容量，并且槽内实际 ID 完全相等。 |
| `0x471A71..0x471A8A` | InitializeType 把 type 左移 4，加全局定义表 `[0x6A9EE8]`，设置对象 type 后调用 Initialize。适配器检查这段精确签名；定义数量在 `0x6A9EE4`，每项 `0x10` 字节。 |
| `0x471B00` | Initialize 写 definition `+0xC`、dead `+0x14`、animRate `+8`、lastFrameTime `+0x94`；从定义读取轨道数，每轨道分配 `0x60` 字节，实例指针写 `+0x58`。 |
| `0x471920` 与 `0x471890` | 对象和轨道构造器确认标量及颜色初始化；bool 使用单字节，指针和对齐 padding 不参与规范化摘要。 |
| `0x471BC0`、`0x473B70` | Update 和 timed-event 判断使用当前/前次动画进度。进攻、锤击等进度必须保留，不以“只是动画”为由排除。 |
| `Plant::Die 0x4679B0` | `0x4679FE` 明确写 `plant+0x141=1`，随后 `0x467A05` 调用 RemoveEffects。因此 `0x141` 是死亡标志，不能用 `0x142` 替换。 |
| `Plant::Squish 0x462B80` | `0x462C2D` 写 `plant+0x142=1`，设置 `+0x4C=500`；`0x462C8F` 调用 RemoveEffects，但未写 `mDead`。适配器额外检查这两段指令的精确签名。 |
| `Plant::RemoveEffects 0x4629F0` | 核实七个动画字段 `+0x94/98/9C/A0/A8/A4/AC` 的完整代次后调用 `ReanimationDie 0x4733F0`；不清空植物保存的原始句柄。 |

动画对象的关键偏移是 type `0x00`、animTime `0x04`、animRate `0x08`、loopType `0x10`、dead `0x14`、frameStart/count/basePose `0x18/1C/20`、loopCount `0x5C`、lastFrameTime `0x94`。所有 float 保留 32 位原始值。另保留矩阵、颜色、附着标志、renderOrder/filter 和已核实的轨道混合、变换、震动、颜色及 flags 标量。

## 身份映射规则

1. 同一运行持续使用一个 `ReanimationAuditor`。它在 `Initialize` 重置一次，**不能每帧重置**。所有调用由暂停边界的游戏线程执行。
2. 对每个僵尸/植物/推车/场地物/Board fwoosh 的已知动画字段，用完整 handle 查实际池条目。Owner 的原始代次 ID 不变，并参与逻辑名称，如 `zombies/00010000/body#0`。
3. 初次遇到一个有效动画，依据稳定排序的 owner/role 建立逻辑身份。相同原始对象被多个角色引用时，保留一个节点和全部 owner，不能把共享复制成多个独立对象。
4. 在后续边界交换两个仍存活对象的角色，即使动画标量相同，也会改变关联图。相同 slot 换成新的有效 generation 时，产生新的逻辑 lifetime。观察到 ID 退役后数值再次出现，也产生新 lifetime。
5. 尚未进入已核实效果退役阶段的 owner，其代次不匹配是 `dangling`：比较状态保留原始坏 handle 与槽内实际 ID，输出 `valid=false`。受控逐帧审计先保存旁证和 fault，再停止严格实验，不会掩盖悬挂引用。
6. 只有 owner 已死亡，或**植物实际 `mSquished(+0x142)` 已置位**，且句柄实际查找失败，才规范化为 `expired`。植物被压扁时会立即清除这七个效果，植物槽本身尚未死亡并继续保留消失倒计时；不扩展为睡眠、蹦极、受伤或其他“非活动”状态。两个原始标志仍分别参与完整状态比较。
7. 已死亡或压扁的 owner 若仍指向实际有效动画，继续保留节点；动画自身 dead 标志决定其为 `retiring` 或 `live`，不会误作过期。失效查找原因、原始句柄、槽内实际 ID 和退役依据均留在旁证，不能将新代次当作旧动画复活。

映射只消除 opaque animation handle 的分配历史数值差异。它不会去除僵尸、植物等模拟实体自身的 slot/generation，也不会忽略动画类型、进度、循环或与实体的关联。

## 文件与接口

`CaptureState()` 返回带逻辑引用和 `reanimations.nodes` 的可比较状态。`ProbeTarget().coverage.reanimations` 及 manifest 的 `normalize_scope` 描述范围，`complete_animation_state=false`、`live_validated=false` 保持显式。

每个 `pre_step/post_step` 将该**同一次捕获**的原始句柄、实际槽 ID、池头（包括 free head 和 next key）、查找结果、逻辑映射写入 `audit/reanimation-handles.jsonl`。它与 `checksums.jsonl` / `state-deltas.jsonl` 使用相同 `seq`、`version`、`kind`。首条为 `initial`，后续为标准 JSON Patch `patch`；空补丁表示旁证没有变化，减少逐帧重复写盘。依序应用补丁可还原每个原始句柄和查找结果。这里记录的是池头和所有已占用槽 ID，尚不包含动画池完整空闲链。

新版 `coverage.reanimations.owner_retirement_rules` 为 `["owner_dead", "plant_squished_remove_effects"]`。`owner_dead` 始终是实际死亡标志；每个植物 link 另有必需的 `owner_squished`，其值必须与同一帧植物 `fields["00000142"]` 相符，其他实体不带此字段。只有 `expired` link 写 `retirement_reason`：死亡优先为 `owner_dead`，否则为 `plant_squished_remove_effects`。读取器必须验证这一依据，不能仅信旁证自述的 bool；旧的未声明规则证据维持 dead-only 语义，不改写旧文件。

只读 `audit_snapshot` 更新同一个 auditor 和内存中的旁证缓存，不立即写磁盘；之后的受控 pre/post 会重新捕获并写入与其准确对应的旁证。新运行的初始状态必须在 B(0) 起首次捕获，不能先在另一段模拟历史采样再假定身份映射仍是全新。

```cpp
lvz::determinism::ReanimationAuditor auditor;
auditor.Reset();
auto snapshot = auditor.Capture(raw_owner_state);
// snapshot.comparable：参与状态比较；snapshot.raw：独立保存。
// snapshot.valid：所有覆盖的活对象引用是否通过实际池验证。
```

## 覆盖缺口与验证

尚未覆盖：未被这些 owner 引用的动画语义、轨道 AttachmentID 对应的效果图、image/font/text/override 资源身份、粒子与 trail 池、完全发生于两次采样之间的分配生命周期。对象 definition 指针通过“type → 固定定义表”关系验证，但不对整个资源定义内容做逐帧哈希；实验仍须锁定资源文件。瞬时分配又释放、有限 generation 恰好在一次采样间绕回等情况不能由边界采样证明不存在。不得把当前语义比较提升为完整进程确定性证明。

`tests/determinism_reanimation.cpp` 验证不同原始代次的语义等价和旁证差异、死亡失效/仍有效的区分、坏 generation 拒绝、交换与共享关系、同 slot 新 generation 的 lifecycle、已观察到的 ID 退役与复用、浮点末位/前次进度/循环变化、定义不匹配和格式拒绝。植物退役测试还覆盖七角色、真实死亡与压扁标志分别保存、压扁但仍有效时保留动画、压扁标志变化进入摘要、普通活植物坏代次继续失败，以及睡眠/蹦极/其他实体不能套用压扁规则。这些使用独立内存模型，证明规范化规则，**不替代原版冷启动验收**。

真实隐藏引擎长局 `017` 在 tick 4166 暴露了先前 dead-only 规则的缺口。最后两个完整 post 状态（4164、4165）中，植物槽 65 是 5 路 7 列的小喷菇（type 8、实体 ID `0xDC6D0041`），HP 244，dead/squished 均为 0；动画 `0xD6F20032` 在槽 50 查找有效。同路红眼槽 45 的锤击 phase 为 70，4165 的动画前次/当前进度为约 `0.6399995/0.6424237`。原版 `0x526D57` 按 `0.64` 定时事件判断锤击、`0x526DAB` 调用 SquishAllInSquare；下一 tick 的旁证记录槽 50 已释放、原始植物句柄未清零、实际 mDead 仍为 false。

017 在报错前只写出了 4166 的原始动画旁证，没有写出该失败边界的完整状态；因此“该次 mSquished 已置位”是结合相邻帧、锤击阈值和已核实机器码的推断，不能伪装成直接采样值。旧失败记录保持封存且不改判成功。

修复后的真实隐藏引擎长局 `020` 已直接观察到这个生命周期。私有提取报告 `work/squish-live-020.json` 核验了至 tick 4500 的 9000 个 pre/post 边界的状态摘要及原始动画关联。在 tick 4166 的 post 状态，植物槽 65（同一实体 ID `0xDC6D0041`、type 8、5 路 7 列）实际 `+0x141=0`、`+0x142=1`、消失倒计时 `+0x4C=500`；body 原始句柄 `0xD6EF000C` 对应槽 12 已释放，旁证为 `owner_dead=false`、`owner_squished=true`、`lookup_failure=not_allocated`、`retirement_reason=plant_squished_remove_effects`，规范引用为 `expired`。该运行成功跨过了此前报错的阶段。

020 的这份验证仅覆盖已读取的真实运行前缀，尚不是封存日志的完整校验，也不是冷启动重放的逐帧等价证明。017 与 020 的原始动画 ID、部分其他状态不同；上述结果确认退役规则与实际字段吻合，不声称两局原始状态相同或整体游戏确定性已获证明。
