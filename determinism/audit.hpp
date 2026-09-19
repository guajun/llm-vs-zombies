#pragma once

#include <filesystem>
#include <string>
#include <nlohmann/json.hpp>
#include "fp_environment.hpp"

// Every API except ValidateTargetImage must run on the runtime's game thread,
// at a stable boundary. This module never installs the AvZ update hook;
// Initialize/Shutdown own the separate ZombieInitialize observation hooks.
namespace lvz::determinism {
bool ValidateTargetImage() noexcept;
void Initialize(const std::filesystem::path& runDir);
nlohmann::json ProbeTarget();
nlohmann::json ActivateFloatingPoint(int ui,uintptr_t board,const nlohmann::json& context);
void CheckFloatingPoint(fpenv::Phase phase,bool required=false);
nlohmann::json FloatingPointEvidence();
// Comparable scalar state + verified semantic animation references, not raw
// opaque animation handles. Per-step raw evidence is persisted separately.
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
