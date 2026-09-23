#include "runtime/controller.hpp"
#include "determinism/app_update_anchor.hpp"
#include <iostream>
#include <stdexcept>
using namespace lvz::runtime;
using lvz::determinism::InitialAppUpdateAnchor;
void Check(bool ok,const char* why){if(!ok)throw std::runtime_error(why);}
struct Fake:Backend {
    uint32_t beforeGuard=0x31415926,count=1294,afterGuard=0x27182818;
    InitialAppUpdateAnchor anchor;
    bool enabled=true,seeded=false,warm=false,fight=true,closed=false,occupied=false,channel=false,badRng=false;
    bool mutateHistory=false,throwAfter=false;int captures=0,clock=3000,draws=0,seedCalls=0;uintptr_t board=1;
    uint32_t seed=42;Json demo={{"00000510",0},{"00000511",0},{"00000578",17},{"0000049c",19},{"000004a0",23}};
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
    bool AppUpdateAnchored()const override{return anchor.Applied();}
    void ResetRenderPreparation()override{anchor.Reset();warm=seeded=false;}
    Json SeedRng(uint32_t value)override{seed=value;seeded=true;++seedCalls;return {{"ok",true}};}
    Json RestoreClocks(const Json&)override{return {{"ok",true}};}
    Json RestoreRng(const Json&)override{return {{"ok",true}};}
    Json State(){
        ++captures;if(throwAfter&&captures==2)throw std::runtime_error("snapshot failure after write");
        Json words=Json::array();uint32_t word=seed?seed:4357;
        for(unsigned i=0;i<624;++i){words.push_back(word);word=1812433253u*(word^(word>>30))+i+1;}
        if(badRng)words[623]=123;
        Json histories=Json::array();for(unsigned i=0;i<110;++i){Json slots=Json::array();for(unsigned j=0;j<8;++j)slots.push_back({0,0,0,800,3});histories.push_back({{"last_variation",i},{"slots",slots}});}
        if(occupied)histories[0]["slots"][0][0]=123;
        if(mutateHistory&&captures==2)histories[109]["last_variation"]=200;
        Json channels=std::vector<int>(32,0);if(channel)channels[31]=1;
        return {{"schema","lvz.audit.v1"},{"rng",{{"instances",{{"global_mt",{{"words",words},{"cursor",624}}},
            {"game_thread_crt",{{"state",seed}}}}}}},
            {"sound_effects",{{"mode","sound_effects_allocation_none_v1"},{"calls",5},{"errors",0},{"patch_owned",true},{"slots_empty",true},
                {"app_update_count",count},{"histories",histories},{"parameters",{{"fixture",7}}},{"channels",channels}}},
            {"board",{{"game_clock",clock}}},{"unchanged_guard",0x12345678}};
    }
    Json AnchorAppUpdate(uint32_t requested)override{
        captures=0;return anchor.Apply(reinterpret_cast<uintptr_t>(&count),requested,seed,enabled,seeded,warm,
            [&]{return State();},[&]{return demo;});
    }
    Json RenderFrame(const Json& version,bool isWarm)override{
        Check(!isWarm||(!enabled||anchor.Applied()),"unanchored silent draw");warm=true;++draws;return {{"frame_version",version},{"phase",isWarm?"warm":"step"}};
    }
    void CloseRecording()override{closed=true;}
    void Audit(const std::string& kind,const Json& payload,const Json& observation)override{events.push_back({{"kind",kind},{"payload",payload},{"version",observation["version"]}});}
};
Json Req(Controller& c,const std::string& id,const std::string& method,Json params=Json::object()){
    return {{"protocol",1},{"request_id",id},{"method",method},{"params",params},{"expect",c.Version()}};
}
Json Now(Controller& c,const Json& request){std::optional<Json> response;c.Request(request,[&](Json r){response=std::move(r);});Check(response.has_value(),"unexpected pending request");return *response;}
Json Set(Controller& c,const std::string& id,uint32_t target){return Now(c,Req(c,id,"app_update_anchor",{{"app_update_count",target}}));}
void Seed(Controller& c){Check(Now(c,Req(c,"seed","rng_seed",{{"seed",42}}))["ok"],"fixture seed");}
int main(){try{
    Fake f;Controller c(f);c.Boundary();
    Check(!Set(c,"before-seed",1295)["ok"],"unseeded anchor allowed");Check(f.count==1294,"rejected anchor wrote memory");
    auto seedRequest=Req(c,"seed","rng_seed",{{"seed",42}});auto seeded=Now(c,seedRequest);
    Check(!Now(c,Req(c,"before-anchor-warm","prepare_render"))["ok"]&&f.draws==0,"warm admitted before anchor");
    for(Json bad:{Json(-1),Json(2147483648ull),Json(1.5),Json(true),Json("1")})
        Check(Now(c,Req(c,"bad-"+bad.dump(),"app_update_anchor",{{"app_update_count",bad}}))["error"]["code"]=="invalid_params","bad count accepted");
    auto stale=Req(c,"stale","app_update_anchor",{{"app_update_count",1295}});stale["expect"]["revision"]=0;
    Check(Now(c,stale)["error"]["code"]=="stale_observation","stale anchor accepted");
    auto request=Req(c,"anchor","app_update_anchor",{{"app_update_count",1295}});auto before=c.Version();auto response=Now(c,request);
    Check(response["ok"]&&response["result"]["anchored"]&&f.count==1295&&f.beforeGuard==0x31415926&&f.afterGuard==0x27182818,"actual isolated field write failed");
    const auto& receipt=response["result"]["anchor"];auto expected=receipt["before_state"];expected["sound_effects"]["app_update_count"]=1295;
    Check(expected==receipt["after_state"]&&receipt["before"]==1294&&receipt["after"]==1295,"receipt/full state check");
    Check(receipt["before_version"]==before&&receipt["after_version"]==c.Version()&&c.Version()["revision"]==2,"real revision receipt mismatch");
    Check(receipt["demo_before"]==receipt["demo_after"]&&f.demo["00000578"]==17,"demo fields offset or lost");
    Check(f.events.back()["kind"]=="app_update_anchored"&&f.events.back()["payload"]["anchor"]==receipt,"native successful receipt not emitted");
    auto eventCount=f.events.size();Check(Now(c,request)==response&&f.events.size()==eventCount,"retry repeated write/event");
    request["params"]["app_update_count"]=1296;Check(Now(c,request)["error"]["code"]=="request_id_conflict","request identity changed");
    Check(!Set(c,"anchor-again",1296)["ok"]&&f.count==1295,"new-id repeat allowed");
    Check(Now(c,seedRequest)==seeded&&f.seedCalls==1,"pre-anchor retry incorrectly rejected/re-executed");
    for(auto method:{"rng_seed","rng_restore","clock_restore"})Check(Now(c,Req(c,std::string("sealed-")+method,method,
        std::string(method)=="rng_seed"?Json{{"seed",7}}:Json{{"snapshot",Json::object()}}))["error"]["code"]=="initialization_sealed","post-anchor initializer admitted");
    Check(Now(c,Req(c,"warm","prepare_render"))["ok"]&&f.draws==1,"warm after anchor failed");
    Check(!Set(c,"after-warm",1296)["ok"],"postwarm anchor allowed");
    std::optional<Json> advanced;c.Request(Req(c,"step","advance",{{"max_ticks",1}}),[&](Json r){advanced=r;});
    c.RunEngineFrame([&]{++f.clock;++f.count;});Check(advanced&&(*advanced)["ok"],"actual engine wrapper fixture failed");
    Check(!Set(c,"after-step",5)["ok"]&&f.count==1296,"poststep anchor allowed");
    for(unsigned rejection=0;rejection<8;++rejection){Fake bad;Controller b(bad);b.Boundary();Seed(b);
        if(rejection==0)bad.enabled=false;if(rejection==1)bad.occupied=true;if(rejection==2)bad.channel=true;
        if(rejection==3)bad.badRng=true;if(rejection==4)bad.demo["00000510"]=1;if(rejection==5)bad.demo["00000511"]=1;
        if(rejection==6)bad.warm=true;if(rejection==7)bad.demo["000004a0"]=256;
        Check(!Set(b,"reject",1295)["ok"]&&bad.count==1294&&!bad.anchor.Applied(),"native precondition rejection wrote state");}
    for(unsigned failure=0;failure<2;++failure){Fake bad;bad.mutateHistory=failure==0;bad.throwAfter=failure==1;Controller b(bad);b.Boundary();Seed(b);
        auto failed=Set(b,"fault",1295);Check(failed["error"]["code"]=="app_update_anchor_failed"&&bad.count==1295,"partial write was rolled back or hidden");
        Check(!b.ShouldStep()&&!Now(b,Req(b,"forbidden-warm","prepare_render"))["ok"],"fault admitted warm");
        Check(bad.events.back()["kind"]=="app_update_anchor_failed"&&!bad.events.back()["payload"]["anchor"]["before_state"].is_null(),"failure lost pre-write state");
        Check(Now(b,Req(b,"close","stop_recording"))["ok"]&&bad.closed,"fault evidence could not close");}
    // Two worlds with different actual counters and one declared target.
    Fake worldE;worldE.count=1395;Controller e(worldE);e.Boundary();Seed(e);
    auto worldEResult=Set(e,"common-target",1500);Check(worldEResult["ok"]&&worldE.count==1500,"declared target write failed for the first world");
    Fake worldF;worldF.count=1422;Controller g(worldF);g.Boundary();Seed(g);
    auto worldFResult=Set(g,"common-target",1500);Check(worldFResult["ok"]&&worldF.count==1500,"declared target write failed for the second world");
    Check(worldEResult["result"]["anchor"]["before"]==1395&&worldFResult["result"]["anchor"]["before"]==1422
        &&worldEResult["result"]["anchor"]["requested"]==1500&&worldFResult["result"]["anchor"]["requested"]==1500
        &&worldEResult["result"]["anchor"]["after"]==worldFResult["result"]["anchor"]["after"]
        &&worldFResult["result"]["anchor"]["after"]==1500,"cross-run declared target/readback mismatch");
    Fake source;Controller s(source);s.Boundary();Seed(s);auto noop=Set(s,"self-anchor",source.count);Check(noop["ok"]&&noop["result"]["anchor"]["before_state"]==noop["result"]["anchor"]["after_state"],"source no-op did not record actual equal states");
    for(uint32_t edge:{0u,uint32_t(INT32_MAX)}){Fake value;Controller v(value);v.Boundary();Seed(v);Check(Set(v,"edge",edge)["ok"]&&value.count==edge,"signed permitted boundary rejected");}
    Fake original;original.enabled=false;Controller o(original);o.Boundary();Seed(o);Check(Now(o,Req(o,"ordinary-warm","prepare_render"))["ok"],"ordinary audio warm compatibility changed");
    std::cout<<"App counter field write, cross-run declared target, complete evidence, actual seed, empty audio, demo, lifecycle/idempotence and failure fixtures passed\n";return 0;
}catch(const std::exception& error){std::cerr<<error.what()<<'\n';return 1;}}
