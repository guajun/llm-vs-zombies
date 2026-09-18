#include "controller.hpp"
#include <psapi.h>
#include <chrono>
#include <iostream>
#include <stdexcept>
using namespace lvz::runtime;
void Check(bool value,const char* message) { if(!value) throw std::runtime_error(message); }
struct Engine:Backend {
    int clock=10,actions=0,closes=0;
    bool ready=true;std::uintptr_t board=1;
    size_t padding=0;
    bool Ready()const override{return ready;}
    std::uintptr_t BoardIdentity()const override{return board;}
    int NativeTick()const override{return clock;}
    int NativeWave()override{return 1;}
    Json Observe()override{return {{"game_clock",clock},{"wave",1},{"actions",actions},{"padding",std::string(padding,'x')}};}
    Json Execute(const Json&)override{++actions;return {{"ok",true}};}
    Json Hello()override{return {{"session","journal-test"}};}
    void CloseRecording()override{++closes;}
    void Audit(const std::string&,const Json&,const Json&)override{}
};
Json Req(Controller& c,std::string id,std::string method,Json params=Json::object()) {
    return {{"protocol",1},{"request_id",std::move(id)},{"method",std::move(method)},{"params",std::move(params)},{"expect",c.Version()}};
}
Json Immediate(Controller& c,const Json& request) {
    std::optional<Json> out;c.Request(request,[&](Json response){out=std::move(response);});
    Check(out.has_value(),"missing immediate response");return std::move(*out);
}
void Step(Controller& c,Engine& e) { Check(c.ShouldStep(),"missing step budget");c.BeforeStep();++e.clock;c.AfterStep(); }
Json Status(Controller& c,const std::string& id) { return Immediate(c,Req(c,"status","status",{{"request_id",id}}))["result"]; }
Json Act(Controller& c,const std::string& id) { return Req(c,id,"commit",{{"actions",Json::array({Json::object()})},{"advance_ticks",0}}); }
size_t PrivateBytes() {
    PROCESS_MEMORY_COUNTERS_EX value{};value.cb=sizeof(value);
    Check(GetProcessMemoryInfo(GetCurrentProcess(),reinterpret_cast<PROCESS_MEMORY_COUNTERS*>(&value),sizeof(value)),"cannot sample private memory");
    return value.PrivateUsage;
}
void LongRun() {
    Engine e;Controller c(e);c.Boundary();
    const auto baseline=PrivateBytes();auto started=std::chrono::steady_clock::now();
    Json firstRequest,firstResponse,lastResponse;
    for(int i=0;i<200005;++i) {
        auto request=Req(c,"single-"+std::to_string(i),"advance",{{"max_ticks",1}});
        std::optional<Json> response;c.Request(request,[&](Json value){response=std::move(value);});
        Check(!response,"advance acknowledged before step");Step(c,e);
        Check(response&&(*response)["result"]["executed_ticks"]==1,"single tick failed in long run");
        if(i==0) { firstRequest=request;firstResponse=*response; }
        lastResponse=std::move(*response);
        if(i%50000==49999) std::cout<<"journal long run: "<<i+1<<" requests\n"<<std::flush;
    }
    Check(c.Version()["tick"]==200005,"long run tick count changed");
    const auto after=PrivateBytes();
    Check(after<=baseline+32*1024*1024,"request-count-proportional memory growth");
    Check(c.Status()["dedup"]["hot_results"]==0,"completed responses stayed in controller RAM");
    Check(Immediate(c,firstRequest)==firstResponse,"oldest request did not recover exact result");
    Check(Status(c,"single-0")["response"]==firstResponse,"status lost oldest response");
    Check(Status(c,"single-200004")["response"]==lastResponse,"status lost newest response");
    auto conflict=firstRequest;conflict["params"]["max_ticks"]=2;
    Check(Immediate(c,conflict)["error"]["code"]=="request_id_conflict","old ID with changed payload executed");
    const auto bytes=c.Status()["dedup"]["disk_bytes"];
    auto close=Req(c,"close","stop_recording");auto closed=Immediate(c,close);
    Check(closed["result"]["closed"]==true&&e.closes==1,"200k requests blocked close");
    auto path=c.JournalForTesting().Path();auto size=std::filesystem::file_size(path);
    Check(!std::filesystem::exists(path.wstring()+L".lock"),"successful close retained journal lock");
    for(auto method:{"pause","cancel","stop_recording","advance"})
        Check(Immediate(c,Req(c,std::string("after-")+method,method))["error"]["code"]=="recording_closed","sealed journal accepted a new mutation");
    Check(Immediate(c,close)==closed&&Status(c,"single-0")["response"]==firstResponse,"sealed history cannot be recovered");
    Check(std::filesystem::file_size(path)==size&&e.closes==1,"readback/new rejected IDs changed a sealed journal");
    std::cout<<"200005 single ticks: "<<std::chrono::duration<double>(std::chrono::steady_clock::now()-started).count()
             <<" seconds, disk="<<bytes<<", private before="<<baseline<<", after="<<after<<"\n";
}
void LargeResponses() {
    Engine e;e.padding=65536;Controller c(e);c.Boundary();Json first,response;
    auto baseline=PrivateBytes();
    for(int i=0;i<1100;++i) {
        auto request=Act(c,"large-"+std::to_string(i));auto out=Immediate(c,request);
        Check(out["ok"]==true,"large completed observations exhausted dedup");
        if(!i) { first=request;response=std::move(out); }
    }
    Check(c.Status()["dedup"]["disk_bytes"].get<uint64_t>()>64*1024*1024,"fixture did not exceed the previous byte limit");
    Check(PrivateBytes()<baseline+32*1024*1024,"large observation history remained in RAM");
    Check(Immediate(c,first)==response&&e.actions==1100,"large oldest response changed or executed again");
    Check(Immediate(c,Req(c,"large-close","stop_recording"))["ok"]==true,"large journal could not close");
}
void QuotaAndControls() {
    Engine e;JournalOptions opts;opts.normalEntries=2;opts.reserveEntries=3;opts.reserveBytes=65536;
    Controller c(e,opts);c.Boundary();auto action=Act(c,"once");auto result=Immediate(c,action);
    auto advance=Req(c,"pending","advance",{{"max_ticks",100}});std::optional<Json> done;
    c.Request(advance,[&](Json response){done=std::move(response);});Step(c,e);
    Check(Immediate(c,Act(c,"over-quota"))["error"]["code"]=="dedup_capacity"&&e.actions==1,"entry quota was not rejected before action");
    for(int i=0;i<10;++i)
        Check(Immediate(c,Req(c,"busy-close-"+std::to_string(i),"stop_recording"))["error"]["code"]=="busy","pending close was not rejected before admission");
    Check(c.Status()["dedup"]["entries"]==2,"busy close consumed its dedicated ID reserve");
    auto pause=Req(c,"reserved-pause","pause");pause.erase("expect");
    Check(Immediate(c,pause)["ok"]==true&&done&&(*done)["result"]["executed_ticks"]==1,"quota blocked pause/pending completion");
    auto cancel=Req(c,"reserved-cancel","cancel");cancel.erase("expect");
    Check(Immediate(c,cancel)["ok"]==true,"quota blocked reserved cancel");
    Check(Immediate(c,Req(c,"exhausted-control","pause"))["error"]["code"]=="dedup_capacity","extra pause consumed dedicated close slot");
    Check(Immediate(c,Req(c,"reserved-close","stop_recording"))["result"]["closed"]==true,"quota blocked reserved close");
    Check(Immediate(c,action)==result&&Status(c,"pending")["response"]==*done,"quota controls lost previous exact results");

    Engine bytes;bytes.padding=2000;opts=JournalOptions{};opts.normalBytes=512;opts.reserveBytes=65536;
    Controller b(bytes,opts);b.Boundary();auto req=Act(b,"byte-result");auto out=Immediate(b,req);
    Check(out["ok"]==true&&bytes.actions==1&&!b.ShouldStep(),"completion did not use byte reserve and freeze");
    Check(Status(b,"byte-result")["response"]==out,"byte-overflow completion was lost");
    Check(Immediate(b,Req(b,"byte-close","stop_recording"))["result"]["closed"]==true,"byte quota blocked close");
}
void IoFailures() {
    for(bool failReserve:{false,true}) {
        Engine e;int normalWrites=0;bool fail=true;
        JournalOptions opts;opts.reserveBytes=65536;
        opts.beforeIo=[&](const char* phase){
            if(std::string(phase)=="write_normal"&&++normalWrites==2) throw JournalError("fixture normal response disk failure");
            if(failReserve&&fail&&std::string(phase)=="write_reserve") throw JournalError("fixture reserve disk failure");
        };
        Controller c(e,opts);c.Boundary();auto request=Act(c,"disk-action");auto result=Immediate(c,request);
        Check(result["ok"]==true&&e.actions==1&&!c.ShouldStep(),"completed action/disk failure changed real outcome");
        Check(Immediate(c,request)==result&&Status(c,"disk-action")["response"]==result,"I/O failure lost exact-once result");
        Check(Immediate(c,Act(c,"no-more"))["error"]["code"]=="dedup_storage_failed"&&e.actions==1,"I/O failure allowed another mutation");
        fail=false;
        Check(Immediate(c,Req(c,"disk-close","stop_recording"))["result"]["closed"]==true,"reserve recovery could not close");
        Check(c.Status()["dedup"]["hot_results"]==0,"seal did not persist RAM-only final result");
    }
    Engine e;bool fail=true;JournalOptions opts;opts.reserveBytes=65536;
    opts.beforeIo=[&](const char* phase){if(fail&&std::string(phase)=="write_normal") throw JournalError("fixture request write failure");};
    Controller c(e,opts);c.Boundary();auto rejected=Act(c,"before-action");
    Check(Immediate(c,rejected)["error"]["code"]=="dedup_storage_failed"&&e.actions==0,"reservation failure executed action");
    Check(Immediate(c,Req(c,"close-after-reservation-failure","stop_recording"))["ok"]==true,"reservation failure blocked emergency close");

    Engine seal;opts=JournalOptions{};opts.reserveBytes=65536;
    opts.beforeIo=[](const char* phase){if(std::string(phase)=="seal") throw JournalError("fixture flush failure");};
    Controller s(seal,opts);s.Boundary();Immediate(s,Act(s,"seal-action"));
    auto close=Req(s,"seal-failed","stop_recording");auto response=Immediate(s,close);
    Check(response["error"]["code"]=="recording_close_incomplete"&&seal.closes==1&&!s.ShouldStep(),"flush failure claimed sealed success");
    Check(std::filesystem::exists(s.JournalForTesting().Path().wstring()+L".lock"),"flush failure removed archive guard");
    Check(Immediate(s,close)==response&&Status(s,"seal-failed")["response"]==response&&seal.closes==1,"failed seal reran close or changed error");
}
void CorruptionAndEpoch() {
    for(auto kind:{"id","body","header","truncate"}) {
        Engine e;Controller c(e);c.Boundary();auto request=Act(c,"corrupt-original");Immediate(c,request);
        auto& journal=c.JournalForTesting();
        // Damage the newest node's ID: checking only a matching candidate would
        // silently skip it and allow that original request to run a second time.
        if(std::string(kind)=="id") journal.TestingWrite(0,64+64+std::string("corrupt-original").size()+request.dump().size()+64,"X");
        if(std::string(kind)=="body") journal.TestingWrite(0,64+64+std::string("corrupt-original").size(),"X");
        if(std::string(kind)=="header") journal.TestingWrite(0,64,"X");
        if(std::string(kind)=="truncate") journal.TestingTruncate(0,65);
        auto duplicate=Immediate(c,request);
        Check(duplicate["error"]["code"]=="dedup_storage_failed"&&e.actions==1&&!c.ShouldStep(),"damaged journal allowed a duplicate action");
    }
    RequestJournal journal;auto first=journal.Reserve("id","epoch-one",false);journal.Complete(first,"id","result-one");
    Check(journal.Find("id")->response=="result-one","direct journal result mismatch");
    journal.Epoch(2);Check(!journal.Find("id"),"epoch did not reset request ID namespace");
    auto second=journal.Reserve("id","epoch-two",false);journal.Complete(second,"id","result-two");
    Check(journal.Find("id")->payload=="epoch-two"&&journal.Find("id")->response=="result-two","new epoch returned previous epoch result");
    journal.Seal();
}
void TerminalWriteFailures() {
    for(bool external:{false,true}) {
        Engine e;bool fail=false;JournalOptions options;options.reserveBytes=65536;
        options.beforeIo=[&](const char* operation){if(fail&&(std::string(operation)=="write_normal"||std::string(operation)=="write_reserve"))throw JournalError("terminal completion write failure");};
        Controller c(e,options);c.Boundary();auto request=Req(c,"terminal-write","advance",{{"max_ticks",10}});
        std::optional<Json> result;c.Request(request,[&](Json value){result=std::move(value);});
        auto epoch=c.Version()["epoch"];fail=true;
        if(external) { e.board=2;c.Boundary(); }
        else { c.BeforeStep();++e.clock;e.ready=false;c.AfterStep(); }
        Check(result&&(*result)["result"]["stop_reason"]=="scene_changed"&&c.Version()["epoch"]==epoch,"terminal fault discarded its unfinished journal epoch");
        Check(Immediate(c,request)==*result&&Status(c,"terminal-write")["response"]==*result,"terminal fault lost actual original completion");
        for(int i=0;i<20;++i)
            Check(Immediate(c,Req(c,"fault-pause-"+std::to_string(i),"pause"))["error"]["code"]=="dedup_storage_failed","fault controls grew RAM-only result history");
        Check(c.Status()["dedup"]["hot_results"]==1,"fault memory grew with rejected pause IDs");
        fail=false;
        Check(Immediate(c,Req(c,"terminal-close","stop_recording"))["result"]["closed"]==true,"terminal completion could not persist before close");
        Check(c.Status()["dedup"]["hot_results"]==0&&Status(c,"terminal-write")["response"]==*result,"sealed terminal result was not readable from disk");
    }
}
int main(){try{
    QuotaAndControls();IoFailures();CorruptionAndEpoch();TerminalWriteFailures();LargeResponses();LongRun();
    std::cout<<"runtime journal tests passed\n";return 0;
}catch(const std::exception& error){std::cerr<<error.what()<<'\n';return 1;}}
