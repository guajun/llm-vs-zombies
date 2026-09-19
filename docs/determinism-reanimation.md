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
| `Zombie::DropFlag 0x529870` | `0x529873/0x52987F` 限定 type 1 与实际 `mHasObject(+0xBC)`；`0x52988E..0x5298B9` 读取 `+0x144`，核完整代次后调用 `ReanimationDie`；`0x5298F4` 只清 `+0xBC`，不清旧动画句柄、不设置死亡。适配器核验这三段精确字节。 |
| `Plant::RemoveEffects 0x4629F0` | 核实七个动画字段 `+0x94/98/9C/A0/A8/A4/AC` 的完整代次后调用 `ReanimationDie 0x4733F0`；不清空植物保存的原始句柄。 |

动画对象的关键偏移是 type `0x00`、animTime `0x04`、animRate `0x08`、loopType `0x10`、dead `0x14`、frameStart/count/basePose `0x18/1C/20`、loopCount `0x5C`、lastFrameTime `0x94`。所有 float 保留 32 位原始值。另保留矩阵、颜色、附着标志、renderOrder/filter 和已核实的轨道混合、变换、震动、颜色及 flags 标量。

## 身份映射规则

1. 同一运行持续使用一个 `ReanimationAuditor`。它在 `Initialize` 重置一次，**不能每帧重置**。所有调用由暂停边界的游戏线程执行。
2. 对每个僵尸/植物/推车/场地物/Board fwoosh 的已知动画字段，用完整 handle 查实际池条目。Owner 的原始代次 ID 不变，并参与逻辑名称，如 `zombies/00010000/body#0`。
3. 初次遇到一个有效动画，依据稳定排序的 owner/role 建立逻辑身份。相同原始对象被多个角色引用时，保留一个节点和全部 owner，不能把共享复制成多个独立对象。
4. 在后续边界交换两个仍存活对象的角色，即使动画标量相同，也会改变关联图。相同 slot 换成新的有效 generation 时，产生新的逻辑 lifetime。观察到 ID 退役后数值再次出现，也产生新 lifetime。
5. 尚未进入已核实效果退役阶段的 owner，其代次不匹配是 `dangling`：比较状态保留原始坏 handle 与槽内实际 ID，输出 `valid=false`。受控逐帧审计先保存旁证和 fault，再停止严格实验，不会掩盖悬挂引用。
6. owner 已死亡，或**植物实际 `mSquished(+0x142)` 已置位**，且句柄实际查找失败，可规范化为 `expired`。植物被压扁时会立即清除这七个效果，植物槽本身尚未死亡并继续保留消失倒计时；不扩展为睡眠、蹦极、受伤或其他“非活动”状态。两个原始标志仍分别参与完整状态比较。
7. 新声明的 `zombie_flag_dropped` 规则只适用于**僵尸 type=1、角色 `+0x144`、实际 `mHasObject(+0xBC)=0`**。它允许有非零代次、位于已使用池范围的旧旗动画句柄在释放或槽重用后成为 `expired`。其他类型、其他动画角色、仍持旗、零代次、范围外/从未使用的槽继续拒绝；不要求 HP70 或已经掉头，因为原版被割草也能调用 DropFlag。原始 owner 死亡字段不改为真。
8. 已死亡、压扁或已掉旗的 owner 若仍指向实际有效动画，继续保留节点；动画自身 dead 标志决定其为 `retiring` 或 `live`，不会误作过期。失效查找原因、原始句柄、槽内实际 ID 和退役依据均留在旁证，不能将新代次当作旧动画复活。

映射只消除 opaque animation handle 的分配历史数值差异。它不会去除僵尸、植物等模拟实体自身的 slot/generation，也不会忽略动画类型、进度、循环或与实体的关联。

## 文件与接口

`CaptureState()` 返回带逻辑引用和 `reanimations.nodes` 的可比较状态。`ProbeTarget().coverage.reanimations` 及 manifest 的 `normalize_scope` 描述范围，`complete_animation_state=false`、`live_validated=false` 保持显式。

每个 `pre_step/post_step` 将该**同一次捕获**的原始句柄、实际槽 ID、池头（包括 free head 和 next key）、查找结果、逻辑映射写入 `audit/reanimation-handles.jsonl`。它与 `checksums.jsonl` / `state-deltas.jsonl` 使用相同 `seq`、`version`、`kind`。首条为 `initial`，后续为标准 JSON Patch `patch`；空补丁表示旁证没有变化，减少逐帧重复写盘。依序应用补丁可还原每个原始句柄和查找结果。这里记录的是池头和所有已占用槽 ID，尚不包含动画池完整空闲链。

新版 `coverage.reanimations.owner_retirement_rules` 为 `["owner_dead", "plant_squished_remove_effects", "zombie_flag_dropped"]`。`owner_dead` 始终是实际死亡标志；每个植物 link 另有必需的 `owner_squished`，其值必须与同一帧植物 `fields["00000142"]` 相符，其他实体不带此字段。只有 `expired` link 写 `retirement_reason`：死亡优先为 `owner_dead`，否则按实际条件为 `plant_squished_remove_effects` 或 `zombie_flag_dropped`。每个新规则下的僵尸 `+0x144` link 必须包含 `owner_zombie_type` 和 `owner_has_object`，Python 读取器逐项核对同一 captured frame 的 `+0x24/+0xBC`，再独立检查原始 generation、实际槽与查找失败原因；不能只信退休标签。旧的两条规则声明仍只有死亡/压扁语义，未声明规则的旧证据维持 dead-only 语义，不改写旧文件。

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

## 045 旗僵尸退役审计缺口

045 在 wave20、受控 tick55821 以 `audit_failed` 停止，UI3，未赢。封存 manifest SHA-256 为 `3def3a2a002993d841352989227e5ac18785bb455612b4188c229000063743c9`。fault seq9248038 完整 captured state 直接记录：僵尸槽38、实体 ID `3832741926`、type1、HP70、`mDead=0`、`mHasHead=0`、`mHasObject=0`，body 动画仍 live；`+0x144` 原 handle `3893952519`（`0xE8190007`）已不在动画池。

该 handle 首次于 B54011 出现。最后有效 post 和下一 pre（B55820）中 HP90、`mHasHead=1/mHasObject=1`，旗动画 type142（`0x8E`）仍 live。下一次原更新后 HP70、两个标志为0，池槽7释放；原版 DropHead→DropFlag→ProcessDeleteQueue 的机器码与此直接证据吻合。`0x529C26` 调 DropFlag，`0x4733FF` 置动画 dead，原更新中的 `0x445680` 删除队列按 dead 回收对象。这里修正的是只依据 owner 死亡判断退休的审计模型，没有改游戏内存或 RNG。

旧 045 fault/不完整 post/未赢结论保持原样。新增 C++ 与 Python 夹具覆盖这一直接观察的生命周期及严格反例；独立测试 DLL 可用于真实原版 DropHead/回收链验收，它与生产 DLL 身份不同，不能当成公平策略或完整两旗成功证据。修复通过离线测试不代表已完成新的原版长局和冷重放。

## 独立的真实生命周期测试 DLL

`recorder_flag_fixture` 是 `EXCLUDE_FROM_ALL` 测试 target；普通构建生成的 `build/recorder.dll` 没有测试触发逻辑。显式构建命令为：

```powershell
cmake --build build/cmake --target recorder_flag_fixture --parallel 2
```

它生成不同文件 `build/recorder_flag_fixture.dll`，不能覆盖生产产物。测试使用独立、全新 fixture root，把该文件复制为该 root 的 `build/recorder.dll`，固定两者 SHA-256，并按真实 open-source 文件准备源码归档；源码目录不能用 junction 绕过 `create_run` 的 linked-source 检查。私有游戏与工具链可使用只读资源路径。已有 run、冻结 DLL 和旧失败档不参与修改。

测试 DLL 的 hello `game.test_fixture` 与 native manifest `test_fixture` 均声明 `mode=test_flag_drop_v1`、`fair_strategy_evidence=false`、`production_readiness_allowed=false`。公开 `evaluation.run_suite` 在 source/cold/recovery 中拒绝出现该字段的 runtime；底层 `live_session` 和正式重放器仍可被专用测试驱动使用。同 fixture 的身份和 DLL 哈希必须一致，不能拿它与生产 DLL 混作同一实验。

只在真实 `EngineCallTracker` 已 Enter、尚未 Returned 的游戏线程 original-update callback 执行：call1 用 AvZ 的原 `PutZombie(2,8,type1)` 创建旗僵尸，复核池槽、完整代次、数量、类型、行和 type142 旗动画；call2 对同一个完整 ID 调用原 `Zombie::DropHead(0)`（锁定 ABI 为两个 stack 参数、stdcall ret8），随即运行原 `GameTotalLoop`；call3 再核验同实体。启动菜单 callback 没有 active call，不能触发夹具。所有步骤继续使用原控制器的 pre/update/draw/post 顺序，没有手写 HP、旗帜标志、动画池或 RNG。

`decisions/flag-drop-fixture.jsonl` 独立保留创建、DropHead 前、DropHead 后但更新前、每次原更新后的实际字段，以及关闭健康；它不会调用 native Audit 改写 spawn/particle 的控制边界。每行的 `version` 是该真实调用的 **pre 版本**，`engine_call_id` 为实际调用 ID，`game_clock` 为采样时原时钟，不能把更新后 sidecar 的 pre 标签当作 post 版本。原始 owner/Board 地址、完整 owner ID、旗与 body 的原 handle/实际池 ID、BA/BC/EC/HP 都作为同运行旁证保留。写盘失败保留 lock 并拒绝成功关闭；中途语义故障的 `closed.healthy` 为 false。

该探针应直接证明：BC1/live旗 → 原 DropHead 后 BC0/retiring旗 → 原 DeleteQueue 后旧 handle 不变而查找失败，body 和 owner 仍 live；三步源录制与相同 fixture 的冷重放还须分别通过正式 reader/关闭/归档/窗口/自有进程清理。它只验这一生命周期，不证明策略获胜、两旗完成或实验全面就绪。
