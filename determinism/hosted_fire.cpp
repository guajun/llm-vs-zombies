// Hosted-fire audit record; see determinism/hosted_fire.hpp.
//
// This translation unit reads nothing but its arguments: the overlay of the
// pinned AvZ cob manager passes the values ACobManager::_BasicFire already
// holds, so no engine state is re-read here and nothing about the shot depends
// on this module.
#include "hosted_fire.hpp"

#ifdef LVZ_AVZ_HOSTED_FIRE_AUDIT

#include "model.hpp"  // kSchema
#include <cstdio>
#include <cstring>
#include <string>
#include <utility>
#include <vector>

namespace lvz::determinism {
namespace {
// FNV-1a, the audit's diagnostic checksum, over 64-bit words in a fixed order.
// determinism/particle_shake.cpp uses the same encoding, and
// src/llm_vs_zombies/audit_compare.py recomputes it from the written payload.
constexpr uint64_t kDigestSeed = 14695981039346656037ULL;
// Digest field order, shared with src/llm_vs_zombies/audit_compare.py
// (_HOSTED_FIRE_FIELDS) and pinned by tests/avz_hosted_fire_tests.cpp.
constexpr const char* kDigestFields[] = {"plant_index", "plant_id", "plant_row", "plant_col",
    "target_row", "target_col_bits", "tick"};
uint64_t digest = kDigestSeed;
uint64_t count = 0;
struct QueuedRecord {
    nlohmann::json payload;
    nlohmann::json version;
};
std::vector<QueuedRecord> queued;

void Mix(uint64_t value) {
    for (unsigned byte = 0; byte < 8; ++byte) {
        digest ^= uint8_t(value);
        digest *= 1099511628211ULL;
        value >>= 8;
    }
}

// The digest word of one payload field: the value the record stores, taken as
// its two's-complement 64-bit pattern. `plant_id` is stored as uint32 and
// therefore zero-extends; this is the exact rule the Python reader implements,
// so a record stays the single source of truth for its own digest.
uint64_t Word(const nlohmann::json& value) {
    return value.is_number_unsigned() ? value.get<uint64_t>()
                                      : static_cast<uint64_t>(value.get<int64_t>());
}

// A readable companion to the authoritative IEEE-754 bits of the float column.
// The strict reader re-derives it from `target_col_bits`, so it cannot drift.
std::string ColumnText(uint32_t bits) {
    float value = 0;
    std::memcpy(&value, &bits, sizeof(value));
    char text[32];
    std::snprintf(text, sizeof(text), "%.3f", value);
    return text;
}
} // namespace

bool HostedFireEnabled() { return true; }

void RecordHostedFire(int plantIndex, int plantId, int plantRow, int plantCol,
                      int targetRow, float targetCol, const nlohmann::json& version) {
    // Without a boundary version there is no frame this shot could be bound to.
    // The tick is accepted as any non-negative JSON integer, so the record does
    // not depend on whether the runtime serialised it as signed or unsigned.
    if (!version.is_object() || !version.contains("tick") || !version["tick"].is_number_integer())
        return;
    if (!version["tick"].is_number_unsigned() && version["tick"].get<int64_t>() < 0)
        return;
    const uint64_t tick = version["tick"].is_number_unsigned()
        ? version["tick"].get<uint64_t>()
        : static_cast<uint64_t>(version["tick"].get<int64_t>());
    uint32_t bits = 0;
    std::memcpy(&bits, &targetCol, sizeof(bits));
    nlohmann::json payload = {{"source", "hosted"},
        {"op", "fire"},
        {"plant_index", plantIndex},
        {"plant_id", static_cast<uint32_t>(plantId)},
        {"plant_row", plantRow},
        {"plant_col", plantCol},
        {"target_row", targetRow},
        {"target_col_bits", bits},
        {"target_col_text", ColumnText(bits)},
        {"tick", tick}};
    // Mixed from the payload, never from the parameters: a plant id arrives as
    // a signed `int` (which sign-extends) while the record stores it as a
    // uint32 (which zero-extends). Reading the written value keeps the two in
    // step for every id, including the ids >= 2^31 the live cannons carry.
    for (const char* field : kDigestFields)
        Mix(Word(payload.at(field)));
    ++count;
    queued.push_back({std::move(payload), version});
}

size_t DrainHostedFireRecords(const std::function<void(const nlohmann::json&)>& sink,
                              uint64_t firstSequence) {
    for (auto& record : queued) {
        sink({{"schema", kSchema},
            {"seq", firstSequence++},
            {"kind", kHostedFireKind},
            {"phase", "controlled_boundary"},
            {"native_phase", "avz_basic_fire"},
            {"payload", record.payload},
            {"version", record.version}});
    }
    const size_t drained = queued.size();
    queued.clear();
    return drained;
}

nlohmann::json HostedFireState() {
    return {{"mode", kHostedFireMode}, {"count", count}, {"digest", digest}};
}

nlohmann::json HostedFireManifest() {
    return {{"mode", kHostedFireMode},
        {"installed", true},
        {"kind", kHostedFireKind},
        {"source", "hosted_avz_script"},
        {"hook", "avz_cob_manager._BasicFire after the reviewed AAsm::Fire call"},
        {"original_engine_bitwise_unmodified", false},
        {"semantic_change", "none: audit-only; no engine call is added, removed or reordered"},
        {"boundary", "the shot is bound to the next audited pre_step (its version)"}};
}

void ResetHostedFire() {
    queued.clear();
    count = 0;
    digest = kDigestSeed;
}
} // namespace lvz::determinism

#else

// Default build: no hosted script is linked into the DLL, the overlay that
// would call this is not compiled, and this branch declares nothing. The
// default half of tests/avz_hosted_fire_tests.cpp compiles exactly this code
// and asserts that a shot leaves no record, no state component and no manifest
// mode behind.
namespace lvz::determinism {
bool HostedFireEnabled() { return false; }
void RecordHostedFire(int, int, int, int, int, float, const nlohmann::json&) {}
size_t DrainHostedFireRecords(const std::function<void(const nlohmann::json&)>&, uint64_t) { return 0; }
nlohmann::json HostedFireState() { return nlohmann::json::object(); }
nlohmann::json HostedFireManifest() { return nlohmann::json::object(); }
void ResetHostedFire() {}
} // namespace lvz::determinism

#endif
