#include "spawn_action.hpp"
#include <iostream>
#include <stdexcept>
#include <string>

// Parameter-validation path of the `spawn` runtime action. The helper under
// test is the same code the live dispatcher calls (runtime/runtime.cpp), so
// these cases pin the protocol contract without a running game: shape, ranges,
// 1-based protocol -> 0-based AvZ conversion and the engine's pool rule.
using lvz::runtime::Json;
using namespace lvz::runtime;

namespace {
void Check(bool okay, const std::string& message) { if (!okay) throw std::runtime_error(message); }

void Accept(const Json& action, int rows, int type, int row, int col, const std::string& label) {
    auto request = ParseSpawnAction(action, rows);
    Check(request.Ok(), label + ": rejected with " + request.error);
    Check(request.action.type == type && request.action.row == row && request.action.col == col,
        label + ": parsed action differs");
    Check(request.action.EngineRow() == row - 1 && request.action.EngineCol() == col - 1,
        label + ": engine grid indices differ");
}

void Reject(const Json& action, int rows, const char* error, const std::string& label) {
    auto request = ParseSpawnAction(action, rows);
    Check(!request.Ok(), label + ": accepted");
    Check(request.error == error, label + ": wrong error code " + request.error);
}
} // namespace

int main() {
    try {
        // Ordinary case: protocol row 3/column 9 is the live-validated fixture
        // placement AAsm::PutZombie(2, 8, type) from tests/flag_drop_live.cpp.
        Accept(Json{{"op","spawn"},{"type",1},{"row",3},{"col",9}}, 5, 1, 3, 9, "valid spawn");
        Accept(Json{{"op","spawn"},{"type",0},{"row",1},{"col",1}}, 5, 0, 1, 1, "first cell");
        Accept(Json{{"op","spawn"},{"type",32},{"row",5},{"col",9}}, 5, 32, 5, 9, "highest type");
        Accept(Json{{"op","spawn"},{"type",13},{"row",6},{"col",1}}, 6, 13, 6, 1, "six-row scene bobsled");
        Accept(Json{{"op","spawn"},{"type",23},{"row",2},{"col",5.0}}, 5, 23, 2, 5, "integral float column");
        Accept(Json{{"op","spawn"},{"type",23},{"col",5},{"row",2}}, 5, 23, 2, 5, "key order");

        Reject(Json{{"op","spawn"},{"row",2},{"col",5}}, 5, "invalid_spawn_type", "missing type");
        Reject(Json{{"op","spawn"},{"type","1"},{"row",2},{"col",5}}, 5, "invalid_spawn_type", "string type");
        Reject(Json{{"op","spawn"},{"type",true},{"row",2},{"col",5}}, 5, "invalid_spawn_type", "boolean type");
        Reject(Json{{"op","spawn"},{"type",1.5},{"row",2},{"col",5}}, 5, "invalid_spawn_type", "fractional type");
        Reject(Json{{"op","spawn"},{"type",-1},{"row",2},{"col",5}}, 5, "invalid_spawn_type", "negative type");
        Reject(Json{{"op","spawn"},{"type",33},{"row",2},{"col",5}}, 5, "invalid_spawn_type", "above AZombieType");
        Reject(Json{{"op","spawn"},{"type",1},{"row",0},{"col",5}}, 5, "invalid_spawn_position", "row below range");
        Reject(Json{{"op","spawn"},{"type",1},{"row",6},{"col",5}}, 5, "invalid_spawn_position", "row above five-row scene");
        Reject(Json{{"op","spawn"},{"type",1},{"row",2},{"col",0}}, 5, "invalid_spawn_position", "column below range");
        Reject(Json{{"op","spawn"},{"type",1},{"row",2},{"col",10}}, 5, "invalid_spawn_position", "column above grid");
        Reject(Json{{"op","spawn"},{"type",1},{"row",2},{"col",5.5}}, 5, "invalid_spawn_position", "fractional column");
        Reject(Json{{"op","spawn"},{"type",1},{"row",2}}, 5, "invalid_spawn_position", "missing column");
        Reject(Json{{"op","spawn"},{"type",1},{"col",5}}, 5, "invalid_spawn_position", "missing row");

        // Pool admission: the engine refuses at mSize >= mMaxSize - 1, so the
        // last usable slot is limit-2 and a bobsled needs three more.
        Check(SpawnExtraPoolSlots(13) == 3, "bobsled riders not reserved");
        Check(SpawnExtraPoolSlots(1) == 0 && SpawnExtraPoolSlots(32) == 0, "extra slots for ordinary types");
        Check(SpawnPoolAccepts(0, 2, 0), "empty two-slot pool rejected");
        Check(!SpawnPoolAccepts(1, 2, 0), "engine's reserved last slot not honoured");
        Check(SpawnPoolAccepts(1022, 1024, 0), "last usable pool slot rejected");
        Check(!SpawnPoolAccepts(1023, 1024, 0), "engine's reserved last slot ignored near capacity");
        Check(SpawnPoolAccepts(1019, 1024, 3), "bobsled team rejected while the pool can hold it");
        Check(!SpawnPoolAccepts(1020, 1024, 3), "bobsled riders not reserved near capacity");
        Check(!SpawnPoolAccepts(0, 0, 0) && !SpawnPoolAccepts(0, -1, 0), "impossible pool accepted");
        Check(!SpawnPoolAccepts(-1, 1024, 0) && !SpawnPoolAccepts(0, 1024, -1), "negative counters accepted");

        std::cout << "spawn action validation, coordinate conversion and pool admission passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
