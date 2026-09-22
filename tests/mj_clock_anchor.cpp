#include "runtime/controller.hpp"
#include "determinism/app_update_anchor.hpp"
#include "determinism/mj_clock_anchor.hpp"
#include <iostream>
#include <stdexcept>
using namespace lvz::runtime;
using lvz::determinism::InitialAppUpdateAnchor;
using lvz::determinism::InitialMjClockAnchor;
void Check(bool ok,const char* why){if(!ok)throw std::runtime_error(why);}
// The actual fixed write must touch exactly four bytes; the sentinels around
// the simulated LawnApp+0x838 field prove no neighboring byte is rewritten.
struct Fake:Backend {
    uint32_t beforeGuard=0x31415926,mj=1307,afterGuard=0x27182818;
    InitialAppUpdateAnchor appAnchor;InitialMjClockAnchor mjAnchor;
    bool enabled=true,seeded=false,warm=false,fight=true,closed=false,occupied=false,channel=false,badRng=false;
    bool mutateHistory=false,throwAfter=false,faultEnabled=false;int captures=0,clock=3000,draws=0,seedCalls=0;uintptr_t board=1;
    uint32_t seed=42,count=1294;
    std::vector<Json> events;
    bool Ready()const override{return fight;}
    uintptr_t BoardIdentity()const override{return board;}
    int NativeTick()const override{return clock;}
    bool UsesEngineCallBoundary()const override{return true;}
    Json Observe()override{return {{"game_clock",clock},{"wave",1}};}
    Json Hello()override{return Json::object();}
    Json Execute(const Json&)override{return {{"ok",true}};}
    bool RequiresRenderPreparation()const override{return true;}
    bool RenderPrepared()const override{return warm;}
    bool SupportsAppUpdateAnchor()const override{return enabled;}
    bool AppUpdateAnchored()const override{return appAnchor.Applied();}
    bool SupportsMjClockAnchor()const override{return enabled;}
    bool MjClockAnchored()const override{return mjAnchor.Applied();}
    void ResetRenderPreparation()override{appAnchor.Reset();mjAnchor.Reset();warm=seeded=false;}
    Json SeedRng(uint32_t value)override{seed=value;seeded=true;++seedCalls;return {{"ok",true}};}
    Json RestoreClocks(const Json&)override{return {{"ok",true}};}
    Json RestoreRng(const Json&)override{return {{"ok",true}};}
    Json State(){
        ++captures;if(faultEnabled&&throwAfter&&captures==2)throw std::runtime_error("snapshot failure after fixed write");
        Json words=Json::array();uint32_t word=seed?seed:4357;
        for(unsigned i=0;i<624;++i){words.push_back(word);word=1812433253u*(word^(word>>30))+i+1;}
        if(badRng)words[623]=123;
        Json histories=Json::array();for(unsigned i=0;i<110;++i){Json slots=Json::array();for(unsigned j=0;j<8;++j)slots.push_back({0,0,0,800,3});histories.push_back({{"last_variation",i},{"slots",slots}});}
        if(occupied)histories[0]["slots"][0][0]=123;
        if(faultEnabled&&mutateHistory&&captures==2)histories[109]["last_variation"]=200;
        Json channels=std::vector<int>(32,0);if(channel)channels[31]=1;
        return {{"schema","lvz.audit.v1"},{"rng",{{"instances",{{"global_mt",{{"words",words},{"cursor",624}}},
            {"game_thread_crt",{{"state",seed}}}}}}},
            {"app",{{"ui",3},{"mj_clock",mj}}},
            {"sound_effects",{{"mode","sound_effects_allocation_none_v1"},{"calls",5},{"errors",0},{"patch_owned",true},{"slots_empty",true},
                {"app_update_count",count},{"histories",histories},{"parameters",{{"fixture",7}}},{"channels",channels}}},
            {"board",{{"game_clock",clock}}},{"unchanged_guard",0x12345678}};
    }
    Json AnchorAppUpdate(uint32_t requested)override{captures=0;
        return appAnchor.Apply(reinterpret_cast<uintptr_t>(&count),requested,seed,enabled,seeded,warm,
            [&]{return State();},[&]{return Json{{"00000510",0},{"00000511",0},{"00000578",17},{"0000049c",19},{"000004a0",23}};});}
    Json AnchorMjClock(uint32_t requested)override{captures=0;
        return mjAnchor.Apply(reinterpret_cast<uintptr_t>(&mj),requested,seed,enabled,appAnchor.Applied(),
            seeded,warm,[&]{return State();});}
    Json RenderFrame(const Json& version,bool isWarm)override{
        Check(!isWarm||!enabled||mjAnchor.Applied(),"unanchored fixed-counter draw");
        warm=true;++draws;return {{"frame_version",version},{"phase",isWarm?"warm":"step"}};
    }
    void CloseRecording()override{closed=true;}
    void Audit(const std::string& kind,const Json& payload,const Json& observation)override{events.push_back({{"kind",kind},{"payload",payload},{"version",observation["version"]}});}
};
Json Req(Controller& c,const std::string& id,const std::string& method,Json params=Json::object()){
    return {{"protocol",1},{"request_id",id},{"method",method},{"params",params},{"expect",c.Version()}};
}
Json Now(Controller& c,const Json& request){std::optional<Json> response;c.Request(request,[&](Json r){response=std::move(r);});Check(response.has_value(),"unexpected pending request");return *response;}
Json App(Controller& c,const std::string& id,uint32_t target){return Now(c,Req(c,id,"app_update_anchor",{{"app_update_count",target}}));}
Json Mj(Controller& c,const std::string& id,uint32_t target){return Now(c,Req(c,id,"mj_clock_anchor",{{"mj_clock",target}}));}
void Seed(Controller& c){Check(Now(c,Req(c,"seed","rng_seed",{{"seed",42}}))["ok"],"fixture seed");}
void AnchorApp(Controller& c,Fake& f){Check(App(c,"app-anchor",f.count)["ok"],"fixture App anchor");}
int main(){try{
    Fake f;Controller c(f);c.Boundary();c.Boundary();
    Check(!Mj(c,"before-app",900)["ok"]&&f.mj==1307,"fixed anchor preceded the App anchor");
    auto seedRequest=Req(c,"seed","rng_seed",{{"seed",42}});auto seeded=Now(c,seedRequest);
    Check(Mj(c,"before-app-2",900)["error"]["code"]=="mj_clock_anchor_rejected"&&f.mj==1307,"unapp-anchored fixed write admitted");
    AnchorApp(c,f);
    Check(Now(c,Req(c,"warm-before-fixed","prepare_render"))["error"]["code"]=="render_prepare_rejected","warm draw preceded the fixed anchor");
    for(Json bad:{Json(-1),Json(2147483648ull),Json(1.5),Json(true),Json("1")})
        Check(Now(c,Req(c,"bad-"+bad.dump(),"mj_clock_anchor",{{"mj_clock",bad}}))["error"]["code"]=="invalid_params","bad fixed target accepted");
    auto missing=Req(c,"missing","mj_clock_anchor");missing["params"]=Json::object();
    Check(Now(c,missing)["error"]["code"]=="invalid_params","missing target accepted");
    auto extra=Req(c,"extra","mj_clock_anchor",{{"mj_clock",900},{"app_update_count",1}});
    Check(Now(c,extra)["error"]["code"]=="invalid_params","extra parameter accepted");
    auto stale=Req(c,"stale","mj_clock_anchor",{{"mj_clock",900}});stale["expect"]["revision"]=0;
    Check(Now(c,stale)["error"]["code"]=="stale_observation","stale fixed anchor accepted");
    auto request=Req(c,"fixed","mj_clock_anchor",{{"mj_clock",900}});
    auto before=c.Version();auto response=Now(c,request);
    Check(response["ok"]&&response["result"]["anchored"]&&f.mj==900,"actual fixed write failed");
    Check(f.beforeGuard==0x31415926&&f.afterGuard==0x27182818,"neighbor bytes changed");
    const auto& receipt=response["result"]["anchor"];auto expected=receipt["before_state"];expected["app"]["mj_clock"]=900;
    Check(expected==receipt["after_state"]&&receipt["before"]==1307&&receipt["after"]==900&&receipt["requested"]==900,"receipt/full state check");
    Check(receipt["schema"]=="lvz.mj-clock-anchor.v1"&&receipt["mode"]=="initial_mj_clock_anchor_v1","receipt identity mismatch");
    Check(receipt["before_version"]==before&&receipt["after_version"]==c.Version(),"adjacent revision receipt mismatch");
    Check(f.events.back()["kind"]=="mj_clock_anchored"&&f.events.back()["payload"]["anchor"]==receipt,"native successful receipt not emitted");
    auto eventCount=f.events.size();
    Check(Now(c,request)==response&&f.events.size()==eventCount,"retry repeated write/event");
    request["params"]["mj_clock"]=901;Check(Now(c,request)["error"]["code"]=="request_id_conflict","request identity changed");
    Check(!Mj(c,"fixed-again",901)["ok"]&&f.mj==900,"new-id repeat allowed");
    Check(Now(c,seedRequest)==seeded&&f.seedCalls==1,"pre-anchor retry incorrectly rejected/re-executed");
    for(auto method:{"rng_seed","rng_restore","clock_restore"})Check(Now(c,Req(c,std::string("sealed-")+method,method,
        std::string(method)=="rng_seed"?Json{{"seed",7}}:Json{{"snapshot",Json::object()}}))["error"]["code"]=="initialization_sealed","post-anchor initializer admitted");
    Check(Now(c,Req(c,"warm","prepare_render"))["ok"]&&f.draws==1,"warm after fixed anchor failed");
    Check(!Mj(c,"after-warm",5)["ok"],"postwarm fixed anchor allowed");
    std::optional<Json> advanced;c.Request(Req(c,"step","advance",{{"max_ticks",1}}),[&](Json r){advanced=r;});
    c.RunEngineFrame([&]{++f.clock;++f.count;++f.mj;});Check(advanced&&(*advanced)["ok"],"actual engine wrapper fixture failed");
    Check(!Mj(c,"after-step",5)["ok"]&&f.mj==901,"poststep fixed anchor allowed");
    for(unsigned rejection=0;rejection<5;++rejection){Fake bad;Controller b(bad);b.Boundary();b.Boundary();Seed(b);
        if(rejection==4)bad.warm=true;
        if(rejection!=0&&!bad.warm)AnchorApp(b,bad);
        if(rejection==0)bad.enabled=false;if(rejection==1)bad.occupied=true;if(rejection==2)bad.channel=true;
        if(rejection==3)bad.badRng=true;
        Check(!Mj(b,"reject",900)["ok"]&&bad.mj==1307&&!bad.mjAnchor.Applied(),"native precondition rejection wrote state");}
    for(unsigned failure=0;failure<2;++failure){Fake bad;
        Controller b(bad);b.Boundary();b.Boundary();Seed(b);AnchorApp(b,bad);
        bad.faultEnabled=true;bad.mutateHistory=failure==0;bad.throwAfter=failure==1;
        auto failed=Mj(b,"fault",900);
        Check(failed["error"]["code"]=="mj_clock_anchor_failed"&&bad.mj==900,"partial fixed write was rolled back or hidden");
        Check(!b.ShouldStep()&&!Now(b,Req(b,"forbidden-warm","prepare_render"))["ok"],"fault admitted warm");
        Check(bad.events.back()["kind"]=="mj_clock_anchor_failed"&&!bad.events.back()["payload"]["anchor"]["before_state"].is_null(),"failure lost pre-write state");
        Check(Now(b,Req(b,"close","stop_recording"))["ok"]&&bad.closed,"fault evidence could not close");}
    // Two worlds with different actual counters and one declared target.
    Fake source;source.mj=1307;Controller s(source);s.Boundary();Seed(s);AnchorApp(s,source);
    auto sourceResult=Mj(s,"common-target",2048);Check(sourceResult["ok"]&&source.mj==2048,"fixed target source write failed");
    Fake cold;cold.mj=1340;Controller d(cold);d.Boundary();Seed(d);AnchorApp(d,cold);
    auto coldResult=Mj(d,"common-target",2048);Check(coldResult["ok"]&&cold.mj==2048,"fixed target cold write failed");
    Check(sourceResult["result"]["anchor"]["before"]==1307&&coldResult["result"]["anchor"]["before"]==1340
        &&sourceResult["result"]["anchor"]["after"]==coldResult["result"]["anchor"]["after"]
        &&coldResult["result"]["anchor"]["requested"]==2048,"cross-run fixed target/readback mismatch");
    Fake noop;Controller n(noop);n.Boundary();Seed(n);AnchorApp(n,noop);
    auto self=Mj(n,"self",noop.mj);Check(self["ok"]&&self["result"]["anchor"]["before_state"]==self["result"]["anchor"]["after_state"],"source no-op did not record actual equal states");
    for(uint32_t edge:{0u,uint32_t(INT32_MAX)}){Fake value;Controller v(value);v.Boundary();Seed(v);AnchorApp(v,value);
        Check(Mj(v,"edge",edge)["ok"]&&value.mj==edge,"signed permitted boundary rejected");}
    Fake original;original.enabled=false;Controller o(original);o.Boundary();Seed(o);
    Check(Now(o,Req(o,"ordinary-warm","prepare_render"))["ok"],"ordinary audio warm compatibility changed");
    std::cout<<"Fixed App counter write, cross-run target, complete evidence, ordering, lifecycle/idempotence and failure fixtures passed\n";return 0;
}catch(const std::exception& error){std::cerr<<error.what()<<'\n';return 1;}}
