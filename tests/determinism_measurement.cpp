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

        // --- Session 3: fault reporting, faulted-close refusal, Abort, reopen ---
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

        // A faulted session must still be terminable so cleanup can run and a
        // new session can start, without emitting a successful close receipt.
        Check(Host().Abort(&error), "Abort must finalize a faulted session");
        Check(Host().Failed() && !Host().CloseReceiptPresent()
                  && Host().Health().at("state") == "failed",
              "Abort must mark failed without a close receipt");
        Check(Counter(Host().Health(), "wrong_thread") == 2 && Counter(Host().Health(), "overflow") == 1,
              "Abort must preserve fault evidence");
        Check(Host().Open(GetCurrentThreadId(), &error) && Host().SessionId() == 4,
              "next-round re-init must succeed after Abort");

        // --- Session 4: persistence failure is not a successful close ---
        Host().OnCaptured();
        Host().OnDelivered(1);
        Check(!Host().Close(&error) && error == "measurement batch is incomplete",
              "Close must refuse a batch that was not persisted");
        Check(Host().Abort(&error), "Abort must finalize an incompletely persisted session");
        Check(Host().Failed() && !Host().CloseReceiptPresent(), "Abort must not fabricate a receipt");

        // --- Shutdown sequence: the body fault (drain overflow) must still
        //     finalize the session and run cleanup before the error returns ---
        Check(Host().Open(GetCurrentThreadId(), &error), "open drain-fault shutdown session");
        bool cleanupRan = false;
        const std::string drainError = lvz::measurement::RunMeasurementShutdown(
            [&] { throw std::runtime_error("spawn_hook_fault: overflow"); },
            [&] { cleanupRan = true; });
        Check(drainError == "spawn_hook_fault: overflow", "the first error must be preserved");
        Check(cleanupRan, "cleanup must run after a drain fault");
        Check(Host().Failed() && !Host().CloseReceiptPresent(),
              "a drain fault must finalize the session without a success receipt");

        // --- Shutdown sequence: a write failure (delivered but not persisted)
        //     also terminates the session as failed ---
        Check(Host().Open(GetCurrentThreadId(), &error), "open write-failure shutdown session");
        cleanupRan = false;
        const std::string writeError = lvz::measurement::RunMeasurementShutdown(
            [&] {
                Host().OnCaptured();
                Host().OnDelivered(1);
                throw std::runtime_error("event write failed");
            },
            [&] { cleanupRan = true; });
        Check(writeError == "event write failed", "the first write error must be preserved");
        Check(cleanupRan, "cleanup must run after a write failure");
        Check(Host().Failed() && !Host().CloseReceiptPresent(),
              "a write failure must not produce a success receipt");

        // --- Shutdown sequence: a clean body closes with a success receipt ---
        Check(Host().Open(GetCurrentThreadId(), &error), "open clean shutdown session");
        cleanupRan = false;
        const std::string cleanError = lvz::measurement::RunMeasurementShutdown(
            [&] { Host().OnCaptured(); Host().OnDelivered(1); Host().OnPersisted(1); },
            [&] { cleanupRan = true; });
        Check(cleanError.empty(), "a clean shutdown must have no error");
        Check(cleanupRan, "cleanup must run on a clean shutdown");
        Check(Host().Closed() && Host().CloseReceiptPresent(), "a clean shutdown must emit a success receipt");

        std::cout << "measurement: shared capture_sequence, fault counters, session/close/abort/shutdown lifecycle passed\n";
        return 0;
    } catch (const std::exception& exception) {
        std::cerr << exception.what() << '\n';
        return 1;
    }
}
