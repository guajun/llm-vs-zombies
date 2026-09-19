#include "launcher/silent_audio.hpp"
#include "launcher/file_hash.hpp"
#include "determinism/silent_audio_audit.hpp"
#include <array>
#include <iostream>
#include <cstring>
#include <thread>
#include <vector>
#include <stdexcept>
extern "C" {
void* audioFixtureEntry;
alignas(16) uint8_t audioFixtureBefore[512]{},audioFixtureAfter[512]{},audioFixtureSave[512]{};
uint32_t audioFixtureRegs[8]{},audioFixtureSp=0,audioFixtureSpAfter=0,audioFixtureFlags=0;
__attribute__((naked)) void AudioFixtureCall(){__asm__ volatile(
    "pushfl\n\tpushal\n\tfxsave _audioFixtureSave\n\tfninit\n\tfldpi\n\tfld1\n\tfxsave _audioFixtureBefore\n\t"
    "movl $0x12345678,%ebx\n\tmovl $0x23456789,%ebp\n\tmovl $0x34567890,%esi\n\tmovl $0x45678901,%edi\n\t"
    "movl $0x11111111,%ecx\n\tmovl $0x22222222,%edx\n\tmovl %esp,_audioFixtureSp\n\t"
    "pushl $7\n\tpushl $0x247\n\tpopfl\n\tcall *_audioFixtureEntry\n\t"
    "movl %esp,_audioFixtureSpAfter\n\tpushfl\n\tpopl _audioFixtureFlags\n\t"
    "movl %eax,_audioFixtureRegs\n\tmovl %ebx,_audioFixtureRegs+4\n\tmovl %ebp,_audioFixtureRegs+8\n\t"
    "movl %esi,_audioFixtureRegs+12\n\tmovl %edi,_audioFixtureRegs+16\n\tmovl %ecx,_audioFixtureRegs+20\n\tmovl %edx,_audioFixtureRegs+24\n\t"
    "fxsave _audioFixtureAfter\n\tfxrstor _audioFixtureSave\n\tpopal\n\tpopfl\n\tretl\n\t");}
}
void Check(bool condition,const char* why){if(!condition)throw std::runtime_error(why);}
using Json=nlohmann::json;
unsigned failure=0;
Json HealthSnapshot(){if(failure)throw std::runtime_error(failure==1?"audio errors/wrong-thread":"audio patch lost/unreadable");return {{"calls",12}};}
DWORD WINAPI HealthQuery(void* value){auto& s=*static_cast<lvz::silentaudio::Status*>(value);s.calls=12;s.errors=failure==1;s.patchOwned=!failure;s.entry=0x5c7650;return 0;}
int main(){using namespace lvz::silentaudio;try{
    SetEnvironmentVariableW(L"LVZ_AUDIO_MODE",nullptr);Check(!Requested(),"default must be off");
    SetEnvironmentVariableW(L"LVZ_AUDIO_MODE",L"wrong");bool threw=false;try{Requested();}catch(...){threw=true;}Check(threw,"unknown mode accepted");
    SetEnvironmentVariableW(L"LVZ_AUDIO_MODE",std::wstring(200,L'x').c_str());threw=false;try{Requested();}catch(...){threw=true;}Check(threw,"oversized mode accepted");
    SetEnvironmentVariableW(L"LVZ_AUDIO_MODE",ModeW);Check(Requested(),"explicit mode rejected");std::string error;
    // Exercise the actual CNG hashing implementation used before activation.
    wchar_t temporary[MAX_PATH],hashFile[MAX_PATH];Check(GetTempPathW(MAX_PATH,temporary)&&GetTempFileNameW(temporary,L"lvz",0,hashFile),"hash fixture path");
    HANDLE file=CreateFileW(hashFile,GENERIC_WRITE,0,nullptr,CREATE_ALWAYS,0,nullptr);DWORD written=0;
    Check(file!=INVALID_HANDLE_VALUE&&WriteFile(file,"abc",3,&written,nullptr)&&written==3,"hash fixture write");CloseHandle(file);
    Check(FileSha256(hashFile)=="ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad","CNG SHA256 mismatch");DeleteFileW(hashFile);
    Check(!Install(nullptr,error),"missing activation accepted");Activation bad;bad.version=2;bad.primaryThread=GetCurrentThreadId()+1;Check(!Install(&bad,error),"wrong activation version accepted");Activation late;late.primaryThread=GetCurrentThreadId();Check(!Install(&late,error),"same-thread/late activation accepted");
    std::vector<uint8_t> image(0x35e000);auto* dos=reinterpret_cast<IMAGE_DOS_HEADER*>(image.data());dos->e_magic=IMAGE_DOS_SIGNATURE;dos->e_lfanew=128;
    auto* nt=reinterpret_cast<IMAGE_NT_HEADERS32*>(image.data()+128);nt->Signature=IMAGE_NT_SIGNATURE;nt->FileHeader.Machine=IMAGE_FILE_MACHINE_I386;
    nt->OptionalHeader.Magic=IMAGE_NT_OPTIONAL_HDR32_MAGIC;nt->OptionalHeader.ImageBase=0x400000;nt->OptionalHeader.SizeOfImage=0x35e000;
    const uint8_t prologue[]={0x55,0x8b,0xec,0x83,0xe4,0xc0,0x6a,0xff,0x68,0xd6,0xf9,0x63,0x00},ret[]={0xc2,4,0},call[]={0xff,0xd0,0x8b,0xf0,0x85,0xf6,0x74,0x72};
    std::memcpy(image.data()+0x1c7650,prologue,13);std::memcpy(image.data()+0x1c772f,ret,3);std::memcpy(image.data()+0x1c77aa,ret,3);std::memcpy(image.data()+0x1151b3,call,8);
    uint32_t target=Target;std::memcpy(image.data()+0x275ff4,&target,4);
    Check(ValidateImage(image.data(),image.size(),true,error),"matching image fixture rejected");
    image[0x1c7658]^=1;Check(!ValidateImage(image.data(),image.size(),true,error),"wrong extended signature accepted");image[0x1c7658]^=1;
    nt->FileHeader.Machine=IMAGE_FILE_MACHINE_AMD64;Check(!ValidateImage(image.data(),image.size(),true,error),"wrong PE accepted");
    auto* page=static_cast<uint8_t*>(VirtualAlloc(nullptr,4096,MEM_COMMIT|MEM_RESERVE,PAGE_EXECUTE_READWRITE));Check(page,"fixture allocation");
    std::memcpy(page,Original,6);page[6]=0xc3;audioFixtureEntry=page;
    Check(InstallFixture(page,error),"fixture install");Check(!InstallFixture(page,error),"double activation");
    Check(RollbackBeforeResume(error),"pre-resume unused rollback rejected");Check(!std::memcmp(page,Original,6),"rollback bytes mismatch");ResetFixture();
    Check(InstallFixture(page,error),"fixture reinstall");Check(SealBeforeResume()==0,"fixture seal");Check(!RollbackBeforeResume(error),"rollback allowed after seal");
    SetLastError(1234);AudioFixtureCall();Check(GetLastError()==1234,"LastError changed");
    const uint32_t expected[]={0,0x12345678,0x23456789,0x34567890,0x45678901,0x11111111,0x22222222};
    Check(!std::memcmp(audioFixtureRegs,expected,sizeof(expected)),"ABI return or preserved register mismatch");
    Check(audioFixtureSp==audioFixtureSpAfter,"ret4 stack cleanup mismatch");Check((audioFixtureFlags&0xcd5)==(0x247&0xcd5),"flags changed");
    Check(!std::memcmp(audioFixtureBefore,audioFixtureAfter,512),"floating environment changed");
    auto status=ReadStatus();Check(status.calls==1&&status.errors==0&&status.patchOwned,"actual call counter missing");
    std::thread other([]{AudioFixtureCall();});other.join();status=ReadStatus();Check(status.calls==2&&status.errors==1,"foreign thread not detected");
    ResetFixture();std::memcpy(page,Original,6);Check(InstallFixture(page,error),"fixture install before unmap");
    VirtualFree(page,0,MEM_RELEASE);Check(!ReadStatus().patchOwned,"unmapped patch not safely rejected");
    // Health must preserve failure evidence while allowing a real recording to
    // close; no healthy cached result is permitted after a persistent fault.
    lvz::determinism::silentaudio::SetHealthFixture(HealthSnapshot,HealthQuery);
    Check(lvz::determinism::silentaudio::Health()["healthy"]==true,"healthy fixture failed");
    for(failure=1;failure<=2;++failure)for(int repeat=0;repeat<2;++repeat){const auto h=lvz::determinism::silentaudio::Health();
        Check(h["healthy"]==false&&h["raw_status"]["calls"]==12&&h["entry_bytes"].is_null(),"fault close missing bounded raw evidence");}
    failure=0;Check(lvz::determinism::silentaudio::Health()["healthy"]==false,"persistent fault was forgotten after recovery");
    std::cout<<"silent audio ABI, mode, signature, lifecycle, counters and unhealthy close fixtures passed\n";return 0;
}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
