# #111 死亡、移除、回收候选原生路径证据（待维护者审查）

日期：2026-09-26。本文件与机器可读的
[`issue111-原生候选证据.json`](issue111-原生候选证据.json) 一起记录：在**不启动游戏、不安装 hook、
不写目标文件**的前提下，从锁定引擎与固定 AvZ 框架得到的死亡/移除/回收候选原生路径。

**Junior 关卡状态：OPEN。** 下面每条都是 `candidate_review_required`，不是已确认的生产捕获点。
`docs/issue111-捕获点表.json` 中对应的三行仍保持 `review_required`、ABI `unknown`、无地址；
本文件不能作为实现生产 hook 的依据。维护者审查通过后，才把选中行升级并把地址写进捕获点表。

## 1. 目标身份与复核方法

| 项 | 值 |
|---|---|
| 引擎（local-engine 提取版） | `sha256 f9669af338964787a3785a7895791297d599295b8bb669b0db49443f736a1322` |
| 包装器（原版 exe） | `sha256 9397ca2dc3a4560eba5f87aef40fd800786ba0312716729ccbdbda185abee27d` |
| 装载基址 | `0x400000`（`determinism/evidence.json` 记录的锁定镜像） |
| AvZ 参考 | `c42676c269b5b482a1eb9203a5b979e9d8a2a5c7`：`inc/avz_pvz_struct.h`（State@0x28、Hp@0xC8、IsDisappeared@0xEC、AtWave@0x6C）、`src/avz_asm.cpp`（`KillZombie=0x5302F0`、`RemoveZombie=0x530510`） |
| 反汇编 | 锁定 LLVM-MinGW 20260908 的 `llvm-objdump 23`，只读反汇编 `.text` |
| 复核命令 | `python tools/issue111_native_candidates.py check`、`python tools/issue111_native_candidates.py verify --exe <locked exe>` |

本机已运行 `verify`：镜像 SHA-256 匹配，9 个候选的 32 字节签名全部 `match`。该命令不会写目标
文件；仓库只保存哈希与字节，不保存 exe。

## 2. 候选摘要

| 候选 | 入口 VA / RVA | ABI | 观察到的语义 | 与 #111 事实的关系 |
|---|---|---|---|---|
| `zombie-removal-unclassified` | `0x530510` / `0x130510` | thiscall，`ECX=AZombie*` | 脱离并置 `+0xEC=1`（全 `.text` 唯一 `movb $1, 0xec(reg)` 写点 `0x530602`）；`KillZombie 0x5302F0` 先调它 | 移除事实的主候选；本身不区分死亡/非死亡移除 |
| `zombie-slot-recycled` | `0x41BAD0` / `0x1BAD0`（free 序列 `0x41BBF4-0x41BC23`） | 入口 `ESI=Board*`，单一调用者 `0x4526F0`（`game_total_loop 0x452650` 内） | 扫描 `+0xEC` 已置位的僵尸，执行 detach 后把槽位挂回 `board+0x9C` free_head、`board+0xA0` count-- | 回收事实的主候选；与 #110 观察到的“disappeared 后下一批边界释放槽位”一致 |
| `zombie-death-state-falling` | `0x533240` / `0x133240`，写点 `0x533377` | `EAX=AZombie*` | 按当前 State 分支；写 `State=1` 后生成粒子效果 | State=1 死亡阶段候选之一 |
| `zombie-death-state-ash` | `0x532B70` / `0x132B70`，写点 `0x532F5F` | thiscall，`ECX=AZombie*` | 写 `State=2`、`StateCountdown=0x12C`，随后调共享收尾 `0x530170`；`0x5303A0` 会遍历附件逐个调用 | State=2 死亡阶段候选；需按完整 ID 去重 |
| `zombie-death-state-mower` | `0x5327E0` / `0x1327E0`，写点 `0x532A62` | `EDI=AZombie*`（调用者 `0x4586D2` 从 `0x8(%EBP)` 载入） | 写 `State=3` 后跳 `0x530170`；入口已检查 `+0xEC` 与 `State==3` | State=3 死亡阶段候选，单调用者待确认是割草机路径 |
| `zombie-death-state-variant-a` | `0x527750` / `0x127750`，写点 `0x5279F4` | `EAX=AZombie*` | 写 `State=1` 并重置行/坐标/计时字段，受 `+0xBA` 保护 | 可能是亚种专属路径或位移而非死亡；不得与上一条合并 |
| `zombie-death-state-variant-b` | `0x52EC00` / `0x12EC00`，写点 `0x52EC92` | `EAX=AZombie*` + 栈标志 | 写 `State=1` 并推进 X 坐标 | 同上，未证实覆盖范围 |
| `zombie-death-effect-shared-aftermath` | `0x530170` / `0x130170` | `EAX=AZombie*` | 多路死亡收尾：读 AtWave，`-2/-3` 提前返回，置全局“已见类型”，调 `0x52FE50` | 可作死亡收尾确认标记，但不是 State 写入点 |
| `zombie-detach-helper` | `0x530850` / `0x130850` | cdecl，栈上传 `AZombie*` | 移除（`0x53051B`）与回收（`0x41BBFA`）前都会调用，也在非移除路径出现 | 仅辅助排序证据 |

完整字节、前 6 条指令、全部调用者列表、限制与开放问题在 JSON 中逐条登记。

## 3. 可靠性判断（本执行者意见，供维护者参考）

- **移除事实较可靠**：`0x530510` 是 AvZ 公开 API `RemoveZombie` 的入口，且是镜像中唯一给 `+0xEC`
  写 1 的位置；`KillZombie` 先调它，符合“死亡也走统一移除”的语义。
- **回收事实较可靠**：`0x41BAD0` 的僵尸分支确实按 `+0xEC` 释放池槽并更新 free_head/count，调用链
  在 `game_total_loop` 内、每个 update 至多一次；与审计里的 `free_head/next_key/count` 字段一一对应。
- **死亡阶段写入点尚不唯一**：至少 5 处 `State=1`、1 处 `State=2`、1 处 `State=3` 的静态写点，
  且有两处 `State=1` 可能是位移/亚种路径。共享收尾 `0x530170` 被多数死亡路径调用，但不能替代
  State 写入证据。**建议维护者先确认“死亡阶段”要以哪个写入点为准**，再决定 hook 位置与去重键。
- **不能据此判定击杀归因**：以上都不提供伤害来源；仍不得用 HP≤0、炮击时间接近或移除函数推断
  击杀。tick 940 的 33 个 disappeared-without-death 对象与 `0x530510` 的具体分支关系仍未证实。

## 4. 需要维护者确认的最小问题

1. `zombie-death-stage-enter` 的权威写入点是单一函数还是必须按 State 值分别 hook？两处
   `State=1` 变体（`0x527750`、`0x52EC00`）是否死亡？
2. `0x530510` 的调用者中哪些是棋盘移除、哪些是预览/菜单对象？是否所有棋盘移除都经过它？
3. 回收 hook 采用函数入口 + stride 过滤，还是在 `0x41BBF9` 做函数中段 trampoline 更安全？
4. 同一僵尸的附件（`0x5303A0` 路径）重复写 `State=2` 时，事实按“完整 ID 一次”还是“每次写入”？
5. `0x530170` 是否可额外作为“死亡收尾”事件，与 State 事件配对表达真实的死亡确认？

维护者给出上述选择后，执行者才会：更新 `docs/issue111-捕获点表.json` 对应行、实现生产 hook、
补原生夹具与 D 阶段真机对照。在此之前不实现、不跑游戏。
