// Issue #32: branch-scoped dedup and version namespace.
//
// The fixture is a scripted backend, never the game. It reproduces the fork
// hazard directly: a sibling instance continues a parent's journal file, so the
// parent's records are physically present in the child's lineage while the
// child owns a different branch scope. The tests then assert that (a) the
// sibling executes a shared request ID itself and answers with its own result,
// (b) a foreign record is never served as this runtime's result, (c) reusing a
// foreign ID with different content fails with an explicit code, and (d) the
// old, unscoped client and recording contract still behaves exactly as before.
#include "controller.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <optional>
#include <stdexcept>
using namespace lvz::runtime;

void Check(bool value,const char* message) { if(!value) throw std::runtime_error(message); }
void Check(bool value,const std::string& message) { if(!value) throw std::runtime_error("branch scope: "+message); }

struct Engine:Backend {
    int clock=10,actions=0,serves=0;
    bool ready=true;std::uintptr_t board=1;
    explicit Engine(int tag):tag_(tag) {}
    bool Ready()const override{return ready;}
    std::uintptr_t BoardIdentity()const override{return board;}
    int NativeTick()const override{return clock;}
    int NativeWave()override{return 1;}
    // ``serves`` counts actual observation boundaries, so a fixture response is
    // born by this instance or is provably a recovered record.
    Json Observe()override{++serves;return {{"game_clock",clock},{"wave",1},{"actions",actions},{"serves",serves}};}
    Json Execute(const Json&)override{++actions;return {{"ok",true},{"plant_id",tag_*100+actions}};}
    Json Hello()override{return {{"session","branch-scope-test"}};}
    void Audit(const std::string&,const Json&,const Json&)override{}
private:
    int tag_;
};

Json Req(Controller& c,std::string id,std::string method,Json params=Json::object()) {
    return {{"protocol",1},{"request_id",std::move(id)},{"method",std::move(method)},{"params",std::move(params)},{"expect",c.Version()}};
}
Json Immediate(Controller& c,const Json& request) {
    std::optional<Json> out;c.Request(request,[&](Json response){out=std::move(response);});
    Check(out.has_value(),"missing immediate response");return std::move(*out);
}
Json Commit(Controller& c,const std::string& id) {
    return Req(c,id,"commit",{{"actions",Json::array({Json::object()})},{"advance_ticks",0}});
}
Json Status(Controller& c,const std::string& id) { return Immediate(c,Req(c,"status","status",{{"request_id",id}}))["result"]; }
void Step(Controller& c,Engine& e) { Check(c.ShouldStep(),"missing step budget");c.BeforeStep();++e.clock;c.AfterStep(); }
std::filesystem::path Scratch(const std::string& name) {
    auto directory=std::filesystem::temp_directory_path()/
        ("lvz-branch-scope-"+name+"-"+std::to_string(GetCurrentProcessId())+"-"+std::to_string(GetTickCount64()));
    std::filesystem::remove_all(directory);
    std::filesystem::create_directories(directory);
    return directory;
}

void RebindScopesLiveHistory() {
    auto directory=Scratch("rebind");JournalOptions options;
    options.path=directory/"runtime-requests.bin";options.branch="branch-a";
    Engine e(1);Controller c(e,options);c.Boundary();
    auto hello=Immediate(c,Req(c,"hello","hello"))["result"];
    Check(hello["branch"]["schema"]=="lvz.branch-scope.v1","hello branch schema mismatch");
    Check(hello["branch"]["branch_id"]=="branch-a"&&hello["branch"]["mode"]=="branch","hello did not declare the runtime branch");
    Check(hello["branch"]["dedup_key"]=="branch_id+epoch+request_id","hello did not publish the branch dedup key");
    Check(hello["capabilities"]["branch_scope_v1"]==true,"hello lacks the branch scope capability");
    auto shared=Req(c,"shared","pause");                       // stable boundary: no version drift
    auto parentResult=Immediate(c,shared);
    Check(parentResult["ok"]==true&&e.serves==1,"branch A control did not execute");
    Check(Immediate(c,shared)==parentResult&&e.serves==1,"same-scope retry did not recover the exact result");
    auto branchOnly=Req(c,"branch-only","pause");
    Check(Immediate(c,branchOnly)["ok"]==true&&e.serves==2,"branch A second control did not execute");
    auto rebound=Immediate(c,Req(c,"rebind","branch_rebind",{{"branch_id","branch-b"},{"from_branch_id","branch-a"}}));
    Check(rebound["result"]["rebound"]==true&&rebound["result"]["previous_branch_id"]=="branch-a","rebind did not report the previous branch");
    Check(c.BranchIdentity()["branch_id"]=="branch-b"&&c.BranchIdentity()["origin"]=="rebound","rebind did not move the runtime identity");
    // The sibling executes the shared ID itself; it never inherits branch A's answer.
    auto siblingResult=Immediate(c,shared);
    Check(siblingResult["ok"]==true&&e.serves==3,"sibling answered a request it never executed");
    Check(siblingResult.dump()!=parentResult.dump(),"sibling response equals the parent's cached response");
    Check(siblingResult["result"]["observation"]["serves"]==3,"sibling did not produce its own observation");
    // An ID bound to different content in another scope is an explicit error.
    auto conflicting=branchOnly;conflicting["params"]=Json{{"different",true}};
    auto conflict=Immediate(c,conflicting);
    Check(conflict["error"]["code"]=="cross_branch_request_id_conflict","foreign ID reuse with different content was not rejected");
    Check(conflict["error"]["details"]["foreign_branch_id"]=="branch-a","conflict did not name the foreign branch");
    Check(e.serves==3,"conflicting request executed work");
    auto foreignStatus=Status(c,"branch-only");
    Check(foreignStatus["request_state"]=="foreign_branch"&&foreignStatus["foreign_branch_id"]=="branch-a","status served a foreign record");
    Check(!foreignStatus.contains("response"),"status returned a foreign response body");
    // Declaring another branch never executes and never answers for that branch.
    auto declared=shared;declared["branch"]="branch-a";
    auto mismatch=Immediate(c,declared);
    Check(mismatch["error"]["code"]=="branch_scope_mismatch","a request declaring a foreign branch was accepted");
    Check(mismatch["error"]["details"]["foreign_record_content_matches"]==true,"mismatch lost the foreign record evidence");
    Check(e.serves==3,"branch mismatch executed work");
    auto stamped=Commit(c,"stamped");stamped["branch"]="branch-b";
    Check(Immediate(c,stamped)["ok"]==true,"a request declaring its own branch was rejected");
    auto invalid=Commit(c,"bad");invalid["branch"]="not a branch!";
    Check(Immediate(c,invalid)["error"]["code"]=="invalid_request","an invalid branch declaration was accepted");
    // Rebind preconditions.
    Check(Immediate(c,Req(c,"rebind-from","branch_rebind",{{"branch_id","branch-c"},{"from_branch_id","branch-a"}}))["error"]["code"]=="branch_rebind_rejected",
        "rebind accepted a wrong from_branch_id");
    Check(Immediate(c,Req(c,"rebind-invalid","branch_rebind",{{"branch_id","bad id"},{"from_branch_id","branch-b"}}))["error"]["code"]=="invalid_params",
        "rebind accepted an invalid branch id");
    auto pending=Req(c,"pending","advance",{{"max_ticks",5}});std::optional<Json> done;
    c.Request(pending,[&](Json response){done=std::move(response);});
    Check(!done.has_value(),"advance completed synchronously");
    Check(Immediate(c,Req(c,"rebind-busy","branch_rebind",{{"branch_id","branch-c"},{"from_branch_id","branch-b"}}))["error"]["code"]=="busy",
        "rebind was admitted while work was pending");
    for(int i=0;i<5;++i) Step(c,e);
    Check(done.has_value()&&(*done)["result"]["executed_ticks"]==5,"pending advance did not finish");
    Check(Immediate(c,Req(c,"rebind-same","branch_rebind",{{"branch_id","branch-b"},{"from_branch_id","branch-b"}}))["result"]["rebound"]==false,
        "idempotent rebind was not reported as a no-op");
    // Version namespace: once this branch's own version has moved on, an
    // ancestor record for its own ID is stale rather than a silent answer.
    Check(Immediate(c,branchOnly)["error"]["code"]=="stale_observation","an ancestor record answered at a drifted version");
    Check(Immediate(c,Req(c,"close","stop_recording"))["result"]["closed"]==true,"journal did not close");
    Check(Immediate(c,Req(c,"rebind-closed","branch_rebind",{{"branch_id","branch-c"},{"from_branch_id","branch-b"}}))["error"]["code"]=="recording_closed",
        "rebind was admitted after the recording closed");
    std::cout<<"branch scope: rebind isolation ok\n";
}

void AdoptedSiblingRebuildsTheIndex() {
    auto directory=Scratch("sibling");
    auto inherited=directory/"decisions/runtime-requests.bin";
    Json shared,parentOnly;std::string parentResponse;
    {
        JournalOptions options;options.path=inherited;options.branch="parent-a";options.reserveBytes=65536;
        Engine e(1);Controller parent(e,options);parent.Boundary();
        shared=Commit(parent,"shared");
        parentResponse=Immediate(parent,shared).dump();
        parentOnly=Commit(parent,"parent-only");   // built at the version the first action reached
        auto parentOnlyResult=Immediate(parent,parentOnly);
        Check(parentOnlyResult["result"]["action_results"][0]["plant_id"]==102,std::string("parent action mismatch: ")+parentOnlyResult.dump());
        // A cloned process holds the parent's journal bytes but neither its RAM
        // index nor its lock handle. Copying the files (and dropping the lock)
        // reproduces exactly what the adopting sibling has to rebuild.
        auto clone=directory/"sibling/decisions";
        std::filesystem::create_directories(clone);
        std::filesystem::copy_file(inherited,clone/"runtime-requests.bin");
        std::filesystem::copy_file(inherited.wstring()+L".reserve",clone/"runtime-requests.bin.reserve");
    }
    JournalOptions adopted;
    adopted.path=directory/"sibling/decisions/runtime-requests.bin";
    adopted.branch="sibling-b";adopted.adopt=true;adopted.reserveBytes=65536;
    Engine e(7);Controller sibling(e,adopted);sibling.Boundary();
    auto hello=Immediate(sibling,Req(sibling,"hello","hello"))["result"];
    Check(hello["branch"]["branch_id"]=="sibling-b"&&hello["branch"]["origin"]=="adopted","adopted sibling identity mismatch");
    auto own=Immediate(sibling,shared);
    Check(own["ok"]==true&&e.actions==1,std::string("adopted sibling answered an inherited ID it never executed: ")+own.dump());
    Check(own.dump()!=parentResponse,"adopted sibling served the parent's cached response");
    Check(own["result"]["action_results"][0]["plant_id"]==701,"adopted sibling result is not its own");
    // The sibling's own request for that ID is built at its own version, so it
    // differs from the ancestor's content and must be refused, never answered.
    auto conflicting=Req(sibling,"parent-only","pause",{{"different",true}});
    auto conflict=Immediate(sibling,conflicting);
    Check(conflict["error"]["code"]=="cross_branch_request_id_conflict",std::string("adopted sibling missed the cross-branch conflict: ")+conflict.dump());
    Check(conflict["error"]["details"]["foreign_branch_id"]=="parent-a","conflict did not name the inherited branch");
    Check(e.actions==1,"adopted sibling executed a conflicting request");
    auto foreign=shared;foreign["branch"]="parent-a";
    Check(Immediate(sibling,foreign)["error"]["code"]=="branch_scope_mismatch","adopted sibling accepted a foreign branch declaration");
    Check(e.actions==1,"foreign branch declaration executed an action");
    Check(Status(sibling,"parent-only")["request_state"]=="foreign_branch","adopted sibling served an inherited record as its own");
    Check(Immediate(sibling,Commit(sibling,"sibling-only"))["ok"]==true,"adopted sibling could not append a new record");
    std::cout<<"branch scope: adopted sibling isolation ok\n";
}

void UnscopedRuntimeKeepsTheLegacyContract() {
    auto directory=Scratch("legacy-client");JournalOptions options;options.path=directory/"runtime-requests.bin";
    Engine e(3);Controller c(e,options);c.Boundary();
    auto hello=Immediate(c,Req(c,"hello","hello"))["result"];
    Check(hello["branch"]["mode"]=="unscoped"&&hello["branch"]["branch_id"].is_null(),"an unscoped runtime invented a branch");
    Check(hello["branch"]["dedup_key"]=="epoch+request_id","unscoped dedup key changed");
    Check(hello["capabilities"]["branch_scope_v1"]==true,"unscoped runtime must still declare the capability");
    auto request=Commit(c,"legacy");
    auto first=Immediate(c,request);
    Check(first["ok"]==true&&Immediate(c,request)==first&&e.actions==1,"legacy epoch+ID dedup semantics changed");
    auto changed=request;changed["params"]["advance_ticks"]=1;
    Check(Immediate(c,changed)["error"]["code"]=="request_id_conflict","legacy same-scope conflict code changed");
    auto declared=Commit(c,"declared");declared["branch"]="some-branch";
    Check(Immediate(c,declared)["error"]["code"]=="branch_scope_mismatch","unscoped runtime accepted a branch-scoped request");
    Check(e.actions==1,"rejected branch-scoped request executed an action");
    std::cout<<"branch scope: unscoped legacy contract ok\n";
}

void AdoptionRefusals() {
    auto directory=Scratch("refusals");
    auto sealedPath=directory/"sealed/decisions/runtime-requests.bin";
    {
        JournalOptions options;options.path=sealedPath;options.branch="sealed-a";options.reserveBytes=65536;
        Engine e(1);Controller c(e,options);c.Boundary();
        Check(Immediate(c,Commit(c,"one"))["ok"]==true,"sealed fixture action failed");
        Check(Immediate(c,Req(c,"close","stop_recording"))["result"]["closed"]==true,"sealed fixture did not close");
    }
    JournalOptions adopt;adopt.adopt=true;adopt.reserveBytes=65536;adopt.branch="sealed-b";adopt.path=sealedPath;
    try { RequestJournal journal(adopt);journal.Reserve("id","payload",false);throw std::runtime_error("a sealed journal was adopted"); }
    catch(const JournalError& error) { Check(std::string(error.what()).find("sealed")!=std::string::npos,"wrong sealed-adoption refusal"); }
    auto legacyPath=directory/"legacy-format/decisions/runtime-requests.bin";
    std::filesystem::create_directories(legacyPath.parent_path());
    for(auto path:{legacyPath,std::filesystem::path(legacyPath.wstring()+L".reserve")}) {
        std::ofstream stream(path,std::ios::binary);std::array<char,64> header{};std::memcpy(header.data(),"LVZREQ01",8);
        stream.write(header.data(),header.size());
    }
    JournalOptions legacy=adopt;legacy.path=legacyPath;
    try { RequestJournal journal(legacy);journal.Reserve("id","payload",false);throw std::runtime_error("a legacy journal was adopted"); }
    catch(const JournalError& error) { Check(std::string(error.what()).find("LVZREQ01")!=std::string::npos,"wrong legacy-format refusal"); }
    auto livePath=directory/"live/decisions/runtime-requests.bin";
    JournalOptions owner;owner.path=livePath;owner.branch="live-a";owner.reserveBytes=65536;
    Engine e(1);Controller c(e,owner);c.Boundary();
    Check(Immediate(c,Commit(c,"one"))["ok"]==true,"live fixture action failed");
    JournalOptions blocked=adopt;blocked.path=livePath;blocked.branch="live-b";
    try { RequestJournal journal(blocked);journal.Reserve("id","payload",false);throw std::runtime_error("a live journal was adopted"); }
    catch(const JournalError& error) { Check(std::string(error.what()).find("locked")!=std::string::npos,"wrong live-journal refusal"); }
    Check(!std::filesystem::exists(std::filesystem::path(sealedPath.wstring()+L".lock")),"sealed parent lost its own lock state");
    std::cout<<"branch scope: adoption refusals ok\n";
}

int main(){try{
    RebindScopesLiveHistory();AdoptedSiblingRebuildsTheIndex();UnscopedRuntimeKeepsTheLegacyContract();AdoptionRefusals();
    std::cout<<"runtime branch scope tests passed\n";return 0;
}catch(const std::exception& error){std::cerr<<error.what()<<'\n';return 1;}}
