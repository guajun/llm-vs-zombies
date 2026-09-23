// Hosted copy of avz/framework/tutorial/scripts/jing_dian_12/jing_dian_12_co_await.cpp
// (AvZ submodule c42676c, untouched): the 12-cannon script behind P2, with the
// body verbatim and only the entry renamed.
//
// Why it is a copy rather than the upstream file: the tutorial source uses the
// `ACoScript()` macro, which expands to `void AScript() { ... }`. The resident
// runtime already owns AScript() (logger/avz/recorder.cpp: run lock, events,
// stop key), so the hosted build takes the coroutine body and launches it from
// there - see logger/avz/hosted_script.hpp.
//
// Two things to know before enabling this in a live run:
//   * ASetZombies/ASelectCards configure the level. The runtime's own
//     `initialize` request also selects cards, and the runtime refuses a card
//     list whose size does not match the seed slots, so the hosted script
//     should either keep them identical to the request or drop these two calls.
//   * `aCobManager.Fire` is a direct engine call (AAsm::Fire -> PvZ 0x466D50),
//     not one of the runtime's `plant`/`shovel`/`spawn` actions: it is not in
//     the request journal, so a hosted run's fires are not audited as actions.
//     See docs/avz-script-hosting.md.

#include <avz.h>

#include "../hosted_script.hpp"

namespace lvz::hosted {
ACoroutine Script() {
    ASetZombies({
        ACG_3,  // 撑杆
        ATT_4,  // 铁桶
        ABC_12, // 冰车
        AXC_15, // 小丑
        AQQ_16, // 气球
        AFT_21, // 扶梯
        ATL_22, // 投篮
        ABY_23, // 白眼
        AHY_32, // 红眼
        ATT_18, // 跳跳
    });
    ASelectCards({
        AICE_SHROOM,   // 寒冰菇
        AM_ICE_SHROOM, // 模仿寒冰菇
        ACOFFEE_BEAN,  // 咖啡豆
        ADOOM_SHROOM,  // 毁灭菇
        ALILY_PAD,     // 荷叶
        ASQUASH,       // 倭瓜
        ACHERRY_BOMB,  // 樱桃炸弹
        ABLOVER,       // 三叶草
        APUMPKIN,      // 南瓜头
        APUFF_SHROOM,  // 小喷菇
    });

    co_await ATime(1, -599);
    aCobManager.AutoSetList();

    for (int wave = 1; wave < 21; ++wave) {
        if (wave == 10) {
            // wave 10 的附加操作
            // 樱桃消延迟
            co_await ATime(wave, 341 - 373);
            aCobManager.Fire({{2, 9}, {5, 9}});
            co_await ATime(wave, 341 - 100);
            ACard(ACHERRY_BOMB, 2, 9);
        } else if (wave == 20) {
            // wave 20 的附加操作
            // 咆哮珊瑚(炮消)
            co_await ATime(wave, 250 - 378);
            aCobManager.Fire(4, 7.625);
            co_await ATime(wave, 341 - 373);
            aCobManager.Fire({{2, 9}, {5, 9}});
            // wave 20 的附加操作
            // 收尾发四门炮
            co_await ATime(wave, 300);
            aCobManager.RecoverFire({{2, 9}, {5, 9}, {2, 9}, {5, 9}});
        } else {
            // P6
            // 主体节奏
            co_await ATime(wave, 341 - 373);
            aCobManager.Fire({{2, 9}, {5, 9}});

            // wave 9 19 的附加操作
            // 收尾发四门炮
            if (wave == 19 || wave == 9) {
                co_await ATime(wave, 300);
                aCobManager.RecoverFire({{2, 9}, {5, 9}, {2, 9}, {5, 9}});
            }
        }
    }
}
} // namespace lvz::hosted
