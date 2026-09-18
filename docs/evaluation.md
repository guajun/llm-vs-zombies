# 实验计划与就绪验收

`python -m llm_vs_zombies.evaluation` 是独立入口，对应 issue #7。它只调用项目 launcher、Client 和 engine replay 的公开 API；默认不连接任何模型服务，也不会产生付费 LLM 请求。策略可以是普通 Python 函数，之后再接任意模型提供者。

## 先生成计划，再执行

在项目根目录运行：

```powershell
$env:PYTHONPATH = 'src'
python -m llm_vs_zombies.evaluation plan work/smoke-plan.json
python -m llm_vs_zombies.evaluation run work/smoke-plan.json --output experiments/runs/eval-smoke-001
```

`plan` 只写 JSON，不启动游戏。默认 smoke 固定种子为 `[0,1,42]`，每种子录制 1000 tick，然后另起一个原版进程重放这段记录，最后用第三个进程验证断线和失败恢复。默认无策略只等待，不代表两仪通关策略。每次运行使用新的输出目录；所有游戏进程、用户档和日志均由 launcher 隔离。

执行阶段默认先构建 runtime/launcher，再运行 Python 与 CTest。构建或测试失败时不会启动游戏。`--skip-build` 允许开发时复用已构建文件，但会让 `build_and_tests` 保持未验证，不能据此生成严格就绪结论。

严格计划需要明确策略：

```powershell
python -m llm_vs_zombies.evaluation plan work/strict-plan.json --tier strict --strategy work/policy.py
python -m llm_vs_zombies.evaluation run work/strict-plan.json --output experiments/runs/eval-strict-001
```

strict 默认每个种子要求 10 次冷启动：一次录制策略轨迹，九次冷启动后重算同一轨迹，**不是让 LLM 再回答九次**。另有恢复测试专用进程，不能拿它凑十次完整重放。默认上限 200000 tick，达到上限仍未完成两旗即不满足通关门槛。可以编辑计划的种子集、预算、暂停时长和超时；strict 不能降低十次冷启动要求。策略相对路径相对于计划文件所在目录；生成命令会保存当时解析的绝对路径。

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

`context` 包含固定种子、决策序号、剩余 tick、smoke/strict 等级和终点名称。动作是普通 Client 协议，例如 `{'op':'plant','type':8,'row':2,'col':5}`。预算不能超过剩余实验预算，连续 16 次没有模拟进度会停止。墙钟上限在决策之间检查；任意外部模型请求仍应在策略代码内设置自己的网络超时。

执行器用 RecordedConsole 记录完整策略源码、SHA256、每次 Python cell、输出、异常、决策结果以及 Client 的请求/响应。策略代码可通过注入的 `record_exchange(provider, request, response)` 保存模型提示与回答；它不会自行调用模型。密钥应由本地环境读取，不能写进提示/响应记录。策略属于用户信任的本地 Python 代码，这个接口不是 Python 安全沙箱。

## 实际执行和证据

launcher 在进入游戏前应用 seed，源实验在真实两仪初态暂停后再次设置相同 seed，读回并核对全局 MT 的 624 项状态、游标和游戏线程 CRT 状态。这个初始化配方不声称已经控制启动期间全部随机来源。第一次实验保存真实时钟锚点，重放的冷启动使用同一个锚点，并比对整个已捕获初始状态。初态不同就失败，不能用“种子相同”替代验证。

在 `capture_initial` 前执行暂停扰动：保存完整 audit snapshot 与观察，等待指定墙钟时间，再确认两者都不变。随后记录单帧推进，再执行策略。完整两旗要求同时满足：实际观察到至少第 20 波、`completed_rounds` 比初值增加、结束时场景仍是 3。仅到第 20 波、计时器归零、僵尸短暂为空或策略自报成功都不算通过。

录制关闭后，engine replay 会读取真实客户端轨迹和原生 audit、核验记录完整性，再在新进程执行相同动作。任何初态差异、帧数差异、动作结果差异或捕获状态分叉均保留首个失败位置。终局跨 epoch 只有在 runtime 与 replay 都提供并核验对应终局边界证据时才可通过。

恢复测试使用另一局：发送完整的 100 tick 请求，通过另一连接确认游戏线程已接受，再主动断开原连接而不读取响应；重连后按**原请求 ID** 查询执行结果，不重新发送动作。断线可以取消尚未执行的预算，结果必须准确报告实际 0..100 tick 与 `client_disconnected`；若已执行完，则必须为 100 tick 与 `budget_exhausted`。观察时钟必须与该实际数量一致，重复查询必须返回同一个结果。再发送一个不支持的动作，确认零推进且明确 action_failed，随后验证合法单帧请求恢复正常。它与策略轨迹分开，避免把故障注入步骤混成策略操作。

整个启动调用期间只读采样前台窗口 HWND 和所属 PID，目标间隔为 25ms。启动返回后用实际游戏 PID 回查所有样本，并枚举游戏窗口确认全部隐藏。前后 HWND 不同仅作诊断，不能据此归因于游戏或用户，也不再单独导致失败；中途任何样本属于游戏 PID，则明确失败，即使前后 HWND 恰好相同。原始采样、PID 解析结果、最大实际采样间隔和结论保存在 `evaluation-windows.json`。

该检查是**有限采样**：可能漏掉两次读取之间的极短激活，不是连续焦点保证。任何 PID 未解析、采样错误、最大间隔超过 250ms 或未观察到游戏窗口，结果为 `unverified`，不能通过严格就绪门槛。无这些问题、全部样本未见游戏前台且最终游戏窗口全部隐藏才满足这里定义的观测门槛；启动器禁止激活的实现和隔离 fixture 是另外的验证依据。旧版只有前后 HWND 的记录不会被此代码追认为新版采样通过；需要新的真实运行补充证据。

整个 suite 前后对原始 ProgramData 用户档和原始 HKCU PopCap 配置做哈希指纹，记录哈希而不复制真实个人存档。用户自己在此期间修改原档也可能产生差异；报告保留真实变化，不能自动归因或忽略。

## 报告与门槛

suite 目录包含 `plan.json`、策略源码副本、`evaluation.json`、`evaluation.md`、各 seed case 结果、重放报告和共享用户档前后哈希。源局及每次冷启动都在相邻的独立 run 目录中，保留初始化、客户端 trace、原生 audit、进程清理结果及首个失败证据。每个检查附证据文件路径和 SHA256；证据文件变化后，readiness 重新计算时不能通过。

严格 `experiment_ready=true` 必须所有门槛均有实际证据：

| 门槛 | 要求 |
|---|---|
| build_and_tests | 当次真实构建和测试通过 |
| private_launch / scenario | 主线程执行前隔离、最终窗口隐藏、启动期有效采样未见游戏前台；真实 Scene 3/阵型/卡序 |
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
