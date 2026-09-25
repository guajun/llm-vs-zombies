#include "determinism/lifecycle_probes.hpp"
#include "determinism/lifecycle_record.hpp"
#include "determinism/measurement.hpp"
#include "determinism/spawn_hook.hpp"
#include <Windows.h>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

// Offline fixture for the issue #111 exact-store probes.
//
// Every case executes the *real patched machine code*: the production
// installer patches executable copies of the locked windows, a naked caller
// sets registers/x87/SSE/MXCSR/flags/LastError, and the resulting machine
// state is compared byte-for-byte against the unpatched original bytes
// executed with the same inputs. The comparison covers all GPRs, EFLAGS, ESP,
// the full x87 register file, MXCSR and XMM0-7, plus the memory effect and the
// thread LastError value. No game is loaded.

namespace {
void Check(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

constexpr size_t kStride = 0x15c;
constexpr uint32_t kBoardMarker = 0xFFFFFFFEu;
constexpr uint32_t kXmmLoadAddress = 0xE0E0E0E0u;

struct Machine {
    alignas(16) uint8_t pool[2 * kStride]{};
    alignas(16) uint8_t board[0x200]{};
    alignas(16) uint8_t scratch[0x200]{};
    uint8_t* zombie(size_t slot) { return pool + slot * kStride; }
    const uint8_t* zombie(size_t slot) const { return pool + slot * kStride; }
};

void PrepareBoard(Machine& machine, uint32_t used, uint32_t head, int32_t count) {
    const uint32_t block = static_cast<uint32_t>(reinterpret_cast<uintptr_t>(machine.pool));
    std::memcpy(machine.board + 0x90, &block, 4);
    std::memcpy(machine.board + 0x94, &used, 4);
    const uint32_t capacity = 8;
    std::memcpy(machine.board + 0x98, &capacity, 4);
    std::memcpy(machine.board + 0x9c, &head, 4);
    std::memcpy(machine.board + 0xa0, &count, 4);
}

void PrepareZombie(Machine& machine, size_t slot, uint32_t generation, int32_t wave) {
    uint8_t* z = machine.zombie(slot);
    const uint32_t board = static_cast<uint32_t>(reinterpret_cast<uintptr_t>(machine.board));
    const uint32_t id = (generation << 16) | static_cast<uint32_t>(slot);
    std::memcpy(z + 0x158, &id, 4);
    std::memcpy(z + 4, &board, 4);
    std::memcpy(z + 0x6c, &wave, 4);
    z[0xec] = 0;
}

struct Context {
    uint32_t regs[6]{};   // edi, esi, ebp, ebx, eax, edx
    uint32_t esp = 0;     // 24
    uint32_t ecx = 0;     // 28
    uint32_t eflags = 0;  // 32
    alignas(16) uint8_t fxsave[512]{};  // 48
};

extern "C" uint32_t lvzTestMxcsr = 0x1f80;

extern "C" void CallPatched(void* fn, uint32_t edi, uint32_t esi, uint32_t ebx, uint32_t ebp,
                            const void* xmm, Context* after);
__attribute__((naked)) void CallPatched(void* fn, uint32_t edi, uint32_t esi, uint32_t ebx,
                                        uint32_t ebp, const void* xmm, Context* after) {
    __asm__ volatile(
        "pushl %ebx\n\tpushl %esi\n\tpushl %edi\n\tpushl %ebp\n\t"
        "movl 24(%esp), %edi\n\t"
        "movl 28(%esp), %esi\n\t"
        "movl 32(%esp), %ebx\n\t"
        "movl 36(%esp), %ebp\n\t"
        "movl 40(%esp), %ecx\n\t"  // xmm sentinels
        "movups 0(%ecx), %xmm0\n\tmovups 16(%ecx), %xmm1\n\tmovups 32(%ecx), %xmm2\n\t"
        "movups 48(%ecx), %xmm3\n\tmovups 64(%ecx), %xmm4\n\tmovups 80(%ecx), %xmm5\n\t"
        "movups 96(%ecx), %xmm6\n\tmovups 112(%ecx), %xmm7\n\t"
        "ldmxcsr _lvzTestMxcsr\n\t"
        "fninit\n\tfld1\n\tfldpi\n\t"
        "movl $0xE0E0E0E0, %eax\n\t"
        "movl $0xECECECEC, %ecx\n\t"
        "movl $0xEDEDEDED, %edx\n\t"
        "pushl $0x847\n\tpopfl\n\t"
        "call *20(%esp)\n\t"
        "pushl %ecx\n\t"
        "movl 48(%esp), %ecx\n\t"  // after context
        "movl %edi, 0(%ecx)\n\tmovl %esi, 4(%ecx)\n\tmovl %ebp, 8(%ecx)\n\t"
        "movl %ebx, 12(%ecx)\n\tmovl %eax, 16(%ecx)\n\tmovl %edx, 20(%ecx)\n\t"
        "movl (%esp), %eax\n\tmovl %eax, 28(%ecx)\n\t"
        "movl %esp, %eax\n\taddl $4, %eax\n\tmovl %eax, 24(%ecx)\n\t"
        "pushfl\n\tpopl %eax\n\tmovl %eax, 32(%ecx)\n\t"
        "fxsave 48(%ecx)\n\t"
        "addl $4, %esp\n\tpopl %ebp\n\tpopl %edi\n\tpopl %esi\n\tpopl %ebx\n\tret\n\t");
}

uint32_t Read32(const void* address) {
    uint32_t value = 0;
    std::memcpy(&value, address, 4);
    return value;
}
uint32_t U32(const void* address) {
    return static_cast<uint32_t>(reinterpret_cast<uintptr_t>(address));
}

// The recycle chain: guard bytes (patched), the real free-list update bytes
// from Board::ProcessDeleteQueue (0x41BBFF..0x41BC1E), then the commit window
// (patched) and a return.
const std::array<uint8_t, 6> kGuardBytes = {0x38, 0x9f, 0xec, 0x00, 0x00, 0x00};
const std::array<uint8_t, 31> kFreeChainBytes = {
    0x0f,0xb7,0x8f,0x58,0x01,0x00,0x00,  // movzwl 0x158(%edi),%ecx
    0x8b,0x86,0x9c,0x00,0x00,0x00,        // movl 0x9c(%esi),%eax
    0x89,0x8e,0x9c,0x00,0x00,0x00,        // movl %ecx,0x9c(%esi)
    0x89,0x87,0x58,0x01,0x00,0x00,        // movl %eax,0x158(%edi)
    0x01,0xae,0xa0,0x00,0x00,0x00,        // addl %ebp,0xa0(%esi)
};
const std::array<uint8_t, 5> kCommitBytes = {0xe9, 0x30, 0xff, 0xff, 0xff};
const std::array<std::array<uint8_t, 7>, 8> kWindows = {{
    {0xc7,0x47,0x28,0x01,0x00,0x00,0x00}, {0xc7,0x46,0x28,0x02,0x00,0x00,0x00},
    {0xc7,0x47,0x28,0x03,0x00,0x00,0x00}, {0xc7,0x47,0x28,0x01,0x00,0x00,0x00},
    {0x89,0x5f,0x28,0xd9,0x47,0x2c,0x00}, {0xc6,0x87,0xec,0x00,0x00,0x00,0x01},
    {0x38,0x9f,0xec,0x00,0x00,0x00,0x00}, {0xe9,0x30,0xff,0xff,0xff,0x00,0x00},
}};
const std::array<size_t, 8> kWindowSizes = {7, 7, 7, 7, 6, 7, 6, 5};

struct Pages {
    uint8_t* page = nullptr;
    uintptr_t addresses[8]{};
    uintptr_t continuations[8]{};
    uintptr_t jumpTargets[8]{};
    uint8_t* window(size_t index) const { return page + index * 0x100; }
};

Pages MakePages(bool baseline) {
    Pages pages;
    pages.page = static_cast<uint8_t*>(VirtualAlloc(nullptr, 0x1000, MEM_COMMIT | MEM_RESERVE,
                                                    PAGE_EXECUTE_READWRITE));
    if (!pages.page) throw std::runtime_error("Cannot allocate the executable test page");
    for (size_t index = 0; index < 6; ++index) {
        uint8_t* slot = pages.window(index);
        std::memcpy(slot, kWindows[index].data(), kWindowSizes[index]);
        slot[kWindowSizes[index]] = 0xc3;
        pages.addresses[index] = reinterpret_cast<uintptr_t>(slot);
        pages.continuations[index] = reinterpret_cast<uintptr_t>(slot + kWindowSizes[index]);
        pages.jumpTargets[index] = reinterpret_cast<uintptr_t>(slot + kWindowSizes[index]);
    }
    uint8_t* chain = pages.window(6);
    std::memcpy(chain, kGuardBytes.data(), kGuardBytes.size());
    // The original instruction after the guard is `je <loop head>`: live
    // objects skip the free path, dead objects fall through into it.
    uint8_t* branch = chain + kGuardBytes.size();
    uint8_t* freeCode = branch + 2;
    std::memcpy(freeCode, kFreeChainBytes.data(), kFreeChainBytes.size());
    uint8_t* jumpBytes = freeCode + kFreeChainBytes.size();
    uint8_t* liveRet = jumpBytes + 5;
    branch[0] = 0x74;
    branch[1] = static_cast<uint8_t>(liveRet - (branch + 2));
    uint8_t* commitSlot = pages.window(7);
    const intptr_t jumpToCommit = reinterpret_cast<uintptr_t>(commitSlot)
        - reinterpret_cast<uintptr_t>(jumpBytes + 5);
    jumpBytes[0] = 0xe9;
    const int32_t jumpRel = static_cast<int32_t>(jumpToCommit);
    std::memcpy(jumpBytes + 1, &jumpRel, 4);
    *liveRet = 0xc3;
    std::memcpy(commitSlot, kCommitBytes.data(), kCommitBytes.size());
    if (baseline) {
        const intptr_t jumpToReturn = reinterpret_cast<uintptr_t>(commitSlot + kCommitBytes.size())
            - reinterpret_cast<uintptr_t>(commitSlot + 5);
        commitSlot[0] = 0xe9;
        std::memcpy(commitSlot + 1, &jumpToReturn, 4);
    }
    commitSlot[kCommitBytes.size()] = 0xc3;
    pages.addresses[6] = reinterpret_cast<uintptr_t>(chain);
    pages.continuations[6] = reinterpret_cast<uintptr_t>(chain + kGuardBytes.size());
    pages.jumpTargets[6] = 0;
    pages.addresses[7] = reinterpret_cast<uintptr_t>(commitSlot);
    pages.continuations[7] = 0;
    pages.jumpTargets[7] = reinterpret_cast<uintptr_t>(commitSlot + kCommitBytes.size());
    return pages;
}

struct RunResult {
    Context context;
    DWORD lastError = 0;
    Machine machine;
};

// Runs one window against `work` in place. Both the baseline and the patched
// run use the same machine address, so the compared register values (including
// the pointer arguments) are identical inputs.
// variation: 0 normal, 1 foreign board pointer, 2 wild board pointer, 3 free
// slot (generation 0).
void RelocatePointers(Machine& work) {
    // Machine structs are copied between runs, so the self-referencing pool
    // and board pointers must be re-anchored to the copy's own address.
    const uint32_t board = U32(work.board);
    const uint32_t block = U32(work.pool);
    std::memcpy(work.board + 0x90, &block, 4);
    for (size_t slot = 0; slot < 2; ++slot)
        std::memcpy(work.zombie(slot) + 4, &board, 4);
}

RunResult RunOne(uintptr_t function, Machine& work, size_t slot, uint32_t esi, uint32_t ebx,
                 uint32_t ebp, const uint8_t* xmm, int variation = 0) {
    RunResult result;
    RelocatePointers(work);
    if (variation == 1) {
        const uint32_t foreign = U32(work.scratch);
        std::memcpy(work.zombie(slot) + 4, &foreign, 4);
    } else if (variation == 2) {
        const uint32_t wild = 1;
        std::memcpy(work.zombie(slot) + 4, &wild, 4);
    } else if (variation == 3) {
        const uint32_t freeSlot = 0;
        std::memcpy(work.zombie(slot) + 0x158, &freeSlot, 4);
    } else if (variation == 4) {
        std::memcpy(work.zombie(slot) + 4, &esi, 4);
    } else if (variation == 5) {
        // Fake board at the caller address; re-anchor its pool block to this
        // copy and point the zombie at it after relocation.
        const uint32_t localBlock = U32(work.pool);
        std::memcpy(reinterpret_cast<void*>(static_cast<uintptr_t>(esi) + 0x90), &localBlock, 4);
        std::memcpy(work.zombie(slot) + 4, &esi, 4);
    }
    const uint32_t edi = U32(work.zombie(slot));
    const uint32_t esiValue = (esi == kBoardMarker) ? U32(work.board) : esi;
    SetLastError(0x5A5A1234);
    CallPatched(reinterpret_cast<void*>(function), edi, esiValue, ebx, ebp, xmm, &result.context);
    result.lastError = GetLastError();
    result.machine = work;
    return result;
}

void ExpectSameMachine(const RunResult& baseline, const RunResult& patched, const char* label) {
    const auto fpu_equal = [](const uint8_t* left, const uint8_t* right) {
        // FCW/FSW/FTW, MXCSR+mask, the eight x87 registers and XMM0-7. The
        // FIP/FOP/FDP diagnostics are not part of the architectural result.
        return std::memcmp(left, right, 6) == 0
            && std::memcmp(left + 24, right + 24, 8) == 0
            && std::memcmp(left + 32, right + 32, 128) == 0
            && std::memcmp(left + 160, right + 160, 128) == 0;
    };
    const bool contextEqual = std::memcmp(baseline.context.regs, patched.context.regs,
                                          sizeof(baseline.context.regs)) == 0
        && baseline.context.esp == patched.context.esp
        && baseline.context.ecx == patched.context.ecx
        && baseline.context.eflags == patched.context.eflags
        && fpu_equal(baseline.context.fxsave, patched.context.fxsave);
    const bool memoryEqual = std::memcmp(baseline.machine.pool, patched.machine.pool,
                                         sizeof(baseline.machine.pool)) == 0
        && std::memcmp(baseline.machine.board, patched.machine.board, sizeof(baseline.machine.board)) == 0
        && std::memcmp(baseline.machine.scratch, patched.machine.scratch,
                       sizeof(baseline.machine.scratch)) == 0;
    if (!contextEqual || !memoryEqual) {
        std::cerr << "context " << (contextEqual ? 1 : 0) << " memory " << (memoryEqual ? 1 : 0) << std::endl;
        for (int i = 0; i < 6; ++i)
            std::cerr << "reg" << i << " " << std::hex << baseline.context.regs[i] << " "
                      << patched.context.regs[i] << std::endl;
        std::cerr << "esp " << std::hex << baseline.context.esp << " " << patched.context.esp << std::endl;
        std::cerr << "ecx " << std::hex << baseline.context.ecx << " " << patched.context.ecx << std::endl;
        std::cerr << "efl " << std::hex << baseline.context.eflags << " " << patched.context.eflags << std::endl;
        std::cerr << "fcw " << std::hex << *reinterpret_cast<const uint16_t*>(baseline.context.fxsave)
                  << " " << *reinterpret_cast<const uint16_t*>(patched.context.fxsave) << std::endl;
        std::cerr << "fsw " << std::hex << *reinterpret_cast<const uint16_t*>(baseline.context.fxsave + 2)
                  << " " << *reinterpret_cast<const uint16_t*>(patched.context.fxsave + 2) << std::endl;
        std::cerr << "ftw " << std::hex << *reinterpret_cast<const uint16_t*>(baseline.context.fxsave + 4)
                  << " " << *reinterpret_cast<const uint16_t*>(patched.context.fxsave + 4) << std::endl;
        std::cerr << "mxcsr " << std::hex << *reinterpret_cast<const uint32_t*>(baseline.context.fxsave + 24)
                  << " " << *reinterpret_cast<const uint32_t*>(patched.context.fxsave + 24) << std::endl;
        throw std::runtime_error(std::string("machine differs from the unpatched original: ") + label);
    }
    if (baseline.lastError != patched.lastError || baseline.lastError != 0x5A5A1234) {
        throw std::runtime_error(std::string("LastError differs from the unpatched original: ") + label);
    }
}

const uint8_t* XmmSentinels() {
    static std::array<uint8_t, 128> sentinels = [] {
        std::array<uint8_t, 128> value{};
        for (size_t index = 0; index < value.size(); ++index)
            value[index] = static_cast<uint8_t>(0x80 + index);
        return value;
    }();
    return sentinels.data();
}

// Executes the same case against the unpatched and the patched page using the
// same machine address and asserts full equality.
RunResult CompareCase(uintptr_t baselineFunction, uintptr_t patchedFunction, const Machine& initial,
                      size_t slot, uint32_t esi, uint32_t ebx, uint32_t ebp, const char* label,
                      int variation = 0) {
    Machine work = initial;
    const RunResult base = RunOne(baselineFunction, work, slot, esi, ebx, ebp, XmmSentinels(), variation);
    work = initial;
    const RunResult shot = RunOne(patchedFunction, work, slot, esi, ebx, ebp, XmmSentinels(), variation);
    ExpectSameMachine(base, shot, label);
    return shot;
}

nlohmann::json EventOfKind(const nlohmann::json& batch, const char* kind) {
    for (const auto& item : batch)
        if (item.value("kind", "") == kind) return item;
    throw std::runtime_error(std::string("missing event kind: ") + kind);
}

size_t CountOfKind(const nlohmann::json& batch, const char* kind) {
    size_t count = 0;
    for (const auto& item : batch)
        if (item.value("kind", "") == kind) ++count;
    return count;
}

void ExpectWindowBytes(const Pages& pages, size_t index, const char* label) {
    if (std::memcmp(pages.window(index), kWindows[index].data(), kWindowSizes[index]) != 0)
        throw std::runtime_error(std::string("window bytes mismatch: ") + label);
}

int RunTests() {
    using lvz::determinism::DrainLifecycleProbeBatch;
    using lvz::determinism::InstallLifecycleProbesForTest;
    using lvz::determinism::LifecycleProbesInstalled;
    using lvz::determinism::LifecycleProbeStatus;
    using lvz::determinism::LvzProbeReaderHandlersAddedForTest;
    using lvz::determinism::LvzProbeReaderHandlersRemovedForTest;
    using lvz::determinism::LvzProbeReaderProtectionInstalledForTest;
    using lvz::determinism::LvzProbeSetInFlightForTest;
    using lvz::determinism::LvzProbeSetActiveForTest;
    using lvz::determinism::LvzProbeSetQueueCapacityForTest;
    using lvz::determinism::LvzProbeSetThreadForTest;
    using lvz::determinism::LvzProbeSetVirtualProtectFailureForTest;
    using lvz::determinism::RemoveLifecycleProbes;
    using lvz::measurement::Host;

    std::string error;
    Check(Host().Open(GetCurrentThreadId(), &error), "open measurement session");
    Pages baseline = MakePages(true);
    Pages patched = MakePages(false);
    {
        const auto offBatch = DrainLifecycleProbeBatch();
        const auto offStatus = LifecycleProbeStatus();
        Check(offBatch.empty(), "never-installed drain must return no facts");
        Check(!offStatus.at("installed").get<bool>() && !offStatus.at("reader_protected").get<bool>(),
              "never-installed state must not claim installation");
        Check(offStatus.at("counters").at("wrong_thread").get<uint64_t>() == 0
                  && offStatus.at("counters").at("faults").get<uint64_t>() == 0,
              "never-installed drain must not poison baseline counters");
    }
    Check(InstallLifecycleProbesForTest(patched.addresses, patched.continuations, patched.jumpTargets, error),
          error.c_str());
    Check(LvzProbeReaderProtectionInstalledForTest(), "reader protection must live while patches are owned");
    Check(LifecycleProbesInstalled(), "probes must be installed");
    Check(*reinterpret_cast<const uint8_t*>(patched.addresses[2]) == 0xe9, "patched byte must be a jump");

    Machine machine;
    PrepareBoard(machine, 2, 0, 2);
    PrepareZombie(machine, 0, 2, 0);
    PrepareZombie(machine, 1, 5, 0);

    // 1. Phase store with a live on-board object: unconditional store and full
    //    machine-state equality against the unpatched window bytes.
    {
        const RunResult shot = CompareCase(baseline.addresses[2], patched.addresses[2], machine, 0,
                                           0x11111111u, 0x22222222u, 0x33333333u, "phase-mowdown");
        Check(Read32(shot.machine.zombie(0) + 0x28) == 3, "the original store must execute exactly once");
    }
    // 2. Off-board preview objects (-2/-3) still execute the store and are
    //    published with on_board=false instead of being suppressed.
    for (int32_t wave : {-2, -3}) {
        Machine initial = machine;
        PrepareZombie(initial, 0, 2, wave);
        (void)CompareCase(baseline.addresses[2], patched.addresses[2], initial, 0, 7u, 8u, 9u,
                          "off-board phase store");
    }
    // 5. Zamboni store + flds: x87 state (all ST registers, control word) must
    //    match the original execution exactly.
    {
        Machine initial = machine;
        initial.zombie(0)[0x28] = 0;
        const float loadTarget = 1.5f;
        std::memcpy(initial.zombie(0) + 0x2c, &loadTarget, 4);
        (void)CompareCase(baseline.addresses[4], patched.addresses[4], initial, 0, 0, 7u, 0,
                          "zamboni store+flds");
    }
    // 6. Removal mDead byte store.
    {
        (void)CompareCase(baseline.addresses[5], patched.addresses[5], machine, 0, 0, 0, 0, "removal mdead");
    }
    // 7. Controlled boundary facts keep the engine call id (same-call
    //    lifetime correlation).
    {
        lvz::determinism::SetSpawnBoundaryForTest(7, 9, 2, 42);
        Machine boundaryWork = machine;
        (void)RunOne(patched.addresses[2], boundaryWork, 0, 0, 0, 0, XmmSentinels());
        const auto batch = DrainLifecycleProbeBatch();
        bool controlled = false;
        for (const auto& item : batch) {
            if (item.value("kind", "") == "zombie_phase_transition"
                && item.value("version_phase", "") == "controlled_boundary"
                && item.contains("engine_call_id") && item.at("engine_call_id") == 42)
                controlled = true;
        }
        Check(controlled, "controlled boundary call id must be captured");
        Check(CountOfKind(batch, "zombie_phase_transition") >= 2 && CountOfKind(batch, "zombie_removal_marked") == 1,
              "phase facts must be preserved, not deduplicated");
        lvz::determinism::ClearSpawnBoundaryForTest();
    }
    // 8. Recycle: live branch leaves no candidate; the full chain executes the
    //    real free-list update and the commit validates it from the pre-free
    //    snapshot.
    {
        Machine live = machine;
        live.zombie(0)[0xec] = 0;
        (void)RunOne(patched.addresses[6], live, 0, kBoardMarker, 0, static_cast<uint32_t>(-1), XmmSentinels());
        Machine initial = machine;
        Check(CountOfKind(DrainLifecycleProbeBatch(), "zombie_slot_recycle_candidate") == 0,
              "live guard branch must not emit a candidate");
        uint32_t head = 0, count = 2;
        std::memcpy(initial.board + 0x9c, &head, 4);
        std::memcpy(initial.board + 0xa0, &count, 4);
        initial.zombie(0)[0xec] = 1;
        (void)CompareCase(baseline.addresses[6], patched.addresses[6], initial, 0, kBoardMarker, 0,
                          static_cast<uint32_t>(-1), "recycle chain");
        const auto batch = DrainLifecycleProbeBatch();
        const auto candidate = EventOfKind(batch, "zombie_slot_recycle_candidate");
        const auto commit = EventOfKind(batch, "zombie_slot_recycle_commit");
        Check(candidate.at("recycle").at("free_head_before") == 0
                  && candidate.at("recycle").at("count_before") == 2,
              "candidate must keep the pre-free free-list state");
        Check(commit.at("recycle").at("candidate_capture_sequence") == candidate.at("capture_sequence")
                  && commit.at("recycle").at("free_head_after") == 0
                  && commit.at("recycle").at("count_after") == 1,
              "commit must validate and record the real free-list transition");
        Check(commit.at("entity").at("id") == candidate.at("entity").at("id"),
              "commit must keep the preserved full entity identity");
    }
    // 9. Second recycle with a nonzero old head and slot 1: head becomes the
    //    slot, count decrements, and the old head is linked into +0x158.
    {
        Machine initial = machine;
        const uint32_t oldHead = 0x0041bb58u;
        uint32_t count = 3;
        std::memcpy(initial.board + 0x9c, &oldHead, 4);
        std::memcpy(initial.board + 0xa0, &count, 4);
        initial.zombie(1)[0xec] = 1;
        (void)CompareCase(baseline.addresses[6], patched.addresses[6], initial, 1, kBoardMarker, 0,
                          static_cast<uint32_t>(-1), "second recycle chain");
        const auto batch = DrainLifecycleProbeBatch();
        const auto commit = EventOfKind(batch, "zombie_slot_recycle_commit");
        Check(commit.at("entity").at("slot") == 1 && commit.at("recycle").at("free_head_after") == 1
                  && commit.at("recycle").at("count_after") == 2,
              "second recycle must record the slot/head/count transition from the preserved identity");
    }
    Check(LifecycleProbeStatus().at("healthy").get<bool>(), "capture must be healthy after the normal cases");

    // 3+4. Lost observations are injected only after the healthy window so the
    // benign live_skips policy stays separate from read/classification faults.
    // 3. Nonmatching pool pointer and generation-0 free slot: observer refuses
    //    to classify but the store still executes exactly once.
    {
        const RunResult shot = CompareCase(baseline.addresses[2], patched.addresses[2], machine, 0, 1u, 2u, 3u,
                                           "foreign board pointer", 1);
        Check(Read32(shot.machine.zombie(0) + 0x28) == 3, "store must survive a refused classification");
        (void)CompareCase(baseline.addresses[2], patched.addresses[2], machine, 0, 1u, 2u, 3u,
                          "generation-0 free slot", 3);
    }
    // 4. Unreadable observer address (wild board pointer): the protected hook
    //    reader must not change the original store semantics.
    {
        (void)CompareCase(baseline.addresses[2], patched.addresses[2], machine, 0, 4u, 5u, 6u,
                          "fault-protected observer read", 2);
    }
    {
        // Classification can succeed while the free-list read fails; the real
        // guard compare still runs, and the lost observation must invalidate
        // completeness instead of being silently accepted.
        {
            const auto refusedStatus = LifecycleProbeStatus();
            Check(!refusedStatus.at("healthy").get<bool>(),
                  "refused classifications must invalidate health");
            Check(refusedStatus.at("counters").at("classify_refused").get<uint64_t>() >= 3,
                  "refused classifications must be counted");
        }
        Check(RemoveLifecycleProbes(error), error.c_str());
        Pages guardOnly = MakePages(false);
        std::memcpy(guardOnly.window(6), kGuardBytes.data(), kGuardBytes.size());
        guardOnly.window(6)[kGuardBytes.size()] = 0xc3;
        guardOnly.continuations[6] = reinterpret_cast<uintptr_t>(guardOnly.window(6) + kGuardBytes.size());
        Check(InstallLifecycleProbesForTest(guardOnly.addresses, guardOnly.continuations,
                                            guardOnly.jumpTargets, error), error.c_str());
        const size_t pageSize = 4096;
        uint8_t* pages = static_cast<uint8_t*>(VirtualAlloc(nullptr, pageSize * 2, MEM_COMMIT | MEM_RESERVE,
                                                            PAGE_READWRITE));
        Check(pages != nullptr, "cannot allocate the boundary board page");
        const uint32_t boardAddress = static_cast<uint32_t>(reinterpret_cast<uintptr_t>(pages + pageSize) - 0x9c);
        const uint32_t block = static_cast<uint32_t>(reinterpret_cast<uintptr_t>(machine.pool));
        const uint32_t used = 2, capacity = 8;
        std::memcpy(pages + pageSize - 0x9c + 0x90, &block, 4);
        std::memcpy(pages + pageSize - 0x9c + 0x94, &used, 4);
        std::memcpy(pages + pageSize - 0x9c + 0x98, &capacity, 4);
        DWORD previousProtection = 0;
        Check(VirtualProtect(pages + pageSize, pageSize, PAGE_NOACCESS, &previousProtection) != 0,
              "cannot protect the boundary page");
        Machine boundary = machine;
        boundary.zombie(0)[0xec] = 1;
        (void)RunOne(guardOnly.addresses[6], boundary, 0, boardAddress, 0, 0, XmmSentinels(), 5);
        VirtualFree(pages, 0, MEM_RELEASE);
        Check(RemoveLifecycleProbes(error), error.c_str());
        const auto failedStatus = LifecycleProbeStatus();
        Check(!failedStatus.at("healthy").get<bool>(), "lost observations must invalidate health");
        Check(failedStatus.at("counters").at("read_failed").get<uint64_t>() >= 1,
              "read failures must be counted");
        Check(InstallLifecycleProbesForTest(patched.addresses, patched.continuations, patched.jumpTargets, error),
              error.c_str());
    }

    // 10. Wrong thread: store still executes, nothing is published.
    {
        LvzProbeSetThreadForTest(GetCurrentThreadId() + 1u);
        (void)CompareCase(baseline.addresses[2], patched.addresses[2], machine, 0, 0, 0, 0, "wrong-thread store");
        Check(LifecycleProbeStatus().at("counters").at("wrong_thread").get<uint64_t>() >= 1,
              "wrong thread must be counted");
        LvzProbeSetThreadForTest(GetCurrentThreadId());
    }
    // 11. Stopped measurement: store still executes, observation suppressed.
    {
        LvzProbeSetActiveForTest(false);
        (void)CompareCase(baseline.addresses[2], patched.addresses[2], machine, 0, 0, 0, 0, "inactive store");
        Check(LifecycleProbeStatus().at("counters").at("inactive_suppressed").get<uint64_t>() >= 1,
              "inactive capture must be counted");
        LvzProbeSetActiveForTest(true);
    }
    // 12. Queue overflow: store still executes and overflow is counted.
    {
        LvzProbeSetQueueCapacityForTest(1);
        (void)CompareCase(baseline.addresses[2], patched.addresses[2], machine, 0, 0, 0, 0,
                          "overflow first store");
        Machine overflowWork = machine;
        (void)RunOne(patched.addresses[2], overflowWork, 0, 0, 0, 0, XmmSentinels());
        Check(LifecycleProbeStatus().at("counters").at("overflow").get<uint64_t>() >= 1,
              "overflow must be counted");
        LvzProbeSetQueueCapacityForTest(0);
        (void)DrainLifecycleProbeBatch();
    }
    // 13. Restore ownership, injected protection failure and retry.
    Check(RemoveLifecycleProbes(error), error.c_str());
    for (size_t index = 0; index < 8; ++index) ExpectWindowBytes(patched, index, "restore after normal removal");
    Check(!LifecycleProbesInstalled(), "probes must be removed");
    Check(!LvzProbeReaderProtectionInstalledForTest(), "a clean removal must release the reader handler");
    Check(LvzProbeReaderHandlersRemovedForTest() == LvzProbeReaderHandlersAddedForTest(),
          "a clean removal must unregister exactly the acquired handler");
    Check(InstallLifecycleProbesForTest(patched.addresses, patched.continuations, patched.jumpTargets, error),
          error.c_str());
    Check(LvzProbeReaderProtectionInstalledForTest(), "reinstall must register a new handler");
    LvzProbeSetVirtualProtectFailureForTest(true);
    Check(!RemoveLifecycleProbes(error), "a protection failure must refuse removal");
    Check(LifecycleProbesInstalled(), "a failed removal must keep the probes tracked");
    Check(LvzProbeReaderProtectionInstalledForTest(), "a failed removal must keep the handler alive");
    Check(*reinterpret_cast<const uint8_t*>(patched.addresses[2]) == 0xe9, "failed removal must keep the jump");
    LvzProbeSetVirtualProtectFailureForTest(false);
    Check(RemoveLifecycleProbes(error), error.c_str());
    Check(!LifecycleProbesInstalled(), "retry after cleared injection must restore");
    Check(!LvzProbeReaderProtectionInstalledForTest(), "the retry must release the handler");
    // 14. Ownership replacement: a foreign patch is never overwritten.
    Check(InstallLifecycleProbesForTest(patched.addresses, patched.continuations, patched.jumpTargets, error),
          error.c_str());
    {
        uint8_t* site = patched.window(5);
        site[0] = 0x90;
        Check(!RemoveLifecycleProbes(error), "a foreign patch must refuse removal");
        Check(LifecycleProbesInstalled(), "refused removal keeps the probes tracked");
        std::array<uint8_t, 7> patchedBytes{};
        patchedBytes[0] = 0xe9;
        const int32_t rel32 = *reinterpret_cast<const int32_t*>(patched.addresses[5] + 1);
        std::memcpy(patchedBytes.data() + 1, &rel32, 4);
        for (size_t i = 5; i < kWindowSizes[5]; ++i) patchedBytes[i] = 0x90;
        std::memcpy(site, patchedBytes.data(), kWindowSizes[5]);
        Check(RemoveLifecycleProbes(error), error.c_str());
        Check(!LifecycleProbesInstalled(), "ownership-restored removal must succeed");
    }
    // 15. Partial install failure must roll back earlier sites.
    {
        Pages broken = MakePages(false);
        broken.window(3)[0] = 0x90;
        std::string mismatch;
        Check(!InstallLifecycleProbesForTest(broken.addresses, broken.continuations, broken.jumpTargets, mismatch),
              "signature mismatch must be refused");
        Check(!LifecycleProbesInstalled(), "a failed install must not leave probes installed");
        Check(!LvzProbeReaderProtectionInstalledForTest(), "a failed install must release its handler");
        Check(LvzProbeReaderHandlersRemovedForTest() == LvzProbeReaderHandlersAddedForTest(),
              "a failed install must unregister the acquired handler");
        for (size_t index = 0; index < 3; ++index) ExpectWindowBytes(broken, index, "rollback of the partial install");
    }
    // 16. Incomplete recycle candidate: pending blocks removal, drain clears it.
    {
        Pages guardOnly = MakePages(false);
        std::memcpy(guardOnly.window(6), kGuardBytes.data(), kGuardBytes.size());
        guardOnly.window(6)[kGuardBytes.size()] = 0xc3;
        guardOnly.continuations[6] = reinterpret_cast<uintptr_t>(guardOnly.window(6) + kGuardBytes.size());
        Check(InstallLifecycleProbesForTest(guardOnly.addresses, guardOnly.continuations,
                                            guardOnly.jumpTargets, error), error.c_str());
        Machine guardInput = machine;
        guardInput.zombie(0)[0xec] = 1;
        (void)RunOne(guardOnly.addresses[6], guardInput, 0, kBoardMarker, 0, 0, XmmSentinels());
        Check(LifecycleProbeStatus().at("pending_candidate").get<bool>(), "guard must leave a pending candidate");
        Check(!RemoveLifecycleProbes(error), "pending candidate must block removal");
        (void)DrainLifecycleProbeBatch();
        Check(!LifecycleProbeStatus().at("pending_candidate").get<bool>(), "drain must clear the pending candidate");
        Check(RemoveLifecycleProbes(error), error.c_str());
    }
    // 17. Drain requires the owning thread.
    {
        Check(InstallLifecycleProbesForTest(patched.addresses, patched.continuations, patched.jumpTargets, error),
              error.c_str());
        LvzProbeSetInFlightForTest(1);
        Check(!RemoveLifecycleProbes(error), "an in-flight callback must refuse removal");
        Check(LifecycleProbesInstalled() && LvzProbeReaderProtectionInstalledForTest(),
              "an in-flight refusal must keep patches and handler");
        LvzProbeSetInFlightForTest(0);
        LvzProbeSetThreadForTest(GetCurrentThreadId() + 1u);
        bool threw = false;
        try {
            (void)DrainLifecycleProbeBatch();
        } catch (const std::exception&) {
            threw = true;
        }
        LvzProbeSetThreadForTest(GetCurrentThreadId());
        Check(threw, "drain from a foreign thread must throw");
        Check(RemoveLifecycleProbes(error), error.c_str());
        Check(!LvzProbeReaderProtectionInstalledForTest(), "the final removal must release the handler");
        Check(LvzProbeReaderHandlersRemovedForTest() == LvzProbeReaderHandlersAddedForTest(),
              "every acquired handler must be released");
    }
    std::string closeError;
    Host().OnPersisted(0);
    if (!Host().Close(&closeError))
        Host().Abort();  // the fixture deliberately exercises an incomplete session
    std::cout << "lifecycle probes: unconditional store replay, full-context equality, protected reads, "
                 "recycle pairing, rollback/ownership/pending safety passed\n";
    return 0;
}

int EmitAudit(const std::filesystem::path& directory) {
    using lvz::determinism::DrainLifecycleProbeBatch;
    using lvz::determinism::InstallLifecycleProbesForTest;
    using lvz::determinism::LifecycleProbeStatus;
    using lvz::determinism::LifecycleRecorder;
    using lvz::measurement::Host;
    std::filesystem::create_directories(directory);
    std::string error;
    Check(Host().Open(GetCurrentThreadId(), &error), "open measurement session");
    Pages patched = MakePages(false);
    Check(InstallLifecycleProbesForTest(patched.addresses, patched.continuations, patched.jumpTargets, error),
          error.c_str());
    Machine machine;
    PrepareBoard(machine, 2, 0, 2);
    PrepareZombie(machine, 0, 2, 0);
    PrepareZombie(machine, 1, 5, 0);
    lvz::determinism::SetSpawnBoundaryForTest(7, 9, 2, 42);
    Machine work = machine;
    (void)RunOne(patched.addresses[2], work, 0, 0, 0, 0, XmmSentinels());
    work = machine;
    (void)RunOne(patched.addresses[5], work, 0, 0, 0, 0, XmmSentinels());
    work = machine;
    work.zombie(0)[0xec] = 1;
    (void)RunOne(patched.addresses[6], work, 0, kBoardMarker, 0, static_cast<uint32_t>(-1), XmmSentinels());
    lvz::determinism::ClearSpawnBoundaryForTest();
    nlohmann::json probes = DrainLifecycleProbeBatch();
    Check(probes.size() >= 4, "artifact must contain phase, removal and recycle facts");
    std::string removalError;
    Check(lvz::determinism::RemoveLifecycleProbes(removalError), removalError.c_str());
    const nlohmann::json finalStatus = LifecycleProbeStatus();

    const std::string module = "fixture-recorder.dll";
    const std::string sha(64, 'b');
    lvz::determinism::LifecycleIdentity identity;
    identity.run_id = directory.filename().string();
    identity.branch_id = "fixture-branch";
    identity.build_module = module;
    identity.build_sha256 = sha;
    identity.session_id = 7;
    lvz::determinism::LifecycleRecorder recorder;
    recorder.Open(directory, identity);
    uint64_t sequence = 1;
    for (auto& event : probes) {
        event["capture_sequence"] = sequence++;
        recorder.Write(event);
    }
    const nlohmann::json initialization = {
        {"schema", "lvz.lifecycle-event.v1"},
        {"kind", "zombie_initialized"},
        {"capture_sequence", sequence++},
        {"version", nullptr},
        {"version_phase", "initialization"},
        {"engine_call_id", nullptr},
        {"invocation", {{"invocation_id", 1}, {"depth", 0}, {"parent_invocation_id", nullptr}}},
        {"entity", {{"id", 0x00020001}, {"slot", 1}, {"generation", 2}}},
        {"before_after", {{"before", nullptr},
                          {"after", {{"id", 0x00020001}, {"slot", 1}, {"generation", 2},
                                     {"row0", 0}, {"type", 16}, {"game_clock", 42}}}}},
        {"classification", {{"class", "initialization"}, {"cause", "unknown"}}},
        {"probe", {{"name", "zombie-initialize-exit"}, {"schema", "lvz.spawn.v1"},
                   {"sequence_domain", "lvz.measurement.capture-sequence"}}},
        {"complete", true},
    };
    recorder.Write(initialization);
    const nlohmann::json lifecycleCapability = {
        {"mode", "lvz.lifecycle-recording.v1"}, {"enabled", true},
        {"event_schema", "lvz.lifecycle-event.v1"}, {"envelope_schema", "lvz.lifecycle-record.v1"},
        {"receipt_schema", "lvz.lifecycle-close-receipt.v1"},
        {"sequence_domain", "lvz.measurement.capture-sequence"}, {"session_id", 7},
        {"probe", {{"name", "zombie-initialize-exit"}, {"schema", "lvz.spawn.v1"},
                   {"event_kind", "zombie_initialized"}}},
        {"build", {{"module", module}, {"sha256", sha}}},
        {"files", {{"events", "lifecycle-events.jsonl"}, {"close_receipt", "lifecycle-close-receipt.jsonl"}}},
        {"live_validated", false}};
    const nlohmann::json probesCapability = {
        {"mode", "lvz.lifecycle-probes.v1"}, {"enabled", true},
        {"record_schema", "lvz.lifecycle-event.v2"},
        {"event_schemas", {"lvz.lifecycle-event.v2"}},
        {"probe_set", {"zombie-phase-store", "zombie-removal-marked", "zombie-slot-recycle"}},
        {"session_id", 7},
        {"build", {{"module", module}, {"sha256", sha}}},
        {"sites", finalStatus.at("sites")},
        {"patch_windows_evidence", "docs/issue111-patch-windows.json"},
        {"probe_counters", finalStatus.at("counters")},
        {"healthy", finalStatus.at("healthy")},
        {"pending_candidate", finalStatus.at("pending_candidate")},
        {"active", finalStatus.at("active")},
        {"installed", finalStatus.at("installed")},
        {"live_validated", false}};
    {
        std::ofstream manifest(directory / "manifest.json", std::ios::out | std::ios::binary | std::ios::trunc);
        manifest << nlohmann::json{{"schema", "lvz.audit.v1"}, {"target", "lifecycle-probes-fixture"},
                                   {"lifecycle_recording", lifecycleCapability},
                                   {"lifecycle_probes", probesCapability}}.dump(2) << '\n';
        if (!manifest) throw std::runtime_error("cannot write the fixture audit manifest");
    }
    {
        std::ofstream auditEvents(directory / "events.jsonl", std::ios::out | std::ios::binary | std::ios::trunc);
        auditEvents << nlohmann::json{{"schema", "lvz.audit.v1"}, {"seq", 1},
                                      {"kind", "lifecycle_probes_closed"},
                                      {"payload", finalStatus}}.dump() << '\n';
        if (!auditEvents) throw std::runtime_error("cannot write the fixture audit events stream");
    }
    const uint64_t records = recorder.Count() + 1;
    const nlohmann::json counters = {
        {"initialization", {{"captured", 1}, {"delivered", 1}, {"persisted", 1}, {"overflow", 0},
                            {"wrong_thread", 0}, {"nesting_mismatch", 0}, {"incomplete_events", 0}}},
        {"probes", finalStatus.at("counters")},
        {"total", {{"persisted", records}, {"records", records}, {"bytes", 0}}}};
    recorder.Finish(true, counters, finalStatus, true);
    std::string closeError;
    Host().OnPersisted(records);
    (void)Host().Close(&closeError);
    std::cout << "lifecycle probes artifact: mixed v1/v2 audit written to " << directory.string() << '\n';
    return 0;
}
}  // namespace

int main(int argc, char** argv) {
    try {
        if (argc >= 3 && std::string(argv[1]) == "--emit-audit")
            return EmitAudit(argv[2]);
        return RunTests();
    } catch (const std::exception& exception) {
        std::cerr << exception.what() << '\n';
        return 1;
    }
}
