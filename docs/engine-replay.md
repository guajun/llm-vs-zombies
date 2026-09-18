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

输出目录必须是新目录。封装结果包含 `trajectory.json`、原始 SessionTrace 和原生 `audit/` 四个文件。所有证据文件以 SHA-256 绑定，轨迹身份是清单内容的 SHA-256；这是内容完整性检查，不是身份签名或第三方真实性认证。重放时重新检查文件、schema、目标、源请求/响应与原生权威事件的一致性，不只信任已经生成的动作清单。

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

支持动画身份归一化的记录在 `coverage.reanimations.raw_handle_evidence` 声明必需的 `audit/reanimation-handles.jsonl`。该旁证和 checksum/delta 每条 pre/post 的 schema、seq、kind、version、payload 一一对应，使用首态加 JSON Patch 保留原始句柄、槽位及代次、分配池、归属关系。封包复制该文件并绑定 SHA-256；读取器重建旁证，核对原始查找结果、全部 owner 路径、归一化引用与共享节点关系。缺失、截断、数量/边界不符、无效 patch、损坏查找或原生 link fault 都立即拒绝。实际重放的增量 `AuditTail` 执行相同验证。

跨运行比较的是 manifest 声明的 **semantic identity**；原始动画句柄可因分配历史不同而不同，不做逐位等同，也不把旁证归档称为完整内存确定性。旧 manifest 和旧轨迹没有旁证仍兼容；只要旁证存在，就必须校验并纳入轨迹文件身份。声明 `animation_normalization=true` 或 `raw_handle_evidence.required=true` 却缺少旁证的记录不会降级成旧格式。

精确出生钩子的初始化期记录使用 `phase:initialization`、`version:null`、`payload.boundary:null`，读取器只为此明确契约接受未赋边界。受控期仍须把 `boundary.segment/tick/revision` 与事件 version 精确对应。出生事件保留全局序号；旧版共用帧 seq 的首次可见注释仍可读，新版独立 seq 必须连续。`recording_closed` 后允许一条同版本的 `spawn_hook_closed`，其健康状态、零溢出/故障/排队/活动初始化数，以及已捕获事件数与连续 ordinal 均须核验。

`audit_compare.py` 支持原生 nlohmann JSON diff 产生的 add/remove/replace，检查 JSON Pointer、数组下标、缺失路径、前后序号、每帧原生 delta、摘要与 state schema。逐帧状态从首态与 patch 流式重建，不把整局的所有完整状态同时驻留内存。实际重放使用 `AuditTail` 只处理上次完成之后新增的记录，检查文件替换、截断、序号缺口和每个新增帧；不会按请求数反复重扫整局前缀。请求事件和帧元数据也按 request_id 建索引。

每帧仍验证所有组件摘要和整体摘要。优化只复用未变组件的规范化编码、检查新写入 patch 的值，并使用流式状态。比较时直接比较重建出的规范化字节，避免把 FNV 摘要相等误当无碰撞的完整状态证明；只有出现差异才递归展开首个字段路径。

项目自带 LLVM-MinGW 或系统 `cc/clang/gcc` 可把模块中约十行的 FNV C 函数编译成当前 **Python 进程架构**的本机辅助库，缓存在 `work/audit-hash/`。它仅加速本地字节哈希，不注入游戏、不改变算法、不增加 Python 包依赖；加载时用空串、全字节值和含 NUL 数据核对参考结果。编译器不可用时自动使用完全相同的 Python 算法，`LVZ_PYTHON_FNV=1` 可在新进程强制回退。重放报告记录 `audit_hash_backend`。本机已关闭的 007 实验前六帧抽样中，相同校验的 cProfile 时间从 0.447 秒降至 0.022 秒；这只是抽样性能证据，不是整局验收。

2026-09-19 对已关闭的真实 `eval-headless-010-s42-c0` 离线封包验证了 1,000 ticks / 2,000 帧边界和 11 条执行请求，包含约 33.9 MB 状态差分、0.65 MB 动画旁证及精确出生事件。两遍完整重建校验、复制和 SHA-256 封包共 28.62 秒，使用 native C FNV。旧格式的 `eval-headless-007-s42-c0` 同样 1,000 ticks / 2,000 帧边界、11 请求，完整封包用时 7.40 秒；其状态覆盖较小且无动画旁证，不能把两者直接作同工作量速度比较。这些计时只证明对应实际录制可完整读取归档，不证明冷重放相等或两旗实验通过。

报告中的 JSON Pointer 例如 `/zombies/slots/3/fields/0000002c` 精确定位原始位模式差异。FNV 摘要仅用于诊断；文件完整性由 SHA-256 检查。读入日志时拒绝重复 JSON 键、非 JSON 数值、截断尾行和缺失证据。原生审计的完整覆盖能力仍由 `determinism.md` 中的实际能力决定，比较通过不会把 `complete_game_state` 或 `original_engine_replay_verified` 自动改成 true。

无需游戏的演示：

```powershell
python -m llm_vs_zombies.engine_replay demo work/replay-demo
python -m unittest discover -s tests -p test_engine_replay.py -v
python -m unittest discover -s tests -p test_audit_compare.py -v
```

demo 明确使用 synthetic counter，创建两个独立计数器实例并经过真实 Client/SessionTrace/封装/重放路径。它不启动游戏。测试覆盖失败动作的原顺序、同帧 revision、epoch 映射、从头 seek、父分支身份、首个隐藏字段差异、实际响应差异、资产篡改、源记录缺失和已验证/未验证的终局。捕获测试还覆盖强制绘制影响隐藏状态、明确失败与未知结果、轻量/旧版像素证据、保护字段变化，以及图像不同但状态和元数据一致的合法重放。原版冷启动、两旗与扰动证据由独立实机验收提供。
