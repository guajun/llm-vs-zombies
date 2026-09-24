#pragma once
#include <nlohmann/json.hpp>
#include <algorithm>
#include <array>
#include <cstdint>
#include <string>
#include <vector>

// The controlled "pick the shovel up and put it back" action (issue #99).
//
// It is deliberately *not* lvz::Shovel()/AShovel: those dig a plant out of the
// lawn. This action reproduces what a player does when the shovel is taken and
// then put away again - one left click on the shovel rectangle of the seed bank
// and one cancel - so nothing is ever removed from the board.
//
// Everything in this header is pure: the protocol ranges, the plant-set
// comparison and the JSON helpers can be exercised by an offline test without a
// running game. The live click path lives in ``shovel_hold.hpp``.
namespace lvz::runtime {
using Json = nlohmann::json;

inline constexpr char ShovelHoldCancelOp[] = "shovel_hold_cancel";
inline constexpr char ShovelHoldCancelSchema[] = "lvz.shovel-hold-cancel.v1";
// Ten seed slots (the pool endless level selects ten cards) put the shovel
// rectangle of the seed bank behind the last slot; measured in the precheck:
// ``GetSeedBankExtraWidth()`` is 153 there and the shovel rectangle starts at
// x=609. 640 is inside it and outside every seed slot.
inline constexpr int kShovelSlotClickX = 640;
inline constexpr int kShovelSlotClickY = 30;
inline constexpr int kShovelSlotSeedCount = 10;
// ``Board::MouseAttribution()->Type()``: 0 = empty hand, 6 = shovel.
inline constexpr int kCursorEmpty = 0;
inline constexpr int kCursorShovel = 6;
// ``AAsm::ClickScene`` key: 1 = left, -1 = right (avz_asm.h:13-15).
inline constexpr int kLeftKey = 1;
inline constexpr int kRightKey = -1;
// The action may not silently inspect more sound histories than it reports.
inline constexpr int kMaxReportedChanges = 64;

enum class ShovelCancel { Right, Left };

struct ShovelAction {
    int x = kShovelSlotClickX;
    int y = kShovelSlotClickY;
    ShovelCancel cancel = ShovelCancel::Right;
    [[nodiscard]] int CancelKey() const {
        return cancel == ShovelCancel::Left ? kLeftKey : kRightKey;
    }
    [[nodiscard]] const char* CancelName() const {
        return cancel == ShovelCancel::Left ? "left" : "right";
    }
};

struct ShovelRequest {
    ShovelAction action{};
    std::string error; // empty when the action can be dispatched
    [[nodiscard]] bool Ok() const { return error.empty(); }
};

// The runtime's RNG snapshot is ``{"instances":{"global_mt":{...},
// "game_thread_crt":{...}}}`` (determinism/audit.cpp CaptureRng). The two
// instances are read separately because the precheck found only the global MT
// moving and the game-thread CRT staying put: reporting one number for "the
// RNG" would blur exactly the distinction the issue asks to keep.
struct ShovelRngView {
    bool complete = false;
    int mtCursor = -1;
    Json mtWords = Json(nullptr);
    std::uint32_t crtState = 0;
};

[[nodiscard]] inline ShovelRngView ViewShovelRng(const Json& rng) {
    ShovelRngView view;
    if (!rng.is_object() || !rng.contains("instances") || !rng["instances"].is_object()) return view;
    const Json& instances = rng["instances"];
    if (!instances.contains("global_mt") || !instances.contains("game_thread_crt")) return view;
    const Json& mt = instances["global_mt"];
    const Json& crt = instances["game_thread_crt"];
    if (!mt.is_object() || !mt.contains("cursor") || !mt.contains("words") || !mt["words"].is_array()) return view;
    if (!crt.is_object() || !crt.contains("state") || !crt["state"].is_number_unsigned()) return view;
    view.complete = true;
    view.mtCursor = mt["cursor"].get<int>();
    view.mtWords = mt["words"];
    view.crtState = crt["state"].get<std::uint32_t>();
    return view;
}

// One plant identity: object id plus the 1-based position the protocol uses.
// Type is the AvZ plant enum. A replacement with the same id but another
// type/position is a change, and so is a removed or added plant.
struct PlantIdentity {
    std::uint32_t id = 0;
    int type = 0;
    int row = 0;
    int col = 0;
    [[nodiscard]] bool operator<(const PlantIdentity& other) const {
        if (id != other.id) return id < other.id;
        if (type != other.type) return type < other.type;
        if (row != other.row) return row < other.row;
        return col < other.col;
    }
    [[nodiscard]] bool operator==(const PlantIdentity& other) const {
        return id == other.id && type == other.type && row == other.row && col == other.col;
    }
};

// Sorted comparison, so a re-ordered plant array is not reported as a change:
// only an added, removed or altered plant is.
[[nodiscard]] inline bool SamePlantSet(std::vector<PlantIdentity> left,
                                       std::vector<PlantIdentity> right) {
    std::sort(left.begin(), left.end());
    std::sort(right.begin(), right.end());
    return left == right;
}

// FNV-1a over the canonical dump of one JSON value. nlohmann orders object keys,
// so the digest is stable for the same captured state.
[[nodiscard]] inline std::string DigestJson(const Json& value) {
    const std::string text = value.dump();
    std::uint64_t hash = 1469598103934665603ull;
    for (unsigned char byte : text) {
        hash ^= byte;
        hash *= 1099511628211ull;
    }
    static constexpr char Hex[] = "0123456789abcdef";
    std::string out(16, '0');
    for (int index = 15; index >= 0; --index) {
        out[static_cast<std::size_t>(index)] = Hex[hash & 0xf];
        hash >>= 4;
    }
    return out;
}

// Leaf-level differences between two captured documents, in a stable order,
// capped at ``kMaxReportedChanges`` with a ``truncated`` flag. Only what the two
// documents disagree about is kept: a value that moved or a leaf that appeared
// or disappeared.
inline void CollectLeafChanges(const Json& before, const Json& after, const std::string& path,
                               std::vector<Json>& out, bool& truncated) {
    if (out.size() >= static_cast<std::size_t>(kMaxReportedChanges)) {
        truncated = true;
        return;
    }
    if (before.type() != after.type() || before.is_primitive() || before.is_null()) {
        if (before != after) out.push_back({{"path", path}, {"before", before}, {"after", after}});
        return;
    }
    if (before.is_array()) {
        const std::size_t size = std::max(before.size(), after.size());
        for (std::size_t index = 0; index < size; ++index) {
            const Json missing = Json(nullptr);
            const Json& leftValue = index < before.size() ? before[index] : missing;
            const Json& rightValue = index < after.size() ? after[index] : missing;
            CollectLeafChanges(leftValue, rightValue, path + "/" + std::to_string(index), out, truncated);
        }
        return;
    }
    std::vector<std::string> keys;
    for (auto it = before.begin(); it != before.end(); ++it) keys.push_back(it.key());
    for (auto it = after.begin(); it != after.end(); ++it) {
        if (!before.contains(it.key())) keys.push_back(it.key());
    }
    std::sort(keys.begin(), keys.end());
    keys.erase(std::unique(keys.begin(), keys.end()), keys.end());
    for (const std::string& key : keys) {
        const Json missing = Json(nullptr);
        const Json& leftValue = before.contains(key) ? before[key] : missing;
        const Json& rightValue = after.contains(key) ? after[key] : missing;
        CollectLeafChanges(leftValue, rightValue, path + "/" + key, out, truncated);
    }
}

// Validates one ``shovel_hold_cancel`` request. The only accepted keys are the
// op and the cancel method: a request that spells anything else is rejected
// instead of being silently reinterpreted.
inline ShovelRequest ParseShovelAction(const Json& action) {
    ShovelRequest request;
    if (!action.is_object()) {
        request.error = "invalid_action";
        return request;
    }
    for (auto it = action.begin(); it != action.end(); ++it) {
        if (it.key() != "op" && it.key() != "cancel") {
            request.error = "invalid_action";
            return request;
        }
    }
    if (action.value("op", std::string()) != ShovelHoldCancelOp) {
        request.error = "invalid_action";
        return request;
    }
    if (action.contains("cancel")) {
        if (!action["cancel"].is_string()) {
            request.error = "invalid_shovel_cancel";
            return request;
        }
        const std::string cancel = action["cancel"].get<std::string>();
        if (cancel == "right") request.action.cancel = ShovelCancel::Right;
        else if (cancel == "left") request.action.cancel = ShovelCancel::Left;
        else {
            request.error = "invalid_shovel_cancel";
            return request;
        }
    }
    return request;
}
} // namespace lvz::runtime
