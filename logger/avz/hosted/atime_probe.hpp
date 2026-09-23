#pragma once
// Observable state of the minimal hosted script used by the offline test in
// tests/avz_hosted_script_tests.cpp. The script itself is deliberately trivial
// (three `co_await ATime(1, ...)` waits); everything a reader can assert about
// it is in this struct, so the test never has to inspect engine memory to see
// whether AvZ advanced the coroutine.
namespace lvz::hosted {
struct ProbeState {
    int started = 0;      // the body ran at least once (launched by AScript())
    int resumes = 0;      // completed co_await(ATime) waits
    int resumeWave = 0;   // wave of the wait that last completed
    int resumeTime = 0;   // ANowTime(wave) observed at that resume
    int finished = 0;     // the body ran to completion
    int clockAtFinish = 0;
};

extern ProbeState probe;

// Waits that script() goes through, in order, as ATime(1, offset) offsets.
inline constexpr int kProbeWaits[] = {-599, -549, -499};
} // namespace lvz::hosted
