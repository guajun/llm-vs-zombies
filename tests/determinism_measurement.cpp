#include "determinism/measurement.hpp"
#include <Windows.h>
#include <atomic>
#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <thread>

// Offline fixture for the issue #111 stage B measurement common layer. It
// never loads the game, installs a hook or runs an experiment: it proves the
// shared capture_sequence domain, the fault counters and the single-host
// batch/close ledger that every lifecycle capture point reuses.
namespace {
using lvz::measurement::Host;
void Check(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}
uint64_t Counter(const lvz::measurement::Json& health, const char* key) {
    const auto value = health.at(key);
    if (!value.is_number_unsigned()) throw std::runtime_error("counter is not uint64");
    return value.get<uint64_t>();
}
}

int main() {
    try {
        Host().Open(GetCurrentThreadId());
        auto health = Host().Health();
        for (const char* key : {"captured", "delivered", "persisted", "overflow",
                                "wrong_thread", "nesting_mismatch", "incomplete_events"})
            Check(Counter(health, key) == 0, "Open must zero every counter");
        Check(health.at("close_receipt_present") == false, "Open must clear the close receipt");

        // Shared, monotonic, drain-independent capture_sequence.
        const uint64_t a = Host().NextSequence();
        const uint64_t b = Host().NextSequence();
        Check(a == 1 && b == 2, "capture_sequence must start at 1 and be monotonic");

        // Two independent capture points interleave in the same order domain.
        Host().OnCaptured();                       // probe A captures a record
        const uint64_t c = Host().NextSequence();  // probe B captures next
        Host().OnCaptured();
        Check(c == 3, "a second probe must allocate from the same shared sequence domain");

        // Fault accounting is explicit and granular.
        Host().OnOverflow();
        Host().OnWrongThread();
        Host().OnNestingMismatch();
        Host().OnIncomplete();
        health = Host().Health();
        Check(Counter(health, "overflow") == 1 && Counter(health, "wrong_thread") == 1
                  && Counter(health, "nesting_mismatch") == 1 && Counter(health, "incomplete_events") == 1,
              "fault counters must be reported individually");

        // Delivery/persistence belong to the recorder adapter, not the probe.
        Host().OnDelivered(2);
        Host().OnPersisted(2);
        health = Host().Health();
        Check(Counter(health, "captured") == 2 && Counter(health, "delivered") == 2
                  && Counter(health, "persisted") == 2,
              "delivery/persistence counters must not be fabricated by the probe");

        // A foreign thread never receives a sequence and is counted as a fault.
        std::atomic<bool> leaked{false};
        std::thread other([&leaked] { leaked = Host().NextSequence() != 0; });
        other.join();
        Check(!leaked, "foreign thread must not receive a capture_sequence");
        Check(Counter(Host().Health(), "wrong_thread") == 2, "foreign-thread allocation must be counted");

        // Close receipt is the only way to finalize health.
        const auto receipt = Host().Close();
        Check(receipt.at("close_receipt_present") == true, "Close must emit the receipt flag");
        Check(Host().CloseReceiptPresent(), "CloseReceiptPresent accessor must agree");

        // Re-opening (a new measurement run) resets the domain and the ledger.
        Host().Open(GetCurrentThreadId());
        Check(Host().NextSequence() == 1, "Open must reset the capture_sequence domain");
        Check(Counter(Host().Health(), "delivered") == 0, "Open must reset the batch ledger");

        std::cout << "measurement: shared capture_sequence, fault counters, batch ledger, close receipt passed\n";
        return 0;
    } catch (const std::exception& exception) {
        std::cerr << exception.what() << '\n';
        return 1;
    }
}
