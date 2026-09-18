#include "runtime.hpp"
#include "pipe_server.hpp"
#include "recorder.hpp"
#include "determinism/audit.hpp"
#include "determinism/memory.hpp"
#include "recording/native_capture.hpp"
#include <avz.h>
#include <wincrypt.h>
#include <cmath>
#include <memory>
#include <set>

namespace lvz::runtime {
namespace {
std::string session;
DWORD ownerThread=0;
bool wasFight=false;
std::vector<int> pendingCards;
std::string initializationState="idle",initializationError;
bool cardsSubmitted=false;
void FinishContinueDialog() {
    auto dialog=AGetPvzBase()->MouseWindow()->TopWindow();
    if(!dialog || dialog->MRef<uintptr_t>(0)!=0x657e28 || dialog->MRef<int>(0x13c)!=37) return;
    // Locked engine: ContinueDialog::ButtonDepress receives the ButtonListener
    // subobject (+0x88), not the Widget pointer. ID 0 resumes the private save.
    const unsigned char signature[]={0x64,0xa1,0,0,0,0,0x6a,0xff,0x68,0x76,0xf2,0x64,0};
    if(std::memcmp(reinterpret_cast<void*>(0x4336c0),signature,sizeof(signature)))
        throw std::runtime_error("ContinueDialog target signature mismatch");
    using Resume=void(__thiscall*)(void*,int);
    reinterpret_cast<Resume>(0x4336c0)(reinterpret_cast<char*>(dialog)+0x88,0);
}
bool LoadingComplete() {
    auto title=AGetPvzBase()->MPtr<APvzStruct>(0x76c);
    return title && lvz::determinism::Accessible(reinterpret_cast<uintptr_t>(title)+0xa1,1)
        && title->MRef<uint8_t>(0xa1)!=0;
}
bool FinishTitle() {
    if(!LoadingComplete()) return false;
    // Confirmed in the locked engine: TitleScreen +0xA1 gates LoadingCompleted;
    // LawnApp::LoadingCompleted at 0x452CB0 is thiscall and clears +0x76C.
    const unsigned char signature[]={0x6a,0xff,0x68,0x48,0xf3,0x64,0x00,0x64,0xa1};
    if(std::memcmp(reinterpret_cast<void*>(0x452cb0),signature,sizeof(signature))) return false;
    using Finish=void(__thiscall*)(APvzBase*);
    reinterpret_cast<Finish>(0x452cb0)(AGetPvzBase());
    return AGetPvzBase()->GameUi()==1;
}
class GameBackend final : public Backend {
public:
    bool Ready() const override {
        // AvZ MemoryInit rejects this same transient false fight on save load.
        return AGetMainObject()&&AGetPvzBase()->GameUi()==3
            &&(initializationState=="ready"||!AGetMainObject()->LevelEndCountdown());
    }
    std::uintptr_t BoardIdentity() const override { return reinterpret_cast<std::uintptr_t>(AGetMainObject()); }
    int NativeTick() const override { return AGetMainObject()?AGetMainObject()->GameClock():0; }
    int NativeWave() override { return AGetMainObject()?AGetMainObject()->Wave():0; }
    Json Observe() override {
        Json out={{"game_ui",AGetPvzBase()->GameUi()},{"game_clock",NativeTick()},
            {"scene",nullptr},{"wave",0},{"sun",0},{"plants",Json::array()},{"zombies",Json::array()},{"seeds",Json::array()}};
        out["initialization"]={{"state",initializationState},{"error",initializationError}};
        out["loading_complete"]=AGetPvzBase()->GameUi()==0?LoadingComplete():true;
        auto board=AGetMainObject(); if(!board) return out;
        out["game_paused"]=board->GamePaused();out["level_end_countdown"]=board->LevelEndCountdown();
        auto dialog=AGetPvzBase()->MouseWindow()->TopWindow();
        out["dialog_id"]=dialog?Json(dialog->MRef<int>(0x13c)):Json(nullptr);
        out["scene"]=board->Scene();out["wave"]=board->Wave();out["sun"]=board->Sun();
        out["refresh_countdown"]=board->RefreshCountdown();out["completed_rounds"]=board->CompletedRounds();
        if(Ready()&&__aScriptManager.isLoaded)
            out["wave_time"]=board->Wave()>0?ANowTime(board->Wave()):-board->RefreshCountdown();
        for(auto& p:aAlivePlantFilter) out["plants"].push_back({{"id",p.Id()},{"type",p.Type()},{"row",p.Row()+1},
            {"col",p.Col()+1},{"hp",p.Hp()},{"state",p.State()},{"shoot_countdown",p.ShootCountdown()},{"effect_countdown",p.ExplodeCountdown()}});
        for(auto& z:aAliveZombieFilter) out["zombies"].push_back({{"id",z.Id()},{"type",z.Type()},{"row",z.Row()+1},
            {"x",z.Abscissa()},{"y",z.Ordinate()},{"hp",z.Hp()},{"armor1",z.OneHp()},{"armor2",z.TwoHp()},
            {"state",z.State()},{"state_countdown",z.StateCountdown()},{"speed",z.Speed()},{"freeze",z.FreezeCountdown()},
            {"slow",z.SlowCountdown()},{"fixation",z.FixationCountdown()},{"age",z.ExistTime()},{"at_wave_raw",z.AtWave()}});
        int slot=0;
        for(auto& s:ABasicFilter<ASeed>()) out["seeds"].push_back({{"slot",++slot},{"type",s.Type()},{"imitator_type",s.ImitatorType()},
            {"cd_raw",s.Cd()},{"initial_cd",s.InitialCd()},{"usable",AIsSeedUsable(&s)}});
        if(Ready()&&__aScriptManager.isLoaded) {
            out["plantable"]=Json::object();
            for(auto& s:ABasicFilter<ASeed>()) {
                int type=s.Type()==48?s.ImitatorType()+49:s.Type(),base=type>=49?type-49:type;
                Json grid=Json::array();
                for(int row=0;row<((board->Scene()==2||board->Scene()==3)?6:5);++row) {
                    Json cells=Json::array();
                    for(int col=0;col<9;++col) cells.push_back(AAsm::GetPlantRejectType(base,row,col)==AAsm::NIL);
                    grid.push_back(std::move(cells));
                }
                out["plantable"][std::to_string(type)]=std::move(grid);
            }
        }
        return out;
    }
    Json Execute(const Json& a) override {
        auto fail=[](const char* error) { return Json{{"ok",false},{"error",error}}; };
        if(!a.is_object()||!a.contains("op")||!a["op"].is_string()||!a.contains("row")||!a["row"].is_number_integer()
            ||!a.contains("col")||!a["col"].is_number()) return fail("invalid_action");
        int row=a["row"].get<int>(); float col=a["col"].get<float>();
        int scene=AGetMainObject()->Scene(); int rows=(scene==2||scene==3)?6:5;
        if(row<1||row>rows||!std::isfinite(col)||col<1||col>9) return fail("invalid_position");
        std::string op=a["op"];
        if(op=="plant") {
            if(!a.contains("type")||!a["type"].is_number_integer()) return fail("invalid_plant_type");
            int type=a["type"].get<int>();
            if(type<0||type>88||type==48) return fail("invalid_plant_type");
            // Avoid AvZ error-dialog paths: preflight card presence/usability and legal planting.
            ASeed* seed=AGetCardPtr(static_cast<APlantType>(type));
            if(!seed) return fail("card_not_selected");
            if(!AIsSeedUsable(seed)) return fail("card_not_usable");
            int baseType=type>=49?type-49:type;
            if(AAsm::GetPlantRejectType(baseType,row-1,static_cast<int>(col+0.5f)-1)!=AAsm::NIL) return fail("plant_rejected");
            auto plant=lvz::Plant(static_cast<APlantType>(type),row,col);
            return plant?Json{{"ok",true},{"plant_id",plant->Id()}}:fail("plant_failed");
        }
        if(op=="shovel") {
            int target=a.value("target_type",-1);
            if(target<-1||target>48) return fail("invalid_target_type");
            bool found=false;
            for(auto& p:aAlivePlantFilter) if(p.Row()+1==row&&p.Col()+1==static_cast<int>(col+0.5f)&&(target<0||p.Type()==target)) {found=true;break;}
            if(!found) return fail("no_plant");
            return lvz::Shovel(row,col,target)?Json{{"ok",true}}:fail("shovel_failed");
        }
        return fail("unsupported_action");
    }
    Json Hello() override {
        return {{"session",session},{"pid",GetCurrentProcessId()},
            {"build",{{"runtime_protocol",1},{"avz_commit","c42676c269b5b482a1eb9203a5b979e9d8a2a5c7"},{"pointer_bits",32}}},
            {"game",lvz::determinism::ProbeTarget()},
            {"capabilities",{{"observe",true},{"commit",true},{"advance",true},{"cancel",true},{"checkpoints",false},
                {"strict_determinism",false},{"step_clock_guard",true},{"exact_step_live_validated",false},{"native_demo",false},{"initialize",true},
                {"audit_snapshot",true},{"rng_restore",true},{"rng_seed",true},{"clock_restore",true},{"stop_recording",true},
                {"capture_frame",lvz::recording::ValidateCaptureTarget()},{"capture_frame_live_validated",false}}}};
    }
    Json CaptureFrame(const Json& params) override {
        if(params.value("format",std::string("bgr24"))!="bgr24")
            return {{"capture_ok",false},{"reason","only bgr24 capture format is implemented"},{"forced_render",false}};
        const auto frame=lvz::recording::CaptureOriginalFrame(ownerThread);
        Json result={{"capture_ok",frame.ok},{"source","original_game_frame"},{"method",frame.method},
            {"origin",frame.origin},{"pixel_format","bgr24"},{"width",frame.width},{"height",frame.height},
            {"row_stride",frame.rowStride},{"forced_render",frame.forcedRender},{"used_3d",frame.used3D}};
        // Pre-render rejection does not imply that game state changed. Once an
        // engine draw is attempted, include even failed/incomplete guards so the
        // controller invalidates a possibly changed boundary instead of retrying.
        if(frame.forcedRender) {
            result["known_rng_unchanged"]=frame.knownRngUnchanged;
            result["game_clock_before"]=frame.gameClockBefore;
            result["game_clock_after"]=frame.gameClockAfter;
        }
        if(!frame.ok) {result["reason"]=frame.error;return result;}
        constexpr DWORD flags=CRYPT_STRING_BASE64|CRYPT_STRING_NOCRLF;
        DWORD count=0;
        if(!CryptBinaryToStringA(frame.pixels.data(),static_cast<DWORD>(frame.pixels.size()),flags,nullptr,&count))
            throw std::runtime_error("capture base64 size conversion failed");
        std::string encoded(count,'\0');
        if(!CryptBinaryToStringA(frame.pixels.data(),static_cast<DWORD>(frame.pixels.size()),flags,encoded.data(),&count))
            throw std::runtime_error("capture base64 conversion failed");
        while(!encoded.empty()&&encoded.back()=='\0') encoded.pop_back();
        result["pixels_base64"]=std::move(encoded);
        return result;
    }
    Json AuditSnapshot() override { return lvz::determinism::CaptureState(); }
    Json RestoreRng(const Json& snapshot) override {
        std::string error;
        bool ok=lvz::determinism::RestoreRng(snapshot,"paused_at_boundary",error);
        return {{"ok",ok},{"error",error}};
    }
    Json SeedRng(uint32_t seed) override {
        std::string error;bool ok=lvz::determinism::SeedRng(seed,"paused_at_boundary",error);
        return {{"ok",ok},{"error",error}};
    }
    Json RestoreClocks(const Json& snapshot) override {
        std::string error;bool ok=lvz::determinism::RestoreClocks(snapshot,"paused_at_boundary",error);
        return {{"ok",ok},{"error",error}};
    }
    void CloseRecording() override { lvz::determinism::Shutdown();lvz::CloseRecording(); }
    Json Initialize(const Json& params) override {
        auto reject=[](const char* reason){return Json{{"ok",false},{"error",reason}};};
        int ui=AGetPvzBase()->GameUi();
        if((ui!=0&&ui!=1)||AGetMainObject()) return reject("initialize requires a loaded title screen or the main menu without a Board");
        if(initializationState=="initializing") return reject("initialization is already pending");
        if(!params.contains("game_mode")||!params["game_mode"].is_number_integer()) return reject("game_mode integer required");
        int mode=params["game_mode"].get<int>();if(mode<0||mode>73) return reject("game_mode must be in 0..73");
        if(!params.contains("cards")||!params["cards"].is_array()||params["cards"].empty()||params["cards"].size()>10)
            return reject("cards must list every selected slot, between 1 and 10 cards");
        std::set<int> unique;int imitators=0;std::vector<int> cards;
        for(const auto& item:params["cards"]) {
            if(!item.is_number_integer()) return reject("card types must be integers");
            int type=item.get<int>();if(type<0||type>88||type==48||!unique.insert(type).second) return reject("invalid or duplicate card type");
            if(type>=49&&++imitators>1) return reject("only one imitator card may be selected");
            cards.push_back(type);
        }
        if(ui==0&&!FinishTitle()) return reject("title screen is still loading or does not match the verified target");
        if(params.contains("seed")) {
            const auto& seed=params.at("seed");
            if(!seed.is_number_integer()||seed.get<int64_t>()<0||seed.get<uint64_t>()>UINT32_MAX) return reject("seed must be uint32");
            auto seeded=SeedRng(seed.get<uint32_t>());if(!seeded.value("ok",false)) return seeded;
        }
        pendingCards=std::move(cards);cardsSubmitted=false;initializationState="initializing";initializationError.clear();
        AEnterGame(mode,false);
        return {{"ok",true},{"state","initializing"},{"completion","configuration_applied; poll observation.initialization for fight readiness"}};
    }
    void Audit(const std::string& kind,const Json& payload,const Json& observation) override {
        lvz::RecordRuntime(kind,payload.dump());
        lvz::determinism::Audit(kind,payload,observation);
    }
};
GameBackend backend;
std::unique_ptr<Controller> controller;
// Intentionally process-lifetime storage: the module is pinned after startup.
// Explicit Shutdown joins workers on the game thread, never under loader lock.
PipeServer* server=nullptr;
void CheckThread() { if(GetCurrentThreadId()!=ownerThread) throw std::runtime_error("Runtime accessed off game thread"); }
}
void Start(const std::filesystem::path& directory) {
    if(controller) return;
    ownerThread=GetCurrentThreadId();session=directory.filename().string();
    HMODULE pinned=nullptr;
    if(!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_PIN,
        reinterpret_cast<LPCWSTR>(&Start),&pinned)) throw std::runtime_error("Cannot pin resident runtime");
    lvz::determinism::Initialize(directory);
    controller=std::make_unique<Controller>(backend);controller->Boundary();
    server=new PipeServer();server->Start();
}
bool Started() { return controller!=nullptr; }
void Shutdown() {
    if(!controller) return;
    CheckThread();controller->Stop("runtime_shutdown");
    server->Stop();delete server;server=nullptr;
    lvz::determinism::Shutdown();controller.reset();
}
bool BeforeFrameImpl() {
    if(!controller) return true;
    CheckThread();
    if(initializationState=="initializing") FinishContinueDialog();
    bool fight=backend.Ready();
    if(wasFight&&!fight) {
        __APublicExitFightHook::RunAll();
        __aScriptManager.isLoaded=false;
    }
    if(!wasFight&&fight) {
        // Populate AvZ's card index and EnterFight hooks before the first paused observation.
        __aScriptManager.RunTotal();
        if(initializationState=="initializing") initializationState="ready";
    }
    wasFight=fight;
    if(initializationState=="initializing"&&!cardsSubmitted&&AGetPvzBase()->GameUi()==2&&AGetMainObject()&&__aScriptManager.isLoaded) {
        if(static_cast<int>(pendingCards.size())!=AGetMainObject()->SeedArray()->Count()) {
            initializationState="error";initializationError="cards count must exactly equal available slots; random autofill is forbidden";
        } else { ASelectCards(pendingCards,1);cardsSubmitted=true; }
    }
    controller->Boundary();server->Drain(*controller);
    return controller->ShouldStep();
}
bool BeforeFrame() {
    try { return BeforeFrameImpl(); }
    catch(const std::exception& error) { if(controller) controller->Fail(error.what());return false; }
}
bool BeforeEngineFrame() {
    if(!controller) return true;
    try {
        CheckThread();controller->Boundary();
        if(!controller->ShouldStep()) return false;
        controller->BeforeStep();return true;
    } catch(const std::exception& error) { controller->Fail(error.what());return false; }
}
void AfterEngineFrame() {
    if(controller) try { CheckThread();controller->AfterStep(); }
    catch(const std::exception& error) { controller->Fail(error.what()); }
}
void RecordEnvironmentCollect(uint32_t itemId,int type,int x,int y) {
    if(!controller) return;
    CheckThread();
    Json payload={{"source","environment_assist"},{"op","collect_attempt"},{"item_id",itemId},{"type",type},{"x",x},{"y",y}};
    try { backend.Audit("environment_collect",payload,{{"version",controller->Version()}}); }
    catch(const std::exception& error) { controller->Fail(error.what()); }
}
}
