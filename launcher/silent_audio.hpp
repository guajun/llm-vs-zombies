#pragma once
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <cstdint>
#include <string>
namespace lvz::silentaudio {
inline constexpr char Mode[]="sound_effects_allocation_none_v1";
inline constexpr wchar_t ModeW[]=L"sound_effects_allocation_none_v1";
inline constexpr char EngineSha256[]="f9669af338964787a3785a7895791297d599295b8bb669b0db49443f736a1322";
// Locked file PE SizeOfImage includes .rsrc; the earlier 0x35e000 dump
// intentionally stopped at .rsrc and is not the full image size.
inline constexpr uint32_t ImageBase=0x400000,ImageSize=0x394000;
// Sound ID pointers refer to uint32 globals in .data (including its zero-fill
// tail), not arbitrary resources/code or the entire image. End is exclusive.
inline constexpr uint32_t SoundIdDataBeginRva=0x299000,SoundIdDataEndRva=0x35dc1c;
inline constexpr bool SoundIdRvaValid(uint32_t rva){return rva>=SoundIdDataBeginRva&&rva<=SoundIdDataEndRva-4;}
inline constexpr uint32_t Magic=0x41565a4c,Version=1,Target=0x5c7650;
inline constexpr uint8_t Original[]={0x55,0x8b,0xec,0x83,0xe4,0xc0};
struct Activation {uint32_t size=sizeof(Activation),magic=Magic,version=Version,primaryThread=0;};
// Exported POD only. Pointer values are raw process evidence, never cross-run identity.
struct Status {
    uint32_t size=sizeof(Status),magic=Magic,version=Version,enabled=0,installed=0;
    uint32_t phase=0,pinned=0,primaryThread=0,ownerModule=0,entry=Target,replacement=0;
    uint32_t calls=0,errors=0,patchOwned=0,preexistingApp=0;
    uint8_t original[6]{},patch[6]{};
    char engineSha256[65]{},bootstrapSha256[65]{};
};
using Query=DWORD (WINAPI*)(void*);
bool Requested(); // Rejects unknown environment values; absent/original means off.
std::string FileSha256(const wchar_t* path);
bool ValidateImage(const uint8_t* base,size_t length,bool pristine,std::string& error);
bool Install(const Activation* activation,std::string& error);
bool RollbackBeforeResume(std::string& error);
DWORD SealBeforeResume() noexcept;
Status ReadStatus() noexcept;
#ifdef LVZ_SILENT_AUDIO_TESTING
bool InstallFixture(uint8_t* entry,std::string& error);
void ResetFixture();
void* FixtureReplacement();
#endif
}
extern "C" __declspec(dllexport) DWORD WINAPI LvzAudioStatus(void* output);
extern "C" __declspec(dllexport) DWORD WINAPI LvzAudioSeal(void*);
