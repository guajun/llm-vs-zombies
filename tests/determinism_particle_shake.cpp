#include "determinism/particle_shake.hpp"
#include <Windows.h>
#include <array>
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <thread>

extern "C" {
alignas(16) uint8_t particleFixture[0xa0]{},relocatedFixture[0xa0]{},appFixture[0x824]{},holderFixture[0x54]{};
uint32_t emitterFixture[2]{},systemFixture[3]{},effectFixture[4]{};
uint32_t fixtureAppPointer=0,fixtureParticlePointer=0,fixtureCrt=0,fixtureSrandCalls=0;
uint32_t fixtureSeedCorruption=0;
alignas(16) uint8_t fixtureResult[560]{},fixtureCallerFx[512]{};
alignas(16) uint32_t fixtureXmm[4]={0x12345678,0x90abcdef,0xfedcba09,0x87654321};
void ParticlePreviousCall();void ParticleCurrentCall();
__attribute__((naked)) void OriginalFixtureSrand() {
    __asm__ volatile("pushfl\n\tpushl %eax\n\tmovl 12(%esp),%eax\n\tmovl %eax,_fixtureCrt\n\tincl _fixtureSrandCalls\n\tpopl %eax\n\tpopfl\n\tretl\n\t");
}
__attribute__((naked)) void ParticleCalls() {
    __asm__ volatile(
        "pushl %esi\n\tmovl _fixtureParticlePointer,%esi\n\t"
        "movl 8(%esi),%eax\n\tsubl $1,%eax\n\tcmpl $-1,%eax\n\tjne 1f\n\t"
        "movl 4(%esi),%eax\n\tsubl $1,%eax\n\t1:\n\timull %esi,%eax\n\txorl _fixtureSeedCorruption,%eax\n\tpushl %eax\n\t"
        ".globl _ParticlePreviousCall\n\t_ParticlePreviousCall:\n\tcall _OriginalFixtureSrand\n\taddl $4,%esp\n\t"
        "movl 8(%esi),%eax\n\timull %esi,%eax\n\txorl _fixtureSeedCorruption,%eax\n\tpushl %eax\n\t"
        ".globl _ParticleCurrentCall\n\t_ParticleCurrentCall:\n\tcall _OriginalFixtureSrand\n\taddl $4,%esp\n\t"
        "popl %esi\n\tretl\n\t");
}
__attribute__((naked)) void CallFixture() {
    __asm__ volatile(
        "pushfl\n\tpushal\n\tfxsave _fixtureCallerFx\n\tfninit\n\tfldpi\n\tfld1\n\t"
        "movdqu _fixtureXmm,%xmm0\n\tmovdqu _fixtureXmm,%xmm1\n\tmovdqu _fixtureXmm,%xmm2\n\tmovdqu _fixtureXmm,%xmm3\n\t"
        "movdqu _fixtureXmm,%xmm4\n\tmovdqu _fixtureXmm,%xmm5\n\tmovdqu _fixtureXmm,%xmm6\n\tmovdqu _fixtureXmm,%xmm7\n\t"
        "movl $0x11111111,%ebx\n\tmovl $0x22222222,%ebp\n\tmovl $0x33333333,%ecx\n\t"
        "movl $0x44444444,%edx\n\tmovl $0x55555555,%esi\n\tmovl $0x66666666,%edi\n\t"
        "pushl $0x247\n\tpopfl\n\tcall _ParticleCalls\n\t"
        "pushfl\n\tpushal\n\tcld\n\tmovl %esp,%esi\n\tmovl $_fixtureResult,%edi\n\tmovl $9,%ecx\n\trep movsl\n\t"
        "fxsave _fixtureResult+48\n\tpopal\n\tpopfl\n\tfxrstor _fixtureCallerFx\n\tpopal\n\tpopfl\n\tretl\n\t");
}
}
namespace {
void Check(bool value,const char* message) {if(!value)throw std::runtime_error(message);}
void Put(uint8_t* buffer,size_t offset,uint32_t value) {std::memcpy(buffer+offset,&value,4);}
uint32_t Ptr(const void* pointer) {return reinterpret_cast<uintptr_t>(pointer);}
void Setup(uint8_t* particle=particleFixture,int age=1) {
    std::memset(particle,0,0xa0);Put(particle,0,Ptr(emitterFixture));Put(particle,4,5);Put(particle,8,age);Put(particle,0x9c,0x3e90000);
    fixtureAppPointer=Ptr(appFixture);fixtureParticlePointer=Ptr(particle);
    Put(appFixture,0x820,Ptr(effectFixture));effectFixture[0]=Ptr(holderFixture);
    emitterFixture[1]=Ptr(systemFixture);systemFixture[2]=Ptr(holderFixture);
    const uint32_t pool[]={Ptr(particle),1,1,1,1,1002,0};std::memcpy(holderFixture+0x38,pool,sizeof(pool));
    fixtureCrt=fixtureSrandCalls=fixtureSeedCorruption=0;
}
void Install() {
    std::string error;
    Check(lvz::determinism::InstallParticleShakeHookForTest(Ptr(reinterpret_cast<void*>(&ParticlePreviousCall)),
        Ptr(reinterpret_cast<void*>(&ParticleCurrentCall)),Ptr(reinterpret_cast<void*>(&OriginalFixtureSrand)),Ptr(&fixtureAppPointer),error),error.c_str());
}
void Remove() {std::string error;Check(lvz::determinism::RemoveParticleShakeHook(error),error.c_str());}
}
int main() {using namespace lvz::determinism;try {
    std::string error;Check(!ValidateParticleShakeTarget(),"fixture unexpectedly identified as PvZ");
    Check(!InstallParticleShakeHookForTest(0,0,0,0,error),"invalid target accepted");
    Setup();CallFixture();std::array<uint8_t,560> baseline{};std::memcpy(baseline.data(),fixtureResult,baseline.size());
    Install();SetParticleShakeBoundary(10,2,3,"pre_step",0x100000001ull);Setup();SetLastError(1234);CallFixture();
    Check(GetLastError()==1234,"hook leaked Win32 LastError");
    for(size_t n=0;n<36;++n)if(n<12||n>=16)Check(fixtureResult[n]==baseline[n],"hook changed GPR/flags");
    for(size_t n=48;n<560;++n)Check(fixtureResult[n]==baseline[n],"hook changed x87/XMM state");
    Check(fixtureSrandCalls==2&&fixtureCrt==0x3e90000,"original srand did not receive canonical seed");
    auto events=DrainParticleShakeEvents();Check(events.size()==2,"missing seed events");
    for(const auto& event:events)Check(event["semantic"]["engine_call_id"]==0x100000001ull&&event["raw"]["engine_call_id"]==0x100000001ull,"Particle/raw lost 64-bit call association");
    Check(events[0]["semantic"]["factor"]==0&&events[1]["semantic"]["factor"]==1,"age factor incorrect");
    Check(events[1]["raw"]["original_seed"]==Ptr(particleFixture),"raw seed evidence lost");
    const auto canonical=fixtureCrt;
    Setup(relocatedFixture);CallFixture();Check(fixtureCrt==canonical,"heap relocation changed canonical seed");
    events=DrainParticleShakeEvents();Check(events[1]["raw"]["particle_address"]==Ptr(relocatedFixture),"relocation evidence lost");
    Check(events[1]["semantic"]["particle_id"]==0x3e90000,"full generation ID lost");
    Setup();Put(particleFixture,0x9c,0x3ea0000);Put(holderFixture,0x38+20,1003);CallFixture();events=DrainParticleShakeEvents();
    Check(fixtureCrt==0x3ea0000&&events[1]["semantic"]["generation"]==1002,"same-slot different generation was normalized away");
    Setup(particleFixture,0);CallFixture();events=DrainParticleShakeEvents();
    Check(events[0]["semantic"]["factor"]==4&&events[1]["semantic"]["factor"]==0,"duration wrap factor incorrect");
    Setup(particleFixture,5);Put(particleFixture,0x38,2);CallFixture();events=DrainParticleShakeEvents();
    Check(events.size()==2&&events[1]["semantic"]["crossfade_duration"]==2,"legitimate first crossfade frame rejected");
    Check(ParticleShakeSnapshot()["controlled_calls"]==10,"controlled count incorrect");
    ClearParticleShakeBoundary();Setup();CallFixture();events=DrainParticleShakeEvents();
    Check(events[0]["semantic"]["engine_call_id"].is_null()&&events[0]["raw"]["engine_call_id"].is_null(),"Initialization particle inherited prior call ID");
    Remove();Setup();CallFixture();Check(fixtureCrt==Ptr(particleFixture),"uninstall failed to restore original seed behavior");

    Install();Setup();Put(particleFixture,0x9c,0x3e90001);CallFixture();
    Check(ParticleShakeStatus()["faults"]==2&&ParticleShakeStatus()["healthy"]==false,"wrong pool ID did not fail closed");
    Check(DrainParticleShakeEvents().empty()&&fixtureCrt==Ptr(particleFixture),"invalid pool was translated");Remove();
    Install();Setup();fixtureSeedCorruption=1;CallFixture();
    Check(ParticleShakeStatus()["faults"]==2&&ParticleShakeStatus()["last_fault_code"]==8,"wrong caller seed was translated");
    Check(DrainParticleShakeEvents().empty()&&fixtureCrt==(Ptr(particleFixture)^1),"bad seed did not preserve original call");Remove();
    Install();Setup();systemFixture[2]=Ptr(particleFixture);CallFixture();
    Check(ParticleShakeStatus()["healthy"]==false,"foreign holder was accepted");Remove();
    Install();Setup();std::thread wrong([]{CallFixture();});wrong.join();
    Check(ParticleShakeStatus()["wrong_thread_calls"]==2&&ParticleShakeStatus()["healthy"]==false,"wrong thread accepted");Remove();
    Install();Setup();for(int n=0;n<4097;++n)CallFixture();
    Check(ParticleShakeStatus()["captured"]==8192&&ParticleShakeStatus()["overflow"]==2,"overflow was not bounded/fail closed");
    Check(DrainParticleShakeEvents().size()==8192,"overflow corrupted queue");
    auto* site=reinterpret_cast<uint8_t*>(&ParticlePreviousCall);DWORD old=0;
    Check(VirtualProtect(site,5,PAGE_EXECUTE_READWRITE,&old)!=0,"cannot test foreign ownership");
    site[4]^=1;Check(!RemoveParticleShakeHook(error),"foreign callsite overwritten");site[4]^=1;
    DWORD ignored=0;VirtualProtect(site,5,old,&ignored);FlushInstructionCache(GetCurrentProcess(),site,5);Remove();
    std::cout<<"particle shake: signature refusal, full pool ID validation, relocation, age wrap, original srand, ABI/FX, thread/overflow/ownership checks passed\n";
    return 0;
}catch(const std::exception& error){std::cerr<<error.what()<<'\n';std::string ignored;RemoveParticleShakeHook(ignored);return 1;}}
