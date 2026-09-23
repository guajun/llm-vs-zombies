#include "recorder.hpp"
#include "buffered_writer.hpp"
#include "hosted_script.hpp"
#include "hosted_observation.hpp"
#include "runtime/runtime.hpp"
#include "runtime/diagnostics.hpp"
#include "runtime/spawn_action.hpp"
#include <iomanip>
#include <set>
#include <sstream>

#ifdef LVZ_AVZ_HOSTED_SCRIPT
#ifndef LVZ_AVZ_HOSTED_SCRIPT_NAME
// CMakeLists.txt derives this from LVZ_AVZ_HOSTED_SCRIPT; the fallback keeps a
// manual compile of the hosted path working.
#define LVZ_AVZ_HOSTED_SCRIPT_NAME "(unnamed hosted script)"
#endif
#endif

namespace lvz {
namespace {
BufferedWriter writer;
std::filesystem::path runDir;
std::string runId;
uint64_t sequence = 0;
int segment = -1, lastTick = -1, lastSample = -1, interval = 10;
bool opened = false, inSegment = false, stopped = false;
HANDLE runLock = INVALID_HANDLE_VALUE;
std::map<uint32_t, std::string> knownZombies;
std::set<uint32_t> knownPlants;

int Clock() { return AGetMainObject() ? std::max(0, AGetMainObject()->GameClock()) : std::max(0, lastTick); }

#ifdef LVZ_AVZ_HOSTED_SCRIPT
// Hosted observability: sample the state the hosted script publishes and let
// the writer decide whether it changed. Called from an AfterTick hook, i.e.
// after AvZ's RunScript() processed this frame (RunTotal: BeforeTick ->
// RunScript -> AfterTick), so the tick written with a resume of
// co_await ATime(wave, time) is the tick of the frame that resumed it, not the
// one after. Nothing here exists in a default build; frames the controller
// withholds never reach this hook, so a paused stretch writes nothing.
std::string hostedFields;
void SampleHosted() {
    if (!opened || stopped) return;
    hostedFields.clear();
    lvz::hosted::Observe(hostedFields);
    hosted_observation::Sample(Clock(), std::max(0, segment), hostedFields);
}
#endif

void Emit(const std::string& kind, const std::string& payload, int tick = -1, const std::string& phase = "avz_callback") {
    if (!opened || stopped) return;
    if (tick < 0) tick = Clock();
    writer.Append("{\"schema_version\":1,\"run_id\":" + Quote(runId)
        + ",\"seq\":" + std::to_string(sequence++) + ",\"segment\":" + std::to_string(std::max(0, segment))
        + ",\"tick\":" + std::to_string(tick) + ",\"phase\":" + Quote(phase) + ",\"kind\":" + Quote(kind)
        + ",\"payload\":" + payload + "}");
}

void OpenRun() {
    if (opened || stopped) return;
    HMODULE module = nullptr;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
            reinterpret_cast<LPCWSTR>(&OpenRun), &module)) throw std::runtime_error("Cannot find recorder module");
    wchar_t path[32768];
    DWORD length = GetModuleFileNameW(module, path, 32768);
    if (!length || length >= 32768) throw std::runtime_error("Cannot find recorder configuration");
    runtime::SetDiagnosticsPath(std::filesystem::path(path).parent_path()/"runtime-diagnostics.log");
    std::ifstream config(std::filesystem::path(path).parent_path() / "recorder.cfg");
    std::string directory;
    std::getline(config, directory);
    config >> interval;
    if (!config || directory.empty() || interval < 1 || interval > 1000)
        throw std::runtime_error("Run tools/lvz.ps1 new-run before loading recorder.dll");
    runDir = std::filesystem::path(std::u8string(directory.begin(), directory.end()));
    if (!runDir.is_absolute() || !std::filesystem::exists(runDir / "manifest.json"))
        throw std::runtime_error("Invalid experiment directory");
    runId = runDir.filename().string();
    runLock = CreateFileW((runDir / "capture.lock").c_str(), GENERIC_WRITE, 0, nullptr, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (runLock == INVALID_HANDLE_VALUE) throw std::runtime_error("Run already locked; create a fresh run");
    try {
        writer.Open(runDir / "events.jsonl");
#ifdef LVZ_AVZ_HOSTED_SCRIPT
        // A hosted build also owns the hosted script's observation file
        // (logger/avz/hosted_observation.hpp). It is opened here, under the run
        // lock and before any frame, and a run directory that still holds one
        // fails the same way events.jsonl does: create a new run.
        hosted_observation::Open(runDir, runId, LVZ_AVZ_HOSTED_SCRIPT_NAME, Clock());
#endif
    }
    catch (...) { CloseHandle(runLock); runLock = INVALID_HANDLE_VALUE; throw; }
    opened = true;
}

void Begin(int tick) {
    ++segment;
    knownZombies.clear(); knownPlants.clear();
    lastTick = lastSample = -1;
    inSegment = true;
    Emit("segment_start", "{\"game_mode\":" + std::to_string(AGetPvzBase()->LevelId())
        + ",\"scene\":" + std::to_string(AGetMainObject()->Scene())
        + ",\"completed_rounds\":" + std::to_string(AGetMainObject()->CompletedRounds())
        + ",\"sampling\":\"observed entities; not exact spawn hooks\"}", tick);
    std::ostringstream types;
    types << "{\"total_waves\":" << AGetMainObject()->TotalWave() << ",\"type_slots\":[";
    int slots = AGetMainObject()->TotalWave() * 50;
    if (slots < 0 || slots > 5000) throw std::runtime_error("Unsupported wave table size");
    for (int i=0; i<slots; ++i) { if (i) types << ','; types << AGetMainObject()->ZombieList()[i]; }
    types << "]}";
    Emit("wave_table", types.str(), tick);
}

std::string Zombie(AZombie& z) {
    std::ostringstream out;
    out << std::setprecision(9) << "{\"id\":" << z.Id() << ",\"type\":" << z.Type()
        << ",\"row\":" << z.Row()+1 << ",\"x\":" << z.Abscissa() << ",\"y\":" << z.Ordinate()
        << ",\"hp\":" << z.Hp() << ",\"armor1\":" << z.OneHp() << ",\"armor2\":" << z.TwoHp()
        << ",\"state\":" << z.State() << ",\"state_countdown\":" << z.StateCountdown()
        << ",\"speed\":" << z.Speed() << ",\"freeze\":" << z.FreezeCountdown()
        << ",\"slow\":" << z.SlowCountdown() << ",\"fixation\":" << z.FixationCountdown()
        << ",\"age\":" << z.ExistTime() << ",\"at_wave_raw\":" << z.AtWave() << '}';
    return out.str();
}

void Capture() {
    if (!opened || stopped || !AGetMainObject() || AGetPvzBase()->GameUi()!=3) return;
    int tick = Clock();
    if (!inSegment || tick < lastTick) Begin(tick);
    if (tick == lastTick) return;
    lastTick = tick;
    std::map<uint32_t, std::string> zombies;
    for (auto& z : aAliveZombieFilter) {
        auto body = Zombie(z);
        if (!knownZombies.contains(z.Id())) Emit("zombie_first_observed", body, tick);
        zombies.emplace(z.Id(), std::move(body));
    }
    for (const auto& [id, unused] : knownZombies)
        if (!zombies.contains(id)) Emit("zombie_no_longer_observed", "{\"id\":"+std::to_string(id)+"}", tick);
    knownZombies = std::move(zombies);
    std::set<uint32_t> plants;
    for (auto& p : aAlivePlantFilter) plants.insert(p.Id());
    for (auto id : knownPlants)
        if (!plants.contains(id)) Emit("plant_no_longer_observed", "{\"id\":"+std::to_string(id)+"}", tick);
    knownPlants = std::move(plants);
    if (lastSample >= 0 && tick - lastSample < interval) return;
    lastSample = tick;
    auto board = AGetMainObject();
    std::ostringstream out;
    out << std::setprecision(9) << "{\"wave\":" << board->Wave() << ",\"sun\":" << board->Sun()
        << ",\"scene\":" << board->Scene() << ",\"refresh_countdown\":" << board->RefreshCountdown()
        << ",\"plants\":[";
    bool comma=false;
    for (auto& p : aAlivePlantFilter) {
        if (comma) out << ','; comma=true;
        out << "{\"id\":" << p.Id() << ",\"type\":" << p.Type() << ",\"row\":" << p.Row()+1
            << ",\"col\":" << p.Col()+1 << ",\"hp\":" << p.Hp() << ",\"state\":" << p.State()
            << ",\"shoot_countdown\":" << p.ShootCountdown() << ",\"effect_countdown\":" << p.ExplodeCountdown() << '}';
    }
    out << "],\"zombies\":["; comma=false;
    for (const auto& [id, body] : knownZombies) { if (comma) out << ','; comma=true; out << body; }
    out << "],\"seeds\":["; comma=false;
    for (auto& seed : ABasicFilter<ASeed>()) {
        if (comma) out << ','; comma=true;
        out << "{\"type\":" << seed.Type() << ",\"imitator_type\":" << seed.ImitatorType()
            << ",\"cd_raw\":" << seed.Cd() << ",\"initial_cd\":" << seed.InitialCd()
            << ",\"usable\":" << (AIsSeedUsable(&seed)?"true":"false") << '}';
    }
    out << "]}";
    Emit("state", out.str(), tick);
    if (tick % 100 == 0) writer.Flush();
}

void EndSegment() {
    if (!inSegment || stopped) return;
    Emit("segment_end", "{\"reason\":\"exit_fight\"}", std::max(Clock(),lastTick));
    writer.Flush();
#ifdef LVZ_AVZ_HOSTED_SCRIPT
    // The hosted observation file gets the same treatment as events.jsonl: one
    // flush per segment boundary, so a fight that ends leaves readable
    // evidence even if the process is later killed.
    hosted_observation::Flush();
#endif
    inSegment=false;
}

void Close(bool stopRuntime=true) {
    if (!opened || stopped) return;
    if(stopRuntime) runtime::Shutdown();
    EndSegment();
    writer.Close(); stopped=true;
#ifdef LVZ_AVZ_HOSTED_SCRIPT
    hosted_observation::Close();
#endif
    if (runLock != INVALID_HANDLE_VALUE) { CloseHandle(runLock); runLock=INVALID_HANDLE_VALUE; }
    std::filesystem::remove(runDir / "capture.lock");
    std::ofstream(runDir / "capture.closed") << "Native recorder closed normally.\n";
}
} // namespace

APlant* Plant(APlantType type, int row, float col) {
    if (!opened || stopped || !AGetMainObject() || AGetPvzBase()->GameUi()!=3)
        throw std::runtime_error("Logged actions require an active recorder and fight");
    Capture();
    auto result = ACard(type, row, col);
    Emit("action", "{\"op\":\"plant\",\"type\":"+std::to_string(int(type))
        +",\"row\":"+std::to_string(row)+",\"col\":"+std::to_string(col)
        +",\"success\":"+(result?"true":"false")+"}");
    return result;
}

bool Shovel(int row, float col, int targetType) {
    if (!opened || stopped || !AGetMainObject() || AGetPvzBase()->GameUi()!=3)
        throw std::runtime_error("Logged actions require an active recorder and fight");
    Capture();
    std::set<uint32_t> before;
    for(auto& plant:aAlivePlantFilter) before.insert(plant.Id());
    AShovel(row,col,targetType);
    for(auto& plant:aAlivePlantFilter) before.erase(plant.Id());
    bool changed = !before.empty();
    Emit("action", "{\"op\":\"shovel\",\"row\":"+std::to_string(row)
        +",\"col\":"+std::to_string(col)+",\"target_type\":"+std::to_string(targetType)
        +",\"success\":"+(changed?"true":"false")+"}");
    return changed;
}

AZombie* SpawnZombie(AZombieType type, int row, int col) {
    if (!opened || stopped || !AGetMainObject() || AGetPvzBase()->GameUi()!=3)
        throw std::runtime_error("Logged actions require an active recorder and fight");
    Capture();
    auto* board = AGetMainObject();
    // The primitive at engine 0x42A0F0 dereferences AddZombieInRow's result
    // without a check, so a spawn the pool cannot take would crash the game
    // thread. Reserve room for the new zombie, the engine's own last pool slot
    // and the three extra riders a bobsled team allocates.
    const int live = board->ZombieCount();
    if (!runtime::SpawnPoolAccepts(live, board->ZombieLimit(), runtime::SpawnExtraPoolSlots(int(type)))) {
        Emit("action", "{\"op\":\"spawn\",\"type\":"+std::to_string(int(type))
            +",\"row\":"+std::to_string(row)+",\"col\":"+std::to_string(col)
            +",\"success\":false,\"reason\":\"zombie_pool_full\"}");
        return nullptr;
    }
    // AvZ takes 0-based engine grid indices; the caller passes protocol coordinates.
    auto result = AAsm::PutZombie(row-1,col-1,type);
    const bool created = result && result->Id() && board->ZombieCount()>live;
    Emit("action", "{\"op\":\"spawn\",\"type\":"+std::to_string(int(type))
        +",\"row\":"+std::to_string(row)+",\"col\":"+std::to_string(col)
        +",\"success\":"+(created?"true":"false")+"}");
    return created ? result : nullptr;
}

void Note(const std::string& text) { Emit("note", "{\"text\":"+Quote(text)+"}"); }
void RecordRuntime(const std::string& kind, const std::string& jsonPayload) {
    Emit("runtime_"+kind,jsonPayload,-1,(kind=="pre_step"||kind=="post_step")?kind:"control_boundary");
    if(kind=="request_completed") writer.Flush();
}
void CloseRecording() { Close(false); }
} // namespace lvz

void AScript() {
    lvz::OpenRun();
    ASetReloadMode(AReloadMode::MAIN_UI_OR_FIGHT_UI);
    // Recording only: manual card choice, no SetZombies, no automatic policy or save writes.
    AConnect('7', [] { lvz::Close(); });
#ifdef LVZ_AVZ_HOSTED_SCRIPT
    // Optional hosted script (see logger/avz/hosted_script.hpp). It runs as one
    // more AvZ coroutine under the runtime's frame ownership: ScriptHook()
    // reaches RunScript(), whose operation queue resumes it at the ATime it
    // asked for. The runtime keeps pause, IPC and the engine-call boundary.
    lvz::hosted::Launch();
#endif
}
AOnBeforeTick(lvz::Capture());
#ifdef LVZ_AVZ_HOSTED_SCRIPT
// Hosted builds sample the published script state after the frame's script
// work; see lvz::SampleHosted above.
AOnAfterTick(lvz::SampleHosted());
#endif
AOnAfterInject(lvz::OpenRun(); lvz::runtime::Start(lvz::runDir));
AOnExitFight(lvz::EndSegment());
AOnBeforeExit(lvz::Close());
