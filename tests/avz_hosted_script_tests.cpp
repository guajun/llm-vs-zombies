// Offline proof that a frame granted by the resident runtime advances a hosted
// AvZ script coroutine.
//
// Really linked and run here:
//   * the generated overlay of the pinned upstream avz_script.cpp
//     (build/cmake/avz_script_overlay.cpp - the very file recorder.dll is built
//     from, produced by runtime/avz_overlay.cmake),
//   * AvZ's own ScriptHook / RunTotal / RunScript, coroutine, tick-runner,
//     state-hook, time-queue, logger, painter and assembly translation units,
//   * the hosted script logger/avz/hosted/atime_probe.cpp.
// AvZ reads the game through its normal accessors against a fake PvZ image: the
// real App pointer cell (0x6a9ec0) plus a fake APvzBase/AMainObject filled with
// the documented offsets from avz/framework/inc/avz_pvz_struct.h. No AvZ source
// is patched, and no engine code is emulated.
//
// The two test doubles, and why they sit on the boundary:
//   * the runtime frame gate (Started/BeforeFrame/AfterAvzRunTotal/
//     RunOneEngineFrame) stands in for recorder.dll. A granted frame is the only
//     thing that advances the fake clock, exactly like a controller step.
//   * LoadScript/MemoryInit is not entered, because its BeforeScript hook set
//     calls PvZ code at fixed RVAs (AFieldInfo::_BeforeScript ->
//     AAsm::GridToOrdinate -> `call *0x41C740`), which exists only inside the
//     game process. The test starts from the state the first load frame leaves
//     behind - script loaded, per-wave queues built - and calls AScript(), which
//     is exactly what LoadScript() does on the game's first load frame.
//
// Proven here: a granted frame reaches the real ScriptHook -> RunTotal ->
// RunScript and resumes the hosted coroutine at its ATime targets; a closed gate
// advances neither the script nor the clock. Not proven here: anything needing
// the real engine (the load-frame hook set, a real Board, cannons, wave
// refresh) - that is the live acceptance run. Hosting a script says nothing
// about whether a 12-cannon script wins a level.
//
// Known harness limitation: raising the logger to INFO inside this fake image
// faults in AAbstractLogger::_CreateHeader while it formats its "[wave, time]"
// header, so the test keeps the production level ({ERROR, WARNING}) and cannot
// use AvZ's own log lines as a cross-check. The same formatting path runs
// against the real App in the game; it is a limitation of the fake image, not
// of the hosting mechanism, and it is reported as an open item.

#include <avz.h>

#include "hosted_script.hpp"
#include "hosted/atime_probe.hpp"
#include "runtime/diagnostics.hpp"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <Windows.h>

namespace {
// AvZ reads the game through fixed addresses and documented offsets; both come
// from avz/framework/inc/avz_pvz_struct.h.
constexpr uintptr_t kAppPointerCell = 0x6a9ec0; // AGetPvzBase()
constexpr uintptr_t kGameUpdateAsm = 0x41600E;  // __AGameControllor::_oriAsm source
constexpr std::size_t kPageSize = 0x1000;

constexpr std::size_t kAppMouseWindow = 0x320;
constexpr std::size_t kAppBoard = 0x768;
constexpr std::size_t kAppLevelId = 0x7f8;
constexpr std::size_t kAppGameUi = 0x7fc;
constexpr std::size_t kAppD3d = 0x36c;

constexpr std::size_t kBoardPlantArray = 0xac;
constexpr std::size_t kBoardItemArray = 0xe4;
constexpr std::size_t kBoardMouseAttribution = 0x138;
constexpr std::size_t kBoardSeedArray = 0x144;
constexpr std::size_t kBoardZombieArray = 0x90;
constexpr std::size_t kBoardTotalWave = 0x5564;
constexpr std::size_t kBoardGameClock = 0x5568;
constexpr std::size_t kBoardGlobalClock = 0x556c;
constexpr std::size_t kBoardWave = 0x557c;
constexpr std::size_t kBoardRefreshCountdown = 0x559c;
constexpr std::size_t kBoardInitialCountdown = 0x55a0;
constexpr std::size_t kBoardLevelEndCountdown = 0x5604;

uint8_t* app = nullptr;
uint8_t* board = nullptr;
bool grantFrame = false;
int grantedFrames = 0;
int engineCalls = 0;
std::string imageError;

void Check(bool condition, const std::string& why) {
    if (!condition) throw std::runtime_error(why);
}

// Written with the Win32 API so it also works from a static initializer.
void Marker(const char* text) {
    HANDLE file = CreateFileA("avz-hosted-script-trace.txt", FILE_APPEND_DATA, FILE_SHARE_READ,
        nullptr, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) return;
    DWORD written = 0;
    WriteFile(file, text, static_cast<DWORD>(std::strlen(text)), &written, nullptr);
    WriteFile(file, "\n", 1, &written, nullptr);
    CloseHandle(file);
}

// AvZ installs its own unhandled-exception filter (avz_seh.cpp) that opens a
// modal "Error!" window; a headless test must never wait for a user, and a real
// fault has to fail the run with its address.
LONG WINAPI FaultHandler(EXCEPTION_POINTERS* info) {
    const auto* record = info->ExceptionRecord;
    const bool accessViolation = record->ExceptionCode == EXCEPTION_ACCESS_VIOLATION;
    const char* kind = !accessViolation ? "n/a"
        : record->ExceptionInformation[0] == 1 ? "write"
        : record->ExceptionInformation[0] == 8 ? "execute" : "read";
    char text[256];
    std::snprintf(text, sizeof(text), "unhandled exception code=0x%lx eip=%p access=%s address=%p",
        record->ExceptionCode, record->ExceptionAddress, kind,
        accessViolation ? reinterpret_cast<void*>(record->ExceptionInformation[1]) : nullptr);
    Marker(text);
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

// AvZ dereferences the App pointer at a fixed address. In this binary the link
// layout (default image base, no ASLR) puts that cell inside the image itself,
// so the page only has to be made writable - and it has to be this module's own
// page, otherwise the test would be scribbling on someone else's memory.
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

void InstallFakeImage() noexcept {
    if (!ClaimAppCell()) {
        imageError = "AvZ's App cell 0x6a9ec0 is not this module's own image page";
        return;
    }
    try {
        app = Allocate(kPageSize);
        board = Allocate(0x8000);
        // Zeroed engine objects: every list AvZ walks reports count 0, the
        // mouse window answers "not in window" and the D3D chain is absent.
        Put<uint32_t>(app, kAppMouseWindow, reinterpret_cast<uint32_t>(Allocate(kPageSize)));
        Put<uint32_t>(board, kBoardPlantArray, reinterpret_cast<uint32_t>(Allocate(kPageSize)));
        Put<uint32_t>(board, kBoardItemArray, reinterpret_cast<uint32_t>(Allocate(kPageSize)));
        Put<uint32_t>(board, kBoardMouseAttribution, reinterpret_cast<uint32_t>(Allocate(kPageSize)));
        Put<uint32_t>(board, kBoardSeedArray, reinterpret_cast<uint32_t>(Allocate(kPageSize)));
        Put<uint32_t>(board, kBoardZombieArray, reinterpret_cast<uint32_t>(Allocate(kPageSize)));
    } catch (const std::exception& error) {
        imageError = error.what();
        return;
    }
    // The game bytes __AGameControllor saves at static-init time: readable
    // inside this image, never written because advanced pause is unused here.
    (void)kGameUpdateAsm;

    *reinterpret_cast<uint8_t**>(kAppPointerCell) = app;
    Put<uint32_t>(app, kAppBoard, reinterpret_cast<uint32_t>(board));
    Put<int>(app, kAppLevelId, 13);  // survival endless, not the zen garden
    Put<int>(app, kAppGameUi, 3);    // fight screen: RunScript processes frames
    Put<uint32_t>(app, kAppD3d, 0);  // no 3D acceleration: painter stays idle

    Put<int>(board, kBoardTotalWave, 20);
    Put<int>(board, kBoardGameClock, 0);
    Put<int>(board, kBoardGlobalClock, 0);
    Put<int>(board, kBoardWave, 0);  // wave 1 has not refreshed yet
    Put<int>(board, kBoardRefreshCountdown, 601);
    Put<int>(board, kBoardInitialCountdown, 601);
    Put<int>(board, kBoardLevelEndCountdown, 0);
}

// Runs before every default-priority dynamic initializer: AvZ's own globals
// read the App pointer and save game bytes while this binary is starting up.
struct FakeImageInstaller {
    FakeImageInstaller() {
        InstallFaultHandler();
        InstallFakeImage();
        Marker(imageError.empty() ? "fake pvz image installed" : imageError.c_str());
    }
};
__attribute__((init_priority(101))) FakeImageInstaller fakeImageInstaller;

int Clock() { return Get<int>(board, kBoardGameClock); }

// One granted frame is exactly the production ScriptHook frame.
void Frame() {
    Check(*reinterpret_cast<void**>(kAppPointerCell) == app,
        "the fake App pointer was overwritten between frames");
    __aScriptManager.ScriptHook();
}

int PendingWaits() {
    if (__aOpQueueManager.queues.size() <= 1) return 0;
    return static_cast<int>(__aOpQueueManager.queues[1].queue.size());
}

// Runs whole frames until the fight clock reads `targetClock`, without running
// the frame that starts at that clock: a time connection fires while
// RunScript() processes the frame that begins at its target.
void RunFramesUntil(int targetClock) {
    for (int guard = 0; Clock() < targetClock; ++guard) {
        if (guard > 1000) throw std::runtime_error("the fake clock stopped advancing");
        Frame();
    }
}

} // namespace

// --- test double: the runtime frame gate --------------------------------
namespace lvz::runtime {
bool Started() { return true; }

bool BeforeFrame() {
    if (!grantFrame) return false;
    ++grantedFrames;
    return true;
}

bool AfterAvzRunTotal() { return true; }

bool RunOneEngineFrame() {
    ++engineCalls;
    // The original App update is the only thing that moves the fight clock; the
    // wave-1 refresh countdown follows it down.
    Put<int>(board, kBoardGameClock, Clock() + 1);
    Put<int>(board, kBoardRefreshCountdown, Get<int>(board, kBoardRefreshCountdown) - 1);
    return true;
}

// avz_smart_overlay.cpp records environment collects for the resident runtime;
// this frame path never reaches it.
void RecordEnvironmentCollect(uint32_t, int, int, int) {}
} // namespace lvz::runtime

// avz_hook_overlay.cpp checks the target image before installing the update
// hook in DllMain; this binary installs no hook and never calls DllMain.
namespace lvz::determinism {
bool ValidateTargetImage() noexcept { return true; }
} // namespace lvz::determinism

// The hosted entry. In the game this is called by LoadScript() (which is where
// the runtime's overlay replaces AvZ's recursive lifetime loop with RunScript);
// the body below is the whole difference the hosted build adds.
void AScript() {
    lvz::hosted::Launch();
}

int main() {
    InstallFaultHandler(); // AvZ's ASeh global installed its own filter earlier
    try {
        Check(imageError.empty(), "fake PvZ image could not be installed: " + imageError);
        const std::filesystem::path log = "avz-hosted-script-test.log";
        std::filesystem::remove(log);
        lvz::runtime::SetDiagnosticsPath(log);

        // A closed gate stops the frame before RunTotal, so nothing at all can
        // happen - not even an engine call.
        grantFrame = false;
        Frame();
        Check(lvz::hosted::probe.started == 0, "a closed gate still launched the script");
        Check(Clock() == 0 && engineCalls == 0, "a closed gate still advanced the engine");
        Check(PendingWaits() == 0, "a closed gate registered a time connection");

        // The state the first load frame leaves behind: global init ran, the
        // script is loaded, and the per-wave queues that _BeforeScript builds
        // exist (all AvZ's own public state; only the hook set that would build
        // them is skipped). GlobalInit() is RunTotal's first step in production,
        // and it is what installs the logger AScript() will log through.
        __aScriptManager.GlobalInit();
        Marker("global init done");
        __aScriptManager.isLoaded = true;
        __aOpQueueManager.isInitialized = true;
        __aOpQueueManager.totalWave = AGetMainObject()->TotalWave();
        __aOpQueueManager.queues.assign(__aOpQueueManager.totalWave + 2, {});
        Marker("queues built");
        Check(!__aScriptManager.willBeExit, "the script manager is not in its normal state");

        // AScript() launches the hosted coroutine; it runs to its first ATime.
        AScript();
        Marker("AScript launched");
        Check(lvz::hosted::probe.started == 1, "AScript() did not launch the hosted coroutine");
        Check(lvz::hosted::probe.resumes == 0, "the first wait did not suspend");
        Check(PendingWaits() == 1, "the suspended coroutine has no pending time connection");

        // First granted frame: RunTotal -> _EnterFight hooks -> RunScript, which
        // records the wave-1 refresh time and then arms and checks the queue.
        grantFrame = true;
        Frame();
        Check(Clock() == 1 && engineCalls == 1, "the first frame did not run one engine call");
        Check(lvz::hosted::probe.resumes == 0, "the coroutine resumed before its target time");

        // Waits are ATime(1, -599) / -549 / -499; with the wave-1 refresh at
        // clock 601 they land on clocks 2, 52 and 102.
        Frame();
        Check(Clock() == 2 && lvz::hosted::probe.resumes == 0, "wait resumed before its target time");
        Frame();
        Check(lvz::hosted::probe.resumes == 1, "the -599 wait never resumed");
        Check(lvz::hosted::probe.resumeWave == 1 && lvz::hosted::probe.resumeTime == -599,
            "the -599 wait resumed at the wrong AvZ time");
        Check(Clock() == 3, "engine calls and granted frames disagree");
        Check(PendingWaits() == 1, "the coroutine did not re-arm after its first resume");

        // Paused stretch: the gate is the only thing that can move the script.
        grantFrame = false;
        for (int i = 0; i < 10; ++i) Frame();
        Check(lvz::hosted::probe.resumes == 1, "a paused stretch resumed the coroutine");
        Check(Clock() == 3 && engineCalls == 3, "a paused stretch advanced the engine");

        grantFrame = true;
        RunFramesUntil(52);
        Check(lvz::hosted::probe.resumes == 1, "the -549 wait fired before its clock");
        Frame();
        Check(lvz::hosted::probe.resumes == 2, "the -549 wait never resumed");
        Check(lvz::hosted::probe.resumeTime == -549, "the -549 wait resumed at the wrong AvZ time");

        RunFramesUntil(102);
        Check(lvz::hosted::probe.resumes == 2, "the -499 wait fired before its clock");
        Frame();
        Check(lvz::hosted::probe.resumes == 3, "the -499 wait never resumed");
        Check(lvz::hosted::probe.resumeTime == -499, "the -499 wait resumed at the wrong AvZ time");
        Check(lvz::hosted::probe.finished == 1, "the hosted coroutine never finished");
        Check(lvz::hosted::probe.clockAtFinish == 102, "the coroutine finished at the wrong clock");
        Check(PendingWaits() == 0, "a finished script left a time connection behind");

        // A finished script must stay finished across further granted frames.
        for (int i = 0; i < 5; ++i) Frame();
        Check(lvz::hosted::probe.resumes == 3 && lvz::hosted::probe.finished == 1,
            "a finished script ran again");
        Check(engineCalls == grantedFrames, "engine calls did not match granted frames");

        char summary[200];
        std::snprintf(summary, sizeof(summary),
            "hosted script advanced on %d granted frames; waits -599/-549/-499 observed; "
            "paused frames advanced nothing", grantedFrames);
        Marker(summary);
        std::printf("%s\n", summary);
        return 0;
    } catch (const std::exception& error) {
        std::fprintf(stderr, "avz hosted script test failed: %s\n", error.what());
        return 1;
    }
}
