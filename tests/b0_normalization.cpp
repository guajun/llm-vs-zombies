#include "runtime/controller.hpp"
#include "determinism/b0_normalization.hpp"
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <vector>
using namespace lvz::runtime;
using lvz::determinism::InitialB0Normalization;
void Check(bool ok,const char* why){if(!ok)throw std::runtime_error(why);}
constexpr uintptr_t AppUpdateOffset=0x484,MjClockOffset=0x838;
// The declared writes must touch exactly four bytes each; sentinels in the same
// simulated LawnApp prove no neighbouring byte is rewritten.
struct Fake:Backend {
    std::vector<uint8_t> app=std::vector<uint8_t>(0x900,0);
    InitialB0Normalization table;
    bool enabled=true,seeded=false,warm=false,fight=true,closed=false,counterBound=false,occupied=false,channel=false,badRng=false;
    bool mutateOutsider=false,throwAfter=false,faultEnabled=false;int captures=0,clock=3000,draws=0,seedCalls=0;uintptr_t board=1;
    uint32_t seed=42;
    std::vector<Json> events;
    Fake(){Set32(0x480,0x31415926u);Set32(0x488,0x27182818u);Set32(0x834,0x16180339u);Set32(0x83c,0x14142135u);
        Set32(AppUpdateOffset,1294);Set32(MjClockOffset,1307);}
    uint32_t Get32(uintptr_t offset)const{uint32_t value=0;std::memcpy(&value,app.data()+offset,4);return value;}
    void Set32(uintptr_t offset,uint32_t value){std::memcpy(app.data()+offset,&value,4);}
    bool Ready()const override{return fight;}
    uintptr_t BoardIdentity()const override{return board;}
    int NativeTick()const override{return clock;}
    bool UsesEngineCallBoundary()const override{return true;}
    Json Observe()override{return {{"game_clock",clock},{"wave",1}};}
    Json Hello()override{return Json::object();}
    Json Execute(const Json&)override{return {{"ok",true}};}
    bool RequiresRenderPreparation()const override{return true;}
    bool RenderPrepared()const override{return warm;}
    bool SupportsSoundCounterOrigin()const override{return enabled;}
    bool SoundCounterBound()const override{return counterBound;}
    Json BindSoundCounterOrigin()override{
        if(!enabled||!seeded||warm||counterBound)return {{"ok",false},{"error","origin rejected"}};
        counterBound=true;return {{"ok",true},{"counter_origin",{{"schema","lvz.sound-counter-origin.v1"}}},{"bind_attempted",true}};
    }
    bool SupportsB0Normalization()const override{return enabled;}
    bool B0Normalized()const override{return table.Applied();}
    bool SupportsAppUpdateAnchor()const override{return false;}
    bool SupportsMjClockAnchor()const override{return false;}
    void ResetRenderPreparation()override{table.Reset();warm=seeded=counterBound=false;}
    Json SeedRng(uint32_t value)override{seed=value;seeded=true;++seedCalls;return {{"ok",true}};}
    Json RestoreClocks(const Json&)override{return {{"ok",true}};}
    Json RestoreRng(const Json&)override{return {{"ok",true}};}
    Json State(){
        ++captures;if(faultEnabled&&throwAfter&&captures==2)throw std::runtime_error("snapshot failure after the table write");
        Json words=Json::array();uint32_t word=seed?seed:4357;
        for(unsigned i=0;i<624;++i){words.push_back(word);word=1812433253u*(word^(word>>30))+i+1;}
        if(badRng)words[623]=123;
        Json histories=Json::array();for(unsigned i=0;i<110;++i){Json slots=Json::array();for(unsigned j=0;j<8;++j)slots.push_back({0,0,0,800,3});histories.push_back({{"last_variation",i},{"slots",slots}});}
        if(occupied)histories[0]["slots"][0][0]=123;
        if(faultEnabled&&mutateOutsider&&captures==2)histories[109]["last_variation"]=200;
        Json channels=std::vector<int>(32,0);if(channel)channels[31]=1;
        return {{"schema","lvz.audit.v1"},{"rng",{{"instances",{{"global_mt",{{"words",words},{"cursor",624}}},
            {"game_thread_crt",{{"state",seed}}}}}}},
            {"app",{{"ui",3},{"mj_clock",Get32(MjClockOffset)}}},
            {"sound_effects",{{"mode","sound_effects_allocation_none_v1"},{"calls",5},{"errors",0},{"patch_owned",true},{"slots_empty",true},
                {"app_update_count",Get32(AppUpdateOffset)},{"histories",histories},{"parameters",{{"fixture",7}}},{"channels",channels}}},
            {"board",{{"game_clock",clock}}},{"unchanged_guard",0x12345678}};
    }
    Json NormalizeB0(const Json& entries)override{
        captures=0;
        return table.Apply(reinterpret_cast<uintptr_t>(app.data()),entries,seed,enabled,counterBound,seeded,warm,
            [&]{return State();},[]{return Json{{"00000510",0},{"00000511",0},{"00000578",17},{"0000049c",19},{"000004a0",23}};});
    }
    Json RenderFrame(const Json& version,bool isWarm)override{
        Check(!isWarm||!enabled||table.Applied(),"unanchored warm draw");warm=true;++draws;
        return {{"frame_version",version},{"phase",isWarm?"warm":"step"}};
    }
    void CloseRecording()override{closed=true;}
    void Audit(const std::string& kind,const Json& payload,const Json& observation)override{
        events.push_back({{"kind",kind},{"payload",payload},{"version",observation["version"]}});}
};
Json Req(Controller& c,const std::string& id,const std::string& method,Json params=Json::object()){
    return {{"protocol",1},{"request_id",id},{"method",method},{"params",params},{"expect",c.Version()}};
}
Json Now(Controller& c,const Json& request){std::optional<Json> response;c.Request(request,[&](Json r){response=std::move(r);});
    Check(response.has_value(),"unexpected pending request");return *response;}
Json Table(Controller& c,const std::string& id,Json entries){return Now(c,Req(c,id,"b0_normalization",{{"entries",entries}}));}
Json Both(uint32_t app,uint32_t mj){return Json::array({Json{{"field","/sound_effects/app_update_count"},{"target",app},{"reason","B(0) 前循环圈数"}},
    Json{{"field","/app/mj_clock"},{"target",mj},{"reason","同一循环的绝对计数"}}});}
void Seed(Controller& c){Check(Now(c,Req(c,"seed","rng_seed",{{"seed",42}}))["ok"],"fixture seed");}
void Origin(Controller& c){Check(Now(c,Req(c,"origin","sound_counter_origin"))["ok"],"fixture origin");}
int main(){try{
    Fake f;Controller c(f);c.Boundary();Seed(c);
    Check(!Table(c,"before-origin",Both(2048,2048))["ok"]&&f.Get32(AppUpdateOffset)==1294,"table before the counter origin was admitted");
    Origin(c);
    Check(Now(c,Req(c,"warm-before-table","prepare_render"))["error"]["code"]=="render_prepare_rejected","warm draw preceded the table");
    unsigned badIndex=0;
    for(Json bad:Json::array({Json::object(),Json::array(),Json::array({Json{{"field","/sound_effects/app_update_count"}}}),
        Json::array({Json{{"field","/board/00005568"},{"target",1},{"reason","outside"}}}),
        Json::array({Json{{"field","/sound_effects/app_update_count"},{"target",-1},{"reason","range"}}}),
        Json::array({Json{{"field","/sound_effects/app_update_count"},{"target",2147483648ull},{"reason","range"}}}),
        Json::array({Json{{"field","/sound_effects/app_update_count"},{"target",1},{"reason",""}}}),
        Json::array({Json{{"field","/sound_effects/app_update_count"},{"target",1},{"reason",std::string(201,'x')}}}),
        Json::array({Json{{"field","/sound_effects/app_update_count"},{"target",1},{"reason","line\nbreak"}}}),
        Json::array({Json{{"field","/app/mj_clock"},{"target",2048},{"reason","wrong order"}},
                     Json{{"field","/sound_effects/app_update_count"},{"target",2048},{"reason","wrong order"}}}),
        Json::array({Json{{"field","/sound_effects/app_update_count"},{"target",1},{"reason","a"},{"extra",1}}})})) {
        Check(!Table(c,"bad-"+std::to_string(badIndex++),bad)["ok"]&&f.Get32(AppUpdateOffset)==1294,"invalid table wrote state");}
    auto stale=Req(c,"stale","b0_normalization",{{"entries",Both(2048,2048)}});stale["expect"]["revision"]=0;
    Check(Now(c,stale)["error"]["code"]=="stale_observation","stale table accepted");
    auto request=Req(c,"table","b0_normalization",{{"entries",Both(2048,2048)}});auto before=c.Version();auto response=Now(c,request);
    Check(response["ok"]&&response["result"]["normalized"]&&f.Get32(AppUpdateOffset)==2048&&f.Get32(MjClockOffset)==2048,
        "actual declared table write failed");
    Check(f.Get32(0x480)==0x31415926u&&f.Get32(0x488)==0x27182818u&&f.Get32(0x834)==0x16180339u&&f.Get32(0x83c)==0x14142135u,
        "neighbouring bytes changed");
    const auto& receipt=response["result"]["normalization"];
    auto expected=receipt["before_state"];expected["sound_effects"]["app_update_count"]=2048;expected["app"]["mj_clock"]=2048;
    Check(expected==receipt["after_state"]&&receipt["schema"]=="lvz.b0-normalization.v1"
        &&receipt["mode"]=="initial_b0_normalization_v1"&&receipt["requested"]==Both(2048,2048),
        "receipt shape or full-state check failed");
    Check(receipt["before"][0]["value"]==1294&&receipt["before"][1]["value"]==1307
        &&receipt["after"][0]["value"]==2048&&receipt["after"][1]["value"]==2048,"receipt before/after values are not the real ones");
    Check(receipt["before_version"]==before&&receipt["after_version"]==c.Version()
        &&c.Version()["revision"]==before["revision"].get<uint64_t>()+1,"the table did not consume exactly one revision");
    Check(f.events.back()["kind"]=="b0_normalized"&&f.events.back()["payload"]["normalization"]==receipt,
        "native successful receipt not emitted");
    auto eventCount=f.events.size();
    Check(Now(c,request)==response&&f.events.size()==eventCount,"retry repeated the write or the event");
    request["params"]["entries"][0]["target"]=2049;
    Check(Now(c,request)["error"]["code"]=="request_id_conflict","request identity changed");
    Check(!Table(c,"table-again",Both(2049,2049))["ok"]&&f.Get32(AppUpdateOffset)==2048,"new-id repeat allowed");
    for(auto method:{"rng_seed","rng_restore","clock_restore"})Check(Now(c,Req(c,std::string("sealed-")+method,method,
        std::string(method)=="rng_seed"?Json{{"seed",7}}:Json{{"snapshot",Json::object()}}))["error"]["code"]=="initialization_sealed",
        "post-table initializer admitted");
    for(auto method:{"app_update_anchor","mj_clock_anchor"})Check(Now(c,Req(c,std::string("legacy-")+method,method,
        std::string(method)=="app_update_anchor"?Json{{"app_update_count",1}}:Json{{"mj_clock",1}}))["error"]["code"]=="legacy_anchor_retired",
        "legacy per-field anchor was accepted beside the table");
    Check(Now(c,Req(c,"warm","prepare_render"))["ok"]&&f.draws==1,"warm after the table failed");
    Check(!Table(c,"after-warm",Both(1,1))["ok"],"postwarm table allowed");
    std::optional<Json> advanced;c.Request(Req(c,"step","advance",{{"max_ticks",1}}),[&](Json r){advanced=r;});
    c.RunEngineFrame([&]{++f.clock;f.Set32(AppUpdateOffset,f.Get32(AppUpdateOffset)+1);});
    Check(advanced&&(*advanced)["ok"],"actual engine wrapper fixture failed");
    Check(!Table(c,"after-step",Both(1,1))["ok"]&&f.Get32(AppUpdateOffset)==2049,"poststep table allowed");
    for(unsigned rejection=0;rejection<5;++rejection){Fake bad;Controller b(bad);b.Boundary();Seed(b);
        if(rejection==4)bad.warm=true;else Origin(b);
        if(rejection==0)bad.enabled=false;if(rejection==1)bad.occupied=true;if(rejection==2)bad.channel=true;
        if(rejection==3)bad.badRng=true;
        Check(!Table(b,"reject",Both(2048,2048))["ok"]&&bad.Get32(AppUpdateOffset)==1294&&!bad.table.Applied(),
            "native precondition rejection wrote state");}
    for(unsigned failure=0;failure<2;++failure){Fake bad;bad.mutateOutsider=failure==0;bad.throwAfter=failure==1;
        Controller b(bad);b.Boundary();Seed(b);Origin(b);bad.faultEnabled=true;
        auto failed=Table(b,"fault",Both(2048,2048));
        Check(failed["error"]["code"]=="b0_normalization_failed"&&bad.Get32(AppUpdateOffset)==2048,
            "partial table write was rolled back or hidden");
        Check(!b.ShouldStep()&&!Now(b,Req(b,"forbidden-warm","prepare_render"))["ok"],"fault admitted warm");
        Check(bad.events.back()["kind"]=="b0_normalization_failed"
            &&!bad.events.back()["payload"]["normalization"]["before_state"].is_null(),"failure lost pre-write state");
        Check(Now(b,Req(b,"close","stop_recording"))["ok"]&&bad.closed,"fault evidence could not close");}
    // Two worlds with different real counters and one declared table.
    Fake source;source.Set32(AppUpdateOffset,1295);source.Set32(MjClockOffset,1307);
    Controller s(source);s.Boundary();Seed(s);Origin(s);
    auto sourceResult=Table(s,"common",Both(2048,2048));
    Fake cold;cold.Set32(AppUpdateOffset,1422);cold.Set32(MjClockOffset,1340);
    Controller d(cold);d.Boundary();Seed(d);Origin(d);
    auto coldResult=Table(d,"common",Both(2048,2048));
    Check(sourceResult["ok"]&&coldResult["ok"]&&source.Get32(AppUpdateOffset)==2048&&cold.Get32(MjClockOffset)==2048,
        "cross-run declared targets were not written");
    Check(sourceResult["result"]["normalization"]["before"]!=coldResult["result"]["normalization"]["before"]
        &&sourceResult["result"]["normalization"]["after"]==coldResult["result"]["normalization"]["after"],
        "cross-run before/after semantics changed");
    for(uint32_t edge:{0u,uint32_t(INT32_MAX)}){Fake value;Controller v(value);v.Boundary();Seed(v);Origin(v);
        Check(Table(v,"edge",Both(edge,edge))["ok"]&&value.Get32(AppUpdateOffset)==edge,"signed permitted boundary rejected");}
    for(auto items:{std::string("{\"field\":\"/sound_effects/app_update_count\",\"target\":2048,\"reason\":\"only\"}")}) {
        Fake single;Controller v(single);v.Boundary();Seed(v);Origin(v);
        Check(Table(v,"single",Json::parse("["+items+"]"))["ok"],"a single declared field is not an admissible table");
    }
    std::cout<<"Unified B(0) table, one revision, one receipt, declared order, fail-closed shape checks, "
               "cross-run targets, lifecycle/idempotence and failure fixtures passed\n";return 0;
}catch(const std::exception& error){std::cerr<<error.what()<<'\n';return 1;}}
