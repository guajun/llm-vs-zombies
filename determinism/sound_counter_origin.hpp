#pragma once
#include <nlohmann/json.hpp>
#include <functional>
#include <cstdint>
#include <string>
namespace lvz::determinism {
inline constexpr char SoundCounterMode[]="sound_effects_counter_origin_v1";
nlohmann::json SoundCounterManifest();
// Lives for the recording, deliberately not reset on controller epoch changes.
// Never writes the bootstrap counter or any original game memory.
class SoundCounterOrigin {
public:
    using Json=nlohmann::json;
    bool Bound()const{return bound_;}
    Json Present(Json audio,const Json& raw);
    Json Apply(uint32_t seed,bool enabled,bool seeded,bool warmed,bool appAnchored,
        const std::function<Json()>& capture,const std::function<Json()>& sampledRaw);
    Json Boundary(const Json& envelope,const Json& state,const Json& sampledRaw)const;
    Json Health(Json health,const Json& state,const Json& sampledRaw)const;
    const std::string& Fault()const{return fault_;}
private:
    bool attempted_=false,bound_=false,originAssigned_=false,seen_=false;
    uint32_t origin_=0,lastRaw_=0;
    std::string fault_;
    void CheckBound()const;
    uint32_t CheckSample(const Json& state,const Json& raw)const;
};
}
