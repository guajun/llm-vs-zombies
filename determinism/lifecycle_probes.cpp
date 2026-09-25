#include "lifecycle_probes.hpp"
#include "measurement.hpp"
#include "spawn_hook.hpp"
#include <Windows.h>
#include <array>
#include <atomic>
#include <cstring>
#include <stdexcept>

// Production probe design notes (review PR #116):
//  * shims replay the displaced instruction unconditionally; handlers only
//    observe,
//  * the observer survives unreadable addresses through a VEH-protected,
//    strictly non-allocating reader,
//  * the recycle candidate keeps the pre-free identity/board/slot and the
//    commit validates the observed free-list transition from that snapshot,
//  * restoration is ownership-checked per site and never reports success while
//    a jump to this module is still reachable.

namespace lvz::determinism {
namespace {
using Json = nlohmann::json;
constexpr size_t kDefaultQueueCapacity = 4096;
constexpr size_t kSiteCount = 8;
constexpr uintptr_t kZombieStride = 0x15c;

// ---------------------------------------------------------------------------
// Non-allocating fault-protected reads. No Win32 call, no exception, no
// allocation: a fault inside one of these stubs is redirected by the vectored
// handler to the adjacent recovery label.
// ---------------------------------------------------------------------------
extern "C" uint32_t lvzProbeRead8(const void*, uint32_t*);
extern "C" uint32_t lvzProbeRead16(const void*, uint32_t*);
extern "C" uint32_t lvzProbeRead32(const void*, uint32_t*);
extern "C" void lvzProbeRead8Recovery();
extern "C" void lvzProbeRead16Recovery();
extern "C" void lvzProbeRead32Recovery();

__asm__(
    ".text\n"
    ".p2align 4\n"
    ".globl _lvzProbeRead8\n"
    "_lvzProbeRead8:\n"
    "  movl 4(%esp), %ecx\n"
    "  movl 8(%esp), %edx\n"
    "  movl $0, (%edx)\n"
    "  movzbl (%ecx), %eax\n"
    "  movl %eax, (%edx)\n"
    "  movl $1, %eax\n"
    "  retl\n"
    ".globl _lvzProbeRead8Recovery\n"
    "_lvzProbeRead8Recovery:\n"
    "  xorl %eax, %eax\n"
    "  retl\n"
    ".p2align 4\n"
    ".globl _lvzProbeRead16\n"
    "_lvzProbeRead16:\n"
    "  movl 4(%esp), %ecx\n"
    "  movl 8(%esp), %edx\n"
    "  movl $0, (%edx)\n"
    "  movzwl (%ecx), %eax\n"
    "  movl %eax, (%edx)\n"
    "  movl $1, %eax\n"
    "  retl\n"
    ".globl _lvzProbeRead16Recovery\n"
    "_lvzProbeRead16Recovery:\n"
    "  xorl %eax, %eax\n"
    "  retl\n"
    ".p2align 4\n"
    ".globl _lvzProbeRead32\n"
    "_lvzProbeRead32:\n"
    "  movl 4(%esp), %ecx\n"
    "  movl 8(%esp), %edx\n"
    "  movl $0, (%edx)\n"
    "  movl (%ecx), %eax\n"
    "  movl %eax, (%edx)\n"
    "  movl $1, %eax\n"
    "  retl\n"
    ".globl _lvzProbeRead32Recovery\n"
    "_lvzProbeRead32Recovery:\n"
    "  xorl %eax, %eax\n"
    "  retl\n");

struct ReadRange {
    uintptr_t begin;
    uintptr_t end;
};
const std::array<ReadRange, 3> kReadRanges = {{
    {reinterpret_cast<uintptr_t>(&lvzProbeRead8), reinterpret_cast<uintptr_t>(&lvzProbeRead8Recovery)},
    {reinterpret_cast<uintptr_t>(&lvzProbeRead16), reinterpret_cast<uintptr_t>(&lvzProbeRead16Recovery)},
    {reinterpret_cast<uintptr_t>(&lvzProbeRead32), reinterpret_cast<uintptr_t>(&lvzProbeRead32Recovery)},
}};

LONG CALLBACK ProbeVectoredHandler(PEXCEPTION_POINTERS info) {
    if (info->ExceptionRecord->ExceptionCode == EXCEPTION_ACCESS_VIOLATION
        && info->ExceptionRecord->NumberParameters >= 1
        && info->ExceptionRecord->ExceptionInformation[0] == 0) {  // read fault
        const uintptr_t fault = static_cast<uintptr_t>(info->ContextRecord->Eip);
        for (const auto& range : kReadRanges) {
            if (fault >= range.begin && fault < range.end) {
                info->ContextRecord->Eip = static_cast<DWORD>(range.end);
                return EXCEPTION_CONTINUE_EXECUTION;
            }
        }
    }
    return EXCEPTION_CONTINUE_SEARCH;
}

void* readerHandler = nullptr;
std::atomic<uint64_t> readerHandlersAdded{0}, readerHandlersRemoved{0};
std::atomic<uint32_t> inFlightCallbacks{0};
std::atomic<uint32_t> inFlightForTest{0};

bool ProtectReaderInstalled() noexcept {
    if (readerHandler) return true;
    void* handler = AddVectoredExceptionHandler(1, ProbeVectoredHandler);
    if (!handler) return false;
    readerHandler = handler;
    readerHandlersAdded.fetch_add(1);
    return true;
}

bool ReaderProtectionInstalled() noexcept { return readerHandler != nullptr; }

uint32_t InFlightCount() noexcept { return inFlightCallbacks.load() + inFlightForTest.load(); }

bool ReleaseReaderProtection() noexcept {
    if (!readerHandler) return true;
    if (LifecycleProbesInstalled()) return false;
    if (InFlightCount() != 0) return false;
    if (!RemoveVectoredExceptionHandler(readerHandler)) return false;
    readerHandler = nullptr;
    readerHandlersRemoved.fetch_add(1);
    return true;
}

struct InFlightGuard {
    InFlightGuard() noexcept { inFlightCallbacks.fetch_add(1); }
    ~InFlightGuard() noexcept { inFlightCallbacks.fetch_sub(1); }
    InFlightGuard(const InFlightGuard&) = delete;
    InFlightGuard& operator=(const InFlightGuard&) = delete;
};

inline bool SafeRead8(uintptr_t address, uint8_t& out) noexcept {
    uint32_t value = 0;
    if (!lvzProbeRead8(reinterpret_cast<const void*>(address), &value)) return false;
    out = static_cast<uint8_t>(value);
    return true;
}
inline bool SafeRead16(uintptr_t address, uint16_t& out) noexcept {
    uint32_t value = 0;
    if (!lvzProbeRead16(reinterpret_cast<const void*>(address), &value)) return false;
    out = static_cast<uint16_t>(value);
    return true;
}
inline bool SafeRead32(uintptr_t address, uint32_t& out) noexcept {
    return lvzProbeRead32(reinterpret_cast<const void*>(address), &out) != 0;
}
inline bool SafeReadI32(uintptr_t address, int32_t& out) noexcept {
    return SafeRead32(address, reinterpret_cast<uint32_t&>(out));
}

// ---------------------------------------------------------------------------
// Locked site table (mirrors docs/issue111-patch-windows.json).
// ---------------------------------------------------------------------------
struct Site {
    const char* id;
    uintptr_t va;
    uint8_t window;
    uint8_t bytes[7];
    uint8_t kind;  // 0 phase EDI, 1 phase ESI, 2 zamboni, 3 mdead, 4 guard, 5 commit
    uintptr_t continuation;
    uintptr_t jumpTarget;
};

const Site kSites[kSiteCount] = {
    {"phase-playdeathanim", 0x533377, 7, {0xc7,0x47,0x28,0x01,0x00,0x00,0x00}, 0, 0x53337e, 0},
    {"phase-applyburn", 0x532f5f, 7, {0xc7,0x46,0x28,0x02,0x00,0x00,0x00}, 1, 0x532f66, 0},
    {"phase-mowdown", 0x532a62, 7, {0xc7,0x47,0x28,0x03,0x00,0x00,0x00}, 0, 0x532a69, 0},
    {"phase-catapult", 0x52ec92, 7, {0xc7,0x47,0x28,0x01,0x00,0x00,0x00}, 0, 0x52ec99, 0},
    {"phase-zamboni", 0x52ea38, 6, {0x89,0x5f,0x28,0xd9,0x47,0x2c,0x00}, 2, 0x52ea3e, 0},
    {"removal-mdead", 0x530602, 7, {0xc6,0x87,0xec,0x00,0x00,0x00,0x01}, 3, 0x530609, 0},
    {"recycle-guard", 0x41bba9, 6, {0x38,0x9f,0xec,0x00,0x00,0x00,0x00}, 4, 0x41bbaf, 0},
    {"recycle-commit", 0x41bc23, 5, {0xe9,0x30,0xff,0xff,0xff,0x00,0x00}, 5, 0x41bb58, 0x41bb58},
};

struct ObjectInfo {
    uint32_t entity = 0;
    uint32_t slot = 0;
    uint32_t board = 0;
    int32_t wave = 0;
    bool onBoard = false;
};

// Real pool geometry plus the engine's own allocation marker. The engine's
// delete loop skips a slot whose high 16 bits are zero (free list link), so a
// generation of 0 is not a live object. A pool pointer alone never suffices.
bool Classify(uint32_t zombie, ObjectInfo& info) noexcept {
    if (!zombie) return false;
    uint32_t entity = 0;
    if (!SafeRead32(zombie + 0x158, entity)) return false;
    if (!entity) return false;
    if ((entity & 0xffff0000u) == 0) return false;
    uint32_t board = 0;
    if (!SafeRead32(zombie + 4, board)) return false;
    if (!board) return false;
    uint32_t block = 0, used = 0, capacity = 0;
    if (!SafeRead32(board + 0x90, block)) return false;
    if (!SafeRead32(board + 0x94, used)) return false;
    if (!SafeRead32(board + 0x98, capacity)) return false;
    if (!block || !used || used > capacity || capacity > 0x1000) return false;
    const uint32_t slot = entity & 0xffffu;
    if (slot >= used) return false;
    if (block + static_cast<uintptr_t>(slot) * kZombieStride != zombie) return false;
    int32_t wave = 0;
    if (!SafeReadI32(zombie + 0x6c, wave)) return false;
    info.entity = entity;
    info.slot = slot;
    info.board = board;
    info.wave = wave;
    info.onBoard = wave != -2 && wave != -3;
    return true;
}

// ---------------------------------------------------------------------------
// Bounded capture state. Published only after the original store executed.
// ---------------------------------------------------------------------------
struct Record {
    uint8_t kind = 0;
    uint8_t site = 0;
    uint8_t onBoard = 0;
    uint8_t boundaryValid = 0;
    uint32_t entity = 0;
    uint32_t slot = 0;
    uint32_t board = 0;
    uint32_t wave = 0;
    int32_t before = 0;
    int32_t after = 0;
    uint32_t freeHead = 0;
    uint32_t count = 0;
    uint32_t freeHeadAfter = 0;
    uint32_t countAfter = 0;
    uint64_t sequence = 0;
    uint64_t candidateSequence = 0;
    uint64_t tick = 0;
    uint64_t revision = 0;
    uint64_t engineCallId = 0;
    uint32_t segment = 0;
};

std::array<Record, kDefaultQueueCapacity> queue;
size_t queued = 0;
std::atomic<uint64_t> counterCaptured{0}, counterDelivered{0}, counterOverflow{0}, counterWrongThread{0},
    counterInactive{0}, counterClassifyRefused{0}, counterReadFailed{0}, counterLiveSkips{0},
    counterUnmatchedCommits{0}, counterPairMismatch{0}, counterOverwrittenPending{0}, counterFaults{0};
std::atomic<uint32_t> queueCapacityOverride{0};
std::atomic<bool> captureActive{false};
std::atomic<uint32_t> virtualProtectFailureForTest{0};
bool everInstalled = false;

struct TlsCapture {
    bool valid = false;
    uint32_t site = 0;
    uint32_t zombie = 0;
    uint32_t target = 0;
    int32_t before = 0;
    ObjectInfo info{};
    CaptureBoundary boundary{};
};
thread_local TlsCapture tlsCapture;

struct RecyclePending {
    bool valid = false;
    uint32_t zombie = 0;
    uint32_t board = 0;
    uint32_t entity = 0;
    uint32_t slot = 0;
    uint32_t wave = 0;
    uint8_t onBoard = 0;
    uint32_t freeHead = 0;
    uint32_t count = 0;
    uint64_t sequence = 0;
};
RecyclePending recyclePending;
DWORD gameThread = 0;
DWORD owningThread = 0;

size_t QueueCapacity() noexcept {
    const uint32_t override = queueCapacityOverride.load();
    return override ? override : kDefaultQueueCapacity;
}

// Handlers are noexcept and preserve the thread error state across every call.
struct LastErrorGuard {
    DWORD value;
    LastErrorGuard() noexcept : value(GetLastError()) {}
    ~LastErrorGuard() noexcept { SetLastError(value); }
    LastErrorGuard(const LastErrorGuard&) = delete;
    LastErrorGuard& operator=(const LastErrorGuard&) = delete;
};

uint64_t NextSequence() noexcept {
    const uint64_t sequence = measurement::Host().NextSequence();
    if (!sequence) {
        counterFaults.fetch_add(1);
        measurement::Host().OnIncomplete();
    }
    return sequence;
}

void Push(const Record& record) noexcept {
    if (queued >= QueueCapacity()) {
        counterOverflow.fetch_add(1);
        measurement::Host().OnOverflow();
        return;
    }
    queue[queued++] = record;
    counterCaptured.fetch_add(1);
    measurement::Host().OnCaptured();
}

void Fill(Record& record, uint8_t kind, uint8_t site, const ObjectInfo& info) noexcept {
    record.kind = kind;
    record.site = site;
    record.entity = info.entity;
    record.slot = info.slot;
    record.board = info.board;
    record.wave = static_cast<uint32_t>(info.wave);
    record.onBoard = info.onBoard ? 1 : 0;
    const CaptureBoundary boundary = CurrentCaptureBoundary();
    record.boundaryValid = boundary.valid ? 1 : 0;
    record.tick = boundary.tick;
    record.revision = boundary.revision;
    record.segment = boundary.segment;
    record.engineCallId = boundary.engineCallId;
    record.sequence = 0;
    record.candidateSequence = 0;
    record.before = 0;
    record.after = 0;
    record.freeHead = 0;
    record.count = 0;
    record.freeHeadAfter = 0;
    record.countAfter = 0;
}

bool AcceptsObservation() noexcept {
    if (!captureActive.load()) {
        counterInactive.fetch_add(1);
        return false;
    }
    if (GetCurrentThreadId() != owningThread) {
        counterWrongThread.fetch_add(1);
        measurement::Host().OnWrongThread();
        return false;
    }
    return true;
}

}  // namespace

// ---------------------------------------------------------------------------
// Naked shims: copy before -> execute displaced instruction -> observe after.
// pushal/pushfl/fxsave frame preserves every GPR, flags, x87, MXCSR and SSE
// register; the TEB LastError value is saved next to the frame.
// ---------------------------------------------------------------------------
extern "C" {
uint32_t lvzContinuation0 = static_cast<uint32_t>(0x53337e);
uint32_t lvzContinuation1 = static_cast<uint32_t>(0x532f66);
uint32_t lvzContinuation2 = static_cast<uint32_t>(0x532a69);
uint32_t lvzContinuation3 = static_cast<uint32_t>(0x52ec99);
uint32_t lvzContinuation4 = static_cast<uint32_t>(0x52ea3e);
uint32_t lvzContinuation5 = static_cast<uint32_t>(0x530609);
uint32_t lvzContinuation6 = static_cast<uint32_t>(0x41bbaf);
uint32_t lvzCommitTarget = static_cast<uint32_t>(0x41bb58);

void __cdecl LvzProbeBeforePhase(uint32_t site, uint32_t zombie, uint32_t target) noexcept;
void __cdecl LvzProbeAfterPhase(uint32_t site, uint32_t zombie, uint32_t target) noexcept;
void __cdecl LvzProbeRecycleGuard(uint32_t zombie, uint32_t deadBranch) noexcept;
void __cdecl LvzProbeRecycleCommit(uint32_t zombie, uint32_t board) noexcept;

#define LVZ_PROBE_PROLOGUE                                                                       \
    "pushfl\n\tpushal\n\t"                                                                       \
    "movl %esp, %eax\n\tandl $-16, %esp\n\tsubl $544, %esp\n\t"                                  \
    "movl %esp, %ebp\n\tmovl %eax, 528(%ebp)\n\t"                                                \
    "fxsave (%ebp)\n\t"                                                                          \
    "movl %fs:0x34, %eax\n\tmovl %eax, 532(%ebp)\n\tcld\n\t"

#define LVZ_PROBE_RELOAD_OBSERVED_REGS                                                           \
    "movl 528(%ebp), %eax\n\t"                                                                   \
    "movl 0(%eax), %edi\n\tmovl 4(%eax), %esi\n\tmovl 16(%eax), %ebx\n\t"

#define LVZ_PROBE_EPILOGUE(CONT)                                                                 \
    "fxrstor (%ebp)\n\t"                                                                         \
    "movl 532(%ebp), %eax\n\tmovl %eax, %fs:0x34\n\t"                                            \
    "movl 528(%ebp), %esp\n\tpopal\n\tpopfl\n\tjmp *" #CONT "\n\t"

#define LVZ_PHASE_SHIM(NAME, CONT, SITE, REC, IMM)                                               \
    __attribute__((naked)) void NAME() {                                                         \
        __asm__ volatile(                                                                        \
            LVZ_PROBE_PROLOGUE                                                                   \
            "leal 0x28(%" REC "), %edx\n\tpushl %edx\n\tpushl %" REC "\n\tpushl $" #SITE "\n\t"   \
            "call _LvzProbeBeforePhase\n\taddl $12, %esp\n\tfxrstor (%ebp)\n\t"                   \
            LVZ_PROBE_RELOAD_OBSERVED_REGS                                                       \
            "movl $" #IMM ", 0x28(%" REC ")\n\t"                                                 \
            "leal 0x28(%" REC "), %edx\n\tpushl %edx\n\tpushl %" REC "\n\tpushl $" #SITE "\n\t"   \
            "call _LvzProbeAfterPhase\n\taddl $12, %esp\n\t" LVZ_PROBE_EPILOGUE(CONT));           \
    }

LVZ_PHASE_SHIM(LvzPhaseShim0, lvzContinuation0, 0, "edi", 1)
LVZ_PHASE_SHIM(LvzPhaseShim1, lvzContinuation1, 1, "esi", 2)
LVZ_PHASE_SHIM(LvzPhaseShim2, lvzContinuation2, 2, "edi", 3)
LVZ_PHASE_SHIM(LvzPhaseShim3, lvzContinuation3, 3, "edi", 1)

// ZamboniDeath: `movl %ebx,0x28(%edi)` plus `flds 0x2c(%edi)`. The flds state
// is captured again after the replay so the after-observer cannot drop it.
__attribute__((naked)) void LvzPhaseShim4() {
    __asm__ volatile(
        LVZ_PROBE_PROLOGUE
        "leal 0x28(%edi), %edx\n\tpushl %edx\n\tpushl %edi\n\tpushl $4\n\t"
        "call _LvzProbeBeforePhase\n\taddl $12, %esp\n\tfxrstor (%ebp)\n\t"
        "movl 528(%ebp), %eax\n\tmovl 0(%eax), %edi\n\tmovl 16(%eax), %ebx\n\t"
        "movl %ebx, 0x28(%edi)\n\tflds 0x2c(%edi)\n\t"
        "fxsave (%ebp)\n\t"
        "leal 0x28(%edi), %edx\n\tpushl %edx\n\tpushl %edi\n\tpushl $4\n\t"
        "call _LvzProbeAfterPhase\n\taddl $12, %esp\n\t" LVZ_PROBE_EPILOGUE(lvzContinuation4));
}

// Removal mDead store (`movb $1,0xec(%edi)`).
__attribute__((naked)) void LvzRemovalShim5() {
    __asm__ volatile(
        LVZ_PROBE_PROLOGUE
        "leal 0xec(%edi), %edx\n\tpushl %edx\n\tpushl %edi\n\tpushl $5\n\t"
        "call _LvzProbeBeforePhase\n\taddl $12, %esp\n\tfxrstor (%ebp)\n\t"
        "movl 528(%ebp), %eax\n\tmovl 0(%eax), %edi\n\t"
        "movb $1, 0xec(%edi)\n\t"
        "leal 0xec(%edi), %edx\n\tpushl %edx\n\tpushl %edi\n\tpushl $5\n\t"
        "call _LvzProbeAfterPhase\n\taddl $12, %esp\n\t" LVZ_PROBE_EPILOGUE(lvzContinuation5));
}

// Recycle guard: the original `cmpb %bl,0xec(%edi)` executes once; the shim
// passes the real branch result (ZF) and continues to the original `je`.
__attribute__((naked)) void LvzGuardShim6() {
    __asm__ volatile(
        "cmpb %bl, 0xec(%edi)\n\t"
        LVZ_PROBE_PROLOGUE
        // Derive the real branch result from the flags word saved immediately
        // after the compare (the prologue itself clobbers EFLAGS).
        "movl 528(%ebp), %eax\n\t"
        "movl 32(%eax), %eax\n\t"
        "shrl $6, %eax\n\t"
        "andl $1, %eax\n\t"
        "xorl $1, %eax\n\t"
        "pushl %eax\n\tpushl %edi\n\t"
        "call _LvzProbeRecycleGuard\n\taddl $8, %esp\n\t"
        LVZ_PROBE_EPILOGUE(lvzContinuation6));
}

// Recycle commit: the free-list update already ran in the original code; the
// observer validates it from the pre-free candidate snapshot, then the
// original jump target is taken.
__attribute__((naked)) void LvzCommitShim7() {
    __asm__ volatile(
        LVZ_PROBE_PROLOGUE
        "pushl %esi\n\tpushl %edi\n\t"
        "call _LvzProbeRecycleCommit\n\taddl $8, %esp\n\t"
        LVZ_PROBE_EPILOGUE(lvzCommitTarget));
}
#undef LVZ_PHASE_SHIM
#undef LVZ_PROBE_EPILOGUE
#undef LVZ_PROBE_RELOAD_OBSERVED_REGS
#undef LVZ_PROBE_PROLOGUE
}  // extern "C"

namespace {
const uintptr_t kShims[kSiteCount] = {
    reinterpret_cast<uintptr_t>(&LvzPhaseShim0), reinterpret_cast<uintptr_t>(&LvzPhaseShim1),
    reinterpret_cast<uintptr_t>(&LvzPhaseShim2), reinterpret_cast<uintptr_t>(&LvzPhaseShim3),
    reinterpret_cast<uintptr_t>(&LvzPhaseShim4), reinterpret_cast<uintptr_t>(&LvzRemovalShim5),
    reinterpret_cast<uintptr_t>(&LvzGuardShim6), reinterpret_cast<uintptr_t>(&LvzCommitShim7),
};

enum SiteState : uint8_t { kSiteClear = 0, kSiteOwned = 1 };
std::array<uint8_t, kSiteCount> siteState{};
std::array<std::array<uint8_t, 7>, kSiteCount> originalBytes{};
std::array<std::array<uint8_t, 7>, kSiteCount> patchedBytes{};
std::array<uintptr_t, kSiteCount> target{};
std::array<uintptr_t, kSiteCount> continuation{};
std::array<uintptr_t, kSiteCount> jumpTarget{};

void ResetState() {
    queued = 0;
    recyclePending = RecyclePending{};
    counterCaptured = counterDelivered = counterOverflow = counterWrongThread = counterInactive = 0;
    counterClassifyRefused = counterReadFailed = counterLiveSkips = counterUnmatchedCommits = 0;
    counterPairMismatch = counterOverwrittenPending = counterFaults = 0;
    queueCapacityOverride.store(0);
    virtualProtectFailureForTest.store(0);
    inFlightForTest.store(0);
    everInstalled = false;
}

void PrepareTable() {
    for (size_t index = 0; index < kSiteCount; ++index) {
        target[index] = kSites[index].va;
        continuation[index] = kSites[index].continuation;
        jumpTarget[index] = kSites[index].jumpTarget;
    }
}

void SyncContinuationGlobals() {
    lvzContinuation0 = static_cast<uint32_t>(continuation[0]);
    lvzContinuation1 = static_cast<uint32_t>(continuation[1]);
    lvzContinuation2 = static_cast<uint32_t>(continuation[2]);
    lvzContinuation3 = static_cast<uint32_t>(continuation[3]);
    lvzContinuation4 = static_cast<uint32_t>(continuation[4]);
    lvzContinuation5 = static_cast<uint32_t>(continuation[5]);
    lvzContinuation6 = static_cast<uint32_t>(continuation[6]);
    lvzCommitTarget = static_cast<uint32_t>(jumpTarget[7]);
}

bool BytesMatch(uintptr_t address, const uint8_t* bytes, size_t count) noexcept {
    if (!address) return false;
    uint8_t buffer[8];
    if (count > sizeof(buffer)) return false;
    for (size_t index = 0; index < count; ++index) {
        if (!SafeRead8(address + index, buffer[index])) return false;
    }
    return std::memcmp(buffer, bytes, count) == 0;
}

bool VirtualProtectWithInjection(void* address, size_t size, DWORD protection, DWORD& previous) noexcept {
    if (virtualProtectFailureForTest.load()) return false;
    return VirtualProtect(address, size, protection, &previous) != 0;
}

bool ApplyPatch(size_t index, std::string& error) {
    if (siteState[index] != kSiteClear) {
        error = std::string("probe site already owned: ") + kSites[index].id;
        return false;
    }
    const uintptr_t address = target[index];
    if (!BytesMatch(address, kSites[index].bytes, kSites[index].window)) {
        error = std::string("probe site signature mismatch: ") + kSites[index].id;
        return false;
    }
    std::array<uint8_t, 7> replacement{};
    replacement[0] = 0xe9;
    const intptr_t relative = static_cast<intptr_t>(kShims[index]) - static_cast<intptr_t>(address + 5);
    if (relative < INT32_MIN || relative > INT32_MAX) {
        error = std::string("probe site jump is out of range: ") + kSites[index].id;
        return false;
    }
    const int32_t rel32 = static_cast<int32_t>(relative);
    std::memcpy(replacement.data() + 1, &rel32, 4);
    for (size_t i = 5; i < kSites[index].window; ++i) replacement[i] = 0x90;
    std::memcpy(originalBytes[index].data(), kSites[index].bytes, kSites[index].window);
    std::memcpy(patchedBytes[index].data(), replacement.data(), kSites[index].window);
    DWORD previous = 0;
    if (!VirtualProtectWithInjection(reinterpret_cast<void*>(address), kSites[index].window,
                                     PAGE_EXECUTE_READWRITE, previous)) {
        error = std::string("VirtualProtect failed for ") + kSites[index].id;
        return false;
    }
    std::memcpy(reinterpret_cast<void*>(address), replacement.data(), kSites[index].window);
    siteState[index] = kSiteOwned;
    DWORD ignored = 0;
    const bool protectionRestored = VirtualProtectWithInjection(reinterpret_cast<void*>(address), kSites[index].window,
                                                       previous, ignored);
    FlushInstructionCache(GetCurrentProcess(), reinterpret_cast<void*>(address), kSites[index].window);
    if (!protectionRestored) {
        counterFaults.fetch_add(1);
        error = std::string("VirtualProtect restore failed for ") + kSites[index].id;
        return false;
    }
    return true;
}

// Restores one site. Returns false and keeps ownership when the patch is no
// longer ours or protection/restore fails; never overwrites another writer.
bool RestorePatch(size_t index, std::string& error) {
    if (siteState[index] != kSiteOwned) return true;
    const uintptr_t address = target[index];
    if (!BytesMatch(address, patchedBytes[index].data(), kSites[index].window)) {
        error = std::string("probe patch ownership changed; refusing to overwrite: ") + kSites[index].id;
        return false;
    }
    DWORD previous = 0;
    if (!VirtualProtectWithInjection(reinterpret_cast<void*>(address), kSites[index].window,
                                     PAGE_EXECUTE_READWRITE, previous)) {
        counterFaults.fetch_add(1);
        error = std::string("VirtualProtect failed while restoring ") + kSites[index].id;
        return false;
    }
    std::memcpy(reinterpret_cast<void*>(address), originalBytes[index].data(), kSites[index].window);
    siteState[index] = kSiteClear;
    DWORD ignored = 0;
    const bool protectionRestored = VirtualProtectWithInjection(reinterpret_cast<void*>(address), kSites[index].window,
                                                       previous, ignored);
    FlushInstructionCache(GetCurrentProcess(), reinterpret_cast<void*>(address), kSites[index].window);
    if (!protectionRestored) {
        counterFaults.fetch_add(1);
        error = std::string("VirtualProtect restore failed while restoring ") + kSites[index].id;
        return false;
    }
    return true;
}

std::string RestoreFailure(const std::string& first) {
    std::string message = "lifecycle probe removal failed; keep runtime DLL loaded: " + first;
    return message;
}
}  // namespace

extern "C" void __cdecl LvzProbeBeforePhase(uint32_t site, uint32_t zombie, uint32_t address) noexcept {
    LastErrorGuard guard;
    InFlightGuard flight;
    if (!AcceptsObservation()) {
        tlsCapture = TlsCapture{};
        return;
    }
    if (site >= kSiteCount) {
        counterFaults.fetch_add(1);
        tlsCapture = TlsCapture{};
        return;
    }
    TlsCapture capture;
    if (kSites[site].kind == 3) {
        uint8_t before = 0;
        if (!SafeRead8(address, before)) {
            counterReadFailed.fetch_add(1);
            tlsCapture = TlsCapture{};
            return;
        }
        capture.before = before;
    } else {
        int32_t before = 0;
        if (!SafeReadI32(address, before)) {
            counterReadFailed.fetch_add(1);
            tlsCapture = TlsCapture{};
            return;
        }
        capture.before = before;
    }
    if (!Classify(zombie, capture.info)) {
        counterClassifyRefused.fetch_add(1);
        tlsCapture = TlsCapture{};
        return;
    }
    capture.valid = true;
    capture.site = site;
    capture.zombie = zombie;
    capture.target = address;
    capture.boundary = CurrentCaptureBoundary();
    tlsCapture = capture;
}

extern "C" void __cdecl LvzProbeAfterPhase(uint32_t site, uint32_t zombie, uint32_t address) noexcept {
    LastErrorGuard guard;
    InFlightGuard flight;
    if (!tlsCapture.valid || tlsCapture.site != site || tlsCapture.zombie != zombie
        || tlsCapture.target != address) {
        tlsCapture = TlsCapture{};
        return;
    }
    if (!captureActive.load() || GetCurrentThreadId() != owningThread) {
        tlsCapture = TlsCapture{};
        return;
    }
    Record record;
    Fill(record, kSites[site].kind == 3 ? 2 : 0, static_cast<uint8_t>(site), tlsCapture.info);
    record.before = tlsCapture.before;
    record.boundaryValid = tlsCapture.boundary.valid ? 1 : 0;
    record.tick = tlsCapture.boundary.tick;
    record.revision = tlsCapture.boundary.revision;
    record.segment = tlsCapture.boundary.segment;
    record.engineCallId = tlsCapture.boundary.engineCallId;
    if (kSites[site].kind == 3) {
        uint8_t after = 0;
        if (!SafeRead8(address, after)) {
            counterReadFailed.fetch_add(1);
            tlsCapture = TlsCapture{};
            return;
        }
        record.after = after;
    } else {
        int32_t after = 0;
        if (!SafeReadI32(address, after)) {
            counterReadFailed.fetch_add(1);
            tlsCapture = TlsCapture{};
            return;
        }
        record.after = after;
    }
    const uint64_t sequence = NextSequence();
    if (!sequence) {
        tlsCapture = TlsCapture{};
        return;
    }
    record.sequence = sequence;
    Push(record);
    tlsCapture = TlsCapture{};
}

extern "C" void __cdecl LvzProbeRecycleGuard(uint32_t zombie, uint32_t deadBranch) noexcept {
    LastErrorGuard guard;
    InFlightGuard flight;
    if (!AcceptsObservation()) return;
    if (!deadBranch) {
        counterLiveSkips.fetch_add(1);
        return;
    }
    ObjectInfo info;
    Record candidate;
    if (!Classify(zombie, info)) {
        counterClassifyRefused.fetch_add(1);
        return;
    }
    uint32_t freeHead = 0, count = 0;
    if (!SafeRead32(info.board + 0x9c, freeHead) || !SafeRead32(info.board + 0xa0, count)) {
        counterReadFailed.fetch_add(1);
        return;
    }
    if (recyclePending.valid) {
        counterOverwrittenPending.fetch_add(1);
        counterFaults.fetch_add(1);
        measurement::Host().OnIncomplete();
    }
    const uint64_t sequence = NextSequence();
    if (!sequence) {
        recyclePending = RecyclePending{};
        return;
    }
    Fill(candidate, 3, 6, info);
    candidate.sequence = sequence;
    candidate.freeHead = freeHead;
    candidate.count = count;
    recyclePending = RecyclePending{true, zombie, info.board, info.entity, info.slot,
                                    static_cast<uint32_t>(info.wave), static_cast<uint8_t>(info.onBoard ? 1 : 0),
                                    freeHead, count, sequence};
    Push(candidate);
}

extern "C" void __cdecl LvzProbeRecycleCommit(uint32_t zombie, uint32_t board) noexcept {
    LastErrorGuard guard;
    InFlightGuard flight;
    if (!AcceptsObservation()) return;
    if (!recyclePending.valid || recyclePending.zombie != zombie || recyclePending.board != board) {
        counterUnmatchedCommits.fetch_add(1);
        counterFaults.fetch_add(1);
        measurement::Host().OnIncomplete();
        recyclePending = RecyclePending{};
        return;
    }
    const uint32_t oldHead = recyclePending.freeHead;
    const uint32_t oldCount = recyclePending.count;
    const uint32_t slot = recyclePending.slot;
    uint32_t link = 0, head = 0, count = 0;
    if (!SafeRead32(zombie + 0x158, link) || !SafeRead32(board + 0x9c, head) || !SafeRead32(board + 0xa0, count)) {
        counterReadFailed.fetch_add(1);
        counterFaults.fetch_add(1);
        measurement::Host().OnIncomplete();
        recyclePending = RecyclePending{};
        return;
    }
    if (link != oldHead || head != slot || count + 1 != oldCount) {
        counterPairMismatch.fetch_add(1);
        counterFaults.fetch_add(1);
        measurement::Host().OnIncomplete();
        recyclePending = RecyclePending{};
        return;
    }
    const uint64_t sequence = NextSequence();
    if (!sequence) {
        recyclePending = RecyclePending{};
        return;
    }
    Record record;
    ObjectInfo info;
    info.entity = recyclePending.entity;
    info.slot = recyclePending.slot;
    info.board = recyclePending.board;
    info.wave = static_cast<int32_t>(recyclePending.wave);
    info.onBoard = recyclePending.onBoard != 0;
    Fill(record, 4, 7, info);
    record.sequence = sequence;
    record.candidateSequence = recyclePending.sequence;
    record.freeHeadAfter = head;
    record.countAfter = count;
    Push(record);
    recyclePending = RecyclePending{};
}

bool LifecycleProbesInstalled() noexcept {
    for (const uint8_t state : siteState)
        if (state == kSiteOwned) return true;
    return false;
}

bool InstallLifecycleProbes(std::string& error) {
    if (LifecycleProbesInstalled()) {
        error = "Lifecycle probes are already installed";
        return false;
    }
    if (!measurement::Host().BoundTo(GetCurrentThreadId())) {
        error = "Measurement session is not open on the game thread";
        return false;
    }
    if (!ProtectReaderInstalled()) {
        error = "Cannot install the probe fault-protection handler";
        return false;
    }
    gameThread = GetCurrentThreadId();
    PrepareTable();
    ResetState();
    for (size_t index = 0; index < kSiteCount; ++index) {
        if (!ApplyPatch(index, error)) {
            std::string rollbackError;
            for (size_t applied = index + 1; applied-- > 0;) {
                std::string siteError;
                if (!RestorePatch(applied, siteError) && rollbackError.empty())
                    rollbackError = siteError;
            }
            if (!rollbackError.empty())
                error = RestoreFailure(error + "; " + rollbackError);
            if (!ReleaseReaderProtection() && rollbackError.empty())
                error = RestoreFailure(error + "; reader protection could not be released");
            return false;
        }
    }
    SyncContinuationGlobals();
    gameThread = GetCurrentThreadId();
    owningThread = gameThread;
    captureActive.store(true);
    everInstalled = true;
    error.clear();
    return true;
}

#ifdef LVZ_LIFECYCLE_PROBES_TESTING
void LvzProbeSetThreadForTest(uint32_t thread) noexcept { owningThread = thread; }
void LvzProbeSetActiveForTest(bool active) noexcept { captureActive.store(active); }
void LvzProbeSetVirtualProtectFailureForTest(bool fail) noexcept { virtualProtectFailureForTest.store(fail ? 1 : 0); }
void LvzProbeSetQueueCapacityForTest(uint32_t capacity) noexcept { queueCapacityOverride.store(capacity); }

bool InstallLifecycleProbesForTest(const uintptr_t targets[8], const uintptr_t continuations[8],
                                   const uintptr_t jumpTargets[8], std::string& error) {
    if (LifecycleProbesInstalled()) {
        error = "Lifecycle probes are already installed";
        return false;
    }
    if (!ProtectReaderInstalled()) {
        error = "Cannot install the probe fault-protection handler";
        return false;
    }
    gameThread = GetCurrentThreadId();
    for (size_t i = 0; i < kSiteCount; ++i) {
        target[i] = targets[i];
        continuation[i] = continuations[i];
        jumpTarget[i] = jumpTargets[i];
    }
    ResetState();
    for (size_t index = 0; index < kSiteCount; ++index) {
        if (!ApplyPatch(index, error)) {
            std::string rollbackError;
            for (size_t applied = index + 1; applied-- > 0;) {
                std::string siteError;
                if (!RestorePatch(applied, siteError) && rollbackError.empty())
                    rollbackError = siteError;
            }
            if (!rollbackError.empty())
                error = RestoreFailure(error + "; " + rollbackError);
            if (!ReleaseReaderProtection() && rollbackError.empty())
                error = RestoreFailure(error + "; reader protection could not be released");
            return false;
        }
    }
    SyncContinuationGlobals();
    gameThread = GetCurrentThreadId();
    owningThread = gameThread;
    captureActive.store(true);
    everInstalled = true;
    error.clear();
    return true;
}

bool LvzProbeReaderProtectionInstalledForTest() noexcept { return ReaderProtectionInstalled(); }
uint64_t LvzProbeReaderHandlersAddedForTest() noexcept { return readerHandlersAdded.load(); }
uint64_t LvzProbeReaderHandlersRemovedForTest() noexcept { return readerHandlersRemoved.load(); }
void LvzProbeSetInFlightForTest(uint32_t value) noexcept { inFlightForTest.store(value); }
#endif

bool RemoveLifecycleProbes(std::string& error) {
    if (!LifecycleProbesInstalled() && !ReaderProtectionInstalled()) {
        error.clear();
        return true;
    }
    if (GetCurrentThreadId() != gameThread) {
        error = "Remove requires the owning game thread";
        return false;
    }
    if (recyclePending.valid) {
        error = "Recycle candidate is pending completion; keep runtime DLL loaded";
        return false;
    }
    if (InFlightCount() != 0) {
        error = "Probe callback is still in flight; keep runtime DLL loaded";
        return false;
    }
    for (size_t index = 0; index < kSiteCount; ++index) {
        if (siteState[index] != kSiteOwned) continue;
        if (!BytesMatch(target[index], patchedBytes[index].data(), kSites[index].window)) {
            error = std::string("Probe patch ownership changed; refusing to overwrite: ") + kSites[index].id;
            return false;
        }
    }
    captureActive.store(false);
    std::string failure;
    for (size_t index = kSiteCount; index-- > 0;) {
        std::string siteError;
        if (!RestorePatch(index, siteError) && failure.empty()) failure = siteError;
    }
    if (!failure.empty()) {
        error = RestoreFailure(failure);
        return false;
    }
    if (!ReleaseReaderProtection()) {
        error = RestoreFailure("reader protection handler could not be released");
        return false;
    }
    error.clear();
    return true;
}

Json LifecycleProbeStatus() {
    Json counters = {
        {"captured", counterCaptured.load()},
        {"queued", queued},
        {"delivered", counterDelivered.load()},
        {"overflow", counterOverflow.load()},
        {"wrong_thread", counterWrongThread.load()},
        {"inactive_suppressed", counterInactive.load()},
        {"classify_refused", counterClassifyRefused.load()},
        {"read_failed", counterReadFailed.load()},
        {"live_skips", counterLiveSkips.load()},
        {"unmatched_commits", counterUnmatchedCommits.load()},
        {"pair_mismatch", counterPairMismatch.load()},
        {"overwritten_pending", counterOverwrittenPending.load()},
        {"faults", counterFaults.load()},
    };
    Json sites = Json::array();
    Json patchedSites = Json::array();
    for (size_t index = 0; index < kSiteCount; ++index) {
        Json bytes = Json::array();
        for (size_t i = 0; i < kSites[index].window; ++i) bytes.push_back(kSites[index].bytes[i]);
        sites.push_back({{"id", kSites[index].id}, {"va", kSites[index].va},
                         {"window_bytes", kSites[index].window}, {"bytes", bytes},
                         {"continuation_va", kSites[index].continuation}});
        if (siteState[index] == kSiteOwned) patchedSites.push_back(kSites[index].id);
    }
    const bool installed = LifecycleProbesInstalled();
    // Policy: live_skips is the expected live guard branch and is benign.
    // read_failed/classify_refused/inactive_suppressed are lost observations
    // and invalidate completeness; everything else is a fault.
    const bool healthy = everInstalled && counterFaults.load() == 0 && counterOverflow.load() == 0
        && counterWrongThread.load() == 0 && counterInactive.load() == 0
        && counterClassifyRefused.load() == 0 && counterReadFailed.load() == 0
        && counterUnmatchedCommits.load() == 0 && counterPairMismatch.load() == 0
        && counterOverwrittenPending.load() == 0 && !recyclePending.valid;
    return {{"installed", installed},
            {"reader_protected", ReaderProtectionInstalled()},
            {"pending_callbacks", InFlightCount()},
            {"active", captureActive.load()},
            {"healthy", healthy},
            {"pending_candidate", recyclePending.valid},
            {"patched_sites", patchedSites},
            {"counters", counters},
            {"sites", sites}};
}

Json DrainLifecycleProbeBatch() {
    if (!everInstalled) return Json::array();
    if (GetCurrentThreadId() != owningThread) {
        counterWrongThread.fetch_add(1);
        measurement::Host().OnWrongThread();
        throw std::runtime_error("Lifecycle probe drain requires the owning game thread");
    }
    if (recyclePending.valid) {
        counterFaults.fetch_add(1);
        measurement::Host().OnIncomplete();
        recyclePending = RecyclePending{};
    }
    Json batch = Json::array();
    for (size_t index = 0; index < queued; ++index) {
        const auto& record = queue[index];
        Json version = nullptr;
        std::string phase = "uncontrolled_update";
        Json callId = nullptr;
        if (record.boundaryValid) {
            version = {{"epoch", record.segment}, {"tick", record.tick}, {"revision", record.revision}};
            phase = "controlled_boundary";
            if (record.engineCallId) callId = record.engineCallId;
        }
        const auto site = kSites[record.site];
        Json base = {
            {"schema", "lvz.lifecycle-event.v2"},
            {"kind", ""},
            {"capture_sequence", record.sequence},
            {"version", version},
            {"version_phase", phase},
            {"engine_call_id", callId},
            {"entity", {{"id", record.entity}, {"slot", record.slot}, {"generation", record.entity >> 16}}},
            {"object", {{"class", "zombie"},
                        {"on_board", record.onBoard != 0},
                        {"wave", record.wave},
                        {"board", record.board}}},
            {"probe", {{"name", "zombie-lifecycle-store"}, {"schema", "lvz.lifecycle-event.v2"},
                       {"sequence_domain", "lvz.measurement.capture-sequence"}}},
            {"complete", true},
        };
        switch (record.kind) {
            case 0:
            case 1:
                base["kind"] = "zombie_phase_transition";
                base["phase"] = {{"site", site.id}, {"before", record.before}, {"after", record.after}};
                break;
            case 2:
                base["kind"] = "zombie_removal_marked";
                base["removal"] = {{"source", "dienoloot_mdead_store"},
                                   {"before", record.before}, {"after", record.after}};
                break;
            case 3:
                base["kind"] = "zombie_slot_recycle_candidate";
                base["recycle"] = {{"state", "candidate"}, {"slot", record.slot},
                                   {"free_head_before", record.freeHead}, {"count_before", record.count}};
                break;
            default:
                base["kind"] = "zombie_slot_recycle_commit";
                base["recycle"] = {{"state", "committed"}, {"slot", record.slot},
                                   {"candidate_capture_sequence", record.candidateSequence},
                                   {"free_head_after", record.freeHeadAfter}, {"count_after", record.countAfter}};
                break;
        }
        batch.push_back(std::move(base));
    }
    const size_t delivered = queued;
    queued = 0;
    counterDelivered.fetch_add(delivered);
    measurement::Host().OnDelivered(delivered);
    return batch;
}
}  // namespace lvz::determinism
