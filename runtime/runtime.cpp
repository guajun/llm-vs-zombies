#include "determinism/app_update_anchor.hpp"
#include "determinism/memory.hpp"
#include "determinism/silent_audio_audit.hpp"
#include "runtime.hpp"
#include "pipe_server.hpp"
#include "pump_guard.hpp"
#include "fp_guard.hpp"
#include "recorder.hpp"
#include "determinism/audit.hpp"
#include "recording/native_capture.hpp"
#include "recording/frame_cache.hpp"
#include "determinism/model.hpp"
#include "determinism/foley_trace.hpp"
#ifdef LVZ_FLAG_DROP_LIVE_FIXTURE
#include "tests/flag_drop_live.hpp"
#endif
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
std::string EnvironmentValue(const char* name) {
    char buffer[512]{};DWORD size=GetEnvironmentVariableA(name,buffer,sizeof(buffer));
    if(!size) return std::string();
    if(size>=sizeof(buffer)) throw std::runtime_error(std::string(name)+" is longer than 511 characters");
    return std::string(buffer,size);
}
std::string EnvironmentBranch(const char* name) {
    auto value=EnvironmentValue(name);
    if(!value.empty()&&!RequestJournal::ValidBranch(value))
        throw std::runtime_error(std::string(name)+" must be 1-64 characters of [A-Za-z0-9._:-] starting alphanumeric");
    return value;
}
// Every runtime instance owns exactly one branch scope. The orchestrator sets
// LVZ_BRANCH_ID when it creates or forks a process; the default is a
// process-instance label, so two live processes never share one scope by
// accident and a clone can still be rebound explicitly over IPC.
std::string InstanceBranchScope(const std::filesystem::path& directory) {
    auto explicitScope=EnvironmentBranch("LVZ_BRANCH_ID");
    if(!explicitScope.empty()) return explicitScope;
    auto alnum=[](char c){return (c>='0'&&c<='9')||(c>='A'&&c<='Z')||(c>='a'&&c<='z');};
    std::string sanitized;
    for(char c:directory.filename().string()) {
        const bool allowed=alnum(c)||c=='.'||c=='_'||c==':'||c=='-';
        sanitized.push_back(allowed?c:'-');
    }
    while(!sanitized.empty()&&!alnum(sanitized.front())) sanitized.erase(sanitized.begin());
    if(sanitized.size()>32) sanitized.resize(32);
    if(sanitized.empty()) sanitized="session";
    return sanitized+"-"+std::to_string(GetCurrentProcessId());
}
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
    lvz::recording::FrameCache frameCache_;
    bool renderPrepared_=false,seededAtBoundary_=false;
    lvz::determinism::InitialAppUpdateAnchor appAnchor_;
    uint32_t boundarySeed_=0;
public:
    bool Ready() const override {
        // AvZ MemoryInit rejects this same transient false fight on save load.
        return AGetMainObject()&&AGetPvzBase()->GameUi()==3
            &&(initializationState=="ready"||!AGetMainObject()->LevelEndCountdown());
    }
    std::uintptr_t BoardIdentity() const override { return reinterpret_cast<std::uintptr_t>(AGetMainObject()); }
    int NativeTick() const override { return AGetMainObject()?AGetMainObject()->GameClock():0; }
    bool UsesEngineCallBoundary() const override { return true; }
    int GameUi() const override { return AGetPvzBase()->GameUi(); }
    Json NativeClocks() const override { return lvz::determinism::CaptureClocks(); }
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
            {"capabilities",{{"observe",true},{"commit",true},{"advance",true},{"pause",true},{"status",true},{"cancel",true},{"checkpoints",false},
                {"strict_determinism",false},{"step_clock_guard",true},{"exact_step_live_validated",false},{"native_demo",false},{"initialize",true},
                {"audit_snapshot",true},{"rng_restore",true},{"rng_seed",true},{"clock_restore",true},{"stop_recording",true},
                {"capture_frame",lvz::recording::ValidateCaptureTarget()},{"capture_frame_live_validated",false},
                {"app_update_anchor",lvz::determinism::silentaudio::Enabled()},{"initial_app_update_anchor_v1",lvz::determinism::silentaudio::Enabled()},
                {"sound_counter_origin",lvz::determinism::silentaudio::Enabled()},{"sound_effects_counter_origin_v1",lvz::determinism::silentaudio::Enabled()},
                {"fixed_owner_fp_v1",true},
                {"sound_effects_allocation_none_v1",lvz::determinism::silentaudio::Enabled()},{"prepare_render",true},{"deterministic_draw_schedule_v1",true},{"controlled_engine_call_v1",true}}}};
    }
    bool RequiresRenderPreparation()const override {return true;}
    bool RenderPrepared()const override {return renderPrepared_;}
    bool SupportsAppUpdateAnchor()const override{return lvz::determinism::silentaudio::Enabled();}
    bool AppUpdateAnchored()const override{return appAnchor_.Applied();}
    bool SupportsSoundCounterOrigin()const override{return lvz::determinism::silentaudio::Enabled();}
    bool SoundCounterBound()const override{return lvz::determinism::silentaudio::CounterBound();}
    Json BindSoundCounterOrigin()override{
        if(!Ready()||GetCurrentThreadId()!=ownerThread)return {{"ok",false},{"error","Sound origin requires ready owner game thread"}};
        return lvz::determinism::silentaudio::BindCounterOrigin(boundarySeed_,seededAtBoundary_,renderPrepared_,appAnchor_.Applied(),
            []{return lvz::determinism::CaptureState();});
    }
    Json AnchorAppUpdate(uint32_t requested)override {
        if(!Ready()||GetCurrentThreadId()!=ownerThread)return {{"ok",false},{"error","App anchor requires ready owner game thread"}};
        if(SupportsSoundCounterOrigin()&&!SoundCounterBound())return {{"ok",false},{"error","Explicit sound counter origin must precede App anchor"}};
        const auto app=lvz::determinism::Read<uint32_t>(0x6a9ec0);
        if(!app)return {{"ok",false},{"error","App anchor target unavailable"}};
        return appAnchor_.Apply(app+0x484,requested,boundarySeed_,SupportsAppUpdateAnchor(),seededAtBoundary_,renderPrepared_,
            []{return lvz::determinism::CaptureState();},[app]{
                using lvz::determinism::Read;
                return Json{{"00000510",Read<uint8_t>(app+0x510)},{"00000511",Read<uint8_t>(app+0x511)},
                    {"00000578",Read<uint32_t>(app+0x578)},{"0000049c",Read<uint32_t>(app+0x49c)},{"000004a0",Read<uint8_t>(app+0x4a0)}};});
    }
    void InvalidateFrame(const std::string& reason)override {frameCache_.Invalidate(reason);}
    void ResetRenderPreparation()override {appAnchor_.Reset();renderPrepared_=seededAtBoundary_=false;frameCache_.Invalidate("epoch_changed");}
    Json RenderFrame(const Json& version,bool warm)override {
        lvz::determinism::CheckFloatingPoint(warm?lvz::determinism::fpenv::Phase::BeforeWarm:lvz::determinism::fpenv::Phase::BeforeDraw,true);
        lvz::recording::CheckDrawGate();
        if(!Ready()||warm==renderPrepared_)throw std::runtime_error("Controlled drawing preparation/order mismatch");
        const auto beforeRng=lvz::determinism::CaptureRng();
        if(warm) {
            if(SupportsSoundCounterOrigin()&&!SoundCounterBound())throw std::runtime_error("Sound counter origin must precede warm drawing");
            if(SupportsAppUpdateAnchor()&&!appAnchor_.Applied())throw std::runtime_error("App anchor must precede warm drawing");
            if(!seededAtBoundary_)throw std::runtime_error("rng_seed at the paused fight boundary must precede warm drawing");
            const auto& instances=beforeRng.at("instances");
            const auto& mt=instances.at("global_mt");
            uint32_t word=boundarySeed_?boundarySeed_:4357u;
            if(mt.at("cursor")!=624||mt.at("words").size()!=624||instances.at("game_thread_crt").at("state")!=boundarySeed_)
                throw std::runtime_error("Warm draw RNG readback does not match explicit seed");
            for(size_t index=0;index<624;++index) {
                if(mt.at("words")[index]!=word)throw std::runtime_error("Warm draw MT word readback differs from explicit seed");
                word=1812433253u*(word^(word>>30))+uint32_t(index+1);
            }
        }
        const auto beforeClocks=lvz::determinism::CaptureClocks();
        const auto board=BoardIdentity();
        frameCache_.Invalidate("render_in_progress");
        auto frame=[&]{lvz::determinism::foleytrace::Phase phase(warm);
            return lvz::recording::CaptureOriginalFrame(ownerThread);}();
        lvz::determinism::CheckFloatingPoint(warm?lvz::determinism::fpenv::Phase::AfterWarm:lvz::determinism::fpenv::Phase::AfterDraw,true);
        if(!frame.ok)throw std::runtime_error(frame.error);
        const auto afterClocks=lvz::determinism::CaptureClocks();
        if(beforeClocks!=afterClocks||board!=BoardIdentity()||!Ready())
            throw std::runtime_error("Controlled draw changed clocks or active Board");
        const auto afterRng=lvz::determinism::CaptureRng();
        Json receipt={{"schema","lvz.controlled-render.v1"},{"mode",lvz::recording::DrawMode},
            {"phase",warm?"warm":"step"},{"frame_version",version},
            {"native_clock",NativeTick()},{"width",frame.width},{"height",frame.height},{"pixel_format","bgr24"},
            {"clocks_before",beforeClocks},{"clocks_after",afterClocks},
            {"rng_before",lvz::determinism::Digests(beforeRng.at("instances"))},
            {"rng_after",lvz::determinism::Digests(afterRng.at("instances"))},
            {"rng_unchanged",beforeRng==afterRng},{"rng_restored",false}};
        if(warm)receipt["seed_readback"]={{"seed",boundarySeed_},{"global_mt_words",624},{"global_mt_cursor",624},{"game_thread_crt",boundarySeed_},{"verified_before_draw",true}};
        frameCache_.Store(version,board,NativeTick(),std::move(frame));
        lvz::recording::RecordDrawnFrame(warm);renderPrepared_=true;
        receipt["counts"]=lvz::recording::DrawScheduleSnapshot();return receipt;
    }
    Json CaptureFrame(const Json& params) override {
        if(params.value("format",std::string("bgr24"))!="bgr24")
            return {{"capture_ok",false},{"reason","only bgr24 capture format is implemented"},{"forced_render",false}};
        lvz::recording::CheckDrawGate();
        const auto* cached=frameCache_.Find(params.at("frame_version"),BoardIdentity(),NativeTick());
        if(!cached)return {{"capture_ok",false},{"reason",frameCache_.Reason()},{"forced_render",false}};
        const auto& frame=*cached;
        Json result={{"capture_ok",frame.ok},{"source","original_game_frame"},{"method",frame.method},
            {"origin",frame.origin},{"pixel_format","bgr24"},{"width",frame.width},{"height",frame.height},
            {"row_stride",frame.rowStride},{"forced_render",false},{"used_3d",frame.used3D},
            {"mode",lvz::recording::DrawMode},{"frame_version",frameCache_.Version()},
            {"known_rng_unchanged",true},{"game_clock_before",NativeTick()},{"game_clock_after",NativeTick()}};
        result["method"]="cached_controlled_engine_frame";
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
    Json FloatingPointEvidence()override{return lvz::determinism::FloatingPointEvidence();}
    Json RestoreRng(const Json& snapshot) override {
        std::string error;
        bool ok=lvz::determinism::RestoreRng(snapshot,"paused_at_boundary",error);
        return {{"ok",ok},{"error",error}};
    }
    Json SeedRng(uint32_t seed) override {
        std::string error;bool ok=lvz::determinism::SeedRng(seed,"paused_at_boundary",error);
        if(ok&&Ready()) {seededAtBoundary_=true;boundarySeed_=seed;}
        return {{"ok",ok},{"error",error}};
    }
    Json RestoreClocks(const Json& snapshot) override {
        std::string error;bool ok=lvz::determinism::RestoreClocks(snapshot,"paused_at_boundary",error);
        return {{"ok",ok},{"error",error}};
    }
    void CloseRecording() override {
        lvz::recording::SealDrawGate();lvz::determinism::Shutdown();
#ifdef LVZ_FLAG_DROP_LIVE_FIXTURE
        lvz::flagfixture::Close();
#endif
        lvz::CloseRecording();
    }
    Json Initialize(const Json& params,const Json& context) override {
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
        if(params.contains("seed")) {
            const auto& seed=params.at("seed");
            if(!seed.is_number_integer()||seed.get<int64_t>()<0||seed.get<uint64_t>()>UINT32_MAX) return reject("seed must be uint32");
        }
        if(ui==0&&!FinishTitle()) return reject("title screen is still loading or does not match the verified target");
        auto fp=lvz::determinism::ActivateFloatingPoint(AGetPvzBase()->GameUi(),BoardIdentity(),context);
        if(!fp.value("ok",false))return {{"ok",false},{"error","Fixed owner FP activation failed"},{"fixed_fp",fp}};
        if(params.contains("seed")) {
            const auto& seed=params.at("seed");
            auto seeded=SeedRng(seed.get<uint32_t>());if(!seeded.value("ok",false)) return seeded;
        }
        pendingCards=std::move(cards);cardsSubmitted=false;initializationState="initializing";initializationError.clear();
        AEnterGame(mode,false);
        return {{"ok",true},{"state","initializing"},{"fixed_fp",std::move(fp)},
            {"completion","configuration_applied; poll observation.initialization for fight readiness"}};
    }
    void Audit(const std::string& kind,const Json& payload,const Json& observation) override {
        if(payload.contains("_engine_call_raw")) {auto semantic=payload;semantic.erase("_engine_call_raw");lvz::RecordRuntime(kind,semantic.dump());}
        else lvz::RecordRuntime(kind,payload.dump());
        lvz::determinism::Audit(kind,payload,observation);
    }
};
GameBackend backend;
bool DrawReady() {return backend.Ready();}
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
    std::string drawError;
    if(!lvz::recording::InstallDrawGate(ownerThread,&DrawReady,drawError))throw std::runtime_error(drawError);
    lvz::determinism::Initialize(directory);
#ifdef LVZ_FLAG_DROP_LIVE_FIXTURE
    lvz::flagfixture::Initialize(directory,ownerThread);
#endif
    JournalOptions journal;journal.path=directory/"decisions/runtime-requests.bin";
    journal.branch=InstanceBranchScope(directory);
    journal.parentBranch=EnvironmentBranch("LVZ_BRANCH_PARENT_ID");
    if(EnvironmentValue("LVZ_JOURNAL_ADOPT")=="1") {
        // Sibling path used by the fork/snapshot host: continue the parent's
        // unsealed journal under this instance's own branch scope.
        journal.adopt=true;
        auto epoch=EnvironmentValue("LVZ_JOURNAL_EPOCH");
        if(!epoch.empty()) {
            try { journal.adoptEpoch=std::stoull(epoch); }
            catch(const std::exception&) { throw std::runtime_error("LVZ_JOURNAL_EPOCH must be a positive integer"); }
            if(!journal.adoptEpoch) throw std::runtime_error("LVZ_JOURNAL_EPOCH must be a positive integer");
        }
    }
    controller=std::make_unique<Controller>(backend,std::move(journal));controller->Boundary();
    server=new PipeServer();server->Start();
}
bool Started() { return controller!=nullptr; }
void Shutdown() {
    if(!controller) return;
    CheckThread();controller->Stop("runtime_shutdown");
    server->Stop();delete server;server=nullptr;
    lvz::recording::SealDrawGate();lvz::determinism::Shutdown();
#ifdef LVZ_FLAG_DROP_LIVE_FIXTURE
    lvz::flagfixture::Close();
#endif
    controller.reset();
}
bool BeforeFrameImpl() {
    if(!controller) return true;
    // A nested ScriptHook must stop before RunTotal, Boundary or IPC can
    // mutate/complete the outer call. The outer wrapper reports this fault.
    if(!controller->GuardEngineEntry())return false;
    CheckThread();
    if(!CheckPumpGate(*controller,[]{controller->CheckEngineGuard();lvz::recording::CheckDrawGate();
        lvz::determinism::CheckFloatingPoint(lvz::determinism::fpenv::Phase::Loop);},[]{server->Drain(*controller);}))return false;
    if(initializationState=="initializing") {
        FinishContinueDialog();
        lvz::determinism::CheckFloatingPoint(lvz::determinism::fpenv::Phase::Initialization,true);
    }
    bool fight=backend.Ready();
    if(wasFight&&!fight) {
        __APublicExitFightHook::RunAll();
        __aScriptManager.isLoaded=false;
    }
    if(!wasFight&&fight) {
        // Populate AvZ's card index and EnterFight hooks before the first paused observation.
        __aScriptManager.RunTotal();
        lvz::determinism::CheckFloatingPoint(lvz::determinism::fpenv::Phase::Ready,true);
        if(initializationState=="initializing") initializationState="ready";
    }
    wasFight=fight;
    if(initializationState=="initializing"&&!cardsSubmitted&&AGetPvzBase()->GameUi()==2&&AGetMainObject()&&__aScriptManager.isLoaded) {
        if(static_cast<int>(pendingCards.size())!=AGetMainObject()->SeedArray()->Count()) {
            initializationState="error";initializationError="cards count must exactly equal available slots; random autofill is forbidden";
        } else { ASelectCards(pendingCards,1);cardsSubmitted=true;
            lvz::determinism::CheckFloatingPoint(lvz::determinism::fpenv::Phase::Initialization,true); }
    }
    controller->Boundary();server->Drain(*controller);
    return controller->ShouldStep();
}
bool BeforeFrame() {
    try { return BeforeFrameImpl(); }
    catch(const std::exception& error) { if(controller) controller->Fail(error.what());return false; }
}
bool AfterAvzRunTotal() {
    if(!controller)return true;
    return CheckPumpGate(*controller,[]{lvz::determinism::CheckFloatingPoint(lvz::determinism::fpenv::Phase::Initialization);},
        []{server->Drain(*controller);});
}
bool RunOneEngineFrame() {
    if(!controller) {AAsm::GameTotalLoop();return true;}
    try {
        CheckThread();
        lvz::determinism::CheckFloatingPoint(lvz::determinism::fpenv::Phase::BeforeUpdate);
        return controller->RunEngineFrame([] {
#ifdef LVZ_FLAG_DROP_LIVE_FIXTURE
            lvz::flagfixture::BeforeOriginalUpdate(controller->EngineCallHealth(),controller->Version(),backend.Ready());
#endif
            AAsm::GameTotalLoop();
            // The original call has returned. Record a drift while the wrapper
            // still marks it in-flight, then return normally so AfterStep keeps
            // its actual entered/returned call and measured clock count.
            CheckReturnedFloatingPoint(*controller,[]{lvz::determinism::CheckFloatingPoint(lvz::determinism::fpenv::Phase::AfterUpdate);});
#ifdef LVZ_FLAG_DROP_LIVE_FIXTURE
            lvz::flagfixture::AfterOriginalUpdate(controller->EngineCallHealth(),controller->Version(),backend.Ready());
#endif
        });
    } catch(const std::exception& error) { controller->Fail(error.what());return false; }
}
void RecordEnvironmentCollect(uint32_t itemId,int type,int x,int y) {
    if(!controller) return;
    CheckThread();
    Json payload={{"source","environment_assist"},{"op","collect_attempt"},{"item_id",itemId},{"type",type},{"x",x},{"y",y}};
    try { backend.Audit("environment_collect",payload,{{"version",controller->Version()}}); }
    catch(const std::exception& error) { controller->Fail(error.what()); }
}
}
