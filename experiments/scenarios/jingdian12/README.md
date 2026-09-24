# AvZ 教程经典十二炮参考场景

来源：AvZ 固定提交（`dependencies.lock.json` 的 `avz_commit`）的
`tutorial/scripts/jing_dian_12/game1_13.dat`，与教程脚本
`jing_dian_12_co_await.cpp` 同一目录。本项目保留原始文件，不自动安装进用户档。
`experiments/scenarios/**/*.dat` 被 git 忽略，每个检出由
`tools/prepare-checkout.ps1` 的 `jingdian12 save` 步骤从子模块复制，并核对
`dependencies.lock.json` 里登记的 SHA256 与尺寸。

许可：AvZ 框架为 GPL-3.0（`avz/framework/LICENSE`），该存档是教程配套文件，
本项目按同一许可保留。

## 注册表声明

`src/llm_vs_zombies/launcher.py` 的 `SCENARIOS["jingdian12"]`：

| 项 | 值 |
|---|---|
| 名称 | `jingdian12` |
| 运行配置 | `experiments/configs/jingdian12.json` |
| 存档 | `experiments/scenarios/jingdian12/game1_13.dat`（275,364 字节；SHA256 `5d5165dc46cf30c69358bb55e85d8b94d7b114ce251cf410019f7f1c3d2a7b25`）|
| `game_mode` | 13（与两仪相同；教程脚本自身不调用 `AEnterGame`）|
| 期望场景 | `Board::Scene() == 2`（泳池，`scene_label="pool"`）|
| 卡序 | `[14, 63, 35, 15, 16, 17, 2, 27, 30, 8]` |
| 阵型校验 | 至少 12 门玉米加农炮（`ACOB_CANNON = 47`）|

卡序逐项来自教程 `ASelectCards`，按 `avz/framework/inc/avz_types.h` 的
`APlantType` 取值换算成本仓库观察里的 type id（模仿者卡片记 `49 + 被模仿的种子`，
与运行时 `type == 48 ? imitator_type + 49 : type` 的写法一致）：

| 教程枚举 | 我们的 id | 中文 |
|---|---|---|
| `AICE_SHROOM` | 14 | 寒冰菇 |
| `AM_ICE_SHROOM` | 63 | 模仿寒冰菇 |
| `ACOFFEE_BEAN` | 35 | 咖啡豆 |
| `ADOOM_SHROOM` | 15 | 毁灭菇 |
| `ALILY_PAD` | 16 | 荷叶 |
| `ASQUASH` | 17 | 倭瓜 |
| `ACHERRY_BOMB` | 2 | 樱桃炸弹 |
| `ABLOVER` | 27 | 三叶草 |
| `APUMPKIN` | 30 | 南瓜头 |
| `APUFF_SHROOM` | 8 | 小喷菇 |

## 这一版校验刻意宽松（收紧待真机）

第一版校验只要求处于战斗（`game_ui == 3`）+ 场景号等于注册表值 + 卡序等于上表 +
至少 12 门玉米加农炮。**没有**逐格阵型坐标或精确植物集合：真机载入前，这个存档的
完整阵型没有经过载入验证，凭空写死坐标只会把未证实的假设变成门槛。
真机跑完、拿到实际 `observe` 的 observation 之后再由主 agent 收紧
（候选收紧点：精确玉米加农炮/冰瓜/伞/忧郁菇坐标、初始阳光、荷叶覆盖范围、
僵尸/植物总数）。

静态读取（不属于载入证据）只用于选注册表默认值：按存档格式
（`SaveFileHeader` + `SyncBytes` 分块）与 AvZ 的 `Board` 偏移
（`Scene()=+0x554c`、`Sun()=+0x5560`）读出
`scene=2`、`sun=1600`、`wave=0`、52 株存活植物，其中玉米加农炮 12 门。
同一读取方法在两仪存档上复现了 `docs/雾夜两仪详细教程.md` 已记录的
4 曾/6 花/2 伞/6 南瓜/8 荷叶（Scene 3、8000 阳光），因此这两个数字用作注册表的
初始值；它们仍是静态值，真机 observation 才是验收证据。

## 与托管脚本的分工

`logger/avz/hosted/jing_dian_12.cpp` 是教程正文的托管副本：**保留** `ASetZombies`
（出怪表没有别的下发路径），**去掉** `ASelectCards`（选卡由 runtime 的 `initialize`
请求负责，卡序就是本注册表里那一份；两份同时存在会互相覆盖）。
详见 `docs/avz-script-hosting.md` §3。

## 长局跑法

教程脚本自带 20 波 P6 节奏。计划字段的计数单位是 round：**1 round = 2 flag = 20 波**，
默认 `--rounds-to-complete 1` 就是打完一个 round（两面旗）后收工，两旗不需要再填 2。
要接着打下一个 round 必须在运行中重新提交卡片（打完一个 round 游戏会回到下一轮选卡界面），
当前 runtime 只在 initialization 阶段处理选卡，所以 `--rounds-to-complete >1` 会在源局第一次动作前
被明确拒绝（原始名 `--flags-to-complete` 只作过渡只读别名，见 #97）。真跑长局时
`--tick-budget` 要按实际波间隔放大（泳池无尽每波间隔随实际出怪时间增长）。详见 `docs/evaluation.md`。
