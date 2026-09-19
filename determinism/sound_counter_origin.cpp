#include "sound_counter_origin.hpp"
#include "app_update_anchor.hpp"
#include "launcher/silent_audio.hpp"
#include <windows.h>
namespace lvz::determinism {
using Json=nlohmann::json;
namespace {
uint32_t U32(const Json& value){
    if(!value.is_number_integer()||(value.is_number_unsigned()?value.get<uint64_t>()>UINT32_MAX:
        value.get<int64_t>()<0||value.get<int64_t>()>UINT32_MAX))throw std::runtime_error("sound counter requires uint32 evidence");
    return value.get<uint32_t>();
}
uint32_t RawCalls(const Json& raw){
    // Status() verifies resident patch bytes and owner. Retain that exact full
    // activation-shaped receipt, including absolute addresses, in the binding.
    if(!raw.is_object()||raw.at("mode")!=lvz::silentaudio::Mode||raw.at("installed")!=true
        ||raw.at("before_primary_thread_resume")!=true||raw.at("phase")!="sealed_before_resume"
        ||raw.at("owner_pinned")!=true||raw.at("patch_owned")!=true||raw.at("errors")!=0
        ||U32(raw.at("primary_thread"))!=GetCurrentThreadId()||raw.at("preexisting_app")!=0)
        throw std::runtime_error("sound counter raw status unhealthy or wrong original thread");
    return U32(raw.at("calls"));
}
}
Json SoundCounterManifest(){return {{"mode",SoundCounterMode},{"available",true},
    {"field","/sound_effects/calls"},{"phase","after_seed_before_app_anchor_and_warm"},
    {"once_per_recording",true},{"counter_bits",32},{"wrap_allowed",false},{"raw_counter_reset",false},
    {"snapshot_representation","native_calls_since_origin"},{"receipt_event","sound_counter_origin_bound"},
    {"raw_boundary_evidence","sound-counter-raw.jsonl"},{"live_verified",false}};}
Json SoundCounterOrigin::Present(Json audio,const Json& raw){
    try {
        if(!fault_.empty())throw std::runtime_error(fault_);
        const auto calls=RawCalls(raw);
        if(U32(audio.at("calls"))!=calls||(seen_&&calls<lastRaw_)||(originAssigned_&&calls<origin_))
            throw std::runtime_error("sound counter absolute sample mismatch, regression or wrap");
        lastRaw_=calls;seen_=true;
        audio["counter_scope"]=originAssigned_?"experiment":"bootstrap_lifetime";
        audio["calls"]=originAssigned_?calls-origin_:calls;
        return audio;
    }catch(const std::exception& error){if(fault_.empty())fault_=error.what();throw;}
}
Json SoundCounterOrigin::Apply(uint32_t seed,bool enabled,bool seeded,bool warmed,bool appAnchored,
    const std::function<Json()>& capture,const std::function<Json()>& sampledRaw){
    Json receipt={{"schema","lvz.sound-counter-origin.v1"},{"mode",SoundCounterMode},
        {"origin_raw_calls",nullptr},{"raw_before",nullptr},{"raw_after",nullptr},
        {"before_state",nullptr},{"after_state",nullptr}};
    bool boundAttempt=false;
    try {
        if(!enabled||!seeded||warmed||appAnchored||attempted_||!fault_.empty())
            throw std::runtime_error("sound counter origin requires an unused seeded pre-App-anchor prewarm silent recording");
        auto before=capture();receipt["before_state"]=before;
        auto raw=sampledRaw();receipt["raw_before"]=raw;
        ValidateEmptyInitialAudio(before);ValidateSeededInitialRng(before,seed);
        const auto total=RawCalls(raw);receipt["origin_raw_calls"]=total;
        if(before.at("sound_effects").at("counter_scope")!="bootstrap_lifetime"
            ||U32(before.at("sound_effects").at("calls"))!=total)
            throw std::runtime_error("sound counter origin before-state does not match same raw sample");
        attempted_=boundAttempt=true;originAssigned_=true;origin_=total;
        // This is a representation boundary only. There is no store to the
        // bootstrap counter, engine state, RNG or histories, including on error.
        auto after=capture();receipt["after_state"]=after;
        receipt["raw_after"]=sampledRaw();
        auto expected=std::move(before);expected["sound_effects"]["calls"]=0;
        expected["sound_effects"]["counter_scope"]="experiment";
        if(receipt["raw_after"]!=raw||after!=expected)
            throw std::runtime_error("sound counter binding changed raw status or captured game state");
        ValidateEmptyInitialAudio(after);ValidateSeededInitialRng(after,seed);bound_=true;
        return {{"ok",true},{"bind_attempted",true},{"counter_origin",std::move(receipt)}};
    }catch(const std::exception& error){
        if(boundAttempt&&fault_.empty())fault_=error.what();
        return {{"ok",false},{"bind_attempted",boundAttempt},{"error",error.what()},{"counter_origin",std::move(receipt)}};
    }
}
void SoundCounterOrigin::CheckBound()const{
    if(!fault_.empty())throw std::runtime_error(fault_);
    if(!bound_||!originAssigned_)throw std::runtime_error("sound counter origin was not successfully bound");
}
uint32_t SoundCounterOrigin::CheckSample(const Json& state,const Json& raw)const{
    CheckBound();const auto total=RawCalls(raw);
    if(total<origin_||!seen_||total!=lastRaw_||state.at("counter_scope")!="experiment"
        ||U32(state.at("calls"))!=total-origin_)
        throw std::runtime_error("sound counter evidence is not the same successful snapshot sample");
    return total;
}
Json SoundCounterOrigin::Boundary(const Json& envelope,const Json& state,const Json& raw)const{
    const auto total=CheckSample(state,raw);
    const auto& kind=envelope.at("kind");
    if(kind!="pre_step"&&kind!="post_step")throw std::runtime_error("sound counter raw record needs an authoritative boundary");
    return {{"schema","lvz.sound-counter-raw.v1"},{"seq",envelope.at("seq")},{"kind",kind},
        {"version",envelope.at("version")},{"engine_call_id",envelope.at("payload").at("engine_call").at("engine_call_id")},
        {"raw_calls",total},{"origin_raw_calls",origin_},{"experiment_calls",total-origin_}};
}
Json SoundCounterOrigin::Health(Json health,const Json& state,const Json& raw)const{
    const auto total=CheckSample(state,raw);
    health["counter_scope"]="experiment";health["origin_raw_calls"]=origin_;health["raw_calls"]=total;
    return health;
}
}
