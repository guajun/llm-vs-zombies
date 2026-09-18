# ZombieInitialize 精确出口记录

`determinism/spawn_hook.hpp/.cpp` 是原版初始化函数的观测模块。它复用 AvZ 内置 MinHook，但不安装或替换主循环、不调用另一个 AvZ 实例。当前已经通过本地 x86 ABI 测试，**尚待真实游戏运行验收**。

## 目标与 ABI 证据

本项目已提取并锁定的英文 1.0.0.1051 引擎：`popcapgame1.exe` SHA-256 为 `f9669af338964787a3785a7895791297d599295b8bb669b0db49443f736a1322`。对私有 `work/live-probe/loaded-image.bin` 的只读镜像反汇编确认：

- `0x522580` 为 `ZombieInitialize` 入口，EAX 是行号；原始栈为返回地址、僵尸指针、类型、variant、父僵尸指针、来源波号。
- 入口字节为 `55 8b ec 83 e4 f8 83 ec 14 53 56 8b f0 8b 45 18 57 8b 7d 08`。
- 从 `0x522580` 至下一函数 `0x524040`，唯一正常返回指令是 `0x52403B: ret 0x14`。共享 epilogue 从 `0x524035` 开始，字节为 `5f 5e 5b 8b e5 5d c2 14 00`，此刻 EDI 仍指向僵尸。
- 两处观测分别位于原始入口执行前和共享 epilogue 执行前；不会改写调用者的返回地址，也不依赖只覆盖某一个僵尸种类的内部分支。
- `[zombie+0x158]` 是对象池生成代次与槽位组成的 ID，`[zombie+4]` 是 Board。全局 MT 在 `0x75A910`，包含 624 个状态字和 `+0x9C0` 游标。

语义名称为 `zombie_initialized`，相位为 `zombie_initialize_exit`。这是 **ZombieInitialize 返回给调用者之前**的真实对象状态；某些调用者返回后仍可能调整位置或属性。因此它不冒充“所有调用者完成放置后的最终出生状态”，后者继续通过帧边界状态与具体调用者分析验证。

## runtime 集成

此模块现已由 `determinism::Initialize/Audit/Shutdown` 自动管理并加入 CMake runtime target。现有 runtime 调用审计 API 即可，不要重复安装。下面的底层接口示例用于说明生命周期；独立集成时才直接调用。

把 `determinism/spawn_hook.cpp` 加入现有 runtime DLL，使用现有 `avz/framework/inc`、`runtime/vendor` include 与 MinHook 源，不链接第二份 MinHook 实现。

```cpp
#include "determinism/spawn_hook.hpp"
std::string error;
// 在现有目标签名已通过、模拟未运行的游戏线程上安装。
if (!lvz::determinism::InstallSpawnHook(error)) throw std::runtime_error(error);

// 每个动作/推进边界之前更新标签；参数采用控制器自己的版本。
lvz::determinism::SetSpawnBoundary(tick, revision, segment);
// 原版推进/动作……
auto births = lvz::determinism::DrainSpawnEvents();
// 将每个事件原样写入实验日志；其 phase 必须保持 initializer exit。
auto health = lvz::determinism::SpawnHookStatus();
if (!health["healthy"].get<bool>()) {
    // 停止严格实验并保留失败；不得静默忽略队列溢出/跨线程调用。
}
// 控制动作完成后清除标签，避免后续初始化沿用旧版本。
lvz::determinism::ClearSpawnBoundary();
// 最后一次 drain 完成后、仍在游戏线程且没有运行中的 initializer：
if (!lvz::determinism::RemoveSpawnHook(error)) {
    // 保留 DLL 加载状态。不能在仍有指向 shim 的 hook 时卸载。
}
```

安装验证两处精确字节及内存范围；失败撤回本模块已创建的 hook。卸载先比较当前跳转字节，所有权改变时拒绝覆盖。模块只启停自己的两个 hook，永远不使用 `MH_ALL_HOOKS` 或全局 `MH_Uninitialize`。安装/拆卸必须由唯一 runtime 的游戏线程负责，不能从 DllMain 或通信线程直接进行。

记录包含初始化输入、父僵尸 ID、对象 ID/代次/槽位、调用点 RVA、控制器边界标签、前后游戏时钟、初始化结束的 row/type/X/Y/speed/variant 和全部已声明标量字段，以及全局 MT 的前后完整状态与游标。浮点只保存原始 32 位。原始对象中的 App/Board 指针与 padding 不出现在序列化事件中。

hook 内只复制到预分配队列：最多 1024 条未 drain 记录、32 层初始化调用深度，没有 JSON、文件 I/O 或堆分配。正常每个 post-step drain 一次。队列满、嵌套超限、不可读状态、生成 ID 改变、跨线程调用等均令健康状态失败；不能将不完整记录标为严格验收通过。异常展开、longjmp 或不经正常 epilogue 的强制终止不属于受支持轨迹，遗留 depth 会使边界健康检查失败。

两处 x86 shim 保存/恢复 EFLAGS、全部通用寄存器和对齐的 512 字节 FXSAVE 状态，包含 x87 栈/控制状态、MXCSR 与 XMM。调用 C++ 观测器时保持栈对齐，清除 DF 并在返回前恢复原值，同时保留 LastError。原始机器码由 MinHook trampoline 原样执行，观测器不替换游戏的 RNG 算法。

## 已完成的测试与真实验收要求

`tests/determinism_spawn_hook.cpp` 在独立测试进程中构造相同寄存器/栈 ABI 的小函数，通过真实 MinHook 与同一套 shim 对照：

- 未知签名拒绝；多个分支与嵌套调用均捕获。
- hook 开/关时完整 GPR、EFLAGS、x87、XMM 保存状态及对象写入一致。
- 初始化参数、bool 高位隔离、生成 ID、浮点低位、完整 MT 状态游标及边界标签正确。
- 跨线程调用、容量溢出、其他模块改动 hook 所有权时显式拒绝或报告失败。

该测试证明 ABI 和队列机制，不替代原版验收。root 应在隐藏窗口的真实引擎中检查普通僵尸、小丑、红眼投掷小鬼、舞王/伴舞、冰车/雪橇、蹦极等路径；比较启用/关闭该模块的原版逐帧摘要与最终随机状态，并检查所有记录的 `SpawnHookStatus.healthy`。`original_game_live_validated` 默认保持 false，外部验收证据成立后才可更新能力记录。

## CaptureState 范围复核

本次未修改现有 `audit.cpp`。已向 root 提交以下具体检查点：

1. 当前实体标量段已排除 App/Board 指针及已知 bool padding。对象池未占用槽只保留 free-list/ID；不应扩大为整块对象内存散列。
2. Board 波表 `0x6B4 + wave*200` 每波 50 槽中，首个 `-1` 之后的尾槽不保证由 `PickZombieWaves` 初始化；规范化宜截到首个终止符，不能将未使用尾槽当作出怪差异。
3. `0x240..0x4C8` 为构造阶段随机格子外观/偏移；如果 B(0) 才播种，它们可能已经不同。这是不同初态，不是恢复 MT 失败。应固定构造流程或明确视觉审计分组。
4. 动画/粒子关联 ID（例如植物 `0x94..0xAC`、僵尸 `0x110/0x118/0x140/0x144/0x150`、Board `0x63C..0x654`、`0x5620..0x5744`）受对象池历史影响；候选 `DataArrayFreeAll` 不重设下一代次。不得直接删除关联以掩盖分叉，应固定初始化或建立经验证的关联规范化。
5. 雾网格 `0x4C8..0x5C4` 在 `UpdateFog` 修改，不能仅凭看起来是绘制数据就删除。`0x5570` DrawCount 和 `0x577C..0x5790` 墙钟/绘制统计当前已排除；需要单独检查排除 DrawCount 后是否遗漏它触发的效果初始化，因为 Board 更新代码会用它决定创建池水粒子。
