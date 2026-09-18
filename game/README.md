# 本地游戏副本

`original/` 来自本机 `D:\pvz\Plants_Vs_Zombies_V1.0.0.1051_EN`，复制日期 2026-09-18。源目录未修改。EXE/DAT/资源包/DLL 的哈希见根目录 `dependencies.lock.json`。

版本依据为源目录名称，尚未运行并验证内存布局；AvZ 使用硬编码的 32 位游戏地址，实际注入前必须确认是兼容的英文 1.0.0.1051 版本。

用户档位置可能在 `C:\ProgramData\PopCap Games\PlantsVsZombies\userdata`。游戏副本不等于独立用户档沙箱；当前启动脚本不替换用户档。官方两仪参考存档单独保存在 `experiments/scenarios/liangyi`，应在确认模式和用户编号、备份原存档后再准备实际试验。

游戏文件被 `.gitignore` 排除。迁移源码时，从你本机的游戏副本重新放入此目录；bootstrap 不下载游戏。
