#include "fp_environment.hpp"
#include <limits>
#include <stdexcept>

namespace lvz::determinism::fpenv {
Json Sample::Value()const{return {{"x87_control",x87},{"x87_status",status},{"mxcsr",mxcsr},
    {"mxcsr_control",mxcsr&MxcsrControlMask}};}
Sample Read()noexcept {
    Sample value;
    __asm__ volatile("fnstcw %0":"=m"(value.x87));
    __asm__ volatile("fnstsw %0":"=m"(value.status));
    __asm__ volatile("stmxcsr %0":"=m"(value.mxcsr));
    return value;
}
Json Manifest(){return {{"mode",Mode},{"x87_control",X87Control},{"mxcsr_control",MxcsrControl},
    {"mxcsr_control_mask",MxcsrControlMask},{"activation","initialize_after_finish_title_before_seed_and_enter_game"},
    {"scope","game_owner_thread_boundary_controls"},{"activation_count",1},{"sticky_status_preserved",true},
    {"drift_policy","latch_fault_without_repair"},{"raw_evidence","fp-environment-raw.jsonl"},
    {"original_engine_bitwise_unmodified",false},{"whole_process_fp_controlled",false}};}
const char* Name(Phase phase) {
    static constexpr const char* names[]={"loop","initialization","ready","before_original_update","after_original_update",
        "before_warm_draw","after_warm_draw","before_step_draw","after_step_draw","pre_step","post_step","snapshot","close"};
    return names[size_t(phase)];
}
void Monitor::Fault(const char* reason,const char* phase,const Sample& sample) {
    if(fault_.is_null())fault_={{"reason",reason},{"phase",phase},{"owner_thread_id",owner_},
        {"actual_thread_id",GetCurrentThreadId()},{"raw",sample.Value()}};
}
bool Monitor::CheckLocked(Phase phase,const Sample& sample,bool required) {
    last_=sample;
    if(GetCurrentThreadId()!=owner_) {++wrongThreads_;Fault("wrong_thread",Name(phase),sample);return false;}
    if(closed_)return fault_.is_null()&&active_;
    if(!active_) {
        if(required)Fault("mode_not_activated",Name(phase),sample);
        return !required&&fault_.is_null();
    }
    auto& count=checks_[size_t(phase)];
    if(count==std::numeric_limits<uint64_t>::max()) {Fault("check_counter_overflow",Name(phase),sample);return false;}
    ++count;
    if(!sample.Target())Fault("control_drift",Name(phase),sample);
    return fault_.is_null();
}
Json Monitor::Activate(int ui,uintptr_t board,const Json& context) {
    const auto before=Read();
    std::lock_guard lock(mutex_);
    // Repeated calls are rejected before any write and do not reset the first
    // receipt. RPC same-ID retry is handled by the controller journal.
    if(attempted_)throw std::runtime_error("Fixed FP activation already attempted; restart required");
    attempted_=true;
    Json receipt={{"schema","lvz.fp-activation.v1"},{"mode",Mode},{"request_id",context.at("request_id")},
        {"version",context.at("version")},{"phase","initialize_before_seed_and_enter_game"},
        {"owner_thread_id",owner_},{"actual_thread_id",GetCurrentThreadId()},
        {"game_ui",ui},{"board_address",board},{"before",before.Value()},{"write_attempted",false}};
    if(GetCurrentThreadId()!=owner_) {++wrongThreads_;Fault("wrong_thread","activation",before);}
    else if(ui!=1||board)Fault("requires_menu_without_board","activation",before);
    else if(closed_||!fault_.is_null())Fault("unavailable_lifecycle","activation",before);
    else {
        const uint16_t cw=X87Control;
        const uint32_t mxcsr=(before.mxcsr&~MxcsrControlMask)|MxcsrControl;
        receipt["write_attempted"]=true;
        // No finit/fnclex/fxrstor: preserve x87 stack/status and SSE sticky flags.
        __asm__ volatile("fldcw %0"::"m"(cw));
        __asm__ volatile("ldmxcsr %0"::"m"(mxcsr));
        ++activations_;active_=true;
    }
    last_=Read();
    if(receipt.at("write_attempted")==true&&(!last_.Target()||last_.status!=before.status
            ||(last_.mxcsr&~MxcsrControlMask)!=(before.mxcsr&~MxcsrControlMask)))
        Fault("activation_readback_failed","activation",last_);
    receipt["after"]=last_.Value();receipt["activation_count"]=activations_;
    receipt["ok"]=active_&&fault_.is_null();receipt["first_fault"]=fault_;
    activation_=receipt;
    return receipt;
}
bool Monitor::Check(Phase phase,bool required) {
    const auto sample=Read();std::lock_guard lock(mutex_);return CheckLocked(phase,sample,required);
}
Sample Monitor::Capture() {
    const auto sample=Read();std::lock_guard lock(mutex_);
    // Capturing fault evidence is allowed. It never writes or clears controls.
    CheckLocked(Phase::Snapshot,sample,false);return sample;
}
Json Monitor::Boundary(const Json& envelope,const Sample& sample) {
    std::lock_guard lock(mutex_);
    const auto phase=envelope.at("kind")=="pre_step"?Phase::PreStep:Phase::PostStep;
    if(!CheckLocked(phase,sample,true))throw std::runtime_error("Fixed FP boundary control failed");
    if(rawFrames_==std::numeric_limits<uint64_t>::max()) {Fault("raw_counter_overflow",Name(phase),sample);throw std::runtime_error("FP raw count exhausted");}
    ++rawFrames_;
    auto payload=sample.Value();payload["owner_thread_id"]=owner_;payload["actual_thread_id"]=GetCurrentThreadId();
    return {{"schema","lvz.fp-environment-raw.v1"},{"seq",envelope.at("seq")},{"kind",envelope.at("kind")},
        {"version",envelope.at("version")},{"engine_call_id",envelope.at("payload").at("engine_call").at("engine_call_id")},
        {"payload",std::move(payload)}};
}
Json Monitor::HealthLocked()const {
    Json checks=Json::object();for(size_t i=0;i<checks_.size();++i)checks[Name(Phase(i))]=checks_[i];
    return {{"schema","lvz.fp-health.v1"},{"mode",Mode},{"activated",active_},{"activation_count",activations_},
        {"owner_thread_id",owner_},{"closed",closed_},{"healthy",active_&&fault_.is_null()&&wrongThreads_==0},
        {"wrong_thread_checks",wrongThreads_},{"raw_frames",rawFrames_},{"checks",std::move(checks)},
        {"first_fault",fault_},{"last_raw",last_.Value()}};
}
Json Monitor::Evidence()const {std::lock_guard lock(mutex_);return {{"activation",activation_},{"health",HealthLocked()}};}
Json Monitor::Close(){const auto sample=Read();std::lock_guard lock(mutex_);
    if(!closed_) {CheckLocked(Phase::Close,sample,true);closed_=true;}
    return HealthLocked();}
}
