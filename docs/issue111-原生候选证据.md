# #111 死亡、移除、回收候选原生路径证据（待维护者审查）

日期：2026-09-26。本文件与机器可读的
[`issue111-原生候选证据.json`](issue111-原生候选证据.json) 一起记录：在**不启动游戏、不安装 hook、
不写目标文件**的前提下，从锁定引擎、固定 AvZ 框架与固定 commit 的公开反编译得到的完整控制流、
对象身份判定与 ABI-safe 精确拦截方案。

**Junior 关卡状态：OPEN。** 捕获点表三行仍保持 `review_required`、ABI `unknown`、无地址。
本文件是**设计证据**，不是已验收实现；但常规 hook 机制由执行者按证据决定，不需要用户挑选地址。

## 1. 来源与复核

| 项 | 值 |
|---|---|
| 引擎 | `sha256 f9669af…`（local-engine 提取版，基址 0x400000） |
| AvZ | `c42676c269b5b482a1eb9203a5b979e9d8a2a5c7` |
| 公开反编译（研究辅助） | `https://github.com/Patoke/re-plants-vs-zombies` @ `c4692036c5e11d227c8fb7c593b734dac96da028`，`Zombie.cpp`（原地址注释；锁定二进制仍为权威） |
| 社区内存表 | `https://wiki.pvz1.com/doku.php?id=技术:内存基址`（+0x28 状态/动画、+0x6C 波次、+0xBA 头/非濒死、+0xBD 水中、抛射物也有 +0x28） |
| 复核 | `python tools/issue111_native_candidates.py check`；`verify --exe <locked exe>` 本机 11/11 字节 match |

反编译只引用短结论，不 vendoring 第三方源码。

## 2. 关键语义修正

- `0x527750 = UpdateZombieGatlingHead` 是**假阳性**：`0x527952` 调 `AddProjectile(0x41DF60)` 并把返回的
  **抛射物**放进 ESI，`0x5279F4` 的 `movl $1, 0x28(%esi)` 写的是抛射物字段，不是僵尸 phase。
  该条目保留为对象身份过滤的负例。
- `0x530510 = DieNoLoot`：唯一 `+0xEC=1` 写点（`0x530602`），停声、删 reanimation、设置 mDead；
  **不**写 phase、**不**释放槽位。`0x5302F0 = DieWithLoot = DieNoLoot + DropLoot`。
- `0x530170 = DropLoot`：只做掉落/图鉴/奖励结算（`IsOnBoard()` 不过即返回），**不是死亡确认**。
- `0x530850 = StopZombieSound`：停 dancer/box/digger 音效并可能遍历棋盘；入口**不能**当作回收标记。
- `0x530310 = BobsledDie`、`0x5303A0 = BobsledBurn`（此前误标为附件 ash 变体）。
- `0x532B70 = ApplyBurn`（phase 2 + countdown 0x12C + DropLoot，部分路径 DieWithLoot）。
- `0x5327E0 = MowDown`（phase 3 后 tail-jump DropLoot；catapult/zamboni 直接 DieWithLoot）。
- `0x533240 = PlayDeathAnim`（phase 1；无死亡动画等分支改走 DieNoLoot）。
- `0x52E9A0 = ZamboniDeath`（spike 分支用**寄存器写** phase，`0x52EA38`，此前即时值扫描漏掉；
  否则内联 DieWithLoot）。`0x52EC00 = CatapultDeath`（栈标志 bit 0x20 = DAMAGE_SPIKE 选分支）。
- `0x41BAD0` 是棋盘回收 sweep；僵尸分支 `0x41BBA9` 读 `+0xEC`，free 序列 `0x41BBF4-0x41BC23`
  更新 `free_head(board+0x9C) <-> slot+0x158` 与 `count(board+0xA0)`；`0x41BC23` 是 5 字节 jmp，
  正好在 free 之后，可作可执行的 commit 观测点。
- **IsOnBoard**：来源 `mFromWave(+0x6C) != -2 && != -3`。预览对象可能也在 Board 池里，
  **池指针本身不证明在棋盘上**；对象身份过滤要用“池/stride 归类 + +0x6C 波次标记”。

## 3. 生产设计（执行者方案，供维护者审查）

### 3.1 原始 phase 转换（不做过早 dedup）

- 在僵尸 `+0x28` 的**精确 store 指令**上做 instruction-granular MinHook：`0x533377`(1)、`0x532F5F`(2)、
  `0x532A62`(3)、`0x52EC92`(1)、`0x52EA38`(寄存器值)，以及对象归类后发现的其它僵尸 store。
- 每个 hook 记录 store 处的僵尸指针（各站点寄存器：EDI/ESI）、store VA、前后 phase 原始值与
  `capture_sequence`；不按完整 ID/stage 去重，重复事实原样保留。
- 离线分类把 phase 1/2/3 归为死亡阶段并输出过滤计数（其他 phase 转换单独计数）；这样不会用
  dedup 掩盖重复或多段事实。
- 短 store（3 字节）用 MinHook 的重定位窗口（向后复制到 ≥5 字节）处理；实现前必须验证窗口内
  没有分支目标。函数入口/出口做 net-diff 的方案已否决：无法定位内部 store 相对嵌套移除的顺序。

### 3.2 移除标记

- 在 `0x530602` 的 `movb $0x1, 0xec(%edi)`（7 字节）精确拦截，记录 mDead 0→1 与类型；
  可另发 DieNoLoot 入口事件做来源标注，但事实以 store 为准。

### 3.3 回收提交

- 在 sweep 内配对：`0x41BBA9`（7 字节 guard，EDI=僵尸，记录完整 ID 的 candidate）与
  `0x41BC23`（5 字节 jmp，free-list 更新后，记录 slot/free_head/count 的 commit）。
- 离线按同一次 sweep 迭代配对；**绝不**在入口处声称回收完成，也**不**用 StopZombieSound 入口当标记。

### 3.4 对象身份过滤

- 把对象指针归类到棋盘池（僵尸 stride 0x15c @board+0x90、抛射物 0x94 @board+0xC8、植物 0x14c
  @board+0xAC、格子/硬币），再读 `+0x6C`；只有“僵尸池 + wave ∉ {-2,-3}”才算棋盘僵尸。
  该过滤同时用于 phase/removal/recycle 三类事件，并写入每条事件。

## 4. 仍未解决/需动态确认

1. `0x52EA38` 的 EBX 值为 PHASE_ZOMBIE_DYING 是源码交叉结论，需在实现时读寄存器值并在夹具中验证。
2. 僵尸 `+0x28` 的 store 全集需在对象归类后复核（即时值/寄存器扫描找到大量非僵尸写点）。
3. 回收 sweep 的源函数身份（Board.cpp）与“唯一 free 路径”需进一步复核。
4. 预览对象是否可能通过“僵尸池 + wave ∉ {-2,-3}”过滤，需真机/夹具确认。
5. 附件 `+0xF4` 是独立池实体，离线分类需定义同一逻辑实体的折叠规则。

以上确认前不实现生产 hook、不跑游戏；D 阶段只使用已合并的初始化探针，捕获级结论 `unavailable`。
