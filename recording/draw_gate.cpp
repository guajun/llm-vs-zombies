#include "draw_gate.hpp"
#include <windows.h>
#include <minhook/MinHook.h>
#include <array>
#include <atomic>
#include <cstring>
#include <stdexcept>

namespace lvz::recording {
namespace {
using Draw=bool(__stdcall*)(void*);
constexpr uintptr_t Entry=0x538eb0;
constexpr std::array<uint8_t,20> Signature={0x6a,0xff,0x68,0xee,0x78,0x64,0,0x64,0xa1,0,0,0,0,0x50,0x81,0xec,0x68,1,0,0};
Draw original=nullptr;
uintptr_t entry=0;
uint32_t owner=0;
DrawReadyPredicate ready=nullptr;
std::array<uint8_t,20> patch{};
size_t patchLength=0;
bool installed=false,latched=false,sealed=false,active=false;
uint64_t automaticAllowed=0,automaticDenied=0,controlled=0;
uint64_t warmFrames=0,stepFrames=0;
std::atomic<uint32_t> faults{0},wrongThread{0};
bool Readable(uintptr_t at,size_t n) noexcept {
    MEMORY_BASIC_INFORMATION m{};
    return at&&VirtualQuery(reinterpret_cast<void*>(at),&m,sizeof(m))&&m.State==MEM_COMMIT
        &&!(m.Protect&(PAGE_NOACCESS|PAGE_GUARD))&&at+n>=at
        &&at+n<=reinterpret_cast<uintptr_t>(m.BaseAddress)+m.RegionSize;
}
bool Match(uintptr_t at,const uint8_t* bytes,size_t n) noexcept {
    return Readable(at,n)&&!std::memcmp(reinterpret_cast<void*>(at),bytes,n);
}
bool __stdcall Gate(void* manager) {
    // Never throw through the original engine caller, including wrong-thread
    // calls. The next owning boundary checks faults and stops the experiment.
    if(GetCurrentThreadId()!=owner) {++wrongThread;++faults;return false;}
    if(active) {++faults;return false;}
    try {
        if(ready&&ready())latched=true;
        if(latched||sealed||faults.load()) {++automaticDenied;return false;}
        ++automaticAllowed;active=true;
        const bool result=original(manager);active=false;return result;
    } catch(...) {active=false;++faults;return false;}
}
bool Install(uintptr_t address,uint32_t thread,DrawReadyPredicate predicate,std::string& error,bool production) {
    if(installed||!thread||thread!=GetCurrentThreadId()||!predicate) {error="Draw gate requires a fresh owning game thread and ready predicate";return false;}
    static constexpr uint8_t fixture[]={0x55,0x89,0xe5,0x90,0x90};
    if(sizeof(void*)!=4||!Match(address,production?Signature.data():fixture,production?Signature.size():sizeof(fixture))) {
        error="DrawScreen target signature mismatch";return false;
    }
    auto status=MH_Initialize();
    if(status!=MH_OK&&status!=MH_ERROR_ALREADY_INITIALIZED) {error="Cannot initialize DrawScreen MinHook";return false;}
    status=MH_CreateHook(reinterpret_cast<void*>(address),reinterpret_cast<void*>(&Gate),reinterpret_cast<void**>(&original));
    if(status!=MH_OK) {error=std::string("Cannot create DrawScreen gate: ")+MH_StatusToString(status);return false;}
    owner=thread;ready=predicate;entry=address;latched=sealed=active=false;
    automaticAllowed=automaticDenied=controlled=warmFrames=stepFrames=0;faults=wrongThread=0;
    status=MH_EnableHook(reinterpret_cast<void*>(address));
    if(status!=MH_OK) {MH_RemoveHook(reinterpret_cast<void*>(address));original=nullptr;error="Cannot enable DrawScreen gate";return false;}
    patchLength=production?Signature.size():sizeof(fixture);
    std::memcpy(patch.data(),reinterpret_cast<void*>(address),patchLength);installed=true;error.clear();return true;
}
}
bool InstallDrawGate(uint32_t thread,DrawReadyPredicate ready,std::string& error) {return Install(Entry,thread,ready,error,true);}
bool DrawEntryVerified() noexcept {
    return installed?Match(entry,patch.data(),patchLength):Match(Entry,Signature.data(),Signature.size());
}
void CheckDrawGate() {
    if(!installed||GetCurrentThreadId()!=owner||!DrawEntryVerified()||faults.load())
        throw std::runtime_error("DrawScreen gate is unavailable, modified, off-thread or faulted");
}
bool DrawControlled(void* manager) {
    CheckDrawGate();
    if(active||sealed||!ready()) {++faults;throw std::runtime_error("Controlled drawing requires a ready, open, non-reentrant boundary");}
    latched=true;active=true;++controlled;
    try {const bool result=original(manager);active=false;CheckDrawGate();return result;}
    catch(...) {active=false;++faults;throw;}
}
void SealDrawGate() {
    if(GetCurrentThreadId()!=owner||active)throw std::runtime_error("Draw gate sealing requires idle owner thread");
    sealed=latched=true;
}
void RecordDrawnFrame(bool warm) {CheckDrawGate();if(warm)++warmFrames;else ++stepFrames;}
nlohmann::json DrawScheduleSnapshot() {return {{"mode",DrawMode},{"warm_frames",warmFrames},{"step_frames",stepFrames}};}
nlohmann::json DrawGateManifest() {
    return {{"mode",DrawMode},{"installed",installed},{"original_engine_bitwise_unmodified",false},
        {"target_entry_rva",Entry-0x400000},{"schedule","one warm draw before B0; one original draw after each verified update before post_step"},
        {"autonomous_fight_draws",false},{"capture","cached_bgr24_only"},{"rng_restore_after_draw",false},
        {"live_verified",false}};
}
nlohmann::json DrawGateStatus() {
    return {{"mode",DrawMode},{"installed",installed},{"latched",latched},{"sealed",sealed},{"active",active},
        {"automatic_allowed",automaticAllowed},{"automatic_denied",automaticDenied},{"controlled_calls",controlled},
        {"warm_frames",warmFrames},{"step_frames",stepFrames},
        {"faults",faults.load()},{"wrong_thread_calls",wrongThread.load()},
        {"healthy",installed&&DrawEntryVerified()&&!faults.load()&&!wrongThread.load()&&!active&&controlled==warmFrames+stepFrames}};
}
#ifdef LVZ_DRAW_GATE_TESTING
bool InstallDrawGateForTest(uintptr_t at,uint32_t thread,DrawReadyPredicate predicate,std::string& error) {return Install(at,thread,predicate,error,false);}
bool RemoveDrawGateForTest(std::string& error) {
    if(!installed)return true;
    if(GetCurrentThreadId()!=owner||active||!DrawEntryVerified()) {error="Draw gate ownership changed";return false;}
    if(MH_DisableHook(reinterpret_cast<void*>(entry))!=MH_OK||MH_RemoveHook(reinterpret_cast<void*>(entry))!=MH_OK) {error="Cannot remove fixture draw gate";return false;}
    installed=false;original=nullptr;return true;
}
#endif
}
