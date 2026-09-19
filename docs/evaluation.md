# 实验计划与就绪验收

`python -m llm_vs_zombies.evaluation` 是独立入口，对应 issue #7。它只调用项目 launcher、Client 和 engine replay 的公开 API；默认不连接任何模型服务，也不会产生付费 LLM 请求。策略可以是普通 Python 函数，之后再接任意模型提供者。

## 先生成计划，再执行

在项目根目录运行：

```powershell
$env:PYTHONPATH = 'src'
python -m llm_vs_zombies.evaluation plan work/smoke-plan.json
python -m llm_vs_zombies.evaluation run work/smoke-plan.json --output experiments/runs/eval-smoke-001
```

显式的静音实验计划可加 `--audio-mode sound_effects_allocation_none_v1`，例如：

```powershell
python -m llm_vs_zombies.evaluation plan work/silent-plan.json --audio-mode sound_effects_allocation_none_v1 --strategy examples/liangyi_baseline.py
```

计划的 `audio_mode` 会传给每一个冷启动及恢复测试进程；省略时保持 `original`。两种模式必须分别录制和验收，不能将旧音频轨迹切换模式后视为同一实验。该模式仍保留音乐路径、原版音效变体选择代码和实际随机数消耗；详见[空音效分配模式](silent-audio.md)。

`plan` 只写 JSON，不启动游戏。默认 smoke 固定种子为 `[0,1,42]`，每种子录制 1000 tick，然后另起一个原版进程重放这段记录，最后用第三个进程验证断线和失败恢复。默认无策略只等待，不代表两仪通关策略。每次运行使用新的输出目录；所有游戏进程、用户档和日志均由 launcher 隔离。

执行阶段默认先构建 runtime/launcher，再运行 Python 与 CTest。构建或测试失败时不会启动游戏。`--skip-build` 允许开发时复用已构建文件，但会让 `build_and_tests` 保持未验证，不能据此生成严格就绪结论。

严格计划需要明确策略：

```powershell
python -m llm_vs_zombies.evaluation plan work/strict-plan.json --tier strict --seeds 42 --strategy examples/liangyi_baseline.py --audio-mode sound_effects_allocation_none_v1
python -m llm_vs_zombies.evaluation run work/strict-plan.json --output experiments/runs/eval-strict-001
```

strict 默认每个种子要求 10 次冷启动：一次录制策略轨迹，九次冷启动后重算同一轨迹，**不是让 LLM 再回答九次**。另有恢复测试专用进程，不能拿它凑十次完整重放。默认上限 200000 tick，达到上限仍未完成两旗即不满足通关门槛。源未完成两旗时封存真实结果并跳过九次重放；smoke 的正常来源仍运行一次真实 cold 和恢复探针。首个 cold 失败后停止派发该种子尚未开始的重放，已运行的 cold 完成正常关闭和证据保留。这里的 baseline 是公开候选策略，命令可执行并不证明它已能赢完整两旗。

`plan` 支持 `--seeds 0,1,42`、`--tick-budget`、`--cold-starts`、`--cold-workers 1|2`、`--timeout-seconds`、`--wall-budget-seconds`、`--cold-wall-budget-seconds`、`--min-free-bytes`、`--packaging-reserve-bytes`、`--disk-check-ticks` 和 `--pause-points '[[1000,1],[2500,5]]'`。strict 生成默认单请求超时600秒、源墙钟预算86400秒、每次cold墙钟预算86400秒；smoke 单请求/源默认仍为90/3600秒。已有计划保持其显式值，不自动延长。strict 不能降低十次冷启动要求。策略相对路径相对于计划文件所在目录；生成命令会保存当时解析的绝对路径。

`cold_workers` 默认 `1`，仅接受整数 `1` 或 `2`；旧计划缺失此字段时仍串行。`2` 在 Windows 上最多同时运行两个独立冷重放宿主，source、不同种子和 recovery 仍顺序执行。默认 smoke 只有一次 cold；若要两次 cold，可同时设置 `--cold-starts 3`。每个 worker 都独立对源轨迹执行完整核验并保留原请求预算；其窗口、关闭、宿主、资源及归档门槛各自进入总报告。并发不会降低 strict 条件。配置、资源边界和回执见[并行冷重放](parallel-cold.md)。

source/recovery 墙钟预算从初始化完成的真实B0开始；cold 从该 attempt 实际开始计算，包含读取源轨迹、初始化和重放，排队时间不计。在完成的原请求边界检查预算，包含策略调用/暂停/在线读取的等待；不等于整个suite总时限，也不强行中断正在执行的原版请求。单请求超时另行限制IPC。cold的最后请求若已超预算，先完成实际结果和关闭health验证，再让资源门槛失败；不会为了及时报告预算而跳过尾部。无限阻塞的受信任策略函数仍需自己设置外部调用超时。

比较不同策略时，用相同计划种子、场景和预算分别创建 suite，并比较每个 seed 的结果、动作与耗时。各自的 replay 验证各自记录的轨迹。相同种子不自动保证不同策略面对逐只完全相同的随机事件：不同动作可能改变 RNG 消耗；需要固定外生出怪事件的评测是另一项实验模式，不能由本运行器的 replay 通过结果推出。

## 策略协议

策略文件必须定义 `decide(observation, context)`，返回明确动作列表及推进预算。例如这段仅用于管线检查的等待策略：

```python
def decide(observation, context):
    return {
        'actions': [],
        'advance_ticks': min(100, context['remaining_ticks']),
    }
```

`context` 包含固定种子、决策序号、剩余 tick、smoke/strict 等级和终点名称。动作是普通 Client 协议，例如 `{'op':'plant','type':8,'row':2,'col':5}`。预算不能超过剩余实验预算，连续 16 次没有模拟进度会停止。墙钟上限在决策前后检查；任意外部模型请求仍应在策略代码内设置自己的网络超时。

执行器用 RecordedConsole 记录完整策略源码、SHA256、每次 Python cell、输出、异常、决策结果以及 Client 的请求/响应。策略代码可通过注入的 `record_exchange(provider, request, response)` 保存模型提示与回答；它不会自行调用模型。密钥应由本地环境读取，不能写进提示/响应记录。策略属于用户信任的本地 Python 代码，这个接口不是 Python 安全沙箱。

## 实际执行和证据

launcher 在进入游戏前应用 seed，源实验在真实两仪初态暂停后再次设置相同 seed，读回并核对全局 MT 的 624 项状态、游标和游戏线程 CRT 状态。这个初始化配方不声称已经控制启动期间全部随机来源。第一次实验保存真实时钟锚点，重放的冷启动使用同一个锚点，并比对整个已捕获初始状态。初态不同就失败，不能用“种子相同”替代验证。

当前显式静音 runtime 还声明两项独立初始化能力：[App 更新计数锚定](app-update-anchor-native.md)与[诊断音效计数起点](sound-counter-origin-native.md)。顺序为种子/三时钟读回、绑定本进程诊断计数起点、写入记录的真实 App 计数、warm 绘制、B0。原始 App 写入保留完整前后回执；bootstrap 的绝对计数不重置，每个更新边界另存原始累计旁证，实验内计数包含 warm。完整游戏状态直接比较，允许不同的启动诊断累计值不等于允许游戏字段分叉。旧档没有这些声明时仍按原合同读取，不能自动升级为新模式。

在 `capture_initial` 前执行暂停扰动：保存 audit snapshot 与观察，等待指定墙钟时间，再确认**已捕获模拟状态与version逐字段不变**。暂停证明的冻结对象是模拟状态与版本，不是整份 snapshot 字节：声明固定 owner 浮点模式时，每次读取 snapshot 都会新增一次 monitor 检查，两次读取必然不同；探针改为显式核验该模式的激活回执未变，且 after 侧 monitor 仍健康、控制位仍在目标值，并把实际比较的字段写入probe记录，不静默丢弃这段证据。随后记录单帧推进，再执行策略：**公开runner首个策略决策在B1**，与私有045在B0决策不同；当前公开源自行记录真实初始锚点，不继承私有041的B0。每个cold使用自己的实际进程及源recipe，从完整B0重新执行包括这次advance1在内的相同请求。

source及cold还按 `pause_points`，在首次达到指定tick的已完成战斗请求边界等待。默认为≥1000时1秒、≥2500时5秒；保存请求目标、实际版本和同样的模拟状态不变证明（比较范围与B0暂停探针一致）。一次请求跨过多个点就在同一实际边界逐个检查，不拆预算、不新增推进、不重试动作。末请求恰好停在B1000且仍在战斗时照常探测。报告分别列出configured、completed、已达阈值却无法执行的unexecuted_reached和尚未到达终点的not_reached。直接到终局或资源停止导致已达点无法合法探测时，coverage为unverified，不能用B0暂停通过来遮盖；超出真实终点的点明确not applicable。记录在 `pause-probes-during-play.json` 和客户端trace中。

完整两旗要求同时满足：实际观察到至少第 20 波、`completed_rounds` 比初值增加、结束时场景仍是 3。正常返回选卡允许短于请求预算，GameOver则是可正常录制的策略失败。仅到第 20 波、计时器归零、僵尸短暂为空或策略自报成功都不算通过。实际零时钟终局仍是一笔原生调用，前后完整state可能变化；公开runner不把同tick当作没有执行，严格reader继续核验所有原生调用、终局和原始旁证。

录制关闭后，engine replay 会读取真实客户端轨迹和原生 audit、核验记录完整性，再在新进程执行相同动作。任何初态差异、帧数差异、动作结果差异或捕获状态分叉均保留首个失败位置。终局跨 epoch 只有在 runtime 与 replay 都提供并核验对应终局边界证据时才可通过。

公共runner在source、每个cold和recovery自己的实际会话中检查runtime身份；任何声明 `test_fixture` 的DLL都在实验初始化前被拒绝，并正常清理自有录制和进程。独立生命周期夹具可由专用工具录制及重放，其证据不能认证生产实验就绪。

恢复测试使用另一局：发送完整的 100 tick 请求，通过另一连接确认游戏线程已接受，再主动断开原连接而不读取响应；重连后按**原请求 ID** 查询执行结果，不重新发送动作。断线可以取消尚未执行的预算，结果必须准确报告实际 0..100 tick 与 `client_disconnected`；若已执行完，则必须为 100 tick 与 `budget_exhausted`。观察时钟必须与该实际数量一致，重复查询必须返回同一个结果。再发送一个不支持的动作，确认零推进且明确 action_failed，随后验证合法单帧请求恢复正常。它与策略轨迹分开，避免把故障注入步骤混成策略操作。

整个启动调用期间只读采样前台窗口 HWND 和所属 PID，目标间隔为 25ms。启动返回后用实际游戏 PID 回查所有样本，并枚举游戏窗口确认全部隐藏。前后 HWND 不同仅作诊断，不能据此归因于游戏或用户，也不再单独导致失败；中途任何样本属于游戏 PID，则明确失败，即使前后 HWND 恰好相同。原始采样、PID 解析结果、最大实际采样间隔和结论保存在 `evaluation-windows.json`。

采样现由[独立观察器进程](window-observer.md)执行，避免与父进程的状态解析共用 Python 执行锁；子进程首样本确认后才进入启动调用，实际 PID 在退出采样上下文前绑定。原始 JSONL、进程创建身份、文件 SHA256 与封口证据保存在 `decisions/window-observer-launch/`。source、每次cold及恢复探针另外保存V3运行期原始证据于 `decisions/window-observer-runtime/`，结果为 `evaluation-runtime-windows.json`。它从ready之后、受控初始化之前开始，覆盖到原生recording关闭；目标仍活着时观察器完成最后样本和封口，随后才停止自有游戏进程。两个采样上下文之间的交接不声称无间隙连续观察。保留25ms目标/250ms最大间隔；系统调度或存储导致的漏采仍然拒绝。

窗口失败或unverified只影响单独门槛，不在replay initializer退出时抛错打断原生关闭health检查。完成这些检查及归档后，总报告仍拒绝把该session算成功。启动通过不能代替运行期通过，同一个seed的source通过也不能遮盖某个cold或恢复探针的窗口失败。

该检查是**有限采样**：可能漏掉两次读取之间的极短激活，不是连续焦点保证。任何 PID 未解析、采样错误、最大间隔超过 250ms 或未观察到游戏窗口，结果为 `unverified`，不能通过严格就绪门槛。无这些问题、全部样本未见游戏前台且最终游戏窗口全部隐藏才满足这里定义的观测门槛；启动器禁止激活的实现和隔离 fixture 是另外的验证依据。旧版只有前后 HWND 的记录不会被此代码追认为新版采样通过；需要新的真实运行补充证据。

整个 suite 前后对原始 ProgramData 用户档和原始 HKCU PopCap 配置做哈希指纹，记录哈希而不复制真实个人存档。用户自己在此期间修改原档也可能产生差异；报告保留真实变化，不能自动归因或忽略。

## 报告与门槛

suite 目录包含 `plan.json`、策略源码副本、`evaluation.json`、`evaluation.md`、各 seed case 结果、每局外部 `*-retention.json`、重放报告和共享用户档前后哈希。源局及每次冷启动都在相邻的独立 run 目录中，保留初始化、客户端 trace、原生 audit、进程清理结果及首个失败证据。每个检查附证据文件路径和 SHA256；证据文件变化后，readiness 重新计算时不能通过。

实际执行策略副本及文本hash保存在trace；每局已有 `inputs/implementation.zip` 是宿主/构建源码快照。`inputs/evaluation-host.json` 和 `evaluation-host-final.json` 另记录真实导入模块路径与Python包磁盘源码hash，要求与该ZIP一致且期间未变，不能把任意 `--root` 的源码冒充实际import实现。这是磁盘源码provenance，不是Python内存全快照；受信任策略任意导入外部模块的内容也不由它自动封存。

失败收尾独立记录 `primary_error` 和清理/封包/写盘的 `secondary_errors`。若重放分叉后又因磁盘问题写报告失败，保留异常链与实际分叉内容；不会只剩最后一个OSError。必须有recording/client/trace/自有进程四项明确关闭回执，才允许封包或封存；锁不存在不是关闭证明。源策略异常的真实完整前缀可尝试严格封包，缺初态/未知请求结果/不健康原生尾部则不能伪造轨迹。实际原档可在全部writer关闭后封存为失败，仍不等于严格录制或重放成功。仍有writer lock、关闭失败或未启动的目录保留原证据及外部错误，不删锁、不改旧档以取得pass。`evaluation-finalization.json` 是封存前的尝试记录，外部retention回执才记录实际seal/校验结果，封存后不再修改run内容。

默认启动前/B0及每跨500 tick的首次完成边界检查实际可用空间，低于2 GiB以 `disk_reserve_stop` 停止发新请求并正常关闭。单请求可能跨过多个区间，检查只发生在其真实完成后；不会悄悄拆成更多请求。严格封包前计算已关闭audit文件与决策trace的真实复制字节，要求可用空间至少为复制量加1 GiB；不足时记录 `packaging_skipped_insufficient_space`，不调用封包器，并在真实关闭条件满足时封存原档，录制门槛不通过。参数都可以在计划中显式调整。它们是有界检查，不是文件系统预留；其他进程和当前请求仍可能用尽磁盘，IO错误继续走失败保留路径。不假设每tick日志量或压缩比，不删除旧失败档来挤出空间。

`completed_cases` 是整个请求流程完成数；`full_cycle_successes` 才是实际两旗成功数。另列cold尝试/成功数量、源真实失败action数量。预算/墙钟/no-progress/资源停止都不是胜利；完整健康失败轨迹和基础设施失败要分别查看。恢复探针有意丢弃一条客户端响应，不宣称它是完整可重放客户端轨迹；关闭后直接使用严格 `AuditLog(require_closed=True)` 核验原生全部帧/旁证/健康尾部。

严格 `experiment_ready=true` 必须所有门槛均有实际证据：

| 门槛 | 要求 |
|---|---|
| build_and_tests | 当次真实构建和测试通过 |
| private_launch / scenario | 主线程执行前隔离、最终窗口隐藏、启动期有效采样未见游戏前台；真实 Scene 3/阵型/卡序 |
| runtime_windows | source、每次cold和恢复探针均有完整绑定的V3运行期观察证据 |
| session_cleanup / archive_integrity | 各局真实关闭、自有进程停止、原档正常封存；次级错误不能变成pass |
| host_identity / resource_limits | 实际导入源码绑定且未变、每局实际磁盘/墙钟预算未超限 |
| fixed_rng / initial_state | 读回种子状态正确，重复初始化完整捕获状态一致 |
| single_step / pause_invariance | 一次请求对应一帧，墙钟暂停不改变捕获状态 |
| disconnect_recovery / failure_recovery | 丢响应后可查原请求结果，无额外推进，错误后可继续 |
| recording / engine_replay | 真实日志关闭并可封装，原版冷启动重放捕获状态一致 |
| full_cycle / ten_cold_starts | 实际完整两旗，每种子至少十次冷启动通过 |
| shared_profile_unchanged | 原始用户档及原配置前后哈希不变 |

`mock`、离线模型测试、计划中的功能、无证据的 pass 标签和缺失文件都不能通过 live 门槛。某阶段没有执行标记为 `unverified`，实际失败保存 `fail` 或 case 错误。启动后尚未进入战斗的诊断局记为 `startup_failed`，不会伪造 state，也不会用正常 played-run finalize 掩盖缺失的录制。

smoke 即使全部短测通过，也始终不会标成严格就绪。返回码 0 代表请求的 smoke 管线完成；strict 返回码 0 才代表它的所有就绪门槛通过。返回码 2 表示运行产生了报告但验收未通过，返回码 1 表示配置或启动整个 suite 出错。

即使通过，`strict_engine_determinism_proven` 仍是 false：当前 audit 仍列有未覆盖的效果、线程和引擎内部状态。就绪结论限定为已测版本、场景、种子集、动作与捕获状态，不是对任意输入或全部隐藏状态的数学证明。

## CI 的边界

`.github/workflows/ci.yml` 使用固定 action commit、固定 AvZ submodule commit 和 SHA256 锁定 LLVM-MinGW。Linux 跑标准库单元测试；Windows 构建原生模块，跑 Python/CTest，并启动不含游戏的隔离 fixture，验证 pre-main 注入、appdata/注册表重定向与隐藏窗口。失败时上传有限诊断日志。

公开 CI 不包含游戏本体、个人用户档、付费模型密钥或真实 PvZ 重放。CI 通过只能证明可分发代码的构建和离线验证通过；真正 live suite 由本机持有合法游戏文件的环境执行。
