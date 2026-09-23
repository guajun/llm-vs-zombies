#include <avz.h>

#include "../hosted_script.hpp"
#include "atime_probe.hpp"

namespace lvz::hosted {
ProbeState probe;

ACoroutine Script() {
    probe.started = 1;
    for (int offset : kProbeWaits) {
        co_await ATime(1, offset);
        ++probe.resumes;
        probe.resumeWave = 1;
        probe.resumeTime = ANowTime(1);
    }
    probe.finished = 1;
    probe.clockAtFinish = AGetMainObject()->GameClock();
}

// Hosted observability (../hosted_script.hpp): the probe's counters, with the
// JSON member names the recorder writes into
// <run>/decisions/hosted-script.jsonl. This is what a live run reads instead
// of the in-process lvz::hosted::probe struct; the names are snake_case like
// every other recorded payload in this project. The wait sequence above is
// untouched: only the state it leaves behind is published.
void Observe(std::string& fields) {
    fields = "\"started\":" + std::to_string(probe.started)
        + ",\"resumes\":" + std::to_string(probe.resumes)
        + ",\"resume_wave\":" + std::to_string(probe.resumeWave)
        + ",\"resume_time\":" + std::to_string(probe.resumeTime)
        + ",\"finished\":" + std::to_string(probe.finished)
        + ",\"clock_at_finish\":" + std::to_string(probe.clockAtFinish);
}
} // namespace lvz::hosted
