#include "runtime/controller.hpp"
#include "determinism/sound_counter_origin.hpp"
#include "determinism/app_update_anchor.hpp"
#include "launcher/silent_audio.hpp"
#include <windows.h>
#include <iostream>
using namespace lvz::runtime;
using lvz::determinism::SoundCounterOrigin;
void Check(bool value,const char* why){if(!value)throw std::runtime_error(why);}
template<class F> void Rejects(F fn,const char* why){bool rejected=false;try{fn();}catch(...){rejected=true;}Check(rejected,why);}
struct Fake:Backend {
    SoundCounterOrigin origin;
    lvz::determinism::InitialAppUpdateAnchor appAnchor;
    uint32_t rawCalls=14,count=1362,seed=42,untouched=0x12345678;
    bool enabled=true,seeded=false,warm=false,fight=true,closed=false;
    bool occupied=false,channel=false,badRng=false,mutateHistory=false,throwAfter=false,changedRaw=false;
    uint32_t rawErrors=0,thread=GetCurrentThreadId();
    int captures=0,clock=3000,seedCalls=0,draws=0;uintptr_t board=1;
    Json lastRaw,closedHealth;
    std::vector<Json> events;
    bool Ready()const override{return fight;}
    uintptr_t BoardIdentity()const override{return board;}
    int NativeTick()const override{return clock;}
    bool UsesEngineCallBoundary()const override{return true;}
    Json Observe()override{return {{"game_ui",fight?3:1},{"game_clock",clock},{"wave",1}};}
    Json Hello()override{return Json::object();}
    Json Execute(const Json&)override{return {{"ok",true}};}
    bool RequiresRenderPreparation()const override{return true;}
    bool RenderPrepared()const override{return warm;}
    bool SupportsAppUpdateAnchor()const override{return enabled;}
    bool AppUpdateAnchored()const override{return appAnchor.Applied();}
    bool SupportsSoundCounterOrigin()const override{return enabled;}
    bool SoundCounterBound()const override{return origin.Bound();}
    // This epoch reset deliberately cannot clear the recording's origin.
    void ResetRenderPreparation()override{appAnchor.Reset();warm=seeded=false;}
    Json SeedRng(uint32_t value)override{seed=value;seeded=true;++seedCalls;return {{"ok",true}};}
    Json RestoreRng(const Json&)override{return {{"ok",true}};}
    Json RestoreClocks(const Json&)override{return {{"ok",true}};}
    Json Raw(){return {{"mode",lvz::silentaudio::Mode},{"installed",true},{"before_primary_thread_resume",true},
        {"phase","sealed_before_resume"},{"primary_thread",thread},{"owner_module",0x10000000},{"owner_pinned",true},
        {"entry",lvz::silentaudio::Target},{"replacement",0x10001000},{"patch_owned",true},{"calls",rawCalls},{"errors",rawErrors},
        {"preexisting_app",0},{"original_bytes",lvz::silentaudio::Original},{"patch_bytes",{0xe9,1,2,3,4,0x90}},
        {"engine_sha256",lvz::silentaudio::EngineSha256},{"bootstrap_sha256",std::string(64,'a')}};}
    Json State(){
        ++captures;if(throwAfter&&captures==2)throw std::runtime_error("post-bind capture failed");
        if(changedRaw&&captures==2)++rawCalls;
        Json words=Json::array();uint32_t word=seed?seed:4357;
        for(unsigned i=0;i<624;++i){words.push_back(word);word=1812433253u*(word^(word>>30))+i+1;}
        if(badRng)words[623]=123;
        Json histories=Json::array();for(unsigned i=0;i<110;++i){Json slots=Json::array();
            for(unsigned j=0;j<8;++j)slots.push_back({0,0,0,800,3});
            histories.push_back({{"last_variation",i},{"slots",slots}});}
        if(occupied)histories[0]["slots"][0][0]=123;
        if(mutateHistory&&captures==2)histories[109]["last_variation"]=200;
        Json channels=std::vector<int>(32,0);if(channel)channels[31]=1;
        Json audio={{"mode",lvz::silentaudio::Mode},{"calls",rawCalls},{"errors",0},{"patch_owned",true},{"slots_empty",true},
            {"app_update_count",count},{"histories",histories},{"parameters",{{"fixture",7}}},{"channels",channels}};
        lastRaw=Raw();audio=origin.Present(std::move(audio),lastRaw);
        return {{"schema","lvz.audit.v1"},{"app",{{"ui",3}}},
            {"rng",{{"instances",{{"global_mt",{{"words",words},{"cursor",624}}},{"game_thread_crt",{{"state",seed}}}}}}},
            {"sound_effects",std::move(audio)},{"board",{{"game_clock",clock}}},{"untouched",untouched}};
    }
    Json BindSoundCounterOrigin()override{
        captures=0;return origin.Apply(seed,enabled,seeded,warm,appAnchor.Applied(),[&]{return State();},[&]{return lastRaw;});
    }
    Json AnchorAppUpdate(uint32_t requested)override{
        return appAnchor.Apply(reinterpret_cast<uintptr_t>(&count),requested,seed,enabled,seeded,warm,[&]{return State();},
            []{return Json{{"00000510",0},{"00000511",0},{"00000578",17},{"0000049c",19},{"000004a0",23}};});
    }
    Json RenderFrame(const Json& version,bool isWarm)override{
        Check(!isWarm||(!enabled||(origin.Bound()&&appAnchor.Applied())),"uninitialized silent draw");
        warm=true;++draws;rawCalls+=2;return {{"frame_version",version},{"phase",isWarm?"warm":"step"}};
    }
    void Audit(const std::string& kind,const Json& payload,const Json& observation)override{
        events.push_back({{"kind",kind},{"payload",payload},{"version",observation["version"]}});
    }
    void CloseRecording()override{
        closed=true;
        try{auto state=State();closedHealth=origin.Health({{"healthy",true},{"calls",state["sound_effects"]["calls"]}},state["sound_effects"],lastRaw);}
        catch(const std::exception& e){closedHealth={{"healthy",false},{"failure",e.what()},{"raw_calls",rawCalls}};}
    }
};
Json Req(Controller& c,const std::string& id,const std::string& method,Json params=Json::object()){
    return {{"protocol",1},{"request_id",id},{"method",method},{"params",params},{"expect",c.Version()}};
}
Json Now(Controller& c,const Json& request){std::optional<Json> response;c.Request(request,[&](Json r){response=std::move(r);});Check(response.has_value(),"unexpected pending request");return *response;}
Json Bind(Controller& c,const std::string& id="bind"){return Now(c,Req(c,id,"sound_counter_origin"));}
void Seed(Controller& c){Check(Now(c,Req(c,"seed","rng_seed",{{"seed",42}}))["ok"],"fixture seed failed");}
int main(){try{
    Fake f;Controller c(f);c.Boundary();
    Check(f.State()["sound_effects"]["counter_scope"]=="bootstrap_lifetime","pre-origin scope wrong");
    Check(!Bind(c,"before-seed")["ok"]&&!f.origin.Bound()&&f.rawCalls==14,"unseeded origin accepted or raw reset");
    auto seedReq=Req(c,"seed","rng_seed",{{"seed",42}});auto seedReply=Now(c,seedReq);
    Check(!Now(c,Req(c,"app-before","app_update_anchor",{{"app_update_count",1362}}))["ok"],"App anchor admitted without origin");
    Check(!Now(c,Req(c,"warm-before","prepare_render"))["ok"],"warm admitted without origin");
    Check(Now(c,Req(c,"params","sound_counter_origin",{{"bad",1}}))["error"]["code"]=="invalid_params","nonempty origin params accepted");
    auto stale=Req(c,"stale","sound_counter_origin");stale["expect"]["revision"]=0;
    Check(Now(c,stale)["error"]["code"]=="stale_observation","stale origin accepted");
    auto req=Req(c,"bind","sound_counter_origin"),before=c.Version(),response=Now(c,req);
    Check(response["ok"]&&response["result"]["bound"]&&response["result"]["observation"]["counter_origin_bound"],"origin failed");
    const auto receipt=response["result"]["counter_origin"];
    Check(receipt.size()==9&&receipt["origin_raw_calls"]==14&&receipt["raw_before"]==receipt["raw_after"],"full raw receipt missing");
    auto expected=receipt["before_state"];expected["sound_effects"]["counter_scope"]="experiment";expected["sound_effects"]["calls"]=0;
    Check(receipt["after_state"]==expected&&f.rawCalls==14&&f.count==1362&&f.untouched==0x12345678,"origin mutated original state or counter");
    Check(receipt["before_version"]==before&&receipt["after_version"]==c.Version()&&c.Version()["revision"]==2,"binding versions wrong");
    Check(f.events.back()["kind"]=="sound_counter_origin_bound"&&f.events.back()["payload"].size()==2&&f.events.back()["payload"]["counter_origin"]==receipt,"origin event mismatch");
    auto events=f.events.size();Check(Now(c,req)==response&&events==f.events.size(),"same ID repeated origin");
    auto conflict=req;conflict["params"]["x"]=1;Check(Now(c,conflict)["error"]["code"]=="request_id_conflict","conflicting retry allowed");
    Check(!Bind(c,"again")["ok"],"new ID rebound origin");
    Check(Now(c,seedReq)==seedReply&&f.seedCalls==1,"old seed retry executed again");
    for(auto method:{"rng_seed","rng_restore","clock_restore"})Check(Now(c,Req(c,std::string("seal-")+method,method,
        std::string(method)=="rng_seed"?Json{{"seed",7}}:Json{{"snapshot",Json::object()}}))["error"]["code"]=="initialization_sealed","post-origin init mutation allowed");
    auto anchored=Now(c,Req(c,"app","app_update_anchor",{{"app_update_count",1363}}));
    Check(anchored["ok"]&&anchored["result"]["anchor"]["before_state"]==receipt["after_state"],"origin/App full-state link missing");
    Check(Now(c,Req(c,"warm","prepare_render"))["ok"],"warm rejected after both operations");
    auto state=f.State();Check(state["sound_effects"]["calls"]==2&&f.rawCalls==16,"warm calls incorrectly discarded");
    Json envelope={{"seq",87},{"kind","pre_step"},{"version",c.Version()},{"payload",{{"engine_call",{{"engine_call_id",1}}}}}};
    auto raw=f.origin.Boundary(envelope,state["sound_effects"],f.lastRaw);
    Check(raw==Json{{"schema","lvz.sound-counter-raw.v1"},{"seq",87},{"kind","pre_step"},{"version",c.Version()},
        {"engine_call_id",1},{"raw_calls",16},{"origin_raw_calls",14},{"experiment_calls",2}},"same-sample boundary alignment/arithmetic wrong");
    auto wrong=state["sound_effects"];wrong["calls"]=3;
    Rejects([&]{f.origin.Boundary(envelope,wrong,f.lastRaw);},"mismatched semantic count accepted");
    auto staleRaw=f.lastRaw;staleRaw["calls"]=15;
    Rejects([&]{f.origin.Boundary(envelope,state["sound_effects"],staleRaw);},"stale raw sample accepted");
    f.CloseRecording();Check(f.closedHealth["healthy"]&&f.closedHealth["calls"]==2&&f.closedHealth["raw_calls"]==16&&f.closedHealth["origin_raw_calls"]==14,"relative closed counters incorrect");
    f.ResetRenderPreparation();Check(f.origin.Bound()&&!f.BindSoundCounterOrigin()["ok"],"epoch reset removed recording origin");
    Fake cold; cold.rawCalls=16;cold.seeded=true;auto coldReceipt=cold.BindSoundCounterOrigin();
    Check(coldReceipt["ok"]&&coldReceipt["counter_origin"]["after_state"]==receipt["after_state"],"different absolute origins did not produce equal actual relative state");
    for(unsigned rejection=0;rejection<7;++rejection){Fake b;Controller bc(b);bc.Boundary();Seed(bc);
        if(rejection==0)b.enabled=false;if(rejection==1)b.occupied=true;if(rejection==2)b.channel=true;
        if(rejection==3)b.badRng=true;if(rejection==4)b.warm=true;if(rejection==5)b.rawErrors=1;if(rejection==6)b.thread++;
        auto rejected=Bind(bc);Check(!rejected["ok"]&&!b.origin.Bound()&&b.rawCalls==14,"invalid origin precondition admitted/reset");
        if(rejection<5){b.enabled=true;b.occupied=b.channel=b.badRng=b.warm=false;Check(Bind(bc,"retry-corrected")["ok"],"guard failure consumed one-shot origin");}}
    for(unsigned failure=0;failure<3;++failure){Fake b;Controller bc(b);bc.Boundary();Seed(bc);
        b.mutateHistory=failure==0;b.throwAfter=failure==1;b.changedRaw=failure==2;
        auto rejected=Bind(bc);Check(rejected["error"]["code"]=="sound_counter_origin_failed"&&!b.origin.Bound(),"post-bind failure not latched");
        Check(!bc.ShouldStep()&&!Now(bc,Req(bc,"forbid","prepare_render"))["ok"],"post-bind failure admitted warm");
        Check(b.events.back()["payload"]["counter_origin"]["before_state"].is_object(),"failure lost original snapshot");
        b.mutateHistory=b.throwAfter=b.changedRaw=false;
        Check(Now(bc,Req(bc,"close","stop_recording"))["ok"]&&b.closed&&b.closedHealth["healthy"]==false,"fault evidence cannot close or became healthy");}
    Fake absent;absent.CloseRecording();Check(absent.closedHealth["healthy"]==false,"missing origin closed healthy");
    Fake regression;regression.seeded=true;Check(regression.BindSoundCounterOrigin()["ok"],"regression setup");regression.rawCalls=13;
    Rejects([&]{regression.State();},"raw regression/wrap accepted");regression.rawCalls=14;
    Rejects([&]{regression.State();},"regression fault forgotten after recovery");
    Fake wrap;wrap.rawCalls=UINT32_MAX;wrap.seeded=true;Check(wrap.BindSoundCounterOrigin()["ok"],"uint32 origin edge rejected");wrap.rawCalls=0;
    Rejects([&]{wrap.State();},"wrapped lifetime counter accepted");
    std::cout<<"sound origin relative snapshots, original-state preservation, same-sample raw evidence, lifecycle/retry/guards and fault close passed\n";return 0;
}catch(const std::exception& error){std::cerr<<error.what()<<'\n';return 1;}}
