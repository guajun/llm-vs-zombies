# #110：四轨迹离线死亡阶段补证

本交付只读分析 #99 封存的 fc2 四轨迹，承接 [#110](https://github.com/guajun/llm-vs-zombies/issues/110)、
[#99](https://github.com/guajun/llm-vs-zombies/issues/99)、[#105](https://github.com/guajun/llm-vs-zombies/issues/105)
和 [PR #107](https://github.com/guajun/llm-vs-zombies/pull/107)。没有启动游戏、重录、修改原轨迹、seal 或旧报告。

**离线分析交付完成；全窗口绝对首次击杀门槛仍未验证，不勾选父任务正向验收项。**

## 单一离线入口

在仓库根执行（Python 3.11+，标准库；不要求运行 AvZ 或游戏）：

```powershell
python tools/issue110_deaths.py --output work/issue110-fc2-review
```

输出目录必须不存在；工具拒绝在原 `experiments/runs` 或 `experiments/trees` 内写报告。
可用 `--root <旧检出根>` 读取另一位置的原始证据；旧 seal 的相对路径按该根解析。
命令完成四条全窗口分析，输出 `report.json`、`report.md`、`SHA256SUMS.json`。
缺失输入、身份不符、原生边界不完整以失败退出并保留诊断；不以新游戏运行替代缺失输入。
退出 0 表示分析交付完成，**不表示首次击杀门槛通过**。

## 输入身份和保护

- 原源码基线：`346e495aaabd5ccb1b7d52ef75ee95c8b4f4b82a`。
- tree：`experiments/trees/issue99-fc2-shovel-fork`。
- tree_id：`ce770e531a225234b53ac3b810bc3209e4417b257410605f105bc38b0bc90220`。
- seal：`work/issue99-fc2-seal.json`。
- 旧 JSON SHA256：`05d83bc30ca952aa568b077a97b3bebe1994202ed7aaca0ec89a33705970c639`。
- 旧 Markdown SHA256：`68495027150d4f296ed28bbfe67ea2aa4e357dd0675695b57a27974f8e4fd7e6`。
- 四源运行：`experiments/runs/issue99-fc2-{control,intervention}-{a,b}-s42-c0`。

重新调用既有 `read_tree` / `verify_seal`，全量校验四个树节点；逐项确认源运行的 initial、steps、files
与其封存节点相符，再校验实际分析的 `run/audit` 文件摘要等于树节点绑定的摘要。
因此没有把树中一份证据的校验结果误用于旁边另一份未绑定的 audit。
冻结计划与旧报告内的计划逐项一致；终点与轨迹末尾、experiment-end、停止条件核对。

分析前后对 164 个输入文件计算 SHA256（树全体、源 audit/trajectory、运行 manifest/终点、四计划、seal、旧报告）；
全部一致。之后复查 seal/旧报告；树字节保持不变，因此复用刚完成的树验证结果，不再次解码同一树。
这是相关证据输入的完整摘要清单，不是视频等全部运行附件的新增全量审计。

## 锁定语义和保守判据

主依据为 AvZ `c42676c269b5b482a1eb9203a5b979e9d8a2a5c7` 的 `inc/avz_pvz_struct.h`，
以及原基线的 `determinism/audit.cpp`。报告绑定游戏目标 `pvz-1.0.0.1051-en`、32 位布局、
`loaded_signatures_match=true` 和实际引擎输入摘要
`f9669af338964787a3785a7895791297d599295b8bb669b0db49443f736a1322`。

| 字段 | 布局/宽度 | 判定用途 |
|---|---|---|
| State | +0x28，int32；audit 记录 uint32 原始字 | `IsDead()` 当且仅当 1/2/3：倒地/灰烬/小推车死亡阶段 |
| 本体 HP | +0xc8，int32 | 仅作为前后原始证据；正数或非正数均不能单独决定死亡 |
| 饰品 HP | +0xd0、+0xdc，int32 | 同上 |
| IsDisappeared | +0xec，1 字节 | 消失标记，不等于伤害死亡 |
| AtWave | +0x6c，int32 | 负值初态/选卡对象不当作正常战斗死亡转移 |
| 完整 ID | +0x158，uint32 | 低 16 位槽位，高 16 位代次；高位为零时 id_or_free_next 是空闲链信息 |

`WordRange` / `Byte` 和 `Pool` 实际捕获以上字段及完整 ID，不能把 uint32 编码的负数当正 HP/波数。
解析复用 `EvidenceStore` 和 `iter_states(in_place=True)`；只复制被跟踪的实体事实，避免后续 patch 修改前态。

确认“进入死亡阶段”需要同一完整 ID 的有效前态（未消失、非死亡、战斗对象）到 1/2/3；
初态已经死亡只算首次观测，不能算窗口内新死亡。
同槽新代次不继承旧实体生命史。正常死亡后的延迟释放单列；立即消失/释放且没有死亡阶段证据时原因未知。
初态已经置消失的对象释放单列，不能声称其死亡发生在声明窗口内。

非死亡前态白名单为 0/11/15/20/69/70/71/73/76，均不满足锁定 AvZ 的 `IsDead()`；
状态名称辅助核对本机 `work/replay-research/ConstEnums.h`，其 SHA256 为
`2601042777012223c0a638f3265fc75f9c27451db50d35058623be92c531c175`。
它是仓库既有研究档中的候选反编译资料（声明来源 `ruslan831/PlantsVsZombies-decompilation@8a2d1218…`），
**不是对目标二进制的逐项独立反汇编证明**。实际死亡判据只来自锁定 AvZ；其他原始状态码标未分类，不默认为死亡。
本工具针对本次四轨迹，不是覆盖所有僵尸行为的通用击杀计数器。

每条逻辑行与 checksums / engine-call-raw 的 seq、kind、version、调用 ID 对齐。
检查原生 pre/post 配对、调用连续性、pre_version、tick 增量、board 保持和声明首尾。
不按 tick 覆盖记录；保留同 tick 的 post、下一次 pre 和动作增加的 revision。

## 四轨迹结论

冻结硬上限 2000 ticks，事件终点为首次达到 wave>=2；实际每条从 epoch 3 / tick 0 / revision 5
至 epoch 3 / tick 1201 / revision 0，`stop_condition_reached`，最终 wave=2。
**每条完整扫描 2402 个边界，没有只查看 tick 941。**

| 轨迹 | 首次确认死亡阶段 | 来源逻辑行 | seq | 原生调用 | 同边界候选 |
|---|---|---:|---:|---:|---:|
| control-a | epoch 3 / tick 940 / revision 0，post_step | 1880 | 2033 | 940 | 5 |
| control-b | 同上 | 1880 | 2033 | 940 | 5 |
| intervention-a | 同上 | 1880 | 2034 | 940 | 5 |
| intervention-b | 同上 | 1880 | 2034 | 940 | 5 |

来源均为各 run 的 `audit/state-deltas.jsonl.gz`，行号指解压后 JSONL 行，从 1 起；同坐标可交叉查
`checksums.jsonl.gz` / `engine-call-raw.jsonl.gz`。前态在逻辑行 1879、tick 939 pre_step；原生时钟 55091→55092。

| 完整 ID | 槽位 | 代次 | 前→后 state | 前→后本体 HP | 前→后 disappeared |
|---:|---:|---:|---|---|---|
| 3749445638 | 6 | 57212 | 73→2 | 270→270 | 0→0 |
| 3750101006 | 14 | 57222 | 73→2 | 270→270 | 0→0 |
| 3750756376 | 24 | 57232 | 73→2 | 270→270 | 0→0 |
| 3751411746 | 34 | 57242 | 73→2 | 270→270 | 0→0 |
| 3752067116 | 44 | 57252 | 73→2 | 270→270 | 0→0 |

四条轨迹候选 ID 一致，type=16、AtWave=0。同一采样边界内没有更细事件顺序，因此保留全部 5 个候选，
不能把槽位最小者称为唯一“第一个”。这里的正 HP 灰烬状态也说明只用 HP<=0 会漏掉真实死亡阶段。

每条轨迹还观测到：

- 11 个初态已消失对象在 tick 1 释放；其原始 AtWave=-2。它们不是窗口内确认击杀。
- tick 940 同边界有 33 个实体从 disappeared=0 变为 1，但没有观测到死亡阶段，原因未知。
- 这 33 个槽位在 tick 941 释放；旧 first_removal 的 tick 941 结论保持不变，仍不是击杀计数。
- 总计 44 次槽位释放（11 个初态残留 + 33 个未知原因离场），不能与 5 次确认死亡阶段相加当击杀数。
- 66 条不确定事件是 33 次消失和后续 33 次槽位释放两种观测，**不是 66 个不同未知死亡实体**。

control-a/b、intervention-a/b 的逐事件集合均完全一致：完整 ID/代次、原始字段、行号、seq、版本、
原生调用坐标及初态残留都参与比较；只排除来源路径与请求标签。没有用累计数量相同代替事件一致。
跨组 audit seq 差 1 来自干预事件，报告保持原值，未为对齐而篡改坐标。

## 未验证范围及最小补录

可以确认首次**观测到并从非死亡状态进入** AvZ 死亡阶段的边界是 tick 940。
但 33 个同边界直接消失实体可能在调用内部先于或后于这 5 个实体；当前材料不能判断其原因和顺序。
另有 `coverage.exact_spawn_hook=false`，无法排除在两个采样边界之间创建又立即删除的对象。
因此不能证明整个窗口的绝对首次死亡，更不能把首次植物击杀、累计击杀或某一发炮的伤害来源判通过。

若需进一步证明，最小新证据应为同一冻结窗口内：

1. 核验锁定二进制的死亡分支与无伤害回收分支，记录完整 ID/代次、原生调用号、调用内递增序号、
   前后 state/HP/disappeared、明确原因和创建/回收。只钩一个通用清理函数不足。
2. 若要区分植物、小推车、自爆、友军伤害，另录攻击者/投射物 ID、伤害类别和目标 ID。
   State=3 只证明小推车死亡阶段，State=2 不能唯一归因于炮击。
3. 仍覆盖原根至冻结终点，不能只补 tick 940 的局部而声称排除了更早事件；不扩大窗口追求正结果。
   可先单分支验证插桩，再决定是否补同组复跑和其他分支，本次没有自动补跑。

## 测试与报告

```powershell
$env:PYTHONPATH='src;tests'
python -m unittest tests.test_issue110_deaths tests.test_issue99_shovel_fork tests.test_tree_evidence tests.test_evidence_codec tests.test_audit_compare -q
```

82 项通过：18 项新增行为夹具及 64 项现有相关回归。覆盖正常死亡后延迟释放、无死亡证据离场、
同槽代次复用、初态已死亡/已消失、同 tick pre/post 与 revision、立即消失、同边界多个候选、
缺字段/身份、未知状态、断流/调用跳号、数量相同但身份不同、缺输入与输出不可覆盖。

本机最终派生报告位于 `work/issue110-fc2-final/report.json`、`report.md`；摘要见同目录 `SHA256SUMS.json`。
报告绑定源 seal/tree_id、逐文件输入 SHA256、规则版本 `lvz.issue110-death-stage.v2`、分析器提交及源码/读取器摘要。
证据和派生报告均属本机文件，不随源码 clone 出现；缺失时工具明确失败。
