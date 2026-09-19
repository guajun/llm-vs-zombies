#include "launcher/silent_audio.hpp"
#include "launcher/file_hash.hpp"
#include "determinism/silent_audio_audit.hpp"
#include <array>
#include <fstream>
#include <filesystem>
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
int main(int argc,char** argv){using namespace lvz::silentaudio;try{
    if(argc==3&&!std::strcmp(argv[1],"--verify-pe")){
        const std::filesystem::path path=argv[2];const auto hash=FileSha256(path.wstring().c_str());
        Check(hash==EngineSha256,"offline locked engine hash mismatch");
        std::ifstream input(path,std::ios::binary);std::vector<uint8_t> file((std::istreambuf_iterator<char>(input)),{});
        Check(file.size()>sizeof(IMAGE_DOS_HEADER),"offline truncated DOS");
        IMAGE_DOS_HEADER dos{};std::memcpy(&dos,file.data(),sizeof(dos));
        Check(dos.e_lfanew>=0&&size_t(dos.e_lfanew)+sizeof(IMAGE_NT_HEADERS32)<=file.size(),"offline truncated NT headers");
        IMAGE_NT_HEADERS32 nt{};std::memcpy(&nt,file.data()+dos.e_lfanew,sizeof(nt));
        Check(nt.OptionalHeader.SizeOfImage==ImageSize&&nt.OptionalHeader.SizeOfHeaders<=file.size(),"offline PE image extent");
        std::vector<uint8_t> image(nt.OptionalHeader.SizeOfImage);
        std::memcpy(image.data(),file.data(),nt.OptionalHeader.SizeOfHeaders);
        const auto sectionOffset=dos.e_lfanew+24+nt.FileHeader.SizeOfOptionalHeader;Json sections=Json::array();
        for(unsigned i=0;i<nt.FileHeader.NumberOfSections;++i){
            IMAGE_SECTION_HEADER section{};const auto offset=sectionOffset+i*sizeof(section);
            Check(offset+sizeof(section)<=file.size(),"offline section header bounds");std::memcpy(&section,file.data()+offset,sizeof(section));
            Check(uint64_t(section.PointerToRawData)+section.SizeOfRawData<=file.size()
                &&uint64_t(section.VirtualAddress)+section.SizeOfRawData<=image.size(),"offline section file/memory bounds");
            std::memcpy(image.data()+section.VirtualAddress,file.data()+section.PointerToRawData,section.SizeOfRawData);
            char name[9]{};std::memcpy(name,section.Name,8);
            sections.push_back({{"name",name},{"rva",section.VirtualAddress},{"virtual_size",section.Misc.VirtualSize},
                {"raw_size",section.SizeOfRawData},{"raw_offset",section.PointerToRawData}});
        }
        std::string error;Check(ValidateImage(image.data(),image.size(),true,error),error.c_str());
        unsigned types=0,references=0;uint32_t minimum=UINT32_MAX,maximum=0;
        for(;types<110;++types){const auto p=image.data()+0x29fad0+types*0x34;uint32_t kind=0;std::memcpy(&kind,p,4);if(kind!=types)break;
            for(unsigned i=0;i<10;++i){uint32_t address=0;std::memcpy(&address,p+8+i*4,4);if(!address)continue;
                Check(address>=ImageBase&&SoundIdRvaValid(address-ImageBase),"offline sound ID pointer not in declared data extent");
                ++references;minimum=std::min(minimum,address);maximum=std::max(maximum,address);}}
        std::cout<<Json{{"schema","lvz.silent-audio-pe-validation.v1"},{"engine_sha256",hash},{"file_bytes",file.size()},
            {"header",{{"e_lfanew",dos.e_lfanew},{"signature",nt.Signature},{"machine",nt.FileHeader.Machine},
                {"optional_magic",nt.OptionalHeader.Magic},{"image_base",nt.OptionalHeader.ImageBase},
                {"size_of_image",nt.OptionalHeader.SizeOfImage},{"size_of_headers",nt.OptionalHeader.SizeOfHeaders}}},
            {"sections",sections},{"compiled_validate_image",true},{"mapped_without_execution",true},
            {"foley_parameter_types",types},{"sound_id_references",references},{"sound_id_min_va",minimum},{"sound_id_max_va",maximum},
            {"sound_id_data_begin_rva",SoundIdDataBeginRva},{"sound_id_data_end_rva",SoundIdDataEndRva},
            {"game_started",false}}.dump(2)<<'\n';return 0;
    }
    Check(argc==1,"usage: silent_audio_tests [--verify-pe FILE]");
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
    std::vector<uint8_t> image(ImageSize);auto* dos=reinterpret_cast<IMAGE_DOS_HEADER*>(image.data());dos->e_magic=IMAGE_DOS_SIGNATURE;dos->e_lfanew=128;
    auto* nt=reinterpret_cast<IMAGE_NT_HEADERS32*>(image.data()+128);nt->Signature=IMAGE_NT_SIGNATURE;nt->FileHeader.Machine=IMAGE_FILE_MACHINE_I386;
    nt->OptionalHeader.Magic=IMAGE_NT_OPTIONAL_HDR32_MAGIC;nt->OptionalHeader.ImageBase=0x400000;nt->OptionalHeader.SizeOfImage=ImageSize;
    const uint8_t prologue[]={0x55,0x8b,0xec,0x83,0xe4,0xc0,0x6a,0xff,0x68,0xd6,0xf9,0x63,0x00},ret[]={0xc2,4,0},call[]={0xff,0xd0,0x8b,0xf0,0x85,0xf6,0x74,0x72};
    std::memcpy(image.data()+0x1c7650,prologue,13);std::memcpy(image.data()+0x1c772f,ret,3);std::memcpy(image.data()+0x1c77aa,ret,3);std::memcpy(image.data()+0x1151b3,call,8);
    uint32_t target=Target;std::memcpy(image.data()+0x275ff4,&target,4);
    Check(ValidateImage(image.data(),image.size(),true,error),"matching image fixture rejected");
    Check(!ValidateImage(image.data(),0x35e000,true,error),"old truncated dump extent accepted as whole image");
    nt->OptionalHeader.SizeOfImage=0x35e000;Check(!ValidateImage(image.data(),image.size(),true,error),"old incorrect SizeOfImage accepted");nt->OptionalHeader.SizeOfImage=ImageSize;
    Check(SoundIdRvaValid(SoundIdDataBeginRva)&&SoundIdRvaValid(SoundIdDataEndRva-4),"valid data word edges rejected");
    Check(!SoundIdRvaValid(SoundIdDataBeginRva-1)&&!SoundIdRvaValid(SoundIdDataEndRva-3)&&!SoundIdRvaValid(0x35e000),"partial data word/resource accepted");
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
