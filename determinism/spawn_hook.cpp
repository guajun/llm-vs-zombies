#include "spawn_hook.hpp"
#include "measurement.hpp"
#include "memory.hpp"
#include "model.hpp"
#include <minhook/MinHook.h>
#include <array>
#include <atomic>
#include <cstring>
#include <stdexcept>

// Separate, externally named symbols keep the naked x86 shims inspectable.
extern "C" {
void* lvzSpawnEntryTrampoline = nullptr;
void* lvzSpawnExitTrampoline = nullptr;
void __cdecl LvzSpawnEnter(void* frame) noexcept;
void __cdecl LvzSpawnLeave(void* frame) noexcept;

__attribute__((naked)) void LvzSpawnEntryShim() {
    __asm__ volatile(
        "pushfl\n\tpushal\n\tmovl %esp, %eax\n\t"
        "subl $528, %esp\n\tandl $-16, %esp\n\t"
        "movl %eax, 512(%esp)\n\tfxsave (%esp)\n\t"
        "cld\n\tsubl $12, %esp\n\tpushl %eax\n\tcall _LvzSpawnEnter\n\taddl $16, %esp\n\t"
        "fxrstor (%esp)\n\tmovl 512(%esp), %esp\n\t"
        "popal\n\tpopfl\n\tjmp *_lvzSpawnEntryTrampoline\n\t");
}
__attribute__((naked)) void LvzSpawnExitShim() {
    __asm__ volatile(
        "pushfl\n\tpushal\n\tmovl %esp, %eax\n\t"
        "subl $528, %esp\n\tandl $-16, %esp\n\t"
        "movl %eax, 512(%esp)\n\tfxsave (%esp)\n\t"
        "cld\n\tsubl $12, %esp\n\tpushl %eax\n\tcall _LvzSpawnLeave\n\taddl $16, %esp\n\t"
        "fxrstor (%esp)\n\tmovl 512(%esp), %esp\n\t"
        "popal\n\tpopfl\n\tjmp *_lvzSpawnExitTrampoline\n\t");
}
}

namespace lvz::determinism {
namespace {
constexpr uintptr_t kEntry = 0x522580, kExit = 0x524035, kMt = 0x75a910;
constexpr size_t kCapacity = 1024, kDepthLimit = 32;
struct SavedRegisters {
    uint32_t edi, esi, ebp, espBeforePushad, ebx, edx, ecx, eax, eflags;
};
static_assert(sizeof(SavedRegisters)==36);
struct Boundary { uint64_t tick=0, revision=0; uint32_t segment=0; bool valid=false; uint64_t engineCallId=0; };
struct EntryData {
    uintptr_t zombie=0;
    uint32_t callerRva=0, generationId=0, parentId=0;
    int32_t row=0, type=0, wave=0;
    uint8_t variant=0;
    bool parentPresent=false, parentReadable=false, readable=false;
    uint32_t gameClock=0;
    uint64_t invocationId=0, parentInvocationId=0;
    uint32_t depth=0;
    MtState mt{};
    Boundary boundary{};
};
struct Record {
    EntryData entry{};
    std::array<uint8_t,0x15c> zombie{};
    MtState mtAfter{};
    uint32_t gameClockAfter=0;
    uint64_t ordinal=0;
    uint64_t captureSequence=0;
};
std::array<EntryData,kDepthLimit> stack;
std::array<Record,kCapacity> queue;
size_t depth=0, count=0;
uint64_t ordinal=0, captured=0, invocationId=0;
std::atomic<uint64_t> wrongThread{0}, faults{0}, overflow{0};
DWORD gameThread=0;
bool installed=false;
Boundary currentBoundary;
uintptr_t entryAddress=0, exitAddress=0, mtAddress=kMt;
std::array<uint8_t,5> entryPatch{}, exitPatch{};

void RequireOwner() {
    if(!installed || GetCurrentThreadId()!=gameThread)
        throw std::runtime_error("Spawn hook API requires installed game-thread owner");
}
bool Copy(uintptr_t address, void* destination, size_t size) noexcept {
    if(!Accessible(address,size)) return false;
    std::memcpy(destination,reinterpret_cast<const void*>(address),size);
    return true;
}
template<class T> bool Copy(uintptr_t address,T& result) noexcept {
    return Copy(address,&result,sizeof(result));
}
uint32_t Word(const std::array<uint8_t,0x15c>& bytes,unsigned offset) {
    uint32_t word=0; std::memcpy(&word,bytes.data()+offset,4); return word;
}
uint32_t GameClock(uintptr_t zombie) noexcept {
    uint32_t board=0, tick=0;
    if(Copy(zombie+4,board)&&board) Copy(board+0x5568,tick);
    return tick;
}
void Enter(SavedRegisters* frame) noexcept {
    if(GetCurrentThreadId()!=gameThread) { ++wrongThread; lvz::measurement::Host().OnWrongThread(); return; }
    if(depth>=kDepthLimit) { ++depth; ++overflow; lvz::measurement::Host().OnOverflow(); return; }
    const uint32_t enterDepth=depth;
    auto& item=stack[depth++];
    item=EntryData{};
    // Invocation identity is entry-order and is separate from the event's
    // capture_sequence, which is allocated at the actual exit capture point.
    item.invocationId=invocationId++;
    item.depth=enterDepth;
    item.parentInvocationId=enterDepth?stack[enterDepth-1].invocationId:0;
    const auto* args=reinterpret_cast<const uint32_t*>(frame+1);
    // Original ABI: row in EAX; return address, this, type, variant, parent, wave on stack.
    item.zombie=args[1]; item.row=static_cast<int32_t>(frame->eax);
    item.type=static_cast<int32_t>(args[2]);item.variant=static_cast<uint8_t>(args[3]);
    item.wave=static_cast<int32_t>(args[5]); item.parentPresent=args[4]!=0;
    item.callerRva=args[0]>=0x400000&&args[0]<0x75e000?args[0]-0x400000:0;
    item.boundary=currentBoundary;
    item.parentReadable=!item.parentPresent || Copy(args[4]+0x158,item.parentId);
    item.gameClock=GameClock(item.zombie);
    item.readable=Copy(item.zombie+0x158,item.generationId) && Copy(mtAddress,item.mt)
        && item.mt.cursor<=625 && item.parentReadable;
    if(!item.readable) ++faults;
}
void Leave(SavedRegisters* frame) noexcept {
    if(GetCurrentThreadId()!=gameThread) { ++wrongThread; lvz::measurement::Host().OnWrongThread(); return; }
    if(!depth) { ++faults; lvz::measurement::Host().OnNestingMismatch(); return; }
    if(depth>kDepthLimit) { --depth; return; }
    const auto& item=stack[--depth];
    if(!item.readable || frame->edi!=item.zombie) { ++faults; lvz::measurement::Host().OnIncomplete(); return; }
    if(count==kCapacity) { ++overflow; lvz::measurement::Host().OnOverflow(); return; }
    auto& record=queue[count];
    record.entry=item; record.ordinal=ordinal++;
    // Event order is allocated at the actual exit capture point, so nested
    // exits are ordered by what was really observed first (child, then parent).
    record.captureSequence=lvz::measurement::Host().NextSequence();
    if(!record.captureSequence) { ++faults; return; }
    record.gameClockAfter=GameClock(item.zombie);
    if(!Copy(item.zombie,record.zombie) || !Copy(mtAddress,record.mtAfter)
        || record.mtAfter.cursor>625 || Word(record.zombie,0x158)!=item.generationId) {
        ++faults; lvz::measurement::Host().OnIncomplete(); return;
    }
    ++count;++captured; lvz::measurement::Host().OnCaptured();
}
Json Fields(const Record& record) {
    Json result=Json::object();
    auto words=[&](unsigned begin,unsigned end) {
        for(unsigned at=begin;at<end;at+=4) result[Hex(at)]=Word(record.zombie,at);
    };
    auto byte=[&](unsigned at) {result[Hex(at)]=record.zombie[at];};
    words(8,0x18);byte(0x18);words(0x1c,0x50);byte(0x50);byte(0x51);
    words(0x54,0x70);byte(0x70);words(0x74,0x78);byte(0x78);
    words(0x7c,0x88);byte(0x88);words(0x8c,0xb8);
    for(unsigned at=0xb8;at<0xc0;++at) byte(at);
    words(0xc0,0xec);byte(0xec);words(0xf0,0x104);byte(0x104);
    words(0x108,0x14c);byte(0x14c);words(0x150,0x158);
    return result;
}
bool SamePatch(uintptr_t address,const std::array<uint8_t,5>& expected) noexcept {
    std::array<uint8_t,5> actual{};
    return Copy(address,actual)&&actual==expected;
}
bool Install(uintptr_t entry,uintptr_t epilogue,uintptr_t mt,std::string& error,bool production) {
    if(installed) { error="Spawn hook is already installed";return false; }
    // Exit signature contains the complete unique normal return epilogue.
    static constexpr uint8_t start[]={0x55,0x8b,0xec,0x83,0xe4,0xf8};
    static constexpr uint8_t end[]={0x5f,0x5e,0x5b,0x8b,0xe5,0x5d,0xc2,0x14,0x00};
    static constexpr uint8_t originalStart[]={0x55,0x8b,0xec,0x83,0xe4,0xf8,0x83,0xec,0x14,0x53,0x56,0x8b,0xf0,0x8b,0x45,0x18,0x57,0x8b,0x7d,0x08};
    if(sizeof(void*)!=4 || !Accessible(entry,sizeof(originalStart)) || !Accessible(epilogue,sizeof(end))
        || std::memcmp(reinterpret_cast<void*>(entry),start,sizeof(start))
        || std::memcmp(reinterpret_cast<void*>(epilogue),end,sizeof(end))
        || (production && std::memcmp(reinterpret_cast<void*>(entry),originalStart,sizeof(originalStart)))
        || !Accessible(mt,sizeof(MtState))) {
        error="Unsupported ZombieInitialize bytes/MT layout";return false;
    }
    // The probe binds to an already-open measurement session; it never opens or
    // resets the shared host, so installing/removing one probe cannot wipe the
    // ledger or sequence of any other probe on the same domain.
    if(!lvz::measurement::Host().BoundTo(GetCurrentThreadId())) {
        error="Measurement session is not open on the game thread";return false;
    }
    // MinHook is shared with AvZ; this module never globally disables or uninitializes it.
    auto status=MH_Initialize();
    if(status!=MH_OK&&status!=MH_ERROR_ALREADY_INITIALIZED) {
        error="Cannot initialize MinHook";return false;
    }
    bool entryCreated=false,exitCreated=false;
    status=MH_CreateHook(reinterpret_cast<void*>(entry),reinterpret_cast<void*>(&LvzSpawnEntryShim),&lvzSpawnEntryTrampoline);
    if(status==MH_OK) {
        entryCreated=true;
        status=MH_CreateHook(reinterpret_cast<void*>(epilogue),reinterpret_cast<void*>(&LvzSpawnExitShim),&lvzSpawnExitTrampoline);
        exitCreated=status==MH_OK;
    }
    if(status==MH_OK) {
        gameThread=GetCurrentThreadId();entryAddress=entry;exitAddress=epilogue;mtAddress=mt;
        depth=count=0;ordinal=captured=0;invocationId=0;wrongThread=0;faults=0;overflow=0;currentBoundary={};
        status=MH_EnableHook(reinterpret_cast<void*>(epilogue));
        if(status==MH_OK) status=MH_EnableHook(reinterpret_cast<void*>(entry));
    }
    if(status!=MH_OK) {
        if(entryCreated) {MH_DisableHook(reinterpret_cast<void*>(entry));MH_RemoveHook(reinterpret_cast<void*>(entry));}
        if(exitCreated) {MH_DisableHook(reinterpret_cast<void*>(epilogue));MH_RemoveHook(reinterpret_cast<void*>(epilogue));}
        lvzSpawnEntryTrampoline=lvzSpawnExitTrampoline=nullptr;
        error=std::string("Spawn hook installation failed: ")+MH_StatusToString(status);return false;
    }
    Copy(entry,entryPatch);Copy(epilogue,exitPatch);installed=true;error.clear();return true;
}
Json LegacyEvent(const Record& record) {
    const auto& input=record.entry;
    Json boundary=nullptr;
    if(input.boundary.valid) boundary={{"tick",input.boundary.tick},{"revision",input.boundary.revision},{"segment",input.boundary.segment}};
    return {{"schema","lvz.spawn.v1"},{"kind","zombie_initialized"},
        {"phase","zombie_initialize_exit"},{"ordinal",record.ordinal},{"boundary",boundary},
        {"engine_call_id",input.boundary.engineCallId?Json(input.boundary.engineCallId):Json(nullptr)},
        {"id",Word(record.zombie,0x158)},{"slot",Word(record.zombie,0x158)&0xffffu},
        {"generation",Word(record.zombie,0x158)>>16},{"caller_rva",input.callerRva},
        {"inputs",{{"row0",input.row},{"type",input.type},{"variant_byte",input.variant},
            {"wave_raw",input.wave},{"parent_id",input.parentPresent?Json(input.parentId):Json(nullptr)}}},
        {"initial",{{"row0",static_cast<int32_t>(Word(record.zombie,0x1c))},
            {"type",Word(record.zombie,0x24)},{"x_bits",Word(record.zombie,0x2c)},
            {"y_bits",Word(record.zombie,0x30)},{"speed_bits",Word(record.zombie,0x34)},
            {"variant",record.zombie[0x50]},{"raw_scalar_fields",Fields(record)}}},
        {"game_clock_before",input.gameClock},{"game_clock_after",record.gameClockAfter},
        {"global_mt_before",EncodeMt(input.mt)},{"global_mt_after",EncodeMt(record.mtAfter)}};
}
Json LifecycleEvent(const Record& record) {
    const auto& input=record.entry;
    const uint32_t id=Word(record.zombie,0x158);
    Json version=nullptr;
    uint64_t engineCallId=0;
    if(input.boundary.valid) {
        version={{"epoch",input.boundary.segment},{"tick",input.boundary.tick},{"revision",input.boundary.revision}};
        engineCallId=input.boundary.engineCallId;
    }
    Json invocation=nullptr;
    if(input.depth) invocation={{"depth",input.depth},{"invocation_id",input.invocationId},{"parent_invocation_id",input.parentInvocationId}};
    return {{"schema","lvz.lifecycle-event.v1"},{"kind","zombie_initialized"},
        {"capture_sequence",record.captureSequence},
        {"version",version},
        {"version_phase",input.boundary.valid?"controlled_boundary":"initialization"},
        {"engine_call_id",input.boundary.valid&&engineCallId?Json(engineCallId):Json(nullptr)},
        {"invocation",invocation},
        {"entity",{{"id",id},{"slot",id&0xffffu},{"generation",id>>16}}},
        {"before_after",{{"before",nullptr},{"after",{{"id",id},{"slot",id&0xffffu},
            {"generation",id>>16},{"row0",static_cast<int32_t>(Word(record.zombie,0x1c))},
            {"type",Word(record.zombie,0x24)},{"game_clock",record.gameClockAfter}}}}},
        {"classification",{{"class","initialization"},{"cause","unknown"}}},
        {"probe",{{"name","zombie-initialize-exit"},{"schema","lvz.spawn.v1"},
            {"sequence_domain","lvz.measurement.capture-sequence"}}},
        {"complete",true}};
}
}

bool InstallSpawnHook(std::string& error) {return Install(kEntry,kExit,kMt,error,true);}
#ifdef LVZ_SPAWN_HOOK_TESTING
bool InstallSpawnHookForTest(uintptr_t entry,uintptr_t epilogue,uintptr_t mt,std::string& error) {
    return Install(entry,epilogue,mt,error,false);
}
#endif
bool RemoveSpawnHook(std::string& error) {
    if(!installed) {error.clear();return true;}
    if(GetCurrentThreadId()!=gameThread || depth) {error="Remove requires idle owning game thread";return false;}
    if(!SamePatch(entryAddress,entryPatch)||!SamePatch(exitAddress,exitPatch)) {
        error="Hook ownership changed; refusing to overwrite another module";return false;
    }
    const auto entryStatus=MH_DisableHook(reinterpret_cast<void*>(entryAddress));
    const auto exitStatus=MH_DisableHook(reinterpret_cast<void*>(exitAddress));
    if(entryStatus!=MH_OK || exitStatus!=MH_OK) {
        error="Could not disable both hooks; keep runtime DLL loaded";return false;
    }
    MH_RemoveHook(reinterpret_cast<void*>(entryAddress));MH_RemoveHook(reinterpret_cast<void*>(exitAddress));
    lvzSpawnEntryTrampoline=lvzSpawnExitTrampoline=nullptr;installed=false;error.clear();return true;
}
void SetSpawnBoundary(uint64_t tick,uint64_t revision,uint32_t segment,uint64_t engineCallId) {
    RequireOwner();currentBoundary={tick,revision,segment,true,engineCallId};
}
void ClearSpawnBoundary() {RequireOwner();currentBoundary={};}
SpawnBatch DrainSpawnBatch() {
    RequireOwner();
    if(depth) throw std::runtime_error("Cannot drain while ZombieInitialize is running");
    SpawnBatch batch;
    batch.count=count;
    for(size_t index=0;index<count;++index) {
        const auto& record=queue[index];
        batch.legacy.push_back(LegacyEvent(record));
        batch.lifecycle.push_back(LifecycleEvent(record));
    }
    count=0;
    lvz::measurement::Host().OnDelivered(batch.count);
    return batch;
}
Json DrainSpawnEvents() {
    return DrainSpawnBatch().legacy;
}
Json SpawnHookStatus() {
    if(installed) RequireOwner();
    return {{"installed",installed},{"captured",captured},{"queued",count},
        {"wrong_thread_calls",wrongThread.load()},{"faults",faults.load()},{"overflow",overflow.load()},
        {"active_initializers",depth},
        {"healthy",wrongThread.load()==0&&faults.load()==0&&overflow.load()==0&&depth==0},
        {"phase","ZombieInitialize exit, before caller resumes"},
        {"original_game_live_validated",false},
        {"measurement",lvz::measurement::Host().Health()}};
}
}

extern "C" void __cdecl LvzSpawnEnter(void* frame) noexcept {
    const DWORD previousError=GetLastError();
    lvz::determinism::Enter(static_cast<lvz::determinism::SavedRegisters*>(frame));
    SetLastError(previousError);
}
extern "C" void __cdecl LvzSpawnLeave(void* frame) noexcept {
    const DWORD previousError=GetLastError();
    lvz::determinism::Leave(static_cast<lvz::determinism::SavedRegisters*>(frame));
    SetLastError(previousError);
}
