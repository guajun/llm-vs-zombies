# LLM vs Zombies

在原版 PvZ 1.0.0.1051 中做雾夜两仪实验。固定的 AvZ DLL 提供游戏线程接口，Python REPL 发送有限动作；游戏停在帧边界等待模型，执行时无需重新编译。所有实验在独立用户档和隐藏窗口中运行，不需要桌面键鼠操作。

公开仓库包含源码、教程、设计和测试；游戏本体、参考存档副本、工具链和实验数据留在本机，不随仓库分发。

## 目录

| 目录 | 内容 |
|---|---|
| `avz/framework` | 固定提交的官方 AvZ 子模块 |
| `runtime` | 常驻命名管道、原子动作序列、精确帧预算与失败恢复 |
| `launcher` | 挂起启动、进程内隔离、隐藏窗口及 DLL 注入 |
| `logger/avz` | 状态、出怪类型表与操作日志 |
| `determinism` | 目标二进制校验、已识别 RNG 状态、逐帧状态差分 |
| `recording` | 原版绘制表面捕获，无桌面截图 |
| `src/llm_vs_zombies` | Python 客户端、REPL、流式视频、原引擎重放与评测 |
| `replay/viewer` | 单文件 HTML 状态复盘器 |
| `game/original`、`game/local-engine` | 本机兼容游戏及已解包引擎，Git 忽略 |
| `experiments/runs` | 每轮独立用户档、模块、观察、决策、视频、审计和封存清单 |
| `docs`、`examples`、`tests` | 教程、接口、实验基准和验证 |

## 准备与后台运行

```powershell
git clone --recurse-submodules https://github.com/guajun/llm-vs-zombies.git
cd llm-vs-zombies
./tools/bootstrap.ps1
./tools/build-avz.ps1
./launcher/build.ps1
# 按 docs/launcher.md 放置自己持有的兼容游戏和锁定引擎。
./tools/launch-experiment.ps1 -Name my-run
```

启动器核对所有输入和模块哈希，创建私有用户档与注册表配置，加载参考存档并核验真实雾夜场景、26 株阵型及 10 卡顺序。`headless` 当前含义是隐藏普通游戏窗口，保留原版 Win32/DirectDraw 初始化；尚未实现脱离桌面会话的纯无窗口服务器。

```powershell
$env:PYTHONPATH = 'src'
python -m llm_vs_zombies.repl --help
python -m llm_vs_zombies.evaluation plan work/smoke-plan.json
python -m llm_vs_zombies.evaluation run work/smoke-plan.json --output experiments/runs/eval-smoke
```

客户端示例（PID 从本轮 `launcher.json` 读取）：

```python
from llm_vs_zombies.client import connect, plant
from llm_vs_zombies.session import SessionTrace

with SessionTrace('experiments/runs/my-run/decisions/session.jsonl') as trace:
    with connect(pid=12345, trace=trace, timeout=90) as game:
        observation = game.observe()
        result = game.commit([plant(8, 1, 8)], advance_ticks=20)
        # Python/模型可在此思考任意时长，游戏保持暂停。
        game.request('stop_recording', expect=game.observe()['version'])
```

每条修改请求携带观察版本，结果报告实际动作成功与实际推进帧数。连接丢失后查询原请求 ID，不用新 ID 盲目重发。AvZ 的自动收集是固定环境辅助，其收集尝试另外记录为 `environment_collect`，不属于模型决策。

```powershell
./tools/launch-experiment.ps1 -Name my-run -Stop
./tools/lvz.ps1 finalize experiments/runs/my-run --outcome completed
./tools/lvz.ps1 validate experiments/runs/my-run
./tools/lvz.ps1 export experiments/runs/my-run
```

## 可重复性与录制边界

- 固定游戏、资源、存档、框架、DLL 和初始化配方；在进场前及首个受控帧播种已确认的全局 MT 与游戏线程 CRT，保存完整随机状态。单有一个 seed 不等于任意环境下全局确定性。
- 原生审计记录每次更新前后状态哈希与 JSON 差分，涵盖主要实体池、卡槽、波表与已识别 RNG。仍有未覆盖的动画、特效、线程与时源，能力清单明确列出。
- 动画关联比较采用经过实际对象池代次验证的语义身份，保留动画类型、进度、循环与实体关联；原始句柄另存 `audit/reanimation-handles.jsonl`。这不是 animation handle 原始位完全相同，具体 [规范化范围](docs/determinism-reanimation.md) 在 manifest 中明示。
- 原版粒子抖动会用堆地址重新播种 CRT。当前实验启用 [deterministic_particle_shake_v1](docs/particle-shake-determinism.md)，在两个确认过的调用点以验证后的完整粒子 ID 代替地址因子；原始种子、转换种子和调用序列均保存。这会改变该粒子的抖动序列，不能称为未经修改的原版逐位重放。
- 原引擎 replay 从相同初态重新执行真实请求，定位首个分叉。seek 从起点重算；目前不声称支持完整进程检查点或任意时刻直接恢复。
- 视频按游戏 tick 采样并流式送入 FFmpeg，不逐帧保存截图。原画来自引擎绘制表面；状态示意图始终标为非原版画面。JSONL 保存捕获元数据和像素 SHA256，原始像素送编码器。
- 本机已验证隐藏窗口初始化、真实种植/铲除、100 次单步与一次 100 步等价、短程录像开关等价，以及当前确定性模式下两次冷启动的完整 1000 tick 轨迹相同。完整两旗、十次冷启动与严格实验就绪仍以验收报告为准，不能由这些短程通过项推定。

## 文档与验收

- [雾夜两仪详细教程](docs/雾夜两仪详细教程.md)
- [后台启动与隔离](docs/launcher.md)
- [运行时协议](docs/runtime-protocol.md)
- [Python 客户端与 REPL](docs/client.md)
- [录制与视频](docs/video.md)
- [原版引擎重放](docs/engine-replay.md)
- [评测与就绪门槛](docs/evaluation.md)
- [后台实机验收记录](docs/headless-validation.md)
- [实施 issue 与验收规则](docs/implementation-board.md)
- [LLM 控制设计](docs/LLM交互控制设计.md)
- [原版确定性重放器提案](docs/原版确定性重放器提案.md)

```powershell
$env:PYTHONPATH = 'src'
python -m unittest discover -s tests -v
ctest --test-dir build/cmake --output-on-failure
```

公开 CI 只构建公开代码和运行不依赖游戏的测试；真实实验报告来自本机原版引擎。AvZ 固定为 `c42676c269b5b482a1eb9203a5b979e9d8a2a5c7`。本项目遵循 GPL-3.0；游戏及第三方依赖许可独立。
