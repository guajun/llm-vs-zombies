# 原版动作重放与首处分叉诊断

本实现对应 [issue #6](https://github.com/guajun/llm-vs-zombies/issues/6)。`engine_replay.py` 通过现有 `Client.request()` 调用与 REPL 相同的原生执行器，不再次调用 LLM。`audit_compare.py` 校验原生审计并从 JSON Patch 展开每帧状态。离线测试、synthetic demo 只能证明调度与校验逻辑，不能证明 PvZ 完整确定性。

## 录制边界

启动器先创建新的私有运行目录、启动自己的隐藏窗口游戏进程、加载指定阵型，并应用明确的初始化配方。所有 `rng_seed`、`rng_restore`、`clock_restore` 和加载场景操作必须在初态标记之前完成。只有实际加载后的 `observe` 和 `audit_snapshot` 才能作为 B(0)；存档文件的存在、IPC 返回 accepted 或种子数值相同都不足以代替这个检查。

```python
from llm_vs_zombies.engine_replay import capture_initial, identity_from_launcher

# client 是已连接、已初始化并暂停在 B(0) 的 Client。
# launcher_state 来自这个实际进程的 launcher.start()，不能从目标轨迹复制。
identity = identity_from_launcher(client.hello(), launcher_state)
marker = capture_initial(
    client,
    identity=identity,
    initialization={
        "scenario": "liangyi",
        "game_mode": 13,
        "cards": [16, 30, 14, 63, 15, 2, 20, 17, 8, 27],
        # 记录实际执行的 seed/clock 初始配置；不要填写尚未执行的配置。
    },
)

client.commit([{"op": "plant", "type": 8, "row": 2, "col": 5}], advance_ticks=1)
client.advance(100)

# 若末帧发生终局转场，Controller 已可能进入下一 epoch，先更新版本。
client.observe()
client.request("stop_recording", expect=client.version)
# 再关闭 Client 与 SessionTrace，并终止启动器拥有的游戏进程。
```

`capture_initial()` 在 SessionTrace 写入 `replay_initial`，内容包括实际完整审计快照、观察、初始化配方和身份。身份覆盖 `hello.build`、`hello.game`、启动器校验的游戏/场景、DLL、辅助程序、用户档 SHA-256；启动器提供资源文件哈希时也纳入。PID、目录和新进程的 epoch 不要求复用。

目前源轨迹要求顺序请求，不接受不明结果、并发取消、丢失响应或未声明的中途变更。失败的游戏动作属于合法轨迹：保存原始动作列表、实际尝试过的每个 ordinal、每次成功/失败及停止原因。首个动作失败后的后续动作仍留在原请求中，但不会伪造其执行结果。

完成正数 ticks 的推进之后，可以记录一次参数为空的 `pause` 控制探针。它必须返回 `paused_at_boundary`，观察与版本逐项等于刚完成的推进，且在任何后续变更之前记录同版本 `audit_snapshot`；该快照须逐字节等于原生末 post-step。封包保留原始 pause，请求没有 expect 的旧 v1 格式仍可读。重放先调用 status 证明当前已暂停、没有 pending 请求或故障，再真实执行 pause，并比较实际前后 status、完整状态快照及源记录。取消正在推进的 pause、缺快照、边界改变或隐藏状态变化都拒绝。报告 `pause_controls` 分别计数 recorded/executed/verified_noop；seek 到目标 tick 后尚未发生的 pause 不提前执行。

## 封装与检查

```powershell
$env:PYTHONPATH = 'src'
python -m llm_vs_zombies.engine_replay pack `
  experiments/runs/source/decisions/session.jsonl `
  experiments/runs/source/audit `
  experiments/runs/source/exports/trajectory

python -m llm_vs_zombies.engine_replay inspect `
  experiments/runs/source/exports/trajectory
```

输出目录必须是新目录。封装结果包含 `trajectory.json`、原始 SessionTrace、原生 `audit/` 基础四个文件，以及 manifest 声明或实际存在的动画/粒子旁证。所有证据文件以 SHA-256 绑定，轨迹身份是清单内容的 SHA-256；这是内容完整性检查，不是身份签名或第三方真实性认证。重放时重新检查文件、schema、目标、源请求/响应与原生权威事件的一致性，不只信任已经生成的动作清单。

若只有原生权威事件，可以使用：

```powershell
python -m llm_vs_zombies.engine_replay pack-native `
  initial.json experiments/runs/source/audit output-native-trajectory
```

`initial.json` 必须是 `capture_initial()` 返回的真实 marker，不能从第一帧动作之后的状态倒推。两种封装方式都要求原生日志已 `recording_closed`，并拒绝任何无法证实的执行。

## 冷启动与执行

API 为：

```python
replay(trajectory, initializer, output_directory,
       target_tick=None, on_takeover=None)
```

`initializer(trajectory, output_directory)` 是 context manager，负责创建一个新的原版进程、应用 `trajectory.initial["initialization"]` 的实际初始化配方并验证场景，最后 yield：

```python
ReplaySession(
    client=actual_client,
    identity=identity_from_launcher(actual_client.hello(), actual_launcher_state),
    audit_directory=actual_run / "audit",
)
```

context manager 同时负责结束记录、关闭连接和调用 `launcher.stop(actual_run)`。不能直接以源轨迹中的 identity 作为实际进程 identity。initializer 只完成初始化，不再调用 `capture_initial()`：replayer 在核验实际初态后会写入带父轨迹身份的 actual `replay_initial`，保证本次实际轨迹和之后分支也可独立封装。

replayer 核对 capability、身份、B(0) 观察和完整审计快照。仅映射 source epoch → actual epoch，并显式记录初始化产生的 revision 差额；GameClock、RNG、对象代次 ID、卡槽和其他游戏字段一律参与比较。每条请求得到新的 request_id，原 ID 到实际 ID 的映射进入报告。

每个请求实际完成后，先保存真实返回值，再核对动作结果、请求/执行帧数、停止原因、观察与每个 `pre_step/post_step`。遇到差异立即停止，不覆盖游戏状态、不借用源响应填补实际响应。输出 `replay-report.json` 保留首次差异路径和已经得到的实际证据。

可通过命令行加载自己显式指定的初始化适配函数：

```powershell
python -m llm_vs_zombies.engine_replay run trajectory-dir replay-output `
  --initializer my_experiment:cold_start
```

这里 `my_experiment:cold_start` 是使用者自己的 Python 函数入口，签名如上；本命令不会自动下载游戏或猜测初始化配置。项目实验运行器可以直接组合 launcher 与 replay API。

## 捕获原画面也是要重现的操作

`capture_frame` 会在源 SessionTrace 的原位置保留为独立干预，并在重放中实际调用同一接口。不会因为 tick 没有增加就跳过一次强制绘制。成功帧、协议成功但 `capture_ok:false` 的捕获失败、以及有明确错误响应的 `RemoteError` 都能保留；超时、断连或无法判断是否完成的操作仍拒绝打包/继续重放。

比较内容包括 `capture_ok`、失败原因、尺寸/格式/方法、`forced_render`、`used_3d`、已知 RNG 检查、前后 GameClock 和真实 controller version。失败响应没有 version 时，源记录必须在下一次干预前实际调用 `observe()` 或 `audit_snapshot` 取得边界；不能从预期 revision 推算。重放也读取实际观察。源有捕获后的完整审计快照时，会逐字段比较该状态；其余情况继续由后续模拟帧审计检查状态，不把捕获元数据冒充完整状态证明。

轻量记录在 capture 的 response 中保存 `pixels_evidence:{sha256,byte_length}` 并标记 `trace_metadata_only:true`，不用把每帧约 1.92 MB Base64 反复写进 SessionTrace。实际像素流直接交给视频编码器；旧版含 `pixels_base64` 的记录仍可读。轨迹和重放报告保留各自图像摘要及长度，比较长度和格式，**不要求像素哈希一致，也不以像素一致代替游戏状态一致**。

`replay-report.json` 的 `capture_interventions` 会分别报告 recorded、attempted、executed、compared、expected_in_scope、reproduced 和 unknown_outcome。完整重放的 scope 是 entire_trajectory；seek 的 scope 是 executed_prefix，到达目标边界后尚未发生的同 tick 捕获不会提前执行。明确响应失败属于已执行的接口尝试；unknown_outcome 不会标为已确认执行或已重现。

原生 `audit/` 当前不写捕获事件和像素，捕获顺序的证据来源是 SessionTrace。`pack-native` 没有该信息，因此报告 capture 来源为 `unknown_native_only`，recorded/reproduced 为 null；不能据此宣称原录制没有发生任何绘制干预。包含原画面捕获的正式实验应保留并封装 SessionTrace。

## Seek 与接管

`target_tick=500` 表示从 B(0) 重新执行到首次到达 B(500)。此前所有请求和失败动作均执行；若目标落在一条推进请求中，只缩短该请求的推进预算，保留其动作，并与源日志中的实际 B(500) 比较。这个模式不恢复中途快照。

到达 B(500) 后尚未执行源轨迹随后发生的同 tick 动作；`target_tick=0` 则不执行初始 tick 的动作。未指定 target 时完整执行源轨迹，包括最终 tick 的零推进动作。完整重放与按 tick seek 的语义因此是明确区分的。

```python
def take_over(session, parent):
    # 此时进程仍存活，parent 含父 trajectory_id、tick、已消费请求数、
    # actual_version 和显式 epoch 映射。
    session.client.commit([{"op": "shovel", "row": 2, "col": 5, "target_type": -1}])

replay(trajectory, cold_start, output, target_tick=500, on_takeover=take_over)
```

接管回调在 initializer 的上下文内运行。它的实际操作继续写同一 SessionTrace；本次 trace 从实际 B(0) 开始，包含重算前缀、`replay_takeover` 父分支身份与后续操作，可封装为独立完整分支。没有回调时正常结束并执行 initializer 的清理。

## 两旗结束与终局转场

最后一条请求可以返回 `scene_changed`，但必须有明确的原生证据：

1. 同一个 Board 上的最后一次更新产生真实 `native_tick_delta=1`；
2. 记录顺序是 `post_step → terminal_transition → request_completed`；
3. `terminal_transition` 包含同 request_id、`tick_delta_verified=true`、`board_identity_preserved=true`，保留旧 epoch 的末 tick；
4. 转场是轨迹末请求，实际 observation 已离开战斗 UI。

replayer 比较末帧状态及转场字段，然后刷新观察以获取 Controller 更新后的 epoch。报告分别保留测量终点 `reached_version` 和清理时可用的 `current_runtime_version`。旧 Board 被销毁、计数无法确定、缺少最后 `post_step` 的情况均拒绝严格重放。终局不能直接接管战斗操作；接管应 seek 到较早的稳定战斗边界，下一局需要新的初始化段。

支持终局审计不等于自动判定已完成两旗。实验运行器仍需证明场景、实际观察到第 20 波及完成轮数增长，并执行多次冷启动和暂停扰动验收。

## 单步/批量与录像干预的独立验收

`tools/check-boundary-equivalence.py` 只读取两份已封包的真实记录，不启动游戏。两组都应从独立冷启动开始，用同一 DLL、游戏/资源/存档散列、seed、初始化配方与 clock anchor；先验证初态相同，再执行确定的测试输入。组间的运行时间、请求 ID 和推进预算无需相同。

单步/批量组关闭 capture，左组连续执行 100 次 `advance(1)`，右组只执行一次 `advance(100)`，两者均关闭录制并封包。工具要求恰好满足此安排，且没有动作或 capture 混入：

```powershell
python tools/check-boundary-equivalence.py work/single-trajectory work/batch-trajectory --purpose batch --ticks 100 --output work/batch-acceptance.json
```

录像干预组使用相同动作时序与 seed/初始化配方，左组关闭 capture，右组按实际录像节奏调用成功的 `capture_frame`。右组所有捕获都必须实际 forced_render、报告 known RNG 未变且前后 GameClock 一致。每次捕获后必须至少有一个受审计的推进帧；在最后一帧才捕获而没有后续审计，不能证明动画等隐藏状态未变，工具会拒绝。视频是否写入 FFmpeg 不影响这里的游戏状态判据，但正式录像实验仍应保留视频和 frame mapping。

```powershell
python tools/check-boundary-equivalence.py work/capture-off-trajectory work/capture-on-trajectory --purpose render --ticks 100 --output work/render-acceptance.json
```

两种比较都锁步遍历每个 tick 的 pre/post 完整规范化状态，包含已覆盖 RNG、动画时间/速率/track 等字段，而非只比较末态。强制绘制可能改变这些字段，即使 RNG/Clock 保护通过，也会在首个真实分叉处失败。动作尝试按实际 tick 和顺序比较；受控出生按 tick 和出现顺序比较初始化原始属性、初始位置、随机属性和 RNG 前后态。出生中的四个动画原句柄仅在随后 post 边界的原始旁证证明其角色/句柄映射时转换为稳定节点引用；调用者在初始化后替换句柄、缺少对应旁证或无法证明映射时直接失败，不删除字段来制造相等。

报告明确写出 `spawn_exercised`。100 ticks 内没有出生只证明该区间的状态等价，不能当作出生路径验收；另加覆盖实际出生的较长录像对照，或把相同的初始化配方设置在即将出生的稳定边界后再测试。初始 marker 之前的初始化出生历史不在此区间比较范围，初态本身仍严格比较。工具通过不等于未采集字段也确定，`original_engine_replay_verified` 始终保持 false。

## 审计、边界与验证

真实冷重放曾在粒子 FIELD_SHAKE 的 CRT 随机状态上分叉：原版两处 `srand` 用进程内的粒子地址乘年龄相关因子，独立进程的地址不同会改变之后的随机数。`deterministic_particle_shake_v1` 是明确改变该行为的实验模式：只在 RVA `0x116b3c`、`0x116ba5` 两处把地址因子换成经 DataArray 槽位/代次验证的完整粒子 ID，原来的 CRT srand/rand 仍执行。**这不是原版逐位不变的执行模式。** manifest 必须声明 `particle_shake.mode`、`installed:true`、`original_engine_bitwise_unmodified:false` 及必需的 `particle-shake-seeds.jsonl`；未知模式、缺证据或用旧模式冒充均拒绝。

每次调用的语义事件保存完整粒子 ID、槽位/代次、age/duration/crossfade_duration、调用位置、因子、新种子和分配池头。原始旁证完整保留这些字段，并额外保存粒子/发射器/系统/holder/池地址及原版地址种子，绑定同 seq/kind/version 后随轨迹复制并计算 SHA-256。读取器独立核验地址与槽位关系、旧/新种子的 uint32 乘法公式、连续 ordinal 和两条精确调用路径。旧 shake 的因子是 `age==0 ? duration-1 : age-1`，新 shake 的因子是 `age`；合法 crossfade 首帧可以保留超过 duration 的 age，此时必须有正的 crossfade_duration。

逐帧还将语义事件重新计算成原生累计 FNV1a64 摘要，核对 state 的 `particle_shake.controlled_calls/controlled_digest`。摘要只输入实际种子相关标量，避免混入不同运行的 epoch 名称；边界版本和 control_phase 仍单独按重放映射精确比较。任何完整粒子 ID、年龄、调用次数/顺序或 canonical_seed 的差异都会失败，CRT RNG 字段继续完整比较。原始地址种子无需跨进程相等，但不会从证据中删除。

该模式的正式封包要求关闭顺序为 `recording_closed → particle_shake_closed → spawn_hook_closed`（没有出生钩子的测试适配器可省去最后一项）。健康摘要必须 installed/healthy 为真、queued/wrong_thread_calls/faults/overflow 为零、captured 与全部事件数一致、controlled_calls 与受控事件数一致。实际重放也在 initializer 完成关闭后增量读取健康尾部，缺尾部或溢出都不发布 equal。`replay-report.json.particle_shake` 记录模式、比较调用数、原始地址未跨进程比较，以及实际关闭健康是否已验证。初始化无边界调用保留为初始化证据；受控帧开始后再丢失边界，或种子调用没有后续受审计边界，都会停止严格校验。

支持动画身份归一化的记录在 `coverage.reanimations.raw_handle_evidence` 声明必需的 `audit/reanimation-handles.jsonl`。该旁证和 checksum/delta 每条 pre/post 的 schema、seq、kind、version、payload 一一对应，使用首态加 JSON Patch 保留原始句柄、槽位及代次、分配池、归属关系。封包复制该文件并绑定 SHA-256；读取器重建旁证，核对原始查找结果、全部 owner 路径、归一化引用与共享节点关系。缺失、截断、数量/边界不符、无效 patch、损坏查找或原生 link fault 都立即拒绝。实际重放的增量 `AuditTail` 执行相同验证。

声明 `owner_retirement_rules:["owner_dead","plant_squished_remove_effects"]` 的新记录还覆盖原版 `Plant::Squish → RemoveEffects` 生命周期：植物压扁字段 `0x142` 已置位但死亡字段 `0x141` 未置位时，动画句柄可以合法失效。读取器从同帧实际实体字段核对 `owner_dead`、植物专属 `owner_squished`，并核对仅 expired 引用允许出现的 `retirement_reason`；死亡优先于压扁。仍有效的句柄必须保留节点，普通存活对象的悬挂句柄继续拒绝。旧 manifest 保持只允许死亡对象过期的旧规则。

跨运行比较的是 manifest 声明的 **semantic identity**；原始动画句柄可因分配历史不同而不同，不做逐位等同，也不把旁证归档称为完整内存确定性。旧 manifest 和旧轨迹没有旁证仍兼容；只要旁证存在，就必须校验并纳入轨迹文件身份。声明 `animation_normalization=true` 或 `raw_handle_evidence.required=true` 却缺少旁证的记录不会降级成旧格式。

精确出生钩子的初始化期记录使用 `phase:initialization`、`version:null`、`payload.boundary:null`，读取器只为此明确契约接受未赋边界。受控期仍须把 `boundary.segment/tick/revision` 与事件 version 精确对应。出生事件保留全局序号；旧版共用帧 seq 的首次可见注释仍可读，新版独立 seq 必须连续。`recording_closed` 后允许一条同版本的 `spawn_hook_closed`，其健康状态、零溢出/故障/排队/活动初始化数，以及已捕获事件数与连续 ordinal 均须核验。

正式 `engine_replay.replay` 现在也独立比较每条受控 `zombie_initialized`，无需另跑边界对照工具。更新内出生必须绑定到紧邻的实际 pre/post 版本；动作内出生绑定到同 tick、同 epoch 且 revision 不倒退的下一个 pre 边界。单边界最多暂存 1,024 条，与原生出生队列容量一致；没有后续审计边界、错误版本或更新中丢失边界而伪称 initialization 的事件均拒绝。每个 `AuditFrame.spawn_events` 保留实际捕获顺序，比较不依赖出生后的末态是否还看得出差异。

跨运行只映射 epoch 和 tick 0 的初始 revision 偏移。payload 的重复 boundary 已单独核验；全局 ordinal 包含菜单预览历史，因此改为逐条核对受控序列的相对顺序。完整僵尸 ID/槽位/代次、caller RVA、输入参数、全部原始标量、初始位置/速度/variant、GameClock 与完整 MT 前后态都保留比较。仅四个已知动画字段使用相同读取器校验过的原始旁证映射，和独立边界对照工具共用实现；原始记录和原始句柄仍完整归档。初始化无版本历史单独计数并检查其原始序号/关闭健康，不要求跨进程预览数量相同；可执行初态本身仍严格相等。

报告增加 `spawn_events_compared`（已逐条匹配的出生数）、`spawn_exercised`（至少匹配一条），以及 `spawn_comparison` 中的源受控总数、双方初始化历史数、是否验证初态与实际关闭健康。零出生通过不能当作出生路径验证。seek 只比较实际重算前缀；完整重放还必须把双方全部受控出生计数与已比较数对齐，防止尾部多余事件被忽略。首个分叉报告请求/帧索引、边界 tick/phase、相对受控 ordinal、双方原生 seq 和具体 JSON Pointer；初始属性不同但随后 post 状态相同、丢失、额外或乱序出生都会失败。

已关闭的真实 022 动作源记录与其实际冷重放记录经新路径离线复核，2,000 个边界及 71 条受控初始化退出事件全部相等，双方各 12 条无边界初始化历史独立保留。完整读取、逐帧状态/出生比较及文件身份复核耗时 41.44 秒。该复核没有启动游戏，不代替新 DLL 的完整周期冷重放验收。

`audit_compare.py` 支持原生 nlohmann JSON diff 产生的 add/remove/replace，检查 JSON Pointer、数组下标、缺失路径、前后序号、每帧原生 delta、摘要与 state schema。逐帧状态从首态与 patch 流式重建，不把整局的所有完整状态同时驻留内存。实际重放使用 `AuditTail` 只处理上次完成之后新增的记录，检查文件替换、截断、序号缺口和每个新增帧；不会按请求数反复重扫整局前缀。请求事件和帧元数据也按 request_id 建索引。

粒子语义事件与原始旁证也逐条流式合并验证，序号连续性用单游标核验，不构造全部事件列表或排序副本。`AuditLog.events` 是可重复读取的 `EventStream`，只将控制/出生事件和小型帧头建索引；`AuditTail.events` 是已消费文件前缀的可重复视图。粒子记录在其所属边界比较后释放，最多暂存 8,192 条；单条 JSONL 的读取上限为 32 MiB。超过读取预算明确拒绝，不丢弃或抽样证据。这使内存由当前状态、单边界粒子和控制/帧索引决定，不随整局粒子总数线性增长。

关闭文件的每次流式遍历都核对原始 inode/大小/时间戳和全部字节的 SHA-256。提前结束的迭代器在显式关闭时仍验证未读后缀；重放和 seek 必须完成关闭及全部源文件核验后才发布成功或交给接管回调。增量读取保留各文件已消费前缀的 SHA-256，正式关闭时再读取字节核验此前前缀未被原地改写，不重新解码和重建整局。显式原生 hook fault 无条件使证据无效，即使其余序号、关闭摘要和文件格式完整。

每帧仍验证所有组件摘要和整体摘要。优化只复用未变组件的规范化编码、检查新写入 patch 的值，并使用流式状态。比较时直接比较重建出的规范化字节，避免把 FNV 摘要相等误当无碰撞的完整状态证明；只有出现差异才递归展开首个字段路径。

严格 JSON Pointer 的解析结果使用最多 16,384 项的 LRU 缓存，值为不可变 tuple，且只缓存不超过 512 字符的路径。更长的合法路径仍完整解析而不驻留缓存；非字符串和非法转义继续拒绝。缓存只复用路径文本的拆分结果，不缓存当前容器、数组边界、字段存在性、owner 或代次是否有效，每帧仍重新检查这些动态条件，JSON 重复键检查也保持独立。

在真实 012 录制的全部 2,000 个边界上，合入后的独立对照分别完整重建并散列规范化状态、原始动画旁证和边界 envelope。未缓存/缓存两次结果以及此前独立实验的完整流 SHA-256 完全相同；本次耗时 13.36 / 9.79 秒，缓存命中 1,420,935 次、解析 5,144 个不同路径。计时受同时运行的进程影响；相同的完整证据摘要是这次优化的等价性依据。

项目自带 LLVM-MinGW 或系统 `cc/clang/gcc` 可把模块中约十行的 FNV C 函数编译成当前 **Python 进程架构**的本机辅助库，缓存在 `work/audit-hash/`。它仅加速本地字节哈希，不注入游戏、不改变算法、不增加 Python 包依赖；加载时用空串、全字节值和含 NUL 数据核对参考结果。编译器不可用时自动使用完全相同的 Python 算法，`LVZ_PYTHON_FNV=1` 可在新进程强制回退。重放报告记录 `audit_hash_backend`。本机已关闭的 007 实验前六帧抽样中，相同校验的 cProfile 时间从 0.447 秒降至 0.022 秒；这只是抽样性能证据，不是整局验收。

粒子累计摘要使用同一辅助库的 FNV 延续函数，每条调用仍按规定的 12 个 uint64 小端字段更新。多种初始种子和分块输入与独立 Python 参考实现逐项相等；无法加载辅助库时继续使用相同的参考算法。该加速只减少每字节解释器循环，不省略种子公式、原始地址、调用顺序或边界检查。

真实已关闭的 016 录制经新旧读取路径分别重建全部 2,000 个边界、40,946 次粒子调用，对全部状态、原始动画旁证、粒子事件及边界/摘要字段的综合 SHA-256 均为 `6f6ab456ed83938c2399315495fbf2376a619a481c49f449ed6cce42417c07bc`。独立 Python 进程的峰值工作集从旧列表路径约 194.0 MB 降至流式路径约 55.2 MB；新路径单边界最多暂存 472 条粒子，只保留 205 条控制/出生记录。新路径完整加载校验 13.88 秒，随后再次遍历并计算对照摘要，两遍总计 36.71 秒。旧对照只做一次状态重建，因此不将两者总耗时作为速度比较。这里的内存数据只针对这一实际 1,000 ticks 记录，长局仍有随帧数增长的小型索引。

2026-09-19 对已关闭的真实 `eval-headless-010-s42-c0` 离线封包验证了 1,000 ticks / 2,000 帧边界和 11 条执行请求，包含约 33.9 MB 状态差分、0.65 MB 动画旁证及精确出生事件。两遍完整重建校验、复制和 SHA-256 封包共 28.62 秒，使用 native C FNV。旧格式的 `eval-headless-007-s42-c0` 同样 1,000 ticks / 2,000 帧边界、11 请求，完整封包用时 7.40 秒；其状态覆盖较小且无动画旁证，不能把两者直接作同工作量速度比较。这些计时只证明对应实际录制可完整读取归档，不证明冷重放相等或两旗实验通过。

报告中的 JSON Pointer 例如 `/zombies/slots/3/fields/0000002c` 精确定位原始位模式差异。FNV 摘要仅用于诊断；文件完整性由 SHA-256 检查。读入日志时拒绝重复 JSON 键、非 JSON 数值、截断尾行和缺失证据。原生审计的完整覆盖能力仍由 `determinism.md` 中的实际能力决定，比较通过不会把 `complete_game_state` 或 `original_engine_replay_verified` 自动改成 true。

无需游戏的演示：

```powershell
python -m llm_vs_zombies.engine_replay demo work/replay-demo
python -m unittest discover -s tests -p test_engine_replay.py -v
python -m unittest discover -s tests -p test_audit_compare.py -v
```

demo 明确使用 synthetic counter，创建两个独立计数器实例并经过真实 Client/SessionTrace/封装/重放路径。它不启动游戏。测试覆盖失败动作的原顺序、同帧 revision、epoch 映射、从头 seek、父分支身份、首个隐藏字段差异、实际响应差异、资产篡改、源记录缺失和已验证/未验证的终局。捕获测试还覆盖强制绘制影响隐藏状态、明确失败与未知结果、轻量/旧版像素证据、保护字段变化，以及图像不同但状态和元数据一致的合法重放。原版冷启动、两旗与扰动证据由独立实机验收提供。
