#pragma once
#include <cstdint>
#include <filesystem>
#include <nlohmann/json.hpp>
#include <stdexcept>

namespace lvz::flagfixture {
using Json=nlohmann::json;
inline constexpr const char* Mode="test_flag_drop_v1";
inline Json Manifest() {
    return {{"mode",Mode},{"test_fixture",true},{"fair_strategy_evidence",false},
        {"production_readiness_allowed",false},{"spawn_engine_call_id",1},{"drop_head_engine_call_id",2},
        {"verification_engine_call_id",3},{"zombie_type",1},{"row_zero_based",2},{"col_zero_based",8},
        {"sidecar","decisions/flag-drop-fixture.jsonl"},
        {"mutation","original PutZombie then original DropHead(0); no direct game-field writes"}};
}
inline uint64_t ControlledCall(const Json& health,bool ready,uint32_t owner,uint32_t current) {
    if(!health.contains("active_call_id")||health.at("active_call_id").is_null())return 0;
    if(!ready||owner==0||owner!=current)
        throw std::runtime_error("Fixture requires an active healthy owner-thread fight call");
    // Health().healthy describes a fully idle/closed boundary and is false
    // during a valid original callback. Check the actual entered phase.
    for(const char* key:{"faults","reentrant_calls","wrong_thread_calls","aborted_calls"})
        if(health.at(key)!=0)throw std::runtime_error("Fixture engine tracker has a fault");
    const auto& id=health.at("active_call_id");
    if(!id.is_number_unsigned()&&(!id.is_number_integer()||id.get<int64_t>()<1))
        throw std::runtime_error("Invalid fixture engine call identity");
    auto value=id.get<uint64_t>();
    if(value==0||health.at("reserved_calls")!=value||health.at("entered_calls")!=value
        ||health.at("returned_calls")!=value-1||health.at("written_post_boundaries")!=value-1
        ||health.at("verified_clock_steps")!=value-1||health.at("verified_terminal_zero_calls")!=0
        ||health.at("terminal_clock_steps")!=0)
        throw std::runtime_error("Fixture is outside the actual original-update callback");
    return value;
}
using DropHeadFn=void(__stdcall*)(void*,uint32_t);
inline void InvokeDropHead(DropHeadFn target,void* zombie) { target(zombie,0); }
void Initialize(const std::filesystem::path& run,uint32_t thread);
void BeforeOriginalUpdate(const Json& health,const Json& version,bool ready);
void AfterOriginalUpdate(const Json& health,const Json& version,bool ready);
void Close();
}
