#include "foley_trace.hpp"
#include "memory.hpp"
#include <minhook/MinHook.h>
#include <algorithm>
#include <array>
#include <atomic>
#include <fstream>
#include <limits>

extern "C" {
void* lvzFoleyTrampolines[12]{};
void __cdecl LvzFoleyObserve(unsigned,void*) noexcept;
#define LVZ_FOLEY_SHIM(N,OFFSET) __attribute__((naked)) void LvzFoleyShim##N(){__asm__ volatile( \
    "pushfl\n\tpushal\n\tmovl %esp,%eax\n\t" \
    "subl $528,%esp\n\tandl $-16,%esp\n\tmovl %eax,512(%esp)\n\tfxsave (%esp)\n\t" \
    "cld\n\tsubl $8,%esp\n\tpushl %eax\n\tpushl $" #N "\n\tcall _LvzFoleyObserve\n\taddl $16,%esp\n\t" \
    "fxrstor (%esp)\n\tmovl 512(%esp),%esp\n\tpopal\n\tpopfl\n\tjmp *_lvzFoleyTrampolines+" #OFFSET "\n\t");}
LVZ_FOLEY_SHIM(0,0) LVZ_FOLEY_SHIM(1,4) LVZ_FOLEY_SHIM(2,8) LVZ_FOLEY_SHIM(3,12)
LVZ_FOLEY_SHIM(4,16) LVZ_FOLEY_SHIM(5,20) LVZ_FOLEY_SHIM(6,24) LVZ_FOLEY_SHIM(7,28)
LVZ_FOLEY_SHIM(8,32) LVZ_FOLEY_SHIM(9,36) LVZ_FOLEY_SHIM(10,40) LVZ_FOLEY_SHIM(11,44)
#undef LVZ_FOLEY_SHIM
}
namespace lvz::determinism::foleytrace {
namespace {
using Json=nlohmann::json;
constexpr size_t Capacity=16384,DepthLimit=16,TypeLimit=110,SlotCount=8;
constexpr uintptr_t ImageBase=0x400000;
constexpr std::array<uintptr_t,12> OriginalSites={0x515020,0x514f9c,0x514fa5,0x515050,0x515188,0x51518d,0x5151b5,0x5150a4,0x5150c0,0x51522d,0x54b983,0x54b98a};
// These prefixes end at original instruction boundaries. All are checked before
// any patch is enabled, including adjacent original-call/return sites.
const std::array<std::vector<uint8_t>,12> Signatures={
    std::vector<uint8_t>{0x55,0x8b,0xec,0x83,0xe4,0xc0,0x83,0xec,0x34},
    {0x8b,0x0e,0x8b,0x01,0x8b,0x50,0x24},
    {0x84,0xc0,0x75,0x16,0x8b,0x0e},
    {0x83,0xc4,0x04,0x84,0xc0},
    {0xe8,0x73,0xa2,0x09,0x00},
    {0x8b,0x7c,0x84,0x18,0x8b,0x44,0x24,0x14},
    {0x8b,0xf0,0x85,0xf6,0x74,0x72},
    {0x5f,0x5e,0x5b,0x8b,0xe5},
    {0x5f,0x5e,0x5b,0x8b,0xe5},
    {0x5f,0x5e,0x5b,0x8b,0xe5},
    {0x83,0x86,0x84,0x04,0x00,0x00,0x01},
    {0x80,0xbe,0xcf,0x04,0x00,0x00,0x00}};
const std::array<void*,12> Shims={reinterpret_cast<void*>(&LvzFoleyShim0),reinterpret_cast<void*>(&LvzFoleyShim1),reinterpret_cast<void*>(&LvzFoleyShim2),reinterpret_cast<void*>(&LvzFoleyShim3),reinterpret_cast<void*>(&LvzFoleyShim4),reinterpret_cast<void*>(&LvzFoleyShim5),reinterpret_cast<void*>(&LvzFoleyShim6),reinterpret_cast<void*>(&LvzFoleyShim7),reinterpret_cast<void*>(&LvzFoleyShim8),reinterpret_cast<void*>(&LvzFoleyShim9),reinterpret_cast<void*>(&LvzFoleyShim10),reinterpret_cast<void*>(&LvzFoleyShim11)};
struct Registers{uint32_t edi,esi,ebp,savedEsp,ebx,edx,ecx,eax,flags;};
static_assert(sizeof(Registers)==36);
struct Context {uint64_t epoch=0,tick=0,revision=0,engineCall=0;unsigned phase=0;bool version=false;std::array<char,257> request{};};
struct Slot {uint32_t instance=0,refs=0,paused=0,start=0,pauseOffset=0;uint64_t generation=0;};
struct Record {
    Context context;uint64_t invocation=0,pair=0;unsigned kind=0;DWORD thread=0;
    uint32_t type=0,slot=0,appCount=0,mtCursor=0,caller=0,instance=0,target=0,x=0,y=0,z=0;
    std::array<Slot,8> slots{};std::array<uint32_t,13> params{};std::array<uint32_t,10> candidates{};
};
struct Call {uint64_t id=0;uintptr_t esp=0,returnAddress=0;uint32_t type=0,pitch=0,selectedSlot=0;bool recentSeen=false,recent=false,variation=false,variationReturned=false,creationSeen=false;uint32_t created=0,range=0,cursorBefore=0;};
struct Query {uint64_t id=0,invocation=0;uint32_t type=0,slot=0,instance=0,target=0;uintptr_t esp=0;};
struct Increment {uint64_t id=0;uint32_t before=0;uintptr_t esp=0;};
std::array<Record,Capacity> queue;
std::array<Call,DepthLimit> calls;
std::array<Query,DepthLimit> queries;
std::array<Increment,DepthLimit> increments;
std::array<std::array<uint64_t,8>,TypeLimit> generations{};
size_t queued=0,callDepth=0,queryDepth=0,incrementDepth=0;
uint64_t entered=0,returned=0,queriesBegun=0,queriesReturned=0,variationsBegun=0,variationsReturned=0,creations=0,incrementsBegun=0,incrementsReturned=0,written=0,boundariesWritten=0;
std::atomic<uint64_t> faults{0},overflow{0},wrongThread{0};
std::atomic<uint32_t> firstForeignThread{0},firstForeignSite{0};
std::atomic<bool> capturing{false};
DWORD owner=0;bool enabled=false,installed=false,sealed=false,armed=false,testing=false;
uint64_t beginTick=0,endTick=5000;
uintptr_t app=0,system=0,params=0,cursorAddress=0,stackBase=0,stackLimit=0;
uint32_t types=0;
std::array<uintptr_t,12> sites{};
std::array<std::array<uint8_t,16>,12> patches{};
std::array<size_t,12> patchSizes{};
Context context;
std::ofstream output;
std::filesystem::path healthPath,baselinePath;
Json baselines=Json::object();
uint32_t lastBoundaryCount=0;uint64_t lastBoundaryIncrements=0;bool haveBoundaryCount=false;

bool Same(uintptr_t address,const void* bytes,size_t n)noexcept{return Accessible(address,n)&&!std::memcmp(reinterpret_cast<void*>(address),bytes,n);}
bool Owned()noexcept{for(size_t n=0;n<sites.size();++n)if(!Same(sites[n],patches[n].data(),patchSizes[n]))return false;return true;}
void Owner(){if(GetCurrentThreadId()!=owner)throw std::runtime_error("Foley diagnostic requires owner game thread");}
uint32_t Scalar(uintptr_t p)noexcept{uint32_t x;std::memcpy(&x,reinterpret_cast<void*>(p),4);return x;}
bool StackReadable(uintptr_t p,size_t n)noexcept{return p>=stackLimit&&p<=stackBase&&n<=stackBase-p;}
bool LiveRoots()noexcept{return testing||(Scalar(0x6a9ec0)==app&&Scalar(app+0x784)==system&&Scalar(0x6a9f00)==params&&Scalar(0x6a9f04)==types);}
bool ProbeRead(uintptr_t address,uint32_t& value)noexcept{if(!Accessible(address,4))return false;value=Scalar(address);return true;}
Slot ReadSlot(unsigned type,unsigned slot)noexcept{
    const auto at=system+type*0xa4+slot*0x14;
    return {Scalar(at),Scalar(at+4),uint32_t(*reinterpret_cast<uint8_t*>(at+8)),Scalar(at+12),Scalar(at+16),generations[type][slot]};
}
void Slots(Record& r)noexcept{for(unsigned n=0;n<8;++n)r.slots[n]=ReadSlot(r.type,n);}
Record Make(unsigned kind)noexcept{Record r;r.context=context;r.kind=kind;r.thread=owner;r.appCount=Scalar(app+0x484);r.mtCursor=Scalar(cursorAddress);if(callDepth&&callDepth<=DepthLimit)r.invocation=calls[callDepth-1].id;return r;}
void Push(const Record& r)noexcept{if(queued==Capacity){++overflow;return;}queue[queued++]=r;}
Call* Active()noexcept{if(!callDepth||callDepth>DepthLimit){++faults;return nullptr;}return &calls[callDepth-1];}
void Observe(unsigned site,Registers* f)noexcept{
    if(!capturing.load(std::memory_order_relaxed))return;
    if(GetCurrentThreadId()!=owner){++wrongThread;++faults;uint32_t zero=0;if(firstForeignThread.compare_exchange_strong(zero,GetCurrentThreadId()))firstForeignSite=uint32_t(OriginalSites[site]);return;}
    if(!LiveRoots()){++faults;return;}
    const auto esp=reinterpret_cast<uintptr_t>(f+1);
    if(!StackReadable(esp,4)){++faults;return;}
    if(site==0){
        ++entered;if(callDepth==DepthLimit){++overflow;++callDepth;return;}if(callDepth>DepthLimit){++callDepth;return;}
        if(f->eax>=types||f->ecx!=system||!StackReadable(esp,8)){++faults;return;}
        Call c;c.id=entered;c.type=f->eax;c.esp=esp;c.returnAddress=Scalar(esp);c.pitch=Scalar(esp+4);calls[callDepth++]=c;
        auto r=Make(0);r.type=c.type;r.caller=c.returnAddress;r.x=c.pitch;r.y=Scalar(system+c.type*0xa4+0xa0);Slots(r);
        std::memcpy(r.params.data(),reinterpret_cast<void*>(params+c.type*0x34),0x34);
        if(r.params[0]!=c.type)++faults;
        for(unsigned n=0;n<10;++n)if(r.params[n+2]&&!ProbeRead(r.params[n+2],r.candidates[n]))++faults;
        Push(r);return;
    }
    if(site==1){
        ++queriesBegun;if(queryDepth>=DepthLimit){++queryDepth;++overflow;return;}
        const uint32_t type=f->ebx,slot=8-f->edi;
        if(type>=types||f->edi<1||f->edi>8||f->esi!=system+type*0xa4+slot*0x14){++faults;return;}
        const auto s=ReadSlot(type,slot);uint32_t vtable=0,target=0;
        if(!s.refs||s.paused||!ProbeRead(s.instance,vtable)||!ProbeRead(vtable+0x24,target)){++faults;return;}
        Query q{queriesBegun,callDepth&&callDepth<=DepthLimit?calls[callDepth-1].id:0,type,slot,s.instance,target,esp};queries[queryDepth++]=q;
        auto r=Make(1);r.pair=q.id;r.type=type;r.slot=slot;r.instance=s.instance;r.target=target;r.slots[0]=s;Push(r);return;
    }
    if(site==2){
        ++queriesReturned;if(!queryDepth){++faults;return;}if(queryDepth>DepthLimit){--queryDepth;return;}
        const auto q=queries[--queryDepth];if(q.esp!=esp||f->esi!=system+q.type*0xa4+q.slot*0x14){++faults;return;}
        auto r=Make(2);r.invocation=q.invocation;r.pair=q.id;r.type=q.type;r.slot=q.slot;r.instance=q.instance;r.target=q.target;r.x=f->eax&255;Push(r);return;
    }
    if(site==10){
        ++incrementsBegun;if(incrementDepth>=DepthLimit){++incrementDepth;++overflow;return;}
        if(f->esi!=app){++faults;return;}Increment n{incrementsBegun,Scalar(app+0x484),esp};increments[incrementDepth++]=n;
        auto r=Make(10);r.pair=n.id;r.x=n.before;Push(r);return;
    }
    if(site==11){
        ++incrementsReturned;if(!incrementDepth){++faults;return;}if(incrementDepth>DepthLimit){--incrementDepth;return;}
        const auto n=increments[--incrementDepth];const auto now=Scalar(app+0x484);
        if(n.esp!=esp||f->esi!=app||now!=n.before+1)++faults;
        auto r=Make(11);r.pair=n.id;r.x=n.before;r.y=now;Push(r);return;
    }
    if(site>=7&&site<=9&&callDepth>DepthLimit){--callDepth;++returned;return;}
    auto* c=Active();if(!c)return;
    auto r=Make(site);r.type=c->type;
    if(site==3){
        if(c->recentSeen||f->esi!=c->type||f->edi!=system)++faults;
        c->recentSeen=true;c->recent=(f->eax&255)!=0;r.x=f->eax&255;Slots(r);Push(r);return;
    }
    if(site==4){
        ++variationsBegun;if(c->variation||!c->recentSeen||f->eax<1||f->eax>10||!StackReadable(esp,0x18+f->eax*4)){++faults;return;}
        c->variation=true;c->range=f->eax;c->cursorBefore=r.mtCursor;const auto target=Scalar(esp+0x10);
        if(target<system+c->type*0xa4||target>=system+c->type*0xa4+0xa0||(target-system-c->type*0xa4)%0x14){++faults;return;}
        c->selectedSlot=(target-system-c->type*0xa4)/0x14;r.slot=c->selectedSlot;r.x=f->eax;
        std::memcpy(r.candidates.data(),reinterpret_cast<void*>(esp+0x18),f->eax*4);Push(r);return;
    }
    if(site==5){
        ++variationsReturned;if(!c->variation||c->variationReturned||f->eax>=c->range||!StackReadable(esp,0x18+c->range*4)){++faults;return;}
        if(c->cursorBefore>625||r.mtCursor!=(c->cursorBefore>=624?1:c->cursorBefore+1))++faults;
        c->variationReturned=true;r.slot=c->selectedSlot;r.x=f->eax;r.y=Scalar(esp+0x18+f->eax*4);r.z=c->range;Push(r);return;
    }
    if(site==6){
        ++creations;if(!c->variationReturned||c->creationSeen)++faults;
        c->creationSeen=true;c->created=f->eax;r.instance=f->eax;r.slot=c->selectedSlot;
        if(f->eax)++generations[c->type][c->selectedSlot];r.pair=generations[c->type][c->selectedSlot];Push(r);return;
    }
    if(site>=7&&site<=9){
        ++returned;
        if(!StackReadable(f->ebp,8)||Scalar(f->ebp+4)!=c->returnAddress||f->ebp+4!=c->esp||!c->recentSeen)++faults;
        if(site==7&&(c->variation||c->creationSeen))++faults;
        if(site==8&&(c->variation||c->creationSeen))++faults;
        if(site==9&&!c->recent&&(!c->variationReturned||!c->creationSeen))++faults;
        r.x=c->recent;r.y=c->variationReturned;r.z=c->creationSeen;r.instance=c->created;Slots(r);Push(r);--callDepth;return;
    }
    ++faults;
}
const char* PhaseName(unsigned n){switch(n){case 1:return "update";case 2:return "draw";case 3:return "warm";case 4:return "request_started";case 5:return "action";case 6:return "environment_collect";default:return "outside_controlled_call";}}
Json Version(const Context& c){return c.version?Json{{"epoch",c.epoch},{"tick",c.tick},{"revision",c.revision}}:Json(nullptr);}
Json EncodeSlot(const Slot& s,uint32_t now){return {{"instance",s.instance},{"refcount",s.refs},{"paused",s.paused},{"start_count",s.start},{"pause_offset",s.pauseOffset},{"creation_generation",s.generation},{"elapsed_u32",uint32_t(now-s.start)},{"elapsed_i32",int32_t(now-s.start)}};}
Json EncodeSlots(const Record& r){Json out=Json::array();for(auto& s:r.slots)out.push_back(EncodeSlot(s,r.appCount));return out;}
Json Encode(const Record& r,uint64_t ordinal){
    static const char* names[]={"foley_enter","is_playing_begin","is_playing_return","recent_return","variation_begin","variation_return","sound_instance_return","foley_return_full","foley_return_reuse","foley_return_common","app_increment_begin","app_increment_return"};
    Json out={{"schema","lvz.foley-event.v1"},{"ordinal",ordinal},{"kind",names[r.kind]},
        {"version",Version(r.context)},{"phase",PhaseName(r.context.phase)},{"request_id",r.context.request[0]?Json(r.context.request.data()):Json(nullptr)},
        {"engine_call_id",r.context.engineCall?Json(r.context.engineCall):Json(nullptr)},{"thread_id",r.thread},
        {"invocation_id",r.invocation?Json(r.invocation):Json(nullptr)},{"app_update_count",r.appCount},{"mt_cursor",r.mtCursor}};
    if(r.kind<10)out["foley_type"]=r.type;
    switch(r.kind){
    case 0:out["caller"]=r.caller;out["pitch_bits"]=r.x;out["last_variation"]=r.y;out["slots"]=EncodeSlots(r);out["params_words"]=r.params;out["resource_ids"]=Json::array();for(unsigned n=0;n<10;++n)out["resource_ids"].push_back(r.params[n+2]?Json(r.candidates[n]):Json(nullptr));break;
    case 1:out["query_id"]=r.pair;out["slot"]=r.slot;out["instance"]=r.instance;out["target"]=r.target;out["slot_before"]=EncodeSlot(r.slots[0],r.appCount);break;
    case 2:out["query_id"]=r.pair;out["slot"]=r.slot;out["instance"]=r.instance;out["target"]=r.target;out["returned_al"]=r.x;break;
    case 3:out["returned_al"]=r.x;out["slots_after_release"]=EncodeSlots(r);break;
    case 4:out["slot"]=r.slot;out["range"]=r.x;out["candidates"]=Json::array();for(unsigned n=0;n<r.x;++n)out["candidates"].push_back(r.candidates[n]);break;
    case 5:out["slot"]=r.slot;out["returned_index"]=r.x;out["selected_variation"]=r.y;out["range"]=r.z;break;
    case 6:out["slot"]=r.slot;out["returned_instance"]=r.instance;out["creation_generation"]=r.pair;break;
    case 7:case 8:case 9:out["return_site"]=OriginalSites[r.kind];out["recent"]=bool(r.x);out["variation_executed"]=bool(r.y);out["creation_called"]=bool(r.z);out["created_instance"]=r.instance;out["slots_after"]=EncodeSlots(r);break;
    case 10:case 11:out["increment_id"]=r.pair;out["before"]=r.x;if(r.kind==11)out["after"]=r.y;break;
    }
    return out;
}
uint64_t EnvNumber(const wchar_t* name,uint64_t fallback){wchar_t text[32]{};const auto n=GetEnvironmentVariableW(name,text,32);if(!n)return fallback;if(n>=32)throw std::runtime_error("Foley range too long");uint64_t v=0;for(unsigned i=0;i<n;++i){if(text[i]<'0'||text[i]>'9'||v>1000000)throw std::runtime_error("Invalid Foley range");v=v*10+text[i]-'0';}return v;}
bool Install(const std::array<uintptr_t,12>& requested,bool test,std::string& error){
    if(installed){error="Foley hooks already installed";return false;}static const uint8_t nop[5]={0x90,0x90,0x90,0x90,0x90};
    for(size_t n=0;n<requested.size();++n)if(!Same(requested[n],test?nop:Signatures[n].data(),test?5:Signatures[n].size())){error="Foley exact hook signature mismatch at site "+std::to_string(n);return false;}
    auto status=MH_Initialize();if(status!=MH_OK&&status!=MH_ERROR_ALREADY_INITIALIZED){error=MH_StatusToString(status);return false;}
    size_t created=0;for(;created<requested.size();++created){status=MH_CreateHook(reinterpret_cast<void*>(requested[created]),Shims[created],&lvzFoleyTrampolines[created]);if(status!=MH_OK)break;}
    if(created==requested.size())for(size_t n=0;n<requested.size();++n){status=MH_EnableHook(reinterpret_cast<void*>(requested[n]));if(status!=MH_OK)break;}
    if(status!=MH_OK){for(size_t n=0;n<created;++n){MH_DisableHook(reinterpret_cast<void*>(requested[n]));MH_RemoveHook(reinterpret_cast<void*>(requested[n]));lvzFoleyTrampolines[n]=nullptr;}error=MH_StatusToString(status);return false;}
    sites=requested;for(size_t n=0;n<sites.size();++n){patchSizes[n]=test?5:Signatures[n].size();std::memcpy(patches[n].data(),reinterpret_cast<void*>(sites[n]),patchSizes[n]);}
    owner=GetCurrentThreadId();__asm__ volatile("movl %%fs:4,%0\n\tmovl %%fs:8,%1":"=r"(stackBase),"=r"(stackLimit));
    queued=callDepth=queryDepth=incrementDepth=0;entered=returned=queriesBegun=queriesReturned=variationsBegun=variationsReturned=creations=incrementsBegun=incrementsReturned=written=boundariesWritten=0;
    faults=overflow=wrongThread=0;firstForeignThread=firstForeignSite=0;context={};generations={};sealed=armed=false;capturing=false;baselines=Json::object();haveBoundaryCount=false;
    installed=true;testing=test;error.clear();return true;
}
bool Remove(std::string& error){
    if(!installed){error.clear();return true;}if(GetCurrentThreadId()!=owner||callDepth||queryDepth||incrementDepth){error="Foley hook removal requires idle owner";return false;}
    if(!Owned()){error="Foley hook ownership changed";return false;}capturing=false;
    for(size_t n=0;n<sites.size();++n)if(MH_DisableHook(reinterpret_cast<void*>(sites[n]))!=MH_OK){error="Cannot disable Foley hook; keep DLL loaded";return false;}
    for(size_t n=0;n<sites.size();++n){MH_RemoveHook(reinterpret_cast<void*>(sites[n]));lvzFoleyTrampolines[n]=nullptr;}installed=false;return true;
}
void Roots(){
    if(testing)return;
    app=Read<uint32_t>(0x6a9ec0);system=Read<uint32_t>(app+0x784);params=Read<uint32_t>(0x6a9f00);types=Read<uint32_t>(0x6a9f04);cursorAddress=0x75a910+0x9c0;
    if(!app||!system||types<1||types>TypeLimit||!Accessible(app,0x900)||!Accessible(system,types*0xa4)||!Accessible(params,types*0x34)||!Accessible(cursorAddress,4))throw std::runtime_error("Unsupported Foley diagnostic roots");
}
Json Snapshot(){
    Roots();const auto now=Scalar(app+0x484);Json table=Json::array();
    for(unsigned type=0;type<types;++type){Record r;r.type=type;r.appCount=now;Slots(r);std::memcpy(r.params.data(),reinterpret_cast<void*>(params+type*0x34),0x34);
        Json resources=Json::array();for(unsigned n=0;n<10;++n)resources.push_back(r.params[n+2]?Json(Read<uint32_t>(r.params[n+2])):Json(nullptr));
        table.push_back({{"type",type},{"slots",EncodeSlots(r)},{"last_variation",Scalar(system+type*0xa4+0xa0)},{"params_words",r.params},{"resource_ids",std::move(resources)}});}
    const auto board=Read<uint32_t>(app+0x768);
    return {{"version",Version(context)},{"app",app},{"system",system},{"params",params},{"type_count",types},{"app_update_count",now},
        {"game_clock",board?Json(Read<uint32_t>(board+0x5568)):Json(nullptr)},{"effect_clock",board?Json(Read<uint32_t>(board+0x556c)):Json(nullptr)},
        {"mj_clock",Scalar(app+0x838)},{"mt_cursor",Scalar(cursorAddress)},{"table",std::move(table)}};
}
void SaveBaselines(){std::ofstream file(baselinePath,std::ios::binary|std::ios::out);file<<Json{{"schema","lvz.foley-baseline.v1"},{"snapshots",baselines}}.dump(2)<<'\n';file.close();if(file.fail())throw std::runtime_error("Foley baseline write failed");}
}
void Initialize(const std::filesystem::path& runDir){
    wchar_t value[8]{};const auto n=GetEnvironmentVariableW(L"LVZ_FOLEY_TRACE",value,8);if(!n||std::wstring(value)==L"0"){enabled=false;return;}if(n!=1||value[0]!='1')throw std::runtime_error("LVZ_FOLEY_TRACE must be 0 or 1");
    beginTick=EnvNumber(L"LVZ_FOLEY_TRACE_BEGIN",0);endTick=EnvNumber(L"LVZ_FOLEY_TRACE_END",5000);if(beginTick>=endTick||endTick>1000000)throw std::runtime_error("Invalid Foley trace range");
    const auto directory=runDir/"audit";std::filesystem::create_directories(directory);healthPath=directory/"foley-trace-health.json";baselinePath=directory/"foley-baseline.json";
    const auto path=directory/"foley-events.jsonl";if(std::filesystem::exists(healthPath)||std::filesystem::exists(baselinePath)||(std::filesystem::exists(path)&&std::filesystem::file_size(path)))throw std::runtime_error("Foley trace already exists");
    output.open(path,std::ios::out|std::ios::binary);if(!output)throw std::runtime_error("Cannot open Foley trace");std::string error;
    if(!Install(OriginalSites,false,error)){output.close();throw std::runtime_error(error);}enabled=true;
}
void Boundary(const std::string& kind,const Json& payload,const Json& version){
    if(!enabled)return;Owner();if(callDepth||queryDepth||incrementDepth)throw std::runtime_error("Foley boundary crosses unfinished original call");
    if(kind=="recording_closed"||kind=="engine_call_closed"){capturing=false;return;}
    const bool activating=!armed&&(kind=="render_preparing"||kind=="pre_step");
    if(!armed&&!activating)return;
    if(activating)Roots();
    if(!LiveRoots())throw std::runtime_error("Foley roots changed");
    const auto now=Scalar(app+0x484);
    if(capturing&&haveBoundaryCount&&uint32_t(now-lastBoundaryCount)!=uint32_t(incrementsReturned-lastBoundaryIncrements)){++faults;throw std::runtime_error("App update count changed outside observed original increment");}
    lastBoundaryCount=now;lastBoundaryIncrements=incrementsReturned;haveBoundaryCount=true;
    Context next{};if(version.is_object()&&version.contains("tick")){next.version=true;next.epoch=version.at("epoch");next.tick=version.at("tick");next.revision=version.at("revision");}
    const auto request=payload.value("request_id",std::string());if(request.size()>256)throw std::runtime_error("Foley request ID too long");std::memcpy(next.request.data(),request.data(),request.size());
    next.phase=kind=="pre_step"?1:kind=="render_preparing"?3:kind=="request_started"?4:kind=="action"?5:kind=="environment_collect"?6:0;
    if(kind=="pre_step"&&payload.contains("engine_call"))next.engineCall=payload.at("engine_call").at("engine_call_id");
    if(kind=="render_preparing"||kind=="pre_step")armed=true;
    context=next;capturing=armed&&next.version&&next.tick>=beginTick&&next.tick<endTick&&!sealed;
    if(kind=="render_preparing"||kind=="render_prepared"){
        baselines[kind=="render_preparing"?"before_warm":"B0_after_warm"]=Snapshot();SaveBaselines();
    }
    if((!testing||output.is_open())&&armed&&next.tick>=beginTick&&next.tick<=endTick&&(kind=="pre_step"||kind=="post_step"||kind=="terminal_transition"||kind=="render_prepared")){
        Json event={{"schema","lvz.foley-event.v1"},{"ordinal",written++},{"kind","boundary"},{"audit_kind",kind},{"version",Version(next)},
            {"engine_call_id",payload.contains("engine_call")?payload.at("engine_call").at("engine_call_id"):Json(nullptr)},
            {"app_update_count",now},{"observed_increments",incrementsReturned}};
        output<<event.dump()<<'\n';if(!output)throw std::runtime_error("Foley boundary output failed");++boundariesWritten;
    }
}
Phase::Phase(bool warm){if(!enabled)return;Owner();previous_=context.phase;context.phase=warm?3:2;}
Phase::~Phase(){if(enabled)context.phase=previous_;}
void DrainAndCheck(bool allowFault){
    if(!enabled)return;Owner();if(callDepth||queryDepth||incrementDepth)throw std::runtime_error("Unmatched Foley hook entry");
    for(size_t n=0;n<queued;++n){output<<Encode(queue[n],written).dump()<<'\n';if(!output)throw std::runtime_error("Foley trace output failed");++written;}queued=0;
    if(!Owned()||faults||overflow||wrongThread){Flush();if(!allowFault)throw std::runtime_error("Foley trace incomplete or guard fault");}
}
void Flush(){if(enabled){Owner();if(sealed&&!installed&&!output.is_open())return;output.flush();if(!output)throw std::runtime_error("Foley trace flush failed");}}
Json Manifest(){return {{"mode","foley_branch_trace_v1"},{"diagnostic_only",true},{"enabled",enabled},{"installed",installed},{"rng_modified",false},{"timing_perturbation",true},
    {"range_begin",beginTick},{"range_end",endTick},{"range_semantics","pre_tick_half_open"},{"capacity",Capacity},{"depth_limit",DepthLimit},
    {"hook_sites",OriginalSites},{"generation_semantics","observed nonnull GetSoundInstance returns per type/slot since activation; baseline instances are generation zero"},
    {"raw_evidence","foley-events.jsonl"},{"baseline_evidence","foley-baseline.json"},{"health_evidence","foley-trace-health.json"}};}
Json Health(){return {{"enabled",enabled},{"installed",installed},{"sealed",sealed},{"entered",entered},{"returned",returned},{"queries_begun",queriesBegun},{"queries_returned",queriesReturned},
    {"variations_begun",variationsBegun},{"variations_returned",variationsReturned},{"sound_instance_returns",creations},{"increments_begun",incrementsBegun},{"increments_returned",incrementsReturned},
    {"written",written},{"boundaries_written",boundariesWritten},{"queued",queued},{"call_depth",callDepth},{"query_depth",queryDepth},{"increment_depth",incrementDepth},
    {"faults",faults.load()},{"overflow",overflow.load()},{"wrong_thread_calls",wrongThread.load()},{"first_foreign_thread",firstForeignThread.load()},{"first_foreign_site",firstForeignSite.load()},
    {"healthy",!faults&&!overflow&&!wrongThread&&!callDepth&&!queryDepth&&!incrementDepth&&entered==returned&&queriesBegun==queriesReturned&&variationsBegun==variationsReturned&&incrementsBegun==incrementsReturned}};}
void Shutdown(){
    if(!enabled)return;Owner();DrainAndCheck(true);capturing=false;sealed=true;Flush();std::string error;if(!Remove(error))throw std::runtime_error(error);
    output.close();if(output.fail())throw std::runtime_error("Foley trace close failed");std::ofstream file(healthPath,std::ios::out|std::ios::binary);
    auto health=Health();health["schema"]="lvz.foley-health.v1";health["configuration"]=Manifest();file<<health.dump(2)<<'\n';file.close();if(file.fail())throw std::runtime_error("Foley health write failed");
}
#ifdef LVZ_FOLEY_TRACE_TESTING
bool InstallForTest(const std::array<uintptr_t,12>& requested,uintptr_t a,uintptr_t s,uintptr_t p,uintptr_t mt,uint32_t count,std::string& error){
    app=a;system=s;params=p;cursorAddress=mt;types=count;bool ok=Install(requested,true,error);enabled=ok;beginTick=0;endTick=5000;return ok;
}
bool RemoveForTest(std::string& error){const bool ok=Remove(error);if(ok)enabled=false;return ok;}
Json DrainForTest(){Owner();Json rows=Json::array();for(size_t n=0;n<queued;++n)rows.push_back(Encode(queue[n],written++));queued=0;return rows;}
void OpenOutputForTest(const std::filesystem::path& directory){Owner();std::filesystem::create_directories(directory);healthPath=directory/"foley-trace-health.json";baselinePath=directory/"foley-baseline.json";output.clear();output.open(directory/"foley-events.jsonl",std::ios::out|std::ios::binary);if(!output)throw std::runtime_error("Foley fixture output failed");}
#endif
}
extern "C" void __cdecl LvzFoleyObserve(unsigned site,void* frame)noexcept{const DWORD error=GetLastError();lvz::determinism::foleytrace::Observe(site,static_cast<lvz::determinism::foleytrace::Registers*>(frame));SetLastError(error);}
