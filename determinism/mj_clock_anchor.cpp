#include "mj_clock_anchor.hpp"
#include "app_update_anchor.hpp"
#include "memory.hpp"
#include "launcher/silent_audio.hpp"
#include <cstring>
#include <limits>
namespace lvz::determinism {
using Json=nlohmann::json;
Json MjClockAnchorManifest(){return {{"mode",MjClockAnchorMode},{"available",true},
    {"field","LawnApp+0x838/mAppCounter"},{"phase","after_seed_before_warm"},
    {"requires_audio_mode",lvz::silentaudio::Mode},{"once_per_epoch",true},
    {"value_range",{0,INT32_MAX}},{"target_policy","explicit_fixed_common_target"},
    {"source_actual_target",false},{"preceded_by","app_update_anchored"},
    {"original_engine_bitwise_unmodified",false},{"live_verified",false},
    {"receipt_event","mj_clock_anchored"}};}
namespace {
bool U32(const Json& value){return value.is_number_integer()&&(value.is_number_unsigned()?value.get<uint64_t>()<=UINT32_MAX
    :value.get<int64_t>()>=0&&value.get<int64_t>()<=UINT32_MAX);}
}
Json InitialMjClockAnchor::Apply(uintptr_t address,uint32_t requested,uint32_t seed,bool audioEnabled,bool appAnchored,
                                 bool seeded,bool warmed,const std::function<Json()>& capture){
    Json receipt={{"schema","lvz.mj-clock-anchor.v1"},{"mode",MjClockAnchorMode},{"requested",requested},
        {"before",nullptr},{"after",nullptr},{"before_state",nullptr},{"after_state",nullptr}};
    bool wrote=false;
    try {
        if(!audioEnabled||!appAnchored||!seeded||warmed||attempted_||requested>INT32_MAX)
            throw std::runtime_error("MJ clock anchor requires allocation-none audio, a completed App anchor, an explicit seed, no warm, and an unused initial anchor");
        const auto before=capture();receipt["before_state"]=before;
        // Independent of the caller: the complete prewarm audio/RNG readbacks
        // must still be the actual seeded allocation-none state.
        ValidateEmptyInitialAudio(before);ValidateSeededInitialRng(before,seed);
        const auto& count=before.at("app").at("mj_clock");
        if(!U32(count)||count.get<uint32_t>()>INT32_MAX)
            throw std::runtime_error("MJ clock anchor original counter is outside the signed range");
        receipt["before"]=count;
        if(!Accessible(address,4,true)||Read<uint32_t>(address)!=count.get<uint32_t>())
            throw std::runtime_error("MJ clock anchor writable target differs from the actual snapshot");
        attempted_=true;wrote=true;
        // One explicit original field write to the fixed declared target. A
        // post-write verification failure is preserved, never rolled back.
        std::memcpy(reinterpret_cast<void*>(address),&requested,4);
        auto after=capture();receipt["after_state"]=after;
        receipt["after"]=after.at("app").at("mj_clock");
        auto expected=std::move(before);expected["app"]["mj_clock"]=requested;
        if(receipt["after"]!=requested||after!=expected||Read<uint32_t>(address)!=requested)
            throw std::runtime_error("MJ clock anchor changed fields other than the declared absolute counter");
        ValidateEmptyInitialAudio(after);ValidateSeededInitialRng(after,seed);applied_=true;
        return {{"ok",true},{"write_attempted",true},{"anchor",std::move(receipt)}};
    }catch(const std::exception& error){return {{"ok",false},{"write_attempted",wrote},{"error",error.what()},{"anchor",std::move(receipt)}};}
}
}
