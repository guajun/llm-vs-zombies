#pragma once

#include <array>
#include <cstdint>
#include <cstring>
#include <stdexcept>
#include <string>
#include <string_view>
#include <nlohmann/json.hpp>

namespace lvz::determinism {
using Json = nlohmann::json;
inline constexpr const char* kSchema = "lvz.audit.v1";
inline constexpr const char* kTarget = "pvz-1.0.0.1051-en";
struct MtState {
    std::array<uint32_t, 624> words{};
    uint32_t cursor = 0;
    bool operator==(const MtState&) const = default;
};
static_assert(sizeof(MtState) == 2500);
inline MtState SeedMt(uint32_t seed) {
    MtState state;
    state.words[0] = seed ? seed : 4357u;
    for (uint32_t i=1; i<624; ++i)
        state.words[i] = 1812433253u * (state.words[i-1] ^ (state.words[i-1] >> 30)) + i;
    state.cursor=624;
    return state;
}

template<size_t Size> inline std::string FixedHex(uint64_t value) {
    static constexpr char digits[]="0123456789abcdef";
    std::string result(Size,'0');
    for(size_t i=Size;i>0;--i) {result[i-1]=digits[value&15];value>>=4;}
    return result;
}
// Diagnostic checksum, deliberately not a cryptographic file identity.
inline std::string Digest(std::string_view text) {
    uint64_t value = 14695981039346656037ULL;
    for (unsigned char byte : text) { value ^= byte; value *= 1099511628211ULL; }
    return FixedHex<16>(value);
}
inline std::string Hex(uint32_t value) {
    return FixedHex<8>(value);
}
inline Json EncodeMt(const MtState& state) {
    return {{"algorithm", "sexy_mt19937_31"}, {"words", state.words},
            {"cursor", state.cursor}};
}
inline MtState DecodeMt(const Json& value) {
    if (!value.is_object() || value.value("algorithm", "") != "sexy_mt19937_31"
        || !value.contains("words") || !value["words"].is_array()
        || value["words"].size() != 624 || !value.contains("cursor")
        || !value["cursor"].is_number_unsigned())
        throw std::invalid_argument("Invalid MT snapshot schema");
    const auto cursor = value["cursor"].get<uint64_t>();
    if (cursor > 625) throw std::invalid_argument("Invalid MT cursor");
    MtState result; result.cursor = static_cast<uint32_t>(cursor);
    for (size_t index = 0; index < result.words.size(); ++index) {
        const auto& entry = value["words"][index];
        if (!entry.is_number_unsigned() || entry.get<uint64_t>() > UINT32_MAX)
            throw std::invalid_argument("Invalid MT state word");
        result.words[index] = entry.get<uint32_t>();
    }
    return result;
}
inline Json Digests(const Json& state) {
    Json result = Json::object();
    std::string whole="{";
    for (auto it = state.begin(); it != state.end(); ++it) {
        auto encoded=it.value().dump();
        result[it.key()] = Digest(encoded);
        if(it!=state.begin()) whole+=',';
        whole+=Json(it.key()).dump();whole+=':';whole+=encoded;
    }
    whole+='}';
    // Native snapshots are objects. Preserve the old behavior for other
    // supported values rather than applying object encoding accidentally.
    result["all"] = Digest(state.is_object()?whole:state.dump());
    return result;
}
}
