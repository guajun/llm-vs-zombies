#pragma once
#include <nlohmann/json.hpp>
#include <atomic>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <thread>

namespace lvz::runtime {
inline constexpr const char* kEngineCallMode="controlled_engine_call_v1";
inline nlohmann::json EngineCallManifest() {
    return {{"mode",kEngineCallMode},{"entry_rva",337488},
        {"invocation_evidence","checked_wrapper_enter_and_return"},
        {"call_id_scope","process_controlled_calls_only"},{"counter_bits",64},
        {"initialization_calls_counted",false},{"ordinary_clock_delta",1},
        {"terminal_zero_clock_update",true},{"zero_terminal_ui_pairs",{{3,4}}},
        {"board_identity_basis","non-null App.Board pointer preserved across the checked call"},
        {"internal_board_tick_count_verified",false},{"live_verified",false},
        {"raw_evidence","engine-call-raw.jsonl"}};
}
// Scalar lifecycle evidence lives independently of pending requests. In
// particular, rejecting a nested callback never completes the outer request.
class EngineCallTracker {
    const std::thread::id owner_=std::this_thread::get_id();
    uint64_t reserved_=0,entered_=0,returned_=0,posts_=0,steps_=0,zero_=0,terminalSteps_=0,aborted_=0,active_=0;
    enum class Phase { Idle, Prepared, Entered, Returned } phase_=Phase::Idle;
    std::atomic<uint64_t> faults_{0},reentrant_{0},wrongThread_{0};
    void Reject(const char* reason) {++faults_;throw std::runtime_error(reason);}
    void Owner() {if(std::this_thread::get_id()!=owner_){++wrongThread_;Reject("Engine call accessed off its owner thread");}}
    void Token(uint64_t id,Phase phase) {Owner();if(!id||id!=active_||phase_!=phase)Reject("Engine call token/lifecycle mismatch");}
public:
    bool GuardEntry() noexcept {
        if(std::this_thread::get_id()!=owner_){++wrongThread_;++faults_;return false;}
        if(active_){++reentrant_;++faults_;return false;}
        return true;
    }
    uint64_t Reserve() {
        Owner();if(active_)Reject("Engine call already active");
        if(reserved_==std::numeric_limits<uint64_t>::max())Reject("Engine call ID exhausted");
        active_=++reserved_;phase_=Phase::Prepared;return active_;
    }
    void Enter(uint64_t id) {Token(id,Phase::Prepared);++entered_;phase_=Phase::Entered;}
    void Returned(uint64_t id) {Token(id,Phase::Entered);++returned_;phase_=Phase::Returned;}
    void Complete(uint64_t id,bool post,bool step,bool terminalZero,bool terminalStep) {
        Token(id,Phase::Returned);posts_+=post;steps_+=step;zero_+=terminalZero;terminalSteps_+=terminalStep;
        active_=0;phase_=Phase::Idle;
    }
    void Abort(uint64_t id) {
        Owner();if(id!=active_||!active_)Reject("Engine call abort token mismatch");
        // An audit/serialization failure after normal return must not turn a
        // measured return into an aborted original invocation.
        if(phase_==Phase::Entered)++aborted_;
        ++faults_;active_=0;phase_=Phase::Idle;
    }
    bool InFlight() const {return phase_==Phase::Entered;}
    bool Active() const {return active_!=0;}
    bool Faulted() const {return faults_.load()!=0;}
    uint64_t EnteredCount() const {return entered_;}
    uint64_t ReturnedCount() const {return returned_;}
    nlohmann::json Health() const {
        return {{"mode",kEngineCallMode},{"reserved_calls",reserved_},{"entered_calls",entered_},
            {"returned_calls",returned_},{"written_post_boundaries",posts_},
            {"verified_clock_steps",steps_},{"verified_terminal_zero_calls",zero_},{"terminal_clock_steps",terminalSteps_},
            {"active_call_id",active_?nlohmann::json(active_):nlohmann::json(nullptr)},
            {"faults",faults_.load()},{"reentrant_calls",reentrant_.load()},{"wrong_thread_calls",wrongThread_.load()},{"aborted_calls",aborted_},
            {"healthy",!Faulted()&&!active_&&reserved_==entered_&&entered_==returned_&&returned_==posts_&&returned_==steps_+zero_&&zero_<=1}};
    }
};
}
