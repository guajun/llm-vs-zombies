# LLM vs Zombies

PvZ 雾夜两仪实验项目：AvZ 负责游戏内操作和观测，logger 保存结构化记录，replay 用于离线复盘与状态对比。

公开仓库只包含代码、文档及依赖锁定信息；游戏本体和实验记录保留在本机。克隆时使用 `git clone --recurse-submodules`，然后运行 `tools/bootstrap.ps1` 准备工具链和上游参考场景，将自己的兼容游戏副本放入 `game/original/`。

## 当前内容

```text
avz/framework/       固定提交的官方 AvZ 源码（Git 子模块）
avz/runtime/         官方 2.9.2 运行包：头文件、静态库、注入器
logger/avz/          C++ 缓冲记录器及带日志的动作入口
logger/schemas/      事件协议
game/original/      从本机 D:\pvz 复制的游戏本体
replay/viewer/       可导出单文件 HTML 的状态复盘器
src/llm_vs_zombies/  实验管理、校验、对比、导出 CLI（Python 标准库）
experiments/configs/ 实验配置
experiments/scenarios/liangyi/ 官方两仪参考存档
experiments/runs/    每次实验独立目录，含元数据、事件、观察、决策、视频和检查点
docs/               雾夜两仪教程与录制回放方案
tools/              准备、编译、启动、注入及 CLI 脚本
third_party/        已验证哈希的 LLVM-MinGW 编译工具链
tests/              记录完整性、复盘与本机 C++ 写入测试
```

框架固定为 `c42676c269b5b482a1eb9203a5b979e9d8a2a5c7`。游戏来源目录标识为英文 `1.0.0.1051`，实际文件哈希记录在 `dependencies.lock.json`；尚未做本机游戏运行和注入验证。

## 先验证不启动游戏的流程

在本目录打开 PowerShell：

```powershell
.\tools\lvz.ps1 doctor
.\tools\lvz.ps1 demo
```

`demo` 会创建清楚标记为 synthetic 的演示实验，生成 `experiments/runs/<id>/exports/review.html`。用浏览器打开即可拖动时间线、查看植物和僵尸的位置示意及事件。演示数据不是模型打过的对局。

## 录制真实对局

```powershell
.\tools\build-avz.ps1
.\tools\lvz.ps1 new-run --name my-first-run
.\tools\start-game.ps1
.\tools\inject-recorder.ps1
```

1. 注入器选择本项目启动的兼容版本游戏窗口。AvZ 使用固定内存地址，勿选择其他版本游戏。
2. 自行选卡并进入战斗。记录器不修改出怪、不自动操作策略。
3. 当前采样间隔由 `experiments/configs/liangyi.json` 的 `state_interval_ticks` 指定；设为 1 才会每个观测帧记录状态。
4. 在游戏内按数字 **7** 停止本次记录，关闭写入句柄；再执行下面的整理命令。下一次记录创建新实验并重新加载 DLL。

```powershell
.\tools\lvz.ps1 finalize experiments/runs/my-first-run --outcome manual_stop
.\tools\lvz.ps1 validate experiments/runs/my-first-run
.\tools\lvz.ps1 export experiments/runs/my-first-run
```

原版用户档可能位于共享的 ProgramData，复制游戏目录不代表存档隔离。本项目保留官方参考存档，但不会替换你已有的用户档。`game1_13.dat` 的编号不能单凭文件名认定已是当前雾夜游戏模式，需实际加载并核实 `Scene()==3` 后开始正式评测。

## 实验文件与比较

每次 `new-run` 保存配置副本、依赖锁定信息、参考存档副本、本项目实现源码 ZIP 和已编译 DLL 哈希。注入脚本会拒绝使用创建实验后又改变的 DLL。原始画面／模型观察、请求响应、检查点、视频都可以附入同一实验：

```powershell
.\tools\lvz.ps1 attach experiments/runs/my-first-run path/to/response.json --category decisions
.\tools\lvz.ps1 attach experiments/runs/my-first-run path/to/initial.dat --category checkpoints
.\tools\lvz.ps1 compare experiments/runs/my-first-run experiments/runs/my-second-run
```

附入文件须在 `finalize` 前完成；封存后会保存这些文件的 SHA-256。导出的 HTML 可重新生成，原始记录应保持不变。`compare` 返回首个采样时刻或状态差异，不宣称验证了未记录字段、完整 RNG 或整个进程的位级一致性。

## 实现边界

- **已实现**：项目独立游戏副本、固定框架、原生缓冲日志、实体首次／不再观测事件、植物/僵尸/卡片状态采样、出怪表记录、显式动作包装、实验打包与哈希校验、状态对比、HTML 复盘。
- **待接入**：LLM 调用与 IPC 控制、完整游戏 RNG 恢复、精确生成函数钩子、所有对象状态与进程检查点、在原版引擎中按输入确定性重放、按游戏时间抓帧的视频采集。
- 实验目录支持完整收纳一次运行的材料；当前原生采样并不是完整引擎存档。手动鼠标操作和直接调用其他 AvZ 接口的动作未被自动拦截。正式模型动作应统一调用 `lvz::Plant` / `lvz::Shovel` 等记录入口。
- 存档与采样状态不可混用；`zombie_first_observed` 不宣称精确出生时刻。销毁事件只表示离开存活过滤器，不推断死亡原因。
- 记录器在 AvZ 回调边界采样；准确的帧前／帧后关系仍需首次游戏集成实验验证。

## 资料与开发

- [实验就绪实施与验收：GitHub issues](docs/implementation-board.md)
- [雾夜两仪详细教程](docs/雾夜两仪详细教程.md)
- [PvZ 录制与回放方案](docs/PvZ录制与回放方案.md)
- [LLM 交互控制设计：常驻适配器与 Python REPL](docs/LLM交互控制设计.md)
- [原版确定性重放器提案](docs/原版确定性重放器提案.md)
- [logger 使用说明](logger/README.md)
- [replay 协议与后续实现](replay/README.md)
- [验证记录](docs/验证记录.md)

```powershell
$env:PYTHONPATH = "$PWD/src"
python -m unittest discover -s tests -v
```

Python 工具无第三方运行依赖。构建原生模块需要 Python 之外的 CMake、Ninja；LLVM-MinGW 已放入本项目，重新取依赖可运行 `tools/bootstrap.ps1`。无需全局安装 AvZ 或编辑原游戏目录。

AvZ 遵循 GPL-3.0，本项目源码按 GPL-3.0 发布（见 LICENSE）。游戏、第三方工具的许可各自独立；游戏副本不加入 Git，不随源码分发。
