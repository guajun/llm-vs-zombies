#pragma once
#include <cstdint>
#include <map>
#include <set>
#include <string>
#include <nlohmann/json.hpp>

namespace lvz::determinism {
bool ValidateReanimationTarget() noexcept;
nlohmann::json ReanimationCoverage();
struct ReanimationSample {
    uint32_t id=0;
    bool dead=false;
    bool definitionValid=false;
    nlohmann::json semantic=nlohmann::json::object();
};
struct ReanimationPoolSnapshot {
    uint32_t capacity=0,used=0,count=0,freeHead=0,nextKey=0;
    // Includes each allocated slot's actual generation ID. Only referenced
    // entries need semantic fields; all IDs participate in lifetime tracking.
    std::map<uint32_t,ReanimationSample> slots;
};
struct ReanimationAudit {
    nlohmann::json comparable;
    nlohmann::json raw;
    nlohmann::json coverage;
    bool valid=true;
};
class ReanimationAuditor {
public:
    // Read-only capture on the same stopped game thread as ownerState. Pass
    // std::move(state) to reuse its owned JSON tree instead of copying a frame.
    ReanimationAudit Capture(nlohmann::json ownerState);
    // Lvalues remain unchanged; owned/rvalue trees move into comparable.
    ReanimationAudit Normalize(nlohmann::json ownerState,const ReanimationPoolSnapshot& pool);
    // Required at each independent run/B(0), not every frame.
    void Reset();
private:
    struct Identity {std::string logical;bool present=false;};
    std::map<uint32_t,Identity> identities_;
    std::map<std::string,uint64_t> nextLifetime_;
};
}
