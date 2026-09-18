#pragma once
#include <nlohmann/json.hpp>
#include "request_journal.hpp"
#include <cstdint>
#include <deque>
#include <functional>
#include <map>
#include <optional>
#include <string>
#include <vector>

namespace lvz::runtime {
using Json = nlohmann::json;
struct Backend {
    virtual ~Backend() = default;
    virtual bool Ready() const = 0;
    virtual std::uintptr_t BoardIdentity() const = 0;
    virtual int NativeTick() const = 0;
    virtual int NativeWave() { return Observe().value("wave",0); }
    virtual Json Observe() = 0;
    virtual Json Execute(const Json& action) = 0;
    virtual Json Hello() = 0;
    virtual Json Initialize(const Json&) { return {{"ok",false},{"error","unsupported"}}; }
    virtual Json AuditSnapshot() { throw std::runtime_error("audit snapshots are unsupported"); }
    virtual Json RestoreRng(const Json&) { return {{"ok",false},{"error","unsupported"}}; }
    virtual Json SeedRng(uint32_t) { return {{"ok",false},{"error","unsupported"}}; }
    virtual Json RestoreClocks(const Json&) { return {{"ok",false},{"error","unsupported"}}; }
    virtual Json CaptureFrame(const Json&) { return {{"capture_ok",false},{"reason","unsupported"}}; }
    virtual void CloseRecording() {}
    virtual void Audit(const std::string&, const Json&, const Json&) = 0;
};

// This class is deliberately single-threaded. Only the game thread may call it.
// Native I/O queues immutable requests; it never calls a Backend method.
class Controller {
public:
    static constexpr int MaxTicks = 100000;
    static constexpr int MaxActions = 256;
    using Reply = std::function<void(Json)>;
    explicit Controller(Backend& backend, JournalOptions journal={}) : backend_(backend),journal_(std::move(journal)) {}
    void Boundary();
    void Request(const Json& request, Reply reply);
    bool ShouldStep() const;
    void BeforeStep();
    void AfterStep();
    void Stop(const std::string& reason);
    void Disconnect(const std::string& requestId);
    void Fail(const std::string& message);
    Json Observe();
    Json Version() const;
    Json Status() const;
#ifdef LVZ_REQUEST_JOURNAL_TESTING
    RequestJournal& JournalForTesting() { return journal_; }
#endif
private:
    struct Entry { std::string payload; std::optional<Json> response; std::vector<Reply> waiters; uint64_t token=0; };
    struct CaptureEntry { std::string payload; std::optional<Json> response; };
    struct Pending { std::string key; std::string id; int requested=0; int executed=0; int startWave=0; bool untilWave=false; Json actions=Json::array(); };
    Backend& backend_;
    RequestJournal journal_;
    uint64_t epoch_=1, tick_=0, revision_=0;
    size_t captureMetadataBytes_=0;
    std::uintptr_t board_=0;
    bool initialized_=false, ready_=false, inStep_=false;
    bool recordingClosed_=false;
    bool terminalFrozen_=false;
    std::string fault_;
    std::string storageFault_;
    int nativeTick_=0, preTick_=0;
    std::map<std::string, Entry> cache_;
    std::map<std::string, CaptureEntry> captureCache_;
    std::deque<std::string> captureResponses_;
    std::optional<Pending> pending_;
    void Complete(const std::string& key, Json response, bool seal=false);
    void StorageFail(const std::string& message);
    void Finish(const std::string& reason);
    void Audit(const std::string& kind, const Json& payload);
};
Json Error(const std::string& id, const std::string& code, const std::string& message);
Json Success(const std::string& id, Json result);
}
