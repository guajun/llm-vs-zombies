# 游戏 tick 对齐的流式视频

视频是实验记录的可选附件。动作、观察、随机状态、审计和结果仍由结构化日志保存；视频不能替代原版重放或确定性校验。

本实现直接将 RGB24/BGR24 字节送入 FFmpeg 管道，不逐帧生成 PNG，不用墙钟录屏代替模拟时间。没有 FFmpeg 时仍保存帧映射和 disabled 状态，实验可以继续。FFmpeg 启动、写入和结束失败会记入文件；不会报告录像完成。

## 已实现能力

`src/llm_vs_zombies/video.py` 提供 `StreamingVideo`。一个视频对应一个 epoch、一个固定取样网格。默认游戏时钟每秒 100 tick，每 10 tick 一帧，因此视频 10 fps。`slowdown=2` 会以 5 fps 播放这些帧，实现两倍慢放；等待 LLM 或暂停窗口多久，都不改变视频时长。

`src/llm_vs_zombies/recording.py` 提供可插拔 `FrameProvider`、`TickRecorder` 和纯 Python 的状态图绘制器：

| 来源 | 当前能力 | 含义 |
|---|---|---|
| `state_visualization` | 可运行、已编码验收 | 根据记录的植物、僵尸位置等字段绘图，画面上永久标注非原版画面 |
| `original_game_frame` | 当前固定绘制模式的1,000 tick录像对照已通过 | 来自指定游戏边界的真实缓存原画；验收仅覆盖已测引擎和显示环境 |
| `synthetic_fixture` | 测试用 | 用纯色帧验证编码、色彩通道和时间映射 |

隐藏窗口的 `BitBlt`、`PrintWindow` 或桌面录屏可能得到黑屏、旧帧或其他窗口。现有模块不会因为有窗口句柄就声明原画捕获可用；RPC 适配器还会拒绝全黑帧，并记录原因。但“不是全黑”不能证明画面新鲜，真实渲染端仍须携带并验证对应的游戏版本。

## 离线生成状态图视频

在项目目录运行：

```powershell
$env:PYTHONPATH = 'src'
python -m llm_vs_zombies.video experiments/runs/demo-verified work/video-demo.mp4 --segment 0 --stride 10
python -m llm_vs_zombies.video experiments/runs/demo-verified work/video-demo-slow.mp4 --stride 10 --slowdown 4
```

仅绘制选定 segment。离线 epoch 标识取自 `record.segment`，不能与运行时 epoch 混为同一标识。状态记录必须按 tick 严格递增。网格之外的样本会记录为 `observation_not_on_video_grid`；没有样本的网格点保持上一张已知图，不进行未来信息插值。

已用项目现有合成记录验证：`work/video-smoke/diagram.mp4` 为 640×400、10 fps、121 帧、12.1 秒。该文件明确标记为状态图视频。

## 实时接入

调用方先完成 `advance()`，取返回的最终 observation，再捕获同一版本的画面。视频模块不自行推进游戏。

```python
from pathlib import Path
from llm_vs_zombies.video import StreamingVideo, TickStamp
from llm_vs_zombies.recording import TickRecorder, StateVisualizationProvider

obs = game.observe()
provider = StateVisualizationProvider(640, 400)
stamp = TickStamp.from_observation(obs)
video = StreamingVideo(
    Path(run_dir) / "video" / "state.mp4",
    width=640, height=400, epoch=stamp.epoch, start_tick=stamp.tick,
    tick_stride=10, source="state_visualization",
    provider=provider.capabilities(),
)
with TickRecorder(provider, video, Path(run_dir) / "video" / "recording.jsonl") as recorder:
    recorder.record(obs)
    for _ in range(100):
        result = game.advance(10)
        if result["executed_ticks"] != 10:
            break  # 检查退出原因，必要时另开视频段；不能伪造完成了 10 tick
        obs = result["observation"]
        recorder.record(obs)
```

使用事件条件提前停止时，最终 tick 可能不在网格上。应保留该次结构化观察，再决定推进到下一个采样点或结束当前视频。不能擅自将帧贴到一个不同的 tick。

同一个 tick 的重复提交、倒序、跨 epoch、错尺寸、错像素格式、错来源均拒绝。epoch 改变时另开 MP4。每帧必须恰好为 `width * height * 3` 个紧密排列字节；RPC 适配器支持有行填充的原始缓冲区和上下方向转换。

## 原画 capture_frame RPC 接入合同

原生部分已接入 runtime 的 `capture_frame` 方法，`RemoteGameFrameProvider` 使用以下合同。调用前检查 `hello.capabilities.capture_frame`；该字段表示目标签名与适配器匹配，`capture_frame_live_validated=false` 则表示尚未完成实机原画验收，不能据此宣称所有显示模式均可用：

```json
{
  "method": "capture_frame",
  "params": {"format": "bgr24"},
  "expect": {"epoch": 1, "tick": 100, "revision": 0}
}
```

当前 `deterministic_draw_schedule_v1` 模式的成功 result 包含：

```json
{
  "capture_ok": true,
  "source": "original_game_frame",
  "version": {"epoch": 1, "tick": 100, "revision": 0},
  "width": 800,
  "height": 600,
  "pixel_format": "bgr24",
  "row_stride": 2400,
  "origin": "top_left",
  "method": "cached_controlled_engine_frame",
  "mode": "deterministic_draw_schedule_v1",
  "frame_version": {"epoch": 1, "tick": 100, "revision": 0},
  "forced_render": false,
  "used_3d": false,
  "known_rng_unchanged": true,
  "game_clock_before": 100,
  "game_clock_after": 100,
  "pixels_base64": "..."
}
```

固定绘制模式在初始化时预热一次，并在每个经验证的时钟步后执行一次原版绘制；`capture_frame` 只复制该版本缓存，不增加绘制调用。暂停不自主绘制，同tick动作使旧缓存失效，随后应完成受控推进才能取新缓存。绘制回执和状态变化仍进入逐边界审计；IPC 工作者不直接读取游戏对象。旧档可能使用 `engine_widget_draw_to_directdraw_surface` / `forced_render:true`，其捕获会执行绘制，不能按新缓存合同重新解释。

`capture_frame` 要求暂停、战斗有效、记录未关闭、没有待完成推进，并精确匹配 `expect`。若绘制改变游戏时钟或已确认 RNG，controller 拒绝像素并递增 revision；原始绘制错误也可能使旧观察失效。适配器在捕获失败后尝试 `observe` 刷新同一连接，并将结果保存在 `provider.last_observation`；刷新失败则为 `None`，需要重新连接。使用独立动作连接时，恢复实验前仍应在动作连接调用 `observe`，不能继续使用旧的 `expect`。

图像结果单独保留最近四个用于去重；更早的相同请求 ID 返回 `request_result_expired`，不会重新绘制。像素不写入 native audit 或动作缓存，视频文件与帧映射保存其内容和哈希。

可使用独立的 capture 客户端连接，避免可选视频的超时影响动作连接；能力直接取自实际 hello：

```python
from llm_vs_zombies.recording import RemoteGameFrameProvider

provider = RemoteGameFrameProvider.from_hello(
    lambda method, params, expect: capture_client.request(method, params, expect=expect),
    capture_client.hello_result,
)
```

将这个 provider 交给上文的 `TickRecorder`，并把视频尺寸设为800×600、像素格式设为`bgr24`、来源设为`original_game_frame`即可录制原画。记录网格须与实际暂停边界一致；原画录像当前通过 Python API 接线，`evaluation plan` 尚无原画录像开关。

041/043实机对照使用每100 tick一帧，共10帧、1 fps、10秒；1,000次单步加录像与一次批量推进的全部2,000个捕获边界、71次受控出生、40,764次粒子调用相同。B0三次重复缓存读取相同，1/5秒暂停完整状态相同，视频实际解码及归档校验通过。完整身份与限制见[实机证据](headless-validation.md#显式诊断计数起点与录像复验041043)。这不证明任意显示驱动、锁屏或无桌面环境均可用。

4 MiB 的现有管道帧限制足够容纳 800×600 BGR24 的 Base64 结果。更高分辨率可扩展为共享内存/二进制通道，但首版不需要传指针到 64 位客户端。

## 产物及失败语义

每个 `name.mp4` 配套：

- `name.frames.jsonl`：每个视频帧的序号、epoch/tick/revision、原始来源版本、像素 SHA-256、视频 PTS、模拟时间，以及 captured/generated/held/blank 状态。
- `name.video.json`：帧率、慢放比、编码命令、来源、帧数、丢帧数、失败原因和结束状态。
- `name.ffmpeg.log`：编码器 stderr；FFmpeg 缺失时不会创建此文件。
- 使用 `TickRecorder` 时另外保存调用方指定的 `recording.jsonl`，记录能力、每次捕获结果和关闭状态。

漏采 tick 会用上一帧补位；没有上一帧则用黑占位，并明确记为 dropped/blank。这样缺失不会压缩实验时间。含补位的视频状态为 `partial`；全是补位则为 `no_content_frames`，不能声称捕获成功。首帧及最后一帧各占一个采样间隔，因此 N 帧时长为 `N / fps`。

原始帧写入失败后不继续喂编码器。`TickRecorder` 默认将视频标为失败并允许调用者继续结构化实验；编码结束失败同样记录。错误尺寸和排序检查发生在发送字节之前。默认最多补 10,000 个缺失网格点，避免错误 tick 造成无界编码；超过时要求显式开始新视频段。

视频流是同步、有期限的：每次输入使用持久写入线程，15 秒未完成则结束 FFmpeg。游戏应已暂停在完成边界，编码墙钟耗时不会多推进游戏。此方案节省逐帧图片压缩与落盘开销，并不使原始像素传输零开销。

生成视频应在实验 `finalize` 前完成，使主项目能将附件加入校验清单；不要向已封存的实验增补或覆盖文件。

## 音轨来源

视频本身不带音轨（`-an`）。音频由 tick 驱动的虚拟音频设备模型离线生成：`tools/offline_soundtrack.py` 把音效事件流（资源、起播 tick、音量/声道、真实抽到的随机变体）渲染成与游戏 tick 网格对齐的 44.1 kHz 立体声 WAV，并写出可核对的覆盖清单（帧数、union/silence、每段边界、payload 哈希）。`--video-manifest` 会在写盘前核对 `timing_basis`、`ticks_per_second` 与 `start_tick` 偏移，`--mux` 用 `-c:v copy` 把音轨接到既有 mp4 上，不改动画面编码。语义取舍与声明见 `docs/虚拟音频设备与离线音轨.md`；没有 FFmpeg 时保留 WAV，工具明确报错而不是静默降级。

## 验证

```powershell
$env:PYTHONPATH = 'src'
python -m unittest discover -s tests -p test_video.py -v
```

九项测试包含真实 FFmpeg 编码与 ffprobe 验证、RGB/BGR 解码通道检查、慢放时长、缺帧占位、编码器被终止、无 FFmpeg、帧大小/顺序/epoch 拒绝，以及原画 RPC 的版本/步长/黑屏检查。测试不依赖游戏，不代表原版渲染捕获已通过验收。

## 原生绘制表面适配器

`recording/native_capture.hpp/.cpp` 实现了固定原版引擎的画面捕获，已经加入 DLL 构建。runtime 在游戏线程的暂停边界调用：

```cpp
auto picture = lvz::recording::CaptureOriginalFrame(establishedGameThreadId);
// picture.ok 为 false 时返回明确错误；不能发送成功的 capture_ok。
// 成功时 pixels 是 width*height*3 的 BGR24，origin 为 top_left。
```

调用链为：保存各 Widget 的 dirty 标志并强制置脏 → 原版 `WidgetManager::DrawScreen` → 如果使用 3D 则提交原版 D3D 绘制队列 → 锁定原版 `mDrawSurface` → 将 RGB565/24/32 位表面转换为紧密 BGR24 → 解锁并恢复 dirty 标志。不会调用 `DrawDirtyStuff` 的墙钟节流和窗口呈现逻辑，不调用 `BitBlt`、`PrintWindow`、`SetForegroundWindow` 或任何键鼠操作。

此处布局和调用约定均与项目锁定的原版 EXE 核对，文件 SHA-256 为 `f9669af338964787a3785a7895791297d599295b8bb669b0db49443f736a1322`：

| 核对点 | 原版地址/偏移 | 二进制证据 |
|---|---|---|
| 应用 → WidgetManager | App + `0x320` | `0x54c74b` 取指针、压栈并调用 DrawScreen |
| WidgetManager → screen image | manager + `0x60` | `0x538f5a` 构造绘制 Graphics |
| 应用 → DDInterface | App + `0x36c` | `0x54c7c6` 和 `0x54bb11` |
| screen image | DD + `0xce8` | `GetScreenImage` 的 `0x561400` getter |
| 真正 draw surface | DD + `0x68` | `0x5630ed` 取表面，使用 108 字节 DDSURFACEDESC 调用 Lock |
| 绘制 widgets | `0x538eb0` | manager 为栈参数；函数在 `0x539133` 使用 `ret 4` |
| 提交 D3D | `0x568ec0` | `0x563011` 以 ESI 传入 DD + `0x30`；函数调用 EndScene |
| 原版图面模式 | DD + `0x38` / `0xcfc` | 3D / video-only 分支分别在 `0x562fcc`、`0x56354c` |

适配器运行前比较实际函数指令签名，拒绝其他版本或不兼容补丁；要求 32 位、原版基址、活跃战斗、已初始化 renderer、800×600 逻辑表面，并检查调用线程。表面锁定使用有限重试，不无限等待 GPU。当前明确拒绝 video-only 的另一块 secondary 表面，避免错误地读取旧 draw surface。

原版候选源码提供了交叉验证：它自己的 `TakeScreenshot` 也读取 `mDrawSurface`；`DrawScreen` 可独立绘制到框架图像；`DDInterface::Redraw` 在呈现前提交 D3D 队列。[TakeScreenshot/DrawDirtyStuff](https://github.com/ruslan831/PlantsVsZombies-decompilation/blob/8a2d121899ba5cb4df644cd7d2e4c1aaf88dd238/SexyAppFramework/SexyAppBase.cpp)、[WidgetManager](https://github.com/ruslan831/PlantsVsZombies-decompilation/blob/8a2d121899ba5cb4df644cd7d2e4c1aaf88dd238/SexyAppFramework/WidgetManager.cpp)、[DDInterface](https://github.com/ruslan831/PlantsVsZombies-decompilation/blob/8a2d121899ba5cb4df644cd7d2e4c1aaf88dd238/SexyAppFramework/DDInterface.cpp)。代码按本机二进制证据实现，没有将整个候选框架源码重新编入。

捕获前后还比较 GameClock、完整全局 MT 数组及游标、游戏线程 CRT 随机状态。若发生改变，会拒绝该帧并报告错误，**不会偷偷恢复 RNG 来掩盖副作用**；调用方此时应停止该次验收并调查。此检查不证明所有绘制副作用均不存在，runtime 仍须保存完整审计前后差异，并比较录像开/关轨迹。像素转换和非目标进程/线程拒绝已由独立 32 位 `tests/native_capture_tests.cpp` 验证；真实隐藏窗口、3D/软件模式及连续帧效果由集成后的游戏实验验收。
