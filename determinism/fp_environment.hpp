#pragma once
#include <nlohmann/json.hpp>
#include <windows.h>
#include <array>
#include <cstdint>
#include <mutex>

namespace lvz::determinism::fpenv {
using Json=nlohmann::json;
inline constexpr char Mode[]="fixed_owner_fp_v1";
inline constexpr uint16_t X87Control=0x027f;
inline constexpr uint32_t MxcsrControl=0x1f80, MxcsrControlMask=0xffffffc0u;
struct Sample {
    uint16_t x87=0,status=0;
    uint32_t mxcsr=0;
    Json Value()const;
    bool Target()const{return x87==X87Control&&(mxcsr&MxcsrControlMask)==MxcsrControl;}
};
Sample Read()noexcept;
Json Manifest();
enum class Phase:size_t { Loop,Initialization,Ready,BeforeUpdate,AfterUpdate,BeforeWarm,AfterWarm,
    BeforeDraw,AfterDraw,PreStep,PostStep,Snapshot,Close,Count };
const char* Name(Phase phase);
// This is a recording-lifetime owner-thread policy. It is never reset by an
// epoch transition. Only Activate writes FP controls; Check never repairs them.
class Monitor {
    mutable std::mutex mutex_;
    DWORD owner_=0;
    bool attempted_=false,active_=false,closed_=false;
    uint64_t activations_=0,wrongThreads_=0,rawFrames_=0;
    std::array<uint64_t,size_t(Phase::Count)> checks_{};
    Json activation_=nullptr,fault_=nullptr;
    Sample last_;
    bool CheckLocked(Phase phase,const Sample& sample,bool required);
    void Fault(const char* reason,const char* phase,const Sample& sample);
public:
    explicit Monitor(DWORD owner):owner_(owner){}
    Json Activate(int ui,uintptr_t board,const Json& context);
    bool Check(Phase phase,bool required=false);
    // The same raw sample supplies captured state and its boundary sidecar.
    Sample Capture();
    Json Boundary(const Json& envelope,const Sample& sample);
    Json Evidence()const;
    Json Close();
private:
    Json HealthLocked()const;
};
}
