#pragma once
#include <filesystem>
#include <windows.h>
#include <nlohmann/json.hpp>
#include <functional>
namespace lvz::determinism::silentaudio {
void Initialize(const std::filesystem::path& run);
bool Enabled();
nlohmann::json Manifest();
nlohmann::json Snapshot();
nlohmann::json Health();
bool CounterBound();
nlohmann::json BindCounterOrigin(uint32_t seed,bool seeded,bool warmed,bool appAnchored,
    const std::function<nlohmann::json()>& capture);
// Read-only evidence cache from the same successful Snapshot sample; no query.
nlohmann::json SampledRawStatus();
nlohmann::json CounterBoundary(const nlohmann::json& envelope,const nlohmann::json& state);
#ifdef LVZ_SILENT_AUDIO_TESTING
void SetHealthFixture(nlohmann::json (*snapshot)(),DWORD (WINAPI* query)(void*));
#endif
}
