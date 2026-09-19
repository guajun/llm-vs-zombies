#pragma once
#include <nlohmann/json.hpp>
#include <cstdint>
#include <functional>
namespace lvz::determinism {
inline constexpr char AppUpdateAnchorMode[]="initial_app_update_anchor_v1";
nlohmann::json AppUpdateAnchorManifest();
// Shared original-state checks for the two explicit prewarm operations.
void ValidateEmptyInitialAudio(const nlohmann::json& state);
void ValidateSeededInitialRng(const nlohmann::json& state,uint32_t seed);
// The owner-thread controller supplies the paused/tick/entered-call gates.
// This class independently enforces audio state, actual seeded RNG, one write
// per epoch, and full state equality except the single declared field.
class InitialAppUpdateAnchor {
public:
    using Json=nlohmann::json;
    void Reset(){attempted_=applied_=false;}
    bool Applied()const{return applied_;}
    Json Apply(uintptr_t address,uint32_t requested,uint32_t seed,bool audioEnabled,
               bool seeded,bool warmed,const std::function<Json()>& capture,
               const std::function<Json()>& captureDemo);
private:
    bool attempted_=false,applied_=false;
};
}
