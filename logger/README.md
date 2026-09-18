# logger

`avz/recorder.cpp` 编译为 32 位 `build/recorder.dll`。官方注入器加载该 DLL，由 AvZ 的游戏回调读取结构化信息。

## 记录协议

每条 UTF-8 JSONL 都包含 `schema_version/run_id/seq/segment/tick/phase/kind/payload`，模式定义在 `schemas/event.schema.json`。

- `seq` 从 0 开始，连续递增；`segment` 在新战斗或游戏时钟回退时递增。
- 坐标向外统一为行列从 1 开始，实体 ID 是游戏 ID，需和 run/segment 组合识别。
- `phase=avz_callback`，不提前宣称是游戏更新完成后的状态。
- `state`：植物、僵尸、卡片、阳光、场景、波次和刷新倒计时；默认 10 tick 一次。
- `wave_table`：每段开始时读取的各波类型槽位，不包含准确出怪行路或未来随机结果。
- `zombie_first_observed/zombie_no_longer_observed`：每个新 tick 检查存活集合；同帧创建又销毁可能观察不到。
- `action`：经包装函数执行的操作结果；铲除记录 `count_decreased`，它是检测值而非精确目标销毁证明。

## 开销与可靠性

当前实现为**游戏线程缓冲批量写盘**：累计 64 KiB、周期边界或正常关闭时 flush。没有把游戏对象指针交给后台线程，也没有每僵尸一条就 flush。磁盘慢时仍可能阻塞该线程；后续可增加有界异步队列。

`capture.lock` 防止多个原生记录器同时写入一个实验。异常退出可能留下锁与最后一小段未写入数据；不会把这样的记录自动标成完整。保留原始文件后检查缺行/截断，再单独恢复，勿为了通过校验直接删掉末尾记录。

`lvz::Plant/::Shovel/::Note` 是后续策略接入点。项目暂未安装全局鼠标拦截、生成函数钩子或 LLM 控制服务。已有外部脚本如果继续调用裸 `ACard`，其动作不会自动出现为 `action`，但局面采样仍可能观察到效果。

模型观察与完整审计日志应分开：只看截图的实验不能把这里读到的雾后信息偷偷交给模型。
