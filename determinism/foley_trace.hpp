#pragma once
#include <filesystem>
#include <nlohmann/json.hpp>
#include <string>
#include <array>
namespace lvz::determinism::foleytrace {
void Initialize(const std::filesystem::path& runDir);
void Boundary(const std::string& kind,const nlohmann::json& payload,const nlohmann::json& version);
void DrainAndCheck(bool allowFault=false);
void Flush();
void Shutdown();
nlohmann::json Manifest();
nlohmann::json Health();
class Phase {
    unsigned previous_=0;
public:
    explicit Phase(bool warm);
    ~Phase();
};
#ifdef LVZ_FOLEY_TRACE_TESTING
// The fixture runs the same real machine-code shims at independent NOP sites.
bool InstallForTest(const std::array<uintptr_t,12>& sites,uintptr_t app,uintptr_t system,
    uintptr_t params,uintptr_t cursor,uint32_t types,std::string& error);
bool RemoveForTest(std::string& error);
nlohmann::json DrainForTest();
void OpenOutputForTest(const std::filesystem::path& directory);
#endif
}
