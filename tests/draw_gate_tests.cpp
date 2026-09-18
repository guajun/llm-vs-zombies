#include "recording/draw_gate.hpp"
#include "recording/frame_cache.hpp"
#include <windows.h>
#include <iostream>
#include <stdexcept>
#include <thread>
using namespace lvz::recording;
void Check(bool value,const char* message){if(!value)throw std::runtime_error(message);}
template<class F>void Reject(F fn){try{fn();}catch(const std::exception&){return;}throw std::runtime_error("Expected rejection");}
bool ready=false,nested=false;
bool Ready(){return ready;}
extern "C" void FixtureBody(int* counter) {
    ++*counter;
    if(nested) {Reject([&]{DrawControlled(counter);});}
}
extern "C" __attribute__((naked)) bool __stdcall Fixture(void*) {
    __asm__ volatile("pushl %ebp\n\tmovl %esp,%ebp\n\tnop\n\tnop\n\tpushl 8(%ebp)\n\tcall _FixtureBody\n\taddl $4,%esp\n\tmovl $1,%eax\n\tpopl %ebp\n\tret $4\n\t");
}
int main(){try{
    std::string error;int calls=0;const auto at=reinterpret_cast<uintptr_t>(&Fixture);
    Check(!InstallDrawGateForTest(at+1,GetCurrentThreadId(),&Ready,error),"mismatched signature accepted");
    Check(InstallDrawGateForTest(at,GetCurrentThreadId(),&Ready,error),error.c_str());
    auto invoke=reinterpret_cast<bool(__stdcall*)(void*)>(at);
    uintptr_t before=0,after=0;asm volatile("movl %%esp,%0":"=r"(before));
    Check(invoke(&calls)&&calls==1,"initialization drawing did not pass through");
    asm volatile("movl %%esp,%0":"=r"(after));Check(before==after,"stdcall stack cleanup changed");
    ready=true;Check(!invoke(&calls)&&calls==1,"first ready automatic draw escaped gate");
    ready=false;Check(!invoke(&calls)&&calls==1,"latched gate reopened on scene change");
    ready=true;Check(DrawControlled(&calls)&&calls==2,"controlled original drawing not executed once");
    RecordDrawnFrame(true);
    Check(DrawScheduleSnapshot()["warm_frames"]==1&&DrawGateStatus()["controlled_calls"]==1,"warm count differs");
    SealDrawGate();Check(!invoke(&calls)&&calls==2,"sealed gate reopened");
    Reject([&]{DrawControlled(&calls);});
    Check(RemoveDrawGateForTest(error),error.c_str());
    Check(InstallDrawGateForTest(at,GetCurrentThreadId(),&Ready,error),error.c_str());
    nested=true;Reject([&]{DrawControlled(&calls);});nested=false;
    Check(DrawGateStatus()["faults"]!=0,"nested drawing not faulted");
    Check(RemoveDrawGateForTest(error),error.c_str());
    Check(InstallDrawGateForTest(at,GetCurrentThreadId(),&Ready,error),error.c_str());
    std::thread worker([&]{Check(!invoke(&calls),"wrong-thread draw ran");});worker.join();
    Reject([]{CheckDrawGate();});Check(DrawGateStatus()["wrong_thread_calls"]==1,"wrong thread evidence missing");
    Check(RemoveDrawGateForTest(error),error.c_str());

    FrameCache cache;const nlohmann::json version={{"epoch",1},{"tick",8},{"revision",0}};
    CaptureResult frame;frame.ok=true;frame.width=800;frame.height=600;frame.rowStride=2400;frame.pixels.resize(1440000,42);
    cache.Store(version,11,3159,std::move(frame));
    const auto* current=cache.Find(version,11,3159);Check(current&&current->pixels[0]==42,"frame pixels not cached");
    auto stale=version;stale["revision"]=1;
    Check(!cache.Find(stale,11,3159)&&!cache.Find(version,12,3159)&&!cache.Find(version,11,3160),"stale frame relabelled");
    Check(cache.Find(version,11,3159)==current,"repeated capture replaced CPU-owned pixels");
    cache.Invalidate("action_attempt");Check(!cache.Find(version,11,3159),"action invalidation ignored");
    Reject([&]{cache.Store(version,11,3159,CaptureResult{});});
    std::cout<<"draw gate real MinHook/stdcall, readiness latch, closure, thread/nesting and CPU frame cache passed\n";
    return 0;
}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
