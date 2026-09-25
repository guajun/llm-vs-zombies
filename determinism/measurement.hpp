#pragma once
#include <atomic>
#include <cstdint>
#include <nlohmann/json.hpp>

// Minimal measurement common layer for issue #111 (stage B).
//
// This module owns the two things every lifecycle capture point must share,
// without depending on the game, the Agent, reward functions or experiment
// scripts:
//
// 1. a process-internal capture_sequence allocator -- one monotonic uint64
//    order domain shared by every capture point on the game thread; drain,
//    boundaries and epochs never reset it, only Open()/re-install does;
// 2. a single-host batch ledger -- captured/delivered/persisted plus the
//    fault counters and the close receipt required by
//    lvz.lifecycle-event.v1.
//
// A recorder adapter drains batches exactly once through the owning probe's
// drain entry point; nothing here exposes a second destructive drain that a
// future consumer could use to steal records.
namespace lvz::measurement {
using Json = nlohmann::json;

class MeasurementHost {
public:
    // Bind the owner game thread and reset the whole ledger. Called by the
    // runtime host when a measurement run is (re)started, before hooks run.
    void Open(uint32_t ownerThread) noexcept;
    // Final health receipt; marks close_receipt_present for the offline reader.
    Json Close() noexcept;
    // Allocate the next shared capture_sequence. Returns 0 and counts a
    // wrong-thread fault when called off the owner thread.
    uint64_t NextSequence() noexcept;
    void OnCaptured() noexcept;
    void OnDelivered(uint64_t count) noexcept;
    void OnPersisted(uint64_t count) noexcept;
    void OnOverflow() noexcept;
    void OnWrongThread() noexcept;
    void OnNestingMismatch() noexcept;
    void OnIncomplete() noexcept;
    Json Health() const noexcept;
    bool CloseReceiptPresent() const noexcept { return close_receipt_present_.load(); }

private:
    uint32_t owner_ = 0;
    uint64_t next_sequence_ = 1;
    std::atomic<uint64_t> captured_{0}, delivered_{0}, persisted_{0}, overflow_{0};
    std::atomic<uint64_t> wrong_thread_{0}, nesting_mismatch_{0}, incomplete_{0};
    std::atomic<bool> close_receipt_present_{false};
};

// The process-wide single host. Hook code calls this from inside the capture
// point; the recorder adapter reads Health()/Close() on the owner thread.
MeasurementHost& Host() noexcept;
}
