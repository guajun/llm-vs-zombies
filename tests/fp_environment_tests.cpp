#include "determinism/fp_environment.hpp"
#include "runtime/controller.hpp"
#include "runtime/fp_guard.hpp"
#include "runtime/pump_guard.hpp"
#include <iostream>
#include <thread>
using namespace lvz::determinism::fpenv;
using namespace lvz::runtime;
void Require(bool ok,const char* reason){if(!ok)throw std::runtime_error(reason);}
void Controls(uint16_t cw,uint32_t mxcsr){__asm__ volatile("fldcw %0"::"m"(cw));__asm__ volatile("ldmxcsr %0"::"m"(mxcsr));}
struct Restore {
    alignas(16) unsigned char state[512];
    Restore(){__asm__ volatile("fxsave %0":"=m"(state));}
    ~Restore(){__asm__ volatile("fxrstor %0"::"m"(state));}
};
Json Context(){return {{"request_id","initialize"},{"version",{{"epoch",1},{"tick",0},{"revision",0}}}};}
void Activation() {
    for(uint16_t start:{uint16_t(0x007f),uint16_t(0x027f)}) {
        Restore restore;
        Controls(start,0x1fa1); // Invalid + precision sticky bits are evidence.
        __asm__ volatile("fld1");
        const auto before=Read();
        Monitor monitor(GetCurrentThreadId());
        auto receipt=monitor.Activate(1,0,Context());
        Require(receipt.at("ok")&&receipt.at("before")==before.Value(),"activation did not retain actual original sample");
        Require(receipt.at("after").at("x87_control")==639&&receipt.at("after").at("mxcsr")==0x1fa1,"target or sticky bits wrong");
        Require(receipt.at("after").at("x87_status")==before.status,"activation cleared x87 status/TOP");
        double top=0;__asm__ volatile("fstpl %0":"=m"(top));
        Require(top==1.0,"activation destroyed x87 data stack");
        Require(monitor.Check(Phase::Initialization,true),"asynchronous initialization check failed");
        auto sample=monitor.Capture();
        Json envelope={{"seq",5},{"kind","pre_step"},{"version",Context().at("version")},
            {"payload",{{"engine_call",{{"engine_call_id",1}}}}}};
        auto raw=monitor.Boundary(envelope,sample);
        Require(raw.at("payload").at("mxcsr")==sample.mxcsr&&raw.at("seq")==5&&raw.at("engine_call_id")==1,"raw evidence lost same sample/context");
        bool duplicate=false;try{monitor.Activate(1,0,Context());}catch(...){duplicate=true;}
        Require(duplicate&&monitor.Evidence().at("activation")==receipt,"activation repeated or overwrote origin");
        const auto closed=monitor.Close();
        Require(closed.at("healthy")&&closed.at("closed")&&closed.at("checks").at("close")==1,"healthy close not real");
        Require(monitor.Close()==closed,"close is not idempotent");
    }
}
void InvalidAndDrift() {
    Restore restore;
    for(auto phase:{Phase::Loop,Phase::Initialization,Phase::Ready,Phase::BeforeUpdate,Phase::AfterUpdate,
                   Phase::BeforeWarm,Phase::AfterWarm,Phase::BeforeDraw,Phase::AfterDraw}) {
        Controls(0x27f,0x1f80);Monitor monitor(GetCurrentThreadId());monitor.Activate(1,0,Context());
        Controls(0x7f,0x1f80);
        Require(!monitor.Check(phase,true)&&Read().x87==0x7f,"drift was silently repaired");
        auto fault=monitor.Evidence().at("health").at("first_fault");
        Controls(0x27f,0x1f80);
        Require(!monitor.Check(phase,true),"restoring controls erased the recorded fault");
        auto h=monitor.Close();Require(!h.at("healthy").get<bool>()&&h.at("closed")&&h.at("first_fault")==fault,"fault close lost first evidence");
    }
    Controls(0x27f,0x1f80);Monitor mx(GetCurrentThreadId());mx.Activate(1,0,Context());
    Controls(0x27f,0x9f80);Require(!mx.Check(Phase::Loop,true)&&Read().mxcsr==0x9f80,"FTZ control drift was ignored or repaired");
    Controls(0x7f,0x1f80);Monitor late(GetCurrentThreadId());
    auto rejected=late.Activate(3,0x1234,Context());Require(!rejected.at("ok").get<bool>()&&!rejected.at("write_attempted").get<bool>()&&Read().x87==0x7f,"late activation wrote controls");
    Require(!late.Close().at("healthy").get<bool>(),"late activation became healthy at close");
    Monitor unused(GetCurrentThreadId());Require(!unused.Close().at("healthy").get<bool>(),"missing activation passed close");
    Monitor wrong(GetCurrentThreadId());Json other;
    std::thread thread([&]{Restore own;Controls(0x7f,0x1f80);other=wrong.Activate(1,0,Context());Require(Read().x87==0x7f,"wrong-thread activation wrote FP");});thread.join();
    Require(!other.at("ok").get<bool>()&&wrong.Close().at("wrong_thread_checks")==1,"wrong owner not rejected");
}
struct Engine:Backend {
    Monitor monitor{GetCurrentThreadId()};int clock=100;bool closed=false;
    Json closeHealth;std::vector<std::string> kinds;
    Engine(){monitor.Activate(1,0,Context());}
    bool Ready()const override{return true;}
    uintptr_t BoardIdentity()const override{return 0x123400;}
    int NativeTick()const override{return clock;}
    bool UsesEngineCallBoundary()const override{return true;}
    Json Observe()override{return {{"game_clock",clock},{"wave",1}};}
    Json Hello()override{return Json::object();}
    Json Execute(const Json&)override{return {{"ok",true}};}
    Json AuditSnapshot()override{return {{"fp_environment",Read().Value()}};}
    Json FloatingPointEvidence()override{return monitor.Evidence();}
    void Audit(const std::string& kind,const Json&,const Json&)override{kinds.push_back(kind);}
    void CloseRecording()override{closed=true;closeHealth=monitor.Close();}
};
Json Request(Controller& c,const char* id,const char* method,Json params=Json::object()){
    return {{"protocol",1},{"request_id",id},{"method",method},{"params",params},{"expect",c.Version()}};
}
Json Immediate(Controller& c,const Json& req){Json result;c.Request(req,[&](Json value){result=std::move(value);});Require(!result.is_null(),"missing immediate reply");return result;}
void ReturnedWorkAndFaultPump() {
    Restore restore;Controls(0x27f,0x1f80);Engine engine;Controller controller(engine);controller.Boundary();
    Json reply;controller.Request(Request(controller,"step","advance",{{"max_ticks",2}}),[&](Json v){reply=std::move(v);});
    int actualCalls=0;
    controller.RunEngineFrame([&]{
        ++actualCalls;++engine.clock;Controls(0x7f,0x1f80);
        CheckReturnedFloatingPoint(controller,[&]{if(!engine.monitor.Check(Phase::AfterUpdate,true))throw std::runtime_error("actual post-call FP drift");});
        Require(reply.is_null(),"returned FP fault delivered result before original callback return");
    });
    auto calls=controller.EngineCallHealth();
    Require(actualCalls==1&&calls.at("entered_calls")==1&&calls.at("returned_calls")==1&&calls.at("aborted_calls")==0,"drift hid or fabricated actual return");
    Require(reply.at("error").at("details").at("executed_ticks")==1&&reply.at("error").at("details").at("executed_engine_calls")==1,"fault discarded measured work");
    Require(!controller.ShouldStep()&&Read().x87==0x7f,"fault mode advanced or repaired");
    int drained=0;
    auto check=[&]{if(!engine.monitor.Check(Phase::Loop))throw std::runtime_error("persistent FP fault");};
    Require(!CheckPumpGate(controller,check,[&]{
        ++drained;
        Require(Immediate(controller,Request(controller,"status","status")).at("result").at("state")=="audit_failed","fault status unavailable");
        Require(Immediate(controller,Request(controller,"pause","pause")).at("ok"),"fault pause unavailable");
        Require(Immediate(controller,Request(controller,"close","stop_recording")).at("ok"),"fault evidence close unavailable");
    }),"fault gate reopened work");
    Require(drained==1&&engine.closed&&!engine.closeHealth.at("healthy").get<bool>()&&engine.closeHealth.at("closed"),"fault did not seal unhealthy evidence");
}
int main(){try{Activation();InvalidAndDrift();ReturnedWorkAndFaultPump();std::cout<<"fixed FP activation, raw status/stack, owner/drift guards and actual callback fault closure passed\n";return 0;}
catch(const std::exception& error){std::cerr<<error.what()<<'\n';return 1;}}
