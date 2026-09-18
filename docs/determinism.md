# 确定性审计适配器

目标为本机英文原版 1.0.0.1051。代码位于 `determinism/`，和唯一的 AvZ runtime DLL 一起构建。它不是独立注入器，也没有安装第二份主循环 hook。

## 已实现与证据

2026-09-18 对由本项目 wrapper 启动的原版进程执行只读映像导出，LLVM 反汇编确认以下结果。提案中“RNG 地址未知”现在已经有目标二进制证据；反汇编可定位不等于已通过完整游戏的重放验收。

| 对象 | 本版本位置及证据 | 实现 |
|---|---|---|
| 全局 MT | `0x5A9930` 把 `0x75A910` 放入 EDX；`0x5A9940` 在 `[EDX+0x9C0]` 读取游标，使用 624 项状态进行 twist/temper | 624 个 32 位状态项 **和游标**捕获、JSON 无损序列化、稳定边界恢复 |
| MT 播种 | `0x5A98D0`，EAX 指向实例、ECX 为种子；零种子替换为 4357；初始化乘数 `0x6C078965` | 作为版本签名和布局证据；首版不把中途重设种子当完整恢复 |
| 引擎 CRT RNG | `rand=0x61E087`、`srand=0x61E07A` 调用引擎静态 CRT 的 `_getptd=0x628A3D`，状态是返回对象的 `+0x14` | 捕获/恢复**游戏线程**的 LCG 状态，绝不误用 DLL 自身的 `rand()` 状态 |
| 独立 MT | 四个直接构造调用点 `0x40A7EA/0x4256B7/0x426A42/0x484173`；部分后续调用不经全局 wrapper | 已识别其存在；没有把单个全局种子宣称为所有 RNG |
| ZombieInitialize | 本机 `0x522580`，行参数在 EAX，僵尸指针在栈首参；入口直接调用全局随机数生成初始 X | 仅作为后续精确出生 hook 的已定位入口；当前记录仍明确标为首次边界观测 |

`determinism/evidence.json` 保存少量函数签名字节、目标文件哈希、定位方法，**不包含游戏二进制或完整反编译源代码**。原版 wrapper 会解包后启动 `popcapgame1.exe`，实际引擎 SHA-256 为 `f9669af338964787a3785a7895791297d599295b8bb669b0db49443f736a1322`。任何函数地址只适用于此目标；运行时要求 x86、固定加载基址、PE 头和多处完整代码签名匹配，否则拒绝适配。

字段布局依据本项目锁定的 [AvZ 结构定义](https://github.com/vector-wlc/AsmVsZombies/blob/c42676c269b5b482a1eb9203a5b979e9d8a2a5c7/inc/avz_pvz_struct.h)，扩充标量字段依据候选反编译项目的 [Zombie](https://github.com/ruslan831/PlantsVsZombies-decompilation/blob/8a2d121899ba5cb4df644cd7d2e4c1aaf88dd238/Lawn/Zombie.h)、[Board](https://github.com/ruslan831/PlantsVsZombies-decompilation/blob/8a2d121899ba5cb4df644cd7d2e4c1aaf88dd238/Lawn/Board.h) 和 [DataArray](https://github.com/ruslan831/PlantsVsZombies-decompilation/blob/8a2d121899ba5cb4df644cd7d2e4c1aaf88dd238/Sexy.TodLib/DataArray.h)。扩充布局仍需原版战斗运行覆盖；运行时检查内存可读、池容量、活跃数量、ID 与 slot 一致性，异常立即报错。

## 接口与时序

`ValidateTargetImage() noexcept` 可在安装 AvZ hook 前使用；不分配堆、不加载库。其他 API 必须在 `Initialize(runDir)` 所在线程执行，即 runtime 的游戏线程，通信线程不能直接调用。

```cpp
lvz::determinism::Initialize(run_directory);
// 本项目 runtime 在真正的 pre_step/post_step 直接调用 Audit(kind,payload,observation)。
auto random_state = lvz::determinism::CaptureRng();
std::string error;
// 调用者必须已经停止模拟且位于稳定边界。
bool ok = lvz::determinism::RestoreRng(random_state, "paused_at_boundary", error);
// 初始化阶段固定已确认随机流；不会恢复局面或重设其他实例。
bool seeded = lvz::determinism::SeedRng(12345, "paused_at_boundary", error);
lvz::determinism::Flush();
lvz::determinism::Shutdown();
```

`RestoreRng` 在写入任何状态之前验证全部字段、算法、实例集合、范围与签名，只允许固定的内部地址。它恢复 RNG，**不恢复 Board**。不能独立用它模拟 rewind。进程 ID、裸指针、线程 ID 不写入规范化比较状态。游戏线程身份由运行时绑定，协议中的字符串不能替代真正暂停控制。

`CaptureClocks()/RestoreClocks(snapshot,"paused_at_boundary",error)` 对已确认的 Board GameClock、EffectCounter 和 App MjClock 提供初始化 sidecar 接口。所有字段始终参与审计，不能为了通过不同菜单等待时长的比较而删除。恢复只允许处于战斗边界；它不调整出怪倒计时、对象年龄或控制器自己的 tick，也不是任意时间点的回滚。实验应明确记录在 B(0) 统一设定的三个值，并在重放用相同初始化流程恢复它们。

## 每帧审计文件

每次 `pre_step/post_step` 读取实际游戏内存，并向 run 的 `audit/` 写入：

- `manifest.json`：目标签名、实现能力和显式覆盖缺口。
- `checksums.jsonl`：每组件及整体的 FNV-1a 64 位诊断摘要。它不是密码学签名，不用于资产真实性证明。
- `state-deltas.jsonl`：首个完整规范化状态，随后保存 JSON Patch，可重建每次审计并展开首个字段差异。
- `events.jsonl`：动作/请求/其他 runtime 审计消息，以及 `zombie_first_boundary_observed`。后者含 ID、slot、全部覆盖的原始标量字段，`exact_spawn=false`。

规范化保留对象池的槽位、代次 ID、已用长度、容量、空闲链头、下一代次和空闲槽链接。活跃对象按 slot 键记录，排除 App/Board 指针、对象布局 padding。僵尸、植物、弹丸、收集物、场地物和推车均有记录；Board 记录格子、行路权重、波表、已允许类型、出怪阈值/倒计时、冰道/冰冻、阳光与关卡进度；卡槽记录冷却、激活和使用次数。

字段键使用结构内十六进制 offset。32 位标量作为 `uint32` 写入，浮点保存 IEEE-754 原始位，因此正负零、NaN payload 及最末位变化不会被 JSON 小数四舍五入吞掉。可依据上述结构定义解码；僵尸 `0000001c` 为行、`0000002c/30/34` 为 X/Y/速度位模式、`00000050` 为 variant、`000000c8` 为本体血量。记录 x87 控制字及 SSE 控制位；暂未锁定或证明时间源无关。

## 当前不能声称的能力

`complete_game_rng=false`、`complete_game_state=false`、`original_engine_replay_verified=false` 为有意设置，不能由“日志比较通过”自动提升。尚未覆盖独立 MT 的运行调用和生命周期、其他线程 CRT RNG、动画轨道/效果池、完整 Challenge 状态、光标输入、部分花盆币属性及时间源；也未实现精确出生 hook。暂停和隐藏窗口可能继续消耗共享随机流，必须进行扰动验收。隐藏窗口只是一种运行方式，不能由本模块宣称为纯无窗口模拟。

现有捕获/恢复与状态审计已可用于定位分叉，但**不足以宣称完整确定性实验就绪**。完整验收至少包括：已确认帧边界的固定脚本，多次同初态原版运行逐帧一致，暂停时长/焦点/渲染变化不引入差异，并对覆盖缺口逐项验证。具体门槛见《原版确定性重放器提案》。禁止用下一检查点覆盖分叉后继续宣称一致。

## 验证

`tests/determinism_model.cpp` 是不启动游戏的原生测试：使用标准库 MT19937 独立核对参考流，跨 twist 边界检查 625 项序列化恢复，拒绝损坏/缺失/溢出的状态，验证浮点位变化影响摘要、组件隔离和增量可重建。测试通过仅证明状态格式和摘要工具正确，不替代原版运行验收。

```powershell
# 在 F:\llm-vs-zombies 运行；通常由根构建统一添加此测试。
& .\third_party\llvm-mingw-20260908-ucrt-x86_64\bin\i686-w64-mingw32-clang++.exe `
  -std=c++23 -static -I . -I runtime/vendor tests/determinism_model.cpp -o build/determinism_model.exe
& .\build\determinism_model.exe
```
