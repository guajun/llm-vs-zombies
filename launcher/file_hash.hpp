#pragma once
#include <windows.h>
#include <bcrypt.h>
#include <array>
#include <string>
#include <stdexcept>
namespace lvz::silentaudio {
inline std::string FileSha256(const wchar_t* path) {
    HANDLE file=CreateFileW(path,GENERIC_READ,FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE,nullptr,OPEN_EXISTING,0,nullptr);
    if(file==INVALID_HANDLE_VALUE)throw std::runtime_error("audio identity file unreadable");
    BCRYPT_ALG_HANDLE alg=nullptr;BCRYPT_HASH_HANDLE hash=nullptr;
    std::array<uint8_t,32> digest{};std::array<uint8_t,65536> buffer{};DWORD got=0;bool ok=true;
    if(BCryptOpenAlgorithmProvider(&alg,BCRYPT_SHA256_ALGORITHM,nullptr,0)<0)ok=false;
    if(ok&&BCryptCreateHash(alg,&hash,nullptr,0,nullptr,0,0)<0)ok=false;
    while(ok){if(!ReadFile(file,buffer.data(),buffer.size(),&got,nullptr)){ok=false;break;}if(!got)break;
        if(BCryptHashData(hash,buffer.data(),got,0)<0)ok=false;}
    if(ok&&BCryptFinishHash(hash,digest.data(),digest.size(),0)<0)ok=false;
    if(hash)BCryptDestroyHash(hash);if(alg)BCryptCloseAlgorithmProvider(alg,0);CloseHandle(file);
    if(!ok)throw std::runtime_error("audio identity SHA-256 failed");
    const char* hex="0123456789abcdef";std::string result;result.reserve(64);
    for(auto b:digest){result+=hex[b>>4];result+=hex[b&15];}return result;
}
}
