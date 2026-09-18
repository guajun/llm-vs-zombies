#include "controller.hpp"
#include "pipe_server.hpp"
#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <thread>
#include <chrono>
#include <atomic>
using namespace lvz::runtime;
void Check(bool value,const char* message) {if(!value) throw std::runtime_error(message);}
struct Fake final:Backend {
    bool fight=true; int clock=40,wave=1,actions=0; std::uintptr_t board=1; std::vector<std::string> audit;
    std::vector<Json> evidence;
    bool acceptInitialize=false,acceptState=true;
    uint32_t seed=7;
    int seedCalls=0,clockCalls=0,initializeCalls=0;
    int captureCalls=0;
    size_t captureBytes=8;
    bool captureChangesRng=false,captureThrows=false;
    std::string auditThrowKind;
    bool Ready()const override{return fight;}
    std::uintptr_t BoardIdentity()const override{return board;}
    int NativeTick()const override{return clock;}
    Json Observe()override{return {{"game_clock",clock},{"game_ui",fight?3:2},{"wave",wave},{"actions",actions}};}
    Json Execute(const Json& a)override{if(a.value("fail",false))return {{"ok",false}};++actions;return {{"ok",true}};}
    Json Hello()override{return {{"session","test"}};}
    Json Initialize(const Json&)override{
        ++initializeCalls;
        if(!acceptInitialize)return {{"ok",false},{"error","fixture initialization denied"}};
        fight=false;board=0;return {{"ok",true}};
    }
    Json SeedRng(uint32_t value)override{
        ++seedCalls;if(!acceptState)return {{"ok",false},{"error","fixture seed denied"}};
        seed=value;return {{"ok",true}};
    }
    Json RestoreClocks(const Json& snapshot)override{
        ++clockCalls;if(!acceptState)return {{"ok",false},{"error","fixture clock restore denied"}};
        clock=snapshot.at("game_clock").get<int>();return {{"ok",true}};
    }
    Json CaptureFrame(const Json&)override{
        ++captureCalls;
        if(captureThrows)throw std::runtime_error("fixture render failed");
        if(captureChangesRng)++seed;
        return {{"capture_ok",true},{"source","original_game_frame"},{"width",2},{"height",1},
            {"pixel_format","bgr24"},{"row_stride",6},{"origin","top_left"},{"method","fixture"},
            {"pixels_base64",std::string(captureBytes,'A')},{"forced_render",false},{"used_3d",false},
            {"known_rng_unchanged",!captureChangesRng},{"game_clock_before",clock},{"game_clock_after",clock}};
    }
    void Audit(const std::string& kind,const Json& payload,const Json& observation)override{
        if(kind==auditThrowKind) throw std::runtime_error("fixture audit unavailable");
        audit.push_back(kind);evidence.push_back({{"kind",kind},{"payload",payload},{"observation",observation}});
    }
};
Json Request(Controller& c,const std::string& id,const std::string& method,Json params=Json::object()) {
    return {{"protocol",1},{"request_id",id},{"method",method},{"params",params},{"expect",c.Version()}};
}
Json Immediate(Controller& c,const Json& req) {
    std::optional<Json> reply;c.Request(req,[&](Json r){reply=r;});Check(reply.has_value(),"response unexpectedly pending");return *reply;
}
void Step(Controller& c,Fake& f,int delta=1){Check(c.ShouldStep(),"step has no budget");c.BeforeStep();f.clock+=delta;c.AfterStep();}
void CoreTests() {
    Fake f;Controller c(f);c.Boundary();Check(!c.ShouldStep(),"initial fight must pause");
    auto req=Request(c,"one","advance",{{"max_ticks",1}});std::optional<Json> reply,retry;
    c.Request(req,[&](Json r){reply=r;});c.Request(req,[&](Json r){retry=r;});Check(!reply&&!retry,"advance acknowledged before simulation");
    auto conflict=req;conflict["params"]["max_ticks"]=2;
    Check(Immediate(c,conflict)["error"]["code"]=="request_id_conflict","pending ID conflict not rejected");
    Step(c,f);Check(reply&&retry&&*reply==*retry,"pending retry does not receive same result");
    Check((*reply)["result"]["executed_ticks"]==1&&f.clock==41&&!c.ShouldStep(),"one-step barrier failed");
    Check(Immediate(c,req)==*reply,"completed mutation replayed or changed");
    auto stale=Request(c,"stale","advance",{{"max_ticks",1}});stale["expect"]["tick"]=0;
    Check(Immediate(c,stale)["error"]["code"]=="stale_observation","stale version accepted");
    auto failed=Immediate(c,Request(c,"partial","commit",{{"actions",Json::array({Json::object(),Json{{"fail",true}},Json::object()})},{"advance_ticks",3}}));
    Check(f.actions==1&&failed["result"]["action_results"].size()==2&&failed["result"]["executed_ticks"]==0,"partial failure did not stop");
    Check(c.Version()["revision"]==2,"same-tick actions did not invalidate observation");
    auto p=Request(c,"long","advance",{{"max_ticks",100000}});std::optional<Json> longReply;
    c.Request(p,[&](Json r){longReply=r;});Step(c,f);Step(c,f);
    auto cancel=Request(c,"cancel","cancel");cancel.erase("expect");auto cr=Immediate(c,cancel);
    Check(cr["ok"]==true&&longReply&&(*longReply)["result"]["executed_ticks"]==2&&(*longReply)["result"]["stop_reason"]=="cancelled","cancel did not stop at boundary");
    std::optional<Json> badStep;c.Request(Request(c,"no-tick","advance",{{"max_ticks",3}}),[&](Json r){badStep=r;});Step(c,f,0);
    Check((*badStep)["error"]["details"]["executed_ticks"]==0&&(*badStep)["error"]["code"]=="no_game_tick","zero tick was reported as one");
    c.Request(Request(c,"multi-tick","advance",{{"max_ticks",3}}),[&](Json r){badStep=r;});Step(c,f,2);
    Check((*badStep)["error"]["details"]["executed_ticks"]==2&&(*badStep)["error"]["code"]=="step_count_mismatch","multiple native ticks hidden");
    auto epoch=c.Version()["epoch"];f.board=2;f.clock=0;c.Boundary();Check(c.Version()["epoch"]!=epoch&&c.Version()["tick"]==0,"board replacement must change epoch");
    f.fight=false;c.Boundary();Check(!c.ShouldStep(),"an external terminal transition must remain frozen");
    Check(Immediate(c,Request(c,"menu","advance",{{"max_ticks",1}}))["error"]["code"]=="not_in_fight","menu advance accepted");
    f.fight=true;c.Boundary();
    Check(!Immediate(c,Request(c,"oversize","advance",{{"max_ticks",100001}}))["ok"].get<bool>(),"tick bound absent");
    auto malformed=Request(c,"bad","commit");malformed["params"]=nullptr;Check(!Immediate(c,malformed)["ok"].get<bool>(),"malformed params accepted");
    Fake single,batch;Controller cs(single),cb(batch);cs.Boundary();cb.Boundary();
    for(int i=0;i<100;++i){std::optional<Json> r;cs.Request(Request(cs,std::to_string(i),"advance",{{"max_ticks",1}}),[&](Json v){r=v;});Step(cs,single);Check(r.has_value(),"single response absent");}
    std::optional<Json> br;cb.Request(Request(cb,"batch","advance",{{"max_ticks",100}}),[&](Json v){br=v;});for(int i=0;i<100;++i)Step(cb,batch);
    Check(cs.Observe()==cb.Observe()&&br&&single.audit.size()==400&&batch.audit.size()==202,"100x1 and 1x100 boundaries diverged");
    std::optional<Json> waveReply;cb.Request(Request(cb,"wave","advance",{{"max_ticks",100},{"until",{{"event","wave_changed"}}}}),[&](Json v){waveReply=v;});batch.wave++;Step(cb,batch);
    Check(waveReply&&(*waveReply)["result"]["stop_reason"]=="wave_changed","wave condition ignored");
    Check(Immediate(cb,Request(cb,"close","stop_recording"))["result"]["closed"]==true,"close recording failed");
    Check(Immediate(cb,Request(cb,"after-close","advance",{{"max_ticks",1}}))["error"]["code"]=="recording_closed","closed recorder accepted a mutation");
}
void TerminalTests() {
    Fake menu;menu.fight=false;menu.board=0;Controller startup(menu);startup.Boundary();
    Check(startup.ShouldStep(),"initial menu must be able to initialize");
    Fake f;Controller c(f);c.Boundary();auto epoch=c.Version()["epoch"];
    std::optional<Json> reply;Json callbackVersion;
    c.Request(Request(c,"terminal","advance",{{"max_ticks",10}}),[&](Json value){reply=value;callbackVersion=c.Version();});
    c.BeforeStep();++f.clock;f.fight=false;c.AfterStep();
    Check(reply&&(*reply)["ok"]==true,"verified terminal response missing");
    auto result=(*reply)["result"];
    Check(result["stop_reason"]=="scene_changed"&&result["executed_ticks"]==1,"terminal native tick was lost");
    Check(result["observation"]["version"]["epoch"]==epoch&&result["observation"]["version"]["tick"]==1,
          "terminal response did not retain measured epoch/tick");
    Check(callbackVersion==result["observation"]["version"],"epoch reset before completion delivery");
    Check(f.audit==std::vector<std::string>({"request_started","pre_step","post_step","terminal_transition","request_completed"}),
          "terminal audit ordering changed");
    auto transition=f.evidence[3];
    Check(transition["payload"]==Json{{"request_id","terminal"},{"native_tick_delta",1},
          {"tick_delta_verified",true},{"board_identity_preserved",true}},"verified terminal metadata incorrect");
    Check(transition["observation"]["version"]==result["observation"]["version"],"terminal audit used a different boundary");
    Check(c.Version()["epoch"]!=epoch&&c.Version()["tick"]==0&&!c.ShouldStep(),"terminal did not freeze/reset next epoch");
    Check(c.Status()["state"]=="terminal_frozen","terminal status is ambiguous");
    for(int i=0;i<10;++i)c.Boundary();
    Check(!c.ShouldStep()&&f.clock==41,"terminal menu was allowed to run");
    Check(Immediate(c,Request(c,"terminal-advance","advance",{{"max_ticks",1}}))["error"]["code"]=="not_in_fight",
          "frozen terminal accepted an unfulfillable advance");
    Check(Immediate(c,Request(c,"denied-init","initialize"))["error"]["code"]=="initialization_rejected"&&!c.ShouldStep(),
          "failed initialization released the terminal freeze");
    f.acceptInitialize=true;
    Check(Immediate(c,Request(c,"new-init","initialize"))["ok"]==true&&c.ShouldStep(),"successful initialization did not release terminal freeze");
    c.Boundary();f.board=3;f.fight=true;f.clock=10;c.Boundary();
    Check(!c.ShouldStep()&&c.Status()["state"]=="paused_at_boundary","new fight did not pause after initialization");

    for(int badDelta:{0,2,-1}) {
        Fake wrong;Controller measured(wrong);measured.Boundary();std::optional<Json> stopped;
        measured.Request(Request(measured,"wrong-delta","advance",{{"max_ticks",10}}),[&](Json value){stopped=value;});
        measured.BeforeStep();wrong.clock+=badDelta;wrong.fight=false;measured.AfterStep();
        Check(stopped&&(*stopped)["result"]["stop_reason"]=="scene_changed","uncertain terminal did not complete");
        Check(wrong.evidence[3]["payload"]["tick_delta_verified"]==false
              &&wrong.evidence[3]["payload"]["native_tick_delta"]==badDelta,"invalid terminal delta certified");
        Check(!measured.ShouldStep(),"uncertain terminal must stay frozen");
    }
    for(auto newBoard:{std::uintptr_t(0),std::uintptr_t(2)}) {
        Fake replaced;Controller measured(replaced);measured.Boundary();std::optional<Json> stopped;
        measured.Request(Request(measured,"replaced","advance",{{"max_ticks",10}}),[&](Json value){stopped=value;});
        measured.BeforeStep();replaced.board=newBoard;replaced.clock=500;replaced.fight=newBoard!=0;measured.AfterStep();
        Check(replaced.audit==std::vector<std::string>({"request_started","pre_step","terminal_transition","request_completed"}),
              "destroyed/replaced Board fabricated post_step evidence");
        Check(replaced.evidence[2]["payload"]["tick_delta_verified"]==false
              &&replaced.evidence[2]["payload"]["board_identity_preserved"]==false
              &&replaced.evidence[2]["payload"]["native_tick_delta"].is_null(),"replacement clock was compared with original Board");
        Check((*stopped)["result"]["executed_ticks"]==0&&!measured.ShouldStep(),"unmeasured terminal tick was counted");
        Check(Immediate(measured,Request(measured,"after-replaced","advance",{{"max_ticks",1}}))["error"]["code"]=="not_in_fight",
              "replacement Board accepted a pending advance while frozen");
    }
}
void InitializationStateTests() {
    Fake f;Controller c(f);c.Boundary();
    auto missing=Request(c,"no-expect","rng_seed",{{"seed",0}});missing.erase("expect");
    Check(Immediate(c,missing)["error"]["code"]=="stale_observation"&&f.seedCalls==0,"seed without exact expect executed");
    auto req=Request(c,"seed-max","rng_seed",{{"seed",UINT32_MAX}});
    auto seeded=Immediate(c,req);
    Check(seeded["ok"]==true&&f.seed==UINT32_MAX&&c.Version()["revision"]==1,"full uint32 seed was truncated/rejected");
    Check(f.audit.back()=="rng_seeded"&&f.evidence.back()["payload"]["seed"]==UINT32_MAX,"seed audit missing actual value");
    Check(Immediate(c,req)==seeded&&f.seedCalls==1,"seed retry mutated state twice");
    auto conflict=req;conflict["params"]["seed"]=0;
    Check(Immediate(c,conflict)["error"]["code"]=="request_id_conflict"&&f.seedCalls==1,"changed seed reused request identity");
    std::vector<Json> invalid={-1,Json(uint64_t(UINT32_MAX)+1),true,1.5,nullptr,"123"};
    int number=0;
    for(const auto& seed:invalid) {
        auto result=Immediate(c,Request(c,"invalid-seed-"+std::to_string(number++),"rng_seed",{{"seed",seed}}));
        Check(result["error"]["code"]=="invalid_params"&&f.seedCalls==1&&c.Version()["revision"]==1,"invalid seed reached backend");
    }
    auto before=c.Version();
    auto restore=Request(c,"clock-init","clock_restore",{{"snapshot",{{"game_clock",0}}}});
    auto restored=Immediate(c,restore);
    Check(restored["ok"]==true&&f.clock==0&&f.clockCalls==1&&c.Version()["revision"]==2,"clock sidecar was not restored");
    c.Boundary();
    Check(c.Version()["epoch"]==before["epoch"]&&c.Version()["tick"]==0&&c.Version()["revision"]==2,
          "authorized initial clock restore was mistaken for an external epoch reset");
    Check(f.audit.back()=="clocks_restored"&&f.evidence.back()["payload"]["snapshot"]["game_clock"]==0,"clock audit missing snapshot");
    Check(Immediate(c,restore)==restored&&f.clockCalls==1,"clock retry restored twice");
    auto stale=Request(c,"old-state","rng_seed",{{"seed",1}});stale["expect"]=before;
    Check(Immediate(c,stale)["error"]["code"]=="stale_observation","state mutation accepted stale revision");
    Check(Immediate(c,Request(c,"bad-snapshot","clock_restore",{{"snapshot",nullptr}}))["error"]["code"]=="invalid_params"
          &&f.clockCalls==1,"invalid clock schema reached backend");
    f.acceptState=false;auto revision=c.Version()["revision"];auto auditCount=f.audit.size();
    Check(Immediate(c,Request(c,"denied-clock","clock_restore",{{"snapshot",{{"game_clock",50}}}}))["error"]["code"]=="clock_restore_rejected",
          "backend clock rejection hidden");
    Check(c.Version()["revision"]==revision&&f.clock==0&&f.audit.size()==auditCount,"rejected clock restore claimed mutation");
    f.acceptState=true;
    std::optional<Json> pending;c.Request(Request(c,"advance","advance",{{"max_ticks",2}}),[&](Json value){pending=value;});
    int calls=f.seedCalls;
    Check(Immediate(c,Request(c,"seed-busy","rng_seed",{{"seed",0}}))["error"]["code"]=="busy"&&f.seedCalls==calls,
          "seed changed an advancing simulation");
    Step(c,f);Step(c,f);Check(pending&&f.clock==2&&c.Version()["tick"]==2,"clock restore broke subsequent measured stepping");
    f.fight=false;c.Boundary();calls=f.seedCalls;
    Check(Immediate(c,Request(c,"seed-menu","rng_seed",{{"seed",0}}))["error"]["code"]=="not_in_fight"&&f.seedCalls==calls,
          "seed changed state outside a paused fight");
    Check(f.Backend::SeedRng(0)["ok"]==false&&f.Backend::RestoreClocks(Json::object())["ok"]==false,
          "default backend state methods must remain unsupported");
}
void CaptureTests() {
    Fake f;Controller c(f);c.Boundary();
    auto request=Request(c,"frame-first","capture_frame");
    auto response=Immediate(c,request);
    Check(response["ok"]==true&&response["result"]["capture_ok"]==true&&response["result"]["version"]==c.Version(),"capture response/version missing");
    Check(c.Version()["revision"]==0&&f.audit.empty(),"unchanged capture mutated revision or wrote pixels to audit");
    Check(Immediate(c,request)==response&&f.captureCalls==1,"capture retry rendered twice");
    auto changed=request;changed["params"]["force_render"]=true;
    Check(Immediate(c,changed)["error"]["code"]=="request_id_conflict"&&f.captureCalls==1,"capture ID accepted changed payload");
    auto noExpect=Request(c,"capture-no-expect","capture_frame");noExpect.erase("expect");
    Check(Immediate(c,noExpect)["error"]["code"]=="stale_observation","capture omitted exact expect");
    auto bigRequest=Request(c,"capture-large-request","capture_frame",{{"unused",std::string(5000,'x')}});
    Check(Immediate(c,bigRequest)["error"]["code"]=="invalid_params"&&f.captureCalls==1,"unbounded capture metadata accepted");
    f.captureBytes=1920000;
    for(int i=0;i<40;++i) {
        auto frame=Immediate(c,Request(c,"large-frame-"+std::to_string(i),"capture_frame"));
        Check(frame["ok"]==true&&frame["result"]["capture_ok"]==true,"large capture exhausted action dedup memory");
    }
    Check(Immediate(c,request)["error"]["code"]=="request_result_expired"&&f.captureCalls==41,
          "expired capture was repeated or retained forever");
    auto recent=Request(c,"large-frame-39","capture_frame");
    Check(Immediate(c,recent)["result"]["capture_ok"]==true&&f.captureCalls==41,"recent capture cache was lost");
    auto collision=Request(c,"frame-first","commit",{{"actions",Json::array()},{"advance_ticks",0}});
    Check(Immediate(c,collision)["error"]["code"]=="request_id_conflict","capture tombstone did not protect cross-method ID reuse");
    auto action=Immediate(c,Request(c,"action-after-frames","commit",{{"actions",Json::array({Json::object()})},{"advance_ticks",0}}));
    Check(action["ok"]==true&&f.actions==1,"capture traffic blocked ordinary actions");
    f.captureBytes=8;f.captureChangesRng=true;
    auto before=c.Version();auto disturbed=Immediate(c,Request(c,"disturbed","capture_frame"));
    Check(disturbed["result"]["capture_ok"]==false&&disturbed["result"]["reason"]=="capture_changed_game_state"
          &&!disturbed["result"].contains("pixels_base64")&&c.Version()["revision"]==before["revision"].get<int>()+1,
          "capture side effect was hidden behind success/stale version");
    f.captureChangesRng=false;f.captureThrows=true;before=c.Version();
    auto failed=Immediate(c,Request(c,"capture-failed","capture_frame"));
    Check(failed["error"]["code"]=="capture_failed"&&c.Version()["revision"]==before["revision"].get<int>()+1,
          "uncertain capture exception did not invalidate observation");
    f.captureThrows=false;
    std::optional<Json> pending;c.Request(Request(c,"capture-budget","advance",{{"max_ticks",5}}),[&](Json value){pending=value;});
    int captures=f.captureCalls;
    Check(Immediate(c,Request(c,"capture-busy","capture_frame"))["error"]["code"]=="busy"&&f.captureCalls==captures,
          "capture executed during advancement");
    c.Stop("cancelled");
    Immediate(c,Request(c,"capture-close","stop_recording"));
    Check(Immediate(c,Request(c,"capture-after-close","capture_frame"))["error"]["code"]=="recording_closed"
          &&f.captureCalls==captures,"capture ignored closed recording");
    Check(f.Backend::CaptureFrame(Json::object())["capture_ok"]==false,"default capture backend must remain unsupported");
}
void AuditFailureTests() {
    for(const auto& phase:{"request_started","pre_step","post_step","request_completed"}) {
        Fake f;Controller c(f);c.Boundary();f.auditThrowKind=phase;
        auto request=Request(c,"audit-failure","advance",{{"max_ticks",1}});
        std::optional<Json> response;int replies=0;
        c.Request(request,[&](Json value){response=std::move(value);++replies;});
        if(!response) try { Step(c,f); } catch(const std::exception&) {}
        Check(response&&(*response)["error"]["code"]=="audit_failed"&&replies==1,"audit failure did not resolve the original request exactly once");
        Check(!c.ShouldStep()&&c.Status()["state"]=="audit_failed","audit failure did not freeze simulation");
        Check((*response)["error"]["details"]["executed_ticks"]==((std::string(phase)=="post_step"||std::string(phase)=="request_completed")?1:0),"audit failure lost actual advancement");
        Check(Immediate(c,request)==*response,"audit failure duplicate lost its immutable result");
        Check(Immediate(c,Request(c,"new-mutation","advance",{{"max_ticks",1}}))["error"]["code"]=="audit_failed","failed audit allowed another mutation");
        f.board=2;f.clock=0;c.Boundary();
        Check(!c.ShouldStep()&&Immediate(c,request)==*response,"board change cleared a failed run's dedup result");
        f.auditThrowKind.clear();
        Check(Immediate(c,Request(c,"seal-failed-run","stop_recording"))["result"]["closed"]==true,"failed run could not close its evidence");
    }
}
void PipeTests() {
    Fake f;Controller c(f);c.Boundary();PipeServer server;server.Start();
    std::atomic<bool> done=false;std::exception_ptr error;
    std::thread client([&]{try{
        auto endpoint=L"\\\\.\\pipe\\llm-vs-zombies-"+std::to_wstring(GetCurrentProcessId());HANDLE h=INVALID_HANDLE_VALUE;
        for(int i=0;i<200&&h==INVALID_HANDLE_VALUE;++i){h=CreateFileW(endpoint.c_str(),GENERIC_READ|GENERIC_WRITE,0,nullptr,OPEN_EXISTING,0,nullptr);if(h==INVALID_HANDLE_VALUE)Sleep(5);}
        Check(h!=INVALID_HANDLE_VALUE,"pipe did not start");
        auto exchange=[&](const std::string& body) {
            uint32_t n=static_cast<uint32_t>(body.size());DWORD count;
            // Exercise partial headers AND partial payloads over a real Windows pipe.
            auto* header=reinterpret_cast<char*>(&n);for(int i=0;i<4;++i)Check(WriteFile(h,header+i,1,&count,nullptr)&&count==1,"header write");
            for(char ch:body)Check(WriteFile(h,&ch,1,&count,nullptr)&&count==1,"payload write");
            auto read=[&](char* out,DWORD size){while(size){Check(ReadFile(h,out,size,&count,nullptr)&&count,"read response");out+=count;size-=count;}};
            read(reinterpret_cast<char*>(&n),4);Check(n<4*1024*1024,"response bound");std::string response(n,'\0');read(response.data(),n);return Json::parse(response);
        };
        Check(exchange("{oops")["error"]["code"]=="invalid_json","invalid JSON framing");
        auto hello=exchange(Json{{"protocol",1},{"request_id","h"},{"method","hello"}}.dump());Check(hello["result"]["session"]=="test","hello response");
        auto observation=exchange(Json{{"protocol",1},{"request_id","o"},{"method","observe"}}.dump());
        auto advance=exchange(Json{{"protocol",1},{"request_id","a"},{"method","advance"},{"expect",observation["result"]["version"]},{"params",{{"max_ticks",5}}}}.dump());
        Check(advance["result"]["executed_ticks"]==5,"pipe did not wait for completion");
        auto longRequest=Json{{"protocol",1},{"request_id","disconnect-test"},{"method","advance"},{"expect",advance["result"]["observation"]["version"]},{"params",{{"max_ticks",100000}}}}.dump();
        uint32_t length=static_cast<uint32_t>(longRequest.size());DWORD written=0;
        Check(WriteFile(h,&length,4,&written,nullptr)&&written==4,"disconnect request header");
        Check(WriteFile(h,longRequest.data(),length,&written,nullptr)&&written==length,"disconnect request body");
        Sleep(20);CloseHandle(h);
    }catch(...){error=std::current_exception();}done=true;});
    auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(5);
    while(!done&&std::chrono::steady_clock::now()<deadline){c.Boundary();server.Drain(c);if(c.ShouldStep())Step(c,f);Sleep(1);}
    // Allow the server's bounded broken-pipe check to enqueue cancellation.
    for(int i=0;i<100;++i){c.Boundary();server.Drain(c);if(c.ShouldStep())Step(c,f);Sleep(1);}
    server.Stop();client.join();if(error)std::rethrow_exception(error);
    Check(done&&f.clock>=45&&f.clock<500&&!c.ShouldStep(),"disconnect did not stop the remaining frame budget");
    auto status=Immediate(c,Json{{"protocol",1},{"request_id","s"},{"method","status"},{"params",{{"request_id","disconnect-test"}}}});
    Check(status["result"]["response"]["result"]["stop_reason"]=="client_disconnected","disconnect reason was not retained for reconnect");
}
int main(){try{CoreTests();TerminalTests();InitializationStateTests();CaptureTests();AuditFailureTests();PipeTests();std::cout<<"runtime tests passed: controller invariants, terminal boundaries, initialization sidecars, bounded capture dedup and real fragmented named-pipe I/O\n";return 0;}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
