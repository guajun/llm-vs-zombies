#pragma once
// First-chance exception log for a hosted AvZ build: <run>/decisions/hosted-crash-probe.log.
//
// Issue #88: a hosted build that calls AvZ accessors from a resumed coroutine
// crashed the game with an access violation *inside* AvZ's own crash filter
// (`ASeh::DoHandleDebugEvent`), so the original fault - and the `crash.txt` it
// should have written - never survived. This probe is the missing witness: a
// vectored exception handler (VEH) is registered as early as the DLL loads, it
// logs every first-chance exception with its code, faulting address,
// module+RVA, thread and the recorder context of the last granted frame, and it
// always returns EXCEPTION_CONTINUE_SEARCH so the game and AvZ keep the exact
// same handling they had before (a VEH runs *before* SEH).
//
// The log is small by construction: one header line, one line per exception
// (capped), and every line is written with WriteFile + FlushFileBuffers, so a
// process that dies mid-crash still leaves the evidence on disk. Nothing in the
// handler allocates, locks the CRT or formats with std::string - it runs while
// the process is faulting.
//
// recorder.cpp owns the calls and makes them only from a build compiled with
// LVZ_AVZ_HOSTED_SCRIPT:
//   Open()    - the run lock is held, before the first frame
//   Context() - once per granted frame, after AvZ's RunScript()
//   Close()   - capture closed
// Without the switch this translation unit compiles to inert functions and a
// build never calls them, let alone creates the file;
// tests/hosted_crash_probe_tests.cpp is built both ways and asserts both
// behaviours. See docs/avz-script-hosting.md section 7.
#include <filesystem>
#include <string>

namespace lvz::hosted_crash_probe {
// Run-relative path of the file. It sits under decisions/ for the same reason
// the hosted observation file does: the archive policy
// (src/llm_vs_zombies/records.py) keeps allowlisted directories, so a run-root
// file would silently fall out of the sealed package.
inline constexpr const char* kRelativePath = "decisions/hosted-crash-probe.log";

// True only in a build compiled with LVZ_AVZ_HOSTED_SCRIPT.
bool Enabled();

// Creates the file - refusing an existing one, exactly like events.jsonl - and
// registers the vectored handler that writes to it. Failure is reported by
// throwing; recorder.cpp keeps the run alive by ignoring it (a missing crash
// log must never turn into a failed experiment).
void Open(const std::filesystem::path& runDirectory, const std::string& runId);

// Records the context the next exception line carries: the tick/segment of the
// last granted frame plus the state the hosted script published on it. Called
// on the game thread; the handler only reads the bounded text this leaves
// behind.
void Context(int tick, int segment, const std::string& fields);

// Writes the closing line and unregisters the handler. Idempotent.
void Close();
} // namespace lvz::hosted_crash_probe
