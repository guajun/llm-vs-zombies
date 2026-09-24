#pragma once
#include <avz.h>
#include "determinism/audit.hpp"
#include "shovel_action.hpp"

// Live half of the controlled shovel action (issue #99). It is compiled into
// recorder.dll only: the click is sent through AvZ's own scene click entry
// (``AAsm::ClickScene`` -> the locked engine's ``Board::MouseDown`` at
// 0x411f20), which is the same path the precheck measured. No OS mouse event,
// no window focus, no direct write to any random source, and no AShovel call:
// the lawn is never touched, only the shovel rectangle of the seed bank.
namespace lvz::runtime {

// The captured sound block is part of the deterministic state in the silent
// audio mode that every declared plan of this experiment uses. Outside that
// mode the same key is absent and the action records ``null`` instead of
// inventing a second reading path.
[[nodiscard]] inline Json ShovelSoundBlock(const Json& state) {
    return state.contains("sound_effects") ? state["sound_effects"] : Json(nullptr);
}

inline Json RunShovelHoldCancel(const Json& request) {
    auto fail = [](const char* error, Json extra = Json::object()) {
        Json out = {{"schema", ShovelHoldCancelSchema}, {"ok", false}, {"error", error}};
        for (auto it = extra.begin(); it != extra.end(); ++it) out[it.key()] = it.value();
        return out;
    };
    const ShovelRequest parsed = ParseShovelAction(request);
    if (!parsed.Ok()) return fail(parsed.error.c_str());
    auto* board = AGetMainObject();
    if (!board || AGetPvzBase()->GameUi() != 3) return fail("shovel_action_requires_fight");
    // Only the ten-seed-slot pool level puts the shovel rectangle where this
    // action measured it; another scene must not be clicked by guesswork.
    if (board->Scene() != 2) return fail("shovel_action_requires_pool_level");
    auto* seeds = board->SeedArray();
    if (!seeds || seeds->Count() != kShovelSlotSeedCount)
        return fail("shovel_action_requires_ten_seed_slots");
    auto* cursor = board->MouseAttribution();
    if (!cursor) return fail("shovel_cursor_unavailable");
    if (cursor->Type() != kCursorEmpty) return fail("shovel_cursor_not_empty");

    auto plants = [] {
        std::vector<PlantIdentity> out;
        for (auto& plant : aAlivePlantFilter)
            out.push_back({static_cast<std::uint32_t>(plant.Id()), plant.Type(),
                           plant.Row() + 1, plant.Col() + 1});
        return out;
    };
    auto plantsJson = [](const std::vector<PlantIdentity>& items) {
        Json out = Json::array();
        for (const auto& plant : items)
            out.push_back({{"id", plant.id}, {"type", plant.type},
                           {"row", plant.row}, {"col", plant.col}});
        return out;
    };
    auto sample = [&](const Json& state, const Json& sound) {
        return Json{{"cursor_type", cursor->Type()},
                    {"rng", lvz::determinism::CaptureRng()},
                    {"state_digest", DigestJson(state)},
                    {"sound_effects_digest", DigestJson(sound)}};
    };

    const auto beforePlants = plants();
    Json beforeState = lvz::determinism::CaptureState(), beforeSound = ShovelSoundBlock(beforeState);
    Json result = {{"schema", ShovelHoldCancelSchema}, {"op", ShovelHoldCancelOp},
                   {"path", "AAsm::ClickScene -> 0x411f20 Board::MouseDown"},
                   {"cancel", parsed.action.CancelName()},
                   {"click", {{"x", parsed.action.x}, {"y", parsed.action.y},
                              {"seed_slots", kShovelSlotSeedCount},
                              {"pick_key", kLeftKey}, {"cancel_key", parsed.action.CancelKey()}}},
                   {"before", sample(beforeState, beforeSound)}};
    result["before"]["plants"] = plantsJson(beforePlants);

    // One real left click on the shovel rectangle: the engine moves the shovel
    // into the cursor. Nothing else is sent.
    AAsm::ClickScene(board, parsed.action.x, parsed.action.y, kLeftKey);
    Json pickedState = lvz::determinism::CaptureState();
    result["picked"] = sample(pickedState, ShovelSoundBlock(pickedState));
    if (cursor->Type() != kCursorShovel) {
        result["ok"] = false;
        result["error"] = "shovel_pickup_not_observed";
        return result;
    }

    // Cancel: the declared second click puts the shovel back.
    AAsm::ClickScene(board, parsed.action.x, parsed.action.y, parsed.action.CancelKey());
    Json afterState = lvz::determinism::CaptureState(), afterSound = ShovelSoundBlock(afterState);
    result["after"] = sample(afterState, afterSound);
    const auto afterPlants = plants();
    result["after"]["plants"] = plantsJson(afterPlants);

    std::vector<Json> soundChanges;
    bool soundTruncated = false;
    CollectLeafChanges(beforeSound, afterSound, "/sound_effects", soundChanges, soundTruncated);
    Json cursors = Json::array(), crtStates = Json::array();
    std::array<ShovelRngView, 3> rng;
    int index = 0;
    for (const char* key : {"before", "picked", "after"}) {
        rng[static_cast<std::size_t>(index++)] = ViewShovelRng(result[key]["rng"]);
    }
    for (const auto& view : rng) {
        cursors.push_back(view.mtCursor);
        crtStates.push_back(view.crtState);
    }
    const bool rngComplete = rng[0].complete && rng[1].complete && rng[2].complete;
    const bool rngWordsEqual = rngComplete && rng[0].mtWords == rng[1].mtWords && rng[1].mtWords == rng[2].mtWords;
    const bool crtEqual = rngComplete && rng[0].crtState == rng[1].crtState && rng[1].crtState == rng[2].crtState;
    result["changes"] = {{"rng_instances", "instances/global_mt + instances/game_thread_crt"},
                         {"rng_complete", rngComplete},
                         {"rng_cursor", cursors},
                         {"rng_words_equal", rngWordsEqual},
                         {"game_thread_crt", crtStates},
                         {"game_thread_crt_equal", crtEqual},
                         {"state_digest_changed", beforeState.dump() != afterState.dump()},
                         {"sound_effects_changed", beforeSound != afterSound},
                         {"sound_effects_changes", soundChanges},
                         {"sound_effects_changes_truncated", soundTruncated},
                         {"plants_changed", !SamePlantSet(beforePlants, afterPlants)}};
    if (!SamePlantSet(beforePlants, afterPlants)) {
        result["ok"] = false;
        result["error"] = "shovel_plant_set_changed";
        return result;
    }
    if (cursor->Type() != kCursorEmpty) {
        result["ok"] = false;
        result["error"] = "shovel_cursor_not_restored";
        return result;
    }
    result["ok"] = true;
    return result;
}
} // namespace lvz::runtime
