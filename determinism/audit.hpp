#pragma once

#include <filesystem>
#include <string>
#include <nlohmann/json.hpp>

// Every API except ValidateTargetImage must run on the runtime's game thread,
// at a stable boundary. This module never installs the AvZ update hook;
// Initialize/Shutdown own the separate ZombieInitialize observation hooks.
namespace lvz::determinism {
bool ValidateTargetImage() noexcept;
void Initialize(const std::filesystem::path& runDir);
nlohmann::json ProbeTarget();
nlohmann::json CaptureState();
nlohmann::json CaptureRng();
// This restores RNG only, not the Board or a complete game checkpoint.
bool RestoreRng(const nlohmann::json& snapshot, const std::string& boundary,
                std::string& error);
// Seed the known global MT and game-thread CRT streams; not every instance.
bool SeedRng(uint32_t seed, const std::string& boundary, std::string& error);
nlohmann::json CaptureClocks();
// Explicit initialization/sidecar operation, never a complete checkpoint.
bool RestoreClocks(const nlohmann::json& snapshot, const std::string& boundary,
                   std::string& error);
void Audit(const std::string& kind, const nlohmann::json& payload,
           const nlohmann::json& observation);
void Flush();
void Shutdown();
}
