#pragma once
#include <cstdint>
#include <string>
#include <nlohmann/json.hpp>

namespace lvz::determinism {
inline constexpr const char* kParticleShakeMode="deterministic_particle_shake_v1";
bool ValidateParticleShakeTarget() noexcept;
bool InstallParticleShakeHook(std::string& error);
bool RemoveParticleShakeHook(std::string& error);
void SetParticleShakeBoundary(uint64_t tick,uint64_t revision,uint32_t epoch,const std::string& phase,uint64_t engineCallId=0);
void ClearParticleShakeBoundary();
nlohmann::json DrainParticleShakeEvents();
nlohmann::json ParticleShakeStatus();
nlohmann::json ParticleShakeSnapshot();
nlohmann::json ParticleShakeManifest();
#ifdef LVZ_PARTICLE_SHAKE_TESTING
bool InstallParticleShakeHookForTest(uintptr_t previousCall,uintptr_t currentCall,
    uintptr_t originalSrand,uintptr_t appPointer,std::string& error);
#endif
}
