# #111 阶段 D：冻结窗口真机验收

2026-09-26，principal 在 `39d0ae7dca07f3a1ced9f77069af1e382dfb47ff` 上独立验收通过。验证器实现为 `6f04b8798003946a497f47a64bdd544f3f1f3582`；随后仅补充本报告与独立证据，不修改已验收的生产代码。

## 运行身份与窗口

- 四条轨迹为 `issue111-d-hosted-v2-{off-a,on-a,off-b,on-b}-s42-c0`，串行独立冷启动，同一 hosted recorder.dll：`a6e7dcf06060a4d3c3a87545b5e3044f2a47786b09cc2a293a42c9deeb9fb8ad`。
- 原生代码基线为 main `b15693714c84e74bff32f054a0d501c4e45252b1`，加 PR117 中已独立审查的 `7f06e73` Windows manifest 输入流关闭修复。`b156937` 本身不包含该修复。运行身份以实际 DLL 摘要为准。
- 原有 hosted 脚本 `logger/avz/hosted/jing_dian_12.cpp` 摘要：`ff7f049a0607c6518e0a755fe7a81c5823035e03a822d409a6a97be8647cd171`；脚本行为未改变。
- 冻结计划 `experiments/plans/issue99-shovel-control.json`：seed42、jingdian12、B0 1500/1340、audio `sound_effects_allocation_none_v1`、wave>=2 停止、tick 硬上限2000、tick1000暂停。未追加干预或延长窗口。
- 四臂实际覆盖 epoch3/tick0/revision5 至 epoch3/tick1201/revision0，并到达 wave2。每臂2402个审计帧、13步请求/结果。

## 独立结果

| 检查 | 结果 |
|---|---|
| 四臂原始归档清单、全部文件摘要与关闭状态 | `records.validate` 全部通过 |
| 四臂严格审计、生命周期、原生请求/结果交叉验证 | 全部通过 |
| 四臂提交后的只读封存验证 | 全部 exit 0 |
| off-A/on-A，共同审计、动作、结果与初始/终点 | 一致 |
| off-A/off-B，共同证据及完整生命周期序列/回执健康 | 一致 |
| on-A/on-B，共同证据及完整生命周期序列/回执健康 | 一致 |
| on-A、on-B，全声明窗口首次击杀 | 两者均 proven=true，全部前提为真 |
| 最终 Python 套件 | 914项，OK，3项跳过 |

原生33项夹具此前已独立通过；后续离线修订不改变原生探针语义。Windows输入流修复另有原版失败/修复成功的独立本机复现。

归一化仅针对声明的运行身份、进程局部 Board 指针，以及已声明的宿主时间/磁盘遥测；动作参数、结果、像素证据、停止原因和完整事件顺序保留比较。结论限于捕获的共同状态、render、engine-call、RNG、动作和结果，不宣称所有隐藏引擎状态均无扰动。

## 首次击杀与覆盖限制

两个 on 臂均有116条初始化记录和137条新增捕获事实（104 gameplay、33 preview），完整序列相同。首个确认事实是实体 `3749838848` 的 capture_sequence `95`，首次 mDead 0→1，来源为经锁定字节与栈帧核验的 `ApplyBurn → DieWithLoot → DieNoLoot` 路径；返回地址为 `0x5302FF` / `0x532FC7`。全窗口验证要求 receipt、probe capability、initialization capture、full window、health 全部有效，未发现阻止首杀证明的更早未知 gameplay 事实。

结论来自原生事实与全窗口前提，不是137条或33条计数。通用移除、foreign 帧、预览对象不因此被推断为击杀；不作完整攻击者/具体炮弹归因，也不提供通用击杀计数器。完整报告仍保留旧的 `boundary_gate` 为未证明，以说明单靠边界快照不足；新的顶层 `first_kill` 来源为 exact-store capture facts。

## 证据与复现

- `experiments/reports/issue111-d-principal/acceptance.json`：principal独立归档验证、三组完整比较、首杀前提、四份seal ID、输入绑定与本次报告摘要。
- 同目录 `on-a-first-kill.json` / `on-b-first-kill.json`：两个完整报告，含事实坐标、实体、顺序和未知/预览信息。
- `experiments/reports/issue111-d-revalidation/<arm>-{revalidation,seal}.json`：四臂严格复验证据与封存副本；`issue111-d-provenance-v2.json` 保存原始状态和摘要。
- 原始数据保留在独立 worktree 的 `experiments/runs/issue111-d-hosted-v2-*`。原始 on 臂 evaluation 仍为旧读取器打包失败；其结构化错误仅为 `strict_packaging/EvidenceError/native event after recording close`，清理完整。补充离线复验不改写原报告。更早失败轨迹及弱seal也保留为历史，不计入四臂通过结果。

在保留数据的 worktree 根目录、`PYTHONPATH=src` 下运行：

```powershell
python tools/issue111_lifecycle_experiment.py --root . verify-revalidation --run issue111-d-hosted-v2-off-a
python tools/issue111_lifecycle_experiment.py --root . verify-revalidation --run issue111-d-hosted-v2-on-a
python tools/issue111_lifecycle_experiment.py --root . verify-revalidation --run issue111-d-hosted-v2-off-b
python tools/issue111_lifecycle_experiment.py --root . verify-revalidation --run issue111-d-hosted-v2-on-b
python experiments/reports/issue111-d-principal/reproduce.py
```

最后一条命令重新核对全部原始归档摘要、严格原生请求/结果、三组比较及两个完整首杀报告，只将派生输出写到 `work/principal-acceptance`，不改变轨迹或seal。验证器指纹绑定实际源码字节；该封存在本次Windows checkout的CRLF源码上生成，移植复验时须保留该源码表示。principal已按LF归一化核对九份源码与记录的实现提交一致。

## 测试覆盖与任务边界

同槽代次、延迟回收、未知移除、初态残留、多次边界变化、嵌套顺序由既有无游戏夹具覆盖；`test_complete_evidence_proves_first_kill` 同时验证同调用初始化/移除关联（same_call_lifetimes=1），原生夹具验证受控调用身份与真实回收链。本次真机四臂中 same_call_lifetimes=0，不声称真机出现了该构造场景。

新增封存编排的合成完整轨迹正例未另行完成；其生产正例由本次四条真实轨迹贯穿验证，错误与seal变异由无游戏测试覆盖。这不替代或缩减原issue要求的生命周期场景夹具。原始 #99/#105 报告与各自正向门槛未修改；本报告不代表那两个issue已整体通过。不实现或加载Agent、奖励或在线控制消费者也能完成本次实验验证。
