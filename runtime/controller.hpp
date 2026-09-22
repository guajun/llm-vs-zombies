#pragma once
#include <nlohmann/json.hpp>
#include "request_journal.hpp"
#include "engine_call.hpp"
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
    virtual bool UsesEngineCallBoundary() const { return false; }
    virtual int GameUi() const { return Ready()?3:0; }
    virtual Json NativeClocks() const { return {{"game_clock",NativeTick()}}; }
    virtual Json Observe() = 0;
    virtual Json Execute(const Json& action) = 0;
    virtual Json Hello() = 0;
    virtual Json Initialize(const Json&) { return {{"ok",false},{"error","unsupported"}}; }
    virtual Json Initialize(const Json& params,const Json&) {return Initialize(params);}
    virtual Json AuditSnapshot() { throw std::runtime_error("audit snapshots are unsupported"); }
    virtual Json FloatingPointEvidence() {return nullptr;}
    virtual Json RestoreRng(const Json&) { return {{"ok",false},{"error","unsupported"}}; }
    virtual Json SeedRng(uint32_t) { return {{"ok",false},{"error","unsupported"}}; }
    virtual Json RestoreClocks(const Json&) { return {{"ok",false},{"error","unsupported"}}; }
    virtual Json CaptureFrame(const Json&) { return {{"capture_ok",false},{"reason","unsupported"}}; }
    virtual bool RequiresRenderPreparation() const { return false; }
    virtual bool RenderPrepared() const { return true; }
    virtual bool SupportsAppUpdateAnchor() const { return false; }
    virtual bool AppUpdateAnchored() const { return false; }
    virtual Json AnchorAppUpdate(uint32_t) { return {{"ok",false},{"error","unsupported"}}; }
    virtual bool SupportsSoundCounterOrigin()const {return false;}
    virtual bool SoundCounterBound()const {return false;}
    virtual Json BindSoundCounterOrigin(){return {{"ok",false},{"error","unsupported"}};}
    virtual Json RenderFrame(const Json&,bool) { return Json::object(); }
    virtual void InvalidateFrame(const std::string&) {}
    virtual void ResetRenderPreparation() {}
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
    explicit Controller(Backend& backend, JournalOptions journal={}) : backend_(backend),journal_(std::move(journal)) {
        const auto& branch=journal_.Branch();
        if(!branch.empty()&&!RequestJournal::ValidBranch(branch)) throw std::runtime_error("branch scope id is invalid");
        branchOrigin_=journal_.Limits().adopt?"adopted":"session";
    }
    void Boundary();
    void Request(const Json& request, Reply reply);
    bool ShouldStep() const;
    void BeforeStep();
    void AfterStep();
    // The production wrapper and fixtures share this actual callback boundary.
    bool RunEngineFrame(const std::function<void()>& originalUpdate);
    bool GuardEngineEntry() { return engineCalls_.GuardEntry(); }
    void CheckEngineGuard() const { if(engineCalls_.Faulted())throw std::runtime_error("Engine invocation guard fault"); }
    Json EngineCallHealth() const { return engineCalls_.Health(); }
    void Stop(const std::string& reason);
    void Disconnect(const std::string& requestId);
    void Fail(const std::string& message);
    Json Observe();
    Json Version() const;
    Json Status() const;
    // Branch scope identity of this runtime instance (issue #32). It belongs to
    // hello/status and to the request namespace, never to the comparable game
    // state, the audit manifest or the state digests.
    Json BranchIdentity() const;
#ifdef LVZ_REQUEST_JOURNAL_TESTING
    RequestJournal& JournalForTesting() { return journal_; }
#endif
private:
    struct Entry { std::string payload; std::optional<Json> response; std::vector<Reply> waiters; uint64_t token=0; };
    struct CaptureEntry { std::string payload; std::optional<Json> response; };
    struct Pending { std::string key; std::string id; int requested=0; int executed=0; int startWave=0; bool untilWave=false; Json actions=Json::array();
        uint64_t calls=0,terminalZero=0,lastCall=0;std::string terminalKind; };
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
    // Only one terminal transition can occur in a frozen run. Keep its exact
    // serialized reply recoverable after Boundary advances the epoch.
    struct TerminalReply {std::string id,canonical,response;};
    std::optional<TerminalReply> terminalReply_;
    EngineCallTracker engineCalls_;
    uint64_t activeCall_=0;
    bool wrapperActive_=false;
    int preUi_=0;
    uintptr_t preBoard_=0;
    std::string branchOrigin_;
    std::string branchParent_;
    Json callMetadata_,callRaw_;
    Json CallCounts(const Pending& pending) const;
    void Complete(const std::string& key, Json response, bool seal=false);
    void StorageFail(const std::string& message);
    void Finish(const std::string& reason);
    void Audit(const std::string& kind, const Json& payload);
};
Json Error(const std::string& id, const std::string& code, const std::string& message);
Json Success(const std::string& id, Json result);
}
