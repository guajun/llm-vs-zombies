#include "silent_audio.hpp"
#include "file_hash.hpp"
#include <array>
#include <cstring>
#include <stdexcept>
#include <vector>

extern "C" {
alignas(4) volatile LONG lvzSilentCalls=0,lvzSilentErrors=0;
DWORD lvzSilentPrimary=0;
// ECX=this, one stack sound id; callee pops exactly 4. No original RNG or
// gameplay data is touched. Integer-only thunk leaves the entire FP state alone.
__attribute__((naked)) void LvzSilentGetSoundInstance() {
    __asm__ volatile("pushfl\n\tpushl %eax\n\tmovl %fs:0x24,%eax\n\tcmpl _lvzSilentPrimary,%eax\n\tje 2f\n\tlock incl _lvzSilentErrors\n\t2:\n\tpopl %eax\n\tlock incl _lvzSilentCalls\n\tjnz 1f\n\tlock incl _lvzSilentErrors\n\t1:\n\tpopfl\n\tmovl $0,%eax\n\tretl $4\n\t");
}
}
namespace lvz::silentaudio {
namespace {
Status status;
uint8_t* site=nullptr;
bool fixture=false;
bool Same(const void* a,const void* b,size_t n){
    uintptr_t at=reinterpret_cast<uintptr_t>(a);if(!at||at>UINTPTR_MAX-n)return false;
    const auto end=at+n;
    while(at<end){MEMORY_BASIC_INFORMATION info{};if(!VirtualQuery(reinterpret_cast<void*>(at),&info,sizeof(info))
        ||info.State!=MEM_COMMIT||(info.Protect&(PAGE_GUARD|PAGE_NOACCESS)))return false;
        const auto protection=info.Protect&0xff;
        if(protection!=PAGE_READONLY&&protection!=PAGE_READWRITE&&protection!=PAGE_WRITECOPY
            &&protection!=PAGE_EXECUTE_READ&&protection!=PAGE_EXECUTE_READWRITE&&protection!=PAGE_EXECUTE_WRITECOPY)return false;
        const auto next=reinterpret_cast<uintptr_t>(info.BaseAddress)+info.RegionSize;
        if(next<=at)return false;at=next;
    }return !std::memcmp(a,b,n);
}
bool Put(const uint8_t* bytes,std::string& error) {
    DWORD old=0;if(!VirtualProtect(site,6,PAGE_EXECUTE_READWRITE,&old)){error="audio patch protection failed";return false;}
    std::memcpy(site,bytes,6);
    DWORD ignored=0;const bool restored=VirtualProtect(site,6,old,&ignored)!=0;
    const bool flushed=FlushInstructionCache(GetCurrentProcess(),site,6)!=0;
    if(!restored||!flushed){error="audio patch protection/cache restoration failed";return false;}return true;
}
bool InstallAt(uint8_t* target,std::string& error) {
    if(status.phase||status.installed){error="audio activation is one-shot";return false;}
    if(!Same(target,Original,6)){error="GetSoundInstance signature mismatch";return false;}
    site=target;status.entry=reinterpret_cast<uintptr_t>(site);
    status.replacement=reinterpret_cast<uintptr_t>(&LvzSilentGetSoundInstance);
    std::memcpy(status.original,Original,6);status.patch[0]=0xe9;
    const uint32_t relative=status.replacement-(status.entry+5);
    std::memcpy(status.patch+1,&relative,4);status.patch[5]=0x90;
    if(!Put(status.patch,error))return false;
    status.enabled=status.installed=1;status.phase=1;return true;
}
}
bool Requested() {
    wchar_t value[96]{};const DWORD n=GetEnvironmentVariableW(L"LVZ_AUDIO_MODE",value,96);
    if(n>=96)throw std::runtime_error("unsupported LVZ_AUDIO_MODE length");
    if(!n||!wcscmp(value,L"original"))return false;
    if(wcscmp(value,ModeW))throw std::runtime_error("unsupported LVZ_AUDIO_MODE");return true;
}
bool ValidateImage(const uint8_t* base,size_t length,bool pristine,std::string& error) {
    auto fail=[&](const char* why){error=why;return false;};
    if(!base||length<ImageSize)return fail("audio image size mismatch");
    IMAGE_DOS_HEADER dos;std::memcpy(&dos,base,sizeof(dos));
    if(dos.e_magic!=IMAGE_DOS_SIGNATURE||dos.e_lfanew<64||dos.e_lfanew>0x1000)return fail("audio DOS header mismatch");
    IMAGE_NT_HEADERS32 nt;std::memcpy(&nt,base+dos.e_lfanew,sizeof(nt));
    if(nt.Signature!=IMAGE_NT_SIGNATURE||nt.FileHeader.Machine!=IMAGE_FILE_MACHINE_I386
        ||nt.OptionalHeader.Magic!=IMAGE_NT_OPTIONAL_HDR32_MAGIC||nt.OptionalHeader.ImageBase!=0x400000
        ||nt.OptionalHeader.SizeOfImage!=ImageSize)return fail("audio PE identity mismatch");
    // Locked RVA evidence includes the complete entry, both ret-4 exits, original
    // virtual slot, and Foley's call/null branch. No guessed function address.
    constexpr uint8_t entry[]={0x55,0x8b,0xec,0x83,0xe4,0xc0,0x6a,0xff,0x68,0xd6,0xf9,0x63,0x00};
    constexpr uint8_t ret[]={0xc2,0x04,0x00};
    constexpr uint8_t call[]={0xff,0xd0,0x8b,0xf0,0x85,0xf6,0x74,0x72};
    uint32_t slot;std::memcpy(&slot,base+0x275ff4,4);
    if((pristine&&!Same(base+0x1c7650,entry,sizeof(entry)))||!Same(base+0x1c772f,ret,3)
        ||!Same(base+0x1c77aa,ret,3)||!Same(base+0x1151b3,call,sizeof(call))||slot!=Target)
        return fail("audio target instruction/vtable mismatch");
    return true;
}
bool Install(const Activation* activation,std::string& error) {
    try {
        if(!Requested())return true;
        if(!activation||activation->size!=sizeof(Activation)||activation->magic!=Magic
            ||activation->version!=Version||!activation->primaryThread||activation->primaryThread==GetCurrentThreadId())
            throw std::runtime_error("audio requires suspended-primary activation contract");
        auto base=reinterpret_cast<uint8_t*>(GetModuleHandleW(nullptr));
        if(reinterpret_cast<uintptr_t>(base)!=0x400000)throw std::runtime_error("audio image relocation unsupported");
        MEMORY_BASIC_INFORMATION region{};
        if(!VirtualQuery(base,&region,sizeof(region))||region.AllocationBase!=base)throw std::runtime_error("audio image mapping unavailable");
        if(!ValidateImage(base,ImageSize,true,error))return false;
        // LawnApp and SexyApp must not have been constructed. There is no late
        // reset/StopAllSounds fallback. Launcher still owns the suspended thread.
        uint32_t app=0,sexy=0;std::memcpy(&app,base+0x2a9ec0,4);std::memcpy(&sexy,base+0x2a9f38,4);
        status.preexistingApp=app|sexy;
        if(status.preexistingApp)throw std::runtime_error("audio activation too late: App already exists");
        wchar_t path[32768]{};
        if(!GetModuleFileNameW(nullptr,path,32768)||FileSha256(path)!=EngineSha256)throw std::runtime_error("audio locked engine SHA-256 mismatch");
        HMODULE owner=nullptr;
        if(!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_PIN,
            reinterpret_cast<LPCWSTR>(&LvzSilentGetSoundInstance),&owner))throw std::runtime_error("audio owner module pin failed");
        status.ownerModule=reinterpret_cast<uintptr_t>(owner);status.pinned=1;status.primaryThread=activation->primaryThread;lvzSilentPrimary=activation->primaryThread;
        if(!GetModuleFileNameW(owner,path,32768))throw std::runtime_error("audio owner path unavailable");
        const auto own=FileSha256(path);std::memcpy(status.engineSha256,EngineSha256,65);std::memcpy(status.bootstrapSha256,own.c_str(),65);
        return InstallAt(base+0x1c7650,error);
    }catch(const std::exception& e){error=e.what();return false;}
}
bool RollbackBeforeResume(std::string& error) {
    if(!status.installed)return true;
    if(status.phase!=1||InterlockedCompareExchange(&lvzSilentCalls,0,0)!=0){error="audio rollback forbidden after seal/use";return false;}
    if(!Same(site,status.patch,6)){error="audio patch ownership lost";return false;}
    if(!Put(Original,error))return false;
    status.installed=status.enabled=0;status.phase=3;return true;
}
DWORD SealBeforeResume() noexcept {
    if(!status.enabled)return 0;
    if(status.phase!=1||!status.pinned||!Same(site,status.patch,6)||InterlockedCompareExchange(&lvzSilentCalls,0,0))return 1;
    status.phase=2;return 0;
}
Status ReadStatus() noexcept {
    Status value=status;value.calls=InterlockedCompareExchange(&lvzSilentCalls,0,0);
    value.errors=InterlockedCompareExchange(&lvzSilentErrors,0,0);
    value.patchOwned=site&&Same(site,status.patch,6);
    return value;
}
#ifdef LVZ_SILENT_AUDIO_TESTING
bool InstallFixture(uint8_t* entry,std::string& error){fixture=true;status.pinned=1;lvzSilentPrimary=GetCurrentThreadId();return InstallAt(entry,error);}
void ResetFixture(){if(!fixture)throw std::runtime_error("not fixture");if(site&&status.installed){std::string error;if(!Put(Original,error))throw std::runtime_error(error);}status=Status{};site=nullptr;lvzSilentCalls=0;lvzSilentErrors=0;}
void* FixtureReplacement(){return reinterpret_cast<void*>(&LvzSilentGetSoundInstance);}
#endif
}
extern "C" __declspec(dllexport) DWORD WINAPI LvzAudioStatus(void* output) {
    if(!output)return 1;auto* result=static_cast<lvz::silentaudio::Status*>(output);
    if(result->size!=sizeof(*result))return 2;*result=lvz::silentaudio::ReadStatus();return 0;
}
extern "C" __declspec(dllexport) DWORD WINAPI LvzAudioSeal(void*) {return lvz::silentaudio::SealBeforeResume();}
