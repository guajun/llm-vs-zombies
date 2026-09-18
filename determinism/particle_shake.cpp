#include "particle_shake.hpp"
#include "memory.hpp"
#include "model.hpp"
#include <array>
#include <atomic>
#include <cstring>
#include <stdexcept>

extern "C" {
void* lvzParticleSrandTarget=nullptr;
void __cdecl LvzParticleSeed(void* frame) noexcept;
__attribute__((naked)) void LvzParticleSrandShim() {
    __asm__ volatile(
        "pushfl\n\tpushal\n\tmovl %esp, %eax\n\t"
        "subl $528, %esp\n\tandl $-16, %esp\n\t"
        "movl %eax, 512(%esp)\n\tfxsave (%esp)\n\t"
        "cld\n\tsubl $12, %esp\n\tpushl %eax\n\tcall _LvzParticleSeed\n\taddl $16, %esp\n\t"
        "fxrstor (%esp)\n\tmovl 512(%esp), %esp\n\t"
        "popal\n\tpopfl\n\tjmp *_lvzParticleSrandTarget\n\t");
}
}
namespace lvz::determinism {
namespace {
constexpr uintptr_t kPreviousCall=0x516b3c,kCurrentCall=0x516ba5,kSrand=0x61e07a,kApp=0x6a9ec0;
constexpr size_t kCapacity=8192;
struct Registers {uint32_t edi,esi,ebp,espBeforePushad,ebx,edx,ecx,eax,eflags;};
static_assert(sizeof(Registers)==36);
struct Boundary {uint64_t tick=0,revision=0;uint32_t epoch=0,phase=0;};
struct Record {
    Boundary boundary{};
    uint64_t ordinal=0;
    uint32_t site=0,particle=0,emitter=0,system=0,holder=0;
    uint32_t id=0,slot=0,original=0,canonical=0,factor=0;
    int32_t age=0,duration=0,crossfade=0;
    std::array<uint32_t,7> pool{};
};
std::array<Record,kCapacity> records;
size_t queued=0;
uint64_t captured=0,controlled=0,digest=14695981039346656037ULL;
std::atomic<uint64_t> wrongThread{0},faults{0},overflow{0};
std::atomic<uint32_t> lastFault{0};
DWORD owner=0;
bool installed=false;
Boundary boundary;
uintptr_t sites[2]{},originalFunction=0,appPointer=0;
std::array<uint8_t,5> originals[2]{},patches[2]{};

bool Copy(uintptr_t address,void* target,size_t count) noexcept {
    if(!Accessible(address,count))return false;
    std::memcpy(target,reinterpret_cast<const void*>(address),count);return true;
}
template<class T>bool Copy(uintptr_t address,T& target) noexcept {return Copy(address,&target,sizeof(target));}
template<size_t N>bool Match(uintptr_t address,const uint8_t (&bytes)[N]) noexcept {
    return Accessible(address,N)&&!std::memcmp(reinterpret_cast<const void*>(address),bytes,N);
}
bool Same(uintptr_t address,const std::array<uint8_t,5>& bytes) noexcept {
    std::array<uint8_t,5> actual{};return Copy(address,actual)&&actual==bytes;
}
std::array<uint8_t,5> Call(uintptr_t site,uintptr_t target) {
    std::array<uint8_t,5> bytes{0xe8};uint32_t displacement=uint32_t(target-site-5);
    std::memcpy(bytes.data()+1,&displacement,4);return bytes;
}
bool WriteCall(uintptr_t site,const std::array<uint8_t,5>& bytes) noexcept {
    DWORD protection=0;if(!VirtualProtect(reinterpret_cast<void*>(site),5,PAGE_EXECUTE_READWRITE,&protection))return false;
    std::memcpy(reinterpret_cast<void*>(site),bytes.data(),5);
    const bool flushed=FlushInstructionCache(GetCurrentProcess(),reinterpret_cast<void*>(site),5)!=0;
    DWORD ignored=0;return VirtualProtect(reinterpret_cast<void*>(site),5,protection,&ignored)&&flushed;
}
void RequireOwner() {if(!installed||GetCurrentThreadId()!=owner)throw std::runtime_error("Particle shake hook requires installed game-thread owner");}
void Fault(uint32_t code) noexcept {++faults;lastFault=code;}
void HashWord(uint64_t value) noexcept {
    for(unsigned byte=0;byte<8;++byte) {digest^=uint8_t(value);digest*=1099511628211ULL;value>>=8;}
}
void Intercept(Registers* frame) noexcept {
    if(GetCurrentThreadId()!=owner) {++wrongThread;return;}
    if(!installed) {Fault(1);return;}
    if(queued==kCapacity) {++overflow;return;}
    auto* args=reinterpret_cast<uint32_t*>(frame+1);
    const uintptr_t site=uintptr_t(args[0])-5;
    if(site!=sites[0]&&site!=sites[1]) {Fault(2);return;}
    Record item;item.boundary=boundary;item.particle=frame->esi;
    item.site=site==sites[0]?uint32_t(kPreviousCall-0x400000):uint32_t(kCurrentCall-0x400000);
    item.original=args[1];
    std::array<uint32_t,0xa0/4> particle{};
    if(!Copy(item.particle,particle)) {Fault(3);return;}
    item.emitter=particle[0];item.duration=static_cast<int32_t>(particle[1]);item.age=static_cast<int32_t>(particle[2]);item.id=particle[0x9c/4];
    item.crossfade=static_cast<int32_t>(particle[0x38/4]);
    uint32_t app=0,effects=0,actualHolder=0;
    if(!Copy(item.emitter+4,item.system)||!item.system||!Copy(item.system+8,item.holder)||!item.holder
        ||!Copy(appPointer,app)||!app||!Copy(app+0x820,effects)||!effects||!Copy(effects,actualHolder)
        ||actualHolder!=item.holder||!Copy(item.holder+0x38,item.pool)) {Fault(4);return;}
    const auto block=item.pool[0],used=item.pool[1],capacity=item.pool[2],count=item.pool[4];
    if(!block||!capacity||capacity>1024||used>capacity||count>used||!count
        ||item.pool[3]>used||!item.pool[5]||item.pool[5]>65535||item.particle<block
        ||(item.particle-block)%0xa0) {Fault(5);return;}
    item.slot=(item.particle-block)/0xa0;
    if(item.slot>=used||!(item.id&0xffff0000u)||(item.id&0xffffu)!=item.slot) {Fault(6);return;}
    // On the first successful CrossFadeParticleToName frame the engine keeps
    // age >= duration and sets crossfade duration; later frames clamp the age.
    if(item.duration<1||item.age<0||item.crossfade<0||(item.age>=item.duration&&item.crossfade==0)) {Fault(7);return;}
    item.factor=site==sites[0]?uint32_t(item.age?item.age-1:item.duration-1):uint32_t(item.age);
    if(item.original!=uint32_t(item.factor*item.particle)) {Fault(8);return;}
    item.canonical=uint32_t(item.factor*item.id);item.ordinal=captured;
    records[queued++]=item;++captured;
    if(boundary.phase) {
        ++controlled;
        HashWord(item.site);HashWord(item.id);HashWord(item.factor);HashWord(item.canonical);
        HashWord(uint32_t(item.age));HashWord(uint32_t(item.duration));HashWord(uint32_t(item.crossfade));
        for(size_t n=1;n<6;++n)HashWord(item.pool[n]);
    }
    // Only the seed argument is changed. The original CRT routine executes
    // after every GPR/flag/x87/XMM register has been restored by the shim.
    args[1]=item.canonical;
}
bool Install(uintptr_t previousCall,uintptr_t currentCall,uintptr_t srandFunction,uintptr_t app,
             bool production,std::string& error) {
    if(installed) {error="Particle shake hook already installed";return false;}
    if(sizeof(void*)!=4||!app||!Same(previousCall,Call(previousCall,srandFunction))||!Same(currentCall,Call(currentCall,srandFunction))
        ||(production&&!ValidateParticleShakeTarget())) {error="Particle shake callsite/target signatures do not match";return false;}
    sites[0]=previousCall;sites[1]=currentCall;originalFunction=srandFunction;appPointer=app;
    owner=GetCurrentThreadId();queued=0;captured=controlled=0;digest=14695981039346656037ULL;
    wrongThread=0;faults=0;overflow=0;lastFault=0;boundary={};
    lvzParticleSrandTarget=reinterpret_cast<void*>(srandFunction);
    for(size_t n=0;n<2;++n) {originals[n]=Call(sites[n],srandFunction);patches[n]=Call(sites[n],reinterpret_cast<uintptr_t>(&LvzParticleSrandShim));}
    installed=true;
    if(!WriteCall(sites[0],patches[0])||!WriteCall(sites[1],patches[1])) {
        bool restored=true;for(size_t n=0;n<2;++n)if(Same(sites[n],patches[n]))restored=WriteCall(sites[n],originals[n])&&restored;
        if(restored) {installed=false;lvzParticleSrandTarget=nullptr;}
        error=restored?"Cannot install both particle seed callsites":"Particle seed patch rollback failed; keep DLL loaded";return false;
    }
    error.clear();return true;
}
}

bool ValidateParticleShakeTarget() noexcept {
    if(sizeof(void*)!=4||reinterpret_cast<uintptr_t>(GetModuleHandleW(nullptr))!=0x400000)return false;
    static constexpr uint8_t previous[]={0x8b,0x46,0x08,0x83,0xe8,0x01,0x83,0xc4,0x0c,0x83,0xf8,0xff,0x75,0x06,0x8b,0x46,0x04,0x83,0xe8,0x01,0x0f,0xaf,0xc6,0x50};
    static constexpr uint8_t current[]={0x8b,0x46,0x08,0x0f,0xaf,0xc6,0xdc,0x0d,0xe0,0x96,0x67,0,0xdc,0xc0,0xdc,0x25,0xf8,0xb8,0x65,0,0x50};
    static constexpr uint8_t allocator[]={0x8d,0x3c,0x9b,0xc1,0xe7,0x05,0x03,0x3e,0x68,0x9c,0,0,0};
    static constexpr uint8_t idStore[]={0x8b,0x46,0x14,0xc1,0xe0,0x10,0x0b,0xc3,0x89,0x87,0x9c,0,0,0};
    static constexpr uint8_t srandCode[]={0xe8,0xbe,0xa9,0,0,0x8b,0x4c,0x24,0x04,0x89,0x48,0x14,0xc3};
    static constexpr uint8_t poolAccess[]={0x8b,0x47,0x04,0x8b,0x48,0x08,0x8b,0x51,0x48,0x3b,0x51,0x40};
    const bool calls=installed?(Same(kPreviousCall,patches[0])&&Same(kCurrentCall,patches[1])):
        (Same(kPreviousCall,Call(kPreviousCall,kSrand))&&Same(kCurrentCall,Call(kCurrentCall,kSrand)));
    return calls&&Match(0x516b24,previous)&&Match(0x516b85,current)&&Match(0x518cdb,allocator)
        &&Match(0x518cf0,idStore)&&Match(kSrand,srandCode)&&Match(0x51616e,poolAccess);
}
bool InstallParticleShakeHook(std::string& error) {return Install(kPreviousCall,kCurrentCall,kSrand,kApp,true,error);}
#ifdef LVZ_PARTICLE_SHAKE_TESTING
bool InstallParticleShakeHookForTest(uintptr_t previousCall,uintptr_t currentCall,uintptr_t srandFunction,uintptr_t app,std::string& error) {
    return Install(previousCall,currentCall,srandFunction,app,false,error);
}
#endif
bool RemoveParticleShakeHook(std::string& error) {
    if(!installed) {error.clear();return true;}
    if(GetCurrentThreadId()!=owner) {error="Removal requires game-thread owner";return false;}
    for(size_t n=0;n<2;++n)if(!Same(sites[n],patches[n])&&!Same(sites[n],originals[n])) {
        error="Particle hook ownership changed; refusing foreign code overwrite";return false;
    }
    for(size_t n=0;n<2;++n)if(Same(sites[n],patches[n])&&!WriteCall(sites[n],originals[n])) {
        error="Particle hook removal failed; keep DLL loaded";return false;
    }
    installed=false;lvzParticleSrandTarget=nullptr;error.clear();return true;
}
void SetParticleShakeBoundary(uint64_t tick,uint64_t revision,uint32_t epoch,const std::string& phase) {
    RequireOwner();const uint32_t tag=phase=="pre_step"?1:phase=="request_started"?2:phase=="action"?3:0;
    if(!tag)throw std::runtime_error("Unsupported particle shake control phase");boundary={tick,revision,epoch,tag};
}
void ClearParticleShakeBoundary() {RequireOwner();boundary={};}
nlohmann::json DrainParticleShakeEvents() {
    RequireOwner();auto out=Json::array();
    for(size_t n=0;n<queued;++n) {
        const auto& item=records[n];Json version=nullptr;
        if(item.boundary.phase)version={{"epoch",item.boundary.epoch},{"tick",item.boundary.tick},{"revision",item.boundary.revision}};
        const char* phase=item.boundary.phase==1?"pre_step":item.boundary.phase==2?"request_started":item.boundary.phase==3?"action":"initialization";
        Json semantic={{"mode",kParticleShakeMode},{"ordinal",item.ordinal},{"callsite_rva",item.site},
            {"particle_id",item.id},{"slot",item.slot},{"generation",item.id>>16},{"age",item.age},{"duration",item.duration},
            {"crossfade_duration",item.crossfade},
            {"factor",item.factor},{"canonical_seed",item.canonical},{"pool_verified",true},{"control_phase",phase},
            {"pool",{{"used",item.pool[1]},{"capacity",item.pool[2]},{"free_head",item.pool[3]},{"count",item.pool[4]},{"next_key",item.pool[5]}}}};
        Json raw=semantic;
        raw["particle_address"]=item.particle;raw["emitter_address"]=item.emitter;raw["system_address"]=item.system;
        raw["holder_address"]=item.holder;raw["pool_block_address"]=item.pool[0];raw["original_seed"]=item.original;
        out.push_back({{"version",std::move(version)},{"semantic",std::move(semantic)},{"raw",std::move(raw)}});
    }
    queued=0;return out;
}
nlohmann::json ParticleShakeStatus() {
    if(installed)RequireOwner();
    return {{"installed",installed},{"captured",captured},{"queued",queued},{"controlled_calls",controlled},
        {"wrong_thread_calls",wrongThread.load()},{"faults",faults.load()},{"overflow",overflow.load()},{"last_fault_code",lastFault.load()},
        {"healthy",wrongThread.load()==0&&faults.load()==0&&overflow.load()==0}};
}
nlohmann::json ParticleShakeSnapshot() {
    RequireOwner();return {{"mode",kParticleShakeMode},{"controlled_calls",controlled},{"controlled_digest",digest}};
}
nlohmann::json ParticleShakeManifest() {
    return {{"mode",kParticleShakeMode},{"installed",installed},{"original_engine_bitwise_unmodified",false},
        {"semantic_change","FIELD_SHAKE pointer factor replaced by verified full particle ID; original CRT srand/rand execute"},
        {"raw_evidence","particle-shake-seeds.jsonl"},{"live_validated",false}};
}
}
extern "C" void __cdecl LvzParticleSeed(void* frame) noexcept {
    const DWORD savedError=GetLastError();lvz::determinism::Intercept(static_cast<lvz::determinism::Registers*>(frame));SetLastError(savedError);
}
