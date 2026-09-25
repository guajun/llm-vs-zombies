#pragma once
#include <atomic>
#include <cstdint>
#include <string>
#include <nlohmann/json.hpp>

// Minimal measurement common layer for issue #111 (stage B).
//
// The runtime host owns the session lifecycle: Open() once before probes are
// installed, Close() once after the final drain and persistence. A probe
// install only binds to an already-open session; it never resets the shared
// capture_sequence or the shared ledger. After Close(), every capture /
// delivery / persistence call is refused and counted, so a receipt remains the
// stable final boundary of the run.
namespace lvz::measurement {
using Json = nlohmann::json;

class MeasurementHost {
public:
    // Start a fresh measurement session on the owner game thread. Refuses while
    // a session is already open: a new session needs a new session identity.
    bool Open(uint32_t ownerThread, std::string* error = nullptr) noexcept;
    // Finalize the session. Refuses when it is not open, when any fault was
    // recorded, or when captured/delivered/persisted are not balanced.
    bool Close(std::string* error = nullptr) noexcept;
    // Shared capture_sequence, allocated at the actual capture point (exit)
    // right before the record enters its queue. Returns 0 when refused (wrong
    // thread or the session is not open); monotonic across drains/boundaries.
    uint64_t NextSequence() noexcept;
    void OnCaptured() noexcept;
    void OnDelivered(uint64_t count) noexcept;
    void OnPersisted(uint64_t count) noexcept;
    void OnOverflow() noexcept;
    void OnWrongThread() noexcept;
    void OnNestingMismatch() noexcept;
    void OnIncomplete() noexcept;
    Json Health() const noexcept;
    // True when a session is open and owned by this thread (probe bind check).
    bool BoundTo(uint32_t thread) const noexcept;
    bool SessionOpen() const noexcept;
    bool Closed() const noexcept;
    uint64_t SessionId() const noexcept;
    bool CloseReceiptPresent() const noexcept;

private:
    bool Refuse() noexcept;

    uint32_t owner_ = 0;
    std::atomic<uint8_t> state_{0};  // 0 idle, 1 open, 2 closed
    uint64_t session_id_ = 0;
    uint64_t next_sequence_ = 1;
    std::atomic<uint64_t> captured_{0}, delivered_{0}, persisted_{0}, overflow_{0};
    std::atomic<uint64_t> wrong_thread_{0}, nesting_mismatch_{0}, incomplete_{0}, refused_{0};
    std::atomic<bool> close_receipt_present_{false};
};

// The process-wide single host. Probe code calls this from inside the capture
// point; the recorder adapter reads Health()/Close() on the owner thread.
MeasurementHost& Host() noexcept;
}
