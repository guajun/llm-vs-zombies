// First-chance exception log; see hosted_crash_probe.hpp.
//
// The handler is deliberately primitive: no CRT, no allocation, no lock, no
// std::string. Everything it writes is formatted into static buffers with the
// helpers below, so it stays usable while the process is faulting - which is
// exactly the state issue #88 left behind (AvZ's own filter faulted before it
// could write crash.txt). It never handles the exception: EXCEPTION_CONTINUE_SEARCH
// keeps the game's and AvZ's handling byte-for-byte what it was.
#include "hosted_crash_probe.hpp"

#ifdef LVZ_AVZ_HOSTED_SCRIPT

#include <windows.h>
#include <tlhelp32.h>

#include <cstdint>
#include <cstring>
#include <stdexcept>
#include <string>

namespace lvz::hosted_crash_probe {
namespace {
constexpr int kMaxModules = 128;
constexpr int kMaxEvents = 256;
constexpr int kTextBudget = 4096;
constexpr int kContextBudget = 768;

struct ModuleRange {
    uintptr_t base = 0;
    uintptr_t end = 0;
    char name[32] = {};
};

// The handler is registered while the DLL is still loading, so it sees
// exceptions before the run has a directory to log into (the game raises
// first-chance SEH of its own during startup). The first few are kept here, in
// the same no-CRT form the handler can produce, and written when the run opens.
constexpr int kPreOpenSlots = 8;
struct PreOpenEvent {
    DWORD code = 0;
    uintptr_t address = 0;
    DWORD thread = 0;
    LONG sequence = 0;
};
PreOpenEvent preOpen[kPreOpenSlots];
LONG preOpenTotal = 0;

ModuleRange modules[kMaxModules];
int moduleCount = 0;
HANDLE logFile = INVALID_HANDLE_VALUE;
volatile LONG eventCount = 0;
volatile LONG inHandler = 0;
volatile LONG skippedEvents = 0;

// Context published by the game thread once per granted frame. `length` is
// written last so the handler never reads a half-written text.
volatile LONG contextTick = -1;
volatile LONG contextSegment = -1;
volatile LONG contextLength = 0;
char contextText[kContextBudget];

// Handler-local scratch. Static on purpose: a stack-overflow exception arrives
// with little stack left.
char textBuffer[kTextBudget];
BYTE readBuffer[1024];

char* Put(char* out, const char* text) {
    while (*text) *out++ = *text++;
    return out;
}

char* PutUint(char* out, uintptr_t value, bool hex) {
    char digits[32];
    int count = 0;
    if (value == 0) {
        digits[count++] = '0';
    } else {
        const char* table = hex ? "0123456789abcdef" : "0123456789";
        const uintptr_t base = hex ? 16 : 10;
        while (value && count < 32) {
            digits[count++] = table[value % base];
            value /= base;
        }
    }
    while (count) *out++ = digits[--count];
    return out;
}

char* PutHex(char* out, uintptr_t value) {
    *out++ = '0';
    *out++ = 'x';
    return PutUint(out, value, true);
}

const char* CodeName(DWORD code) {
    switch (code) {
    case EXCEPTION_ACCESS_VIOLATION: return "access_violation";
    case EXCEPTION_STACK_OVERFLOW: return "stack_overflow";
    case EXCEPTION_ILLEGAL_INSTRUCTION: return "illegal_instruction";
    case EXCEPTION_INT_DIVIDE_BY_ZERO: return "int_divide_by_zero";
    case EXCEPTION_BREAKPOINT: return "breakpoint";
    case EXCEPTION_SINGLE_STEP: return "single_step";
    case EXCEPTION_GUARD_PAGE: return "guard_page";
    case EXCEPTION_IN_PAGE_ERROR: return "in_page_error";
    case EXCEPTION_ARRAY_BOUNDS_EXCEEDED: return "array_bounds";
    case EXCEPTION_FLT_DIVIDE_BY_ZERO: return "float_divide_by_zero";
    case EXCEPTION_PRIV_INSTRUCTION: return "privileged_instruction";
    case EXCEPTION_NONCONTINUABLE_EXCEPTION: return "noncontinuable";
    case EXCEPTION_INVALID_DISPOSITION: return "invalid_disposition";
    case 0xE06D7363: return "cpp_throw_msvc";
    case 0x20474343: return "cpp_throw_gnu";
    case 0xC0000374: return "heap_corruption";
    case 0xC0000409: return "stack_buffer_overrun";
    case 0xC0000602: return "fail_fast";
    default: return "other";
    }
}

void SnapshotModules() {
    moduleCount = 0;
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, GetCurrentProcessId());
    if (snapshot == INVALID_HANDLE_VALUE) return;
    MODULEENTRY32W entry;
    entry.dwSize = sizeof(entry);
    if (Module32FirstW(snapshot, &entry)) {
        do {
            if (moduleCount >= kMaxModules) break;
            ModuleRange& range = modules[moduleCount++];
            range.base = reinterpret_cast<uintptr_t>(entry.modBaseAddr);
            range.end = range.base + entry.modBaseSize;
            // WideCharToMultiByte is not safe to call from the handler, so the
            // name is narrowed here, where ordinary code still runs.
            int written = WideCharToMultiByte(CP_ACP, 0, entry.szModule, -1, range.name,
                                              static_cast<int>(sizeof(range.name)) - 1, nullptr, nullptr);
            if (written <= 0) std::strcpy(range.name, "?");
            range.name[sizeof(range.name) - 1] = '\0';
        } while (Module32NextW(snapshot, &entry));
    }
    CloseHandle(snapshot);
}

const ModuleRange* FindModule(uintptr_t address) {
    const int count = moduleCount;
    for (int index = 0; index < count && index < kMaxModules; ++index)
        if (address >= modules[index].base && address < modules[index].end) return &modules[index];
    return nullptr;
}

// Called from the handler: only WriteFile/FlushFileBuffers, both on an already
// open append handle with FILE_FLAG_WRITE_THROUGH.
void Line(const char* text, int length) {
    if (logFile == INVALID_HANDLE_VALUE || length <= 0) return;
    DWORD written = 0;
    WriteFile(logFile, text, static_cast<DWORD>(length), &written, nullptr);
    FlushFileBuffers(logFile);
}

void AppendModule(char*& out, const char* prefix, uintptr_t address) {
    out = Put(out, prefix);
    const ModuleRange* module = FindModule(address);
    if (!module) {
        out = Put(out, "?+?");
        return;
    }
    out = Put(out, module->name);
    out = Put(out, "+");
    out = PutHex(out, address - module->base);
}

LONG WINAPI Handler(PEXCEPTION_POINTERS info) {
    if (InterlockedCompareExchange(&inHandler, 1, 0) != 0) return EXCEPTION_CONTINUE_SEARCH;
    const LONG index = InterlockedIncrement(&eventCount);
    if (logFile == INVALID_HANDLE_VALUE) {
        const LONG slot = InterlockedIncrement(&preOpenTotal) - 1;
        if (slot < kPreOpenSlots) {
            preOpen[slot].code = info->ExceptionRecord->ExceptionCode;
            preOpen[slot].address = reinterpret_cast<uintptr_t>(info->ExceptionRecord->ExceptionAddress);
            preOpen[slot].thread = GetCurrentThreadId();
            preOpen[slot].sequence = index;
        }
        InterlockedExchange(&inHandler, 0);
        return EXCEPTION_CONTINUE_SEARCH;
    }
    if (index > kMaxEvents) {
        InterlockedIncrement(&skippedEvents);
        InterlockedExchange(&inHandler, 0);
        return EXCEPTION_CONTINUE_SEARCH;
    }
    const EXCEPTION_RECORD* record = info->ExceptionRecord;
    const CONTEXT* context = info->ContextRecord;
    char* out = textBuffer;
    // Keep room for the longest single field (a negative 64-bit decimal plus a
    // terminator) so every bounded write below stays inside the buffer.
    char* const end = textBuffer + kTextBudget - 64;
    auto put = [&out, end](const char* text) { if (out < end) out = Put(out, text); };
    auto putHex = [&out, end](uintptr_t value) { if (out < end) out = PutHex(out, value); };
    auto putInt = [&out, end](long long value) {
        if (out >= end) return;
        if (value < 0) { *out++ = '-'; value = -value; }
        if (out < end) out = PutUint(out, static_cast<uintptr_t>(value), false);
    };
    const auto address = reinterpret_cast<uintptr_t>(record->ExceptionAddress);

    put("evt="); putInt(index);
    put(" first_chance code="); putHex(record->ExceptionCode);
    put(" name="); put(CodeName(record->ExceptionCode));
    put(" addr="); putHex(address);
    const ModuleRange* faultModule = FindModule(address);
    if (faultModule) {
        put(" mod="); put(faultModule->name);
        put(" rva="); putHex(address - faultModule->base);
    } else {
        put(" mod=? rva=?");
    }
    put(" flags="); putHex(record->ExceptionFlags);
    put(" params="); putInt(record->NumberParameters);
    for (DWORD i = 0; i < record->NumberParameters && i < EXCEPTION_MAXIMUM_PARAMETERS; ++i) {
        put(" p"); putInt(i); put("="); putHex(record->ExceptionInformation[i]);
    }
    put(" thread="); putInt(GetCurrentThreadId());
    put(" tick="); putInt(contextTick);
    put(" segment="); putInt(contextSegment);
    put("\r\n");
    if (context) {
        put("  regs eip="); putHex(context->Eip);
        put(" esp="); putHex(context->Esp);
        put(" ebp="); putHex(context->Ebp);
        put(" eax="); putHex(context->Eax);
        put(" ebx="); putHex(context->Ebx);
        put(" ecx="); putHex(context->Ecx);
        put(" edx="); putHex(context->Edx);
        put(" esi="); putHex(context->Esi);
        put(" edi="); putHex(context->Edi);
        put(" eflags="); putHex(context->EFlags);
        put(" cs="); putHex(context->SegCs);
        put(" ds="); putHex(context->SegDs);
        put(" fs="); putHex(context->SegFs);
        put("\r\n");
        // The instruction at Eip, read defensively: ReadProcessMemory fails
        // instead of raising when the page is not there.
        SIZE_T got = 0;
        if (ReadProcessMemory(GetCurrentProcess(), reinterpret_cast<const void*>(context->Eip), readBuffer, 16, &got)
            && got > 0) {
            put("  insn=");
            for (SIZE_T i = 0; i < got; ++i) {
                if (out >= end - 4) break;
                const char hex[] = "0123456789abcdef";
                *out++ = hex[(readBuffer[i] >> 4) & 15];
                *out++ = hex[readBuffer[i] & 15];
                put(" ");
            }
            put("\r\n");
        }
        // A bounded scan of the faulting stack for values that land inside a
        // known module: enough to name the caller frames without a debugger.
        put("  stack:");
        int found = 0;
        for (int offset = 0; offset < 2048 && found < 8; offset += static_cast<int>(sizeof(uintptr_t))) {
            SIZE_T read = 0;
            if (!ReadProcessMemory(GetCurrentProcess(),
                                   reinterpret_cast<const void*>(context->Esp + static_cast<DWORD>(offset)),
                                   readBuffer, sizeof(uintptr_t) * 2, &read) || read < sizeof(uintptr_t))
                continue;
            uintptr_t value = 0;
            std::memcpy(&value, readBuffer, sizeof(value));
            if (!FindModule(value)) continue;
            put(" "); putHex(value); put("=");
            AppendModule(out, "", value);
            ++found;
        }
        if (!found) put(" none");
        put("\r\n");
    }
    const LONG length = InterlockedCompareExchange(&contextLength, 0, 0);
    if (length > 0 && length < kContextBudget) {
        put("  state=");
        char* copied = out;
        for (LONG i = 0; i < length && out < end; ++i) *out++ = contextText[i];
        if (copied == out) put("(empty)");
        put("\r\n");
    }
    Line(textBuffer, static_cast<int>(out - textBuffer));
    InterlockedExchange(&inHandler, 0);
    return EXCEPTION_CONTINUE_SEARCH;
}

void Register() {
    static bool registered = false;
    if (registered) return;
    registered = true;
    AddVectoredExceptionHandler(1, Handler);
}

// Registered from a static initializer so an exception is never missed because
// the run had not opened yet; events before Open() are counted, not lost
// silently (the header reports how many there were).
struct AutoRegister {
    AutoRegister() { Register(); }
};
AutoRegister autoRegister;
} // namespace

bool Enabled() { return true; }

void Open(const std::filesystem::path& runDirectory, const std::string& runId) {
    if (logFile != INVALID_HANDLE_VALUE) return;
    Register();
    SnapshotModules();
    const auto file = runDirectory / kRelativePath;
    std::filesystem::create_directories(file.parent_path());
    if (std::filesystem::exists(file))
        throw std::runtime_error(std::string(kRelativePath) + " already exists; create a new run");
    const HANDLE handle = CreateFileW(file.c_str(), FILE_APPEND_DATA | SYNCHRONIZE,
                                      FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, CREATE_NEW,
                                      FILE_ATTRIBUTE_NORMAL | FILE_FLAG_WRITE_THROUGH, nullptr);
    if (handle == INVALID_HANDLE_VALUE)
        throw std::runtime_error(std::string(kRelativePath) + ": cannot create the file");
    logFile = handle;
    const LONG before = InterlockedExchange(&skippedEvents, 0);
    char header[512];
    char* out = Put(header, "lvz.hosted_crash_probe.v1 run=");
    out = Put(out, runId.c_str());
    out = Put(out, " pid=");
    out = PutUint(out, GetCurrentProcessId(), false);
    out = Put(out, " modules=");
    out = PutUint(out, static_cast<uintptr_t>(moduleCount), false);
    out = Put(out, " exceptions_before_open=");
    out = PutUint(out, static_cast<uintptr_t>(before), false);
    out = Put(out, "\r\n");
    Line(header, static_cast<int>(out - header));
    // The first-chance exceptions the game raised before the run existed, in
    // order, with the module table that is now known.
    const LONG buffered = InterlockedCompareExchange(&preOpenTotal, 0, 0);
    const LONG shown = buffered < kPreOpenSlots ? buffered : kPreOpenSlots;
    for (LONG i = 0; i < shown; ++i) {
        char line[256];
        char* cursor = Put(line, "pre_open evt=");
        cursor = PutUint(cursor, static_cast<uintptr_t>(preOpen[i].sequence), false);
        cursor = Put(cursor, " code=");
        cursor = PutHex(cursor, preOpen[i].code);
        cursor = Put(cursor, " name=");
        cursor = Put(cursor, CodeName(preOpen[i].code));
        cursor = Put(cursor, " addr=");
        cursor = PutHex(cursor, preOpen[i].address);
        const ModuleRange* module = FindModule(preOpen[i].address);
        if (module) {
            cursor = Put(cursor, " mod=");
            cursor = Put(cursor, module->name);
            cursor = Put(cursor, " rva=");
            cursor = PutHex(cursor, preOpen[i].address - module->base);
        }
        cursor = Put(cursor, " thread=");
        cursor = PutUint(cursor, preOpen[i].thread, false);
        cursor = Put(cursor, "\r\n");
        Line(line, static_cast<int>(cursor - line));
    }
}

void Context(int tick, int segment, const std::string& fields) {
    if (logFile == INVALID_HANDLE_VALUE) return;
    contextLength = 0;
    size_t length = fields.size();
    if (length > kContextBudget - 1) length = kContextBudget - 1;
    std::memcpy(contextText, fields.data(), length);
    contextText[length] = '\0';
    contextTick = tick;
    contextSegment = segment;
    contextLength = static_cast<LONG>(length);
}

void Close() {
    if (logFile == INVALID_HANDLE_VALUE) return;
    char footer[256];
    char* out = Put(footer, "closed pid=");
    out = PutUint(out, GetCurrentProcessId(), false);
    out = Put(out, " events=");
    out = PutUint(out, static_cast<uintptr_t>(InterlockedCompareExchange(&eventCount, 0, 0)), false);
    out = Put(out, " skipped=");
    out = PutUint(out, static_cast<uintptr_t>(InterlockedCompareExchange(&skippedEvents, 0, 0)), false);
    const LONG tick = contextTick;
    out = Put(out, " last_tick=");
    if (tick < 0) {
        out = Put(out, "?");
    } else {
        out = PutUint(out, static_cast<uintptr_t>(tick), false);
    }
    out = Put(out, "\r\n");
    Line(footer, static_cast<int>(out - footer));
    CloseHandle(logFile);
    logFile = INVALID_HANDLE_VALUE;
}
} // namespace lvz::hosted_crash_probe

#else

// Default build: no hosted script is linked, so there is no crash log to write
// and no handler to install. tests/hosted_crash_probe_tests.cpp compiles
// exactly this branch and asserts that every entry point stays inert.
namespace lvz::hosted_crash_probe {
bool Enabled() { return false; }
void Open(const std::filesystem::path&, const std::string&) {}
void Context(int, int, const std::string&) {}
void Close() {}
} // namespace lvz::hosted_crash_probe

#endif
