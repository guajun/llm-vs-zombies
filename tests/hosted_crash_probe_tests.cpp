// The hosted first-chance exception log, tested without a game.
//
// Two targets are built from this one source:
//   * hosted_crash_probe_tests         - LVZ_AVZ_HOSTED_SCRIPT=1, the same
//     translation unit a hosted recorder.dll links. It asserts the file lands
//     in <run>/decisions/hosted-crash-probe.log, that every line is flushed as
//     it is written (the parent reads a log the child wrote right before it
//     died), and that the handler neither swallows nor replaces the exception:
//     a child that raises an access violation still exits with 0xC0000005.
//   * hosted_crash_probe_default_tests - no switch, i.e. the way a default
//     build compiles this file: every entry point stays inert and no file
//     appears anywhere.
// See docs/avz-script-hosting.md section 7 and issue #88.
#include "hosted_crash_probe.hpp"

#include <cstdio>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <string>
#include <vector>

#ifdef LVZ_AVZ_HOSTED_SCRIPT
#include <windows.h>
#endif

namespace {
void Check(bool condition, const std::string& why) {
    if (!condition) throw std::runtime_error(why);
}

std::filesystem::path TempDirectory(const std::string& name) {
    const auto directory = std::filesystem::temp_directory_path() / ("lvz-hosted-crash-" + name);
    std::filesystem::remove_all(directory);
    std::filesystem::create_directories(directory);
    return directory;
}

std::string Text(const std::filesystem::path& file) {
    std::ifstream input(file, std::ios::binary);
    return std::string(std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>());
}

void CheckContains(const std::string& text, const std::string& needle, const std::string& why) {
    Check(text.find(needle) != std::string::npos, why + ": " + text);
}

#ifdef LVZ_AVZ_HOSTED_SCRIPT
// The child half: open the probe on a fresh directory, publish a context, then
// fault for real. Everything the parent reads has to have reached the disk
// before this process dies.
int Raise(int argc, char** argv) {
    if (argc < 3) return 90;
    SetErrorMode(SEM_NOGPFAULTERRORBOX | SEM_FAILCRITICALERRORS);
    lvz::hosted_crash_probe::Open(std::filesystem::path(argv[2]), "hosted-crash-child");
    lvz::hosted_crash_probe::Context(4321, 2, "\"resumes\":3,\"finished\":1,\"clock_at_finish\":4321");
    ULONG_PTR parameters[2] = {0, 0x1234};
    RaiseException(EXCEPTION_ACCESS_VIOLATION, 0, 2, parameters);
    return 91; // unreachable while the handler keeps searching
}

// Runs the same binary as a child and returns its exit code.
DWORD RunChild(const std::filesystem::path& run, const std::filesystem::path& self) {
    std::wstring command = L"\"" + self.wstring() + L"\" raise \"" + run.wstring() + L"\"";
    STARTUPINFOW startup{};
    startup.cb = sizeof(startup);
    PROCESS_INFORMATION process{};
    Check(CreateProcessW(nullptr, command.data(), nullptr, nullptr, FALSE, 0, nullptr, nullptr,
                         &startup, &process),
          "could not start the faulting child process");
    WaitForSingleObject(process.hProcess, 120000);
    DWORD code = 0;
    GetExitCodeProcess(process.hProcess, &code);
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    return code;
}
#endif
} // namespace

int main(int argc, char** argv) {
    try {
#ifdef LVZ_AVZ_HOSTED_SCRIPT
        Check(lvz::hosted_crash_probe::Enabled(), "a hosted build reported the crash probe as disabled");
        if (argc >= 2 && std::string(argv[1]) == "raise") return Raise(argc, argv);

        const auto run = TempDirectory("hosted");
        const auto file = run / lvz::hosted_crash_probe::kRelativePath;
        Check(!std::filesystem::exists(file), "the crash log existed before the run opened");
        lvz::hosted_crash_probe::Open(run, "hosted-crash-parent");
        Check(std::filesystem::exists(file), "Open did not create decisions/hosted-crash-probe.log");
        CheckContains(Text(file), "lvz.hosted_crash_probe.v1 run=hosted-crash-parent", "the header line changed");
        lvz::hosted_crash_probe::Close();
        CheckContains(Text(file), "closed pid=", "Close wrote no footer");
        CheckContains(Text(file), "events=0", "the parent's own log counted an exception");

        // Reusing a directory that still holds the file is refused rather than
        // appended to, exactly like events.jsonl.
        bool refused = false;
        try {
            lvz::hosted_crash_probe::Open(run, "hosted-crash-parent");
        } catch (const std::exception&) { refused = true; }
        Check(refused, "the crash log appended to an existing file");

        // The faulting child: the code, the module+RVA of the faulting address,
        // the tick/state context and the exit code all have to survive.
        wchar_t module[32768];
        Check(GetModuleFileNameW(nullptr, module, 32768) > 0, "could not locate the test binary");
        const auto childRun = TempDirectory("child");
        const DWORD code = RunChild(childRun, module);
        Check(code == 0xC0000005ul, "the faulted child did not exit with the access violation code; got "
            + std::to_string(code));
        const std::string log = Text(childRun / lvz::hosted_crash_probe::kRelativePath);
        CheckContains(log, "evt=1 first_chance code=0xc0000005 name=access_violation", "the first-chance line changed");
        CheckContains(log, " tick=4321 segment=2", "the line does not carry the frame context");
        CheckContains(log, "\"resumes\":3,\"finished\":1", "the line does not carry the published script state");
        // RaiseException faults inside the raising API, so the module on the
        // line is KERNELBASE; what matters is that a module and its RVA are
        // named at all, and that the scan names this test binary as the caller.
        CheckContains(log, " mod=", "the faulting module is not named");
        CheckContains(log, " rva=", "the faulting address carries no module-relative offset");
        Check(log.find("mod=?") == std::string::npos, "the faulting module was not resolved");
        CheckContains(log, "hosted_crash_probe_tests.exe+", "the faulting stack does not name the test binary");
        CheckContains(log, "  stack:", "the faulting stack was not scanned");
        CheckContains(log, "exceptions_before_open=0", "events before Open are not accounted for");

        std::printf("hosted crash probe: first-chance line, module+rva, context and continue-search verified\n");
        std::filesystem::remove_all(run);
        std::filesystem::remove_all(childRun);
        return 0;
#else
        Check(!lvz::hosted_crash_probe::Enabled(), "a default build reported the crash probe as enabled");
        const auto run = TempDirectory("default");
        lvz::hosted_crash_probe::Open(run, "hosted-crash-default");
        lvz::hosted_crash_probe::Context(1, 0, "\"resumes\":1");
        lvz::hosted_crash_probe::Close();
        Check(!std::filesystem::exists(run / lvz::hosted_crash_probe::kRelativePath),
              "a default build created the crash log");
        Check(!std::filesystem::exists(run / "decisions"), "a default build created the decisions directory");
        std::printf("default-build crash probe: no file created\n");
        std::filesystem::remove_all(run);
        return 0;
#endif
    } catch (const std::exception& error) {
        std::fprintf(stderr, "hosted crash probe test failed: %s\n", error.what());
        return 1;
    }
}
