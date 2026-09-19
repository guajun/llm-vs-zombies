#include "silent_audio_audit.hpp"
#include "sound_counter_origin.hpp"
#include "memory.hpp"
#include "launcher/silent_audio.hpp"
#include "launcher/file_hash.hpp"
#include <fstream>
namespace lvz::determinism::silentaudio {
namespace {
using Json=nlohmann::json;
namespace contract=lvz::silentaudio;
bool enabled=false,initialized=false;
HMODULE bootstrap=nullptr;
contract::Query query=nullptr;
contract::Status activation;
std::filesystem::path directory;
uint32_t lastCalls=0;
Json spec;
std::string firstFault;
SoundCounterOrigin counterOrigin;
Json sampledRawStatus=nullptr;
#ifdef LVZ_SILENT_AUDIO_TESTING
Json (*fixtureSnapshot)()=nullptr;
#endif
contract::Status Status() {
    contract::Status s;
    if(!query||query(&s)||s.magic!=contract::Magic||s.version!=1||!s.enabled||!s.installed||s.phase!=2
        ||s.size!=sizeof(s)||s.engineSha256[64]||s.bootstrapSha256[64]||!s.pinned||!s.patchOwned||s.errors||s.preexistingApp||s.entry!=contract::Target
        ||s.ownerModule!=reinterpret_cast<uintptr_t>(bootstrap)||!s.primaryThread||!s.replacement
        ||std::memcmp(s.original,contract::Original,6)||std::string(s.engineSha256)!=contract::EngineSha256)
        throw std::runtime_error("silent audio activation/ownership verification failed");
    if(!Accessible(s.entry,6)||std::memcmp(reinterpret_cast<void*>(s.entry),s.patch,6)||s.patch[0]!=0xe9||s.patch[5]!=0x90)
        throw std::runtime_error("silent audio entry patch lost");
    uint32_t jump;std::memcpy(&jump,s.patch+1,4);
    if(s.entry+5+jump!=s.replacement)throw std::runtime_error("silent audio replacement address mismatch");
    MEMORY_BASIC_INFORMATION replacement{};
    if(!VirtualQuery(reinterpret_cast<void*>(s.replacement),&replacement,sizeof(replacement))||replacement.AllocationBase!=bootstrap)
        throw std::runtime_error("silent audio replacement owner mismatch");
    if(s.calls<lastCalls)throw std::runtime_error("silent audio counter decreased/overflowed");lastCalls=s.calls;
    return s;
}
Json Raw(const contract::Status& s){return {{"mode",contract::Mode},{"installed",true},{"before_primary_thread_resume",true},
    {"phase","sealed_before_resume"},{"primary_thread",s.primaryThread},{"owner_module",s.ownerModule},{"owner_pinned",true},
    {"entry",s.entry},{"replacement",s.replacement},{"patch_owned",true},{"calls",s.calls},{"errors",s.errors},
    {"preexisting_app",s.preexistingApp},{"original_bytes",s.original},{"patch_bytes",s.patch},
    {"engine_sha256",s.engineSha256},{"bootstrap_sha256",s.bootstrapSha256}};}
}
bool Enabled(){return enabled;}
void Initialize(const std::filesystem::path& run) {
    if(initialized)throw std::runtime_error("silent audio audit already initialized");
    wchar_t mode[96]{};const DWORD n=GetEnvironmentVariableW(L"LVZ_AUDIO_MODE",mode,96);
    if(n>=96||(n&&wcscmp(mode,L"original")&&wcscmp(mode,contract::ModeW)))throw std::runtime_error("unsupported audio mode");
    enabled=n&&wcscmp(mode,L"original");
    bootstrap=GetModuleHandleW(L"lvz-bootstrap.dll");
    if(bootstrap){query=reinterpret_cast<contract::Query>(GetProcAddress(bootstrap,"LvzAudioStatus@4"));if(!query)query=reinterpret_cast<contract::Query>(GetProcAddress(bootstrap,"LvzAudioStatus"));}
    if(!enabled){
        contract::Status s;if(query&&(!query(&s))&&(s.enabled||s.installed))throw std::runtime_error("audio mode differs from configured original");
        initialized=true;return;
    }
    directory=run/"audit";activation=Status();
    wchar_t path[32768]{};
    if(!GetModuleFileNameW(bootstrap,path,32768)||contract::FileSha256(path)!=activation.bootstrapSha256)
        throw std::runtime_error("silent audio bootstrap file hash changed");
    // The launcher's receipt was written while the primary thread was suspended.
    // Bind it to actual resident code, not to a copied desired run identity.
    std::ifstream source(run/"sandbox/native-receipt.json");Json receipt;source>>receipt;
    if(!source||receipt.at("pid")!=GetCurrentProcessId())throw std::runtime_error("silent audio launcher receipt missing/wrong process");
    Json wanted=Raw(activation);wanted["calls"]=0;
    if(receipt.at("audio_activation")!=wanted)throw std::runtime_error("silent audio launcher/runtime activation mismatch");
    spec={{"mode",contract::Mode},{"installed",true},{"activation","before_primary_thread_resume"},
        {"entry_rva",0x1c7650},{"original_bytes",contract::Original},{"owner_pinned",true},
        {"engine_sha256",contract::EngineSha256},{"bootstrap_sha256",activation.bootstrapSha256},
        {"original_pitch_variation_code",true},{"sound_allocation","always_null"},{"music_virtualized",false},
        {"original_engine_bitwise_unmodified",false},{"live_verified",false},
        {"required_evidence","audio-activation.json"},{"closed_event","sound_effects_closed"},
        {"state_scope","110 Foley histories and slots; active parameters; 32 empty manager channels; actual App update count; allocation counter"}};
    const auto output=directory/"audio-activation.json";
    if(std::filesystem::exists(output))throw std::runtime_error("audio activation evidence already exists");
    std::ofstream file(output);file<<Json{{"schema","lvz.audio-activation.v1"},{"configuration",spec},
        {"pre_resume",receipt.at("audio_activation")},{"recorder_attach",Raw(activation)}}.dump(2)<<'\n';
    file.close();if(file.fail())throw std::runtime_error("cannot persist audio activation evidence");
    initialized=true;
}
Json Manifest(){return enabled?spec:Json(nullptr);}
Json SnapshotImpl(){
    if(!enabled)return nullptr;
    const auto status=Status();
    const auto app=Read<uint32_t>(0x6a9ec0),sexy=Read<uint32_t>(0x6a9f38);
    if(!app||app!=sexy)throw std::runtime_error("silent audio app roots unavailable/mismatched");
    if(status.primaryThread!=GetCurrentThreadId())throw std::runtime_error("silent audio snapshot requires original primary thread");
    const auto system=Read<uint32_t>(app+0x784),manager=Read<uint32_t>(app+0x4b4);
    const auto params=Read<uint32_t>(0x6a9f00),count=Read<uint32_t>(0x6a9f04);
    if(!system||!manager||params!=0x69fad0||count==0||count>110||Read<uint32_t>(manager)!=0x675fd4
        ||Read<uint32_t>(0x675ff4)!=contract::Target)throw std::runtime_error("silent audio real roots/table/vtable mismatch");
    Json histories=Json::array(),parameterState=Json::array(),channels=Json::array();
    for(unsigned type=0;type<110;++type){
        Json slots=Json::array();const auto base=system+type*0xa4;
        for(unsigned slot=0;slot<8;++slot){const auto p=base+slot*0x14;
            auto instance=Read<uint32_t>(p),refs=Read<uint32_t>(p+4);
            if(instance||refs)throw std::runtime_error("silent audio found preexisting/bypassed Foley instance or refcount");
            slots.push_back({instance,refs,Read<uint8_t>(p+8),Read<uint32_t>(p+12),Read<uint32_t>(p+16)});
        }
        histories.push_back({{"last_variation",Read<uint32_t>(base+0xa0)},{"slots",std::move(slots)}});
        if(type<count){const auto p=params+type*0x34;Json ids=Json::array(),rvas=Json::array();
            if(Read<uint32_t>(p)!=type)throw std::runtime_error("silent audio Foley type table changed");
            for(unsigned i=0;i<10;++i){auto address=Read<uint32_t>(p+8+i*4);
                if(address&&(address<contract::ImageBase||!contract::SoundIdRvaValid(address-contract::ImageBase)))throw std::runtime_error("silent audio sound resource outside target image");
                rvas.push_back(address?Json(address-0x400000):Json(nullptr));ids.push_back(address?Json(Read<uint32_t>(address)):Json(nullptr));}
            parameterState.push_back({{"type",type},{"pitch_bits",Read<uint32_t>(p+4)},
                {"flags",Read<uint32_t>(p+0x30)},{"sound_id_rvas",std::move(rvas)},{"sound_ids",std::move(ids)}});
        }
    }
    for(unsigned i=0;i<32;++i){auto channel=Read<uint32_t>(manager+0x3008+i*4);if(channel)throw std::runtime_error("silent audio found bypassed manager channel");channels.push_back(channel);}
    Json audio={{"mode",contract::Mode},{"calls",status.calls},{"errors",status.errors},{"app_update_count",Read<uint32_t>(app+0x484)},
        {"active_types",count},{"histories",std::move(histories)},{"parameters",std::move(parameterState)},
        {"channels",std::move(channels)},{"slots_empty",true},{"patch_owned",true}};
    auto raw=Raw(status);
    auto presented=counterOrigin.Present(std::move(audio),raw);
    sampledRawStatus=std::move(raw);
    return presented;
}
Json Snapshot(){
    sampledRawStatus=nullptr;
    try {return SnapshotImpl();}
    catch(const std::exception& error){if(firstFault.empty())firstFault=error.what();throw;}
}
bool CounterBound(){return enabled&&counterOrigin.Bound();}
Json SampledRawStatus(){
    if(!enabled||sampledRawStatus.is_null())throw std::runtime_error("No successful silent audio snapshot sample");
    return sampledRawStatus;
}
Json BindCounterOrigin(uint32_t seed,bool seeded,bool warmed,bool appAnchored,const std::function<Json()>& capture){
    auto result=counterOrigin.Apply(seed,enabled,seeded,warmed,appAnchored,capture,[]{return SampledRawStatus();});
    if(!result.at("ok").get<bool>()&&result.at("bind_attempted").get<bool>()&&firstFault.empty())
        firstFault=result.at("error").get<std::string>();
    return result;
}
Json CounterBoundary(const Json& envelope,const Json& state){
    return counterOrigin.Boundary(envelope,state.at("sound_effects"),SampledRawStatus());
}
Json Health(){
    if(!enabled)return nullptr;
    try {
        if(!firstFault.empty())throw std::runtime_error(firstFault);
#ifdef LVZ_SILENT_AUDIO_TESTING
        auto state=fixtureSnapshot?fixtureSnapshot():Snapshot();
#else
        auto state=Snapshot();
#endif
        Json health={{"mode",contract::Mode},{"healthy",true},{"calls",state["calls"]},{"errors",0},
            {"patch_owned",true},{"owner_pinned",true},{"slots_empty",true},
            {"scope","allocation-none SFX experiment; music/exhaustive determinism unverified"}};
#ifdef LVZ_SILENT_AUDIO_TESTING
        // Legacy health-only fixture has no installed live counter. The new
        // origin fixture independently exercises Health with actual samples.
        if(fixtureSnapshot)return health;
#endif
        return counterOrigin.Health(std::move(health),state,SampledRawStatus());
    }catch(const std::exception& error){
        if(firstFault.empty())firstFault=error.what();
        // Persist a genuine unhealthy close even if the fault that froze the
        // simulation is still present. Never reuse the last healthy snapshot.
        Json raw=nullptr;contract::Status current;
        if(query&&!query(&current))raw={{"enabled",current.enabled},{"installed",current.installed},
            {"phase",current.phase},{"calls",current.calls},{"errors",current.errors},
            {"patch_owned",current.patchOwned},{"owner_pinned",current.pinned},
            {"entry",current.entry},{"replacement",current.replacement},{"owner_module",current.ownerModule}};
        Json bytes=nullptr;
        if(Accessible(contract::Target,6)){std::array<uint8_t,6> found;std::memcpy(found.data(),reinterpret_cast<void*>(contract::Target),6);bytes=found;}
        return {{"mode",contract::Mode},{"healthy",false},{"failure",error.what()},{"raw_status",std::move(raw)},
            {"entry_bytes",std::move(bytes)},{"slots_empty",nullptr},{"read_error","snapshot failed; null entry_bytes means unreadable"}};
    }
}
#ifdef LVZ_SILENT_AUDIO_TESTING
void SetHealthFixture(Json (*snapshot)(),contract::Query value){enabled=true;fixtureSnapshot=snapshot;query=value;}
#endif
}
