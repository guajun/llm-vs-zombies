// Hosted-script observability writer; see hosted_observation.hpp.
//
// The published state is the only thing this file reads: the recorder samples
// it (lvz::hosted::Observe) and passes the text in, so this translation unit
// stays free of AvZ and of the game image and can be tested without a game.
#include "hosted_observation.hpp"

#ifdef LVZ_AVZ_HOSTED_SCRIPT

#include "buffered_writer.hpp"
#include <cstdint>

namespace lvz::hosted_observation {
namespace {
BufferedWriter writer;
std::string runId;
std::string scriptName;
std::string lastFields;
uint64_t sequence = 0;
bool isOpen = false;
bool isClosed = false;

// The envelope is the one events.jsonl uses, with phase "hosted_script":
// {"schema_version":1,"run_id":...,"seq":...,"segment":...,"tick":...,
//  "phase":"hosted_script","kind":...,"payload":{...}}
// seq numbers the lines of this file only, starting at 0.
std::string Line(const char* kind, int tick, int segment, const std::string& fields) {
    std::string payload = "{\"script\":" + Quote(scriptName);
    if (!fields.empty()) payload += "," + fields;
    payload += '}';
    return "{\"schema_version\":1,\"run_id\":" + Quote(runId)
        + ",\"seq\":" + std::to_string(sequence++)
        + ",\"segment\":" + std::to_string(segment)
        + ",\"tick\":" + std::to_string(tick)
        + ",\"phase\":\"hosted_script\",\"kind\":" + Quote(kind)
        + ",\"payload\":" + payload + "}";
}
} // namespace

bool Enabled() { return true; }

void Open(const std::filesystem::path& runDirectory, const std::string& id,
          const std::string& script, int tick) {
    if (isOpen || isClosed) return;
    runId = id;
    scriptName = script;
    const auto file = runDirectory / kRelativePath;
    // A run created by `new-run` already has decisions/; creating it here keeps
    // the file in its archive-scoped home instead of failing on a run directory
    // assembled by hand.
    std::filesystem::create_directories(file.parent_path());
    // BufferedWriter's messages name events.jsonl, so the file that is in the
    // way is named here and any other failure is reported with its real path.
    if (std::filesystem::exists(file))
        throw std::runtime_error(std::string(kRelativePath) + " already exists; create a new run");
    try {
        writer.Open(file);
    } catch (const std::exception& error) {
        throw std::runtime_error(std::string(kRelativePath) + ": " + error.what());
    }
    isOpen = true;
    writer.Append(Line("hosted_script_active", tick, 0, ""));
    // One line, one flush. The batch writer would hold this state in memory
    // until 64 KiB or a segment boundary, and issue #88 showed exactly what
    // that costs: the live crash left a 0-byte file and nothing to read.
    writer.Flush();
}

void Sample(int tick, int segment, const std::string& fields) {
    if (!isOpen || isClosed || fields == lastFields) return;
    lastFields = fields;
    writer.Append(Line("hosted_script_state", tick, segment, fields));
    // Per-line flush: the state a crash should be read against is on disk
    // before the frame ends, not at the next 64 KiB batch.
    writer.Flush();
}

void Flush() {
    if (!isOpen || isClosed) return;
    writer.Flush();
}

void Close() {
    if (!isOpen || isClosed) return;
    writer.Close();
    isClosed = true;
}
} // namespace lvz::hosted_observation

#else

// Default build: no hosted script is linked into the DLL, so nothing may be
// created. tests/hosted_observation_tests.cpp compiles exactly this branch
// (the default target of the two) and asserts that all four entry points stay
// inert and leave the run directory untouched.
namespace lvz::hosted_observation {
bool Enabled() { return false; }
void Open(const std::filesystem::path&, const std::string&, const std::string&, int) {}
void Sample(int, int, const std::string&) {}
void Flush() {}
void Close() {}
} // namespace lvz::hosted_observation

#endif
