#include "determinism/measurement.hpp"
#include <Windows.h>
#include <atomic>
#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>

// Offline fixture for the issue #111 stage B measurement common layer. It
// never loads the game, installs a hook or runs an experiment: it proves the
// shared capture_sequence domain, the fault counters, and the single-host
// session/close lifecycle that every lifecycle capture point reuses.
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
        std::string error;
        // --- Session 1: clean open, shared domain, balanced close, post-close refusal ---
        Check(Host().Open(GetCurrentThreadId(), &error), "first Open must succeed");
        Check(Host().SessionId() == 1, "first session id");
        auto health = Host().Health();
        for (const char* key : {"captured", "delivered", "persisted", "overflow",
                                "wrong_thread", "nesting_mismatch", "incomplete_events", "refused"})
            Check(Counter(health, key) == 0, "Open must zero every counter");
        Check(health.at("close_receipt_present") == false, "Open must clear the close receipt");

        Check(Host().NextSequence() == 1 && Host().NextSequence() == 2,
              "capture_sequence must start at 1 and be monotonic");

        // Two independent capture points interleave in the same order domain.
        Host().OnCaptured();                            // probe A captures a record
        const uint64_t other = Host().NextSequence();   // probe B captures next
        Host().OnCaptured();
        Check(other == 3, "a second probe must allocate from the same shared sequence domain");

        Host().OnDelivered(2);
        Host().OnPersisted(2);
        health = Host().Health();
        Check(Counter(health, "captured") == 2 && Counter(health, "delivered") == 2
                  && Counter(health, "persisted") == 2,
              "delivery/persistence counters must balance for a clean close");

        Check(Host().Close(&error), "a balanced, fault-free session must close");
        Check(Host().CloseReceiptPresent() && Host().Health().at("close_receipt_present") == true,
              "Close must emit the receipt flag");

        // Post-close capture/delivery/persistence must be refused, not silently
        // appended to the already-final receipt.
        Check(Host().NextSequence() == 0, "post-close NextSequence must be refused");
        Host().OnCaptured();
        Host().OnPersisted(7);
        health = Host().Health();
        Check(Counter(health, "captured") == 2 && Counter(health, "delivered") == 2
                  && Counter(health, "persisted") == 2 && Counter(health, "refused") >= 1,
              "post-close operations must not modify the final ledger");

        // --- Session 2: Open-after-Close, repeated-Open refusal, incomplete-close refusal ---
        Check(Host().Open(GetCurrentThreadId(), &error), "Open after Close must start a new session");
        Check(Host().SessionId() == 2, "a new session needs a new identity");
        Check(!Host().Open(GetCurrentThreadId(), &error) && error == "measurement session is already open",
              "Open must refuse an active session");

        Host().OnCaptured();
        Check(!Host().Close(&error) && error == "measurement batch is incomplete",
              "Close must refuse an undrained batch");
        Host().OnDelivered(1);
        Host().OnPersisted(1);
        Check(Host().Close(&error), "a rebalanced session must close");

        // --- Session 3: fault reporting, faulted-close refusal, wrong-thread refusal ---
        Check(Host().Open(GetCurrentThreadId(), &error), "third Open must succeed");
        Host().OnOverflow();
        Host().OnWrongThread();
        Host().OnNestingMismatch();
        Host().OnIncomplete();
        health = Host().Health();
        Check(Counter(health, "overflow") == 1 && Counter(health, "wrong_thread") == 1
                  && Counter(health, "nesting_mismatch") == 1 && Counter(health, "incomplete_events") == 1,
              "fault counters must be reported individually");
        Check(!Host().Close(&error) && error == "measurement session has faults",
              "Close must refuse a faulted session");

        std::atomic<bool> leaked{false};
        std::thread foreign([&leaked] { leaked = Host().NextSequence() != 0; });
        foreign.join();
        Check(!leaked, "foreign thread must not receive a capture_sequence");
        Check(Counter(Host().Health(), "wrong_thread") == 2, "foreign-thread allocation must be counted");

        std::cout << "measurement: shared capture_sequence, fault counters, session/close lifecycle passed\n";
        return 0;
    } catch (const std::exception& exception) {
        std::cerr << exception.what() << '\n';
        return 1;
    }
}
