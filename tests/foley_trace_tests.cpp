#include "determinism/foley_trace.hpp"
#include <Windows.h>
#include <array>
#include <cstring>
#include <fstream>
#include <iostream>
#include <thread>
using namespace lvz::determinism::foleytrace;
using Json=nlohmann::json;
extern "C" {
alignas(16) uint8_t fixtureApp[0x900]{},fixtureSystem[0xa4]{};
alignas(16) uint32_t fixtureParams[13]{},fixtureIds[3]={7,11,13},fixtureVtable[10]{},fixtureInstance[8]{};
uint32_t fixtureCursor=100,fixturePlaying=1,fixtureCreate=1,fixtureInvoked=0;
uint32_t fixtureFunction=0;
alignas(16) uint8_t fixtureResult[560]{},fixtureCallerFx[512]{};
alignas(16) uint32_t fixtureXmm[4]={0x12345678,0x90abcdef,0xfedcba09,0x87654321};
void FoleySite0();void FoleySite1();void FoleySite2();void FoleySite3();void FoleySite4();void FoleySite5();
void FoleySite6();void FoleySite7();void FoleySite8();void FoleySite9();void FoleySite10();void FoleySite11();
__attribute__((naked)) void FakeIsPlaying(){__asm__ volatile("movl _fixturePlaying,%eax\n\tretl\n\t");}
__attribute__((naked)) void FakeRand(){__asm__ volatile(
    "addl $1,_fixtureInvoked\n\tmovl _fixtureCursor,%edx\n\tcmpl $624,%edx\n\tjb 1f\n\txorl %edx,%edx\n\t"
    "1:incl %edx\n\tmovl %edx,_fixtureCursor\n\tmovl $1,%eax\n\tretl\n\t");}
__attribute__((naked)) void FakeGetInstance(){__asm__ volatile(
    "xorl %eax,%eax\n\tcmpl $0,_fixtureCreate\n\tje 1f\n\tmovl $_fixtureInstance,%eax\n\t1:retl\n\t");}
uint8_t __cdecl FakeRecent(){
    const auto now=*reinterpret_cast<uint32_t*>(fixtureApp+0x484);
    for(unsigned n=0;n<8;++n){auto* s=reinterpret_cast<uint32_t*>(fixtureSystem+n*0x14);if(s[1]&&int32_t(now-s[3])<10)return 1;}return 0;
}
#define SITE(N) ".globl _FoleySite" #N "\n\t_FoleySite" #N ":.byte 0x90,0x90,0x90,0x90,0x90\n\t"
// These NOP sites are exact fixture-only signatures. They surround actual
// callback invocations and instructions, not direct calls to the C observer.
__attribute__((naked)) void FakeFoley(){__asm__ volatile(
    SITE(0)
    "pushl %ebp\n\tmovl %esp,%ebp\n\tsubl $128,%esp\n\t"
    "xorl %ebx,%ebx\n\tmovl $_fixtureSystem,%esi\n\tmovl $8,%edi\n\t"
    "1:cmpl $0,4(%esi)\n\tje 3f\n\tcmpb $0,8(%esi)\n\tjne 3f\n\t"
    SITE(1)
    "movl (%esi),%ecx\n\tcall _FakeIsPlaying\n\t"
    SITE(2)
    "testb %al,%al\n\tjne 3f\n\tmovl $0,(%esi)\n\tmovl $0,4(%esi)\n\t"
    "3:addl $20,%esi\n\tdecl %edi\n\tjne 1b\n\t"
    "xorl %esi,%esi\n\tmovl $_fixtureSystem,%edi\n\tcall _FakeRecent\n\t"
    SITE(3)
    "testb %al,%al\n\tje 4f\n\ttestl $1,_fixtureParams+48\n\tje 9f\n\t"
    "4:testl $2,_fixtureParams+48\n\tje 5f\n\tcmpl $0,_fixtureSystem+4\n\tje 5f\n\t"
    "incl _fixtureSystem+4\n\tmovl _fixtureApp+0x484,%eax\n\tmovl %eax,_fixtureSystem+12\n\tjmp 8f\n\t"
    "5:movl $_fixtureSystem,%edx\n\txorl %eax,%eax\n\t"
    "6:cmpl $0,4(%edx)\n\tje 7f\n\taddl $20,%edx\n\tincl %eax\n\tcmpl $8,%eax\n\tjb 6b\n\tjmp 10f\n\t"
    "7:movl %edx,0x10(%esp)\n\tmovl $0,0x18(%esp)\n\tmovl $1,0x1c(%esp)\n\tmovl $2,0x20(%esp)\n\tmovl $3,%eax\n\t"
    SITE(4)
    "call _FakeRand\n\t"
    SITE(5)
    "movl 0x18(%esp,%eax,4),%edi\n\tmovl %edi,_fixtureSystem+0xa0\n\tcall _FakeGetInstance\n\t"
    SITE(6)
    "testl %eax,%eax\n\tje 9f\n\tmovl 0x10(%esp),%ecx\n\tmovl %eax,(%ecx)\n\tmovl $1,4(%ecx)\n\t"
    "movl _fixtureApp+0x484,%eax\n\tmovl %eax,12(%ecx)\n\tjmp 9f\n\t"
    "10:\n\t" SITE(7) "jmp 11f\n\t"
    "8:\n\t" SITE(8) "jmp 11f\n\t"
    "9:\n\t" SITE(9)
    "11:movl %ebp,%esp\n\tpopl %ebp\n\tretl $4\n\t");}
__attribute__((naked)) void FakeIncrement(){__asm__ volatile(
    "movl $_fixtureApp,%esi\n\t" SITE(10)
    "addl $1,0x484(%esi)\n\t" SITE(11) "retl $4\n\t");}
#undef SITE
__attribute__((naked)) void CallFixture(){__asm__ volatile(
    "pushfl\n\tpushal\n\tfxsave _fixtureCallerFx\n\tfninit\n\tfldpi\n\tfld1\n\t"
    "movdqu _fixtureXmm,%xmm0\n\tmovdqu _fixtureXmm,%xmm1\n\tmovdqu _fixtureXmm,%xmm2\n\tmovdqu _fixtureXmm,%xmm3\n\t"
    "movdqu _fixtureXmm,%xmm4\n\tmovdqu _fixtureXmm,%xmm5\n\tmovdqu _fixtureXmm,%xmm6\n\tmovdqu _fixtureXmm,%xmm7\n\t"
    "movl $0x11111111,%ebx\n\tmovl $0x22222222,%ebp\n\tmovl $0x33333333,%ecx\n\t"
    "movl $0x44444444,%edx\n\tmovl $0x55555555,%esi\n\tmovl $0x66666666,%edi\n\tpushl $0x247\n\tpopfl\n\t"
    "movl $_fixtureSystem,%ecx\n\txorl %eax,%eax\n\tpushl $0x3f800000\n\tcall *_fixtureFunction\n\t"
    "pushfl\n\tpushal\n\tcld\n\tmovl %esp,%esi\n\tmovl $_fixtureResult,%edi\n\tmovl $9,%ecx\n\trep movsl\n\t"
    "fxsave _fixtureResult+48\n\tpopal\n\tpopfl\n\tfxrstor _fixtureCallerFx\n\tpopal\n\tpopfl\n\tretl\n\t");}
}
const std::array<uintptr_t,12> sites={reinterpret_cast<uintptr_t>(&FoleySite0),reinterpret_cast<uintptr_t>(&FoleySite1),reinterpret_cast<uintptr_t>(&FoleySite2),reinterpret_cast<uintptr_t>(&FoleySite3),reinterpret_cast<uintptr_t>(&FoleySite4),reinterpret_cast<uintptr_t>(&FoleySite5),reinterpret_cast<uintptr_t>(&FoleySite6),reinterpret_cast<uintptr_t>(&FoleySite7),reinterpret_cast<uintptr_t>(&FoleySite8),reinterpret_cast<uintptr_t>(&FoleySite9),reinterpret_cast<uintptr_t>(&FoleySite10),reinterpret_cast<uintptr_t>(&FoleySite11)};
void Check(bool ok,const char* reason){if(!ok)throw std::runtime_error(reason);}
void Reset(unsigned flags=0,unsigned live=0,unsigned age=30,bool playing=true,bool create=true,unsigned cursor=100){
    std::memset(fixtureApp,0,sizeof(fixtureApp));std::memset(fixtureSystem,0,sizeof(fixtureSystem));std::memset(fixtureParams,0,sizeof(fixtureParams));
    *reinterpret_cast<uint32_t*>(fixtureApp+0x484)=100;fixturePlaying=playing;fixtureCreate=create;fixtureCursor=cursor;fixtureInvoked=0;
    fixtureVtable[9]=reinterpret_cast<uintptr_t>(&FakeIsPlaying);fixtureInstance[0]=reinterpret_cast<uintptr_t>(fixtureVtable);
    for(unsigned n=0;n<live;++n){auto* s=reinterpret_cast<uint32_t*>(fixtureSystem+20*n);s[0]=reinterpret_cast<uintptr_t>(fixtureInstance);s[1]=1;s[3]=100-age;}
    for(unsigned n=0;n<3;++n)fixtureParams[2+n]=reinterpret_cast<uintptr_t>(&fixtureIds[n]);fixtureParams[12]=flags;
    fixtureFunction=reinterpret_cast<uintptr_t>(&FakeFoley);
}
void Install(){std::string error;Check(InstallForTest(sites,reinterpret_cast<uintptr_t>(fixtureApp),reinterpret_cast<uintptr_t>(fixtureSystem),reinterpret_cast<uintptr_t>(fixtureParams),reinterpret_cast<uintptr_t>(&fixtureCursor),1,error),error.c_str());}
void Remove(){std::string error;Check(RemoveForTest(error),error.c_str());}
void Label(uint64_t tick=0,const char* kind="pre_step"){
    Boundary(kind,{{"request_id","fixture"},{"engine_call",{{"engine_call_id",1}}}},{{"epoch",3},{"revision",2},{"tick",tick}});
}
void EqualMachine(const std::array<uint8_t,560>& original){
    for(size_t n=0;n<36;++n)if(n<12||n>=16)Check(original[n]==fixtureResult[n],"Foley hook changed original GPR/flags");
    for(size_t n=48;n<560;++n)Check(original[n]==fixtureResult[n],"Foley hook changed original x87/XMM");
}
int main(){try{
    struct Case {unsigned flags,live,age;bool playing,create;unsigned cursor;const char* exit;unsigned queries,variations;};
    const Case cases[]={
        {0,0,30,true,true,100,"foley_return_common",0,1},
        {0,1,30,false,true,624,"foley_return_common",1,1},
        {0,1,2,true,true,100,"foley_return_common",1,0},
        {1,1,2,true,true,100,"foley_return_common",1,1},
        {2,1,30,true,true,100,"foley_return_reuse",1,0},
        {0,8,30,true,true,100,"foley_return_full",8,0},
        {0,0,30,true,false,625,"foley_return_common",0,1}};
    for(const auto& c:cases){
        Reset(c.flags,c.live,c.age,c.playing,c.create,c.cursor);CallFixture();std::array<uint8_t,560> original{};std::memcpy(original.data(),fixtureResult,560);
        std::array<uint8_t,0xa4> originalState{};std::memcpy(originalState.data(),fixtureSystem,0xa4);const auto originalCursor=fixtureCursor,originalCalls=fixtureInvoked;
        Reset(c.flags,c.live,c.age,c.playing,c.create,c.cursor);Install();Label();SetLastError(9876);CallFixture();Check(GetLastError()==9876,"Foley hook changed LastError");EqualMachine(original);
        Check(!std::memcmp(originalState.data(),fixtureSystem,0xa4)&&fixtureCursor==originalCursor&&fixtureInvoked==originalCalls,"Foley hook changed original callback/branch/cursor results");
        const auto rows=DrainForTest();Check(rows.front()["kind"]=="foley_enter"&&rows.back()["kind"]==c.exit,"wrong true entry/return site");
        Check(rows.front()["resource_ids"]==Json::array({7,11,13,nullptr,nullptr,nullptr,nullptr,nullptr,nullptr,nullptr}),"resource IDs not actually read");
        unsigned q=0,v=0;for(const auto& r:rows){if(r["kind"]=="is_playing_return"){++q;Check(r["returned_al"]==unsigned(c.playing),"IsPlaying original AL changed");}if(r["kind"]=="variation_return"){++v;Check(r["returned_index"]==1&&r["selected_variation"]==1,"actual variation result lost");}}
        Check(q==c.queries&&v==c.variations&&Health()["healthy"],"Foley causal branch/count guard failed");
        Check(Health()["entered"]==1&&Health()["returned"]==1,"unbalanced Foley callback");Remove();
    }
    Reset();fixtureFunction=reinterpret_cast<uintptr_t>(&FakeIncrement);CallFixture();std::array<uint8_t,560> original{};std::memcpy(original.data(),fixtureResult,560);
    Reset();fixtureFunction=reinterpret_cast<uintptr_t>(&FakeIncrement);Install();Label();SetLastError(321);CallFixture();Check(GetLastError()==321,"counter hook changed LastError");EqualMachine(original);
    auto rows=DrainForTest();Check(rows.size()==2&&rows[0]["before"]==100&&rows[1]["after"]==101,"actual original add not measured");Label(1,"post_step");Check(Health()["healthy"],"real app count increment was rejected");
    Label(5000);CallFixture();Check(DrainForTest().empty(),"out-of-range counter traced");Label(5001,"post_step");Remove();
    Reset();Install();Label();std::thread other([]{CallFixture();});other.join();Check(!Health()["healthy"].get<bool>()&&Health()["wrong_thread_calls"]>0,"foreign Foley hook silently accepted");Remove();
    Reset();Install();Label();++*reinterpret_cast<uint32_t*>(fixtureApp+0x484);bool unexplained=false;
    try{Label(1,"post_step");}catch(const std::exception&){unexplained=true;}
    Check(unexplained&&!Health()["healthy"].get<bool>(),"unobserved App counter write accepted");
    // A latched observation fault must not obstruct the existing close RPC.
    Label(1,"recording_closed");Label(1,"engine_call_closed");Remove();
    Reset();Install();Label();fixtureFunction=reinterpret_cast<uintptr_t>(&FakeIncrement);for(unsigned n=0;n<8193;++n)CallFixture();Check(Health()["overflow"]==2&&!Health()["healthy"].get<bool>(),"Foley bounded queue overflow accepted");Check(DrainForTest().size()==16384,"Foley queue capacity wrong");Remove();
    Reset();Install();Label();auto* bytes=reinterpret_cast<uint8_t*>(sites[0]);DWORD old=0;Check(VirtualProtect(bytes,5,PAGE_EXECUTE_READWRITE,&old),"fixture protection");bytes[4]^=1;std::string error;Check(!RemoveForTest(error),"foreign hook patch overwritten");bytes[4]^=1;DWORD ignored=0;VirtualProtect(bytes,5,old,&ignored);FlushInstructionCache(GetCurrentProcess(),bytes,5);Remove();
    const auto directory=std::filesystem::temp_directory_path()/("lvz-foley-fixture-"+std::to_string(GetCurrentProcessId()));Check(!std::filesystem::exists(directory),"fixture destination exists");
    Reset();Install();OpenOutputForTest(directory);Label(0,"render_preparing");Label(0,"render_prepared");Label();CallFixture();DrainAndCheck();Label(1,"post_step");Shutdown();const auto size=std::filesystem::file_size(directory/"foley-events.jsonl");Flush();Check(std::filesystem::file_size(directory/"foley-events.jsonl")==size,"closed Flush changed evidence");
    const auto health=Json::parse(std::ifstream(directory/"foley-trace-health.json"));Check(health["healthy"]==true&&health["sealed"]==true&&health["installed"]==false,"file closure not complete");
    const auto baseline=Json::parse(std::ifstream(directory/"foley-baseline.json"));Check(baseline["snapshots"]["B0_after_warm"]["app_update_count"]==100,"B0 counter was normalized");
    Check(baseline["snapshots"]["B0_after_warm"]["table"][0]["resource_ids"][1]==11,"B0 stable resource identity missing");
    for(const char* file:{"foley-events.jsonl","foley-baseline.json","foley-trace-health.json"})std::filesystem::remove(directory/file);std::filesystem::remove(directory);
    std::cout<<"Foley real entry/return callbacks: recent/LOOP/reuse/full/create-null/query booleans/variation wrap; app add; GPR/flags/FX/LastError; fault bounds and closed files passed\n";return 0;
}catch(const std::exception& e){std::cerr<<e.what()<<'\n';std::string ignored;RemoveForTest(ignored);return 1;}}
