#pragma once
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <Windows.h>
#include <cstdint>
#include <cstring>
#include <stdexcept>

namespace lvz::determinism {
inline bool Accessible(uintptr_t address, size_t count, bool writable = false) noexcept {
    if (!address || count == 0 || address > UINTPTR_MAX - count) return false;
    const auto limit = address + count;
    while (address < limit) {
        MEMORY_BASIC_INFORMATION info{};
        if (VirtualQuery(reinterpret_cast<void*>(address), &info, sizeof(info)) != sizeof(info)
            || info.State != MEM_COMMIT || (info.Protect & (PAGE_GUARD | PAGE_NOACCESS))) return false;
        const auto protection = info.Protect & 0xff;
        if (protection != PAGE_READONLY && protection != PAGE_READWRITE
            && protection != PAGE_WRITECOPY && protection != PAGE_EXECUTE_READ
            && protection != PAGE_EXECUTE_READWRITE && protection != PAGE_EXECUTE_WRITECOPY)
            return false;
        if (writable && protection != PAGE_READWRITE && protection != PAGE_WRITECOPY
            && protection != PAGE_EXECUTE_READWRITE && protection != PAGE_EXECUTE_WRITECOPY)
            return false;
        const auto next = reinterpret_cast<uintptr_t>(info.BaseAddress) + info.RegionSize;
        if (next <= address) return false;
        address = next;
    }
    return true;
}
template<class T> inline bool TryRead(uintptr_t address, T& value) noexcept {
    if (!Accessible(address, sizeof(T))) return false;
    std::memcpy(&value, reinterpret_cast<const void*>(address), sizeof(T));
    return true;
}
template<class T> inline T Read(uintptr_t address) {
    T value{};
    if (!TryRead(address, value)) throw std::runtime_error("Unreadable audit address");
    return value;
}
}
