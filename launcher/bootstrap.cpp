// Process-local isolation for the pinned 32-bit game. No system-wide hooks.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shlobj.h>
#include <shellapi.h>
#include <string>
#include <cstring>
#include <cstdio>
#include <unordered_set>
#include "silent_audio.hpp"

namespace {
std::string sandbox, registryPrefix;
std::wstring sandboxW;
HWND gameWindow=nullptr;
FILE* audit=nullptr;
SRWLOCK registryLock=SRWLOCK_INIT;
std::unordered_set<HKEY> privateKeys;
bool PrivateKey(HKEY root,const char* name) {
    if((root==HKEY_CURRENT_USER||root==HKEY_LOCAL_MACHINE)&&name&&!_strnicmp(name,"Software\\PopCap",15)) return true;
    AcquireSRWLockShared(&registryLock); bool found=privateKeys.contains(root); ReleaseSRWLockShared(&registryLock); return found;
}
void Track(HKEY key) { AcquireSRWLockExclusive(&registryLock);privateKeys.insert(key);ReleaseSRWLockExclusive(&registryLock); }
void Log(const char* event,const char* value="") { if(audit) { fprintf(audit,"%s\t%s\n",event,value); fflush(audit); } }
bool Popcap(const char* p) { return p && (!_strnicmp(p,"Software\\PopCap",15)); }
std::string Key(HKEY& root,const char* name) {
    if ((root==HKEY_CURRENT_USER||root==HKEY_LOCAL_MACHINE)&&Popcap(name)) {
        std::string mapped=registryPrefix+(root==HKEY_CURRENT_USER?"\\HKCU\\":"\\HKLM\\")+name;
        root=HKEY_CURRENT_USER; Log("registry",mapped.c_str()); return mapped;
    }
    return name?name:"";
}
LSTATUS WINAPI OpenKeyEx(HKEY root,LPCSTR name,DWORD options,REGSAM access,PHKEY key) {
    bool isolated=PrivateKey(root,name);
    auto mapped=Key(root,name); auto result=RegOpenKeyExA(root,mapped.c_str(),options,access,key);
    if(!result&&isolated) Track(*key); return result;
}
LSTATUS WINAPI OpenKey(HKEY root,LPCSTR name,PHKEY key) { return OpenKeyEx(root,name,0,KEY_READ,key); }
LSTATUS WINAPI CreateKeyEx(HKEY root,LPCSTR name,DWORD reserved,LPSTR cls,DWORD options,REGSAM access,LPSECURITY_ATTRIBUTES sa,PHKEY key,LPDWORD disposition) {
    bool isolated=PrivateKey(root,name);
    auto mapped=Key(root,name);
    if(isolated) options|=REG_OPTION_VOLATILE;
    auto result=RegCreateKeyExA(root,mapped.c_str(),reserved,cls,options,access,sa,key,disposition);
    if(!result&&isolated) Track(*key); return result;
}
LSTATUS WINAPI CloseKey(HKEY key) {
    AcquireSRWLockExclusive(&registryLock);privateKeys.erase(key);ReleaseSRWLockExclusive(&registryLock);
    return RegCloseKey(key);
}
HRESULT WINAPI FolderA(HWND window,int folder,HANDLE token,DWORD flags,LPSTR out) {
    if ((folder&0xff)==CSIDL_COMMON_APPDATA||(folder&0xff)==CSIDL_APPDATA||(folder&0xff)==CSIDL_LOCAL_APPDATA) {
        strcpy_s(out,MAX_PATH,(sandbox+"\\appdata").c_str()); Log("folder",out); return S_OK;
    }
    return SHGetFolderPathA(window,folder,token,flags,out);
}
HRESULT WINAPI FolderW(HWND window,int folder,HANDLE token,DWORD flags,LPWSTR out) {
    if ((folder&0xff)==CSIDL_COMMON_APPDATA||(folder&0xff)==CSIDL_APPDATA||(folder&0xff)==CSIDL_LOCAL_APPDATA) {
        wcscpy_s(out,MAX_PATH,(sandboxW+L"\\appdata").c_str()); Log("folder_w"); return S_OK;
    }
    return SHGetFolderPathW(window,folder,token,flags,out);
}
FARPROC WINAPI ProcAddress(HMODULE module,LPCSTR name) {
    if(reinterpret_cast<uintptr_t>(name)>65535) {
        if(!strcmp(name,"SHGetFolderPathA")) return reinterpret_cast<FARPROC>(FolderA);
        if(!strcmp(name,"SHGetFolderPathW")) return reinterpret_cast<FARPROC>(FolderW);
    }
    return GetProcAddress(module,name);
}
HANDLE WINAPI MutexA(LPSECURITY_ATTRIBUTES attributes,BOOL own,LPCSTR name) {
    std::string unique=name?std::string(name)+"-lvz-"+std::to_string(GetCurrentProcessId()):"";
    return CreateMutexA(attributes,own,name?unique.c_str():nullptr);
}
// The game keeps its normal Win32/DirectDraw window, but cannot activate it.
HWND WINAPI WindowA(DWORD ex,LPCSTR cls,LPCSTR title,DWORD style,int x,int y,int w,int h,HWND parent,HMENU menu,HINSTANCE instance,LPVOID data) {
    auto window=CreateWindowExA(ex|WS_EX_NOACTIVATE|WS_EX_TOOLWINDOW,cls,title,style&~WS_VISIBLE,x,y,w,h,parent,menu,instance,data);
    if(!parent && w>=640 && h>=480) { gameWindow=window; Log("window_created_hidden"); }
    return window;
}
HWND WINAPI WindowW(DWORD ex,LPCWSTR cls,LPCWSTR title,DWORD style,int x,int y,int w,int h,HWND parent,HMENU menu,HINSTANCE instance,LPVOID data) {
    auto window=CreateWindowExW(ex|WS_EX_NOACTIVATE|WS_EX_TOOLWINDOW,cls,title,style&~WS_VISIBLE,x,y,w,h,parent,menu,instance,data);
    if(!parent && w>=640 && h>=480) { gameWindow=window; Log("window_created_hidden"); }
    return window;
}
BOOL WINAPI Hidden(HWND window,int) { return ShowWindow(window,SW_HIDE); }
BOOL WINAPI NoForeground(HWND) { return FALSE; }
HWND WINAPI NoFocus(HWND) { return nullptr; }
// The isolated engine needs logical focus for its WidgetManager's internal
// mouse dispatch. This query is virtualized only in its own import table;
// the real foreground/active window and OS input remain untouched.
HWND WINAPI LogicalActiveWindow() { return gameWindow; }
LONG WINAPI NoDisplayChange(DEVMODEA*,DWORD) { Log("blocked_display_change"); return DISP_CHANGE_SUCCESSFUL; }
BOOL WINAPI Parameters(UINT action,UINT param,PVOID value,UINT flags) {
    // Block changing desktop settings; queries still see the ordinary desktop.
    if(action==SPI_SETSCREENSAVERRUNNING) return TRUE;
    return SystemParametersInfoA(action,param,value,flags);
}
int WINAPI MessageA(HWND,LPCSTR text,LPCSTR,UINT) { Log("message_box",text?text:""); return IDCANCEL; }
int WINAPI MessageW(HWND,LPCWSTR text,LPCWSTR,UINT) {
    char buffer[4096]={}; if(text) WideCharToMultiByte(CP_UTF8,0,text,-1,buffer,sizeof(buffer)-1,nullptr,nullptr);
    Log("message_box",buffer); return IDCANCEL;
}
HINSTANCE WINAPI NoShell(HWND,LPCSTR,LPCSTR path,LPCSTR,LPCSTR,INT) { Log("blocked_shell",path?path:""); return reinterpret_cast<HINSTANCE>(SE_ERR_ACCESSDENIED); }
UINT WINAPI NoWinExec(LPCSTR command,UINT) { Log("blocked_winexec",command?command:""); return ERROR_ACCESS_DENIED; }
UINT WINAPI WindowsDirectory(LPSTR out,UINT length) {
    auto result=sandbox+"\\windows";
    if(length<=result.size()) return static_cast<UINT>(result.size()+1);
    memcpy(out,result.c_str(),result.size()+1); return static_cast<UINT>(result.size());
}
struct Hook {const char* name; void* replacement; unsigned count=0;};
Hook hooks[]={
    {"GetProcAddress",reinterpret_cast<void*>(ProcAddress)},
    {"RegOpenKeyExA",reinterpret_cast<void*>(OpenKeyEx)},
    {"RegOpenKeyA",reinterpret_cast<void*>(OpenKey)},
    {"RegCreateKeyExA",reinterpret_cast<void*>(CreateKeyEx)},
    {"RegCloseKey",reinterpret_cast<void*>(CloseKey)},
    {"SHGetFolderPathA",reinterpret_cast<void*>(FolderA)},
    {"SHGetFolderPathW",reinterpret_cast<void*>(FolderW)},
    {"CreateMutexA",reinterpret_cast<void*>(MutexA)},
    {"CreateWindowExA",reinterpret_cast<void*>(WindowA)},
    {"CreateWindowExW",reinterpret_cast<void*>(WindowW)},
    {"ShowWindow",reinterpret_cast<void*>(Hidden)},
    {"SetForegroundWindow",reinterpret_cast<void*>(NoForeground)},
    {"SetActiveWindow",reinterpret_cast<void*>(NoFocus)},
    {"SetFocus",reinterpret_cast<void*>(NoFocus)},
    {"GetActiveWindow",reinterpret_cast<void*>(LogicalActiveWindow)},
    {"ChangeDisplaySettingsA",reinterpret_cast<void*>(NoDisplayChange)},
    {"SystemParametersInfoA",reinterpret_cast<void*>(Parameters)},
    {"MessageBoxA",reinterpret_cast<void*>(MessageA)},
    {"MessageBoxW",reinterpret_cast<void*>(MessageW)},
    {"ShellExecuteA",reinterpret_cast<void*>(NoShell)},
    {"WinExec",reinterpret_cast<void*>(NoWinExec)},
    {"GetWindowsDirectoryA",reinterpret_cast<void*>(WindowsDirectory)},
};
bool Install() {
    auto base=reinterpret_cast<BYTE*>(GetModuleHandleW(nullptr));
    auto dos=reinterpret_cast<IMAGE_DOS_HEADER*>(base);
    auto nt=reinterpret_cast<IMAGE_NT_HEADERS32*>(base+dos->e_lfanew);
    if(dos->e_magic!=IMAGE_DOS_SIGNATURE||nt->Signature!=IMAGE_NT_SIGNATURE||nt->FileHeader.Machine!=IMAGE_FILE_MACHINE_I386) return false;
    auto directory=nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    for(auto imp=reinterpret_cast<IMAGE_IMPORT_DESCRIPTOR*>(base+directory.VirtualAddress);imp->Name;++imp) {
        if(!imp->OriginalFirstThunk) return false;
        auto names=reinterpret_cast<IMAGE_THUNK_DATA32*>(base+imp->OriginalFirstThunk);
        auto slots=reinterpret_cast<IMAGE_THUNK_DATA32*>(base+imp->FirstThunk);
        for(;names->u1.AddressOfData;++names,++slots) {
            if(IMAGE_SNAP_BY_ORDINAL32(names->u1.Ordinal)) continue;
            auto name=reinterpret_cast<IMAGE_IMPORT_BY_NAME*>(base+names->u1.AddressOfData)->Name;
            for(auto& hook:hooks) if(!strcmp(name,hook.name)) {
                DWORD old; if(!VirtualProtect(&slots->u1.Function,sizeof(DWORD),PAGE_READWRITE,&old)) return false;
                slots->u1.Function=reinterpret_cast<DWORD>(hook.replacement);
                DWORD ignored; VirtualProtect(&slots->u1.Function,sizeof(DWORD),old,&ignored);
                ++hook.count; Log("hook",hook.name);
            }
        }
    }
    for(auto& hook:hooks) if(!strcmp(hook.name,"GetProcAddress")||!strcmp(hook.name,"RegOpenKeyExA")||!strcmp(hook.name,"RegCreateKeyExA")||!strcmp(hook.name,"ShowWindow")||!strcmp(hook.name,"CreateWindowExA")) if(!hook.count) return false;
    return true;
}
void SeedRegistry() {
    auto path=registryPrefix+"\\HKCU\\Software\\PopCap\\PlantsVsZombies";
    HKEY key=nullptr;
    if(RegCreateKeyExA(HKEY_CURRENT_USER,path.c_str(),0,nullptr,REG_OPTION_VOLATILE,KEY_ALL_ACCESS,nullptr,&key,nullptr)!=ERROR_SUCCESS) throw 1;
    for(auto name:{"ScreenMode","MusicVolume","SfxVolume","Is3D","InProgress","WaitForVSync"}) { DWORD value=0; RegSetValueExA(key,name,0,REG_DWORD,reinterpret_cast<BYTE*>(&value),sizeof(value)); }
    const char user[]="Experiment"; RegSetValueExA(key,"CurUser",0,REG_SZ,reinterpret_cast<const BYTE*>(user),sizeof(user));
    RegCloseKey(key);
}
}
extern "C" __declspec(dllexport) DWORD WINAPI LvzBootstrapStart(void* activation) {
    try {
        wchar_t path[MAX_PATH]={};
        DWORD size=GetEnvironmentVariableW(L"LVZ_SANDBOX",path,MAX_PATH);
        if(!size||size>=MAX_PATH-40) return 10;
        sandboxW=path;
        char narrow[MAX_PATH]={}; BOOL substituted=FALSE;
        if(!WideCharToMultiByte(CP_ACP,WC_NO_BEST_FIT_CHARS,path,-1,narrow,sizeof(narrow),nullptr,&substituted)||substituted) return 11;
        sandbox=narrow;
        FILETIME created,exit,kernel,user;
        if(!GetProcessTimes(GetCurrentProcess(),&created,&exit,&kernel,&user)) return 15;
        auto identity=(static_cast<unsigned long long>(created.dwHighDateTime)<<32)|created.dwLowDateTime;
        registryPrefix="Software\\LLMVsZombies\\"+std::to_string(GetCurrentProcessId())+"-"+std::to_string(identity);
        audit=fopen((sandbox+"\\bootstrap.log").c_str(),"wb");
        if(!audit) return 12;
        std::string audioError;
        if(!lvz::silentaudio::Install(static_cast<lvz::silentaudio::Activation*>(activation),audioError)) { Log("audio_failed",audioError.c_str()); return 16; }
        SeedRegistry();
        if(!Install()) { std::string ignored; lvz::silentaudio::RollbackBeforeResume(ignored); Log("isolation_failed"); return 13; }
        Log("isolation_ready",registryPrefix.c_str());
        return 0;
    } catch(...) { return 14; }
}
BOOL WINAPI DllMain(HINSTANCE instance,DWORD reason,LPVOID) {
    if(reason==DLL_PROCESS_ATTACH) DisableThreadLibraryCalls(instance);
    return TRUE;
}
