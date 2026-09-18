# 实验就绪实施与验收

公开仓库：[guajun/llm-vs-zombies](https://github.com/guajun/llm-vs-zombies)。初始交付包含设计文档和可编译记录器；以下能力按独立 issue 实现并验收。

| Issue | 交付 | 依赖 |
|---|---|---|
| [#1](https://github.com/guajun/llm-vs-zombies/issues/1) | 常驻 IPC、游戏线程动作、暂停与精确推进 | 固定 AvZ 与目标游戏 |
| [#2](https://github.com/guajun/llm-vs-zombies/issues/2) | Python REPL、客户端与决策记录 | #1 协议 |
| [#3](https://github.com/guajun/llm-vs-zombies/issues/3) | 版本适配、RNG 与逐帧分叉诊断 | 目标二进制证据、#1 帧边界 |
| [#4](https://github.com/guajun/llm-vs-zombies/issues/4) | 后台运行、用户档隔离、启动注入与场景初始化 | #1、#3 |
| [#5](https://github.com/guajun/llm-vs-zombies/issues/5) | 游戏时间视频流与实验归档 | #1 帧号、#2 记录 |
| [#6](https://github.com/guajun/llm-vs-zombies/issues/6) | 原版动作重放、分叉报告与从头快进定位 | #1–#4 |
| [#7](https://github.com/guajun/llm-vs-zombies/issues/7) | 实验运行器、CI 与真实游戏就绪报告 | 前述交付 |
| [#8](https://github.com/guajun/llm-vs-zombies/issues/8) | 长会话有界去重、原结果查询与容量故障下关闭 | #1 的真实长局发现 |
| [#9](https://github.com/guajun/llm-vs-zombies/issues/9) | 计划灰烬针对威胁核心植物的小丑 | 可重复的策略对照 |
| [#10](https://github.com/guajun/llm-vs-zombies/issues/10) | 固定绘制调度与只读缓存画面 | 原始绘制写入动画字段的实测发现 |
| [#11](https://github.com/guajun/llm-vs-zombies/issues/11) | 独立原生调用身份及零 GameClock 终局重放 | #10、完整实际调用与状态证据 |
| [#12](https://github.com/guajun/llm-vs-zombies/issues/12) | 定位相同更新内额外的 MT 随机消费 | 固定绘制版031实测分叉 |

用户优先要求 headless：目标为不抢焦点的后台原版引擎，所有实验控制通过有限 IPC 操作。保留隐藏窗口初始化与完全无窗口是不同能力。不得以自动点击窗口代替后台接口，也不得未经验证就声称去除绘制不影响随机轨迹。

2026-09-19：#1、#2、#4 已由主 agent 完成真实集成验收并关闭。#5 已通过独立真实录像对照验收：1,000 tick、2,000 个前后边界、71 次受控出生和 40,946 次粒子调用全部等价，视频和完整归档核验通过。粒子地址播种修复后，016→019 的 1,000 tick 冷启动重放也已通过；该模式明确改变原版粒子播种行为。#3、#6、#7 的完整两旗、长程重放与十次冷启动仍在验证，不能据短程结果宣称严格实验就绪。详见各 issue 和 [实机证据](headless-validation.md)。

后续验收：#8已由跨旧容量上限的短程压力、真实長局关闭及有界原生 fixture 通过并关闭。#10的新1,000单步/批量比较通过2,000个边界和71次出生，但5,000 tick来源的冷重放在post1,344出现绘制前的额外MT消费，由#12继续定位。#11代码及严格读取器已合入，经两个实现 subagent、独立审查和主 agent复核，221项Python与11项原生测试通过；真实零GameClock终局及其冷重放仍待新记录验收，issue保持打开。#9的候选策略仍未完成有效因果对照。

## 验收规则

实现 agent 交付代码、测试结果与已知边界；主 agent 单独检查接口和失败路径，并执行集成测试。issue 的开发完成与真实实验验收是两件事。仅当 issue 中的验收条件均满足才关闭。

- 公开 CI 不依赖游戏本体；使用协议、日志、重放与原生控制器测试。
- 本机验证使用自行提供的锁定游戏，私有运行目录不加入 Git。
- 完整实验就绪须具备后台启动、真实两仪场景、精确操作、完整记录及同轨迹重放证据。
- 10 次冷启动、完整两旗周期及暂停扰动是严格确定性门槛；有限字段相同不能代替完整能力声明。
- 检查点恢复未通过时使用从起点重算定位，并明确能力差异。
- 初始记录器测试已通过不表示上述功能已完成。最新状态以 issue 验收证据为准。
