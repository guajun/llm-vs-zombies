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
} // namespace lvz::hosted
