#pragma once
#include <filesystem>
#include <windows.h>
#include <nlohmann/json.hpp>
namespace lvz::determinism::silentaudio {
void Initialize(const std::filesystem::path& run);
bool Enabled();
nlohmann::json Manifest();
nlohmann::json Snapshot();
nlohmann::json Health();
#ifdef LVZ_SILENT_AUDIO_TESTING
void SetHealthFixture(nlohmann::json (*snapshot)(),DWORD (WINAPI* query)(void*));
#endif
}
