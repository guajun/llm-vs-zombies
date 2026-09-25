# #111 死亡、移除、回收候选原生路径证据（待维护者审查）

日期：2026-09-26。本文件与机器可读的
[`issue111-原生候选证据.json`](issue111-原生候选证据.json) 一起记录：在**不启动游戏、不安装 hook、
不写目标文件**的前提下，从锁定引擎与固定 AvZ 框架得到死亡/移除/回收候选原生路径的完整控制流、
调用者分类与 ABI-safe 拦截方案。

**Junior 关卡状态：OPEN。** 候选仍是 `candidate_review_required`；`docs/issue111-捕获点表.json`
三行仍保持 `review_required`、ABI `unknown`、无地址。本文件不能作为已验收实现；但语义判断与
拦截路线由执行者基于静态证据给出，不需要维护者替我们挑地址。

## 1. 目标身份与复核方法

| 项 | 值 |
|---|---|
| 引擎（local-engine 提取版） | `sha256 f9669af338964787a3785a7895791297d599295b8bb669b0db49443f736a1322` |
| 包装器（原版 exe） | `sha256 9397ca2dc3a4560eba5f87aef40fd800786ba0312716729ccbdbda185abee27d` |
| 装载基址 | `0x400000`（`determinism/evidence.json` 记录的锁定镜像） |
| AvZ 参考 | `c42676c269b5b482a1eb9203a5b979e9d8a2a5c7`：`inc/avz_pvz_struct.h`（State@0x28、Hp@0xC8、IsDisappeared@0xEC、AtWave@0x6C）、`src/avz_asm.cpp`（`KillZombie=0x5302F0`、`RemoveZombie=0x530510`、`KillZombiesPreview=0x40DF70`）、`inc/avz_types.h`（类型枚举） |
| 反汇编 | 锁定 LLVM-MinGW 20260908 的 `llvm-objdump 23`，只读反汇编 `.text` |
| 复核命令 | `python tools/issue111_native_candidates.py check`、`python tools/issue111_native_candidates.py verify --exe <locked exe>` |

本机 `verify`：镜像 SHA-256 匹配，9 个候选的 32 字节签名全部 `match`。仓库只保存哈希与字节。

## 2. 候选摘要

| 候选 | 入口 VA / RVA | ABI | 语义结论 |
|---|---|---|---|
| `zombie-removal-unclassified` | `0x530510` / `0x130510` | thiscall，`ECX=AZombie*` | 唯一 `+0xEC=1` 写点（`0x530602`）；棋盘与预览清理共用，必须做棋盘池过滤；本身不区分死亡/非死亡 |
| `zombie-slot-recycled` | `0x41BAD0` / `0x1BAD0`（free 序列 `0x41BBF4-0x41BC23`） | 入口 `ESI=Board*`，单一调用者 `0x4526F0`（`game_total_loop 0x452650`） | 唯一僵尸池释放：`free_head(board+0x9C) <-> slot+0x158`、`count(board+0xA0)--`，按 `+0xEC` 每次 update 扫一次 |
| `zombie-death-state-falling` | `0x533240` / `0x133240`，写点 `0x533377` | `EAX=AZombie*` | State∈{1,2,3} 直接返回；有附件/效果前置时才写 `State=1`，否则该函数本身走 `0x530510` 移除 |
| `zombie-death-state-ash` | `0x532B70` / `0x132B70`，写点 `0x532F5F` | thiscall，`ECX=AZombie*` | 入口拒绝已消失/State==2；写 `State=2`+`StateCountdown=0x12C`，随后调 `0x530170`；部分类型再走 `KillZombie`；附件是独立池实体需按完整 ID 去重 |
| `zombie-death-state-mower` | `0x5327E0` / `0x1327E0`，写点 `0x532A62` | `EDI=AZombie*`（调用者 `0x4586D2` 从 `0x8(%EBP)` 载入） | 割草机路径：入口拒绝已消失/State==3；写 `State=3` 后跳 `0x530170`；调用者随后另调 `0x530510` 移除 |
| `zombie-death-state-variant-a` | `0x527750` / `0x127750`，写点 `0x5279F4` | `EAX=AZombie*` | 类型 0x1D（29 = 机枪豌豆僵尸）专属分支；`+0xBA` 保护；写 `State=1` 并重置行/位置/计时字段；**不**移除、**不**调 `0x530170` |
| `zombie-death-state-variant-b` | `0x52EC00` / `0x12EC00`，写点 `0x52EC92` | `EAX=AZombie*` + 栈标志 bit 0x20 | 标志置位分支写 `State=1`+`StateCountdown=0x118`+效果；标志清零分支走 `0x530510`+`0x530170` 移除 |
| `zombie-death-effect-shared-aftermath` | `0x530170` / `0x130170` | `EAX=AZombie*` | 棋盘死亡收尾：AtWave -2/-3 提前返回；多数死亡路径调用，但 variant-a 与 falling 的移除分支不调用 → **不能单独作为死亡阶段事实** |
| `zombie-detach-helper` | `0x530850` / `0x130850` | cdecl，栈上传 `AZombie*` | 移除（返回地址 `0x530520`）与回收 free（返回地址 `0x41BBFF`）前都会调用，可作 ABI-safe 回收触发点 |

## 3. 语义判断与拦截方案

### 3.1 棋盘 vs 预览

- 预览清理路径：`0x40DF70`（pinned AvZ `KillZombiesPreview` 的目标）及其兄弟 `0x40DEA0`/`0x40DF00`，
  加上早期内联列表 `0x401970`；`0x40DF70` 的分支只处理 AtWave==-2 的预览对象。
- 棋盘路径：区域效果 `0x421B10`、割草机 `0x458540`、按 ID/池查找 `0x460060`、各类 update/分支
  `0x52xxxx`、`KillZombie 0x5302F0` 与附件变体 `0x530310`/`0x5303A0`。
- 所有生产 hook 统一用**棋盘池成员过滤**：读 `zombie+4` 的 board 指针，再验证
  `zombie == pool.block(board+0x90) + (id&0xffff)*0x15c` 且 `id != 0`；预览/静态对象不满足。
  该判据只依赖已在审计中验证的池布局，不需要维护者选择地址。

### 3.2 State 转换 vs 重复

- 五个写点所在函数都有入口幂等保护（State 已是死亡阶段、`+0xEC` 已置位或类型/前置条件不符时
  直接返回或改走移除）；静态上 `movl $1/2/3, 0x28(reg)` 的写点扫描（全 `.text`）只找到这 5 处。
- 因此死亡阶段事实按“入口读 State → 出口读 State，仅 0→{1,2,3} 变化时发一条”实现，并以
  `(run, session, 完整 ID, stage)` 去重；附件重复写按完整 ID 折叠为一次。
- `zombie-death-effect-shared-aftermath` 只作为可选的配对/收尾标记；缺失它不推翻 State 事实。

### 3.3 ABI-safe 拦截（执行者决定，供审查）

| 事实 | 策略 | 理由 | 已否决方案 |
|---|---|---|---|
| 移除 | `0x530510` 入口 hook（`ECX=this`），棋盘池过滤 | 唯一 `+0xEC` 写点；入口 hook 不需要中段补丁 | 逐调用者 hook（30+ 脆弱点）；用 `0x530170` 代替（并非所有路径调用） |
| 死亡阶段 | 五个 State 写函数各做 入口+出口 hook（`EAX`/`ECX`/`EDI` 适配），仅发 0→{1,2,3} 变化 | 复用现有寄存器保存 shim；函数边界清晰、各有幂等保护 | 只 hook `0x530170`（漏 variant-a 与 falling 移除分支）；逐条 state-store 指令 hook（5+ 点、无统一 ABI） |
| 回收 | `0x530850` 入口 hook（cdecl），按返回地址过滤：`0x41BBFF`=回收 free，`0x530520`=移除 | 只在入口读栈上的返回地址，无需中段补丁；sweep 是唯一僵尸池释放路径 | hook `0x41BAD0` 按 stride 过滤（同时处理植物/格子项）；`0x41BBF9` 中段 MinHook（6 字节窗口、风险高） |

字节签名、调用者全表与控制流细节在 JSON 中逐条登记；`tools/issue111_native_candidates.py verify`
可对任意锁定副本复核 32 字节签名。

## 4. 仍需动态/维护者确认的残余问题

1. 预览对象的 `+4` 字段在运行时的实际值未动态验证；当前仅以池成员过滤作为静态判据。
2. variant-a（类型 29）与 variant-b 的静态形态符合死亡动画，但未动态观察；需要真机确认它们不是
   位移/重生路径，以及标志位选择的语义。
3. 立即值扫描未发现数据驱动的 State 写点，但不能静态排除计算型写入。
4. `0x52FE50`（掉落/结算）与 `+0xBA`/`+0xBD` 标志的确切语义未解码，影响 variant/附件分类。
5. 附件 `+0xF4` 是独立池槽；一个完整实体可能产生多条 State 写，折叠规则需维护者确认。

这些确认完成前不实现生产 hook、不跑游戏；D 阶段使用初始化探针，捕获级死亡/移除结论输出
`unavailable`。
