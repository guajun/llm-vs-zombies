// Offline proof that a hosted AvZ cannon shot leaves exactly one audit record,
// that the audit drain hands those records to the writer which appends them to
// events.jsonl, and that the per-boundary state count/digest reconciles with
// them.
//
// Built twice from this one source (see CMakeLists.txt), with the same fake PvZ
// image as tests/avz_hosted_script_tests.cpp:
//   * avz_hosted_fire_tests         - the generated overlay of the pinned AvZ
//     cob manager (build/cmake/avz_cob_manager_overlay.cpp) plus the real
//     record/drain path (runtime/hosted_fire.cpp, determinism/hosted_fire.cpp),
//     i.e. the way a hosted recorder.dll is built.
//   * avz_hosted_fire_default_tests - the pristine upstream cob manager and the
//     same record module without LVZ_AVZ_HOSTED_FIRE_AUDIT, i.e. the way the
//     default recorder.dll is built (which links the module not at all, and the
//     default target of tests/test_avz_hosted_script.py asserts that wiring).
//
// Really linked and run here: AvZ's own ACobManager::_BasicFire (pristine or
// overlaid), AGridToCoordinate, the AAsm::Fire/ReleaseMouse/GridToOrdinate call
// sites, the logger, and the runtime's real hosted-fire record path plus the
// real drain (determinism::DrainHostedFireRecords, the function audit.cpp calls
// with `Write(events, ...)` as its sink).
//
// Test doubles, and why they sit on the boundary:
//   * AAsm::Fire, AAsm::ReleaseMouse and AAsm::GridToOrdinate are redirected by
//     `-Wl,--wrap`: in the game they are inline-asm calls into PvZ at fixed
//     RVAs, which only exist inside the game process. Each double counts the
//     call and its arguments, so the engine call sequence is byte-comparable
//     between the two builds.
//   * lvz::runtime::CurrentVersion()/ReportAuditFault() stand in for the
//     resident runtime's controller, exactly like the frame gate in the
//     hosted-script test.
//   * the drain's sink is a file here; in recorder.dll it is the audit writer's
//     `Write(events, ...)`, which appends the same JSON line.
//
// Why the first live run's failure is covered here: the live 12-cannon run
// recorded 20 shots whose plant ids were all >= 2^31 (0xDC740013 and up). The
// writer mixed each id from the signed `int` parameter (sign-extending it)
// while the record stores it as a uint32 (zero-extending it), so the boundary
// state digest disagreed with the records the reader recomputed from - the
// reader reported "hosted fire state count/digest differs from the verified
// records" even though both events were present. The 20 shots below use the
// same id range and the same "two shots per granted frame, drained at the next
// boundary" shape. The documented encoding (payload values, two's complement)
// is pinned twice: here for the first live shot's payload, and in
// tests/test_hosted_fire_audit.py, which recomputes the whole chain from the
// artifacts this test writes.
//
// Not proven here: the real engine (a real cannon firing) and the audit
// writer's own file layout (determinism/audit.cpp needs the real image to
// Initialize); the live acceptance run covers those.

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
constexpr std::size_t kBoardZombieArray = 0x90;
constexpr std::size_t kBoardChallenge = 0x160;
constexpr std::size_t kBoardTotalWave = 0x5564;
constexpr std::size_t kBoardGameClock = 0x5568;
constexpr std::size_t kBoardGlobalClock = 0x556c;
constexpr std::size_t kBoardWave = 0x557c;
constexpr std::size_t kBoardRefreshCountdown = 0x559c;
constexpr std::size_t kBoardInitialCountdown = 0x55a0;
constexpr std::size_t kBoardLevelEndCountdown = 0x5604;

// APlant (avz/framework/inc/avz_pvz_struct.h).
constexpr std::size_t kPlantStride = 0x14c;
constexpr std::size_t kPlantSlots = 64;
constexpr std::size_t kPlantRow = 0x1c;
constexpr std::size_t kPlantType = 0x24;
constexpr std::size_t kPlantCol = 0x28;
constexpr std::size_t kPlantState = 0x3c;
constexpr std::size_t kPlantId = 0x148;
constexpr int kCannonReadyState = 37; // APlant state "ready to fire"

// The first cannon of the live 12-cannon run, verbatim from its first
// hosted_fire record: plant_index 19, this id, 1-based row/column 1/5, target
// row 2, target column 9.0, tick 567.
constexpr uint32_t kLivePlantId = 0xDC740013u;
constexpr int kLiveTick = 567;
constexpr int kShotCount = 20; // ten P6 rounds, two cannons each
// FNV-1a of that shot's payload under the documented encoding (payload values,
// two's complement 64-bit words). It differs from the sign-extended value the
// pre-fix writer produced, which is exactly what failed the live run.
constexpr uint64_t kLiveShotDigest = 6388981459987454381ULL;

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
        plants = Allocate(kPlantSlots * kPlantStride);
        Put<uint32_t>(app, kAppMouseWindow, reinterpret_cast<uint32_t>(Allocate(kPageSize)));
        Put<uint32_t>(board, kBoardPlantArray, reinterpret_cast<uint32_t>(plants));
        Put<uint32_t>(board, kBoardItemArray, reinterpret_cast<uint32_t>(Allocate(kPageSize)));
        Put<uint32_t>(board, kBoardMouseAttribution, reinterpret_cast<uint32_t>(Allocate(kPageSize)));
        Put<uint32_t>(board, kBoardSeedArray, reinterpret_cast<uint32_t>(Allocate(kPageSize)));
        Put<uint32_t>(board, kBoardZombieArray, reinterpret_cast<uint32_t>(Allocate(kPageSize)));
        // AMainObject::CompletedRounds() (the logger header reads it when a
        // refused shot is logged at ERROR level) walks this pointer; a zeroed
        // block reports zero completed rounds.
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
    Put<int>(board, kBoardGameClock, kLiveTick);
    Put<int>(board, kBoardGlobalClock, kLiveTick);
    Put<int>(board, kBoardWave, 1);
    Put<int>(board, kBoardRefreshCountdown, 0);
    Put<int>(board, kBoardInitialCountdown, 0);
    Put<int>(board, kBoardLevelEndCountdown, 0);

    // Slot 0 is not a cannon and slot 1 is a cannon that is still reloading:
    // _BasicFire must refuse both, and a refused shot must leave no record.
    PlantSlot(0, 0, kCannonReadyState, 0, 0, 0x00010000u);
    PlantSlot(1, ACOB_CANNON, 35, 0, 1, 0x00010001u);
    // The fired cannons carry the live run's id range: 0xDC740013 and up, i.e.
    // every id is >= 2^31.
    for (int index = 0; index < kShotCount; ++index)
        PlantSlot(19 + index, ACOB_CANNON, kCannonReadyState, index % 5, 4 + (index % 4),
            kLivePlantId + static_cast<uint32_t>(index) * 0x1234u);
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
// re-implements. This test computes it independently of the module, from the
// payload the record carries, and takes each value as its two's-complement
// 64-bit pattern (a uint32 plant id zero-extends, a negative int sign-extends).
#ifdef LVZ_AVZ_HOSTED_FIRE_AUDIT
uint64_t Mix(uint64_t digest, uint64_t value) {
    for (unsigned byte = 0; byte < 8; ++byte) {
        digest ^= uint8_t(value);
        digest *= 1099511628211ULL;
        value >>= 8;
    }
    return digest;
}

uint64_t Word(const nlohmann::json& value) {
    return value.is_number_unsigned() ? value.get<uint64_t>()
                                      : static_cast<uint64_t>(value.get<int64_t>());
}

uint64_t ExpectedDigest(const std::vector<nlohmann::json>& payloads, std::size_t limit) {
    uint64_t digest = 14695981039346656037ULL;
    for (std::size_t index = 0; index < limit; ++index)
        for (const char* field : {"plant_index", "plant_id", "plant_row", "plant_col",
                                  "target_row", "target_col_bits", "tick"})
            digest = Mix(digest, Word(payloads[index].at(field)));
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
        };
        // Ten P6 rounds of two cannons: two shots per granted frame, exactly the
        // shape the live run recorded (ticks 567, 577, ... for the same wave).
        std::vector<Shot> shots;
        for (int index = 0; index < kShotCount; ++index)
            shots.push_back({19 + index, index % 2 ? 5 : 2, index % 3 == 2 ? 7.625f : 9.0f,
                {{"epoch", 3}, {"tick", kLiveTick + (index / 2) * 10}, {"revision", 0}}});

        // Two refused shots first: neither may leave a record, and both must
        // still produce the pristine engine call sequence (one mouse release).
        const std::pair<int, float> refused[] = {{0, 1.0f}, {1, 1.0f}};
        for (const auto& [plantIndex, targetCol] : refused) {
            lvz::runtime::reportedVersion = shots[0].version;
            const auto before = engineCalls.size();
            FireHarness::_BasicFire(plantIndex, 1, targetCol);
            Check(engineCalls.size() - before == 1,
                "a refused shot changed the engine call sequence");
        }

        std::vector<nlohmann::json> expectedPayloads, expectedVersions, envelopes, states;
        uint64_t sequence = 0;
        const auto sink = [&envelopes](const nlohmann::json& envelope) {
            envelopes.push_back(envelope);
        };
        for (const auto& shot : shots) {
            lvz::runtime::reportedVersion = shot.version;
            const auto before = engineCalls.size();
            // The reviewed ACobManager::_BasicFire body (pristine upstream file,
            // or the overlay generated from it), driven exactly as the hosted
            // script's aCobManager.Fire(...) drives it.
            FireHarness::_BasicFire(shot.plantIndex, shot.targetRow, shot.targetCol);
            Check(engineCalls.size() - before == 4,
                "the engine call sequence of an accepted shot changed");
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
            states.push_back(lvz::determinism::HostedFireState());
            // Two shots per granted frame: the next audited boundary drains both,
            // with the running sequence counter the audit writer owns.
            if (expectedPayloads.size() % 2 == 0)
                sequence += lvz::determinism::DrainHostedFireRecords(sink, sequence);
        }
        Check(expectedPayloads.size() == kShotCount, "the accepted-shot count changed");
        Check(lvz::determinism::DrainHostedFireRecords(sink, sequence) == 0,
            "the drain left queued records behind after the last boundary");
        const auto expectedCalls = kShotCount * 4 + 2;
        Check(engineCalls.size() == expectedCalls, "the engine call total changed");
        Check(lvz::runtime::auditFaults == 0,
            "the audit-only hook reported a fault: " + lvz::runtime::lastAuditFault);

        // The trace the two builds must agree on byte for byte.
        std::vector<std::string> trace;
        for (const auto& call : engineCalls) trace.push_back(call.line);
        WriteLines(VariantDir(kVariant) / "engine-trace.jsonl", trace);

        // The audit-facing records: one envelope per accepted shot, in shot
        // order, with the sequence the writer would have given them. In
        // recorder.dll the sink is `Write(events, record)`, which appends
        // exactly this line to audit/events.jsonl.
        std::vector<std::string> recordLines;
        for (const auto& envelope : envelopes) recordLines.push_back(envelope.dump());
        WriteLines(VariantDir(kVariant) / "records.jsonl", recordLines);

#ifdef LVZ_AVZ_HOSTED_FIRE_AUDIT
        Check(lvz::determinism::HostedFireEnabled(), "a hosted build reported the audit as disabled");
        const auto manifest = lvz::determinism::HostedFireManifest();
        Check(manifest.is_object() && manifest.value("mode", std::string()) == "hosted_fire_audit_v1"
            && manifest.value("installed", false) && manifest.value("kind", std::string()) == "hosted_fire",
            "the hosted-fire manifest declaration changed");

        Check(envelopes.size() == expectedPayloads.size(),
            "one hosted shot did not produce exactly one audit record");
        Check(sequence == kShotCount, "the drain did not advance the writer's sequence counter");
        for (std::size_t index = 0; index < envelopes.size(); ++index) {
            const auto& envelope = envelopes[index];
            Check(envelope.at("schema") == "lvz.audit.v1" && envelope.at("kind") == "hosted_fire",
                "the hosted-fire record's schema/kind changed");
            Check(envelope.at("phase") == "controlled_boundary"
                && envelope.at("native_phase") == "avz_basic_fire",
                "the hosted-fire record's phase changed");
            Check(envelope.at("seq") == static_cast<int>(index),
                "the hosted-fire record's sequence did not start at zero");
            Check(envelope.at("version") == expectedVersions[index],
                "the hosted-fire record was not bound to the shot's boundary version");
            Check(envelope.at("payload") == expectedPayloads[index],
                "the hosted-fire record payload changed: " + envelope.at("payload").dump());
        }
        // The per-boundary state must account for every record seen so far.
        for (std::size_t index = 0; index < states.size(); ++index) {
            const auto& state = states[index];
            Check(state.is_object() && state.value("mode", std::string()) == "hosted_fire_audit_v1"
                && state.value("count", 0ULL) == index + 1,
                "the hosted-fire state count does not match the records");
            Check(static_cast<uint64_t>(state.value("digest", 0ULL))
                    == ExpectedDigest(expectedPayloads, index + 1),
                "the hosted-fire state digest does not match the records");
        }
        // The encoding is pinned cross-language for the first live shot: this
        // value is what the Python reader computes from the recorded payload.
        Check(ExpectedDigest(expectedPayloads, 1) == kLiveShotDigest,
            "the pinned live-shot digest changed");
        Check(static_cast<uint64_t>(states.front().value("digest", 0ULL)) == kLiveShotDigest,
            "the module's digest for the live shot differs from the pinned value");
        // The artifacts tests/test_hosted_fire_audit.py reconciles against.
        std::vector<std::string> stateLines;
        for (std::size_t index = 0; index < states.size(); ++index)
            stateLines.push_back(nlohmann::json{{"records", index + 1}, {"state", states[index]}}.dump());
        WriteLines(VariantDir(kVariant) / "state.jsonl", stateLines);

        lvz::determinism::ResetHostedFire();
        Check(lvz::determinism::HostedFireState().value("count", 1ULL) == 0,
            "ResetHostedFire left a count behind");
        std::printf("hosted fire audit: %zu records, one per accepted shot, %zu engine calls (unchanged)\n",
            envelopes.size(), engineCalls.size());
#else
        Check(!lvz::determinism::HostedFireEnabled(), "a default build reported the audit as enabled");
        Check(envelopes.empty() && recordLines.empty(),
            "a default build wrote a hosted-fire audit record");
        for (const auto& state : states)
            Check(state.empty(), "a default build declared a hosted-fire state component");
        Check(lvz::determinism::HostedFireManifest().empty(),
            "a default build declared a hosted-fire manifest mode");
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
        Check(std::filesystem::exists(referenceRecords) && ReadLines(referenceRecords).empty(),
            "the default build wrote hosted-fire audit records");
        std::printf("hosted fire audit: engine trace identical to the default build\n");
        return 0;
    } catch (const std::exception& error) {
        std::fprintf(stderr, "avz hosted fire test failed: %s\n", error.what());
        return 1;
    }
}
