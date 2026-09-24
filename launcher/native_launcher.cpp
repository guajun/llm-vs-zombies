#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <tlhelp32.h>
#include <cstdio>
#include <string>
#include <vector>
#include <stdexcept>
#include <nlohmann/json.hpp>
#include "branch_id.hpp"
#include "silent_audio.hpp"

namespace {
std::runtime_error Failure(const char* stage) { return std::runtime_error(std::string(stage)+": Win32 error "+std::to_string(GetLastError())); }
// Branch ids are ASCII by grammar. Convert without assuming a code page so a
// non-ASCII argument fails instead of turning into a different id.
std::string NarrowAscii(const wchar_t* value,const char* what) {
    std::string result;
    for(;*value;++value) {
        if(*value<0x20||*value>0x7e) throw std::runtime_error(std::string(what)+" must be printable ASCII");
        result.push_back(static_cast<char>(*value));
    }
    return result;
}
DWORD Remote(HANDLE process,LPTHREAD_START_ROUTINE function,void* argument) {
    HANDLE thread=CreateRemoteThread(process,nullptr,0,function,argument,0,nullptr);
    if(!thread) throw Failure("CreateRemoteThread");
    DWORD status=WaitForSingleObject(thread,10000),result=0;
    if(status!=WAIT_OBJECT_0) { CloseHandle(thread); throw std::runtime_error("remote initialization did not finish within 10 seconds"); }
    if(!GetExitCodeThread(thread,&result)) { CloseHandle(thread); throw Failure("GetExitCodeThread"); }
    CloseHandle(thread); return result;
}
DWORD Load(HANDLE process,const wchar_t* path) {
    auto size=(wcslen(path)+1)*sizeof(wchar_t);
    auto remote=VirtualAllocEx(process,nullptr,size,MEM_COMMIT|MEM_RESERVE,PAGE_READWRITE);
    if(!remote) throw Failure("VirtualAllocEx");
    if(!WriteProcessMemory(process,remote,path,size,nullptr)) throw Failure("WriteProcessMemory");
    // Launcher and game are both i386. System DLL address is shared on this boot.
    DWORD module=Remote(process,reinterpret_cast<LPTHREAD_START_ROUTINE>(GetProcAddress(GetModuleHandleW(L"kernel32.dll"),"LoadLibraryW")),remote);
    VirtualFreeEx(process,remote,0,MEM_RELEASE);
    if(!module) throw std::runtime_error("remote LoadLibraryW returned NULL");
    return module;
}
ULONGLONG Started(HANDLE process) {
    FILETIME created,exit,kernel,user;
    if(!GetProcessTimes(process,&created,&exit,&kernel,&user)) throw Failure("GetProcessTimes");
    return (static_cast<ULONGLONG>(created.dwHighDateTime)<<32)|created.dwLowDateTime;
}
void Verify(HANDLE process,const wchar_t* expected,ULONGLONG created) {
    wchar_t actual[32768]; DWORD size=32768;
    if(!QueryFullProcessImageNameW(process,0,actual,&size)||_wcsicmp(expected,actual)||Started(process)!=created)
        throw std::runtime_error("PID identity mismatch: refusing to operate on unrelated process");
}
}
int wmain(int argc,wchar_t** argv) {
    HANDLE process=nullptr,thread=nullptr;
    bool launched=false;
    try {
        if((argc==8||argc==9)&&!wcscmp(argv[1],L"launch")) {
            // launch ENGINE BOOTSTRAP SANDBOX CWD RECEIPT BRANCH [AUDIO_MODE]
            const bool silent=argc==9;
            if(silent&&wcscmp(argv[8],lvz::silentaudio::ModeW)) throw std::runtime_error("unsupported audio mode");
            // Validate before anything is created or reconfigured: an illegal
            // or oversized branch id fails the launch instead of silently
            // leaving the run on the runtime's process-instance label.
            const std::string branch=NarrowAscii(argv[7],"branch id");
            lvz::branch::Require(branch);
            if(!SetEnvironmentVariableW(L"LVZ_BRANCH_ID",argv[7]))throw Failure("branch environment");
            if(!SetEnvironmentVariableW(L"LVZ_AUDIO_MODE",silent?lvz::silentaudio::ModeW:L"original"))throw Failure("audio environment");
            if(!SetEnvironmentVariableW(L"LVZ_SANDBOX",argv[4]))throw Failure("sandbox environment");
            STARTUPINFOW startup{}; startup.cb=sizeof(startup); startup.dwFlags=STARTF_USESHOWWINDOW; startup.wShowWindow=SW_HIDE;
            PROCESS_INFORMATION info{};
            std::wstring command=L"\""+std::wstring(argv[2])+L"\"";
            if(!CreateProcessW(argv[2],command.data(),nullptr,nullptr,FALSE,CREATE_SUSPENDED|CREATE_UNICODE_ENVIRONMENT,nullptr,argv[5],&startup,&info)) throw Failure("CreateProcessW");
            process=info.hProcess; thread=info.hThread; launched=true;
            DWORD module=Load(process,argv[3]);
            auto local=LoadLibraryExW(argv[3],nullptr,DONT_RESOLVE_DLL_REFERENCES);
            if(!local) throw Failure("LoadLibraryExW bootstrap exports");
            auto entry=GetProcAddress(local,"LvzBootstrapStart@4");
            if(!entry) entry=GetProcAddress(local,"LvzBootstrapStart");
            if(!entry) throw Failure("bootstrap export");
            auto remote=reinterpret_cast<LPTHREAD_START_ROUTINE>(module+reinterpret_cast<DWORD>(entry)-reinterpret_cast<DWORD>(local));
            lvz::silentaudio::Activation activation;activation.primaryThread=info.dwThreadId;
            void* remoteData=nullptr;
            if(silent) {
                remoteData=VirtualAllocEx(process,nullptr,sizeof(lvz::silentaudio::Status),MEM_COMMIT|MEM_RESERVE,PAGE_READWRITE);
                if(!remoteData||!WriteProcessMemory(process,remoteData,&activation,sizeof(activation),nullptr)) throw Failure("audio activation contract");
                if(!GetProcAddress(local,"LvzAudioStatus@4")&&!GetProcAddress(local,"LvzAudioStatus")) throw std::runtime_error("bootstrap lacks required audio contract");
            }
            DWORD result=Remote(process,remote,remoteData);
            nlohmann::json audio;
            if(!result&&silent) {
                auto seal=GetProcAddress(local,"LvzAudioSeal@4");if(!seal)seal=GetProcAddress(local,"LvzAudioSeal");
                auto query=GetProcAddress(local,"LvzAudioStatus@4");if(!query)query=GetProcAddress(local,"LvzAudioStatus");
                if(!seal||!query)throw std::runtime_error("bootstrap audio exports unavailable");
                auto relocated=[&](FARPROC address){return reinterpret_cast<LPTHREAD_START_ROUTINE>(module+reinterpret_cast<DWORD>(address)-reinterpret_cast<DWORD>(local));};
                if(Remote(process,relocated(seal),nullptr))throw std::runtime_error("audio pre-resume seal failed");
                lvz::silentaudio::Status status;
                if(!WriteProcessMemory(process,remoteData,&status,sizeof(status),nullptr)||Remote(process,relocated(query),remoteData)
                    ||!ReadProcessMemory(process,remoteData,&status,sizeof(status),nullptr))throw Failure("audio activation receipt");
                if(status.magic!=lvz::silentaudio::Magic||status.version!=1||!status.enabled||!status.installed||status.phase!=2
                    ||!status.pinned||!status.patchOwned||status.ownerModule!=module||status.primaryThread!=info.dwThreadId
                    ||status.calls||status.errors||status.preexistingApp)throw std::runtime_error("audio pre-resume receipt rejected");
                audio={{"mode",lvz::silentaudio::Mode},{"installed",true},{"before_primary_thread_resume",true},{"phase","sealed_before_resume"},
                    {"primary_thread",status.primaryThread},{"owner_module",status.ownerModule},{"owner_pinned",true},
                    {"entry",status.entry},{"replacement",status.replacement},{"patch_owned",true},{"calls",status.calls},{"errors",status.errors},
                    {"preexisting_app",status.preexistingApp},{"original_bytes",status.original},{"patch_bytes",status.patch},
                    {"engine_sha256",status.engineSha256},{"bootstrap_sha256",status.bootstrapSha256}};
            }
            if(remoteData)VirtualFreeEx(process,remoteData,0,MEM_RELEASE);
            FreeLibrary(local);
            if(result) throw std::runtime_error("bootstrap isolation failed: code "+std::to_string(result));
            FILE* receipt=_wfopen(argv[6],L"wb");
            if(!receipt) throw std::runtime_error("cannot write launcher receipt");
            nlohmann::json output={{"pid",info.dwProcessId},{"creation_time",Started(process)},
                {"branch_id",branch},{"branch_channel","LVZ_BRANCH_ID"},{"isolation_ready",true}};
            if(silent)output["audio_activation"]=audio;
            const bool written=fprintf(receipt,"%s\n",output.dump().c_str())>=0 && fflush(receipt)==0;
            const bool closed=fclose(receipt)==0;
            if(!written||!closed)throw std::runtime_error("launcher receipt write/close failed before resume");
            if(ResumeThread(thread)==static_cast<DWORD>(-1)) throw Failure("ResumeThread");
            printf("%s\n",output.dump().c_str());
        } else if(argc==6 && (!wcscmp(argv[1],L"inject")||!wcscmp(argv[1],L"stop"))) {
            DWORD pid=wcstoul(argv[2],nullptr,10); ULONGLONG created=_wcstoui64(argv[4],nullptr,10);
            process=OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION|PROCESS_CREATE_THREAD|PROCESS_VM_OPERATION|PROCESS_VM_READ|PROCESS_VM_WRITE|PROCESS_TERMINATE|SYNCHRONIZE,FALSE,pid);
            if(!process) throw Failure("OpenProcess");
            Verify(process,argv[3],created);
            if(!wcscmp(argv[1],L"inject")) { auto module=Load(process,argv[5]); printf("{\"module\":%lu}\n",module); }
            else { if(!TerminateProcess(process,0)) throw Failure("TerminateProcess"); WaitForSingleObject(process,5000); puts("{\"stopped\":true}"); }
        } else throw std::runtime_error("usage: launch ENGINE BOOTSTRAP SANDBOX CWD RECEIPT BRANCH [AUDIO_MODE] | inject PID ENGINE CREATION_TIME DLL | stop PID ENGINE CREATION_TIME unused");
        if(thread) CloseHandle(thread); if(process) CloseHandle(process); return 0;
    } catch(const std::exception& error) {
        if(launched && process) { TerminateProcess(process,100); WaitForSingleObject(process,5000); }
        if(thread) CloseHandle(thread); if(process) CloseHandle(process);
        fprintf(stderr,"launcher: %s\n",error.what()); return 1;
    }
}
