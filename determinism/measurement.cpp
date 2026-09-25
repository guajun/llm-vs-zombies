#include "measurement.hpp"
#include <Windows.h>

namespace lvz::measurement {
namespace {
MeasurementHost host;
constexpr uint8_t kOpen = 1, kClosing = 2, kClosed = 3, kFailed = 4;
const char* StateName(uint8_t state) {
    switch (state) {
        case kOpen: return "open";
        case kClosing: return "closing";
        case kClosed: return "closed";
        case kFailed: return "failed";
        default: return "idle";
    }
}
}

MeasurementHost& Host() noexcept { return host; }

bool MeasurementHost::Refuse() noexcept {
    if (state_.load() != kOpen) {
        refused_.fetch_add(1);
        return true;
    }
    return false;
}

bool MeasurementHost::Open(uint32_t ownerThread, std::string* error) noexcept {
    const uint8_t state = state_.load();
    if (state == kOpen || state == kClosing) {
        if (error) *error = "measurement session is already active";
        return false;
    }
    owner_ = ownerThread;
    ++session_id_;
    next_sequence_ = 1;
    next_invocation_id_ = 1;
    captured_.store(0);
    delivered_.store(0);
    persisted_.store(0);
    overflow_.store(0);
    wrong_thread_.store(0);
    nesting_mismatch_.store(0);
    incomplete_.store(0);
    refused_.store(0);
    state_.store(kOpen);
    return true;
}

bool MeasurementHost::Close(std::string* error) noexcept {
    if (state_.load() != kOpen) {
        if (error) *error = "measurement session is not open";
        return false;
    }
    if (overflow_.load() || wrong_thread_.load() || nesting_mismatch_.load() || incomplete_.load()) {
        if (error) *error = "measurement session has faults";
        return false;
    }
    const uint64_t captured = captured_.load();
    const uint64_t delivered = delivered_.load();
    const uint64_t persisted = persisted_.load();
    if (captured != delivered || delivered != persisted) {
        if (error) *error = "measurement batch is incomplete";
        return false;
    }
    state_.store(kClosing);
    return true;
}

bool MeasurementHost::Commit(std::string* error) noexcept {
    if (state_.load() != kClosing) {
        if (error) *error = "measurement session is not closing";
        return false;
    }
    state_.store(kClosed);
    return true;
}

bool MeasurementHost::Abort(std::string* error) noexcept {
    const uint8_t state = state_.load();
    if (state != kOpen && state != kClosing) {
        if (error) *error = "measurement session is not active";
        return false;
    }
    // No successful close receipt: faults and undelivered counts stay visible.
    state_.store(kFailed);
    return true;
}

uint64_t MeasurementHost::NextSequence() noexcept {
    if (Refuse()) return 0;
    if (GetCurrentThreadId() != owner_) {
        wrong_thread_.fetch_add(1);
        return 0;
    }
    return next_sequence_++;
}

uint64_t MeasurementHost::NextInvocationId() noexcept {
    if (Refuse()) return 0;
    if (GetCurrentThreadId() != owner_) {
        wrong_thread_.fetch_add(1);
        return 0;
    }
    return next_invocation_id_++;
}

void MeasurementHost::OnCaptured() noexcept { if (!Refuse()) captured_.fetch_add(1); }
void MeasurementHost::OnDelivered(uint64_t count) noexcept { if (!Refuse()) delivered_.fetch_add(count); }
void MeasurementHost::OnPersisted(uint64_t count) noexcept { if (!Refuse()) persisted_.fetch_add(count); }
void MeasurementHost::OnOverflow() noexcept { if (!Refuse()) overflow_.fetch_add(1); }
void MeasurementHost::OnWrongThread() noexcept { if (!Refuse()) wrong_thread_.fetch_add(1); }
void MeasurementHost::OnNestingMismatch() noexcept { if (!Refuse()) nesting_mismatch_.fetch_add(1); }
void MeasurementHost::OnIncomplete() noexcept { if (!Refuse()) incomplete_.fetch_add(1); }

Json MeasurementHost::Health() const noexcept {
    const uint8_t state = state_.load();
    return {
        {"session", session_id_},
        {"state", StateName(state)},
        {"captured", captured_.load()},
        {"delivered", delivered_.load()},
        {"persisted", persisted_.load()},
        {"overflow", overflow_.load()},
        {"wrong_thread", wrong_thread_.load()},
        {"nesting_mismatch", nesting_mismatch_.load()},
        {"incomplete_events", incomplete_.load()},
        {"refused", refused_.load()},
        {"close_receipt_present", state == kClosed},
    };
}

bool MeasurementHost::BoundTo(uint32_t thread) const noexcept {
    return state_.load() == kOpen && owner_ == thread;
}

std::string RunMeasurementShutdown(const std::function<void()>& body,
                                   const std::function<void()>& cleanup) {
    std::string firstError;
    try {
        body();
    } catch (const std::exception& exception) {
        firstError = exception.what();
    }
    // Two-phase finalize: validate (Open -> Closing) or terminate as failed.
    // A success receipt is only committed after cleanup has confirmed the
    // persistence/flush/close conditions below.
    bool closing = false;
    if (firstError.empty()) {
        std::string closeError;
        if (!Host().Close(&closeError)) {
            Host().Abort();
            firstError = "Measurement session close failed: " + closeError;
        } else {
            closing = true;
        }
    } else {
        Host().Abort();
    }
    try {
        cleanup();
    } catch (const std::exception& exception) {
        if (firstError.empty()) firstError = exception.what();
    }
    if (firstError.empty()) {
        Host().Commit();
    } else if (closing) {
        // Validated but cleanup/persistence failed: never leave a success receipt.
        Host().Abort();
    }
    return firstError;
}

bool MeasurementHost::SessionOpen() const noexcept { return state_.load() == kOpen; }
bool MeasurementHost::Closing() const noexcept { return state_.load() == kClosing; }
bool MeasurementHost::Closed() const noexcept { return state_.load() == kClosed; }
bool MeasurementHost::Failed() const noexcept { return state_.load() == kFailed; }
uint64_t MeasurementHost::SessionId() const noexcept { return session_id_; }
bool MeasurementHost::CloseReceiptPresent() const noexcept { return state_.load() == kClosed; }
}
