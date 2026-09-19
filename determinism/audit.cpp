#include "audit.hpp"
#include "silent_audio_audit.hpp"
#include "recording/draw_gate.hpp"
#include "model.hpp"
#include "json_diff.hpp"
#include "memory.hpp"
#include "spawn_hook.hpp"
#include "reanimation_audit.hpp"
#include "particle_shake.hpp"
#include "runtime/engine_call.hpp"
#include "foley_trace.hpp"
#include <array>
#include <fstream>
#include <set>
#include <vector>

namespace lvz::determinism {
namespace {
constexpr uintptr_t kGlobalMt = 0x75a910;
constexpr uintptr_t kAppPointer = 0x6a9ec0;
DWORD ownerThread = 0;
bool initialized = false;
uint64_t sequence = 0;
std::ofstream checksums, changes, events, reanimationHandles, particleSeeds, engineCallRaw;
Json previous;
ReanimationAuditor reanimationAuditor;
Json reanimationEvidence;
Json previousReanimationEvidence;
bool reanimationLinksValid=true;
Json lastObservationVersion=Json::object();
std::set<uint32_t> previousZombies;

void RequireThread() {
    if (!initialized || GetCurrentThreadId() != ownerThread)
        throw std::runtime_error("Audit requires the initialized game thread");
}
template<size_t N> bool Match(uintptr_t address, const uint8_t (&bytes)[N]) noexcept {
    if (!Accessible(address, N)) return false;
    return std::memcmp(reinterpret_cast<const void*>(address), bytes, N) == 0;
}
// Exact bytes from the locally loaded, unmodified 1.0.0.1051 engine image.
bool TargetSignatures() noexcept {
    static constexpr uint8_t seed[] = {0x85,0xc9,0x75,0x05,0xb9,0x05,0x11,0x00,0x00,0x89,0x08,0xc7,0x80,0xc0,0x09,0x00,0x00,0x01,0x00,0x00,0x00};
    static constexpr uint8_t next[] = {0x81,0xba,0xc0,0x09,0x00,0x00,0x70,0x02,0x00,0x00,0x0f,0x8c,0xae,0x00,0x00,0x00};
    static constexpr uint8_t global[] = {0xba,0x10,0xa9,0x75,0x00,0xe9,0x06,0x00,0x00,0x00};
    static constexpr uint8_t update[] = {0x55,0x8b,0xec,0x83,0xe4,0xf8,0x53,0x55,0x56,0x57,0x8b,0xf9,0x80,0xbf,0xce,0x04,0x00,0x00,0x00};
    static constexpr uint8_t crtRand[] = {0xe8,0xb1,0xa9,0x00,0x00,0x8b,0x48,0x14,0x69,0xc9,0xfd,0x43,0x03,0x00,0x81,0xc1,0xc3,0x9e,0x26,0x00};
    return Match(0x5a98d0, seed) && Match(0x5a9940, next)
        && Match(0x5a9930, global) && Match(0x452650, update) && Match(0x61e087, crtRand)
        && ValidateReanimationTarget() && ValidateParticleShakeTarget();
}
uintptr_t CrtStateAddress() {
    // rand/srand call the engine's statically linked _getptd (not this DLL's CRT).
    // The return value's +0x14 is proven by rand disassembly in evidence.json.
    using GetPtd = void* (__cdecl*)();
    const auto ptd = reinterpret_cast<uintptr_t>(reinterpret_cast<GetPtd>(0x628a3d)());
    if (!Accessible(ptd + 0x14, sizeof(uint32_t), true))
        throw std::runtime_error("Engine game-thread CRT state is unavailable");
    return ptd + 0x14;
}
void Write(std::ofstream& stream, const Json& value) {
    stream << value.dump() << '\n';
    if (!stream) throw std::runtime_error("Audit output write failed");
}
void DrainAndCheckSpawns() {
    const auto health=SpawnHookStatus();
    if(health.value("active_initializers",size_t(0))==0) {
        for(auto& spawn:DrainSpawnEvents()) {
            const auto& boundary=spawn.at("boundary");
            const bool controlled=boundary.is_object();
            Json version=nullptr;
            if(controlled) version={{"epoch",boundary.at("segment")},
                {"tick",boundary.at("tick")},{"revision",boundary.at("revision")}};
            Write(events,{{"schema",kSchema},{"seq",sequence++},{"kind","zombie_initialized"},
                {"phase",controlled?"controlled_boundary":"initialization"},
                {"native_phase","zombie_initialize_exit"},{"version",version},
                {"payload",std::move(spawn)}});
        }
    }
    if(!health.value("healthy",false)) {
        Write(events,{{"schema",kSchema},{"seq",sequence++},{"kind","spawn_hook_fault"},
            {"version",lastObservationVersion},{"payload",health}});
        Flush();
        throw std::runtime_error("ZombieInitialize capture incomplete; strict experiment stopped");
    }
}
void SetObservedSpawnBoundary(const Json& version,uint64_t engineCallId) {
    if(!version.is_object() || !version.contains("tick") || !version.contains("revision")
        || !version.contains("epoch")) throw std::runtime_error("Missing controlled spawn boundary version");
    auto integer=[&](const char* key) {
        const auto& value=version.at(key);
        if(!value.is_number_integer() || (value.is_number_integer()&&!value.is_number_unsigned()&&value.get<int64_t>()<0))
            throw std::runtime_error("Invalid controlled spawn boundary version");
        return value.get<uint64_t>();
    };
    const auto epoch=integer("epoch");
    if(epoch>UINT32_MAX) throw std::runtime_error("Spawn boundary epoch exceeds supported range");
    SetSpawnBoundary(integer("tick"),integer("revision"),static_cast<uint32_t>(epoch),engineCallId);
}
void DrainAndCheckParticleShake() {
    for(auto& item:DrainParticleShakeEvents()) {
        const auto& version=item.at("version");
        Json envelope={{"schema",kSchema},{"seq",sequence++},{"kind","particle_shake_seed"},
            {"phase",version.is_object()?"controlled_boundary":"initialization"},
            {"native_phase","before_srand"},{"version",version}};
        auto raw=envelope;raw["schema"]="lvz.particle-shake-raw.v1";raw["payload"]=std::move(item["raw"]);
        envelope["payload"]=std::move(item["semantic"]);Write(events,envelope);Write(particleSeeds,raw);
    }
    const auto health=ParticleShakeStatus();
    if(!health.value("healthy",false)) {
        Write(events,{{"schema",kSchema},{"seq",sequence++},{"kind","particle_shake_fault"},
            {"version",lastObservationVersion},{"payload",health}});
        Flush();throw std::runtime_error("Particle shake seed validation failed; strict experiment stopped");
    }
}
void WordRange(Json& fields, uintptr_t base, unsigned begin, unsigned end) {
    for (unsigned offset = begin; offset < end; offset += 4)
        fields[Hex(offset)] = Read<uint32_t>(base + offset);
}
void Byte(Json& fields, uintptr_t base, unsigned offset) {
    fields[Hex(offset)] = Read<uint8_t>(base + offset);
}
void BaseObject(Json& fields, uintptr_t base) {
    WordRange(fields, base, 8, 0x18); Byte(fields, base, 0x18);
    WordRange(fields, base, 0x1c, 0x24);
}
Json Entity(uintptr_t base, const std::string& type) {
    Json fields = Json::object();
    if (type != "grid_items" && type != "mowers") BaseObject(fields, base);
    if (type == "zombies") {
        WordRange(fields, base, 0x24, 0x50); Byte(fields,base,0x50); Byte(fields,base,0x51);
        WordRange(fields, base, 0x54, 0x70); Byte(fields,base,0x70);
        WordRange(fields, base, 0x74, 0x78); Byte(fields,base,0x78);
        WordRange(fields, base, 0x7c, 0x88); Byte(fields,base,0x88);
        WordRange(fields, base, 0x8c, 0xb8);
        for (unsigned offset=0xb8; offset<0xc0; ++offset) Byte(fields,base,offset);
        WordRange(fields, base, 0xc0, 0xec); Byte(fields,base,0xec);
        WordRange(fields, base, 0xf0, 0x104); Byte(fields,base,0x104);
        WordRange(fields, base, 0x108, 0x14c); Byte(fields,base,0x14c);
        WordRange(fields, base, 0x150, 0x158);
    } else if (type == "plants") {
        WordRange(fields,base,0x24,0x140);
        for (unsigned offset=0x140;offset<=0x145;++offset) Byte(fields,base,offset);
    } else if (type == "projectiles") {
        WordRange(fields,base,0x24,0x50); Byte(fields,base,0x50);
        WordRange(fields,base,0x54,0x70); Byte(fields,base,0x70);
        WordRange(fields,base,0x74,0x90);
    } else if (type == "coins") {
        WordRange(fields,base,0x24,0x38); Byte(fields,base,0x38);
        WordRange(fields,base,0x3c,0x50); Byte(fields,base,0x50);
        WordRange(fields,base,0x54,0x6c);
        // PottedPlant internals are deliberately uncovered until their padding/
        // timestamps are verified. They are not silently hashed as raw memory.
        for(unsigned offset=0xc8;offset<=0xca;++offset) Byte(fields,base,offset);
        WordRange(fields,base,0xcc,0xd0);
    } else if (type == "grid_items") {
        WordRange(fields,base,8,0x20); Byte(fields,base,0x20);
        WordRange(fields,base,0x24,0x48); Byte(fields,base,0x48);
        WordRange(fields,base,0x4c,0x54); WordRange(fields,base,0xe4,0xe8);
    } else if (type == "mowers") {
        WordRange(fields,base,8,0x30); Byte(fields,base,0x30); Byte(fields,base,0x31);
        WordRange(fields,base,0x34,0x44);
    }
    return fields;
}
struct PoolSpec { const char* name; unsigned offset, stride, idOffset, limit; };
constexpr PoolSpec pools[] = {
    {"zombies",0x90,0x15c,0x158,1024}, {"plants",0xac,0x14c,0x148,1024},
    {"projectiles",0xc8,0x94,0x90,1024}, {"coins",0xe4,0xd8,0xd0,1024},
    {"mowers",0x100,0x48,0x44,32}, {"grid_items",0x11c,0xec,0xe8,128}
};
Json Pool(uintptr_t board, const PoolSpec& spec) {
    auto header = board + spec.offset;
    const auto block = Read<uint32_t>(header);
    const auto used = Read<uint32_t>(header+4);
    const auto capacity = Read<uint32_t>(header+8);
    const auto count = Read<uint32_t>(header+16);
    if (used > capacity || capacity > spec.limit || count > used)
        throw std::runtime_error(std::string("Invalid pool layout: ")+spec.name);
    if (used && !Accessible(block, size_t(used)*spec.stride))
        throw std::runtime_error(std::string("Unreadable pool: ")+spec.name);
    Json output = {{"used",used},{"capacity",capacity},{"count",count},
        {"free_head",Read<uint32_t>(header+12)}, {"next_key",Read<uint32_t>(header+20)},
        {"slots",Json::object()}};
    uint32_t allocated = 0;
    for (uint32_t slot=0;slot<used;++slot) {
        auto address=block+slot*spec.stride;
        const auto id=Read<uint32_t>(address+spec.idOffset);
        Json entry={{"id_or_free_next",id}};
        if (id & 0xffff0000u) {
            if ((id & 0xffffu) != slot) throw std::runtime_error("Pool ID/slot mismatch");
            entry["fields"]=Entity(address,spec.name); ++allocated;
        }
        output["slots"][std::to_string(slot)] = std::move(entry);
    }
    if (allocated != count) throw std::runtime_error("Pool count/IDs disagree");
    return output;
}
Json Seeds(uintptr_t board) {
    const auto bank = Read<uint32_t>(board+0x144);
    if (!bank) return nullptr;
    const auto count=Read<uint32_t>(bank+0x24);
    if(count>10) throw std::runtime_error("Invalid seed bank size");
    Json out={{"count",count},{"slots",Json::array()}};
    for(uint32_t index=0;index<count;++index) {
        auto address=bank+0x28+index*0x50;
        Json fields=Json::object(); BaseObject(fields,address);
        WordRange(fields,address,0x24,0x48); Byte(fields,address,0x48); Byte(fields,address,0x49);
        WordRange(fields,address,0x4c,0x50); out["slots"].push_back(fields);
    }
    out["conveyor_counter"]=Read<uint32_t>(bank+0x34c);
    return out;
}
Json Coverage() {
    return {{"complete_game_state",false}, {"exact_spawn_hook",false},
        {"exact_initializer_exit",SpawnHookStatus().value("installed",false)},
        {"final_spawn_after_caller",false},{"initializer_exit_live_validated",false},
        {"raw_float_bits",true},{"pool_slot_and_free_list",true},
        {"reanimations",ReanimationCoverage()},
        {"rng_scope","global MT incl. cursor + game-thread CRT; local MT/other threads not intercepted"},
        {"uncovered",{"unreferenced animations and attachment graphs","particle/effect pools","Challenge state except completed rounds",
                      "PottedPlant coin specification","grid motion trails","UI/input state","time-source interception",
                      "RNG calls/instances on other threads","MT instances on the stack between boundaries"}}};
}
}

bool ValidateTargetImage() noexcept {
    IMAGE_DOS_HEADER dos{};
    if (reinterpret_cast<uintptr_t>(GetModuleHandleW(nullptr)) != 0x400000
        || !TryRead(0x400000,dos) || dos.e_magic!=IMAGE_DOS_SIGNATURE
        || dos.e_lfanew<0 || dos.e_lfanew>0x1000) return false;
    IMAGE_NT_HEADERS32 nt{};
    if(!TryRead(0x400000+dos.e_lfanew,nt) || nt.Signature!=IMAGE_NT_SIGNATURE
        || nt.FileHeader.Machine!=IMAGE_FILE_MACHINE_I386
        || nt.OptionalHeader.Magic!=IMAGE_NT_OPTIONAL_HDR32_MAGIC
        || nt.OptionalHeader.ImageBase!=0x400000
        || nt.OptionalHeader.SizeOfImage<0x35e000) return false;
    return TargetSignatures();
}
Json ProbeTarget() {
    // This object is part of hello's immutable run identity. Counters/queue
    // depth belong to audit health events, never to the target description.
    Json spawn={{"installed",SpawnHookStatus().value("installed",false)},
        {"semantic","exact_initializer_exit"},
        {"phase","ZombieInitialize exit, before caller resumes"},
        {"live_validated",false}};
    Json result={{"schema",kSchema},{"target",kTarget},{"loaded_signatures_match",ValidateTargetImage()},
        {"addresses_evidence","determinism/evidence.json"},
        {"normalize_scope","verified animation handle semantic identity; raw handle evidence is stored separately"},
        {"rng_capture",ValidateTargetImage()}, {"rng_restore",ValidateTargetImage()},
        {"rng_seed",ValidateTargetImage()},
        {"spawn_hook",std::move(spawn)},
        {"particle_shake",ParticleShakeManifest()},{"draw_schedule",lvz::recording::DrawGateManifest()},
        {"engine_call_boundary",lvz::runtime::EngineCallManifest()},{"foley_trace",foleytrace::Manifest()},
        {"original_engine_replay_verified",false}, {"coverage",Coverage()}};
    if(silentaudio::Enabled())result["sound_effects"]=silentaudio::Manifest();
    return result;
}
void Initialize(const std::filesystem::path& runDir) {
    if(initialized) {
        RequireThread(); return;
    }
    if(!ValidateTargetImage()) throw std::runtime_error("Unsupported PvZ engine image: deterministic adapter rejected");
    const auto directory=runDir/"audit";
    std::filesystem::create_directories(directory);
    for(auto file : {"checksums.jsonl","state-deltas.jsonl","events.jsonl","reanimation-handles.jsonl","particle-shake-seeds.jsonl","engine-call-raw.jsonl"})
        if(std::filesystem::exists(directory/file) && std::filesystem::file_size(directory/file))
            throw std::runtime_error("Audit output already exists; create a fresh run");
    checksums.open(directory/"checksums.jsonl",std::ios::out|std::ios::binary);
    changes.open(directory/"state-deltas.jsonl",std::ios::out|std::ios::binary);
    events.open(directory/"events.jsonl",std::ios::out|std::ios::binary);
    reanimationHandles.open(directory/"reanimation-handles.jsonl",std::ios::out|std::ios::binary);
    particleSeeds.open(directory/"particle-shake-seeds.jsonl",std::ios::out|std::ios::binary);
    engineCallRaw.open(directory/"engine-call-raw.jsonl",std::ios::out|std::ios::binary);
    if(!checksums||!changes||!events||!reanimationHandles||!particleSeeds||!engineCallRaw) throw std::runtime_error("Cannot open audit outputs");
    ownerThread=GetCurrentThreadId(); initialized=true; sequence=0;
    previous=nullptr; previousZombies.clear();lastObservationVersion=Json::object();
    reanimationAuditor.Reset();reanimationEvidence=nullptr;previousReanimationEvidence=nullptr;reanimationLinksValid=true;
    bool hookInstalled=false;
    try {
        silentaudio::Initialize(runDir);
        std::string error;
        if(!InstallSpawnHook(error)) throw std::runtime_error(error);
        hookInstalled=true;
        if(!InstallParticleShakeHook(error)) throw std::runtime_error(error);
        foleytrace::Initialize(runDir);
        std::ofstream manifest(directory/"manifest.json");
        manifest<<ProbeTarget().dump(2)<<'\n';
        if(!manifest) throw std::runtime_error("Cannot write audit manifest");
    } catch(...) {
        foleytrace::Shutdown();
        std::string particleError;
        if(!RemoveParticleShakeHook(particleError))
            throw std::runtime_error("Audit initialization failed; keep DLL loaded: "+particleError);
        if(hookInstalled) {
            std::string removeError;
            if(!RemoveSpawnHook(removeError))
                throw std::runtime_error("Audit initialization failed; keep DLL loaded: "+removeError);
        }
        checksums.close();changes.close();events.close();reanimationHandles.close();particleSeeds.close();initialized=false;
        throw;
    }
}
Json CaptureRng() {
    RequireThread();
    if(!TargetSignatures()) throw std::runtime_error("RNG code changed since initialization");
    auto mt=Read<MtState>(kGlobalMt);
    if(mt.cursor>625) throw std::runtime_error("Invalid live MT cursor");
    return {{"schema",kSchema},{"target",kTarget},{"complete_game_rng",false},
        {"instances",{{"global_mt",EncodeMt(mt)},
            {"game_thread_crt",{{"algorithm","msvc_lcg_15"},{"state",Read<uint32_t>(CrtStateAddress())}}}}}};
}
bool RestoreRng(const Json& snapshot,const std::string& boundary,std::string& error) {
    try {
        RequireThread();
        if(boundary!="paused_at_boundary") throw std::runtime_error("RNG restore requires paused_at_boundary");
        if(!TargetSignatures()) throw std::runtime_error("RNG signature mismatch");
        if(snapshot.value("schema","")!=kSchema || snapshot.value("target","")!=kTarget)
            throw std::runtime_error("Snapshot target/schema mismatch");
        const auto& instances=snapshot.at("instances");
        if(!instances.is_object()||instances.size()!=2) throw std::runtime_error("Unexpected RNG instance set");
        auto mt=DecodeMt(instances.at("global_mt"));
        const auto& crt=instances.at("game_thread_crt");
        if(crt.value("algorithm","")!="msvc_lcg_15" || !crt.at("state").is_number_unsigned()
            || crt.at("state").get<uint64_t>()>UINT32_MAX) throw std::runtime_error("Invalid CRT RNG state");
        auto crtAddress=CrtStateAddress();
        if(!Accessible(kGlobalMt,sizeof(MtState),true)) throw std::runtime_error("MT is not writable");
        // Validate every part before either write; no user-provided address is accepted.
        const auto crtValue=crt.at("state").get<uint32_t>();
        std::memcpy(reinterpret_cast<void*>(kGlobalMt),&mt,sizeof(mt));
        std::memcpy(reinterpret_cast<void*>(crtAddress),&crtValue,sizeof(crtValue));
        error.clear(); return true;
    } catch(const std::exception& exception) { error=exception.what(); return false; }
}
bool SeedRng(uint32_t seed,const std::string& boundary,std::string& error) {
    Json snapshot={{"schema",kSchema},{"target",kTarget},{"complete_game_rng",false},
        {"instances",{{"global_mt",EncodeMt(SeedMt(seed))},
            {"game_thread_crt",{{"algorithm","msvc_lcg_15"},{"state",seed}}}}}};
    return RestoreRng(snapshot,boundary,error);
}
Json CaptureClocks() {
    RequireThread();
    const auto app=Read<uint32_t>(kAppPointer);
    if(!app) throw std::runtime_error("Game app unavailable");
    const auto board=Read<uint32_t>(app+0x768);
    if(!board) throw std::runtime_error("Board unavailable for clock capture");
    return {{"schema",kSchema},{"target",kTarget},
        {"game_clock",Read<uint32_t>(board+0x5568)},
        {"effect_clock",Read<uint32_t>(board+0x556c)},
        {"mj_clock",Read<uint32_t>(app+0x838)}};
}
bool RestoreClocks(const Json& snapshot,const std::string& boundary,std::string& error) {
    try {
        RequireThread();
        if(boundary!="paused_at_boundary" || !TargetSignatures())
            throw std::runtime_error("Clock restore requires supported target paused at boundary");
        if(snapshot.value("schema","")!=kSchema || snapshot.value("target","")!=kTarget)
            throw std::runtime_error("Clock snapshot target/schema mismatch");
        std::array<uint32_t,3> values{};
        size_t i=0;
        for(const char* name:{"game_clock","effect_clock","mj_clock"}) {
            const auto& value=snapshot.at(name);
            if(!value.is_number_unsigned() || value.get<uint64_t>()>INT32_MAX)
                throw std::runtime_error("Invalid initial clock value");
            values[i++]=value.get<uint32_t>();
        }
        const auto app=Read<uint32_t>(kAppPointer);
        if(!app) throw std::runtime_error("Game app unavailable");
        const auto board=Read<uint32_t>(app+0x768);
        if(!board || Read<uint32_t>(app+0x7fc)!=3)
            throw std::runtime_error("Clock restore requires a fight board");
        const std::array<uintptr_t,3> addresses={board+0x5568,board+0x556c,app+0x838};
        for(auto address:addresses)
            if(!Accessible(address,4,true)) throw std::runtime_error("Clock state not writable");
        for(size_t index=0;index<addresses.size();++index)
            std::memcpy(reinterpret_cast<void*>(addresses[index]),&values[index],4);
        error.clear();return true;
    } catch(const std::exception& exception) { error=exception.what();return false; }
}
Json CaptureState() {
    RequireThread();
    ReadScope snapshotReads;
    const auto app=Read<uint32_t>(kAppPointer);
    if(!app) throw std::runtime_error("Game app unavailable");
    const auto board=Read<uint32_t>(app+0x768);
    Json state={{"schema",kSchema},{"rng",CaptureRng()},{"particle_shake",ParticleShakeSnapshot()},
        {"draw_schedule",lvz::recording::DrawScheduleSnapshot()},
        {"app",{{"game_mode",Read<int32_t>(app+0x7f8)},{"ui",Read<int32_t>(app+0x7fc)},
            {"mj_clock",Read<uint32_t>(app+0x838)}}}};
    if(silentaudio::Enabled())state["sound_effects"]=silentaudio::Snapshot();
    uint16_t x87=0; uint32_t mxcsr=0;
    __asm__ volatile("fnstcw %0":"=m"(x87));
    __asm__ volatile("stmxcsr %0":"=m"(mxcsr));
    state["fp_environment"]={{"x87_control",x87},{"mxcsr_control",mxcsr&~0x3fu}};
    if(!board) {
        state["board"]=nullptr;reanimationEvidence=nullptr;reanimationLinksValid=true;
        return state;
    }
    Json fields=Json::object(); Byte(fields,board,0x164);
    WordRange(fields,board,0x168,0x5c4); Byte(fields,board,0x5c4);
    WordRange(fields,board,0x5c8,0x6b4);
    // Only declared wave rows, not the unused remainder of the 100-wave storage.
    const auto waves=Read<uint32_t>(board+0x5564);
    if(waves>100) throw std::runtime_error("Invalid total wave count");
    WordRange(fields,board,0x6b4,0x6b4+waves*50*4);
    for(unsigned offset=0x54d4;offset<0x5538;++offset) Byte(fields,board,offset);
    WordRange(fields,board,0x5538,0x5558); // prev mouse coordinates and draw counter excluded
    WordRange(fields,board,0x5560,0x5570); WordRange(fields,board,0x5574,0x55a8);
    Byte(fields,board,0x55fc); WordRange(fields,board,0x5600,0x560c); Byte(fields,board,0x560c);
    WordRange(fields,board,0x5610,0x574c); Byte(fields,board,0x574c);
    WordRange(fields,board,0x5750,0x5760);
    for(unsigned offset=0x5760;offset<0x5768;++offset) Byte(fields,board,offset);
    WordRange(fields,board,0x5768,0x5770); WordRange(fields,board,0x5794,0x57b0);
    state["board"]=std::move(fields);
    for(const auto& pool:pools) state[pool.name]=Pool(board,pool);
    state["seeds"]=Seeds(board);
    auto challenge=Read<uint32_t>(board+0x160);
    state["challenge"]={{"completed_rounds",challenge?Json(Read<uint32_t>(challenge+0x6c)):Json(nullptr)}};
    auto reanimations=reanimationAuditor.Capture(std::move(state));
    reanimationEvidence=std::move(reanimations.raw);
    reanimationLinksValid=reanimations.valid;
    return std::move(reanimations.comparable);
}
void Audit(const std::string& kind,const Json& payload,const Json& observation) {
    RequireThread();
    foleytrace::DrainAndCheck(kind=="recording_closed"||kind=="engine_call_closed");
    // Drain before assigning a new request/step label so menu/preview spawns
    // cannot acquire the version of a command that has not run yet.
    DrainAndCheckSpawns();
    DrainAndCheckParticleShake();
    lastObservationVersion=observation.value("version",Json::object());
    foleytrace::Boundary(kind,payload,lastObservationVersion);
    const uint64_t engineCallId=kind=="pre_step"?payload.at("engine_call").at("engine_call_id").get<uint64_t>():0;
    if(kind=="pre_step"||kind=="request_started"||kind=="action") {
        SetObservedSpawnBoundary(lastObservationVersion,engineCallId);
        SetParticleShakeBoundary(lastObservationVersion.at("tick").get<uint64_t>(),lastObservationVersion.at("revision").get<uint64_t>(),
            lastObservationVersion.at("epoch").get<uint32_t>(),kind,engineCallId);
    } else {ClearSpawnBoundary();ClearParticleShakeBoundary();}
    Json envelope={{"schema",kSchema},{"seq",sequence++},{"kind",kind},
        {"payload",payload},{"version",lastObservationVersion}};
    if(kind=="pre_step"||kind=="post_step") {
        const auto id=payload.at("engine_call").at("engine_call_id");
        Write(engineCallRaw,{{"schema","lvz.engine-call-raw.v1"},{"seq",envelope.at("seq")},
            {"kind",kind},{"version",lastObservationVersion},{"engine_call_id",id},
            {"payload",payload.at("_engine_call_raw")}});
        envelope["payload"].erase("_engine_call_raw");
    }
    if(kind!="pre_step"&&kind!="post_step") {
        Write(events,envelope);
        // prepare_render has no request_completed event: its successful RPC
        // acknowledgement must also make warm-draw receipts and drained hook
        // evidence visible to a replay reader before the first update.
        if(kind=="request_completed"||kind=="render_prepared") Flush();
        return;
    }
    Json state=CaptureState();
    // Exactly the same capture as this checksum/delta, with the same seq and
    // version. Read-only audit_snapshot calls only refresh the in-memory cache.
    auto rawAnimations=envelope;
    if(previousReanimationEvidence.is_null()) rawAnimations["initial"]=reanimationEvidence;
    else rawAnimations["patch"]=Json::diff(previousReanimationEvidence,reanimationEvidence);
    Write(reanimationHandles,rawAnimations);
    previousReanimationEvidence=reanimationEvidence;
    if(!reanimationLinksValid) {
        auto fault=envelope;fault["kind"]="reanimation_link_fault";
        fault["payload"]=state.at("reanimations").at("issues");
        // Preserve the actual rejected snapshot for diagnosis. It is explicitly
        // a fault event, never a fabricated successful checksum/post-step.
        fault["incomplete_boundary"]=true;fault["captured_state"]=std::move(state);
        Write(events,fault);
        Flush();throw std::runtime_error("Live owner has invalid animation links; strict audit stopped");
    }
    auto hashes=envelope; hashes["digests"]=Digests(state); Write(checksums,hashes);
    auto delta=envelope;
    if(previous.is_null()) delta["initial"]=state;
    else delta["patch"]=ExactJsonDiff(previous,state);
    Write(changes,delta); previous=std::move(state);
    if(previous.contains("zombies")) {
        std::set<uint32_t> current;
        for(const auto& slot:previous["zombies"]["slots"].items()) {
            const auto& entry=slot.value(); const auto id=entry["id_or_free_next"].get<uint32_t>();
            if(!(id&0xffff0000u)) continue;
            current.insert(id);
            if(!previousZombies.contains(id)) {
                auto birth=envelope; birth["seq"]=sequence++;birth["kind"]="zombie_first_boundary_observed";
                auto rawFields=entry["fields"];
                const auto prefix="/zombies/slots/"+slot.key()+"/fields/";
                for(const auto& link:reanimationEvidence.at("links")) {
                    const auto path=link.at("path").get<std::string>();
                    if(path.starts_with(prefix)) rawFields[path.substr(prefix.size())]=link.at("raw_handle");
                }
                birth["payload"]={{"id",id},{"slot",slot.key()},{"raw_fields",std::move(rawFields)},
                    {"exact_spawn",false}}; Write(events,birth);
            }
        }
        previousZombies=std::move(current);
    } else previousZombies.clear();
    if(sequence%100==0) Flush();
}
void Flush() {
    RequireThread(); checksums.flush();changes.flush();events.flush();reanimationHandles.flush();particleSeeds.flush();engineCallRaw.flush();
    foleytrace::Flush();
    if(!checksums||!changes||!events||!reanimationHandles||!particleSeeds||!engineCallRaw) throw std::runtime_error("Audit output flush failed");
}
void Shutdown() {
    if(!initialized) return;
    RequireThread();DrainAndCheckSpawns();DrainAndCheckParticleShake();
    foleytrace::Shutdown();
    if(silentaudio::Enabled())Write(events,{{"schema",kSchema},{"seq",sequence++},{"kind","sound_effects_closed"},
        {"version",lastObservationVersion},{"payload",silentaudio::Health()}});
    Write(events,{{"schema",kSchema},{"seq",sequence++},{"kind","draw_schedule_closed"},
        {"version",lastObservationVersion},{"payload",lvz::recording::DrawGateStatus()}});
    Write(events,{{"schema",kSchema},{"seq",sequence++},{"kind","particle_shake_closed"},
        {"version",lastObservationVersion},{"payload",ParticleShakeStatus()}});
    const auto finalHealth=SpawnHookStatus();
    Write(events,{{"schema",kSchema},{"seq",sequence++},{"kind","spawn_hook_closed"},
        {"version",lastObservationVersion},{"payload",finalHealth}});
    std::string error;
    if(!RemoveParticleShakeHook(error)) throw std::runtime_error("Keep runtime DLL loaded: "+error);
    if(!RemoveSpawnHook(error)) throw std::runtime_error("Keep runtime DLL loaded: "+error);
    Flush(); checksums.close(); changes.close(); events.close();reanimationHandles.close();particleSeeds.close();engineCallRaw.close();
    if(checksums.fail()||changes.fail()||events.fail()||reanimationHandles.fail()||particleSeeds.fail()||engineCallRaw.fail()) throw std::runtime_error("Audit output close failed");
    initialized=false;previous=nullptr;previousZombies.clear();lastObservationVersion=Json::object();
    reanimationAuditor.Reset();reanimationEvidence=nullptr;previousReanimationEvidence=nullptr;reanimationLinksValid=true;
}
}
