# 后台实验启动器

启动器对应 issue #4。默认模式是 **隐藏窗口运行原版引擎**：保留窗口、消息循环与原本渲染路径，通过本机 IPC 操作。它不需要鼠标键盘自动化，不切换桌面、不抢焦点，也不声称已经实现与可见模式等价的无绘制模拟。实机验收结果必须单独记录。

## 本地前置条件

- `game/original` 中的本地合法游戏副本；EXE、DAT、资源包及 bass.dll 与 `dependencies.lock.json` 一致。
- `game/local-engine/PlantsVsZombies.exe`：从本机原包装器取得的真实原始引擎 PE，不能使用内存映像重建文件替代。当前锁定 SHA256 为 `f9669af338964787a3785a7895791297d599295b8bb669b0db49443f736a1322`。此文件不进入仓库，工具也不下载游戏。
- 已构建的 `build/recorder.dll` 和 `launcher/build.ps1` 产生的 32 位 helper/DLL。
- 官方参考存档 `experiments/scenarios/liangyi/game1_13.dat` 与锁文件一致。

```powershell
.\tools\build-avz.ps1
.\launcher\build.ps1
.\tools\launch-experiment.ps1 -Name headless-001
```

最后一条命令创建新 run、复制私有环境、后台启动、注入统一 runtime，并通过 IPC 选择模式与卡片。成功结果包含 `pid`、`creation_time`、`endpoint`、`hello`、`initial_observation`。对默认 scenario `liangyi`，初始化只在观察确认实际 `Scene==3`、4 曾/6 花/2 伞与指定卡序一致后返回 ready；其它 scenario 按各自注册表规则校验（`launcher` CLI 的 `--scenario`，见下）。使用 `-NoInitialize` 可仅启动并建立 IPC，供诊断或其他受控初始化流程使用。重复实验使用新的 Name。

```powershell
.\tools\launch-experiment.ps1 -Name headless-001 -Stop
```

Stop 校验 PID、进程创建时间与完整引擎路径，仅终止本 run 创建的进程。结束实验前应由客户端调用 runtime 的停止记录方法，让日志完整落盘；强制终止不是正常录制收尾。

## Scenario 注册表

`src/llm_vs_zombies/launcher.py` 的 `SCENARIOS` 把"名称 → 存档 + 运行配置 + 期望卡片顺序 + 期望场景 + 校验规则"放在一处；默认是 `liangyi`，缺省行为与只有两仪时逐字相同：

| scenario | 存档 | 运行配置 | 卡序 | 期望场景 | 校验 |
|---|---|---|---|---|---|
| `liangyi` | `experiments/scenarios/liangyi/game1_13.dat` | `experiments/configs/liangyi.json` | `[16,30,14,63,15,2,20,17,8,27]` | `Scene()==3`（雾夜）| 精确阵型（4 曾/6 花/2 伞/6 南瓜/8 荷叶）+ 卡序 |
| `jingdian12` | `experiments/scenarios/jingdian12/game1_13.dat` | `experiments/configs/jingdian12.json` | `[14,63,35,15,16,17,2,27,30,8]` | `Scene()==2`（泳池）| 战斗 + 卡序 + 至少 12 门玉米加农炮（刻意宽松）|

- 固定输入（`REQUIRED_INPUTS`）由所选 scenario 推导：公共游戏文件加该 scenario 的存档，全部按 `dependencies.lock.json` 核对 SHA256 与尺寸；`launcher.json.input_hashes` 与 `scenario` 字段记录本次实际使用的那一份。
- run 目录的 `config.json` 声明了 `scenario_save` 时，`prepare` 会核对它与所选 scenario 的存档一致：拿 A 场景的配置建 run、再用 B 场景启动，会在复制沙盒前报错，不会出现 inputs 与沙盒来自两个场景的混合证据。
- `verify_scenario()` 按 scenario 分派，使用注册表的期望场景号而不是写死的 3。`jingdian12` 的阵型细节**故意**不校验（真机载入前没有可靠形状），收紧待真机；见 `experiments/scenarios/jingdian12/README.md`。
- 未知 scenario 名在任何进程启动前报错，并列出已知名称。
- 评测侧入口：`evaluation plan --scenario jingdian12`（写进 plan 的 `scenario` 字段，缺省 `liangyi`）；`evaluation run` 连同 `launcher` 的 `--scenario` 一起透传。长局用法见 `docs/evaluation.md`。

## 隔离与初始化流程

1. 所有固定本地输入先核对 SHA256 与尺寸。recorder.dll 还必须与本 run 创建时的绑定哈希一致。
2. 资源、引擎、bootstrap、runtime 复制进 `experiments/runs/<name>/sandbox`。原版会自行改变工作目录，因此不用仅设置 cwd 的方式假装隔离。复制后的资源逐文件记入哈希清单；每局有独立 `recorder.cfg`，不会依赖全局激活状态。
3. 生成单个 `Experiment` 用户的合成档案（ID 1、已通关、10 卡槽），并放置所选 scenario 的参考存档（缺省为原始两仪存档）；完全不读取、复制或修改玩家真实 `users.dat`。固定档案只定义进度和选择条件，不代表已固定完整 RNG。
4. 原始引擎以 `CREATE_SUSPENDED` 创建，主线程恢复前注入 bootstrap 并完成显式初始化。远程线程每一步最多等待 10 秒，失败终止刚创建的进程。bootstrap 安装失败不会继续启动游戏。
5. bootstrap 只改本进程主 EXE 的导入表：`GetProcAddress` 获取的 `SHGetFolderPathA/W` 将常见 appdata 路径重定向到私有目录；PopCap 注册表入口重定向到 `HKCU\Software\LLMVsZombies\<PID>-<creation time>`；游戏 mutex 添加 PID；阻止显示/激活窗口与切换显示模式；屏蔽外部程序启动和阻塞消息框，错误写入日志。没有全局 hook，没有替换系统 DLL。原窗口与 DirectDraw 仍存在。
6. 按已记录的 PID/创建时间/EXE 路径注入该 run 自己的 runtime.dll，然后连接 `\\.\pipe\llm-vs-zombies-<pid>`。游戏线程执行 `initialize`，使用所选 scenario 的 mode 与完整卡序（缺省 `liangyi` 为 mode 13 与 `[16,30,14,63,15,2,20,17,8,27]`，63 为模仿冰）。客户端轮询实际初始化状态，错误与超时均停止自己创建的进程并保留证据。

现阶段只支持已锁定 1.0.0.1051 引擎。引擎的动态 API 路径、存档格式和窗口行为需要该版本实机证据支持；不得把主 EXE IAT 隔离泛化成适用于任意游戏/任意插件的安全沙箱。DLL 使用 ANSI 路径的原版约束仍在，启动器拒绝过长或当前系统编码不可表示的实验路径。

隐藏窗口的实际进场使用已核验签名的 `LoadingCompleted` 与 `ContinueDialog::ButtonDepress` 内部入口。后者传入正确的 ButtonListener 子对象，完成继续存档操作。bootstrap 将本进程 `GetActiveWindow` 查询映射到它自己的隐藏 HWND，让游戏内部控件接受 IPC 动作；不会改变系统实际活动窗口、前台窗口或发送 OS 输入。初始化 seed 默认为 0，可通过 Python `start(..., seed=...)` 或 launcher CLI `--seed` 指定，先在游戏线程播种，再创建场景。

## 分支身份（`LVZ_BRANCH_ID`）

运行时的分支作用域（[分支作用域隔离](分支作用域隔离.md)，issue #32）与证据树的 `branch_id`（[evidence_tree.py](../src/llm_vs_zombies/evidence_tree.py)）必须是**同一个字符串**，否则去重域与证据归档会指向两个不同名字。启动器是这条链的唯一入口：

| 阶段 | 值 / 落点 |
|---|---|
| 决定（`prepare`） | 缺省 = **run 目录名**；显式 `--branch-id` / `start(..., branch_id=...)` 可覆盖。两者都按 `[A-Za-z0-9][A-Za-z0-9._:-]{0,63}` 校验，非法或超长**在创建任何进程之前**就以明确错误失败 |
| 记录 | `launcher.json` 的 `branch_id`、`branch_source`（`run-directory-name` / `explicit`）、`branch_channel`（`LVZ_BRANCH_ID`） |
| 注入 | 原生启动器 `launch ENGINE BOOTSTRAP SANDBOX CWD RECEIPT BRANCH [AUDIO_MODE]`：先校验，再 `SetEnvironmentVariableW(L"LVZ_BRANCH_ID")`，然后才 `CreateProcessW`，所以子进程只可能继承合法的 id |
| 封口 | `sandbox/native-receipt.json` 在主线程恢复**之前**写入 `branch_id`，即使启动客户端中途中断，也有"引擎实际继承了什么"的证据 |
| 生效 | runtime 读 `LVZ_BRANCH_ID` 成为本实例 scope；`hello.branch` / `status.branch` 回报 `{schema, mode, branch_id, parent_branch_id, origin, dedup_key}` |
| 复核 | `start()` 逐项核对三处一致：原生回执、`hello.branch.branch_id`、本 run 声明的 id。任一处不符立即停止自有进程并保留失败档，不静默降级 |
| 再记录 | run manifest 的 `branch`（仅当 runtime 声明 scope 时）、轨迹初始 marker 与重放报告的 `branch_scope` |

兼容性：`LVZ_BRANCH_ID` 未设置时 runtime 仍回退到"会话目录名 + PID"的进程实例标签（旧行为保持不变）；本通道引入之前写下的 `launcher.json`（没有 `branch_id`/`branch_channel`）不参与注入与复核，历史档照旧可读、可校验、可重放。按 R7，分支只进入 `hello`/`status`/manifest/轨迹 marker/重放报告，**不进入**原生审计 manifest、状态摘要、draw/engine_call 证据，因此新旧记录仍可直接比较。原 `run` 名只允许字母/数字/`-`/`_`（[cli.py](../src/llm_vs_zombies/cli.py)），天然落在该语法内；目录名不可用时启动器会要求显式 `--branch-id`，而不是改名。

## 诊断材料

- `launcher.json`：输入/模块/资源/合成档案哈希，进程身份，分支身份（`branch_id`/`branch_source`/`branch_channel`/`runtime_branch`），阶段与错误，初始观察。
- `sandbox/bootstrap.log`：实际安装的 hook、重定向路径、隔离 ready、隐藏窗口和消息框错误。
- `sandbox/native-receipt.json`：主线程恢复前已成功安装隔离的进程凭据（含实际注入的 `branch_id`）。即使启动客户端在取得返回值前中断，Stop 也能从此恢复 PID/创建时间并再次核验身份。
- `sandbox/modules/runtime-diagnostics.log`：runtime 自身诊断（若 runtime 生成）。
- `decisions/launcher.jsonl`：启动与初始化 IPC 的客户端审计。
- `observations/initial.json`：初始化实际返回的观察。

失败时不删除证据目录，不能直接重用同一个 run 接着录制。启动器的私有注册表配置使用 volatile 叶键；路径含进程创建时间，避免 PID 复用继承上一次实验设置。

## 验收层次

Python 测试覆盖固定输入拒绝、全新目录限制、合成用户档结构、原目录保持不变、实际场景与卡序检查。`launcher/isolation_fixture.cpp` 是不依赖游戏的原生验证程序：经过同一路径启动后，输出私有 appdata、隔离注册表访问结果、窗口隐藏、前台窗口未变化证据，以及子进程实际继承的 `LVZ_BRANCH_ID`（`tools/ci.ps1` 会与 `launch` 收到的 id 逐字比对，见 §分支身份）。它用于先验证 `CREATE_SUSPENDED + LoadLibrary` 与 bootstrap 顺序。

实机进一步需要核实原用户档/配置前后不变、隐藏/失焦不阻塞 IPC 与单步、两仪存档实际加载与卡序、重复冷启动初态以及完整两旗。仅有启动成功、私有路径或文件哈希一致，均不证明确定性；完整状态/随机状态的比对归入确定性与 engine replay 验收。

档案格式参考固定版本的 [ProfileMgr](https://github.com/ruslan831/PlantsVsZombies-decompilation/blob/8a2d121899ba5cb4df644cd7d2e4c1aaf88dd238/Lawn/System/ProfileMgr.cpp)、[PlayerInfo](https://github.com/ruslan831/PlantsVsZombies-decompilation/blob/8a2d121899ba5cb4df644cd7d2e4c1aaf88dd238/Lawn/System/PlayerInfo.cpp) 与 [DataSync](https://github.com/ruslan831/PlantsVsZombies-decompilation/blob/8a2d121899ba5cb4df644cd7d2e4c1aaf88dd238/Lawn/System/DataSync.cpp)。这些是用于推导格式的候选反编译源码；本地实际载入结果才是当前二进制的验收证据。

## 显式音效分配模式

Python `start(..., audio_mode="sound_effects_allocation_none_v1")` 或 launcher CLI 的 `--audio-mode sound_effects_allocation_none_v1` 在主线程首次恢复前启用实验补丁；默认 `original` 不启用。启动器核对实际安装回执、resident hello 和复制的 bootstrap 文件哈希，任何不匹配均停止自有进程，不静默降级。该配置不是设置音量为零，语义与证据范围见[模式说明](silent-audio.md)。

启动失败时，即使写 `launcher.json` 也发生 I/O 错误，仍优先停止已有 PID/创建时间/EXE 身份的自有进程；评测结束时 client/trace 关闭报错同样不会跳过进程清理。关闭失败会保留为错误，不能当作完整成功归档。
