# logger

[avz/recorder.cpp](avz/recorder.cpp) 与常驻控制器、确定性审计和原画捕获一起编译为 32 位 `build/recorder.dll`。启动器将其注入本轮独立游戏进程；只有游戏线程调用 AvZ 或读取游戏对象。构建入口为项目根目录的 `./tools/build-avz.ps1`，启动与隔离见[launcher 文档](../docs/launcher.md)。

## 两层日志

实验根目录的 `events.jsonl` 用于采样复盘。每条 UTF-8 JSONL 包含 `schema_version/run_id/seq/segment/tick/phase/kind/payload`，定义在 [event.schema.json](schemas/event.schema.json)。

- `seq` 从 0 连续递增，`segment` 随新战斗或原生游戏时钟回退递增；这里的原生 `tick/segment` 不能直接当作控制器的相对 `tick/epoch`。
- 坐标向外统一为行列从 1 开始；实体 ID 需结合运行与阶段识别。
- `phase=avz_callback` 标明采样位置，不能据此声称是每次原生更新完成后的状态。
- `state` 保存植物、僵尸、卡片、阳光、场景、波次和刷新倒计时，默认每 10 tick 一次。
- `zombie_first_observed/zombie_no_longer_observed` 每个新 tick 检查存活集合，同帧创建又销毁可能观察不到；它们不同于下述精确初始化钩子。
- `wave_table` 记录 segment 开始瞬间的类型槽位，可能属于加载中的旧轮。实际实验波表应读取 `capture_initial.state.board` 的 B(0) 数据，不能把旧事件当作 ready 局类型表；详见[两阶段播种与波表边界](../docs/determinism.md)。类型表也不决定完整出生行路、位置和随机属性。

严格重放使用独立的 `audit/`，由 [determinism/audit.cpp](../determinism/audit.cpp) 写入。它记录每个受控原生更新的 `pre_step/post_step`、完整已捕获状态摘要和 JSON Patch，以及实际协商模式要求的动画句柄、粒子种子、原生调用、音效原始计数等旁证。`zombie_initialized` 来自真实初始化器出口钩子，保存出生参数、属性与 RNG 证据；首次可见采样不冒充精确出生。各项覆盖和未覆盖范围由 manifest 与[审计文档](../docs/determinism.md)明示。

## 动作、模型与录制入口

常驻命名管道、Python `Client` 与 REPL 已实现。模型或普通 Python 策略通过统一的 `commit/advance` 执行器提交有限动作和帧预算，等待时游戏停在受控边界，无需每次决策重新编译 DLL。请求、响应、失败动作及其顺序由 SessionTrace 保存，原生执行与状态另行审计；详见[运行时协议](../docs/runtime-protocol.md)和[评测入口](../docs/evaluation.md)。

`lvz::Plant/Shovel/Note` 仍可供 C++ 集成使用；包装函数的铲除 `count_decreased` 是检测值，不是目标销毁证明。外部脚本绕过统一入口、直接调用裸 `ACard` 的操作不会自动成为完整可重放的客户端请求。项目没有全局鼠标输入录制器；模型提示与回答需由调用方显式记录，默认不会调用模型服务。

严格来源还需在初始化完成后调用 `capture_initial()`，并保留会话日志与原生审计。结束时先用当前版本调用 `stop_recording`，等待关闭、完成视频编码及进程清理，再执行：

```powershell
./tools/lvz.ps1 finalize experiments/runs/my-run --outcome completed
./tools/lvz.ps1 validate experiments/runs/my-run
./tools/lvz.ps1 export experiments/runs/my-run
```

以上命令在项目根目录运行。引擎轨迹封包、冷启动重放与首处分叉诊断见 [replay README](../replay/README.md)；`lvz compare` 仍只是采样 state 比较。

## 写入和失败边界

采样 logger 使用游戏线程缓冲批量写盘，累计 64 KiB、周期边界、请求完成或正常关闭时 flush；严格审计另有受控边界与初始化回执的 flush 合同。没有把游戏对象指针交给后台线程，磁盘慢时仍可能阻塞游戏线程。状态差分与旁证流减少完整快照重复写入，但长局仍需足够磁盘预算。

`capture.lock` 防止并发写入同一实验，正常关闭写入 `capture.closed`。异常退出可能留下锁、截断尾行或缺少关闭健康；这些材料应保留为失败诊断，不能通过删末行、删锁来伪装完整。封存检查绑定文件哈希，严格读取器还检查逐边界配对、旁证对应和关闭健康。

模型观察与完整审计保持独立：只看截图的实验不能把审计读到的雾后信息交给模型。当前已通过范围及完整两旗、十次冷启动的待验收门槛见[实机记录](../docs/headless-validation.md)，日志能力本身不等于完整游戏确定性证明。
