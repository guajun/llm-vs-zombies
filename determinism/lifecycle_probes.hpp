#pragma once
#include <cstdint>
#include <string>
#include <nlohmann/json.hpp>

// Issue #111 production lifecycle probes: exact-store interception for the
// five verified zombie phase stores, the mDead removal store and the
// Board::ProcessDeleteQueue recycle guard/commit pair.
//
// Semantics contract (review PR #116):
//  * The shim always replays the displaced original instruction exactly once,
//    unconditionally. Observation state (thread, classification, queue,
//    measurement active) can never suppress or alter the original operation.
//  * A fact is published only after the original store executed. The shim
//    copies the raw before value, executes the original bytes, then the
//    observer copies the after value and publishes.
//  * Handlers preserve LastError, flags, all GPRs, x87 state, MXCSR and SSE
//    state via the shim's pushal/pushfl/fxsave frame.
//  * Reads inside hooks are strictly non-allocating and fault-protected; an
//    invalid observer read never changes the original access semantics.
//  * Patch ownership is tracked per site; restoration failures propagate and
//    keep the module loaded and the site tracked.
namespace lvz::determinism {
// Explicit mode switch; the audit host installs these probes only when the
// process environment asks for them (same build, no install when disabled).
bool InstallLifecycleProbes(std::string& error);
// Removes only this module's patches. Refuses (and keeps the DLL loaded) when
// an own patch is no longer intact, a recycle candidate is pending, or a
// protection/restore fails for any site.
bool RemoveLifecycleProbes(std::string& error);
bool LifecycleProbesInstalled() noexcept;
// True while any patch, the reader handler or an in-flight shim still belongs
// to this module. Cleanup callers must consult this instead of a local flag:
// a failed install rollback can own resources while the install call returned
// false.
bool LifecycleProbeResourcesOwned() noexcept;
// Pins this module in the process (GetModuleHandleEx ..PIN) so a failed
// removal is enforced, not merely reported.
bool PinLifecycleProbeModule() noexcept;
// Single destructive drain of the bounded probe queue; returns the v2 event
// projection and records delivered counts on the shared host.
nlohmann::json DrainLifecycleProbeBatch();
nlohmann::json LifecycleProbeStatus();

#ifdef LVZ_LIFECYCLE_PROBES_TESTING
// Test-only: install the production shims at caller-provided code copies and
// divert the continuations, so the fixture executes real patched machine code.
bool InstallLifecycleProbesForTest(const uintptr_t targets[8], const uintptr_t continuations[8],
                                   const uintptr_t jumpTargets[8], std::string& error);
void LvzProbeSetThreadForTest(uint32_t thread) noexcept;
void LvzProbeSetActiveForTest(bool active) noexcept;
void LvzProbeSetVirtualProtectFailureForTest(bool fail) noexcept;
void LvzProbeSetQueueCapacityForTest(uint32_t capacity) noexcept;
bool LvzProbeReaderProtectionInstalledForTest() noexcept;
uint64_t LvzProbeReaderHandlersAddedForTest() noexcept;
uint64_t LvzProbeReaderHandlersRemovedForTest() noexcept;
void LvzProbeSetInFlightForTest(uint32_t value) noexcept;
void LvzProbeSetVirtualProtectFailureAfterForTest(uint32_t callIndex) noexcept;
bool LvzProbeHostModulePinnedForTest() noexcept;
void LvzProbeSetCallsiteBytesForTest(uint32_t address, const uint8_t* bytes, uint32_t count) noexcept;
void LvzProbeClearCallsiteBytesForTest() noexcept;
#endif
}
