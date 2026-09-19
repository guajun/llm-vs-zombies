# AvZ 框架与项目接入

`framework/` 是固定提交的官方 [AsmVsZombies](https://github.com/vector-wlc/AsmVsZombies) Git 子模块，提供游戏内 C++ 操作、结构访问和回调。它与用于收集阵型打法的 AvZScript 脚本库用途不同；本项目的运行依赖是该框架子模块。上游说明保留在 [framework/README.md](framework/README.md)。

在项目根目录准备并构建：

```powershell
git submodule update --init --recursive
./tools/bootstrap.ps1
./tools/build-avz.ps1
```

项目 CMake 将框架与 `logger/avz`、`runtime`、`determinism`、`recording` 共同构建为常驻 `build/recorder.dll`。[avz_overlay.cmake](../runtime/avz_overlay.cmake) 在构建目录生成有原文件哈希检查的适配副本，接入受控更新与暂停、非阻塞初始化等行为。上游子模块无需直接改写；本机 `avz/runtime/` 工具材料被 Git 忽略，不是另一份公开框架来源。

策略执行使用 Python `Client` / REPL 经命名管道调用已加载的 DLL。每次动作带观察版本，返回实际成功/失败和执行帧数；思考期间停在边界，因此模型每轮不需要调用 C++ 编译器。修改原生执行器或新增底层能力时仍需重新构建并记录新的模块身份。入口与例子见[项目说明](../README.md)、[运行时协议](../docs/runtime-protocol.md)和[评测接口](../docs/evaluation.md)。

AvZ 的精确键控不自动保证跨进程完整确定性。本项目另行记录初始化配方、已识别 RNG、每个受控更新的状态与原始旁证，并通过[冷启动动作重放](../docs/engine-replay.md)验证已捕获范围。当前实验中的粒子、绘制及显式音效模式有各自声明，不能称为未经修改的原版逐位重放。隐藏窗口仍依赖原版 Win32/DirectDraw 初始化；完整两旗和十次冷启动验收尚未完成，实际范围见[实机记录](../docs/headless-validation.md)。
