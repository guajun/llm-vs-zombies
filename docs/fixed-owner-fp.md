# 游戏线程固定浮点控制模式

`fixed_owner_fp_v1` 是此 runtime 明确声明的实验语义。它在游戏主线程、场景载入前设置一次 x87 控制字 `0x027f`（53 位有效精度、最近舍入、异常屏蔽）和 MXCSR 控制位 `0x1f80`。随后只检查，不在漂移后静默恢复。

旧记录曾在最早初态采样出现 x87 `0x007f` 与 `0x027f`，伴随雾偏移及动画时间等实际标量位差异。记录没有观察到最初写入控制字的调用者，也不能据此归因为并行游戏。此模式建立可核验的初始化条件，不采用浮点容差、不删状态字段、不把旧失败改成成功。

## 激活与持续检查

`initialize` 先验证场景、卡序、seed 参数，并完成 title-to-menu 转换。仅当实际 owner thread 位于 UI1 且没有 Board 时激活模式；激活先于 `SeedRng` 和异步 `AEnterGame`。每个录制生命周期只允许一次激活，同 ID 重试仍使用原请求 journal，不重新写 FP 状态。新模式不是一个可在已有战斗上任意重设的 RPC。

激活使用 `fldcw` 和 `ldmxcsr`，MXCSR 控制 mask 为 `0xffffffc0`，保留低六位 sticky exception flags；不使用 `finit`、`fnclex` 或重置 x87 栈。原始控制字、状态字、完整 MXCSR、owner/实际线程、UI/Board 条件、请求 ID 和实际 pre-version 都写入激活回执，立即读回，并在成功 ACK 前 flush 到 audit。

主循环、AvZ `RunTotal` 后、异步场景载入/选卡到 ready、原版更新前后、warm 与逐帧绘制前后持续检查。内部函数临时改舍入模式但在检查边界前恢复，不算持久漂移。任何边界控制位不符、错误线程或计数溢出会锁存首次故障；稍后恢复原值也不能清除故障。

原版更新返回后才发现的漂移保留实际 entered/returned 调用数及测得的时钟增量，然后停止推进。永久故障不会阻断 status、pause 和录制关闭；关闭在 owner thread 重新采样，保存 `healthy:false` 的真实证据。关闭后的文件 I/O 错误仍按原规则拒绝成功封口。

## 身份与证据

- `hello.game.fixed_fp` 与 native manifest 保存相同稳定模式配置，`capabilities.fixed_owner_fp_v1` 明确声明支持。完整目标身份包含此配置，不能把无该配置的旧源当成新模式源。
- `fp_environment_activated` 事件保存原始激活回执。initialize response 的 `fixed_fp` 为这份回执；launcher 在实际 ready 后再次核对它没有被异步载入替换。
- `audit_snapshot.fixed_fp` 提供激活回执与当前 monitor health。recipe 只存稳定配置；initial marker 的 `fixed_fp_origin` 和初始化 evidence 保留本进程完整实际证据。跨冷启动允许激活前原始 CW、线程 ID、初始化检查次数不同，但两边都必须真实激活并读回同一目标。
- `state.fp_environment` 仍逐字段保存真实控制值。每个 pre/post 同一次读取的原始 x87 状态和完整 MXCSR 写入 `audit/fp-environment-raw.jsonl`，通过 seq、kind、version、engine_call_id 与该状态绑定。sticky flags 是本机原始旁证，不被误充为跨进程可比控制位。
- `fp_environment_closed` 位于 `engine_call_closed` 后、音效/绘制/粒子/出生关闭事件前。严格读取器要求已激活、一次激活、owner 一致、零错误、目标控制读回、所有 pre/post/raw 条数闭合，并核验 warm/正常绘制次数。原更新前检查可发生在最终不调用更新的边界，因此该计数可以大于实际返回后的检查；不把检查次数当成游戏调用数。

声明新模式但缺回执、raw 旁证、健康尾部或初态配置会被拒绝。未声明新模式的旧档仍按原合同读取，不补造激活或自动升级。专用 `test_fixture` 的身份和 public runner 拒绝规则不变。

## 暂停证明的冻结对象

每次 `audit_snapshot` 都会在 `fixed_fp.health.checks` 里真实新增一条 snapshot 检查，主循环检查也会随宿主墙钟推进。因此**不能再用整份 snapshot 的字节相等**作为“暂停期间状态未变”的证明，否则会把 monitor 的正常计数当成模拟状态漂移。#22 集成把公开 runner 的暂停探针改为逐字段比较已捕获模拟 `state` 与 `version`，再单独核验浮点激活回执未变、after 侧 monitor 仍健康且 `last_raw` 仍在目标控制位；probe 记录实际比较的字段，不隐藏跳过的字段。未声明该模式的旧 runtime 仍只比较 `state` 与 `version`。旧档不重写，旧失败不升级。

此模式只约束实际游戏 owner thread 在声明边界的控制状态。激活之前的加载线程、全局资源及未捕获引擎状态仍不属于完整确定性保证。需要使用新 DLL、新来源和完整捕获初态进行实际冷重放验收；原生与 Python 单元测试本身不能证明任意一局重放一致。
