# Python REPL 与运行时客户端

客户端使用 Python 标准库，通过本机 Windows named pipe 与常驻 DLL 通信。策略函数和变量留在 Python 进程；执行普通策略无需编译 C++。这份说明描述已实现客户端，不表示原版游戏的帧边界或确定性已通过实验验收。运行时的 `hello.capabilities` 是实际能力入口；checkpoint 与严格确定性不应自行假定可用。

## 启动

在仓库根目录安装 Python 包：

```powershell
python -m pip install -e .
python -m llm_vs_zombies.repl --pid 1234 --trace experiments/runs/trial-ready/decisions/client-session.jsonl
```

`1234` 必须替换为已加载本项目运行时 DLL 的游戏 PID。客户端不会自行启动游戏、注入 DLL 或选关。也可以传 `--endpoint '\\.\pipe\llm-vs-zombies-1234'`。只接受本机、本项目前缀的命名管道。

进入后已有 `game`、`plant`、`shovel`：

```python
game.hello_result                 # session / epoch / capabilities / limits
obs = game.observe()              # 返回普通 dict，不推进模拟
obs["version"]                   # epoch、tick、revision
result = game.advance(1)
result["executed_ticks"]          # 检查实际执行数量；不能假定等于请求数量
result["stop_reason"]

# 8 是原版/AvZ 小喷菇枚举。格子是 1-based；是否可种由运行时检查。
result = game.commit([plant(8, row=2, col=5)], advance_ticks=1)
result["action_results"]          # 即使 RPC 成功，也可能有具体动作失败
result = game.shovel(row=2, col=5) # 默认不推进模拟
```

`game.plant(8, 2, 5)` 是单动作 `commit` 的方便写法。类型使用 AvZ 数字枚举，**不是选卡槽编号**。字符串植物名暂不接受。

变量、函数、导入和循环会持续存在：

```python
def leftmost_zombie(obs):
    return min(obs["zombies"], key=lambda z: z["x"], default=None)

threat = leftmost_zombie(game.observe())
```

多行函数最后输入空行提交。`until` 只接受运行时协议条件，如 `game.advance(100, until={"event": "wave_changed"})`，不接受 Python callback。运行时首版上限为一次 100000 tick / 256 动作，以协商得到的 `hello.limits` 为准。

两份可运行示例：

```powershell
python -m llm_vs_zombies.repl --pid 1234 --trace experiments/runs/trial-ready/decisions/step-session.jsonl --script examples/observe_and_step.py
python examples/persistent_strategy.py --pid 1234 --trace experiments/runs/trial-ready/decisions/strategy-session.jsonl
```

`--script` 会把整个源文件作为一个代码单元记录，执行失败返回进程退出码 1。普通 Python 模块示例使用 `SessionTrace` 与 `connect()`，但若绕过 `RecordedConsole` 直接执行代码，其源文件不会自动成为 trace 的代码单元；需要完整代码审计时使用 REPL/`--script`。

## 版本、失败和重试

`commit` / `advance` 默认采用最近一次响应中的观察版本；还没有观察时先调用 `observe()`。也可以显式传入 `expect=obs["version"]`。客户端不会偷偷刷新 stale version 后再次执行；该错误会直接交给调用方，并清除本地观察缓存。下一次**新决策**需要重新观察。

动作按顺序执行，结果以服务端响应为准。第二个动作失败不代表第一个动作回滚。客户端保存整个 `action_results`、实际执行帧数和最终观察，而不从请求推测结果。

**客户端完全不自动重试请求，包括跨 epoch 情况。** 每次新调用生成唯一 ID；同一个客户端拒绝重复 ID。若变更请求发送后断线、超时或收到无法信任的响应，会抛出 `OutcomeUnknown`，关闭连接并在 trace 中保留请求 ID、请求内容及异常。此时不能用新 ID 重复原动作；原动作可能已经执行。应先查看服务端执行记录和新的 `hello` / `observe`，确认当前会话、epoch 与实际状态，再决定后续动作。当前客户端不提供跨进程 exactly-once 保证或自动断点续跑。

`--timeout` 默认 15 秒，是一个请求的总通信期限，不会因每个短读重新开始。大跨度推进可显式增加 `game.advance(1000, timeout=60)`。超时不等于取消游戏动作。运行时取消必须使用明确的 `cancel` 请求。

同一客户端串行处理请求。要从另一个进程/线程取消正在执行的长推进，创建**第二个连接及独立 trace 文件**，调用 `control.cancel()` 或 `control.pause()`；这两种边界控制请求允许省略 expect，避免等待一次额外观察。`status()` 返回服务端当前状态。

## 记录内容

`SessionTrace` 为 append-only UTF-8 JSONL，每条记录带 schema、连续 seq 与 UTC 记录时间：

| kind | 内容 |
|---|---|
| `request` | 实际序列化发送前的协议请求、ID、expect 与动作 |
| `response` | 服务端实际返回的完整 JSON，包括失败动作 |
| `observation` | 完整观察与关联 request_id |
| `exception` / `invalid_response` | 传输或协议异常、未知执行结果标记、非法响应字节 |
| `cell` / `cell_output` | 已提交的 Python 源码、stdout / stderr |
| `cell_exception` / `cell_complete` | Python traceback 与代码单元完成情况 |
| `session_start` / `session_end` | REPL 进程边界 |

每条记录立即 flush；可用 `SessionTrace(path, durable=True)` 增加 fsync。普通 flush 不保证断电落盘。已有文件可以继续追加，原有字节不改写；不完整尾行和错误 seq 会拒绝打开。`.lock` 防止两个进程同时写同一 trace，崩溃后应确认原进程已结束并检查文件再手动处理遗留锁，程序不擅自删除。

客户端 trace 与游戏原生事件日志分开保存；前者记录意图、实际 RPC 响应和策略代码，后者才是原版执行边界的权威记录。二者不应混充 CS2 式原版 demo，也不自动证明状态已完整记录。交互代码和输出会原样进入文件，运行记录按仓库规则留在本机。

## 验证范围

测试覆盖真实 Windows named pipe 的分片读写与取消超时、4 MiB 帧限制、请求 ID 校验、同帧 revision 更新、stale epoch 拒绝、超时后不重复执行、动作失败保留，以及多单元 Python 状态和异常审计：

```powershell
python -m unittest discover -s tests -p test_client.py -v
python -m unittest discover -s tests -p test_session.py -v
```

真实 Windows 管道测试使用独立测试服务端，不启动或操作游戏。原版游戏的单帧精度、暂停期间状态稳定性和重放一致性需要项目级验收记录另行证明。
