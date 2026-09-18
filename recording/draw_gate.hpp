#pragma once
#include <cstdint>
#include <string>
#include <nlohmann/json.hpp>

namespace lvz::recording {
inline constexpr const char* DrawMode="deterministic_draw_schedule_v1";
using DrawReadyPredicate=bool(*)();
// Install only on the established game thread. The gate remains installed and
// blocked after recording closes: the resident DLL is process-lifetime pinned.
bool InstallDrawGate(uint32_t thread,DrawReadyPredicate ready,std::string& error);
bool DrawEntryVerified() noexcept;
void CheckDrawGate();
bool DrawControlled(void* manager);
void SealDrawGate();
void RecordDrawnFrame(bool warm);
nlohmann::json DrawScheduleSnapshot();
nlohmann::json DrawGateManifest();
nlohmann::json DrawGateStatus();
#ifdef LVZ_DRAW_GATE_TESTING
bool InstallDrawGateForTest(uintptr_t entry,uint32_t thread,DrawReadyPredicate ready,std::string& error);
bool RemoveDrawGateForTest(std::string& error);
#endif
}
