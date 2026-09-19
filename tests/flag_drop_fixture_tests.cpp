#include "tests/flag_drop_live.hpp"
#include "runtime/engine_call.hpp"
#include <iostream>
#include <windows.h>
using namespace lvz::flagfixture;
void Check(bool value,const char* message){if(!value)throw std::runtime_error(message);}
template<class F>void Reject(F f){try{f();}catch(const std::exception&){return;}throw std::runtime_error("Bad fixture callback accepted");}
volatile uintptr_t receivedOwner=0;
volatile uint32_t receivedFlags=1,calls=0;
__attribute__((noinline)) void __stdcall Callback(void* owner,uint32_t flags){receivedOwner=reinterpret_cast<uintptr_t>(owner);receivedFlags=flags;calls=calls+1;}
int main(){try{
    Json inactive={{"active_call_id",nullptr}};
    Check(ControlledCall(inactive,false,1,2)==0,"Startup callback armed fixture");
    lvz::runtime::EngineCallTracker tracker;
    Json entered;
    for(uint64_t id=1;id<=3;++id){
        Check(tracker.Reserve()==id,"Unexpected actual tracker token");
        Reject([&]{ControlledCall(tracker.Health(),true,7,7);});
        tracker.Enter(id);
        Check(tracker.Health()["healthy"]==false,"Actual in-flight health unexpectedly idle");
        Check(ControlledCall(tracker.Health(),true,7,7)==id,"Actual entered callback rejected");
        if(id==1)entered=tracker.Health();
        tracker.Returned(id);Reject([&]{ControlledCall(tracker.Health(),true,7,7);});
        tracker.Complete(id,true,true,false,false);
        Check(ControlledCall(tracker.Health(),true,7,7)==0,"Idle tracker triggered fixture");
    }
    Reject([&]{ControlledCall(entered,false,7,7);});
    Reject([&]{ControlledCall(entered,true,7,8);});
    Reject([&]{ControlledCall(entered,true,0,0);});
    auto bad=entered;bad["returned_calls"]=1;Reject([&]{ControlledCall(bad,true,7,7);});
    bad=entered;bad["entered_calls"]=2;Reject([&]{ControlledCall(bad,true,7,7);});
    bad=entered;bad["active_call_id"]=true;Reject([&]{ControlledCall(bad,true,7,7);});
    const auto nested=tracker.Reserve();tracker.Enter(nested);Check(!tracker.GuardEntry(),"Nested actual callback allowed");
    Reject([&]{ControlledCall(tracker.Health(),true,7,7);});
    uintptr_t stackBefore=0,stackAfter=0;
    asm volatile("movl %%esp,%0":"=r"(stackBefore));
    for(unsigned i=0;i<100;++i)InvokeDropHead(&Callback,reinterpret_cast<void*>(0x12345678u));
    asm volatile("movl %%esp,%0":"=r"(stackAfter));
    Check(stackBefore==stackAfter&&receivedOwner==0x12345678u&&receivedFlags==0&&calls==100,
        "DropHead stdcall owner/flags/ret8 ABI mismatch");
    auto manifest=Manifest();Check(manifest["test_fixture"]==true&&manifest["production_readiness_allowed"]==false
        &&manifest["fair_strategy_evidence"]==false&&manifest["drop_head_engine_call_id"]==2,"Fixture identity is not explicit");
    std::cout<<"Flag fixture inactive/active callback guards and actual stdcall ABI passed\n";return 0;
}catch(const std::exception& error){std::cerr<<error.what()<<'\n';return 1;}}
