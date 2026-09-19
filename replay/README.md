# 复盘、原引擎重放与视频

项目已实现三种不同用途的记录消费方式：

| 入口 | 用途与范围 |
|---|---|
| `./tools/lvz.ps1 export <run>` | 将采样事件嵌入单文件 HTML，可离线播放、拖动；画面是状态示意图 |
| `./tools/lvz.ps1 compare <left> <right>` | 比较日志中的采样 `state` payload；不执行游戏，不证明采样间状态一致 |
| `python -m llm_vs_zombies.engine_replay` | 在新游戏进程中重做初始化、执行已记录请求，比较实际结果及每个已捕获的更新边界 |

`replay/viewer/` 是 HTML 查看器；原引擎重放实现位于 [engine_replay.py](../src/llm_vs_zombies/engine_replay.py)，严格证据读取位于 [audit_compare.py](../src/llm_vs_zombies/audit_compare.py)。这套动作重放不再次调用 LLM，也不是 AvZ `AReplay` 的存档播放器。

## 封包与冷启动重放

标准录制保存真实初始化身份、配方和 `capture_initial()` 初态，以及完整 SessionTrace 与正常关闭的原生 `audit/`。仅有参考存档或普通 REPL 日志不足以封装严格轨迹；初始化与关闭步骤见[重放接口](../docs/engine-replay.md)。

在项目根目录运行，输出目录必须尚不存在：

```powershell
$env:PYTHONPATH = 'src'
python -m llm_vs_zombies.engine_replay pack `
  experiments/runs/source/decisions/session.jsonl `
  experiments/runs/source/audit work/source-trajectory
python -m llm_vs_zombies.engine_replay inspect work/source-trajectory
python -m llm_vs_zombies.engine_replay run work/source-trajectory work/cold-replay `
  --initializer my_experiment:cold_start
```

`my_experiment:cold_start` 是使用者提供的 context manager：以锁定输入启动新进程，应用源轨迹初始化配方，交出实际 `ReplaySession`，并负责关闭记录和清理进程。命令不会猜测初始化方式。[评测运行器](../docs/evaluation.md)已组合 launcher、初始化、策略录制和冷重放，可作为完整实验入口。

只有原生权威事件时，可用 `pack-native initial.json <audit目录> <新输出目录>`；`initial.json` 必须来自真实 `capture_initial()`。这条路径没有 SessionTrace 中的捕获调用证据，不能证明原录制未发生画面捕获；包含视频干预的实验应保留并封包 SessionTrace。

重放核对构建、目标、模式、文件哈希、实际 B(0)、动作顺序与成功/失败结果，再逐个核对 `pre_step/post_step` 完整已捕获状态和声明模式要求的原始旁证、出生/粒子调用、绘制回执及关闭健康。JSON Patch 展开用于定位首个实际差异；`replay-report.json` 保留差异路径与真实返回值。文件缺失、未知执行结果或不健康封口都会拒绝成功，不能以预期结果补写实际结果。

seek 使用同一入口，例如加 `--target-tick 500`，从 B(0) 执行全部必要前缀，停在第一次 B(500)，不提前执行随后同 tick 的动作或零时钟终局调用。Python API 的 `on_takeover` 可在该处接管并记录父轨迹身份。这是从头重算；目前没有任意时刻的完整原生检查点恢复能力。终局还须满足原生调用进入/返回及完整边界证据合同，旧日志不会被补造为新格式。

## 实验材料与封存

常见材料包括：

```text
manifest.json          实验身份、来源、状态、能力与封存清单
config.json            冻结配置
events.jsonl           便于查看的采样状态与事件
inputs/                场景、源码快照、输入等材料
observations/          模型实际可见观察与初态证据
decisions/             SessionTrace、策略/模型请求响应、动作与运行旁证
audit/                 逐边界摘要、状态差分、原始旁证和关闭健康
video/                 编码视频、帧映射、捕获元数据与编码器日志
checkpoints/           可选附件；文件存在不代表支持完整恢复
exports/review.html    可重建的采样状态复盘页面
```

录制及视频写入结束后，`./tools/lvz.ps1 finalize <run> --outcome <结果>` 封存材料；`validate <run>` 核验记录顺序、清单和文件哈希。封存校验与严格引擎重放是两层不同检查，正常封存的失败诊断不因此变成重放通过。

## 流式原画与状态图视频

[StreamingVideo](../src/llm_vs_zombies/video.py) 和 [TickRecorder / RemoteGameFrameProvider](../src/llm_vs_zombies/recording.py) 已实现游戏 tick 对齐的录像。在显式固定绘制模式中，`capture_frame` 读取同一版本的原画缓存，不额外推进或绘制；RGB/BGR 字节经管道送 FFmpeg，无需逐帧保存 PNG。帧映射与会话日志保留版本、像素哈希和失败信息，慢放按游戏 tick 计算，等待 LLM 的墙钟时间不会延长视频。

原画录制通过 Python API 接入；离线状态图视频可用 `python -m llm_vs_zombies.video <run> <output.mp4> --stride 10 --slowdown 2`。状态图明确标为非原版画面，视频也不能替代状态审计。完整用法、缓存失效、漏帧与关闭语义见[视频文档](../docs/video.md)。

当前 044 已通过 5,000 tick 冷启动重放及关闭、窗口覆盖验收；新终局来源已记录 6,163 个时钟步、6,164 次调用，终局冷重放仍待执行。完整两旗和十次冷启动门槛尚未通过。已采集字段一致不等于全部游戏内存确定，更不代表未经修改的原版逐位一致；明确模式及历次成功/失败范围以[实机记录](../docs/headless-validation.md)和[审计覆盖](../docs/determinism.md)为准。
