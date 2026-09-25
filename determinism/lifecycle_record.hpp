#pragma once
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <memory>
#include <string>
#include <nlohmann/json.hpp>
#include "sha256.hpp"

// Issue #111 lifecycle persistence adapter (experiment-only, no game code and
// no Agent dependency).
//
// The audit host's single destructive DrainSpawnBatch() is the only producer
// of these records. One LifecycleRecorder owns audit/lifecycle-events.jsonl
// for a run: it wraps each immutable lvz.lifecycle-event.v1 projection in an
// envelope that binds run, branch, session and sequence domain, and it keeps
// the exact SHA-256 of the written bytes.
//
// Finish() flushes and closes the events stream first and only then writes
// audit/lifecycle-close-receipt.jsonl through a temporary file plus
// MoveFileExW(MOVEFILE_REPLACE_EXISTING|MOVEFILE_WRITE_THROUGH). The receipt
// carries the record count, the byte count, the digest, the final counters and
// the hash of audit/manifest.json. A crash, a failed write or a failed close
// therefore leaves no receipt, and the offline validator refuses that file
// set. There is no "closed in memory" success flag to trust.
namespace lvz::determinism {
using Json = nlohmann::json;

inline constexpr const char* kLifecycleMode = "lvz.lifecycle-recording.v1";
inline constexpr const char* kLifecycleEnvelopeSchema = "lvz.lifecycle-record.v1";
inline constexpr const char* kLifecycleEventSchema = "lvz.lifecycle-event.v1";
inline constexpr const char* kLifecycleProbeEventSchema = "lvz.lifecycle-event.v2";
inline constexpr const char* kLifecycleReceiptSchema = "lvz.lifecycle-close-receipt.v1";
inline constexpr const char* kLifecycleSequenceDomain = "lvz.measurement.capture-sequence";
inline constexpr const char* kLifecycleEventsFile = "lifecycle-events.jsonl";
inline constexpr const char* kLifecycleReceiptFile = "lifecycle-close-receipt.jsonl";

struct LifecycleIdentity {
    std::string run_id;
    std::string branch_id;
    std::string build_module;
    std::string build_sha256;
    uint64_t session_id = 0;
    std::string sequence_domain = kLifecycleSequenceDomain;
};

class LifecycleRecorder {
public:
    LifecycleRecorder() = default;
    LifecycleRecorder(const LifecycleRecorder&) = delete;
    LifecycleRecorder& operator=(const LifecycleRecorder&) = delete;

    // Create the events file under an existing audit directory. Throws when
    // the identity is malformed or the file cannot be opened. The writer never
    // reopens a file: a run directory is created fresh per run.
    void Open(const std::filesystem::path& auditDir, LifecycleIdentity identity);

    bool Opened() const noexcept { return opened_ && !finished_; }
    const LifecycleIdentity& Identity() const noexcept { return identity_; }

    // Append one envelope. Refuses anything that is not a complete
    // initialization lifecycle projection and refuses a capture_sequence that
    // is not strictly greater than the previous one, so a corrupted batch
    // fails the session instead of producing plausible-looking evidence.
    void Write(const Json& event);

    // Flush and close the events stream, then write the receipt when
    // `complete`. Always throws on a failed write, close or receipt
    // finalization; the close receipt is only ever the last durable step.
    void Finish(bool complete, const Json& counters = Json::object(),
                const Json& probeHealth = Json::object());

    // Best-effort release after a failed Finish so a later Initialize can
    // start a fresh run in the same process. Never writes a receipt.
    void Abandon() noexcept;

    uint64_t Count() const noexcept { return count_; }
    uint64_t Bytes() const noexcept { return bytes_; }
    const std::string& Digest() const noexcept { return digest_; }
    uint64_t FirstSequence() const noexcept { return firstSequence_; }
    uint64_t LastSequence() const noexcept { return lastSequence_; }
    const std::filesystem::path& EventsPath() const noexcept { return eventsPath_; }
    const std::filesystem::path& ReceiptPath() const noexcept { return receiptPath_; }

#ifdef LVZ_LIFECYCLE_TESTING
    // The no-game fixture forces a genuine stream failure through the real
    // close path (setstate(failbit)) and verifies that Finish refuses the
    // receipt and releases the file handle. This is not exposed by the DLL.
    std::ofstream& EventsStreamForTest() noexcept { return events_; }
#endif

private:
    void WriteReceipt(const Json& counters, const Json& probeHealth);
    void CloseEvents();

    std::filesystem::path auditDir_;
    std::filesystem::path eventsPath_;
    std::filesystem::path receiptPath_;
    std::ofstream events_;
    std::unique_ptr<Sha256> hasher_;
    LifecycleIdentity identity_;
    uint64_t count_ = 0;
    uint64_t bytes_ = 0;
    uint64_t firstSequence_ = 0;
    uint64_t lastSequence_ = 0;
    std::string digest_;
    bool opened_ = false;
    bool finished_ = false;
};
}
