#include "runtime/controller.hpp"
#include "runtime/pump_guard.hpp"
#include <iostream>
#include <stdexcept>
using namespace lvz::runtime;
void Check(bool value,const char* message){if(!value)throw std::runtime_error(message);}
struct Rendering:Backend {
    bool prepared=false,seeded=false,throwDraw=false,cache=false,closed=false;
    int clock=20,draws=0,actions=0;uintptr_t board=1;bool fight=true;
    Json cacheVersion,lastPre,drawBoundary;std::vector<std::string> order;std::vector<Json> events;
    bool Ready()const override{return fight;}
    uintptr_t BoardIdentity()const override{return board;}
    int NativeTick()const override{return clock;}
    Json Observe()override{return {{"game_clock",clock},{"wave",1}};}
    Json Hello()override{return Json::object();}
    Json Execute(const Json&)override{++actions;return {{"ok",true}};}
    Json SeedRng(uint32_t)override{seeded=true;return {{"ok",true}};}
    bool RequiresRenderPreparation()const override{return true;}
    bool RenderPrepared()const override{return prepared;}
    void InvalidateFrame(const std::string&)override{cache=false;}
    void ResetRenderPreparation()override{prepared=seeded=cache=false;}
    Json RenderFrame(const Json& version,bool warm)override{
        Check(warm?order.back()=="render_preparing":order.back()=="pre_step","audit reset hook boundary before draw");
        if(warm&&!seeded)throw std::runtime_error("seed required");
        ++draws;cache=false;drawBoundary=lastPre;
        if(throwDraw)throw std::runtime_error("surface failure");
        prepared=true;cache=true;cacheVersion=version;return {{"phase",warm?"warm":"step"},{"frame_version",version},{"native_clock",clock}};
    }
    Json CaptureFrame(const Json& params)override{return {{"capture_ok",cache&&params.at("frame_version")==cacheVersion},{"forced_render",false}};}
    void CloseRecording()override{closed=true;}
    void Audit(const std::string& kind,const Json& payload,const Json& observation)override{
        order.push_back(kind);events.push_back({{"kind",kind},{"payload",payload},{"version",observation.at("version")}});
        if(kind=="pre_step")lastPre=observation.at("version");
    }
};
Json Req(Controller& c,const std::string& id,const std::string& method,Json params=Json::object()) {
    return {{"protocol",1},{"request_id",id},{"method",method},{"params",params},{"expect",c.Version()}};
}
Json Now(Controller& c,const Json& req){std::optional<Json> result;c.Request(req,[&](Json value){result=std::move(value);});Check(result.has_value(),"unexpected pending");return *result;}
void Prepare(Controller& c){Check(Now(c,Req(c,"seed","rng_seed",{{"seed",42}}))["ok"],"seed failed");Check(Now(c,Req(c,"warm","prepare_render"))["ok"],"warm failed");}
int main(){try{
    Rendering f;Controller c(f);c.Boundary();
    Check(Now(c,Req(c,"early","advance",{{"max_ticks",1}}))["error"]["code"]=="render_not_prepared","unprepared update admitted");
    Check(!c.ShouldStep()&&f.draws==0,"rejection ran engine");
    Now(c,Req(c,"seed","rng_seed",{{"seed",42}}));auto warm=Req(c,"warm","prepare_render");auto response=Now(c,warm);
    Check(response["ok"]&&f.draws==1&&c.Version()["tick"]==0&&c.Version()["revision"]==2,"B0 version/draw wrong");
    Check(Now(c,warm)==response&&f.draws==1,"same-ID prepare drew twice");
    Check(Now(c,Req(c,"again","prepare_render"))["error"]["code"]=="render_prepare_rejected"&&f.draws==1,"new-ID prepare drew twice");
    Now(c,Req(c,"pause","pause"));Now(c,Req(c,"obs","observe"));Now(c,Req(c,"status","status"));
    Check(Now(c,Req(c,"pic1","capture_frame"))["result"]["capture_ok"]&&Now(c,Req(c,"pic2","capture_frame"))["result"]["capture_ok"]&&f.draws==1,"idle/capture redrew");
    Now(c,Req(c,"action","commit",{{"actions",Json::array({Json{{"op","fixture"}}})},{"advance_ticks",0}}));
    Check(!Now(c,Req(c,"stale-pic","capture_frame"))["result"]["capture_ok"],"action reused stale image");
    auto advance=Req(c,"advance","advance",{{"max_ticks",2}});std::optional<Json> completed;
    c.Request(advance,[&](Json result){completed=std::move(result);});
    for(int index=0;index<2;++index){auto pre=c.Version();c.BeforeStep();++f.clock;c.AfterStep();Check(f.drawBoundary==pre,"draw hook label shifted to post/next tick");}
    Check(completed&&(*completed)["result"]["executed_ticks"]==2&&f.draws==3,"batch did not draw once per update");
    Check(f.order[f.order.size()-2]=="post_step","post snapshot not after draw");
    Now(c,advance);Check(f.draws==3,"retry redrew completed frames");
    Check(Now(c,Req(c,"latest","capture_frame"))["result"]["capture_ok"],"completed frame unavailable");
    f.throwDraw=true;std::optional<Json> failed;c.Request(Req(c,"failed","advance",{{"max_ticks",4}}),[&](Json r){failed=r;});
    auto eventsBefore=f.events.size();c.BeforeStep();++f.clock;c.AfterStep();
    Check(failed&&(*failed)["error"]["code"]=="audit_failed"&&(*failed)["error"]["details"]["executed_ticks"]==1,"partial render failure hid advanced tick");
    Check(!c.ShouldStep()&&!f.cache&&f.order.back()=="render_failed","render failure did not freeze without successful post");
    for(size_t i=eventsBefore;i<f.events.size();++i)Check(f.events[i]["kind"]!="post_step","failed draw fabricated post audit");
    Check(Now(c,Req(c,"close","stop_recording"))["ok"]&&f.closed,"failed draw blocked evidence closure");
    Rendering zero;Controller z(zero);z.Boundary();Prepare(z);std::optional<Json> noTick;
    z.Request(Req(z,"zero","advance",{{"max_ticks",1}}),[&](Json r){noTick=r;});z.BeforeStep();z.AfterStep();
    Check(zero.draws==1&&!zero.cache&&(*noTick)["error"]["code"]=="no_game_tick","unverified update drew a frame");
    Rendering terminal;Controller t(terminal);t.Boundary();Prepare(t);std::optional<Json> ended;
    t.Request(Req(t,"terminal","advance",{{"max_ticks",2}}),[&](Json r){ended=r;});t.BeforeStep();++terminal.clock;terminal.fight=false;t.AfterStep();
    Check(ended&&(*ended)["result"]["executed_ticks"]==1&&(*ended)["result"]["stop_reason"]=="scene_changed","terminal verified tick lost");
    Check(terminal.draws==1&&!terminal.cache&&!t.ShouldStep(),"terminal attempted rendering or reopened pump");
    bool skipped=false;
    for(const auto& event:terminal.events)if(event["kind"]=="post_step")
        skipped=event["payload"]["render"]["phase"]=="terminal"&&event["payload"]["render"]["skipped"]==true;
    Check(skipped,"terminal post lacks explicit skipped-render receipt");
    Rendering gate;Controller g(gate);g.Boundary();Prepare(g);std::optional<Json> interrupted;
    g.Request(Req(g,"pending","advance",{{"max_ticks",20}}),[&](Json value){interrupted=std::move(value);});
    int drained=0;
    auto broken=[] {throw std::runtime_error("permanent draw gate fault");};
    Check(!CheckPumpGate(g,broken,[&] {
        ++drained;
        Check(Now(g,Req(g,"fault-status","status"))["result"]["state"]=="audit_failed","gate fault status unavailable");
        Check(Now(g,Req(g,"fault-pause","pause"))["ok"],"gate fault pause unavailable");
        Check(Now(g,Req(g,"blocked-init","initialize"))["error"]["code"]=="audit_failed","gate fault admitted initialization");
        Check(Now(g,Req(g,"fault-close","stop_recording"))["ok"],"gate fault closure unavailable");
    }),"permanent gate fault reopened native update");
    Check(interrupted&&(*interrupted)["error"]["details"]["executed_ticks"]==0&&gate.draws==1&&gate.clock==20&&gate.closed,"gate fault advanced or hid pending request");
    Check(!CheckPumpGate(g,broken,[&]{++drained;Check(Now(g,Req(g,"closed-status","status"))["ok"],"closed fault pump stopped");})&&drained==2,"repeated fault failed to service IPC");
    std::cout<<"render preparation, preserved pre-label, exactly-once post order, cache invalidation and partial-failure counts passed\n";return 0;
}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
