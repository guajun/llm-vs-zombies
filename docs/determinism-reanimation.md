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

动画对象的关键偏移是 type `0x00`、animTime `0x04`、animRate `0x08`、loopType `0x10`、dead `0x14`、frameStart/count/basePose `0x18/1C/20`、loopCount `0x5C`、lastFrameTime `0x94`。所有 float 保留 32 位原始值。另保留矩阵、颜色、附着标志、renderOrder/filter 和已核实的轨道混合、变换、震动、颜色及 flags 标量。

## 身份映射规则

1. 同一运行持续使用一个 `ReanimationAuditor`。它在 `Initialize` 重置一次，**不能每帧重置**。所有调用由暂停边界的游戏线程执行。
2. 对每个僵尸/植物/推车/场地物/Board fwoosh 的已知动画字段，用完整 handle 查实际池条目。Owner 的原始代次 ID 不变，并参与逻辑名称，如 `zombies/00010000/body#0`。
3. 初次遇到一个有效动画，依据稳定排序的 owner/role 建立逻辑身份。相同原始对象被多个角色引用时，保留一个节点和全部 owner，不能把共享复制成多个独立对象。
4. 在后续边界交换两个仍存活对象的角色，即使动画标量相同，也会改变关联图。相同 slot 换成新的有效 generation 时，产生新的逻辑 lifetime。观察到 ID 退役后数值再次出现，也产生新 lifetime。
5. 活 owner 的代次不匹配是 `dangling`：比较状态保留原始坏 handle 与槽内实际 ID，输出 `valid=false`。受控逐帧审计先保存旁证和 fault，再停止严格实验，不会掩盖悬挂引用。
6. 只有 owner 已死亡且句柄实际查找失败，才规范化为 `expired`，查找失败原因和两个 ID 仍保留在旁证。死亡 owner 仍指向有效动画时，会保留节点；动画自身 dead 标志决定其为 `retiring` 或 `live`，不会误作过期。

映射只消除 opaque animation handle 的分配历史数值差异。它不会去除僵尸、植物等模拟实体自身的 slot/generation，也不会忽略动画类型、进度、循环或与实体的关联。

## 文件与接口

`CaptureState()` 返回带逻辑引用和 `reanimations.nodes` 的可比较状态。`ProbeTarget().coverage.reanimations` 及 manifest 的 `normalize_scope` 描述范围，`complete_animation_state=false`、`live_validated=false` 保持显式。

每个 `pre_step/post_step` 将该**同一次捕获**的原始句柄、实际槽 ID、池头（包括 free head 和 next key）、查找结果、逻辑映射写入 `audit/reanimation-handles.jsonl`。它与 `checksums.jsonl` / `state-deltas.jsonl` 使用相同 `seq`、`version`、`kind`。首条为 `initial`，后续为标准 JSON Patch `patch`；空补丁表示旁证没有变化，减少逐帧重复写盘。依序应用补丁可还原每个原始句柄和查找结果。这里记录的是池头和所有已占用槽 ID，尚不包含动画池完整空闲链。

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

`tests/determinism_reanimation.cpp` 验证不同原始代次的语义等价和旁证差异、死亡失效/仍有效的区分、坏 generation 拒绝、交换与共享关系、同 slot 新 generation 的 lifecycle、已观察到的 ID 退役与复用、浮点末位/前次进度/循环变化、定义不匹配和格式拒绝。这些使用独立内存模型，证明规范化规则，**不替代原版冷启动验收**。真实引擎应检查关联全部有效、原始差异可追溯，再对每一帧语义状态和攻击结果比较；当前适配完成后仍等待这轮验收。
