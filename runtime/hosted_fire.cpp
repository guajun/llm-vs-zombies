// The hosted-fire hook's runtime side; see runtime/hosted_fire.hpp.
//
// Filled in only for a build compiled with LVZ_AVZ_HOSTED_FIRE_AUDIT: the
// generated overlay of the pinned AvZ cob manager is the only caller, and it is
// only used by such a build.
#include "hosted_fire.hpp"

#ifdef LVZ_AVZ_HOSTED_FIRE_AUDIT

#include "determinism/hosted_fire.hpp"
#include "runtime.hpp"

namespace lvz::runtime {
void RecordHostedFire(int plantIndex, int plantId, int plantRow, int plantCol,
                      int targetRow, float targetCol) {
    // The boundary version the runtime is at right now. When the runtime is not
    // started (or the audit is closed) there is no frame to bind the shot to,
    // and determinism::RecordHostedFire drops the record instead of inventing
    // one - the shot itself is never delayed, retried or reported.
    try {
        lvz::determinism::RecordHostedFire(plantIndex, plantId, plantRow, plantCol,
            targetRow, targetCol, CurrentVersion());
    } catch (const std::exception& error) {
        // An audit-only hook must not change what the shot does: the fault is
        // reported to the runtime (which invalidates the run) and never thrown
        // into the coroutine that called aCobManager.Fire.
        ReportAuditFault(error.what());
    } catch (...) {
        ReportAuditFault("hosted fire audit failed");
    }
}
} // namespace lvz::runtime

#endif
