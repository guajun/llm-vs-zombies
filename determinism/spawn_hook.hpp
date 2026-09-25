#pragma once
#include <cstdint>
#include <string>
#include <nlohmann/json.hpp>

namespace lvz::determinism {
// One destructive drain of the probe's bounded queue, split into two
// projections of the same records:
//  - legacy: the existing lvz.spawn.v1 records (unchanged shape);
//  - lifecycle: lvz.lifecycle-event.v1 records carrying the shared
//    capture_sequence, invocation/parent relation, entity identity and the
//    classification fields from issue #111 stage B.
// The recorder adapter is the single consumer of this batch; calling any
// drain twice for the same boundary yields an empty second batch.
struct SpawnBatch {
    nlohmann::json legacy = nlohmann::json::array();
    nlohmann::json lifecycle = nlohmann::json::array();
    uint64_t count = 0;
};
// Call on the initialized game thread, while no original update is running.
// Installs two hooks: ZombieInitialize entry and its shared normal epilogue.
bool InstallSpawnHook(std::string& error);
bool RemoveSpawnHook(std::string& error);
void SetSpawnBoundary(uint64_t tick, uint64_t revision, uint32_t segment, uint64_t engineCallId=0);
void ClearSpawnBoundary();
// Drain after the simulation step, before another step. No serialization or
// allocation occurs inside the hooked initializer itself.
SpawnBatch DrainSpawnBatch();
nlohmann::json DrainSpawnEvents();
nlohmann::json SpawnHookStatus();

// Snapshot of the controlled boundary the audit host last announced. Probes
// that run inside the game update copy these numbers at capture time so a
// drained record never borrows a later boundary.
struct CaptureBoundary {
    uint64_t tick = 0, revision = 0, engineCallId = 0;
    uint32_t segment = 0;
    bool valid = false;
};
CaptureBoundary CurrentCaptureBoundary() noexcept;

#ifdef LVZ_LIFECYCLE_PROBES_TESTING
// Test-only boundary setter so the probes fixture can exercise controlled
// capture without installing the game-thread spawn hook.
void SetSpawnBoundaryForTest(uint64_t tick, uint64_t revision, uint32_t segment, uint64_t engineCallId = 0);
void ClearSpawnBoundaryForTest();
#endif

#ifdef LVZ_SPAWN_HOOK_TESTING
// Only compiled into the standalone ABI test. Not exposed by the runtime DLL.
bool InstallSpawnHookForTest(uintptr_t entry, uintptr_t epilogue,
                             uintptr_t mtState, std::string& error);
#endif
}
