#pragma once
#include <nlohmann/json.hpp>
#include <cmath>
#include <string>

namespace lvz::runtime {
using Json = nlohmann::json;

// The spawn op maps to one AvZ primitive and nothing else:
// AAsm::PutZombie(row, col, type) (avz/framework/inc/avz_asm.h:106-109,
// src/avz_asm.cpp:371-384), which forwards to the locked engine's
// Challenge::IZombiePlaceZombie at 0x42A0F0 (work/replay-research/
// Lawn_Challenge.cpp:4376). That engine function takes the row/column as
// 0-based grid indices (engine EAX = row; AddZombieInRow(theType, theGridY, 0)
// plus mPosX = GridToPixelX(theGridX, theGridY) - 30), while the protocol keeps
// 1-based AvZ coordinates. Keeping the translation and range checks here as
// pure code lets a native test target exercise exactly what the live
// dispatcher calls, without a running game.
//
// AvZ's own header documents the same 0-based convention:
// "PutZombie(1, 1, APJ_0) ----- 在2行2列放置一个普通僵尸".
inline constexpr int kMaxZombieType = 32; // AZombieType: AZOMBIE(0) .. AGIGA_GARGANTUAR(32)
inline constexpr int kMaxSpawnColumn = 9; // MAX_GRID_SIZE_X (Lawn_Board.h:18)
// AZOMBIE_BOBSLED_TEAM; Board::AddZombieInRow allocates the three riders itself.
inline constexpr int kBobsledZombieType = 13;
inline constexpr int kBobsledExtraSlots = 3;
// Board::AddZombieInRow refuses to create a zombie once mSize >= mMaxSize - 1.
inline constexpr int kSpawnPoolReservedSlots = 1;

struct SpawnAction {
    int type = 0; // AZombieType numeric value, passed to AvZ unchanged
    int row = 0;  // 1-based protocol row
    int col = 0;  // 1-based protocol column
    [[nodiscard]] int EngineRow() const { return row - 1; }
    [[nodiscard]] int EngineCol() const { return col - 1; }
};

struct SpawnRequest {
    SpawnAction action{};
    std::string error; // empty when the action can be dispatched
    [[nodiscard]] bool Ok() const { return error.empty(); }
};

// The engine creates three extra riders for a bobsled team, so the runtime has
// to reserve pool room for all of them before calling the primitive.
[[nodiscard]] inline int SpawnExtraPoolSlots(int type) {
    return type == kBobsledZombieType ? kBobsledExtraSlots : 0;
}

// Mirrors the engine's own admission rule at work/replay-research/
// Lawn_Board.cpp:2676 ("if (mZombies.mSize >= mZombies.mMaxSize - 1) return
// nullptr;"). The primitive dereferences that null result immediately, so a
// spawn the pool cannot hold would crash the game thread instead of failing.
[[nodiscard]] inline bool SpawnPoolAccepts(int live, int limit, int extra) {
    if (live < 0 || limit <= 0 || extra < 0) return false;
    return live + extra < limit - kSpawnPoolReservedSlots;
}

namespace detail {
// Grid indices must be whole numbers; an integral float (5.0) is accepted
// because a JSON encoder may spell an integer either way.
[[nodiscard]] inline bool IntegralCoordinate(const Json& value, int minimum, int maximum, int& out) {
    if (value.is_number_integer()) {
        const int number = value.get<int>();
        if (number < minimum || number > maximum) return false;
        out = number;
        return true;
    }
    if (!value.is_number_float()) return false;
    const double number = value.get<double>();
    if (!std::isfinite(number) || number < minimum || number > maximum) return false;
    if (number != static_cast<double>(static_cast<int>(number))) return false;
    out = static_cast<int>(number);
    return true;
}
} // namespace detail

// Validates one `commit` action object against the protocol ranges and returns
// the engine-ready form. `rows` is the scene row count the dispatcher already
// derived (5 or 6); the engine's own row rule is checked separately with
// AAsm::CanSpawnZombies because it depends on live Board state.
inline SpawnRequest ParseSpawnAction(const Json& action, int rows) {
    SpawnRequest request;
    if (!action.is_object() || !action.contains("type") || !action["type"].is_number_integer()) {
        request.error = "invalid_spawn_type";
        return request;
    }
    const int type = action["type"].get<int>();
    if (type < 0 || type > kMaxZombieType) {
        request.error = "invalid_spawn_type";
        return request;
    }
    if (!action.contains("row") || !action.contains("col")) {
        request.error = "invalid_spawn_position";
        return request;
    }
    int row = 0, col = 0;
    if (!detail::IntegralCoordinate(action["row"], 1, rows, row)
        || !detail::IntegralCoordinate(action["col"], 1, kMaxSpawnColumn, col)) {
        request.error = "invalid_spawn_position";
        return request;
    }
    request.action = SpawnAction{type, row, col};
    return request;
}
} // namespace lvz::runtime
