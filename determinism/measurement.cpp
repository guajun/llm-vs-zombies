#include "measurement.hpp"
#include <Windows.h>

namespace lvz::measurement {
namespace {
MeasurementHost host;
}

MeasurementHost& Host() noexcept { return host; }

void MeasurementHost::Open(uint32_t ownerThread) noexcept {
    owner_ = ownerThread;
    next_sequence_ = 1;
    captured_.store(0);
    delivered_.store(0);
    persisted_.store(0);
    overflow_.store(0);
    wrong_thread_.store(0);
    nesting_mismatch_.store(0);
    incomplete_.store(0);
    close_receipt_present_.store(false);
}

Json MeasurementHost::Close() noexcept {
    close_receipt_present_.store(true);
    return Health();
}

uint64_t MeasurementHost::NextSequence() noexcept {
    if (GetCurrentThreadId() != owner_) {
        wrong_thread_.fetch_add(1);
        return 0;
    }
    return next_sequence_++;
}

void MeasurementHost::OnCaptured() noexcept { captured_.fetch_add(1); }
void MeasurementHost::OnDelivered(uint64_t count) noexcept { delivered_.fetch_add(count); }
void MeasurementHost::OnPersisted(uint64_t count) noexcept { persisted_.fetch_add(count); }
void MeasurementHost::OnOverflow() noexcept { overflow_.fetch_add(1); }
void MeasurementHost::OnWrongThread() noexcept { wrong_thread_.fetch_add(1); }
void MeasurementHost::OnNestingMismatch() noexcept { nesting_mismatch_.fetch_add(1); }
void MeasurementHost::OnIncomplete() noexcept { incomplete_.fetch_add(1); }

Json MeasurementHost::Health() const noexcept {
    return {
        {"captured", captured_.load()},
        {"delivered", delivered_.load()},
        {"persisted", persisted_.load()},
        {"overflow", overflow_.load()},
        {"wrong_thread", wrong_thread_.load()},
        {"nesting_mismatch", nesting_mismatch_.load()},
        {"incomplete_events", incomplete_.load()},
        {"close_receipt_present", close_receipt_present_.load()},
    };
}
}
