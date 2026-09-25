#include "determinism/lifecycle_record.hpp"
#include "determinism/measurement.hpp"
#include <Windows.h>
#include <atomic>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <nlohmann/json.hpp>

// Offline fixture for the issue #111 lifecycle persistence adapter. It never
// loads the game, installs a hook or runs an experiment. It proves the real
// writer/receipt protocol, the fail-closed behavior of genuine stream/close
// failures, handle release, and the RunMeasurementShutdown integration that
// audit.cpp uses.
namespace {
using Json = nlohmann::json;
using lvz::determinism::LifecycleIdentity;
using lvz::determinism::LifecycleRecorder;

std::atomic<uint32_t> counter{0};

void Check(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

struct TempDir {
    std::filesystem::path path;
    TempDir() {
        path = std::filesystem::temp_directory_path()
            / ("lvz-lifecycle-" + std::to_string(GetCurrentProcessId()) + "-" + std::to_string(++counter));
        std::filesystem::create_directories(path);
    }
    ~TempDir() {
        std::error_code ignored;
        std::filesystem::remove_all(path, ignored);
    }
};

void WriteManifest(const std::filesystem::path& audit) {
    std::ofstream manifest(audit / "manifest.json", std::ios::out | std::ios::binary | std::ios::trunc);
    manifest << "{\"schema\":1,\"target\":\"lifecycle-fixture\",\"loaded_signatures_match\":true}\n";
    if (!manifest) throw std::runtime_error("fixture manifest write failed");
}

LifecycleIdentity Identity(uint64_t session = 7) {
    return {"run-lifecycle-fixture", "branch-lifecycle-fixture", "recorder.dll", std::string(64, 'a'), session};
}

Json Event(uint64_t sequence, uint64_t invocation, uint32_t depth, Json parent) {
    return {
        {"schema", "lvz.lifecycle-event.v1"},
        {"kind", "zombie_initialized"},
        {"capture_sequence", sequence},
        {"version", nullptr},
        {"version_phase", "initialization"},
        {"engine_call_id", nullptr},
        {"invocation", {{"invocation_id", invocation}, {"depth", depth}, {"parent_invocation_id", parent}}},
        {"entity", {{"id", 0x00010001}, {"slot", 1}, {"generation", 1}}},
        {"before_after", {{"before", nullptr},
                          {"after", {{"id", 0x00010001}, {"slot", 1}, {"generation", 1}}}}},
        {"classification", {{"class", "initialization"}, {"cause", "unknown"}}},
        {"probe", {{"name", "zombie-initialize-exit"}, {"schema", "lvz.spawn.v1"},
                   {"sequence_domain", "lvz.measurement.capture-sequence"}}},
        {"complete", true},
    };
}

Json Counters(uint64_t records) {
    return {{"captured", records}, {"delivered", records}, {"persisted", records},
            {"overflow", 0}, {"wrong_thread", 0}, {"nesting_mismatch", 0}, {"incomplete_events", 0}};
}

Json ProbeHealth(uint64_t records) {
    return {{"captured", records}, {"queued", 0}, {"wrong_thread_calls", 0}, {"faults", 0},
            {"overflow", 0}, {"active_initializers", 0}, {"healthy", true}};
}

Json ReadSingleJson(const std::filesystem::path& path) {
    std::ifstream stream(path, std::ios::in | std::ios::binary);
    if (!stream) throw std::runtime_error("fixture file unreadable");
    Json value;
    stream >> value;
    return value;
}

uint64_t FileBytes(const std::filesystem::path& path) {
    std::error_code error;
    const auto size = std::filesystem::file_size(path, error);
    if (error) throw std::runtime_error("fixture file size failed");
    return size;
}

// The clean protocol: records are persisted, the events stream is closed, the
// receipt binds their exact bytes, and every handle is released.
void CleanProtocol() {
    TempDir directory;
    const auto audit = directory.path / "audit";
    std::filesystem::create_directories(audit);
    WriteManifest(audit);
    std::filesystem::path events;
    {
        LifecycleRecorder recorder;
        recorder.Open(audit, Identity());
        Check(recorder.Opened(), "recorder must be open");
        Check(!std::filesystem::exists(audit / "lifecycle-close-receipt.jsonl"),
              "no receipt may exist before Finish");
        recorder.Write(Event(1, 1, 0, nullptr));
        recorder.Write(Event(3, 2, 0, nullptr));   // a legitimate gap: no contiguity requirement
        recorder.Write(Event(4, 3, 1, 2));
        Check(recorder.Count() == 3 && recorder.Bytes() > 0, "writer counters must track records");
        Check(recorder.FirstSequence() == 1 && recorder.LastSequence() == 4, "writer sequence bounds");
        events = recorder.EventsPath();
        recorder.Finish(true, Counters(3), ProbeHealth(3));
        Check(!recorder.Opened(), "Finish must close the session");
    }
    Check(std::filesystem::exists(audit / "lifecycle-close-receipt.jsonl"), "receipt must exist after a clean finish");
    Check(!std::filesystem::exists(audit / ("lifecycle-close-receipt.jsonl.partial")), "no partial receipt may remain");
    const auto receipt = ReadSingleJson(audit / "lifecycle-close-receipt.jsonl");
    Check(receipt.at("schema") == "lvz.lifecycle-close-receipt.v1", "receipt schema");
    Check(receipt.at("records") == 3 && receipt.at("first_capture_sequence") == 1
              && receipt.at("last_capture_sequence") == 4, "receipt record bounds");
    Check(receipt.at("bytes") == FileBytes(events), "receipt byte count must match the events file");
    Check(receipt.at("sha256") == lvz::determinism::FileSha256(events),
          "receipt digest must match the events file bytes");
    Check(receipt.at("manifest_sha256") == lvz::determinism::FileSha256(audit / "manifest.json"),
          "receipt must bind the audit manifest digest");
    Check(receipt.at("completed") == true && receipt.at("run_id") == "run-lifecycle-fixture"
              && receipt.at("branch_id") == "branch-lifecycle-fixture" && receipt.at("session_id") == 7,
          "receipt identity");
    // Read back the envelopes: the writer order, identity binding and event
    // payload are on disk exactly as written.
    std::ifstream stream(events, std::ios::in | std::ios::binary);
    std::string line;
    const uint64_t expectedSequences[] = {1, 3, 4};
    uint64_t index = 0;
    while (std::getline(stream, line)) {
        const auto envelope = Json::parse(line);
        Check(envelope.at("schema") == "lvz.lifecycle-record.v1", "envelope schema");
        Check(envelope.at("file_seq") == index, "file_seq must be the written index");
        Check(envelope.at("session_id") == 7 && envelope.at("sequence_domain") == "lvz.measurement.capture-sequence",
              "envelope identity");
        const auto& event = envelope.at("event");
        Check(event.at("capture_sequence") == expectedSequences[index], "events must stay in capture order");
        if (index == 2) Check(event.at("invocation").at("parent_invocation_id") == 2, "nested parent binding");
        ++index;
    }
    Check(index == 3, "events file must contain exactly the written records");
}

// A zero-record enabled run is a measured zero: the receipt proves the stream
// was opened, flushed and closed with no events.
void ZeroRecordProtocol() {
    TempDir directory;
    const auto audit = directory.path / "audit";
    std::filesystem::create_directories(audit);
    WriteManifest(audit);
    LifecycleRecorder recorder;
    recorder.Open(audit, Identity());
    recorder.Finish(true, Counters(0), ProbeHealth(0));
    const auto receipt = ReadSingleJson(audit / "lifecycle-close-receipt.jsonl");
    Check(receipt.at("records") == 0 && receipt.at("first_capture_sequence").is_null()
              && receipt.at("last_capture_sequence").is_null(), "an empty run must record a measured zero");
    Check(receipt.at("sha256") == lvz::determinism::FileSha256(recorder.EventsPath()),
          "an empty events file still needs its digest");
}

// Destroying a recorder without Finish must still release the file handle,
// which is what lets Android/Windows tooling recreate or clean the run.
void HandleRelease() {
    TempDir directory;
    const auto audit = directory.path / "audit";
    std::filesystem::create_directories(audit);
    WriteManifest(audit);
    {
        LifecycleRecorder recorder;
        recorder.Open(audit, Identity());
        recorder.Write(Event(1, 1, 0, nullptr));
        // Deliberately no Finish: only the destructor can release the stream.
    }
    std::error_code error;
    std::filesystem::remove_all(directory.path, error);
    Check(!error && !std::filesystem::exists(directory.path),
          "a destroyed recorder must release the events file handle");
}

// A write failure is detected before any success receipt can be written, and
// the stream can still be closed afterwards without a receipt.
void WriteFailure() {
    TempDir directory;
    const auto audit = directory.path / "audit";
    std::filesystem::create_directories(audit);
    WriteManifest(audit);
    LifecycleRecorder recorder;
    recorder.Open(audit, Identity());
    recorder.EventsStreamForTest().setstate(std::ios::failbit);
    bool wrote = false;
    try { recorder.Write(Event(1, 1, 0, nullptr)); } catch (const std::exception&) { wrote = true; }
    Check(wrote, "a failed write must throw");
    bool closed = false;
    try { recorder.Finish(false); } catch (const std::exception&) { closed = true; }
    Check(closed, "close must surface the injected stream failure");
    Check(!std::filesystem::exists(audit / "lifecycle-close-receipt.jsonl"),
          "a failed write must not leave a success receipt");
}

// A close failure after records were written refuses the receipt even when the
// caller asked for a complete session.
void CloseFailureRefusesReceipt() {
    TempDir directory;
    const auto audit = directory.path / "audit";
    std::filesystem::create_directories(audit);
    WriteManifest(audit);
    LifecycleRecorder recorder;
    recorder.Open(audit, Identity());
    recorder.Write(Event(1, 1, 0, nullptr));
    recorder.EventsStreamForTest().setstate(std::ios::failbit);
    bool failed = false;
    try { recorder.Finish(true, Counters(1), ProbeHealth(1)); } catch (const std::exception&) { failed = true; }
    Check(failed, "a failed close must throw");
    Check(!std::filesystem::exists(audit / "lifecycle-close-receipt.jsonl"),
          "a failed close must not leave a success receipt");
    Check(std::filesystem::exists(recorder.EventsPath()), "the evidence file must remain for diagnosis");
}

// The final atomic rename is the last gate: when it cannot complete, no
// receipt and no partial file may remain.
void ReceiptFinalizeFailure() {
    TempDir directory;
    const auto audit = directory.path / "audit";
    std::filesystem::create_directories(audit);
    WriteManifest(audit);
    std::filesystem::create_directory(audit / "lifecycle-close-receipt.jsonl");  // genuine target obstruction
    LifecycleRecorder recorder;
    recorder.Open(audit, Identity());
    recorder.Write(Event(1, 1, 0, nullptr));
    bool failed = false;
    try { recorder.Finish(true, Counters(1), ProbeHealth(1)); } catch (const std::exception&) { failed = true; }
    Check(failed, "an obstructed receipt target must throw");
    Check(!std::filesystem::exists(audit / "lifecycle-close-receipt.jsonl.partial"),
          "a failed finalization must remove its partial receipt");
}

// A real open failure: the events path is a directory.
void OpenFailure() {
    TempDir directory;
    const auto audit = directory.path / "audit";
    std::filesystem::create_directories(audit / "lifecycle-events.jsonl");
    WriteManifest(audit);
    LifecycleRecorder recorder;
    bool failed = false;
    try { recorder.Open(audit, Identity()); } catch (const std::exception&) { failed = true; }
    Check(failed, "opening over a directory must fail");
    Check(!recorder.Opened(), "a failed open must not leave the recorder open");
    bool identityFailed = false;
    LifecycleIdentity bad = Identity();
    bad.build_sha256 = "not-a-digest";
    try { recorder.Open(audit, bad); } catch (const std::exception&) { identityFailed = true; }
    Check(identityFailed, "an incomplete build identity must be refused");
}

// The writer refuses duplicates and decreases before they reach the file.
void SequenceDiscipline() {
    TempDir directory;
    const auto audit = directory.path / "audit";
    std::filesystem::create_directories(audit);
    WriteManifest(audit);
    LifecycleRecorder recorder;
    recorder.Open(audit, Identity());
    recorder.Write(Event(5, 1, 0, nullptr));
    bool duplicate = false, decrease = false;
    try { recorder.Write(Event(5, 2, 0, nullptr)); } catch (const std::exception&) { duplicate = true; }
    try { recorder.Write(Event(4, 3, 0, nullptr)); } catch (const std::exception&) { decrease = true; }
    Check(duplicate && decrease, "duplicate and decreasing capture_sequence must be refused");
    recorder.Finish(false);
    Check(!std::filesystem::exists(audit / "lifecycle-close-receipt.jsonl"),
          "a refused batch must not produce a receipt");
}

// A failed session can be released so a later Initialize in the same process
// can open a fresh stream (the audit singleton recovery path).
void AbandonAndReopen() {
    TempDir directory;
    const auto audit = directory.path / "audit";
    std::filesystem::create_directories(audit);
    WriteManifest(audit);
    LifecycleRecorder recorder;
    recorder.Open(audit, Identity());
    recorder.Write(Event(1, 1, 0, nullptr));
    recorder.EventsStreamForTest().setstate(std::ios::failbit);
    bool failed = false;
    try { recorder.Finish(true, Counters(1), ProbeHealth(1)); } catch (const std::exception&) { failed = true; }
    Check(failed && !std::filesystem::exists(audit / "lifecycle-close-receipt.jsonl"),
          "a failed finish must leave no receipt");
    recorder.Abandon();
    Check(!recorder.Opened(), "Abandon must release the failed session");
    recorder.Open(audit, Identity());
    recorder.Write(Event(1, 1, 0, nullptr));
    recorder.Finish(true, Counters(1), ProbeHealth(1));
    Check(std::filesystem::exists(audit / "lifecycle-close-receipt.jsonl"),
          "a reopened recorder must be able to finish cleanly");
}

// The audit shutdown path: a clean session commits only after the receipt was
// written; a lifecycle finalization failure downgrades the session to failed
// without a receipt; an aborted body must not write one either.
void ShutdownIntegration() {
    using lvz::measurement::Host;
    using lvz::measurement::RunMeasurementShutdown;
    const auto countersOf = [](const Json& health) {
        return Json{{"captured", health.at("captured")}, {"delivered", health.at("delivered")},
                    {"persisted", health.at("persisted")}, {"overflow", health.at("overflow")},
                    {"wrong_thread", health.at("wrong_thread")}, {"nesting_mismatch", health.at("nesting_mismatch")},
                    {"incomplete_events", health.at("incomplete_events")}};
    };

    {
        TempDir directory;
        const auto audit = directory.path / "audit";
        std::filesystem::create_directories(audit);
        WriteManifest(audit);
        std::string error;
        Check(Host().Open(GetCurrentThreadId(), &error), "open clean shutdown session");
        LifecycleRecorder recorder;
        recorder.Open(audit, Identity(Host().SessionId()));
        const std::string shutdown = RunMeasurementShutdown(
            [&] {
                Host().OnCaptured();
                Host().OnDelivered(1);
                Host().OnPersisted(1);
                recorder.Write(Event(1, 1, 0, nullptr));
            },
            [&] {
                recorder.Finish(Host().Closing(), countersOf(Host().Health()), ProbeHealth(1));
            });
        Check(shutdown.empty(), "clean shutdown must have no error");
        Check(Host().Closed() && Host().CloseReceiptPresent(), "clean shutdown must commit after the receipt");
        Check(std::filesystem::exists(audit / "lifecycle-close-receipt.jsonl"),
              "clean shutdown must leave the close receipt");
    }
    {
        TempDir directory;
        const auto audit = directory.path / "audit";
        std::filesystem::create_directories(audit);
        WriteManifest(audit);
        std::string error;
        Check(Host().Open(GetCurrentThreadId(), &error), "open failing shutdown session");
        LifecycleRecorder recorder;
        recorder.Open(audit, Identity(Host().SessionId()));
        recorder.EventsStreamForTest().setstate(std::ios::failbit);
        const std::string shutdown = RunMeasurementShutdown(
            [&] { Host().OnCaptured(); Host().OnDelivered(1); Host().OnPersisted(1); },
            [&] { recorder.Finish(Host().Closing(), Counters(1), ProbeHealth(1)); });
        Check(!shutdown.empty(), "a lifecycle close failure must propagate");
        Check(Host().Failed() && !Host().CloseReceiptPresent(), "a lifecycle failure must downgrade to failed");
        Check(!std::filesystem::exists(audit / "lifecycle-close-receipt.jsonl"),
              "a failed shutdown must not leave a success receipt");
    }
    {
        TempDir directory;
        const auto audit = directory.path / "audit";
        std::filesystem::create_directories(audit);
        WriteManifest(audit);
        std::string error;
        Check(Host().Open(GetCurrentThreadId(), &error), "open aborted shutdown session");
        LifecycleRecorder recorder;
        recorder.Open(audit, Identity(Host().SessionId()));
        const std::string shutdown = RunMeasurementShutdown(
            [&] {
                recorder.Write(Event(1, 1, 0, nullptr));
                throw std::runtime_error("spawn_hook_fault: overflow");
            },
            [&] { recorder.Finish(Host().Closing(), Counters(0), ProbeHealth(0)); });
        Check(shutdown == "spawn_hook_fault: overflow", "the first body error must be preserved");
        Check(Host().Failed() && !Host().CloseReceiptPresent(), "an aborted body must not commit");
        Check(!std::filesystem::exists(audit / "lifecycle-close-receipt.jsonl"),
              "an aborted body must not produce a receipt");
    }
}
}

int main() {
    try {
        CleanProtocol();
        ZeroRecordProtocol();
        HandleRelease();
        WriteFailure();
        CloseFailureRefusesReceipt();
        ReceiptFinalizeFailure();
        OpenFailure();
        SequenceDiscipline();
        AbandonAndReopen();
        ShutdownIntegration();
        // Releasing every recorder above must release every handle: the
        // temporary directories are removable with no lingering lock.
        std::cout << "lifecycle: persistence protocol, fault injection and shutdown integration passed\n";
        return 0;
    } catch (const std::exception& exception) {
        std::cerr << exception.what() << '\n';
        return 1;
    }
}
