#include "determinism/spawn_hook.hpp"
#include "determinism/measurement.hpp"
#include "determinism/model.hpp"
#include <Windows.h>
#include <array>
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <thread>

// This fixture tests the ABI shims and MinHook lifecycle. It does not execute,
// replace, or claim to validate the original game's ZombieInitialize behavior.
extern "C" {
alignas(16) uint8_t fixtureZombie[0x15c]{};
alignas(16) uint8_t fixtureChild[0x15c]{};
alignas(16) uint8_t fixtureResult[560]{};
alignas(16) uint8_t fixtureCallerFx[512]{};
alignas(16) uint32_t fixtureXmm[4]={0x12345678,0x90abcdef,0xfedcba09,0x87654321};
lvz::determinism::MtState fixtureMt;
uint32_t fixtureType=0;
void SpawnFixtureEpilogue();
__attribute__((naked)) void SpawnFixture() {
    __asm__ volatile(
        ".byte 0x55,0x8b,0xec,0x83,0xe4,0xf8\n\t"
        "subl $20, %esp\n\tpushl %ebx\n\tpushl %esi\n\tmovl %eax, %esi\n\t"
        "movl 24(%ebp), %eax\n\tpushl %edi\n\tmovl 8(%ebp), %edi\n\t"
        "movl %eax, 0x6c(%edi)\n\tmovl %esi, 0x1c(%edi)\n\t"
        "movl 12(%ebp), %eax\n\tmovl %eax, 0x24(%edi)\n\t"
        "movb 16(%ebp), %cl\n\tmovb %cl, 0x50(%edi)\n\t"
        "movl $0x4447c000, 0x2c(%edi)\n\tmovl $0x43020000, 0x30(%edi)\n\t"
        "movl $0x3f000001, 0x34(%edi)\n\t"
        "incl _fixtureMt+2496\n\tcmpl $624, _fixtureMt+2496\n\tjbe 1f\n\t"
        "movl $0, _fixtureMt+2496\n\t1:\n\t"
        "cmpl $2, %eax\n\tje 2f\n\tcmpl $1, %eax\n\tje 3f\n\t"
        "movl $0x11112222, %eax\n\tjmp _SpawnFixtureEpilogue\n\t"
        "3:\n\tmovl $0x33334444, %eax\n\tstc\n\tjmp _SpawnFixtureEpilogue\n\t"
        "2:\n\tpushl $3\n\tpushl %edi\n\tpushl $1\n\tpushl $0\n\t"
        "pushl $_fixtureChild\n\tmovl $5, %eax\n\tcall _SpawnFixture\n\t"
        "movl $0x55556666, %eax\n\t"
        ".globl _SpawnFixtureEpilogue\n\t_SpawnFixtureEpilogue:\n\t"
        ".byte 0x5f,0x5e,0x5b,0x8b,0xe5,0x5d,0xc2,0x14,0x00\n\t");
}
__attribute__((naked)) void CallFixture() {
    __asm__ volatile(
        "pushfl\n\tpushal\n\tfxsave _fixtureCallerFx\n\t"
        "fninit\n\tfldpi\n\tfld1\n\t"
        "movdqu _fixtureXmm, %xmm0\n\tmovdqu _fixtureXmm, %xmm1\n\t"
        "movdqu _fixtureXmm, %xmm2\n\tmovdqu _fixtureXmm, %xmm3\n\t"
        "movdqu _fixtureXmm, %xmm4\n\tmovdqu _fixtureXmm, %xmm5\n\t"
        "movdqu _fixtureXmm, %xmm6\n\tmovdqu _fixtureXmm, %xmm7\n\t"
        "pushl $19\n\tpushl $0\n\tpushl $0xaabbcc01\n\tpushl _fixtureType\n\t"
        "pushl $_fixtureZombie\n\t"
        "movl $7, %eax\n\tmovl $0x11111111, %ebx\n\t"
        "movl $0x22222222, %ebp\n\tmovl $0x33333333, %ecx\n\t"
        "movl $0x44444444, %edx\n\tmovl $0x55555555, %esi\n\t"
        "movl $0x66666666, %edi\n\tpushl $0x247\n\tpopfl\n\t"
        "call _SpawnFixture\n\t"
        "pushfl\n\tpushal\n\tcld\n\tmovl %esp, %esi\n\t"
        "movl $_fixtureResult, %edi\n\tmovl $9, %ecx\n\trep movsl\n\t"
        "fxsave _fixtureResult+48\n\tpopal\n\tpopfl\n\t"
        "fxrstor _fixtureCallerFx\n\tpopal\n\tpopfl\n\tretl\n\t");
}
}
namespace {
void Check(bool condition,const char* message) {if(!condition) throw std::runtime_error(message);}
void Reset(uint32_t type) {
    std::memset(fixtureZombie,0,sizeof(fixtureZombie));
    std::memset(fixtureChild,0,sizeof(fixtureChild));
    std::memset(fixtureResult,0,sizeof(fixtureResult));
    uint32_t parentId=0x3e90000,childId=0x3ea0001;
    std::memcpy(fixtureZombie+0x158,&parentId,4);std::memcpy(fixtureChild+0x158,&childId,4);
    fixtureMt=lvz::determinism::SeedMt(5489);fixtureMt.cursor=100;fixtureType=type;
}
void CompareRegisters(const std::array<uint8_t,560>& baseline) {
    // Ignore saved ESP itself: the two call sites may have different stack
    // addresses, but the original callee cleanup is independently exercised.
    for(size_t index=0;index<36;++index) {
        if(index>=12&&index<16) continue;
        Check(baseline[index]==fixtureResult[index],"GPR/EFLAGS preservation failed");
    }
    // FXSAVE records the last x87 instruction pointer as well as registers;
    // all instructions here run at the same harness address in both passes.
    for(size_t index=48;index<560;++index)
        Check(baseline[index]==fixtureResult[index],"x87/SSE environment preservation failed");
}
}
int main() {
    using namespace lvz::determinism;
    std::string error;
    try {
        // --- #111 stage B: lifecycle projection + shared capture_sequence (clean session) ---
        Check(lvz::measurement::Host().Open(GetCurrentThreadId()),"Open measurement session");
        Check(InstallSpawnHookForTest(reinterpret_cast<uintptr_t>(&SpawnFixture),
            reinterpret_cast<uintptr_t>(&SpawnFixtureEpilogue),reinterpret_cast<uintptr_t>(&fixtureMt),error),error.c_str());
        SetSpawnBoundary(42,7,3,0x200000002ull);
        Reset(2);CallFixture();
        {
            auto batch=DrainSpawnBatch();
            Check(batch.count==2 && batch.legacy.size()==2 && batch.lifecycle.size()==2,
                "Lifecycle/legacy projection must drain the same records");
            const auto& child=batch.lifecycle[0];
            const auto& parent=batch.lifecycle[1];
            // Actual capture order is exit order: the nested child exits before
            // its parent, so the child receives the smaller capture_sequence.
            Check(child["capture_sequence"]==1 && parent["capture_sequence"]==2,
                "capture_sequence must reflect actual exit order");
            // Every event carries an invocation_id; only the top-level parent
            // reference is null. The parent entered first (id 1), child id 2.
            Check(parent["invocation"]["invocation_id"]==1 && parent["invocation"]["depth"]==0
                && parent["invocation"]["parent_invocation_id"].is_null(),
                "Top-level event must carry invocation_id with a null parent reference");
            Check(child["invocation"]["invocation_id"]==2 && child["invocation"]["depth"]==1
                && child["invocation"]["parent_invocation_id"]==1,
                "Nested event must resolve its parent by invocation_id");
            Check(child["version"]["epoch"]==3 && child["version"]["tick"]==42
                && child["version"]["revision"]==7 && child["version_phase"]=="controlled_boundary",
                "Lifecycle event must bind the controlled boundary version");
            Check(child["engine_call_id"]==0x200000002ull,"Lifecycle event must keep the 64-bit original call id");
            Check(child["entity"]["id"]==0x3ea0001u && child["entity"]["slot"]==1
                && child["entity"]["generation"]==1002,"Lifecycle event must identify slot+generation");
            Check(child["classification"]["class"]=="initialization"
                && child["classification"]["cause"]=="unknown","Lifecycle classification must stay neutral");
            Check(child["complete"]==true && child["probe"]["name"]=="zombie-initialize-exit",
                "Lifecycle event must declare completeness and probe identity");
        }
        Reset(0);CallFixture();
        {
            auto batch=DrainSpawnBatch();
            Check(batch.lifecycle[0]["capture_sequence"]==3 && batch.lifecycle[0]["invocation"]["invocation_id"]==3,
                "capture_sequence must not reset on drain and invocation_id must advance");
        }
        // Cross-type interleaving: another probe allocates in the same domains.
        Check(lvz::measurement::Host().NextSequence()==4,"Another probe must interleave in the shared sequence domain");
        Check(lvz::measurement::Host().NextInvocationId()==4,"Another probe must interleave in the invocation domain");
        Reset(0);CallFixture();
        {
            auto batch=DrainSpawnBatch();
            Check(batch.lifecycle[0]["capture_sequence"]==5 && batch.lifecycle[0]["invocation"]["invocation_id"]==5,
                "Spawn after another probe stays ordered in both domains");
        }
        ClearSpawnBoundary();Reset(0);CallFixture();
        {
            auto batch=DrainSpawnBatch();
            Check(batch.lifecycle[0]["capture_sequence"]==6
                && batch.lifecycle[0]["version"].is_null()
                && batch.lifecycle[0]["version_phase"]=="initialization"
                && batch.lifecycle[0]["engine_call_id"].is_null(),
                "Initialization lifecycle event must not forge a controlled identity");
        }
        // Reinstall within the same session must not reset invocation identity:
        // a child's parent reference must stay resolvable after the probe is
        // removed and reinstalled.
        Check(RemoveSpawnHook(error),error.c_str());
        Check(InstallSpawnHookForTest(reinterpret_cast<uintptr_t>(&SpawnFixture),
            reinterpret_cast<uintptr_t>(&SpawnFixtureEpilogue),reinterpret_cast<uintptr_t>(&fixtureMt),error),error.c_str());
        SetSpawnBoundary(42,7,3,0x200000002ull);
        Reset(2);CallFixture();
        {
            auto batch=DrainSpawnBatch();
            const auto& child=batch.lifecycle[0];
            const auto& parent=batch.lifecycle[1];
            Check(child["capture_sequence"]==7 && parent["capture_sequence"]==8,
                "Reinstall must not reset the shared capture_sequence domain");
            Check(child["invocation"]["invocation_id"]==8 && child["invocation"]["parent_invocation_id"]==7
                && parent["invocation"]["invocation_id"]==7,
                "Reinstall must not reset invocation_id scope");
        }
        {
            auto health=SpawnHookStatus()["measurement"];
            Check(health["captured"]==7 && health["delivered"]==7 && health["persisted"]==0,
                "Measurement ledger must count captured/delivered but leave persisted to the adapter");
            Check(health["overflow"]==0 && health["wrong_thread"]==0 && health["nesting_mismatch"]==0
                && health["incomplete_events"]==0 && health["close_receipt_present"]==false,
                "Healthy measurement ledger must have no faults and no premature close receipt");
            lvz::measurement::Host().OnPersisted(7);
            Check(SpawnHookStatus()["measurement"]["persisted"]==7,"Persisted must be recorded by the adapter");
            std::string closeError;
            Check(lvz::measurement::Host().Close(&closeError),"Balanced session must close");
            Check(lvz::measurement::Host().CloseReceiptPresent(),"Close receipt must be present");
        }
        Reset(0);CallFixture();
        Check(SpawnHookStatus()["queued"]==0 && SpawnHookStatus()["measurement"]["captured"]==7,
            "Post-close spawn must be refused and not captured");
        Check(RemoveSpawnHook(error),error.c_str());
        // --- legacy spawn ABI tests (fresh session) ---
        Check(lvz::measurement::Host().Open(GetCurrentThreadId()),"Reopen measurement session");
        Check(!InstallSpawnHookForTest(0,0,0,error),"Unknown code should be rejected");
        for(uint32_t type=0;type<3;++type) {
            Reset(type);CallFixture();
            std::array<uint8_t,560> baseline;std::memcpy(baseline.data(),fixtureResult,baseline.size());
            std::array<uint8_t,0x15c> original;std::memcpy(original.data(),fixtureZombie,original.size());
            Check(InstallSpawnHookForTest(reinterpret_cast<uintptr_t>(&SpawnFixture),
                reinterpret_cast<uintptr_t>(&SpawnFixtureEpilogue),reinterpret_cast<uintptr_t>(&fixtureMt),error),error.c_str());
            SetSpawnBoundary(123,4,2,0x100000001ull);
            Reset(type);CallFixture();CompareRegisters(baseline);
            Check(std::memcmp(original.data(),fixtureZombie,original.size())==0,"Hook changed original object writes");
            auto events=DrainSpawnEvents();
            Check(events.size()==(type==2?2u:1u),"A normal/nested return was missed");
            const auto& event=events.back();
            Check(event["inputs"]["row0"]==7&&event["inputs"]["wave_raw"]==19,"Input ABI mismatch");
            Check(event["inputs"]["variant_byte"]==1,"Variant high padding bytes leaked");
            Check(event["id"]==0x3e90000&&event["generation"]==1001,"Generation identity lost");
            Check(event["initial"]["speed_bits"]==0x3f000001,"Float bit pattern rounded");
            Check(event["global_mt_before"]["cursor"]==100,"Entry RNG cursor lost");
            Check(event["global_mt_after"]["cursor"]==(type==2?102:101),"Exit RNG cursor lost");
            Check(event["boundary"]["tick"]==123,"Boundary metadata lost");
            for(const auto& item:events)Check(item["engine_call_id"]==0x100000001ull,"Nested spawn lost 64-bit original call ID");
            Check(SpawnHookStatus()["healthy"]==true,"Hook health failed");
            ClearSpawnBoundary();Reset(0);CallFixture();
            auto initialization=DrainSpawnEvents();
            Check(initialization.size()==1&&initialization[0]["boundary"].is_null(),
                "Initialization spawn retained a stale controlled boundary");
            Check(initialization[0]["engine_call_id"].is_null(),"Initialization spawn retained original call ID");
            Check(RemoveSpawnHook(error),error.c_str());
        }
        Check(InstallSpawnHookForTest(reinterpret_cast<uintptr_t>(&SpawnFixture),
            reinterpret_cast<uintptr_t>(&SpawnFixtureEpilogue),reinterpret_cast<uintptr_t>(&fixtureMt),error),error.c_str());
        Reset(0);
        std::thread other([]{CallFixture();});other.join();
        Check(SpawnHookStatus()["healthy"]==false,"Foreign thread was silently accepted");
        Check(DrainSpawnEvents().empty(),"Foreign thread polluted game-thread queue");
        Check(RemoveSpawnHook(error),error.c_str());
        Check(InstallSpawnHookForTest(reinterpret_cast<uintptr_t>(&SpawnFixture),
            reinterpret_cast<uintptr_t>(&SpawnFixtureEpilogue),reinterpret_cast<uintptr_t>(&fixtureMt),error),error.c_str());
        Reset(0);
        for(unsigned i=0;i<1026;++i) CallFixture();
        Check(SpawnHookStatus()["overflow"]==2,"Queue overflow was not reported exactly");
        Check(SpawnHookStatus()["healthy"]==false,"Queue overflow silently lost strict events");
        Check(DrainSpawnEvents().size()==1024,"Queue corrupted on overflow");
        // Another hook owner must never be overwritten during removal.
        auto* target=reinterpret_cast<uint8_t*>(&SpawnFixture);
        DWORD oldProtection=0;Check(VirtualProtect(target,5,PAGE_EXECUTE_READWRITE,&oldProtection)!=0,"Cannot test ownership");
        uint8_t saved=target[4];target[4]^=1;
        Check(!RemoveSpawnHook(error),"Foreign hook ownership was overwritten");
        target[4]=saved;DWORD ignored=0;VirtualProtect(target,5,oldProtection,&ignored);
        FlushInstructionCache(GetCurrentProcess(),target,5);
        Check(RemoveSpawnHook(error),error.c_str());
        std::cout<<"spawn ABI: signature refusal, branches/nesting, GPR/EFLAGS/x87/XMM preservation, generation/RNG capture, thread/overflow/ownership rejection passed\n";
        return 0;
    } catch(const std::exception& exception) {
        std::cerr<<exception.what()<<'\n';RemoveSpawnHook(error);return 1;
    }
}
