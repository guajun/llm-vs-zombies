#pragma once
#include <cstdint>
#include <string>
#include <nlohmann/json.hpp>

namespace lvz::determinism {
// Call on the initialized game thread, while no original update is running.
// Installs two hooks: ZombieInitialize entry and its shared normal epilogue.
bool InstallSpawnHook(std::string& error);
bool RemoveSpawnHook(std::string& error);
void SetSpawnBoundary(uint64_t tick, uint64_t revision, uint32_t segment, uint64_t engineCallId=0);
void ClearSpawnBoundary();
// Drain after the simulation step, before another step. No serialization or
// allocation occurs inside the hooked initializer itself.
nlohmann::json DrainSpawnEvents();
nlohmann::json SpawnHookStatus();

#ifdef LVZ_SPAWN_HOOK_TESTING
// Only compiled into the standalone ABI test. Not exposed by the runtime DLL.
bool InstallSpawnHookForTest(uintptr_t entry, uintptr_t epilogue,
                             uintptr_t mtState, std::string& error);
#endif
}
