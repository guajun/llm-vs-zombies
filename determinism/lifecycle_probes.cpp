#include "lifecycle_probes.hpp"
#include "measurement.hpp"
#include "memory.hpp"
#include "spawn_hook.hpp"
#include <Windows.h>
#include <array>
#include <atomic>
#include <cstring>
#include <stdexcept>

namespace lvz::determinism {
namespace {
using Json = nlohmann::json;
constexpr size_t kQueueCapacity = 4096;
constexpr size_t kSiteCount = 8;

struct Site {
    const char* id;
    uintptr_t va;
    uint8_t window;
    uint8_t bytes[7];
    uint8_t registerIndex;  // 0=EDI, 1=ESI
    uint8_t kind;           // 0 immediate phase, 1 zamboni phase, 2 mdead, 3 guard, 4 commit
    int32_t value;
    uintptr_t continuation;
    uintptr_t jumpTarget;
};

// Locked windows mirror docs/issue111-patch-windows.json. Every window is a
// whole instruction (or whole instructions); the offline verifier proves no
// external branch enters a window.
const Site kSites[kSiteCount] = {
    {"phase-playdeathanim", 0x533377, 7, {0xc7,0x47,0x28,0x01,0x00,0x00,0x00}, 0, 0, 1, 0x53337e, 0},
    {"phase-applyburn", 0x532f5f, 7, {0xc7,0x46,0x28,0x02,0x00,0x00,0x00}, 1, 0, 2, 0x532f66, 0},
    {"phase-mowdown", 0x532a62, 7, {0xc7,0x47,0x28,0x03,0x00,0x00,0x00}, 0, 0, 3, 0x532a69, 0},
    {"phase-catapult", 0x52ec92, 7, {0xc7,0x47,0x28,0x01,0x00,0x00,0x00}, 0, 0, 1, 0x52ec99, 0},
    {"phase-zamboni", 0x52ea38, 6, {0x89,0x5f,0x28,0xd9,0x47,0x2c,0x00}, 0, 1, 1, 0x52ea3e, 0},
    {"removal-mdead", 0x530602, 7, {0xc6,0x87,0xec,0x00,0x00,0x00,0x01}, 0, 2, 1, 0x530609, 0},
    {"recycle-guard", 0x41bba9, 6, {0x38,0x9f,0xec,0x00,0x00,0x00,0x00}, 0, 3, 0, 0x41bbaf, 0},
    {"recycle-commit", 0x41bc23, 5, {0xe9,0x30,0xff,0xff,0xff,0x00,0x00}, 0, 4, 0, 0x41bb58, 0x41bb58},
};

struct Handlers {
    uint16_t kind;
    uint32_t entity;
    uint32_t slot;
    int32_t before;
    int32_t after;
    uint64_t sequence;
    uint64_t candidateSequence;
    uint32_t freeHead;
    uint32_t count;
    uint32_t board;
    uint32_t wave;
    uint64_t tick;
    uint64_t revision;
    uint64_t engineCallId;
    uint32_t segment;
    uint8_t boundaryValid;
    uint8_t site;
    uint8_t onBoard;
};
std::array<Handlers, kQueueCapacity> queue;
size_t queued = 0;
std::atomic<uint64_t> captured{0}, overflow{0}, wrongThread{0}, faults{0}, refused{0};
std::atomic<uint64_t> offBoardSkipped{0}, unmatchedCommits{0}, pairMismatch{0}, overwrittenPending{0};
struct Pending { bool valid = false; uint32_t slot = 0; uint32_t entity = 0; uint64_t sequence = 0; };
Pending pending;
bool installed = false;
DWORD gameThread = 0;
std::array<std::array<uint8_t, 7>, kSiteCount> original{};
std::array<std::array<uint8_t, 7>, kSiteCount> patched{};
std::array<uintptr_t, kSiteCount> target{};
std::array<uintptr_t, kSiteCount> continuation{};
std::array<uintptr_t, kSiteCount> jumpTarget{};

bool TryRead32(uintptr_t address, uint32_t& value) noexcept {
    return TryRead(address, value);
}
bool TryReadByte(uintptr_t address, uint8_t& value) noexcept {
    return TryRead(address, value);
}
bool TryWrite32(uintptr_t address, uint32_t value) noexcept {
    if (!Accessible(address, 4, true)) return false;
    std::memcpy(reinterpret_cast<void*>(address), &value, 4);
    return true;
}
bool TryWriteByte(uintptr_t address, uint8_t value) noexcept {
    if (!Accessible(address, 1, true)) return false;
    std::memcpy(reinterpret_cast<void*>(address), &value, 1);
    return true;
}

struct ObjectInfo {
    uint32_t entity = 0, slot = 0, board = 0;
    int32_t wave = 0;
    bool onBoard = false;
};

// Classify by the real board pool geometry and the IsOnBoard wave marker
// (mFromWave != -2 && != -3). The +4 board pointer alone is not trusted.
bool Classify(uint32_t zombie, ObjectInfo& info) noexcept {
    if (!zombie) return false;
    uint32_t entity = 0;
    if (!TryRead32(zombie + 0x158, entity) || !entity) return false;
    uint32_t board = 0;
    if (!TryRead32(zombie + 4, board) || !board) return false;
    uint32_t block = 0, used = 0, capacity = 0;
    if (!TryRead32(board + 0x90, block) || !TryRead32(board + 0x94, used) || !TryRead32(board + 0x98, capacity))
        return false;
    if (!block || used > capacity || capacity > 1024) return false;
    const uint32_t slot = entity & 0xffffu;
    if (slot >= used) return false;
    if (block + static_cast<uintptr_t>(slot) * 0x15c != zombie) return false;
    int32_t wave = 0;
    if (!TryRead32(zombie + 0x6c, reinterpret_cast<uint32_t&>(wave))) return false;
    info.entity = entity;
    info.slot = slot;
    info.board = board;
    info.wave = wave;
    info.onBoard = wave != -2 && wave != -3;
    return true;
}

void Push(const Handlers& record) noexcept {
    if (queued == kQueueCapacity) {
        overflow.fetch_add(1);
        measurement::Host().OnOverflow();
        return;
    }
    queue[queued++] = record;
    captured.fetch_add(1);
    measurement::Host().OnCaptured();
}

// A capture is only published after the original store executed; the shared
// sequence is allocated at that point.
uint64_t PublishSequence() noexcept {
    const uint64_t sequence = measurement::Host().NextSequence();
    if (!sequence) {
        faults.fetch_add(1);
        measurement::Host().OnIncomplete();
    }
    return sequence;
}

void Base(Handlers& record, uint16_t kind, uint8_t site, const ObjectInfo& info) noexcept {
    record.kind = kind;
    record.site = site;
    record.entity = info.entity;
    record.slot = info.slot;
    record.board = info.board;
    record.wave = static_cast<uint32_t>(info.wave);
    record.onBoard = info.onBoard ? 1 : 0;
    // Snapshot the controlled boundary at capture time; the drain must not
    // borrow a later boundary.
    const auto boundary = CurrentCaptureBoundary();
    record.boundaryValid = boundary.valid ? 1 : 0;
    record.tick = boundary.tick;
    record.revision = boundary.revision;
    record.segment = boundary.segment;
    record.engineCallId = boundary.engineCallId;
    record.sequence = 0;
    record.candidateSequence = 0;
    record.freeHead = 0;
    record.count = 0;
    record.before = 0;
    record.after = 0;
}

// Keep the controlled boundary snapshot in the record without another struct
// field explosion: tick/revision/segment/engine_call id are copied below.
struct BoundaryCopy { uint64_t tick, revision, engineCallId; uint32_t segment; bool valid; };
thread_local BoundaryCopy lastBoundary{};
}

extern "C" {
uint32_t lvzContinuation0 = static_cast<uint32_t>(0x53337e);
uint32_t lvzContinuation1 = static_cast<uint32_t>(0x532f66);
uint32_t lvzContinuation2 = static_cast<uint32_t>(0x532a69);
uint32_t lvzContinuation3 = static_cast<uint32_t>(0x52ec99);
uint32_t lvzContinuation4 = static_cast<uint32_t>(0x52ea3e);
uint32_t lvzContinuation5 = static_cast<uint32_t>(0x530609);
uint32_t lvzContinuation6 = static_cast<uint32_t>(0x41bbaf);
uint32_t lvzCommitTarget = static_cast<uint32_t>(0x41bb58);
void __cdecl LvzPhaseHandler(uint32_t site, uint32_t zombie) noexcept;
void __cdecl LvzZamboniHandler(uint32_t zombie, uint32_t value) noexcept;
void __cdecl LvzRemovalHandler(uint32_t zombie) noexcept;
void __cdecl LvzRecycleGuardHandler(uint32_t zombie) noexcept;
void __cdecl LvzRecycleCommitHandler(uint32_t zombie, uint32_t board) noexcept;

__attribute__((naked)) void LvzPhaseShim0() {
    __asm__ volatile(
        "pushfl\n\tpushal\n\tmovl %esp, %eax\n\tsubl $528, %esp\n\tandl $-16, %esp\n\t"
        "movl %eax, 512(%esp)\n\tfxsave (%esp)\n\tcld\n\t"
        "pushl %edi\n\tpushl $0\n\tcall _LvzPhaseHandler\n\taddl $8, %esp\n\t"
        "fxrstor (%esp)\n\tmovl 512(%esp), %esp\n\tpopal\n\tpopfl\n\tjmp *_lvzContinuation0\n\t");
}
__attribute__((naked)) void LvzPhaseShim1() {
    __asm__ volatile(
        "pushfl\n\tpushal\n\tmovl %esp, %eax\n\tsubl $528, %esp\n\tandl $-16, %esp\n\t"
        "movl %eax, 512(%esp)\n\tfxsave (%esp)\n\tcld\n\t"
        "pushl %esi\n\tpushl $1\n\tcall _LvzPhaseHandler\n\taddl $8, %esp\n\t"
        "fxrstor (%esp)\n\tmovl 512(%esp), %esp\n\tpopal\n\tpopfl\n\tjmp *_lvzContinuation1\n\t");
}
__attribute__((naked)) void LvzPhaseShim2() {
    __asm__ volatile(
        "pushfl\n\tpushal\n\tmovl %esp, %eax\n\tsubl $528, %esp\n\tandl $-16, %esp\n\t"
        "movl %eax, 512(%esp)\n\tfxsave (%esp)\n\tcld\n\t"
        "pushl %edi\n\tpushl $2\n\tcall _LvzPhaseHandler\n\taddl $8, %esp\n\t"
        "fxrstor (%esp)\n\tmovl 512(%esp), %esp\n\tpopal\n\tpopfl\n\tjmp *_lvzContinuation2\n\t");
}
__attribute__((naked)) void LvzPhaseShim3() {
    __asm__ volatile(
        "pushfl\n\tpushal\n\tmovl %esp, %eax\n\tsubl $528, %esp\n\tandl $-16, %esp\n\t"
        "movl %eax, 512(%esp)\n\tfxsave (%esp)\n\tcld\n\t"
        "pushl %edi\n\tpushl $3\n\tcall _LvzPhaseHandler\n\taddl $8, %esp\n\t"
        "fxrstor (%esp)\n\tmovl 512(%esp), %esp\n\tpopal\n\tpopfl\n\tjmp *_lvzContinuation3\n\t");
}
// ZamboniDeath: the window is `movl %ebx,0x28(%edi)` plus `flds 0x2c(%edi)`.
// The handler performs the store; the shim restores the pre-handler x87 state
// and then replays flds so the original x87 stack effect stands.
__attribute__((naked)) void LvzPhaseShim4() {
    __asm__ volatile(
        "pushfl\n\tpushal\n\tmovl %esp, %eax\n\tsubl $528, %esp\n\tandl $-16, %esp\n\t"
        "movl %eax, 512(%esp)\n\tfxsave (%esp)\n\tcld\n\t"
        "pushl %ebx\n\tpushl %edi\n\tcall _LvzZamboniHandler\n\taddl $8, %esp\n\t"
        "fxrstor (%esp)\n\tflds 0x2c(%edi)\n\tmovl 512(%esp), %esp\n\tpopal\n\tpopfl\n\tjmp *_lvzContinuation4\n\t");
}
__attribute__((naked)) void LvzRemovalShim5() {
    __asm__ volatile(
        "pushfl\n\tpushal\n\tmovl %esp, %eax\n\tsubl $528, %esp\n\tandl $-16, %esp\n\t"
        "movl %eax, 512(%esp)\n\tfxsave (%esp)\n\tcld\n\t"
        "pushl %edi\n\tcall _LvzRemovalHandler\n\taddl $4, %esp\n\t"
        "fxrstor (%esp)\n\tmovl 512(%esp), %esp\n\tpopal\n\tpopfl\n\tjmp *_lvzContinuation5\n\t");
}
// Recycle guard: execute the original compare first, keep its flags saved on
// the stack, record only the dead branch, then continue at the original `je`.
__attribute__((naked)) void LvzGuardShim6() {
    __asm__ volatile(
        "cmpb %bl, 0xec(%edi)\n\t"
        "pushfl\n\tpushal\n\tmovl %esp, %eax\n\tsubl $528, %esp\n\tandl $-16, %esp\n\t"
        "movl %eax, 512(%esp)\n\tfxsave (%esp)\n\tcld\n\t"
        "pushl %edi\n\tcall _LvzRecycleGuardHandler\n\taddl $4, %esp\n\t"
        "fxrstor (%esp)\n\tmovl 512(%esp), %esp\n\tpopal\n\tpopfl\n\tjmp *_lvzContinuation6\n\t");
}
// Recycle commit: the free-list update already happened; record and take the
// original jmp target directly (a trampoline would never return).
__attribute__((naked)) void LvzCommitShim7() {
    __asm__ volatile(
        "pushfl\n\tpushal\n\tmovl %esp, %eax\n\tsubl $528, %esp\n\tandl $-16, %esp\n\t"
        "movl %eax, 512(%esp)\n\tfxsave (%esp)\n\tcld\n\t"
        "pushl %esi\n\tpushl %edi\n\tcall _LvzRecycleCommitHandler\n\taddl $8, %esp\n\t"
        "fxrstor (%esp)\n\tmovl 512(%esp), %esp\n\tpopal\n\tpopfl\n\tjmp *_lvzCommitTarget\n\t");
}
}

thread_local DWORD lvzProbeThread = 0;

const uintptr_t kShims[kSiteCount] = {
    reinterpret_cast<uintptr_t>(&LvzPhaseShim0), reinterpret_cast<uintptr_t>(&LvzPhaseShim1),
    reinterpret_cast<uintptr_t>(&LvzPhaseShim2), reinterpret_cast<uintptr_t>(&LvzPhaseShim3),
    reinterpret_cast<uintptr_t>(&LvzPhaseShim4), reinterpret_cast<uintptr_t>(&LvzRemovalShim5),
    reinterpret_cast<uintptr_t>(&LvzGuardShim6), reinterpret_cast<uintptr_t>(&LvzCommitShim7),
};

bool ApplyPatch(size_t index, std::string& error) {
    const auto& site = kSites[index];
    const uintptr_t address = target[index];
    if (!Accessible(address, site.window)) {
        error = std::string("probe site is not accessible: ") + site.id;
        return false;
    }
    if (std::memcmp(reinterpret_cast<const void*>(address), site.bytes, site.window) != 0) {
        error = std::string("probe site signature mismatch: ") + site.id;
        return false;
    }
    std::array<uint8_t, 7> replacement{};
    replacement[0] = 0xe9;
    const intptr_t relative = static_cast<intptr_t>(kShims[index]) - static_cast<intptr_t>(address + 5);
    if (relative < INT32_MIN || relative > INT32_MAX) {
        error = std::string("probe site jump is out of range: ") + site.id;
        return false;
    }
    const int32_t rel32 = static_cast<int32_t>(relative);
    std::memcpy(replacement.data() + 1, &rel32, 4);
    for (size_t i = 5; i < site.window; ++i) replacement[i] = 0x90;
    std::memcpy(original[index].data(), reinterpret_cast<const void*>(address), site.window);
    std::memcpy(patched[index].data(), replacement.data(), site.window);
    DWORD previous = 0;
    if (!VirtualProtect(reinterpret_cast<void*>(address), site.window, PAGE_EXECUTE_READWRITE, &previous)) {
        error = std::string("VirtualProtect failed for ") + site.id;
        return false;
    }
    std::memcpy(reinterpret_cast<void*>(address), replacement.data(), site.window);
    DWORD ignored = 0;
    VirtualProtect(reinterpret_cast<void*>(address), site.window, previous, &ignored);
    FlushInstructionCache(GetCurrentProcess(), reinterpret_cast<void*>(address), site.window);
    return true;
}

void RestorePatch(size_t index) {
    DWORD previous = 0;
    const uintptr_t address = target[index];
    if (VirtualProtect(reinterpret_cast<void*>(address), kSites[index].window, PAGE_EXECUTE_READWRITE, &previous)) {
        std::memcpy(reinterpret_cast<void*>(address), original[index].data(), kSites[index].window);
        DWORD ignored = 0;
        VirtualProtect(reinterpret_cast<void*>(address), kSites[index].window, previous, &ignored);
        FlushInstructionCache(GetCurrentProcess(), reinterpret_cast<void*>(address), kSites[index].window);
    }
}

void ResetState() {
    queued = 0;
    pending = Pending{};
    captured = overflow = wrongThread = faults = refused = 0;
    offBoardSkipped = unmatchedCommits = pairMismatch = overwrittenPending = 0;
}

bool PrepareTable() {
    for (size_t i = 0; i < kSiteCount; ++i) {
        target[i] = kSites[i].va;
        continuation[i] = kSites[i].continuation;
        jumpTarget[i] = kSites[i].jumpTarget;
    }
    return true;
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

bool InstallLifecycleProbes(std::string& error) {
    if (installed) {
        error = "Lifecycle probes are already installed";
        return false;
    }
    if (!measurement::Host().BoundTo(GetCurrentThreadId())) {
        error = "Measurement session is not open on the game thread";
        return false;
    }
    PrepareTable();
    ResetState();
    for (size_t index = 0; index < kSiteCount; ++index) {
        if (!ApplyPatch(index, error)) {
            for (size_t applied = 0; applied < index; ++applied) RestorePatch(applied);
            return false;
        }
    }
    SyncContinuationGlobals();
    gameThread = GetCurrentThreadId();
    lvzProbeThread = gameThread;
    installed = true;
    error.clear();
    return true;
}

#ifdef LVZ_LIFECYCLE_PROBES_TESTING
bool InstallLifecycleProbesForTest(const uintptr_t targets[8], const uintptr_t continuations[8],
                                   const uintptr_t jumpTargets[8], std::string& error) {
    if (installed) {
        error = "Lifecycle probes are already installed";
        return false;
    }
    for (size_t i = 0; i < kSiteCount; ++i) {
        target[i] = targets[i];
        continuation[i] = continuations[i];
        jumpTarget[i] = jumpTargets[i];
    }
    ResetState();
    for (size_t index = 0; index < kSiteCount; ++index) {
        if (!ApplyPatch(index, error)) {
            for (size_t applied = 0; applied < index; ++applied) RestorePatch(applied);
            return false;
        }
    }
    SyncContinuationGlobals();
    gameThread = GetCurrentThreadId();
    lvzProbeThread = gameThread;
    installed = true;
    error.clear();
    return true;
}
#endif

bool RemoveLifecycleProbes(std::string& error) {
    if (!installed) {
        error.clear();
        return true;
    }
    if (GetCurrentThreadId() != gameThread) {
        error = "Remove requires the owning game thread";
        return false;
    }
    if (pending.valid) {
        error = "Recycle candidate is pending completion; keep runtime DLL loaded";
        return false;
    }
    for (size_t index = 0; index < kSiteCount; ++index) {
        if (std::memcmp(reinterpret_cast<const void*>(target[index]), patched[index].data(),
                        kSites[index].window) != 0) {
            error = std::string("Probe patch ownership changed; refusing to overwrite: ") + kSites[index].id;
            return false;
        }
    }
    for (size_t index = kSiteCount; index-- > 0;) RestorePatch(index);
    installed = false;
    error.clear();
    return true;
}

bool LifecycleProbesInstalled() noexcept { return installed; }

Json LifecycleProbeStatus() {
    Json counters = {
        {"captured", captured.load()}, {"queued", queued}, {"overflow", overflow.load()},
        {"wrong_thread", wrongThread.load()}, {"faults", faults.load()}, {"refused", refused.load()},
        {"off_board_skipped", offBoardSkipped.load()}, {"unmatched_commits", unmatchedCommits.load()},
        {"pair_mismatch", pairMismatch.load()}, {"overwritten_pending", overwrittenPending.load()},
        {"pending_candidate", pending.valid},
    };
    Json sites = Json::array();
    for (size_t index = 0; index < kSiteCount; ++index) {
        Json bytes = Json::array();
        for (size_t i = 0; i < kSites[index].window; ++i) bytes.push_back(kSites[index].bytes[i]);
        sites.push_back({{"id", kSites[index].id}, {"va", kSites[index].va}, {"window_bytes", kSites[index].window},
                         {"bytes", bytes}, {"continuation_va", continuation[index]}});
    }
    return {{"installed", installed}, {"counters", counters}, {"sites", sites},
            {"healthy", installed && faults.load() == 0 && !pending.valid}};
}

Json DrainLifecycleProbeBatch() {
    if (!installed) return Json::array();
    if (GetCurrentThreadId() != gameThread) {
        wrongThread.fetch_add(1);
        measurement::Host().OnWrongThread();
        throw std::runtime_error("Lifecycle probe drain requires the owning game thread");
    }
    if (pending.valid) {
        faults.fetch_add(1);
        measurement::Host().OnIncomplete();
        pending = Pending{};
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
            {"object", {{"class", "zombie"}, {"on_board", record.onBoard != 0}, {"wave", record.wave},
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
                base["removal"] = {{"source", "dienoloot_mdead_store"}, {"before", record.before},
                                   {"after", record.after}};
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
                                   {"free_head_after", record.freeHead}, {"count_after", record.count}};
                break;
        }
        batch.push_back(std::move(base));
    }
    const size_t delivered = queued;
    queued = 0;
    measurement::Host().OnDelivered(delivered);
    return batch;
}

// Handlers are C functions callable from the naked shims.
extern "C" void __cdecl LvzPhaseHandler(uint32_t site, uint32_t zombie) noexcept {
    using namespace lvz::determinism;
    const DWORD thread = GetCurrentThreadId();
    if (thread != lvzProbeThread) {
        wrongThread.fetch_add(1);
        measurement::Host().OnWrongThread();
        return;
    }
    ObjectInfo info;
    if (!Classify(zombie, info)) {
        faults.fetch_add(1);
        measurement::Host().OnIncomplete();
        return;
    }
    if (!info.onBoard) {
        offBoardSkipped.fetch_add(1);
        return;
    }
    int32_t before = 0;
    if (!TryRead32(zombie + 0x28, reinterpret_cast<uint32_t&>(before))) {
        faults.fetch_add(1);
        measurement::Host().OnIncomplete();
        return;
    }
    if (!TryWrite32(zombie + 0x28, static_cast<uint32_t>(kSites[site].value))) {
        faults.fetch_add(1);
        measurement::Host().OnIncomplete();
        return;
    }
    int32_t after = 0;
    TryRead32(zombie + 0x28, reinterpret_cast<uint32_t&>(after));
    const uint64_t sequence = PublishSequence();
    if (!sequence) return;
    Handlers record;
    Base(record, 0, static_cast<uint8_t>(site), info);
    record.sequence = sequence;
    record.before = before;
    record.after = after;
    Push(record);
}

extern "C" void __cdecl LvzZamboniHandler(uint32_t zombie, uint32_t value) noexcept {
    using namespace lvz::determinism;
    if (GetCurrentThreadId() != lvzProbeThread) {
        wrongThread.fetch_add(1);
        measurement::Host().OnWrongThread();
        return;
    }
    ObjectInfo info;
    if (!Classify(zombie, info)) {
        faults.fetch_add(1);
        measurement::Host().OnIncomplete();
        return;
    }
    if (!info.onBoard) {
        offBoardSkipped.fetch_add(1);
        return;
    }
    int32_t before = 0;
    TryRead32(zombie + 0x28, reinterpret_cast<uint32_t&>(before));
    if (!TryWrite32(zombie + 0x28, value)) {
        faults.fetch_add(1);
        measurement::Host().OnIncomplete();
        return;
    }
    int32_t after = 0;
    TryRead32(zombie + 0x28, reinterpret_cast<uint32_t&>(after));
    const uint64_t sequence = PublishSequence();
    if (!sequence) return;
    Handlers record;
    Base(record, 1, 4, info);
    record.sequence = sequence;
    record.before = before;
    record.after = after;
    Push(record);
}

extern "C" void __cdecl LvzRemovalHandler(uint32_t zombie) noexcept {
    using namespace lvz::determinism;
    if (GetCurrentThreadId() != lvzProbeThread) {
        wrongThread.fetch_add(1);
        measurement::Host().OnWrongThread();
        return;
    }
    ObjectInfo info;
    if (!Classify(zombie, info)) {
        faults.fetch_add(1);
        measurement::Host().OnIncomplete();
        return;
    }
    if (!info.onBoard) {
        offBoardSkipped.fetch_add(1);
        return;
    }
    uint8_t before = 0;
    TryReadByte(zombie + 0xec, before);
    if (!TryWriteByte(zombie + 0xec, 1)) {
        faults.fetch_add(1);
        measurement::Host().OnIncomplete();
        return;
    }
    uint8_t after = 0;
    TryReadByte(zombie + 0xec, after);
    const uint64_t sequence = PublishSequence();
    if (!sequence) return;
    Handlers record;
    Base(record, 2, 5, info);
    record.sequence = sequence;
    record.before = before;
    record.after = after;
    Push(record);
}

extern "C" void __cdecl LvzRecycleGuardHandler(uint32_t zombie) noexcept {
    using namespace lvz::determinism;
    if (GetCurrentThreadId() != lvzProbeThread) {
        wrongThread.fetch_add(1);
        measurement::Host().OnWrongThread();
        return;
    }
    ObjectInfo info;
    if (!Classify(zombie, info)) {
        faults.fetch_add(1);
        measurement::Host().OnIncomplete();
        return;
    }
    uint8_t dead = 0;
    if (!TryReadByte(zombie + 0xec, dead)) {
        faults.fetch_add(1);
        measurement::Host().OnIncomplete();
        return;
    }
    if (!dead) return;  // live branch leaves no pending candidate
    if (pending.valid) {
        overwrittenPending.fetch_add(1);
        faults.fetch_add(1);
        measurement::Host().OnIncomplete();
    }
    const uint64_t sequence = PublishSequence();
    if (!sequence) return;
    uint32_t freeHead = 0, poolCount = 0;
    TryRead32(info.board + 0x9c, freeHead);
    TryRead32(info.board + 0xa0, poolCount);
    pending = Pending{true, info.slot, info.entity, sequence};
    Handlers record;
    Base(record, 3, 6, info);
    record.sequence = sequence;
    record.freeHead = freeHead;
    record.count = poolCount;
    Push(record);
}

extern "C" void __cdecl LvzRecycleCommitHandler(uint32_t zombie, uint32_t board) noexcept {
    using namespace lvz::determinism;
    if (GetCurrentThreadId() != lvzProbeThread) {
        wrongThread.fetch_add(1);
        measurement::Host().OnWrongThread();
        return;
    }
    ObjectInfo info;
    if (!Classify(zombie, info)) {
        faults.fetch_add(1);
        measurement::Host().OnIncomplete();
        return;
    }
    uint32_t freeHead = 0, poolCount = 0;
    TryRead32(board + 0x9c, freeHead);
    TryRead32(board + 0xa0, poolCount);
    if (!pending.valid) {
        unmatchedCommits.fetch_add(1);
        faults.fetch_add(1);
        measurement::Host().OnIncomplete();
        return;
    }
    if (pending.slot != info.slot) {
        pairMismatch.fetch_add(1);
        faults.fetch_add(1);
        measurement::Host().OnIncomplete();
        return;
    }
    const uint64_t candidate = pending.sequence;
    const uint32_t entity = pending.entity;
    pending = Pending{};
    const uint64_t sequence = PublishSequence();
    if (!sequence) return;
    Handlers record;
    Base(record, 4, 7, info);
    record.sequence = sequence;
    record.entity = entity;
    record.candidateSequence = candidate;
    record.freeHead = freeHead;
    record.count = poolCount;
    Push(record);
}
}
