#include "app_update_anchor.hpp"
#include "memory.hpp"
#include "launcher/silent_audio.hpp"
#include <limits>
namespace lvz::determinism {
using Json=nlohmann::json;
Json AppUpdateAnchorManifest(){return {{"mode",AppUpdateAnchorMode},{"available",true},
    {"field","LawnApp+0x484/mUpdateCount"},{"phase","after_seed_before_warm"},
    {"requires_audio_mode",lvz::silentaudio::Mode},{"once_per_epoch",true},
    {"value_range",{0,INT32_MAX}},{"original_engine_bitwise_unmodified",false},
    {"live_verified",false},{"receipt_event","app_update_anchored"},
    {"demo_policy","reject_original_demo_recording_or_playback"}};}
namespace {
bool U32(const Json& value){return value.is_number_integer()&&(value.is_number_unsigned()?value.get<uint64_t>()<=UINT32_MAX
    :value.get<int64_t>()>=0&&value.get<int64_t>()<=UINT32_MAX);}
void NoDemo(const Json& demo){
    if(!demo.is_object()||demo.size()!=5)throw std::runtime_error("App anchor missing demo guard evidence");
    for(auto key:{"00000510","00000511","00000578","0000049c","000004a0"})
        if(!U32(demo.at(key)))throw std::runtime_error("App anchor invalid actual demo field");
    if(demo.at("000004a0").get<uint32_t>()>255)throw std::runtime_error("App anchor demo marker is not a byte");
    if(demo.at("00000510")!=0||demo.at("00000511")!=0)throw std::runtime_error("App anchor rejects original demo recording/playback");
}
void EmptyAudio(const Json& state){
    const auto& audio=state.at("sound_effects");
    if(audio.at("mode")!=lvz::silentaudio::Mode||audio.at("slots_empty")!=true||audio.at("patch_owned")!=true
        ||audio.at("errors")!=0||!U32(audio.at("calls")))throw std::runtime_error("App anchor requires healthy allocation-none audio");
    const auto& histories=audio.at("histories");const auto& channels=audio.at("channels");
    if(!histories.is_array()||histories.size()!=110||!channels.is_array()||channels.size()!=32)
        throw std::runtime_error("App anchor requires actual complete Foley/channel evidence");
    for(const auto& history:histories){const auto& slots=history.at("slots");
        if(!slots.is_array()||slots.size()!=8)throw std::runtime_error("App anchor missing Foley slots");
        for(const auto& slot:slots)if(!slot.is_array()||slot.size()!=5||!U32(slot[0])||!U32(slot[1])||slot[0]!=0||slot[1]!=0)
            throw std::runtime_error("App anchor rejects nonempty Foley instance/refcount");}
    for(const auto& channel:channels)if(!U32(channel)||channel!=0)throw std::runtime_error("App anchor rejects nonempty sound channel");
}
void Seeded(const Json& state,uint32_t seed){
    const auto& rng=state.at("rng").at("instances");const auto& mt=rng.at("global_mt");
    const auto& words=mt.at("words");
    if(mt.at("cursor")!=624||!words.is_array()||words.size()!=624||rng.at("game_thread_crt").at("state")!=seed)
        throw std::runtime_error("App anchor RNG readback differs from explicit seed");
    uint32_t word=seed?seed:4357u;
    for(unsigned i=0;i<624;++i){if(words[i]!=word)throw std::runtime_error("App anchor MT word differs from explicit seed");word=1812433253u*(word^(word>>30))+i+1;}
}
}
Json InitialAppUpdateAnchor::Apply(uintptr_t address,uint32_t requested,uint32_t seed,bool audioEnabled,
                                  bool seeded,bool warmed,const std::function<Json()>& capture,const std::function<Json()>& captureDemo){
    Json receipt={{"schema","lvz.app-update-anchor.v1"},{"mode",AppUpdateAnchorMode},{"requested",requested},
        {"before",nullptr},{"after",nullptr},{"before_state",nullptr},{"after_state",nullptr},
        {"demo_before",nullptr},{"demo_after",nullptr}};
    bool wrote=false;
    try {
        if(!audioEnabled||!seeded||warmed||attempted_||requested>INT32_MAX)
            throw std::runtime_error("App anchor requires allocation-none, explicit seed, no warm, and an unused initial anchor");
        const auto demo=captureDemo();receipt["demo_before"]=demo;NoDemo(demo);
        auto before=capture();receipt["before_state"]=before;
        EmptyAudio(before);Seeded(before,seed);
        const auto& count=before.at("sound_effects").at("app_update_count");
        if(!U32(count)||count.get<uint32_t>()>INT32_MAX)throw std::runtime_error("App anchor original counter is outside signed range");
        receipt["before"]=count;
        if(!Accessible(address,4,true)||Read<uint32_t>(address)!=count.get<uint32_t>())
            throw std::runtime_error("App anchor writable target differs from actual snapshot");
        attempted_=true;wrote=true;
        // One explicit original field write; even a source no-op is a consumed
        // anchor. A post-write verification failure is never rolled back.
        std::memcpy(reinterpret_cast<void*>(address),&requested,4);
        auto after=capture();receipt["after_state"]=after;
        receipt["after"]=after.at("sound_effects").at("app_update_count");
        receipt["demo_after"]=captureDemo();
        if(receipt["demo_after"]!=demo)throw std::runtime_error("App anchor changed original demo state");
        auto expected=std::move(before);expected["sound_effects"]["app_update_count"]=requested;
        if(after!=expected||Read<uint32_t>(address)!=requested)
            throw std::runtime_error("App anchor changed fields other than the declared actual counter");
        EmptyAudio(after);Seeded(after,seed);applied_=true;
        return {{"ok",true},{"write_attempted",true},{"anchor",std::move(receipt)}};
    }catch(const std::exception& error){return {{"ok",false},{"write_attempted",wrote},{"error",error.what()},{"anchor",std::move(receipt)}};}
}
}
