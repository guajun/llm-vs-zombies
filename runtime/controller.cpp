#include "controller.hpp"
#include <algorithm>
#include <limits>

namespace lvz::runtime {
Json Error(const std::string& id,const std::string& code,const std::string& message) {
    return {{"protocol",1},{"request_id",id},{"ok",false},{"error",{{"code",code},{"message",message}}}};
}
Json Success(const std::string& id,Json result) {
    return {{"protocol",1},{"request_id",id},{"ok",true},{"result",std::move(result)}};
}
Json Controller::Version() const { return {{"epoch",epoch_},{"tick",tick_},{"revision",revision_}}; }
Json Controller::Observe() { auto value=backend_.Observe(); value["version"]=Version();
    if(backend_.RequiresRenderPreparation())value["render_prepared"]=backend_.RenderPrepared();return value; }
Json Controller::Status() const {
    return {{"state",!fault_.empty()?"audit_failed":(!storageFault_.empty()||!journal_.Fault().empty()?"dedup_storage_failed":(pending_?"stepping":(terminalFrozen_?"terminal_frozen":(ready_?"paused_at_boundary":"outside_fight"))))},
        {"fault",fault_.empty()?Json(nullptr):Json(fault_)},
        {"dedup_storage_fault",storageFault_.empty()?(journal_.Fault().empty()?Json(nullptr):Json(journal_.Fault())):Json(storageFault_)},
        {"dedup",{{"entries",journal_.Entries()},{"disk_bytes",journal_.Bytes()},{"index_bytes",RequestJournal::BucketCount*sizeof(uint64_t)},{"hot_results",cache_.size()},{"sealed",journal_.Sealed()}}},
        {"version",Version()},{"pending_request_id",pending_?Json(pending_->id):Json(nullptr)},
        {"executed_ticks",pending_?pending_->executed:0}};
}
void Controller::Audit(const std::string& kind,const Json& payload) {
    // Native audit captures authoritative state itself. Its envelope only needs
    // a version; constructing client plantability grids here doubles their cost
    // on every simulated tick without adding any recorded information.
    try { backend_.Audit(kind,payload,{{"version",Version()}}); }
    catch(const std::exception& error) { Fail(error.what());throw; }
}
void Controller::Boundary() {
    if(!fault_.empty()||!storageFault_.empty()||!journal_.Fault().empty()) return;
    auto current=backend_.BoardIdentity();
    auto ready=backend_.Ready();
    auto clock=backend_.NativeTick();
    if (initialized_ && (current!=board_ || ready!=ready_ || (ready && clock<nativeTick_))) {
        // Losing the active Board outside a measured update cannot authorize
        // free-running menu/card-selection updates after the experiment ends.
        if(ready_ && (!ready || current!=board_)) terminalFrozen_=true;
        if (pending_) Finish("scene_changed");
        if(!fault_.empty()||!storageFault_.empty()||!journal_.Fault().empty()) return;
        ++epoch_; tick_=revision_=0; cache_.clear();journal_.Epoch(epoch_);
        captureCache_.clear();captureResponses_.clear();captureMetadataBytes_=0;
        backend_.ResetRenderPreparation();
    }
    initialized_=true; board_=current; ready_=ready; nativeTick_=clock;
}
bool Controller::ShouldStep() const { return fault_.empty() && storageFault_.empty() && journal_.Fault().empty() && !recordingClosed_ && !terminalFrozen_ && (!ready_ || pending_.has_value()); }
void Controller::StorageFail(const std::string& message) {
    if(storageFault_.empty()) storageFault_=message;
    terminalFrozen_=true;
    if(pending_) Finish("dedup_storage_failed");
}
void Controller::Fail(const std::string& message) {
    if(fault_.empty()) fault_=message.empty()?"Native boundary audit failed":message;
    terminalFrozen_=true;inStep_=false;
    backend_.InvalidateFrame("controller_fault");
    if(!pending_) return;
    auto p=std::move(*pending_);pending_.reset();
    auto response=Error(p.id,"audit_failed",fault_);
    response["error"]["details"]={{"version",Version()},{"requested_ticks",p.requested},
        {"executed_ticks",p.executed},{"action_results",p.actions},{"restart_required",true}};
    Complete(p.key,std::move(response));
}
void Controller::BeforeStep() {
    if (!ready_ || !pending_) return;
    preTick_=backend_.NativeTick(); inStep_=true;
    Audit("pre_step",{{"request_id",pending_->id},{"requested_ticks",pending_->requested},{"executed_ticks",pending_->executed}});
}
void Controller::AfterStep() {
    if (!inStep_) return;
    inStep_=false;
    const bool sameBoard=board_!=0 && backend_.BoardIdentity()==board_;
    const bool terminal=!backend_.Ready() || !sameBoard;
    // A replacement/freed Board has no comparable native clock. In particular,
    // do not subtract a new Board's clock or fabricate a post_step for it.
    if(!sameBoard) {
        backend_.InvalidateFrame("terminal_board_transition");
        terminalFrozen_=true;
        Audit("terminal_transition",{{"request_id",pending_->id},{"native_tick_delta",nullptr},
            {"tick_delta_verified",false},{"board_identity_preserved",false}});
        Finish("scene_changed"); Boundary(); return;
    }
    const auto afterTick=backend_.NativeTick();
    const int64_t delta=static_cast<int64_t>(afterTick)-static_cast<int64_t>(preTick_);
    if (delta>0 && delta<=MaxTicks) { tick_+=delta; pending_->executed+=delta; revision_=0; }
    nativeTick_=afterTick;
    Json post={{"request_id",pending_->id},{"native_tick_delta",delta},{"executed_ticks",pending_->executed}};
    if(delta==1&&!terminal&&backend_.RequiresRenderPreparation()) {
        // Do not emit an Audit event before rendering: pre_step's original
        // spawn/particle boundary must stay active through both update+draw.
        try {post["render"]=backend_.RenderFrame(Version(),false);}
        catch(const std::exception& error) {
            Audit("render_failed",{{"request_id",pending_->id},{"actual_executed_ticks",pending_->executed},{"message",error.what()}});
            Fail(std::string("Controlled render failed after native update: ")+error.what());return;
        }
    } else if(terminal||delta!=1) {
        backend_.InvalidateFrame("unverified_or_terminal_update");
        if(terminal&&delta==1&&backend_.RequiresRenderPreparation())
            post["render"]={{"schema","lvz.controlled-render.v1"},{"mode","deterministic_draw_schedule_v1"},
                {"phase","terminal"},{"skipped",true},{"reason","left_ready_fight"},
                {"frame_version",Version()},{"native_clock",afterTick},{"cache_invalidated",true}};
    }
    Audit("post_step",post);
    if(terminal) {
        terminalFrozen_=true;
        Audit("terminal_transition",{{"request_id",pending_->id},{"native_tick_delta",delta},
            {"tick_delta_verified",delta==1},{"board_identity_preserved",true}});
        // Keep the just-measured version in both audit and completed response.
        // Only after delivery may Boundary reset tick/revision for the new epoch.
        Finish("scene_changed"); Boundary(); return;
    }
    // An unexpected update count is an error, never silently called one step.
    if (delta!=1) { Finish(delta==0?"no_game_tick":"step_count_mismatch"); return; }
    if (pending_->untilWave && backend_.NativeWave()!=pending_->startWave) { Finish("wave_changed"); return; }
    if (pending_->executed>=pending_->requested) Finish("budget_exhausted");
}
void Controller::Complete(const std::string& key,Json response,bool seal) {
    auto it=cache_.find(key);
    if (it==cache_.end()) return;
    it->second.response=response;
    bool persisted=false;
    try {
        if(seal) {
            // A previous double-write failure can leave an exact final result
            // only in RAM. Never seal an archive until those results are saved.
            for(auto old=cache_.begin();old!=cache_.end();) {
                if(old==it||!old->second.response) { ++old;continue; }
                journal_.Complete(old->second.token,old->first,old->second.response->dump(),true);
                old=cache_.erase(old);
            }
        }
        journal_.Complete(it->second.token,key,response.dump());
        if(seal) journal_.Seal();
        persisted=true;
    } catch(const JournalError& error) {
        storageFault_=error.what();terminalFrozen_=true;
        if(seal) {
            response=Error(key,"recording_close_incomplete",error.what());
            response["error"]["details"]={{"native_recording_closed",recordingClosed_},{"journal_sealed",false},{"restart_required",true}};
            it->second.response=response;
        }
    }
    auto callbacks=std::move(it->second.waiters);
    if(persisted) cache_.erase(it);
    for (auto& reply:callbacks) reply(response);
    if(pending_&&(!storageFault_.empty()||!journal_.Fault().empty())) StorageFail("request journal storage failed during a pending advancement");
}
void Controller::Finish(const std::string& reason) {
    if (!pending_) return;
    auto p=*pending_; inStep_=false;
    Json result={{"action_results",p.actions},{"requested_ticks",p.requested},{"executed_ticks",p.executed},
        {"stop_reason",reason},{"observation",Observe()}};
    Audit("request_completed",{{"request_id",p.id},{"result",result}});
    pending_.reset();
    if(reason=="no_game_tick"||reason=="step_count_mismatch") {
        auto error=Error(p.id,reason,"Native update count did not equal one; advancement stopped at the observed boundary");
        error["error"]["details"]=std::move(result);Complete(p.key,std::move(error));
    } else Complete(p.key,Success(p.id,std::move(result)));
}
void Controller::Stop(const std::string& reason) { if(pending_) Finish(reason); }
void Controller::Disconnect(const std::string& requestId) { if(pending_&&pending_->id==requestId) Finish("client_disconnected"); }

void Controller::Request(const Json& req,Reply reply) {
    std::string id;
    // A completion can evict its hot Entry before an audit exception unwinds
    // here. Track delivery independently, including callbacks living past this
    // stack frame, so an already answered request never uses a moved callback.
    auto delivered=std::make_shared<bool>(false);
    reply=[original=std::move(reply),delivered](Json response) {
        *delivered=true;original(std::move(response));
    };
    try {
        if(!req.is_object() || !req.contains("request_id") || !req["request_id"].is_string()) {
            reply(Error("","invalid_request","request_id must be a string")); return;
        }
        id=req["request_id"].get<std::string>();
        if(id.empty()||id.size()>256||req.value("protocol",0)!=1||!req.contains("method")||!req["method"].is_string()) {
            reply(Error(id,"invalid_request","Expected protocol 1, bounded request_id and method")); return;
        }
        std::string method=req["method"];
        Json params=req.value("params",Json::object());
        if(!params.is_object()) { reply(Error(id,"invalid_request","params must be an object")); return; }
        if(method=="hello") {
            auto result=backend_.Hello(); result["protocol"]=1; result["epoch"]=epoch_;
            result["limits"]={{"max_ticks",MaxTicks},{"max_actions",MaxActions},{"dedup_entries_per_epoch",journal_.Limits().normalEntries},
                {"dedup_index_bytes",RequestJournal::BucketCount*sizeof(uint64_t)},{"dedup_disk_bytes",journal_.Limits().normalBytes},
                {"dedup_control_reserve_entries",journal_.Limits().reserveEntries},{"dedup_control_reserve_bytes",journal_.Limits().reserveBytes},
                {"dedup_close_reserved_entries",1},{"dedup_close_reserved_bytes",journal_.CloseReserveBytes()},{"control_request_bytes",4096},
                {"dedup_response_storage","append_only_disk_journal"},
                {"capture_cached_responses",4},{"capture_ids_per_epoch",100000},{"capture_metadata_bytes",16*1024*1024},{"capture_request_bytes",4096}};
            reply(Success(id,std::move(result))); return;
        }
        if(method=="observe") { reply(Success(id,Observe())); return; }
        if(method=="audit_snapshot") { reply(Success(id,{{"state",backend_.AuditSnapshot()},{"version",Version()}}));return; }
        if(method=="status") {
            if(params.contains("request_id")) {
                std::string wanted=params["request_id"].get<std::string>();
                auto it=cache_.find(wanted); auto result=Status();
                result["request_state"]=it==cache_.end()?"unknown":(it->second.response?"completed":"pending");
                if(it!=cache_.end()&&it->second.response) result["response"]=*it->second.response;
                if(it==cache_.end()) if(auto old=journal_.Find(wanted)) {
                    result["request_state"]=old->response?"completed":"pending";
                    if(old->response) result["response"]=Json::parse(*old->response);
                }
                reply(Success(id,std::move(result)));
            } else reply(Success(id,Status()));
            return;
        }
        if(method!="commit"&&method!="advance"&&method!="pause"&&method!="cancel"&&method!="initialize"&&method!="rng_restore"&&method!="rng_seed"&&method!="clock_restore"&&method!="capture_frame"&&method!="stop_recording"&&method!="prepare_render") {
            reply(Error(id,"unsupported_method","Method is not implemented")); return;
        }
        std::string canonical=req.dump();
        if(auto it=cache_.find(id);it!=cache_.end()) {
            if(it->second.payload!=canonical) reply(Error(id,"request_id_conflict","Same request_id has different content in this epoch"));
            else if(it->second.response) reply(*it->second.response);
            else it->second.waiters.push_back(std::move(reply));
            return;
        }
        if(auto old=journal_.Find(id)) {
            if(old->payload!=canonical) reply(Error(id,"request_id_conflict","Same request_id has different content in this epoch"));
            else if(old->response) reply(Json::parse(*old->response));
            else reply(Error(id,"dedup_storage_failed","Accepted request has no recoverable completion; it will never execute again"));
            return;
        }
        if(auto it=captureCache_.find(id);it!=captureCache_.end()) {
            if(it->second.payload!=canonical) reply(Error(id,"request_id_conflict","Same request_id has different content in this epoch"));
            else if(it->second.response) reply(*it->second.response);
            else reply(Error(id,"request_result_expired","Capture response expired; the same ID will never trigger another capture"));
            return;
        }
        if(!fault_.empty()&&method!="stop_recording"&&method!="pause"&&method!="cancel") {
            reply(Error(id,"audit_failed",fault_+"; begin a new process and run"));return;
        }
        if((!storageFault_.empty()||!journal_.Fault().empty())&&method!="stop_recording"&&method!="pause"&&method!="cancel") {
            reply(Error(id,"dedup_storage_failed","Request journal is unavailable; advancement is frozen"));return;
        }
        // A successful close seals the journal before acknowledging the request.
        // Later new IDs cannot append rejection/control records to the archive.
        if(recordingClosed_) { reply(Error(id,"recording_closed","This run has been finalized; begin a new process and run"));return; }
        if(method=="capture_frame") {
            if(canonical.size()>4096) { reply(Error(id,"invalid_params","Capture request must fit within 4096 bytes"));return; }
            if(captureCache_.size()>=100000 || captureMetadataBytes_+canonical.size()>16*1024*1024) {
                reply(Error(id,"capture_dedup_capacity","Capture ID metadata limit reached; begin a new controlled session"));return;
            }
            if(!req.contains("expect")||req["expect"]!=Version()) {
                reply(Error(id,"stale_observation","expect must exactly match epoch, tick and revision"));return;
            }
            if(pending_||inStep_) { reply(Error(id,"busy","Capture requires a paused game boundary"));return; }
            if(recordingClosed_) { reply(Error(id,"recording_closed","This run has been finalized"));return; }
            if(!ready_||terminalFrozen_) { reply(Error(id,"not_in_fight","Capture requires a paused active fight"));return; }
            captureMetadataBytes_+=canonical.size();captureCache_[id]={std::move(canonical),std::nullopt};
            Json response;
            try {
                const auto beforeClock=backend_.NativeTick();
                const auto beforeBoard=backend_.BoardIdentity();
                auto captureParams=params;captureParams["frame_version"]=Version();
                auto result=backend_.CaptureFrame(captureParams);
                if(!result.is_object()||!result.contains("capture_ok")||!result["capture_ok"].is_boolean())
                    throw std::runtime_error("Capture backend must return a boolean capture_ok");
                const bool rngChanged=result.contains("known_rng_unchanged")&&result["known_rng_unchanged"]==false;
                const bool clockChanged=result.contains("game_clock_before")&&result.contains("game_clock_after")
                    &&result["game_clock_before"]!=result["game_clock_after"];
                const bool boundaryChanged=beforeClock!=backend_.NativeTick()||beforeBoard!=backend_.BoardIdentity()||!backend_.Ready();
                if(rngChanged||clockChanged||boundaryChanged) {
                    ++revision_;result["capture_ok"]=false;result["reason"]="capture_changed_game_state";
                    result.erase("pixels_base64");
                    if(beforeBoard!=backend_.BoardIdentity()||!backend_.Ready()) terminalFrozen_=true;
                }
                result["version"]=Version();response=Success(id,std::move(result));
                if(response.dump().size()>4*1024*1024) response=Error(id,"capture_frame_too_large","Capture response exceeds the 4 MiB frame limit");
            } catch(const std::exception& error) {
                // Backend exceptions may follow a partial render; invalidate
                // outstanding observations instead of declaring it read-only.
                ++revision_;response=Error(id,"capture_failed",error.what());
            }
            captureCache_[id].response=response;captureResponses_.push_back(id);
            while(captureResponses_.size()>4) {
                captureCache_.at(captureResponses_.front()).response.reset();captureResponses_.pop_front();
            }
            // Pixel data belongs to the video pipeline, never the native state
            // audit or the action dedup budget. Expired IDs remain tombstones.
            reply(std::move(response));return;
        }
        if(method=="commit"||method=="advance"||method=="initialize"||method=="rng_restore"||method=="rng_seed"||method=="clock_restore"||method=="stop_recording"||method=="prepare_render"||req.contains("expect")) {
            if(!req.contains("expect") || req["expect"]!=Version()) {
                reply(Error(id,"stale_observation","expect must exactly match epoch, tick and revision")); return;
            }
        }
        const bool control=method=="pause"||method=="cancel"||method=="stop_recording";
        if(control&&canonical.size()>4096) { reply(Error(id,"invalid_params","Control requests must fit within 4096 bytes"));return; }
        // An unsuccessful close must not consume the dedicated final ID/bytes.
        if(method=="stop_recording"&&pending_) { reply(Error(id,"busy","Stop advancement with pause/cancel before closing recording"));return; }
        if(control&&method!="stop_recording"&&std::any_of(cache_.begin(),cache_.end(),[](const auto& entry){return entry.second.response.has_value();})) {
            reply(Error(id,"dedup_storage_failed","Simulation is already frozen; a RAM-only result must be saved by close before admitting further controls"));return;
        }
        auto token=journal_.Reserve(id,canonical,control,method=="stop_recording");
        cache_[id]={std::move(canonical),std::nullopt,{std::move(reply)},token};
        if(method=="pause"||method=="cancel") {
            Stop(method=="cancel"?"cancelled":"paused"); Complete(id,Success(id,{{"observation",Observe()},{"state",Status()["state"]}})); return;
        }
        if(pending_) { Complete(id,Error(id,"busy","Another advancement is pending; use a second connection to cancel")); return; }
        if(recordingClosed_) { Complete(id,Error(id,"recording_closed","This run has been finalized; begin a new process and run"));return; }
        if(method=="stop_recording") {
            Audit("recording_closed",{{"request_id",id}});backend_.CloseRecording();recordingClosed_=true;
            Complete(id,Success(id,{{"closed",true},{"observation",Observe()}}),true);return;
        }
        if(method=="prepare_render") {
            if(!params.empty()) {Complete(id,Error(id,"invalid_params","prepare_render takes no parameters"));return;}
            if(!ready_||terminalFrozen_||inStep_||tick_!=0) {Complete(id,Error(id,"render_prepare_rejected","Warm drawing requires a paused ready fight at tick zero"));return;}
            if(!backend_.RequiresRenderPreparation()||backend_.RenderPrepared()) {Complete(id,Error(id,"render_prepare_rejected","Warm drawing is unavailable or has already completed"));return;}
            ++revision_;
            Audit("render_preparing",{{"request_id",id}});
            Json receipt;
            try {receipt=backend_.RenderFrame(Version(),true);}
            catch(const std::exception& error) {
                Fail(std::string("Warm drawing failed: ")+error.what());
                Audit("render_failed",{{"request_id",id},{"warm",true},{"message",error.what()}});
                Complete(id,Error(id,"render_failed",error.what()));return;
            }
            Audit("render_prepared",{{"request_id",id},{"render",receipt}});
            Complete(id,Success(id,{{"prepared",true},{"render",receipt},{"observation",Observe()}}));return;
        }
        if(method=="rng_restore"||method=="rng_seed"||method=="clock_restore") {
            if(!ready_||terminalFrozen_||inStep_) { Complete(id,Error(id,"not_in_fight","State initialization requires a paused fight boundary"));return; }
            Json result;
            std::string event;
            Json payload={{"request_id",id}};
            if(method=="rng_seed") {
                if(!params.contains("seed") || !params["seed"].is_number_integer()) {
                    Complete(id,Error(id,"invalid_params","seed must be an integer in 0..4294967295"));return;
                }
                const auto& seed=params["seed"];
                const bool valid=seed.is_number_unsigned() ? seed.get<uint64_t>()<=UINT32_MAX
                    : seed.get<int64_t>()>=0 && static_cast<uint64_t>(seed.get<int64_t>())<=UINT32_MAX;
                if(!valid) { Complete(id,Error(id,"invalid_params","seed must be an integer in 0..4294967295"));return; }
                result=backend_.SeedRng(seed.get<uint32_t>());event="rng_seeded";payload["seed"]=seed;
            } else {
                if(!params.contains("snapshot") || !params["snapshot"].is_object()) {
                    Complete(id,Error(id,"invalid_params","snapshot must be an object"));return;
                }
                result=method=="clock_restore" ? backend_.RestoreClocks(params["snapshot"]) : backend_.RestoreRng(params["snapshot"]);
                event=method=="clock_restore" ? "clocks_restored" : "rng_restored";
                payload["snapshot"]=params["snapshot"];
            }
            if(!result.value("ok",false)) { Complete(id,Error(id,method+"_rejected",result.value("error",std::string("State initialization failed"))));return; }
            if(method=="clock_restore") nativeTick_=backend_.NativeTick();
            backend_.InvalidateFrame(method);
            ++revision_;result["observation"]=Observe();Audit(event,payload);
            Complete(id,Success(id,std::move(result)));return;
        }
        if(method=="initialize") {
            auto result=backend_.Initialize(params);
            if(!result.value("ok",false)) { Complete(id,Error(id,"initialization_rejected",result.value("error",std::string("initialization rejected")))); return; }
            terminalFrozen_=false;
            ++revision_;result["observation"]=Observe();
            Audit("initialization_configured",{{"request_id",id},{"params",params},{"result",result}});
            Complete(id,Success(id,std::move(result)));return;
        }
        if(!ready_||terminalFrozen_) { Complete(id,Error(id,"not_in_fight","A stable active fight without a frozen terminal transition is required")); return; }
        if(backend_.RequiresRenderPreparation()&&!backend_.RenderPrepared()) {
            Complete(id,Error(id,"render_not_prepared","Seed/read back RNG and restore clocks, then prepare_render before advancement/actions"));return;
        }
        const char* countKey=method=="advance"?"max_ticks":"advance_ticks";
        Json count=params.value(countKey,Json(0));
        if(!count.is_number_integer()||count.get<int64_t>()<0||count.get<int64_t>()>MaxTicks) {
            Complete(id,Error(id,"invalid_params","Tick budget must be an integer from 0 to 100000")); return;
        }
        bool untilWave=false;
        if(params.contains("until")) {
            if(params["until"]!=Json{{"event","wave_changed"}}) { Complete(id,Error(id,"unsupported_condition","Only until.event=wave_changed is supported")); return; }
            untilWave=true;
        }
        Json actions=params.value("actions",Json::array());
        if(!actions.is_array()||actions.size()>MaxActions||(method=="advance"&&!actions.empty())) {
            Complete(id,Error(id,"invalid_params","Expected at most 256 actions; advance takes no actions")); return;
        }
        pending_=Pending{id,id,count.get<int>(),0,backend_.NativeWave(),untilWave,Json::array()};
        Audit("request_started",{{"request_id",id},{"request",req}});
        for(size_t i=0;i<actions.size();++i) {
            Json outcome;
            try { outcome=backend_.Execute(actions[i]); }
            catch(const std::exception& e) { outcome={{"ok",false},{"error",e.what()}}; }
            outcome["ordinal"]=i; outcome["action"]=actions[i];
            // Even failed calls can touch cursor/selection state; conservatively invalidate observations.
            ++revision_;
            backend_.InvalidateFrame("action_attempt");
            pending_->actions.push_back(outcome);
            Audit("action",{{"request_id",id},{"ordinal",i},{"action",actions[i]},{"result",outcome}});
            if(!outcome.value("ok",false)) { Finish("action_failed"); return; }
        }
        if(pending_->requested==0) Finish("budget_exhausted");
    } catch(const JournalCapacity& e) {
        reply(Error(id,"dedup_capacity",e.what()));
    } catch(const JournalError& e) {
        StorageFail(e.what());reply(Error(id,"dedup_storage_failed",e.what()));
    } catch(const std::exception& e) {
        if(*delivered) return;
        if(cache_.contains(id)) { if(pending_&&pending_->id==id) pending_.reset(); Complete(id,Error(id,"invalid_request",e.what())); }
        else reply(Error(id,"invalid_request",e.what()));
    }
}
}
