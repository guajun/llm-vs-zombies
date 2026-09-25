#include "determinism/lifecycle_probes.hpp"
#include "determinism/measurement.hpp"
#include <Windows.h>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <string>

// Offline fixture for the issue #111 exact-store probes. It executes *real
// patched machine code*: copies of the production patch windows are placed in
// an executable page, the production installer patches them, and naked callers
// set registers/flags/x87, call the patched code and verify the handler facts,
// the original store semantics and context preservation. No game is loaded.

namespace {
void Check(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

constexpr size_t kSlot = 0x15c;
struct Machine {
    alignas(16) uint8_t zombie[kSlot]{};
    alignas(16) uint8_t board[0x200]{};
};

void PrepareMachine(Machine& machine) {
    std::memset(&machine, 0, sizeof(machine));
    const uint32_t id = (2u << 16) | 0u;  // slot 0, generation 2
    std::memcpy(machine.zombie + 0x158, &id, 4);
    const uint32_t board = static_cast<uint32_t>(reinterpret_cast<uintptr_t>(machine.board));
    std::memcpy(machine.zombie + 4, &board, 4);
    const uint32_t block = static_cast<uint32_t>(reinterpret_cast<uintptr_t>(machine.zombie));
    std::memcpy(machine.board + 0x90, &block, 4);       // zombie pool block
    const uint32_t used = 1, capacity = 8;
    std::memcpy(machine.board + 0x94, &used, 4);
    std::memcpy(machine.board + 0x98, &capacity, 4);
}

struct SiteCopies {
    void* page = nullptr;
    uintptr_t addresses[8]{};
    uintptr_t continuations[8]{};
    uintptr_t jumpTargets[8]{};
};

const std::array<std::array<uint8_t, 7>, 8> kTestWindows = {{
    {0xc7,0x47,0x28,0x01,0x00,0x00,0x00},  // phase-playdeathanim (EDI imm 1)
    {0xc7,0x46,0x28,0x02,0x00,0x00,0x00},  // phase-applyburn (ESI imm 2)
    {0xc7,0x47,0x28,0x03,0x00,0x00,0x00},  // phase-mowdown (EDI imm 3)
    {0xc7,0x47,0x28,0x01,0x00,0x00,0x00},  // phase-catapult (EDI imm 1)
    {0x89,0x5f,0x28,0xd9,0x47,0x2c,0x00},  // phase-zamboni (mov ebx + flds)
    {0xc6,0x87,0xec,0x00,0x00,0x00,0x01},  // removal-mdead
    {0x38,0x9f,0xec,0x00,0x00,0x00,0x00},  // recycle-guard cmp
    {0xe9,0x30,0xff,0xff,0xff,0x00,0x00},  // recycle-commit jmp
}};
const std::array<uint8_t, 8> kWindowSizes = {7,7,7,7,6,7,6,5};

SiteCopies MakeCopies() {
    SiteCopies copies;
    copies.page = VirtualAlloc(nullptr, 4096, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
    if (!copies.page) throw std::runtime_error("Cannot allocate executable test page");
    auto* base = static_cast<uint8_t*>(copies.page);
    for (size_t index = 0; index < 8; ++index) {
        auto* slot = base + index * 64;
        std::memcpy(slot, kTestWindows[index].data(), kWindowSizes[index]);
        slot[kWindowSizes[index]] = 0xc3;  // ret so continuations end cleanly
        copies.addresses[index] = reinterpret_cast<uintptr_t>(slot);
        copies.continuations[index] = reinterpret_cast<uintptr_t>(slot + kWindowSizes[index]);
        copies.jumpTargets[index] = reinterpret_cast<uintptr_t>(slot + kWindowSizes[index]);
    }
    return copies;
}

struct Context {
    alignas(16) uint8_t bytes[576]{};
    uint32_t eflags() const {
        uint32_t value = 0;
        std::memcpy(&value, bytes + 32, 4);
        return value;
    }
    uint32_t reg(int index) const { return *reinterpret_cast<const uint32_t*>(bytes + index * 4); }
};

extern "C" void CaptureContext(Context* context);
__attribute__((naked)) void CaptureContext(Context* context) {
    __asm__ volatile(
        "movl 4(%esp), %edx\n\t"
        "movl %edi, 0(%edx)\n\tmovl %esi, 4(%edx)\n\tmovl %ebp, 8(%edx)\n\t"
        "movl %ebx, 12(%edx)\n\tmovl %ecx, 16(%edx)\n\tmovl %eax, 20(%edx)\n\t"
        "pushfl\n\tpopl %eax\n\tmovl %eax, 32(%edx)\n\t"
        "fxsave 48(%edx)\n\tret\n\t");
}

extern "C" void CallPatched(void* fn, uint32_t edi, uint32_t esi, uint32_t ebx, Context* after);
// Saves callee-saved registers, sets EDI/ESI/EBX and known flags/x87, calls
// the patched window, then saves the resulting context.
__attribute__((naked)) void CallPatched(void* fn, uint32_t edi, uint32_t esi, uint32_t ebx, Context* after) {
    __asm__ volatile(
        "pushl %ebx\n\tpushl %esi\n\tpushl %edi\n\t"
        "movl 16(%esp), %eax\n\t"   // fn
        "movl 20(%esp), %edi\n\t"
        "movl 24(%esp), %esi\n\t"
        "movl 28(%esp), %ebx\n\t"
        "fninit\n\tfld1\n\tfldpi\n\t"  // ST0=pi, ST1=1
        "pushl $0x246\n\tpopfl\n\t"
        "call *%eax\n\t"
        "movl 32(%esp), %edx\n\t"   // after context
        "movl %edi, 0(%edx)\n\tmovl %esi, 4(%edx)\n\tmovl %ebp, 8(%edx)\n\t"
        "movl %ebx, 12(%edx)\n\tmovl %ecx, 16(%edx)\n\tmovl %eax, 20(%edx)\n\t"
        "pushfl\n\tpopl %eax\n\tmovl %eax, 32(%edx)\n\t"
        "fxsave 48(%edx)\n\t"
        "popl %edi\n\tpopl %esi\n\tpopl %ebx\n\tret\n\t");
}

uint32_t Read32(const void* address) {
    uint32_t value = 0;
    std::memcpy(&value, address, 4);
    return value;
}

nlohmann::json EventOfKind(const nlohmann::json& batch, const char* kind) {
    for (const auto& item : batch)
        if (item.value("kind", "") == kind) return item;
    throw std::runtime_error(std::string("missing event kind: ") + kind);
}
}

int main() {
    using lvz::determinism::LifecycleProbesInstalled;
    using lvz::measurement::Host;
    try {
        std::string error;
        Check(Host().Open(GetCurrentThreadId(), &error), "open measurement session");
        Machine machine;
        PrepareMachine(machine);
        SiteCopies copies = MakeCopies();
        Check(lvz::determinism::InstallLifecycleProbesForTest(copies.addresses, copies.continuations,
                                                             copies.jumpTargets, error), error.c_str());
        Check(LifecycleProbesInstalled(), "probes must be installed");
        // The installed patch must really be a jump, not the original bytes.
        Check(*reinterpret_cast<const uint8_t*>(copies.addresses[2]) == 0xe9, "patched byte must be a jmp");

        Context after;
        CallPatched(reinterpret_cast<void*>(copies.addresses[2]), static_cast<uint32_t>(reinterpret_cast<uintptr_t>(machine.zombie)),
                    0x11111111u, 0x22222222u, &after);
        Check(Read32(machine.zombie + 0x28) == 3u, "the original store must execute exactly once");
        Check(after.reg(0) == static_cast<uint32_t>(reinterpret_cast<uintptr_t>(machine.zombie)),
              "shim must preserve EDI");
        Check(after.reg(1) == 0x11111111u && after.reg(3) == 0x22222222u, "shim must preserve ESI/EBX");
        Check((after.eflags() & 0x246u) == 0x246u, "shim must preserve flags");

        // Removal mDead store.
        CallPatched(reinterpret_cast<void*>(copies.addresses[5]), static_cast<uint32_t>(reinterpret_cast<uintptr_t>(machine.zombie)),
                    0x33333333u, 0x44444444u, &after);
        Check(machine.zombie[0xec] == 1, "mDead store must execute once");

        auto logical_st0 = [](const Context& context) {
            // FXSAVE stores the x87 register file with logical ST0 at offset
            // 32 (the TOP tag only describes the stack ordering metadata).
            const uint8_t* entry = context.bytes + 48 + 32;
            uint64_t mantissa = 0;
            uint16_t signExponent = 0;
            std::memcpy(&mantissa, entry, 8);
            std::memcpy(&signExponent, entry + 8, 2);
            const int exponent = (signExponent & 0x7fff) - 16383 - 63;
            const double value = std::ldexp(static_cast<double>(mantissa), exponent);
            return (signExponent & 0x8000) ? -value : value;
        };
        {
            void* plain = VirtualAlloc(nullptr, 64, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
            static_cast<uint8_t*>(plain)[0] = 0xc3;
            Context control;
            CallPatched(plain, 0xdeadbeefu, 0, 0, &control);
            Check(std::abs(logical_st0(control) - 3.14159265358979) < 1e-9,
                  "the test caller must establish a known x87 stack");
        }
        Check(std::abs(logical_st0(after) - 3.14159265358979) < 1e-9,
              "a store-only shim must preserve the x87 stack");

        // Zamboni: store + flds. The logical ST0 (FXSAVE register area at
        // physical index TOP) must gain the loaded value while the pre-call
        // x87 state is preserved underneath.
        machine.zombie[0x28] = 0;
        float loadTarget = 1.5f;
        std::memcpy(machine.zombie + 0x2c, &loadTarget, 4);
        CallPatched(reinterpret_cast<void*>(copies.addresses[4]), static_cast<uint32_t>(reinterpret_cast<uintptr_t>(machine.zombie)),
                    0x55555555u, 7u, &after);
        Check(Read32(machine.zombie + 0x28) == 7u, "zamboni register store must execute");
        Check(std::abs(logical_st0(after) - 1.5) < 1e-9,
              "flds effect must survive the shim at the logical top of stack");

        // Recycle guard (live -> no pending, dead -> candidate) and commit pair.
        machine.zombie[0xec] = 0;
        CallPatched(reinterpret_cast<void*>(copies.addresses[6]), static_cast<uint32_t>(reinterpret_cast<uintptr_t>(machine.zombie)),
                    0, 0, &after);
        machine.zombie[0xec] = 1;
        const uint32_t freeHead = 9, poolCount = 0;
        std::memcpy(machine.board + 0x9c, &freeHead, 4);
        std::memcpy(machine.board + 0xa0, &poolCount, 4);
        CallPatched(reinterpret_cast<void*>(copies.addresses[6]), static_cast<uint32_t>(reinterpret_cast<uintptr_t>(machine.zombie)),
                    0, 0, &after);
        CallPatched(reinterpret_cast<void*>(copies.addresses[7]), static_cast<uint32_t>(reinterpret_cast<uintptr_t>(machine.zombie)),
                    static_cast<uint32_t>(reinterpret_cast<uintptr_t>(machine.board)), 0, &after);

        const auto batch = lvz::determinism::DrainLifecycleProbeBatch();
        const auto phase = EventOfKind(batch, "zombie_phase_transition");
        Check(phase.at("phase").at("before") == 0 && phase.at("phase").at("after") == 3,
              "phase transition must record the real before/after values");
        const auto removal = EventOfKind(batch, "zombie_removal_marked");
        Check(removal.at("removal").at("before") == 0 && removal.at("removal").at("after") == 1,
              "removal must record the mDead transition");
        const auto candidate = EventOfKind(batch, "zombie_slot_recycle_candidate");
        const auto commit = EventOfKind(batch, "zombie_slot_recycle_commit");
        Check(candidate.at("recycle").at("state") == "candidate", "guard must emit a candidate");
        Check(commit.at("recycle").at("state") == "committed"
                  && commit.at("recycle").at("free_head_after") == 9
                  && commit.at("recycle").at("count_after") == 0,
              "commit must record the post-free pool head/count");
        Check(commit.at("recycle").at("candidate_capture_sequence") == candidate.at("capture_sequence"),
              "commit must pair the candidate capture sequence");
        const auto status = lvz::determinism::LifecycleProbeStatus();
        Check(status.at("counters").at("unmatched_commits") == 0
                  && status.at("counters").at("pair_mismatch") == 0
                  && status.at("counters").at("overwritten_pending") == 0,
              "pairing must be healthy in the fixture");

        Check(lvz::determinism::RemoveLifecycleProbes(error), error.c_str());
        Check(!LifecycleProbesInstalled(), "probes must be removed");
        Check(*reinterpret_cast<const uint8_t*>(copies.addresses[2]) == 0xc7,
              "removal must restore the original store bytes");

        // A signature mismatch must be refused without patching anything.
        uint8_t* broken = static_cast<uint8_t*>(copies.page) + 7 * 64;
        broken[0] = 0x90;
        std::string mismatch;
        SiteCopies again = MakeCopies();
        std::memcpy(reinterpret_cast<void*>(again.addresses[7]), broken, 1);
        Check(!lvz::determinism::InstallLifecycleProbesForTest(again.addresses, again.continuations,
                                                               again.jumpTargets, mismatch),
              "a changed window signature must be refused");

        std::string closeError;
        Host().OnPersisted(5);
        Check(Host().Close(&closeError), "close measurement session");
        Check(Host().Commit(&closeError), "commit measurement session");
        std::cout << "lifecycle probes: real patched stores, x87/flags/register preservation, "
                     "recycle pairing and signature refusal passed\n";
        return 0;
    } catch (const std::exception& exception) {
        std::cerr << exception.what() << '\n';
        return 1;
    }
}
