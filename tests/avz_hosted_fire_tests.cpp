// Offline proof that a hosted AvZ cannon shot leaves exactly one audit record
// and that the audit-only overlay changes no engine call.
//
// Built twice from this one source (see CMakeLists.txt), with the same fake PvZ
// image as tests/avz_hosted_script_tests.cpp:
//   * avz_hosted_fire_tests         - the generated overlay of the pinned AvZ
//     cob manager (build/cmake/avz_cob_manager_overlay.cpp) plus the real
//     record path (runtime/hosted_fire.cpp, determinism/hosted_fire.cpp), i.e.
//     the way a hosted recorder.dll is built.
//   * avz_hosted_fire_default_tests - the pristine upstream cob manager and the
//     same record module without LVZ_AVZ_HOSTED_FIRE_AUDIT, i.e. the way the
//     default recorder.dll is built (which links the module not at all, and the
//     default target of tests/test_avz_hosted_script.py asserts that wiring).
//
// Really linked and run here: AvZ's own ACobManager::_BasicFire (pristine or
// overlaid), AGridToCoordinate, AAsm::Fire/ReleaseMouse/GridToOrdinate call
// sites, the logger and the runtime's real hosted-fire record path.
//
// Test doubles, and why they sit on the boundary:
//   * AAsm::Fire, AAsm::ReleaseMouse and AAsm::GridToOrdinate are redirected by
//     `-Wl,--wrap`: in the game they are inline-asm calls into PvZ at fixed
//     RVAs, which only exist inside the game process. Each double counts the
//     call and its arguments, so the engine call sequence is byte-comparable
//     between the two builds.
//   * lvz::runtime::CurrentVersion() stands in for the resident runtime's
//     controller version, exactly like the frame gate in the hosted-script test.
//
// Proven here: with the audit-only hook compiled in, three accepted shots
// produce exactly three audit records (and nothing else), the records carry the
// tick/row/column/plant identity the hook read, the per-boundary state
// count/digest match an independent encoding of those records, the queue drains
// once, and the engine call sequence is byte-identical to the default build's.
// Not proven here: the real engine (a real cannon firing), the audit writer's
// file layout (determinism/audit.cpp needs the real image) and the live
// acceptance run.
//
// Harness note: this test drives shots _BasicFire refuses (wrong plant type,
// still reloading), so AvZ logs them at ERROR level - the production level the
// overlay sets. AAbstractLogger::_CreateHeader reads
// AMainObject::CompletedRounds(), which dereferences the challenge pointer at
// board+0x160, so the fake board points that field at a zeroed block. Without
// it the fake image faults in the logger, exactly like the INFO-level header
// fault tests/avz_hosted_script_tests.cpp records.

#include <avz.h>

#include "determinism/hosted_fire.hpp"
#include "runtime/diagnostics.hpp"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <string>
#include <vector>

#include <Windows.h>

namespace {
// AvZ reads the game through fixed addresses and documented offsets; all of
// them come from avz/framework/inc/avz_pvz_struct.h.
constexpr uintptr_t kAppPointerCell = 0x6a9ec0; // AGetPvzBase()
constexpr std::size_t kPageSize = 0x1000;

constexpr std::size_t kAppBoard = 0x768;
constexpr std::size_t kAppLevelId = 0x7f8;
constexpr std::size_t kAppGameUi = 0x7fc;
constexpr std::size_t kAppD3d = 0x36c;
constexpr std::size_t kAppMouseWindow = 0x320;

constexpr std::size_t kBoardPlantArray = 0xac;
constexpr std::size_t kBoardItemArray = 0xe4;
constexpr std::size_t kBoardMouseAttribution = 0x138;
constexpr std::size_t kBoardSeedArray = 0x144;
constexpr std::size_t kBoardChallenge = 0x160;
constexpr std::size_t kBoardZombieArray = 0x90;
constexpr std::size_t kBoardTotalWave = 0x5564;
constexpr std::size_t kBoardGameClock = 0x5568;
constexpr std::size_t kBoardGlobalClock = 0x556c;
constexpr std::size_t kBoardWave = 0x557c;
constexpr std::size_t kBoardRefreshCountdown = 0x559c;
constexpr std::size_t kBoardInitialCountdown = 0x55a0;
constexpr std::size_t kBoardLevelEndCountdown = 0x5604;

// APlant (avz/framework/inc/avz_pvz_struct.h).
constexpr std::size_t kPlantStride = 0x14c;
constexpr std::size_t kPlantRow = 0x1c;
constexpr std::size_t kPlantType = 0x24;
constexpr std::size_t kPlantCol = 0x28;
constexpr std::size_t kPlantState = 0x3c;
constexpr std::size_t kPlantId = 0x148;
constexpr int kCannonReadyState = 37; // APlant state "ready to fire"

uint8_t* app = nullptr;
uint8_t* board = nullptr;
uint8_t* plants = nullptr;
std::string imageError;

// AvZ dereferences the App pointer at a fixed address 2.7 MB above the default
// image base; the padding pins that page into this module's own image, exactly
// like tests/avz_hosted_script_tests.cpp does.
__attribute__((used)) unsigned char kAppCellImagePadding[0x100000];

#ifdef LVZ_AVZ_HOSTED_FIRE_AUDIT
constexpr const char* kVariant = "hosted";
#else
constexpr const char* kVariant = "default";
#endif

void Check(bool condition, const std::string& why) {
    if (!condition) throw std::runtime_error(why);
}

LONG WINAPI FaultHandler(EXCEPTION_POINTERS* info) {
    const auto* record = info->ExceptionRecord;
    char text[256];
    std::snprintf(text, sizeof(text), "unhandled exception code=0x%lx eip=%p",
        record->ExceptionCode, record->ExceptionAddress);
    std::fprintf(stderr, "%s\n", text);
    return EXCEPTION_EXECUTE_HANDLER;
}

void InstallFaultHandler() {
    SetErrorMode(SEM_NOGPFAULTERRORBOX);
    SetUnhandledExceptionFilter(FaultHandler);
}

uint8_t* Allocate(std::size_t size) {
    void* memory = VirtualAlloc(nullptr, size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!memory) throw std::runtime_error("fake image allocation failed");
    std::memset(memory, 0, size);
    return static_cast<uint8_t*>(memory);
}

template <typename T>
void Put(uint8_t* base, std::size_t offset, T value) {
    std::memcpy(base + offset, &value, sizeof(T));
}

template <typename T>
T Get(uint8_t* base, std::size_t offset) {
    T value{};
    std::memcpy(&value, base + offset, sizeof(T));
    return value;
}

bool ClaimAppCell() {
    MEMORY_BASIC_INFORMATION region{};
    if (!VirtualQuery(reinterpret_cast<void*>(kAppPointerCell), &region, sizeof(region)))
        return false;
    if (region.Type != MEM_IMAGE || region.AllocationBase != GetModuleHandleW(nullptr))
        return false;
    if (region.State != MEM_COMMIT)
        return VirtualAlloc(reinterpret_cast<void*>(kAppPointerCell), sizeof(void*),
                   MEM_COMMIT, PAGE_READWRITE) != nullptr;
    DWORD previous = 0;
    return VirtualProtect(reinterpret_cast<void*>(kAppPointerCell), sizeof(void*),
        PAGE_READWRITE, &previous) != 0;
}

// APlant slot the way the engine lays it out: 1-based row/column live in the
// captured state, so the plant's own Row()/Col() are 0-based and the hook adds
// one, exactly like the recorder's plant records do.
void PlantSlot(int slot, int type, int state, int row, int col, uint32_t id) {
    auto* plant = plants + std::size_t(slot) * kPlantStride;
    Put<int>(plant, kPlantRow, row);
    Put<int>(plant, kPlantType, type);
    Put<int>(plant, kPlantCol, col);
    Put<int>(plant, kPlantState, state);
    Put<uint32_t>(plant, kPlantId, id);
}

void InstallFakeImage() noexcept {
    if (!ClaimAppCell()) {
        imageError = "AvZ's App cell 0x6a9ec0 is not this module's own image page";
        return;
    }
    try {
        app = Allocate(kPageSize);
        board = Allocate(0x8000);
        plants = Allocate(8 * kPlantStride);
        Put<uint32_t>(app, kAppMouseWindow, reinterpret_cast<uint32_t>(Allocate(kPageSize)));
        Put<uint32_t>(board, kBoardPlantArray, reinterpret_cast<uint32_t>(plants));
        Put<uint32_t>(board, kBoardItemArray, reinterpret_cast<uint32_t>(Allocate(kPageSize)));
        Put<uint32_t>(board, kBoardMouseAttribution, reinterpret_cast<uint32_t>(Allocate(kPageSize)));
        Put<uint32_t>(board, kBoardSeedArray, reinterpret_cast<uint32_t>(Allocate(kPageSize)));
        Put<uint32_t>(board, kBoardZombieArray, reinterpret_cast<uint32_t>(Allocate(kPageSize)));
        // AMainObject::CompletedRounds() (the logger header reads it) walks this
        // pointer; a zeroed block reports zero completed rounds.
        Put<uint32_t>(board, kBoardChallenge, reinterpret_cast<uint32_t>(Allocate(kPageSize)));
    } catch (const std::exception& error) {
        imageError = error.what();
        return;
    }

    *reinterpret_cast<uint8_t**>(kAppPointerCell) = app;
    Put<uint32_t>(app, kAppBoard, reinterpret_cast<uint32_t>(board));
    Put<int>(app, kAppLevelId, 13);  // survival endless, not the zen garden
    Put<int>(app, kAppGameUi, 3);    // fight screen
    Put<uint32_t>(app, kAppD3d, 0);  // no 3D acceleration: painter stays idle

    Put<int>(board, kBoardTotalWave, 20);
    Put<int>(board, kBoardGameClock, 40);
    Put<int>(board, kBoardGlobalClock, 40);
    Put<int>(board, kBoardWave, 1);
    Put<int>(board, kBoardRefreshCountdown, 0);
    Put<int>(board, kBoardInitialCountdown, 0);
    Put<int>(board, kBoardLevelEndCountdown, 0);

    // Slot 3 and slot 7 are ready cannons, the two the test fires. Slot 0 is not
    // a cannon and slot 1 is a cannon that is still reloading: _BasicFire must
    // refuse both, and a refused shot must leave no record behind.
    PlantSlot(3, ACOB_CANNON, kCannonReadyState, 0, 2, 0x00010003u);
    PlantSlot(7, ACOB_CANNON, kCannonReadyState, 3, 5, 0x00020007u);
    PlantSlot(0, 0, kCannonReadyState, 0, 0, 0x00010000u);
    PlantSlot(1, ACOB_CANNON, 35, 0, 1, 0x00010001u);
}

struct FakeImageInstaller {
    FakeImageInstaller() {
        InstallFaultHandler();
        InstallFakeImage();
    }
};
__attribute__((init_priority(101))) FakeImageInstaller fakeImageInstaller;

// --- engine entry doubles: `-Wl,--wrap` redirects the reviewed call sites ---
struct EngineCall {
    std::string line;
};
std::vector<EngineCall> engineCalls;

void Engine(const std::string& line) { engineCalls.push_back({line}); }

// --- trace files -----------------------------------------------------------
std::filesystem::path TraceRoot() { return std::filesystem::path(LVZ_FIRE_TRACE_ROOT); }
std::filesystem::path VariantDir(const std::string& variant) { return TraceRoot() / variant; }

void WriteLines(const std::filesystem::path& file, const std::vector<std::string>& lines) {
    std::filesystem::create_directories(file.parent_path());
    std::ofstream output(file, std::ios::binary | std::ios::trunc);
    for (const auto& line : lines) output << line << '\n';
    if (!output) throw std::runtime_error("cannot write " + file.string());
}

std::vector<std::string> ReadLines(const std::filesystem::path& file) {
    std::ifstream input(file, std::ios::binary);
    if (!input) throw std::runtime_error("cannot read " + file.string());
    std::vector<std::string> lines;
    for (std::string line; std::getline(input, line);) lines.push_back(line);
    return lines;
}

// The audit's diagnostic FNV-1a over the payload's integer fields, in the order
// determinism/hosted_fire.cpp documents and src/llm_vs_zombies/audit_compare.py
// re-implements; the test recomputes it independently of the module.
#ifdef LVZ_AVZ_HOSTED_FIRE_AUDIT
uint64_t Mix(uint64_t digest, uint64_t value) {
    for (unsigned byte = 0; byte < 8; ++byte) {
        digest ^= uint8_t(value);
        digest *= 1099511628211ULL;
        value >>= 8;
    }
    return digest;
}

uint64_t ExpectedDigest(const std::vector<nlohmann::json>& payloads) {
    uint64_t digest = 14695981039346656037ULL;
    for (const auto& payload : payloads) {
        for (const char* field : {"plant_index", "plant_id", "plant_row", "plant_col",
                                  "target_row", "target_col_bits", "tick"})
            digest = Mix(digest, static_cast<uint64_t>(payload.at(field).get<int64_t>()));
    }
    return digest;
}
#endif

// _BasicFire is a protected static member of AvZ's cob manager; the harness
// reaches the reviewed body through the standard access-declaration route.
struct FireHarness : ACobManager {
    using ACobManager::_BasicFire;
};

std::string ColumnText(float value) {
    char text[32];
    std::snprintf(text, sizeof(text), "%.3f", value);
    return text;
}
} // namespace

// --- engine doubles --------------------------------------------------------
extern "C" void __wrap__ZN4AAsm4FireEiii(int x, int y, int rank) {
    Engine("fire x=" + std::to_string(x) + " y=" + std::to_string(y)
        + " rank=" + std::to_string(rank));
}
extern "C" void __wrap__ZN4AAsm12ReleaseMouseEv() { Engine("release_mouse"); }
extern "C" int __wrap__ZN4AAsm14GridToOrdinateEii(int row, int col) {
    Engine("grid_to_ordinate row=" + std::to_string(row) + " col=" + std::to_string(col));
    return 100; // y for the fake board; AGridToCoordinate adds 40 and clamps
}

// --- test doubles for the hosted runtime ----------------------------------
namespace lvz::runtime {
nlohmann::json reportedVersion = nlohmann::json::object();
int auditFaults = 0;
std::string lastAuditFault;
// The resident runtime's controller version at the moment of the shot.
nlohmann::json CurrentVersion() { return reportedVersion; }
// The runtime side turns a recording fault into an invalidated run instead of
// letting it reach the script; the test counts them and expects none.
void ReportAuditFault(const std::string& message) { ++auditFaults; lastAuditFault = message; }
bool Started() { return true; }
bool BeforeFrame() { return true; }
bool AfterAvzRunTotal() { return true; }
bool RunOneEngineFrame() { return true; }
void RecordEnvironmentCollect(uint32_t, int, int, int) {}
} // namespace lvz::runtime

namespace lvz::determinism {
bool ValidateTargetImage() noexcept { return true; }
} // namespace lvz::determinism

// The overlay of the pinned avz_script.cpp calls this; the fire path never does.
void AScript() {}

int main() {
    InstallFaultHandler();
    try {
        Check(imageError.empty(), "fake PvZ image could not be installed: " + imageError);
        // Production AvZ initialisation: the logger level ({ERROR, WARNING}) and
        // the tick managers _BasicFire's aLogger->Info(...) writes through.
        __aScriptManager.GlobalInit();

        struct Shot {
            int plantIndex;
            int targetRow;
            float targetCol;
            nlohmann::json version;
            bool accepted;
        };
        const std::vector<Shot> shots = {
            {3, 2, 9.0f, {{"epoch", 1}, {"tick", 40}, {"revision", 0}}, true},
            {7, 5, 9.0f, {{"epoch", 1}, {"tick", 40}, {"revision", 0}}, true},
            {0, 1, 1.0f, {{"epoch", 1}, {"tick", 40}, {"revision", 0}}, false},
            {1, 1, 1.0f, {{"epoch", 1}, {"tick", 40}, {"revision", 0}}, false},
            // Same boundary, unsigned tick: the record must accept both JSON
            // integer spellings of the version the runtime reports.
            {3, 4, 7.625f, {{"epoch", 1u}, {"tick", 41u}, {"revision", 0u}}, true},
        };

        std::vector<nlohmann::json> expectedPayloads, expectedVersions;
        for (const auto& shot : shots) {
            lvz::runtime::reportedVersion = shot.version;
            const auto before = engineCalls.size();
            // The reviewed ACobManager::_BasicFire body (pristine upstream file,
            // or the overlay generated from it), driven exactly as the hosted
            // script's aCobManager.Fire(...) drives it.
            FireHarness::_BasicFire(shot.plantIndex, shot.targetRow, shot.targetCol);
            const auto after = engineCalls.size();
            // _BasicFire always releases the mouse first; an accepted shot then
            // converts the grid position and fires exactly once before releasing
            // the mouse again. The audit-only insertion must add no call in
            // either case.
            const std::size_t expectedCalls = shot.accepted ? 4 : 1;
            Check(after - before == expectedCalls,
                "the engine call sequence of shot plant_index=" + std::to_string(shot.plantIndex)
                + " changed: expected " + std::to_string(expectedCalls) + " calls, got "
                + std::to_string(after - before));
            if (!shot.accepted) continue;
            auto* plant = plants + std::size_t(shot.plantIndex) * kPlantStride;
            uint32_t bits = 0;
            const float column = shot.targetCol;
            std::memcpy(&bits, &column, sizeof(bits));
            expectedPayloads.push_back({{"source", "hosted"},
                {"op", "fire"},
                {"plant_index", shot.plantIndex},
                {"plant_id", Get<uint32_t>(plant, kPlantId)},
                {"plant_row", Get<int>(plant, kPlantRow) + 1},
                {"plant_col", Get<int>(plant, kPlantCol) + 1},
                {"target_row", shot.targetRow},
                {"target_col_bits", bits},
                {"target_col_text", ColumnText(shot.targetCol)},
                {"tick", static_cast<uint64_t>(shot.version["tick"].get<int64_t>())}});
            expectedVersions.push_back(shot.version);
        }
        Check(expectedPayloads.size() == 3, "the accepted-shot count changed");
        const auto expectedCalls = 4 + 4 + 1 + 1 + 4;
        Check(engineCalls.size() == expectedCalls, "the engine call total changed");
        Check(lvz::runtime::auditFaults == 0,
            "the audit-only hook reported a fault: " + lvz::runtime::lastAuditFault);

        // The trace the two builds must agree on byte for byte.
        std::vector<std::string> trace;
        for (const auto& call : engineCalls) trace.push_back(call.line);
        WriteLines(VariantDir(kVariant) / "engine-trace.jsonl", trace);

        // Audit-facing records: exactly one per accepted shot, in shot order.
        const auto records = lvz::determinism::DrainHostedFireEvents(0);
        std::vector<std::string> recordLines;
        for (const auto& record : records) recordLines.push_back(record.dump());
        WriteLines(VariantDir(kVariant) / "records.jsonl", recordLines);

#ifdef LVZ_AVZ_HOSTED_FIRE_AUDIT
        Check(lvz::determinism::HostedFireEnabled(), "a hosted build reported the audit as disabled");
        const auto manifest = lvz::determinism::HostedFireManifest();
        Check(manifest.is_object() && manifest.value("mode", std::string()) == "hosted_fire_audit_v1"
            && manifest.value("installed", false) && manifest.value("kind", std::string()) == "hosted_fire",
            "the hosted-fire manifest declaration changed");

        Check(records.is_array() && records.size() == expectedPayloads.size(),
            "one hosted shot did not produce exactly one audit record");
        Check(lvz::determinism::DrainHostedFireEvents(0).empty(),
            "draining the hosted-fire queue twice produced records twice");
        for (std::size_t index = 0; index < records.size(); ++index) {
            const auto& record = records[index];
            Check(record.at("schema") == "lvz.audit.v1" && record.at("kind") == "hosted_fire",
                "the hosted-fire record's schema/kind changed");
            Check(record.at("phase") == "controlled_boundary"
                && record.at("native_phase") == "avz_basic_fire",
                "the hosted-fire record's phase changed");
            Check(record.at("seq") == static_cast<int>(index),
                "the hosted-fire record's sequence did not start at zero");
            Check(record.at("version") == expectedVersions[index],
                "the hosted-fire record was not bound to the shot's boundary version");
            Check(record.at("payload") == expectedPayloads[index],
                "the hosted-fire record payload changed: " + record.at("payload").dump());
        }
        const auto state = lvz::determinism::HostedFireState();
        Check(state.is_object() && state.value("mode", std::string()) == "hosted_fire_audit_v1",
            "the hosted-fire state component is missing its mode");
        Check(state.value("count", 0ull) == expectedPayloads.size(),
            "the hosted-fire state count does not match the records");
        Check(static_cast<uint64_t>(state.value("digest", 0ull)) == ExpectedDigest(expectedPayloads),
            "the hosted-fire state digest does not match the records");
        lvz::determinism::ResetHostedFire();
        Check(lvz::determinism::HostedFireState().value("count", 1ull) == 0,
            "ResetHostedFire left a count behind");
        std::printf("hosted fire audit: %zu records, one per accepted shot, %zu engine calls (unchanged)\n",
            records.size(), engineCalls.size());
#else
        Check(!lvz::determinism::HostedFireEnabled(), "a default build reported the audit as enabled");
        Check(records.is_array() && records.empty(),
            "a default build recorded a hosted shot");
        Check(lvz::determinism::HostedFireState().empty(),
            "a default build declared a hosted-fire state component");
        Check(lvz::determinism::HostedFireManifest().empty(),
            "a default build declared a hosted-fire manifest mode");
        Check(recordLines.empty(), "a default build wrote an audit record line");
        std::printf("default build: %zu engine calls, no hosted-fire audit evidence\n", engineCalls.size());
        return 0;
#endif

        // The engine call sequence must be exactly the default build's: the hook
        // may add audit records, never an engine call, a reorder or a different
        // argument.
        const auto reference = VariantDir("default") / "engine-trace.jsonl";
        Check(std::filesystem::exists(reference),
            "the default-build reference trace is missing; run ctest -R avz_hosted_fire");
        Check(ReadLines(reference) == trace,
            "the hosted build's engine call sequence differs from the default build's");
        const auto referenceRecords = VariantDir("default") / "records.jsonl";
        Check(std::filesystem::exists(referenceRecords)
            && ReadLines(referenceRecords).empty(),
            "the default build wrote hosted-fire audit records");
        std::printf("hosted fire audit: engine trace identical to the default build\n");
        return 0;
    } catch (const std::exception& error) {
        std::fprintf(stderr, "avz hosted fire test failed: %s\n", error.what());
        return 1;
    }
}
