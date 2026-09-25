#include "lifecycle_record.hpp"
#include "stream_close.hpp"
#include <Windows.h>
#include <system_error>

namespace lvz::determinism {
namespace {
bool HexDigest64(const std::string& value) {
    if (value.size() != 64) return false;
    for (const char character : value)
        if (!((character >= '0' && character <= '9') || (character >= 'a' && character <= 'f')))
            return false;
    return true;
}
}

void LifecycleRecorder::Open(const std::filesystem::path& auditDir, LifecycleIdentity identity) {
    if (opened_ || finished_)
        throw std::runtime_error("Lifecycle recorder is already open");
    if (identity.run_id.empty() || identity.branch_id.empty() || identity.build_module.empty()
        || !HexDigest64(identity.build_sha256) || !identity.session_id
        || identity.sequence_domain != kLifecycleSequenceDomain)
        throw std::runtime_error("Lifecycle recorder identity is incomplete");
    auditDir_ = auditDir;
    eventsPath_ = auditDir / kLifecycleEventsFile;
    receiptPath_ = auditDir / kLifecycleReceiptFile;
    events_.open(eventsPath_, std::ios::out | std::ios::binary | std::ios::trunc);
    if (!events_)
        throw std::runtime_error("Cannot open lifecycle events output");
    hasher_ = std::make_unique<Sha256>();
    identity_ = std::move(identity);
    count_ = bytes_ = firstSequence_ = lastSequence_ = 0;
    digest_.clear();
    opened_ = true;
}

void LifecycleRecorder::Write(const Json& event) {
    if (!Opened())
        throw std::runtime_error("Lifecycle recorder is not open");
    const std::string schema = event.is_object() ? event.value("schema", "") : std::string();
    const std::string kind = event.is_object() ? event.value("kind", "") : std::string();
    const bool initialization = schema == kLifecycleEventSchema && kind == "zombie_initialized";
    const bool probe_event = schema == kLifecycleProbeEventSchema
        && (kind == "zombie_phase_transition" || kind == "zombie_removal_marked"
            || kind == "zombie_slot_recycle_candidate" || kind == "zombie_slot_recycle_commit");
    if (!event.is_object() || (!initialization && !probe_event))
        throw std::runtime_error("Lifecycle recorder accepts only complete lifecycle events");
    const auto& sequence = event.at("capture_sequence");
    if (!sequence.is_number_unsigned() || sequence.get<uint64_t>() == 0)
        throw std::runtime_error("Lifecycle event has no positive capture_sequence");
    const uint64_t captureSequence = sequence.get<uint64_t>();
    if (count_ && captureSequence <= lastSequence_)
        throw std::runtime_error("Lifecycle capture_sequence is not strictly increasing");
    const Json envelope = {
        {"schema", kLifecycleEnvelopeSchema},
        {"file_seq", count_},
        {"run_id", identity_.run_id},
        {"branch_id", identity_.branch_id},
        {"session_id", identity_.session_id},
        {"sequence_domain", identity_.sequence_domain},
        {"event", event},
    };
    const std::string line = envelope.dump();
    events_ << line << '\n';
    if (!events_)
        throw std::runtime_error("Lifecycle events write failed");
    hasher_->Update(line.data(), line.size());
    constexpr char newline = '\n';
    hasher_->Update(&newline, 1);
    bytes_ += line.size() + 1;
    if (!count_) firstSequence_ = captureSequence;
    lastSequence_ = captureSequence;
    ++count_;
}

void LifecycleRecorder::CloseEvents() {
    const std::string error = CloseOptionalStream(events_, "Lifecycle events");
    if (!error.empty())
        throw std::runtime_error(error);
    opened_ = false;
}

void LifecycleRecorder::Finish(bool complete, const Json& counters, const Json& probeHealth) {
    if (!Opened())
        throw std::runtime_error("Lifecycle recorder is not open");
    CloseEvents();
    finished_ = true;
    if (!complete)
        return;
    WriteReceipt(counters, probeHealth);
}

void LifecycleRecorder::Abandon() noexcept {
    try {
        if (events_.is_open()) events_.close();
    } catch (...) {
    }
    hasher_.reset();
    opened_ = false;
    finished_ = false;
    count_ = bytes_ = firstSequence_ = lastSequence_ = 0;
    digest_.clear();
}

void LifecycleRecorder::WriteReceipt(const Json& counters, const Json& probeHealth) {
    digest_ = hasher_->HexDigest();
    const Json receipt = {
        {"schema", kLifecycleReceiptSchema},
        {"run_id", identity_.run_id},
        {"branch_id", identity_.branch_id},
        {"session_id", identity_.session_id},
        {"sequence_domain", identity_.sequence_domain},
        {"envelope_schema", kLifecycleEnvelopeSchema},
        {"event_schema", kLifecycleEventSchema},
        {"probe", {{"name", "zombie-initialize-exit"}, {"schema", "lvz.spawn.v1"}}},
        {"build", {{"module", identity_.build_module}, {"sha256", identity_.build_sha256}}},
        {"manifest_sha256", FileSha256(auditDir_ / "manifest.json")},
        {"records", count_},
        {"first_capture_sequence", count_ ? Json(firstSequence_) : Json(nullptr)},
        {"last_capture_sequence", count_ ? Json(lastSequence_) : Json(nullptr)},
        {"bytes", bytes_},
        {"sha256", digest_},
        {"counters", counters},
        {"probe_health", probeHealth},
        {"completed", true},
        {"persistence", {{"method", "flush_close_then_atomic_receipt"},
                         {"receipt_written_after_close", true}}},
    };
    const auto temporary = auditDir_ / (std::string(kLifecycleReceiptFile) + ".partial");
    try {
        {
            std::ofstream receiptStream(temporary, std::ios::out | std::ios::binary | std::ios::trunc);
            if (!receiptStream)
                throw std::runtime_error("Cannot open lifecycle close receipt");
            receiptStream << receipt.dump() << '\n';
            if (!receiptStream)
                throw std::runtime_error("Lifecycle close receipt write failed");
            const std::string closeError = CloseOptionalStream(receiptStream, "Lifecycle close receipt");
            if (!closeError.empty())
                throw std::runtime_error(closeError);
        }
        if (!MoveFileExW(temporary.c_str(), receiptPath_.c_str(),
                         MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH))
            throw std::runtime_error("Lifecycle close receipt finalize failed");
    } catch (...) {
        std::error_code ignored;
        std::filesystem::remove(temporary, ignored);
        throw;
    }
}
}
