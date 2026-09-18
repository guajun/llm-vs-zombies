#include "runtime/controller.hpp"
#include "runtime/pump_guard.hpp"
#include <iostream>
#include <fstream>
#include <thread>
using namespace lvz::runtime;
void Check(bool x,const char* why){if(!x)throw std::runtime_error(why);}
struct Engine:Backend {
    int clock=100,effect=200,mj=300,ui=3,entries=0,returns=0,draws=0,mutations=0;
    uintptr_t board=0x123400;bool throwPre=false,throwDraw=false,closed=false;
    std::vector<Json> events;std::string lastKind;Json hookLabel;
    bool Ready()const override{return ui==3&&board;}
    std::function<void()> duringDraw;
    uintptr_t BoardIdentity()const override{return board;}
    int NativeTick()const override{return clock;}
    int GameUi()const override{return ui;}
    bool UsesEngineCallBoundary()const override{return true;}
    Json NativeClocks()const override{return {{"game_clock",clock},{"effect_clock",effect},{"mj_clock",mj}};}
    Json Observe()override{return {{"game_clock",clock},{"game_ui",ui},{"wave",1},{"mutations",mutations}};}
    Json Hello()override{return Json::object();}
    Json AuditSnapshot()override{return Observe();}
    Json Execute(const Json& a)override{++mutations;return {{"ok",!a.value("fail",false)}};}
    bool RequiresRenderPreparation()const override{return true;}
    Json RenderFrame(const Json& version,bool)override {
        Check(lastKind=="pre_step","draw relabelled original pre boundary");
        if(duringDraw)duringDraw();
        if(throwDraw)throw std::runtime_error("draw fixture failure");
        ++draws;return {{"frame_version",version},{"phase","step"}};
    }
    void CloseRecording()override{closed=true;}
    void Audit(const std::string& kind,const Json& p,const Json& o)override {
        if(throwPre&&kind=="pre_step")throw std::runtime_error("pre fixture failure");
        lastKind=kind;events.push_back({{"kind",kind},{"payload",p},{"version",o.at("version")},{"state",Observe()}});
        if(kind=="pre_step")hookLabel={{"engine_call_id",p.at("engine_call").at("engine_call_id")},{"version",o.at("version")}};
    }
    void Original(int delta,int nextUi=3){++entries;++mutations;clock+=delta;++effect;++mj;ui=nextUi;++returns;}
};
Json Req(Controller& c,std::string id,std::string method,Json params=Json::object()){
    return {{"protocol",1},{"request_id",id},{"method",method},{"params",params},{"expect",c.Version()}};
}
Json Now(Controller& c,const Json& req){std::optional<Json> out;c.Request(req,[&](Json r){out=r;});Check(out.has_value(),"expected immediate response");return *out;}
std::vector<Json> Of(const Engine& e,const std::string& kind){std::vector<Json> out;for(auto& v:e.events)if(v["kind"]==kind)out.push_back(v);return out;}
std::string FileBytes(const std::filesystem::path& path){std::ifstream f(path,std::ios::binary);return {std::istreambuf_iterator<char>(f),std::istreambuf_iterator<char>()};}
void Terminal(int ordinary,int terminalDelta,bool action=false){
    Engine e;Controller c(e);c.Boundary();std::optional<Json> out;
    auto request=Req(c,"terminal","commit",{{"actions",action?Json::array({Json{{"op","fixture"}}}):Json::array()},{"advance_ticks",ordinary+1}});
    c.Request(request,[&](Json r){out=r;});
    for(int i=0;i<ordinary;++i)Check(c.RunEngineFrame([&]{e.Original(1);}),"ordinary callback failed");
    auto before=c.Version();Check(c.RunEngineFrame([&]{e.Original(terminalDelta,4);}),"terminal callback failed");
    Check(out&&(*out)["ok"],"qualified terminal was rejected");const auto& r=(*out)["result"];
    Check(e.entries==ordinary+1&&e.returns==e.entries,"wrapper entry/return not real");
    Check(r["executed_ticks"]==ordinary+terminalDelta&&r["executed_engine_calls"]==ordinary+1,"clock/call counts conflated");
    Check(r["terminal_zero_clock_calls"]==(terminalDelta==0?1:0)&&r["last_engine_call_id"]==ordinary+1,"terminal counters wrong");
    Check(r["terminal_kind"]==(terminalDelta==0?"terminal_zero_clock_update":"terminal_clock_step"),"terminal classification wrong");
    auto pre=Of(e,"pre_step"),post=Of(e,"post_step");Check(pre.size()==post.size()&&post.size()==size_t(ordinary+1),"missing actual terminal state");
    const auto& p=post.back();const auto& meta=p["payload"]["engine_call"];
    Check(meta["engine_call_completed"]==true&&meta["native_tick_delta"]==terminalDelta&&meta["game_ui_after"]==4,"terminal call facts wrong");
    Check(meta["clocks_after"]["effect_clock"]==201+ordinary&&meta["clocks_after"]["mj_clock"]==301+ordinary,"secondary clocks not measured");
    Check(pre.back()["payload"]["engine_call"]["engine_call_entered"]==false,"pre claimed callback had already run");
    Check(pre.back()["payload"]["_engine_call_raw"]["board_address_after"].is_null(),"pre contains post pointer");
    Check(p["payload"]["_engine_call_raw"]["board_address_before"]==e.board,"raw Board evidence lost");
    Check(p["payload"]["render"]["skipped"]==true&&e.draws==ordinary,"terminal drew an extra frame");
    if(!terminalDelta)Check(p["version"]==before&&p["state"]!=pre.back()["state"],"zero clock state/revision collapsed");
    auto health=c.EngineCallHealth();Check(health["healthy"]&&health["returned_calls"]==ordinary+1&&health["verified_clock_steps"]==ordinary+terminalDelta,"health formula wrong");
    Check(!c.RunEngineFrame([&]{e.Original(1);})&&!c.ShouldStep(),"terminal reopened update budget");
    Check(Now(c,request).dump()==out->dump()&&e.entries==ordinary+1,"terminal retry after epoch change did not recover exact response");
    auto status=Now(c,Req(c,"recover","status",{{"request_id","terminal"}}));
    Check(status["result"]["response"].dump()==out->dump(),"terminal status lost original counts after epoch change");
    auto changed=request;changed["params"]["advance_ticks"]=ordinary+2;
    Check(Now(c,changed)["error"]["code"]=="request_id_conflict","terminal same-ID different params accepted");
    changed=request;changed["expect"]=c.Version();
    Check(Now(c,changed)["error"]["code"]=="request_id_conflict","terminal same-ID different epoch accepted");
    Check(Now(c,Req(c,"after","advance",{{"max_ticks",1}}))["error"]["code"]=="not_in_fight","terminal accepted action/update");
    Check(Now(c,Req(c,"close","stop_recording"))["ok"]&&e.closed&&Of(e,"engine_call_closed").size()==1,"terminal cannot close authoritative health");
    const auto journal=c.JournalForTesting().Path();const auto reserve=std::filesystem::path(journal.wstring()+L".reserve");
    const auto beforeJournal=FileBytes(journal),beforeReserve=FileBytes(reserve);
    Check(Now(c,request).dump()==out->dump()&&e.entries==ordinary+1,"sealed terminal retry mutated/forgot result");
    Check(Now(c,Req(c,"sealed-status","status",{{"request_id","terminal"}}))["result"]["response"].dump()==out->dump(),"sealed status changed terminal reply");
    Check(Now(c,changed)["error"]["code"]=="request_id_conflict","sealed conflicting retry accepted");
    Check(c.JournalForTesting().Sealed()&&FileBytes(journal)==beforeJournal&&FileBytes(reserve)==beforeReserve,"terminal recovery changed sealed evidence");
}
void NormalAndRetry(){
    Engine e;Controller c(e);c.Boundary();auto origin=Now(c,Req(c,"origin","audit_snapshot"))["result"]["engine_call"];
    Check(origin["reserved_calls"]==0&&origin["active_call_id"].is_null()&&origin["healthy"],"baseline is not actual empty tracker");
    std::optional<Json> done;auto request=Req(c,"batch","advance",{{"max_ticks",4}});c.Request(request,[&](Json r){done=r;});
    for(int i=0;i<4;++i)c.RunEngineFrame([&]{auto pre=c.Version();e.Original(1);Check(e.hookLabel["version"]==pre,"update label mismatch");});
    Check(done&&(*done)["result"]["executed_engine_calls"]==4&&e.draws==4,"batch count wrong");
    Check(Now(c,request)==*done&&e.entries==4,"retry executed original again");
    auto zero=Now(c,Req(c,"zero","commit",{{"actions",Json::array()},{"advance_ticks",0}}));
    Check(zero["result"]["executed_engine_calls"]==0&&zero["result"]["last_engine_call_id"].is_null(),"zero request inherited last ID");
    auto failed=Now(c,Req(c,"failed-action","commit",{{"actions",Json::array({Json{{"fail",true}}})},{"advance_ticks",1}}));
    Check(failed["result"]["executed_engine_calls"]==0&&e.entries==4,"failed action called engine");
    Now(c,Req(c,"pause","pause"));Now(c,Req(c,"status","status"));Check(e.entries==4,"control executed engine");
}
void Faults(){
    for(int delta:{-1,0,2}){
        Engine e;Controller c(e);c.Boundary();std::optional<Json> out;c.Request(Req(c,"bad","advance",{{"max_ticks",3}}),[&](Json r){out=r;});
        c.RunEngineFrame([&]{e.Original(delta);});Check(out&&!(*out)["ok"]&&e.entries==1&&!c.ShouldStep(),"bad clock did not fail closed");
        Check((*out)["error"]["details"]["executed_engine_calls"]==1,"bad clock hid completed call");
        Check(!c.EngineCallHealth()["healthy"].get<bool>(),"invalid call was declared healthy");
    }
    for(int outcome:{0,1,2}){
        Engine e;Controller c(e);c.Boundary();std::optional<Json> out;c.Request(Req(c,"bad-terminal","advance",{{"max_ticks",1}}),[&](Json r){out=r;});
        c.RunEngineFrame([&]{e.Original(0,outcome==0?2:4);if(outcome==1)e.board=0;if(outcome==2)e.board+=0x1000;});
        Check(out&&!(*out)["ok"]&&e.entries==1&&!c.ShouldStep(),"unsupported UI or replaced Board admitted");
        if(outcome)Check(Of(e,"post_step").empty(),"replacement Board fabricated comparable post");
    }
    {Engine e;e.throwPre=true;Controller c(e);c.Boundary();std::optional<Json> out;c.Request(Req(c,"pre-fault","advance",{{"max_ticks",1}}),[&](Json r){out=r;});
        c.RunEngineFrame([&]{e.Original(1);});auto h=c.EngineCallHealth();Check(out&&e.entries==0&&h["reserved_calls"]==1&&h["entered_calls"]==0&&h["returned_calls"]==0,"pre audit failure invented invocation");}
    {Engine e;e.throwDraw=true;Controller c(e);c.Boundary();std::optional<Json> out;c.Request(Req(c,"render-fault","advance",{{"max_ticks",1}}),[&](Json r){out=r;});
        c.RunEngineFrame([&]{e.Original(1);});Check(out&&(*out)["error"]["details"]["executed_ticks"]==1&&(*out)["error"]["details"]["executed_engine_calls"]==1&&Of(e,"post_step").empty(),"render failure lost returned work");}
    {Engine e;Controller c(e);c.Boundary();std::optional<Json> out;c.Request(Req(c,"throw","advance",{{"max_ticks",1}}),[&](Json r){out=r;});
        c.RunEngineFrame([&]{++e.entries;throw std::runtime_error("engine fixture exception");});auto h=c.EngineCallHealth();
        Check(out&&h["entered_calls"]==1&&h["returned_calls"]==0&&h["aborted_calls"]==1,"missing return fabricated completed call");}
}
void Reentry(){
    Engine e;Controller c(e);c.Boundary();std::optional<Json> out;c.Request(Req(c,"outer","advance",{{"max_ticks",3}}),[&](Json r){out=r;});
    int nested=0;c.RunEngineFrame([&]{
        ++e.entries;Check(!c.RunEngineFrame([&]{++nested;}),"nested wrapper ran");
        Check(!out,"outer request completed before actual callback return");
        ++e.clock;++e.effect;++e.mj;++e.returns;
    });
    Check(out&&!(*out)["ok"]&&nested==0&&e.entries==1&&(*out)["error"]["details"]["executed_ticks"]==1&&(*out)["error"]["details"]["executed_engine_calls"]==1,"reentry lost actual outer return");
    Check(c.EngineCallHealth()["reentrant_calls"]==1&&c.EngineCallHealth()["returned_calls"]==1,"reentry count wrong");
    Check(Now(c,Req(c,"pause","pause"))["ok"]&&Now(c,Req(c,"status","status"))["ok"]&&Now(c,Req(c,"close","stop_recording"))["ok"],"fault blocked control service");
    Engine drawing;Controller d(drawing);d.Boundary();std::optional<Json> drawOut;
    d.Request(Req(d,"drawing","advance",{{"max_ticks",1}}),[&](Json r){drawOut=r;});
    drawing.duringDraw=[&]{Check(!d.RunEngineFrame([&]{++nested;}),"render nested wrapper ran");};
    d.RunEngineFrame([&]{drawing.Original(1);});
    Check(drawOut&&!(*drawOut)["ok"]&&(*drawOut)["error"]["details"]["executed_engine_calls"]==1&&nested==0&&Of(drawing,"post_step").empty(),"render reentry produced successful post");
    Engine deferred;Controller f(deferred);f.Boundary();std::optional<Json> faultOut;
    f.Request(Req(f,"deferred","advance",{{"max_ticks",1}}),[&](Json r){faultOut=r;});
    f.RunEngineFrame([&]{++deferred.entries;f.Fail("in-flight fixture fault");Check(!faultOut,"in-flight failure completed before return");++deferred.clock;++deferred.returns;});
    Check(faultOut&&(*faultOut)["error"]["details"]["executed_ticks"]==1&&(*faultOut)["error"]["details"]["executed_engine_calls"]==1,"deferred failure lost returned work");
}
void TrackerGuards(){
    auto rejects=[](auto op){bool rejected=false;try{op();}catch(...){rejected=true;}Check(rejected,"invalid token accepted");};
    EngineCallTracker a;auto id=a.Reserve();rejects([&]{a.Returned(id);});rejects([&]{a.Enter(id+1);});a.Enter(id);rejects([&]{a.Enter(id);});a.Returned(id);rejects([&]{a.Returned(id);});a.Complete(id,true,true,false,false);
    EngineCallTracker b;bool allowed=true;std::thread other([&]{allowed=b.GuardEntry();});other.join();Check(!allowed&&b.Health()["wrong_thread_calls"]==1,"wrong thread not rejected");
    Engine e;Controller c(e);c.Boundary();std::optional<Json> out;c.Request(Req(c,"foreign","advance",{{"max_ticks",1}}),[&](Json r){out=r;});
    std::thread foreign([&]{Check(!c.GuardEngineEntry(),"foreign native pump admitted");});foreign.join();
    Check(!CheckPumpGate(c,[&]{c.CheckEngineGuard();},[&]{
        Check(Now(c,Req(c,"foreign-status","status"))["ok"],"owner could not serve status after foreign entry");
        Check(Now(c,Req(c,"foreign-close","stop_recording"))["ok"],"owner could not close after foreign entry");
    }),"foreign guard failure reopened engine");
    Check(out&&(*out)["error"]["details"]["executed_engine_calls"]==0&&e.entries==0,"foreign entry left pending request unresolved");
}
void UnmeasuredTransition(){
    for(int completed:{0,1})for(int change:{0,1,2}) {
        Engine e;Controller c(e);c.Boundary();std::optional<Json> out;
        c.Request(Req(c,"outside","advance",{{"max_ticks",3}}),[&](Json r){out=r;});
        if(completed)Check(c.RunEngineFrame([&]{e.Original(1);}),"ordinary prefix failed");
        if(change==0)e.ui=4;
        if(change==1)e.board+=0x1000;
        if(change==2)--e.clock;
        c.Boundary();
        Check(out&&!(*out)["ok"]&&(*out)["error"]["code"]=="audit_failed","outside transition fabricated successful terminal");
        const auto& facts=(*out)["error"]["details"];
        Check(facts["executed_ticks"]==completed&&facts["executed_engine_calls"]==completed&&e.entries==completed,"outside transition erased or fabricated actual work");
        Check(Of(e,"terminal_transition").empty()&&!c.ShouldStep(),"unmeasured transition received a terminal certificate");
        Check(Of(e,"engine_call_fault").size()==1,"unmeasured transition missing permanent native fault evidence");
        Check(Now(c,Req(c,"close-outside","stop_recording"))["ok"],"outside transition prevented closure");
    }
}
int main(){try{NormalAndRetry();Terminal(0,0);Terminal(3,0);Terminal(0,0,true);Terminal(0,1);Faults();Reentry();TrackerGuards();UnmeasuredTransition();
    std::cout<<"actual callback count/return, zero-clock terminal, secondary clocks, labels, retries, failure and reentry fixtures passed\n";return 0;
}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
