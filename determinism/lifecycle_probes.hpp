#pragma once
#include <cstdint>
#include <string>
#include <nlohmann/json.hpp>

// Issue #111 production lifecycle probes: exact-store interception for the
// five verified zombie phase stores, the mDead removal store and the
// Board::ProcessDeleteQueue recycle guard/commit pair.
//
// Every patch window is fixed and independently verified by
// tools/issue111_patch_windows.py (whole-instruction coverage, no external
// branch target inside the window, locked byte signatures). Each shim replays
// the original instruction with the live registers, copies only bounded facts
// into a preallocated queue, and allocates the shared capture_sequence only
// after the original store has executed. No heap allocation, serialization,
// file I/O, RNG or callbacks happen inside a hook.
namespace lvz::determinism {
// Explicit mode switch; the audit host installs these probes only when the
// process environment asks for them (same build, no install when disabled).
bool InstallLifecycleProbes(std::string& error);
// Removes only this module's patches. Refuses (and keeps the DLL loaded) when
// an own patch is no longer intact or a recycle candidate is still pending.
bool RemoveLifecycleProbes(std::string& error);
bool LifecycleProbesInstalled() noexcept;
// Single destructive drain of the bounded probe queue; returns the v2 event
// projection and records delivered counts on the shared host.
nlohmann::json DrainLifecycleProbeBatch();
nlohmann::json LifecycleProbeStatus();

#ifdef LVZ_LIFECYCLE_PROBES_TESTING
// Test-only: install the production shims at caller-provided code copies and
// divert the continuations, so the fixture executes real patched machine code.
bool InstallLifecycleProbesForTest(const uintptr_t targets[8], const uintptr_t continuations[8],
                                   const uintptr_t jumpTargets[8], std::string& error);
#endif
}
