#include "recorder.hpp"
#include "buffered_writer.hpp"
#include <iomanip>
#include <set>
#include <sstream>

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
void Emit(const std::string& kind, const std::string& payload, int tick = -1) {
    if (!opened || stopped) return;
    if (tick < 0) tick = Clock();
    writer.Append("{\"schema_version\":1,\"run_id\":" + Quote(runId)
        + ",\"seq\":" + std::to_string(sequence++) + ",\"segment\":" + std::to_string(std::max(0, segment))
        + ",\"tick\":" + std::to_string(tick) + ",\"phase\":\"avz_callback\",\"kind\":" + Quote(kind)
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
    try { writer.Open(runDir / "events.jsonl"); }
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
    Emit("segment_end", "{\"reason\":\"exit_fight\"}", std::max(0,lastTick));
    writer.Flush();
    inSegment=false;
}

void Close() {
    if (!opened || stopped) return;
    EndSegment();
    writer.Close(); stopped=true;
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
    auto before = AGetMainObject()->PlantCount();
    AShovel(row,col,targetType);
    bool changed = AGetMainObject()->PlantCount() < before;
    Emit("action", "{\"op\":\"shovel\",\"row\":"+std::to_string(row)
        +",\"col\":"+std::to_string(col)+",\"target_type\":"+std::to_string(targetType)
        +",\"count_decreased\":"+(changed?"true":"false")+"}");
    return changed;
}

void Note(const std::string& text) { Emit("note", "{\"text\":"+Quote(text)+"}"); }
} // namespace lvz

void AScript() {
    lvz::OpenRun();
    ASetReloadMode(AReloadMode::MAIN_UI_OR_FIGHT_UI);
    // Recording only: manual card choice, no SetZombies, no automatic policy or save writes.
    AConnect('7', [] { lvz::Close(); });
}
AOnBeforeTick(lvz::Capture());
AOnExitFight(lvz::EndSegment());
AOnBeforeExit(lvz::Close());
