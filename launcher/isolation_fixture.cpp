#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shlobj.h>
#include <cstdio>
#include <string>
int main() {
    char root[MAX_PATH]={};GetEnvironmentVariableA("LVZ_SANDBOX",root,MAX_PATH);
    FILE* result=fopen((std::string(root)+"\\fixture-result.txt").c_str(),"wb"); if(!result) return 2;
    auto shell=LoadLibraryA("shell32.dll");
    auto folder=reinterpret_cast<HRESULT(WINAPI*)(HWND,int,HANDLE,DWORD,LPSTR)>(GetProcAddress(shell,"SHGetFolderPathA"));
    char path[MAX_PATH]={};HRESULT hr=folder(nullptr,CSIDL_COMMON_APPDATA,nullptr,0,path);
    fprintf(result,"folder=%s\nhr=%ld\n",path,hr);
    bool okay=hr==S_OK&&std::string(path)==std::string(root)+"\\appdata";
    HKEY key=nullptr;
    auto status=RegCreateKeyExA(HKEY_CURRENT_USER,"Software\\PopCap\\LVZ-IsolationFixture",0,nullptr,0,KEY_ALL_ACCESS,nullptr,&key,nullptr);
    fprintf(result,"create_key=%ld\n",status);okay=okay&&status==ERROR_SUCCESS;
    if(!status) {
        HKEY child=nullptr;
        auto childStatus=RegCreateKeyExA(key,"Child",0,nullptr,0,KEY_ALL_ACCESS,nullptr,&child,nullptr);
        fprintf(result,"create_child_key=%ld\n",childStatus);okay=okay&&childStatus==ERROR_SUCCESS;
        if(!childStatus)RegCloseKey(child);RegCloseKey(key);
    }
    status=RegOpenKeyExA(HKEY_CURRENT_USER,"Software\\PopCap\\PlantsVsZombies",0,KEY_READ,&key);
    fprintf(result,"open_key=%ld\n",status);okay=okay&&status==ERROR_SUCCESS;if(!status) RegCloseKey(key);
    // #96: the launcher validates the branch id and sets LVZ_BRANCH_ID before
    // CreateProcessW, so this child must already see it. Printed as evidence;
    // the caller compares it with the id it passed to `launch`.
    char branch[MAX_PATH]={};
    fprintf(result,"branch_id=%s\n",GetEnvironmentVariableA("LVZ_BRANCH_ID",branch,MAX_PATH)?branch:"(unset)");
    HWND before=GetForegroundWindow();
    auto window=CreateWindowExA(0,"STATIC","LVZ isolation fixture",WS_OVERLAPPEDWINDOW|WS_VISIBLE,0,0,800,600,nullptr,nullptr,GetModuleHandleW(nullptr),nullptr);
    ShowWindow(window,SW_SHOW);SetForegroundWindow(window);SetFocus(window);
    fprintf(result,"logical_active=%d\n",GetActiveWindow()==window);
    fprintf(result,"hidden=%d\nforeground_unchanged=%d\n",!IsWindowVisible(window),before==GetForegroundWindow());
    okay=okay&&window&&!IsWindowVisible(window)&&before==GetForegroundWindow()&&GetActiveWindow()==window;
    fprintf(result,"passed=%d\n",okay);
    DestroyWindow(window);fclose(result);return okay?0:3;
}
