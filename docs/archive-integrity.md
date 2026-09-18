# 实验封存范围

`finalize` 对文件计算 SHA-256 并在 manifest 写入明确的 `archive_policy`。它不复制或公开游戏。新封存范围为：

- `inputs/`、`checkpoints/`、`video/`、`observations/`、`decisions/`、`audit/`、`trajectory/` 下的文件。
- 运行根目录所有 `*.json`，其中 `manifest.json` 是校验清单自身，不自校验；因此 launcher、初始化配方、评测、恢复、初态比较和清理结果均纳入。
- 根目录 `events.jsonl`、`capture.closed`、`evaluation.md`、`README.md`；`summary.json` 先生成再封存。

`sandbox/` 内的私有游戏与用户档、`exports/` 派生页面不在封存范围中。证据目录拒绝符号链接和 junction，避免通过链接引入私有目录。验证不仅比较内容哈希，还要求当前白名单文件集合与清单完全一致，因此封存后增加或删除证据文件也会失败。清单不是数字签名；它检测相对于保存清单的改变，不独立证明发布者身份。

封存应在 native `stop_recording`、所有 `SessionTrace.close()`、进程停止及 launcher/cleanup/trajectory 文件写完之后进行。`capture.lock` 和证据目录中任何 `.lock` 都会阻止封存；过期锁需要先检查崩溃原因，工具不自动删除。哈希前后再次检查文件清单、大小、修改时间及锁；最后原子替换 manifest。它不是任意非协作写进程的文件系统快照，仍须先关闭写入方。

旧封存清单未含 `archive_policy` 时沿用原有哈希验证；工具不会悄悄补写历史证据，也不能因此推定旧包具有新版完整覆盖。

`create_run` 的 `inputs/implementation.zip` 包含项目代码、测试、构建脚本、vendored JSON 与实际使用的 AvZ `inc/src`，并附 `ARCHIVE-REBUILD.md`。游戏、工具链、私有实验目录和编译输出不入包；相应依赖身份来自 lock 文件。独立解压后用已锁定的 LLVM-MinGW、CMake/Ninja 重建，不依赖压缩包具有 Git 元数据，也不保证编译结果逐字节相同。

Launcher 保存真实初始观察后调用 `records.record_initial_state(...)`，将文件路径、SHA-256、实际版本和场景验证结果写回仍处于 recording 的 manifest。可另附同一版本的初始审计快照。`runtime_reported` 与 `audit_coverage` 来源于真实 hello，表明实现报告的能力；`original_engine_replay_verified` 不会因此提升为 true。已 finalized 的 manifest 拒绝此更新。
