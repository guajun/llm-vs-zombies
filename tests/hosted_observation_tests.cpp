// The hosted-script observation file, tested without a game.
//
// Two targets are built from this one source:
//   * hosted_observation_tests          - LVZ_AVZ_HOSTED_SCRIPT=1, the same
//     translation unit a hosted recorder.dll links. It asserts the file lands
//     in <run>/decisions/hosted-script.jsonl, that the envelope matches
//     events.jsonl's, that the activation line names the hosted source, and
//     that a state which did not change writes no line.
//   * hosted_observation_default_tests - no switch, i.e. the way a default
//     build compiles this file: every entry point has to stay inert and no
//     file may appear anywhere. This is the "a default build produces no
//     hosted observation file" assertion.
// See docs/avz-script-hosting.md section 7.
#include "hosted_observation.hpp"

#ifdef LVZ_AVZ_HOSTED_SCRIPT
#include "buffered_writer.hpp"  // the refusal check opens the file a second time
#endif

#include <cstdio>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
void Check(bool condition, const std::string& why) {
    if (!condition) throw std::runtime_error(why);
}

// A real run directory is fresh; the test uses a fresh temporary one.
std::filesystem::path TempDirectory(const std::string& name) {
    const auto directory = std::filesystem::temp_directory_path() / ("lvz-hosted-observation-" + name);
    std::filesystem::remove_all(directory);
    std::filesystem::create_directories(directory);
    return directory;
}

std::vector<std::string> Lines(const std::filesystem::path& file) {
    std::ifstream input(file, std::ios::binary);
    std::vector<std::string> lines;
    for (std::string line; std::getline(input, line);) lines.push_back(line);
    return lines;
}

void CheckContains(const std::string& text, const std::string& needle, const std::string& why) {
    Check(text.find(needle) != std::string::npos, why + ": " + text);
}
} // namespace

int main() {
    try {
#ifdef LVZ_AVZ_HOSTED_SCRIPT
        Check(lvz::hosted_observation::Enabled(), "a hosted build reported the observation writer as disabled");
        const auto run = TempDirectory("hosted");
        const auto file = run / lvz::hosted_observation::kRelativePath;
        Check(!std::filesystem::exists(file), "the observation file existed before the run opened");

        lvz::hosted_observation::Open(run, "hosted-observable-01", "atime_probe.cpp", 0);
        Check(std::filesystem::exists(file), std::string("Open did not create ") + lvz::hosted_observation::kRelativePath);
        Check(std::filesystem::is_directory(run / "decisions"), "the run's decisions directory is missing");

        // Frame ticks 1 and 2 in which the script did not advance publish
        // nothing new; the first resume (tick 52) and the finish (tick 102) do.
        lvz::hosted_observation::Sample(1, 0, "\"started\":1,\"resumes\":0");
        lvz::hosted_observation::Sample(2, 0, "\"started\":1,\"resumes\":0");
        lvz::hosted_observation::Sample(52, 0, "\"started\":1,\"resumes\":1,\"resume_time\":-599");
        lvz::hosted_observation::Sample(102, 0, "\"started\":1,\"resumes\":3,\"finished\":1,\"clock_at_finish\":102");
        lvz::hosted_observation::Flush();

        const auto lines = Lines(file);
        Check(lines.size() == 4, "expected the activation line plus three state changes, got " + std::to_string(lines.size()));
        CheckContains(lines[0], "\"schema_version\":1,\"run_id\":\"hosted-observable-01\",\"seq\":0,\"segment\":0,\"tick\":0",
            "the activation line's envelope changed");
        CheckContains(lines[0], "\"phase\":\"hosted_script\",\"kind\":\"hosted_script_active\"",
            "the activation line's kind/phase changed");
        CheckContains(lines[0], "\"payload\":{\"script\":\"atime_probe.cpp\"}",
            "the activation line does not name the hosted source");
        CheckContains(lines[1], "\"seq\":1,\"segment\":0,\"tick\":1", "the first state line's tick/seq changed");
        CheckContains(lines[1], "\"kind\":\"hosted_script_state\"", "the state line's kind changed");
        CheckContains(lines[2], "\"tick\":52,\"phase\":\"hosted_script\"", "the resume line's tick changed");
        CheckContains(lines[2], "\"resumes\":1", "the resume line lost the published counters");
        CheckContains(lines[3], "\"finished\":1,\"clock_at_finish\":102", "the finish line lost the published counters");

        // The writer is one-shot per process, exactly like events.jsonl: a
        // closed run cannot be reopened, and a later Sample adds nothing.
        lvz::hosted_observation::Close();
        lvz::hosted_observation::Open(run, "hosted-observable-01", "atime_probe.cpp", 500);
        lvz::hosted_observation::Sample(600, 0, "\"resumes\":9");
        lvz::hosted_observation::Close();
        Check(Lines(file).size() == lines.size(), "the writer reopened a closed run directory");

        // Reusing a directory that still holds the file is refused rather than
        // appended to, so "create a new run" holds for this evidence too.
        bool refused = false;
        try {
            lvz::BufferedWriter second;
            second.Open(file);
        } catch (const std::exception&) { refused = true; }
        Check(refused, "the writer appended to an existing observation file");
        std::printf("hosted observation writer: %zu lines, one per distinct state\n", lines.size());
        std::filesystem::remove_all(run);
        return 0;
#else
        Check(!lvz::hosted_observation::Enabled(), "a default build reported the observation writer as enabled");
        const auto run = TempDirectory("default");
        lvz::hosted_observation::Open(run, "hosted-observable-01", "atime_probe.cpp", 0);
        lvz::hosted_observation::Sample(1, 0, "\"started\":1,\"resumes\":1");
        lvz::hosted_observation::Flush();
        lvz::hosted_observation::Close();
        Check(!std::filesystem::exists(run / lvz::hosted_observation::kRelativePath),
            "a default build created the hosted observation file");
        Check(!std::filesystem::exists(run / "decisions"), "a default build created the decisions directory");
        std::printf("default-build observation writer: no file created\n");
        std::filesystem::remove_all(run);
        return 0;
#endif
    } catch (const std::exception& error) {
        std::fprintf(stderr, "hosted observation test failed: %s\n", error.what());
        return 1;
    }
}
