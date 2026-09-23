#pragma once
// Overlay-facing entry point of the hosted-fire audit.
//
// The overlay runtime/avz_overlay.cmake generates for the pinned AvZ cob
// manager adds exactly one call to ACobManager::_BasicFire, immediately after
// the reviewed `AAsm::Fire(x, y, cobIdx);` line and before the closing
// `AAsm::ReleaseMouse();`. Everything it needs is already in scope there, so no
// engine call is added, removed or reordered and the shot's return value is
// untouched. See docs/avz-script-hosting.md section 8.
//
// This translation unit and determinism/hosted_fire.cpp exist only in a build
// compiled with LVZ_AVZ_HOSTED_FIRE_AUDIT (which CMake derives from
// LVZ_AVZ_HOSTED_SCRIPT); the default build links neither.
namespace lvz::runtime {
// Records one hosted cannon shot. `plantIndex` is the plant-array index
// (`AGetMainObject()->PlantArray() + plantIndex`), `plantId`/`plantRow`/
// `plantCol` come from the live plant slot (1-based, like the recorder's plant
// records) and `targetRow`/`targetCol` are the shot's AvZ grid row and float
// column as passed to ACobManager::Fire. Audit-only: it never throws into the
// script and never changes what the shot does.
void RecordHostedFire(int plantIndex, int plantId, int plantRow, int plantCol,
                      int targetRow, float targetCol);
} // namespace lvz::runtime
