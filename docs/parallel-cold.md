# 并行冷重放

实验计划的 `cold_workers` 默认是 `1`，可显式设为 `2`。它只控制同一种子的冷重放并发数；源策略录制、不同种子和恢复探针仍顺序执行。两种设置使用相同的 `run_cold_attempt`、原版 replay 和验收门槛。

## 使用

在 Windows 项目根目录生成计划：

```powershell
$env:PYTHONPATH = 'src'
python -m llm_vs_zombies.evaluation plan work/parallel-smoke.json --seeds 42 --tick-budget 5000 --cold-starts 3 --cold-workers 2 --audio-mode sound_effects_allocation_none_v1
python -m llm_vs_zombies.evaluation run work/parallel-smoke.json --output experiments/runs/parallel-smoke-001
```

`--cold-starts 3` 表示一次源录制加两次独立冷重放，因此可以同时存在两个 cold worker。仅设置 `--cold-workers 2` 不增加重放次数：默认 smoke 仍只有一次 cold。strict 仍要求每种子一次完整两旗源录制加九次 cold；可在 strict 的 `plan` 命令中添加 `--cold-workers 2`，其余要求不变。

源必须先通过录制、归档、宿主身份、窗口和资源门槛；strict 源还必须实际完成两旗。source、每次 cold 和 recovery 都核验自己实际加载的 runtime，拒绝任何声明 `test_fixture` 的生命周期测试 DLL。测试夹具可以通过专用录制/重放工具保存，但不能认证生产实验就绪。

## 隔离与比较

默认 `1` 直接在当前宿主运行 cold，不额外启动 Python worker。`2` 为每次 cold 启动一个隐藏 Python 宿主、独立 run 目录和独立 Windows Job Object；其自有游戏与观察器后代归属该 Job。宿主完成 PID/创建时间绑定和 Job 归属确认后才可启动游戏。停止一个 worker 不会用共享 Job 终止另一个。

每个 worker 独立核验同一源归档、轨迹身份和全部原请求、状态、旁证、关闭 health；不是只比较两个 worker 相互一致。请求顺序、预算、expect 和 official replay 的 ID 映射不变，不为并发拆分预算或重试动作。结果按 repeat 编号汇总。

父进程先串行预热原生 FNV 摘要缓存，随后锁定缓存与 Python 源码 SHA256。子进程只加载已核验缓存，不并发编译；缺失或变化即失败。每个 run 仍保存实际导入的宿主源码身份，包括作为 `__main__` 运行的 worker 模块，并要求与源码 ZIP 相符且全程未变。

## 失败、资源和证据

首次观测到 cold 失败后停止派发尚未开始的任务；已经运行的 worker 继续原请求流程、关闭与证据保留。父进程等待真实退出和自有 Job 清空，再返回汇总结果。进程异常或安全期限耗尽会走失败清理；强制终止不能替代原生健康关闭或伪造归档封口。

每个 cold 的墙钟预算从该 attempt 开始计算，包含源轨迹读取、初始化和实际 replay，不包含排队时间。最后一个请求超过预算时仍先核验其真实尾部，再报告资源失败。宿主另有有限安全期限：cold 墙钟预算加两倍 `max(600, timeout_seconds)` 秒和 3600 秒读取/清理余量，起点为宿主派发；该余量不使超预算的 cold 通过。

两局会竞争 CPU、内存和磁盘。磁盘门槛检查实际剩余空间，不进行容量预留；两个 worker 可能在相近时间看到同一份可用空间。资源不足、窗口漏采或 I/O 故障都保留真实失败，不删除旧档。并发选项不放宽 25ms 窗口目标间隔和 250ms 最大间隔。

suite 的 `seed-<seed>-cold-workers/` 保存各 worker 的任务绑定、PID/创建身份、标准输出/错误、回执及 `summary.json`。成功要求真实 exit 0、空 Job、有效封存归档、所有逐 cold 门槛和对应文件 hash；`equal:true` 本身不够。任何 source/cold/recovery 的失败或未验证门槛仍进入总报告。

`measured_positive_rpc_overlaps` 只记录已绑定 official replay 的正推进 RPC 墙钟区间交叠，不声称 CPU 指令同时执行，也不保证速度提升。并发支持和确定性结论均以本机实际实验报告为准；它不扩大已捕获状态的覆盖范围，也不替代完整两旗/十次冷启动的严格验收。

## 首次真实并行集成发现的门槛配对问题

首次真机运行同时暴露两个既有配对缺陷，两者都不是并发本身造成的，但都会被父进程对 worker 回执的严格复核（以及新增的来源侧复核）拒绝：

1. **scenario 门槛哈希过期。** `add("scenario", ..., observations/initial.json)` 原先在 `apply_recipe` 之前记录，而 `apply_recipe` 会把 B0 绑定的观察写回同一个 `initial.json`。source 侧从未被二次核验，因此这个过期哈希此前一直存在于报告中；cold worker 侧由父进程复核哈希，于是失败于 `worker gate artifact changed/foreign`。现在 source 与 cold 都在 `apply_recipe` 之后记录该门槛，所记字节即最终文件。
2. **缺少本地门槛文件复核。** 父进程此前只复核 worker 回执里的产物。现在 `verify_run_artifacts` 在每次 source 会话结束、以及每次 cold attempt 返回前，重新读取该 run 目录内所有门槛声明的产物并与记录哈希比较；run 目录之外的产物（保留回执、seed case、replay 报告）由各自写入者负责，不在该检查内。任何过期或被替换的本地产物都会使该 attempt 失败，而不是留下一个哈希不再成立的通过门槛。

这两项修复只改变门槛记录与复核时机，不放宽任何门槛，也不追改旧档案中已存在的过期哈希。旧档保留原样，其结论不升级。
