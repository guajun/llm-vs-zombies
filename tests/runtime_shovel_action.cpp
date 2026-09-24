#include "shovel_action.hpp"
#include <iostream>
#include <stdexcept>
#include <string>

// Offline contract of the controlled shovel action (issue #99). The helpers
// under test are the ones the live dispatcher calls (runtime/runtime.cpp), so
// these cases pin request shape, the cancel vocabulary, the plant-set
// comparison and the change report without a running game. What they cannot
// prove is the click itself: that needs the real engine and is measured by the
// two live branches, not here.
using lvz::runtime::Json;
using namespace lvz::runtime;

namespace {
void Check(bool okay, const std::string& message) { if (!okay) throw std::runtime_error(message); }

void Accept(const Json& action, ShovelCancel cancel, const std::string& label) {
    auto request = ParseShovelAction(action);
    Check(request.Ok(), label + ": rejected with " + request.error);
    Check(request.action.cancel == cancel, label + ": cancel method differs");
    Check(request.action.x == kShovelSlotClickX && request.action.y == kShovelSlotClickY,
        label + ": click point differs from the frozen rectangle");
    Check(request.action.CancelKey() == (cancel == ShovelCancel::Left ? 1 : -1),
        label + ": cancel key differs");
}

void Reject(const Json& action, const char* error, const std::string& label) {
    auto request = ParseShovelAction(action);
    Check(!request.Ok(), label + ": accepted");
    Check(request.error == error, label + ": wrong error code " + request.error);
}

std::vector<PlantIdentity> Lawn() {
    return {{65539u, 14, 1, 3}, {65540u, 14, 2, 3}, {65541u, 30, 5, 1}};
}
} // namespace

int main() {
    try {
        // Request shape: the op alone is the declared default (right cancel).
        Accept(Json{{"op", "shovel_hold_cancel"}}, ShovelCancel::Right, "default cancel");
        Accept(Json{{"op", "shovel_hold_cancel"}, {"cancel", "right"}}, ShovelCancel::Right, "explicit right");
        Accept(Json{{"op", "shovel_hold_cancel"}, {"cancel", "left"}}, ShovelCancel::Left, "explicit left");
        Accept(Json{{"cancel", "left"}, {"op", "shovel_hold_cancel"}}, ShovelCancel::Left, "key order");

        Reject(Json{{"op", "shovel"}}, "invalid_action", "another op");
        Reject(Json{{"op", "shovel_hold"}}, "invalid_action", "op typo");
        Reject(Json::array(), "invalid_action", "not an object");
        Reject(Json{{"op", "shovel_hold_cancel"}, {"cancel", "middle"}}, "invalid_shovel_cancel", "unknown cancel");
        Reject(Json{{"op", "shovel_hold_cancel"}, {"cancel", true}}, "invalid_shovel_cancel", "boolean cancel");
        Reject(Json{{"op", "shovel_hold_cancel"}, {"cancel", 1}}, "invalid_shovel_cancel", "numeric cancel");
        // No coordinates and no lawn target: a request cannot move the click
        // elsewhere or name a plant to dig up.
        Reject(Json{{"op", "shovel_hold_cancel"}, {"row", 3}, {"col", 4}}, "invalid_action", "lawn coordinates");
        Reject(Json{{"op", "shovel_hold_cancel"}, {"x", 0}, {"y", 0}}, "invalid_action", "explicit click point");
        Reject(Json{{"op", "shovel_hold_cancel"}, {"target_type", 14}}, "invalid_action", "plant target");

        // Plant sets: order is not a change, identity is.
        Check(SamePlantSet(Lawn(), Lawn()), "identical lawn rejected");
        auto reordered = Lawn();
        std::swap(reordered[0], reordered[2]);
        Check(SamePlantSet(Lawn(), reordered), "re-ordered lawn reported as a change");
        Check(SamePlantSet({}, {}), "two empty lawns rejected");
        auto removed = Lawn();
        removed.pop_back();
        Check(!SamePlantSet(Lawn(), removed), "removed plant not reported");
        auto added = Lawn();
        added.push_back({65542u, 14, 6, 3});
        Check(!SamePlantSet(Lawn(), added), "added plant not reported");
        auto retyped = Lawn();
        retyped[0].type = 30;
        Check(!SamePlantSet(Lawn(), retyped), "changed plant type not reported");
        auto moved = Lawn();
        moved[1].col = 4;
        Check(!SamePlantSet(Lawn(), moved), "moved plant not reported");
        auto replaced = Lawn();
        replaced[2].id = 65599u;
        Check(!SamePlantSet(Lawn(), replaced), "replaced plant id not reported");

        // Digests are content digests: key order is irrelevant, values are not.
        const Json one{{"b", 2}, {"a", 1}};
        const Json two{{"a", 1}, {"b", 2}};
        Check(DigestJson(one) == DigestJson(two), "key order changed the digest");
        Check(DigestJson(one) != DigestJson(Json{{"a", 1}, {"b", 3}}), "value change kept the digest");
        Check(DigestJson(one).size() == 16, "digest is not 16 hex characters");

        // RNG view: two named instances are read separately, and an unknown
        // shape is reported as incomplete instead of as a zero cursor.
        const Json snapshot{{"instances", {{"global_mt", {{"algorithm", "sexy_mt19937_31"},
                                                          {"cursor", 577}, {"words", Json::array({1, 2, 3})}}},
                                         {"game_thread_crt", {{"algorithm", "msvc_lcg_15"}, {"state", 42u}}}}}};
        const auto view = ViewShovelRng(snapshot);
        Check(view.complete && view.mtCursor == 577 && view.crtState == 42u
            && view.mtWords.size() == 3, "RNG snapshot not read");
        Check(!ViewShovelRng(Json{{"instances", {{"global_mt", {{"cursor", 1}}}}}}).complete,
            "half a snapshot counted as complete");
        Check(!ViewShovelRng(Json(nullptr)).complete && !ViewShovelRng(Json::array()).complete,
            "an empty snapshot counted as complete");
        Check(!ViewShovelRng(Json{{"instances", {{"global_mt", {{"cursor", 1}, {"words", Json::array()}}},
                                                 {"game_thread_crt", {{"state", "42"}}}}}}).complete,
            "a string CRT state counted as complete");

        // Change report: paths, both sides and a cap.
        std::vector<Json> changes;
        bool truncated = false;
        const Json before{{"sound_effects", {{"calls", 12}, {"histories",
            Json::array({Json{{"last_variation", 1}, {"slots", Json::array({0, 0})}}, Json{{"last_variation", 2}}})}}}};
        const Json after{{"sound_effects", {{"calls", 15}, {"histories",
            Json::array({Json{{"last_variation", 1}, {"slots", Json::array({0, 7})}}, Json{{"last_variation", 2}}})}}}};
        CollectLeafChanges(before["sound_effects"], after["sound_effects"], "/sound_effects", changes, truncated);
        Check(!truncated && changes.size() == 2, "unexpected change count");
        Check(changes[0]["path"] == "/sound_effects/calls" && changes[0]["before"] == 12
            && changes[0]["after"] == 15, "sound call change differs");
        Check(changes[1]["path"] == "/sound_effects/histories/0/slots/1" && changes[1]["after"] == 7,
            "nested slot change differs");

        std::vector<Json> none;
        truncated = false;
        CollectLeafChanges(before["sound_effects"], before["sound_effects"], "/sound_effects", none, truncated);
        Check(none.empty() && !truncated, "identical documents reported as a change");

        std::vector<Json> many;
        truncated = false;
        Json left = Json::object(), right = Json::object();
        for (int index = 0; index < kMaxReportedChanges + 5; ++index) {
            const std::string key = "k" + std::to_string(index);
            left[key] = 0;
            right[key] = 1;
        }
        CollectLeafChanges(left, right, "/x", many, truncated);
        Check(many.size() == static_cast<std::size_t>(kMaxReportedChanges) && truncated,
            "the change cap did not hold");

        std::vector<Json> addedLeaf;
        truncated = false;
        CollectLeafChanges(Json{{"a", 1}}, Json{{"a", 1}, {"b", 2}}, "/p", addedLeaf, truncated);
        Check(addedLeaf.size() == 1 && addedLeaf[0]["path"] == "/p/b" && addedLeaf[0]["before"].is_null()
            && addedLeaf[0]["after"] == 2, "added leaf not reported against null");

        std::cout << "runtime shovel action contract passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "runtime shovel action contract failed: " << error.what() << "\n";
        return 1;
    }
}
