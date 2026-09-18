#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <tlhelp32.h>
#include <cstdio>
#include <string>
#include <vector>
#include <stdexcept>

namespace {
std::runtime_error Failure(const char* stage) { return std::runtime_error(std::string(stage)+": Win32 error "+std::to_string(GetLastError())); }
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
        if(argc==7&&!wcscmp(argv[1],L"launch")) {
            // launch ENGINE BOOTSTRAP SANDBOX CWD RECEIPT
            SetEnvironmentVariableW(L"LVZ_SANDBOX",argv[4]);
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
            DWORD result=Remote(process,remote,nullptr); FreeLibrary(local);
            if(result) throw std::runtime_error("bootstrap isolation failed: code "+std::to_string(result));
            FILE* receipt=_wfopen(argv[6],L"wb");
            if(!receipt) throw std::runtime_error("cannot write launcher receipt");
            fprintf(receipt,"{\"pid\":%lu,\"creation_time\":%llu,\"isolation_ready\":true}\n",info.dwProcessId,Started(process)); fclose(receipt);
            if(ResumeThread(thread)==static_cast<DWORD>(-1)) throw Failure("ResumeThread");
            printf("{\"pid\":%lu,\"creation_time\":%llu,\"isolation_ready\":true}\n",info.dwProcessId,Started(process));
        } else if(argc==6 && (!wcscmp(argv[1],L"inject")||!wcscmp(argv[1],L"stop"))) {
            DWORD pid=wcstoul(argv[2],nullptr,10); ULONGLONG created=_wcstoui64(argv[4],nullptr,10);
            process=OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION|PROCESS_CREATE_THREAD|PROCESS_VM_OPERATION|PROCESS_VM_READ|PROCESS_VM_WRITE|PROCESS_TERMINATE|SYNCHRONIZE,FALSE,pid);
            if(!process) throw Failure("OpenProcess");
            Verify(process,argv[3],created);
            if(!wcscmp(argv[1],L"inject")) { auto module=Load(process,argv[5]); printf("{\"module\":%lu}\n",module); }
            else { if(!TerminateProcess(process,0)) throw Failure("TerminateProcess"); WaitForSingleObject(process,5000); puts("{\"stopped\":true}"); }
        } else throw std::runtime_error("usage: launch ENGINE BOOTSTRAP SANDBOX CWD RECEIPT | inject PID ENGINE CREATION_TIME DLL | stop PID ENGINE CREATION_TIME unused");
        if(thread) CloseHandle(thread); if(process) CloseHandle(process); return 0;
    } catch(const std::exception& error) {
        if(launched && process) { TerminateProcess(process,100); WaitForSingleObject(process,5000); }
        if(thread) CloseHandle(thread); if(process) CloseHandle(process);
        fprintf(stderr,"launcher: %s\n",error.what()); return 1;
    }
}
