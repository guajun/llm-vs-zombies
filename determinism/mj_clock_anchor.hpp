#pragma once
#include <nlohmann/json.hpp>
#include <cstdint>
#include <functional>
namespace lvz::determinism {
inline constexpr char MjClockAnchorMode[]="initial_mj_clock_anchor_v1";
nlohmann::json MjClockAnchorManifest();
// One fixed-target write to LawnApp+0x838 (AvZ MjClock, decompiled LawnApp
// mAppCounter) after the App update anchor and before any warm draw. The
// receipt keeps the real before/after values; cross-run before values differ.
class InitialMjClockAnchor {
public:
    using Json=nlohmann::json;
    void Reset(){attempted_=applied_=false;}
    bool Applied()const{return applied_;}
    Json Apply(uintptr_t address,uint32_t requested,uint32_t seed,bool audioEnabled,bool appAnchored,
               bool seeded,bool warmed,const std::function<Json()>& capture);
private:
    bool attempted_=false,applied_=false;
};
}
