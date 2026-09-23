#pragma once
#include <nlohmann/json.hpp>
#include <cstdint>
#include <functional>
namespace lvz::determinism {
inline constexpr char B0NormalizationMode[]="initial_b0_normalization_v1";
nlohmann::json B0NormalizationManifest();
// One declared, ordered table of {field, target, reason} entries applied once:
// every field is a JSON Pointer into the captured B(0) state, the closed field
// set is this manifest's, and the whole table consumes exactly one revision and
// produces exactly one receipt. The run's own observed values are only the real
// ``before``; they are never substituted for a declared target.
class InitialB0Normalization {
public:
    using Json=nlohmann::json;
    void Reset(){attempted_=applied_=false;}
    bool Applied()const{return applied_;}
    Json Apply(uintptr_t app,const Json& entries,uint32_t seed,bool audioEnabled,bool counterBound,
               bool seeded,bool warmed,const std::function<Json()>& capture,
               const std::function<Json()>& captureDemo);
private:
    bool attempted_=false,applied_=false;
};
}
