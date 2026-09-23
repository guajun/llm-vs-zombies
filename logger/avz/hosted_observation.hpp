#pragma once
// Hosted-script observability: <run>/decisions/hosted-script.jsonl.
//
// A hosted AvZ script (LVZ_AVZ_HOSTED_SCRIPT) keeps its state in process
// memory, which is exactly where a live run cannot read it. This writer turns
// the state a hosted script publishes every granted frame into one JSONL file
// inside the run directory, so the same evidence the offline test asserts from
// lvz::hosted::probe (logger/avz/hosted/atime_probe.hpp) can be read during or
// after a real run.
//
// recorder.cpp owns the calls and makes them only from a build compiled with
// LVZ_AVZ_HOSTED_SCRIPT:
//   Open()   - the run lock is held, before the first frame
//   Sample() - once per granted frame, after AvZ's RunScript()
//   Flush()  - segment boundaries and close
//   Close()  - capture closed
// Without the switch this translation unit compiles to four inert functions
// and a build never calls them, let alone creates the file;
// tests/hosted_observation_tests.cpp is built both ways and asserts both
// behaviours. See docs/avz-script-hosting.md section 7.
#include <filesystem>
#include <string>

namespace lvz::hosted_observation {
// Run-relative path of the file. It sits under decisions/ on purpose: the
// archive policy (src/llm_vs_zombies/records.py) keeps allowlisted
// directories and run-root *.json files, so a run-root *.jsonl would silently
// fall out of the sealed package.
inline constexpr const char* kRelativePath = "decisions/hosted-script.jsonl";

// True only in a build compiled with LVZ_AVZ_HOSTED_SCRIPT.
bool Enabled();

// Creates the file - refusing an existing one, exactly like events.jsonl, so
// the rule "create a new run" holds for this evidence too - and writes the
// activation line that names the source the DLL was built with.
void Open(const std::filesystem::path& runDirectory, const std::string& runId,
          const std::string& scriptName, int tick);

// Writes one line for this granted frame, but only when `fields` differs
// byte-wise from the previous sample. `fields` is the JSON object member list
// a hosted script's Observe() produced; the recorder adds the `script` member.
void Sample(int tick, int segment, const std::string& fields);

void Flush();
void Close();
} // namespace lvz::hosted_observation
