"""AvZ script hosting stays compile-time, opt-in and on the runtime frame path (#82)."""
# The offline proof that a hosted coroutine advances lives in
# tests/avz_hosted_script_tests.cpp; these checks keep the wiring it depends on
# from silently drifting: the script entry stays a link-time symbol, the overlay
# keeps RunScript() on the frame path, and the hosted launch stays behind the
# build switch. tests/test_avz_hosted_script.py also guards the live
# observability path (issue #84) the same way: <run>/decisions/hosted-script.jsonl
# may only be touched by a build compiled with LVZ_AVZ_HOSTED_SCRIPT.
from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


class HostedScriptWiringTests(unittest.TestCase):
    def test_script_entry_is_link_time_only(self):
        script = read("avz/framework/src/avz_script.cpp")
        self.assertIn("void AScript();", script)
        self.assertIn("AScript();", script)
        self.assertNotIn("LoadLibrary", script)

    def test_overlay_keeps_run_script_on_the_frame_path(self):
        overlay = read("runtime/avz_overlay.cmake")
        # LoadScript must hand the frame to RunScript instead of recursing into
        # AvZ's own lifetime loop.
        self.assertIn("if (lvz::runtime::Started()) { RunScript(); return; }", overlay)
        # ... and ScriptHook must keep calling RunTotal for RunScript to run.
        # The overlay text carries \n escapes inside its quoted CMake string.
        self.assertIn(r"if (!lvz::runtime::BeforeFrame()) return;\n    RunTotal();", overlay)
        self.assertIn(r"if (!lvz::runtime::RunOneEngineFrame()) return;\n", overlay)

    def test_recorder_launches_the_hosted_script_only_under_the_switch(self):
        recorder = read("logger/avz/recorder.cpp")
        marker = recorder.index("lvz::hosted::Launch();")
        guard = recorder.rindex("#ifdef LVZ_AVZ_HOSTED_SCRIPT", 0, marker)
        self.assertLess(guard, marker)
        self.assertIn("ACoLaunch", read("logger/avz/hosted_script.hpp"))

    def test_build_switch_is_optional_and_compiles_the_named_source(self):
        cmake = read("CMakeLists.txt")
        self.assertIn('set(LVZ_AVZ_HOSTED_SCRIPT "" CACHE FILEPATH', cmake)
        self.assertIn("target_compile_definitions(recorder PRIVATE LVZ_AVZ_HOSTED_SCRIPT=1)", cmake)
        self.assertIn('target_sources(recorder PRIVATE "${LVZ_AVZ_HOSTED_SCRIPT}")', cmake)
        for hosted in ("logger/avz/hosted/atime_probe.cpp", "logger/avz/hosted/jing_dian_12.cpp"):
            self.assertIn("ACoroutine Script()", read(hosted), hosted)

    def test_hosted_probe_is_the_artifact_the_native_test_links(self):
        cmake = read("CMakeLists.txt")
        self.assertIn("logger/avz/hosted/atime_probe.cpp", cmake)
        self.assertIn("add_test(NAME avz_hosted_script COMMAND avz_hosted_script_tests)", cmake)
        target = cmake.split("add_executable(avz_hosted_script_tests")[1]
        self.assertIn("${AVZ_SOURCES})", target)


class HostedObservabilityTests(unittest.TestCase):
    """The live observation file exists only in a hosted build (issue #84)."""

    def assert_guarded(self, text: str, marker: str):
        """The marker must sit inside an LVZ_AVZ_HOSTED_SCRIPT block."""
        index = text.index(marker)
        guard = text.rindex("#ifdef LVZ_AVZ_HOSTED_SCRIPT", 0, index)
        self.assertNotIn("#endif", text[guard:index], f"{marker} is outside the build switch")

    def test_recorder_touches_the_observation_file_only_under_the_switch(self):
        recorder = read("logger/avz/recorder.cpp")
        for marker in ("hosted_observation::Open(", "hosted_observation::Sample(",
                       "hosted_observation::Flush()", "hosted_observation::Close()",
                       "AOnAfterTick(lvz::SampleHosted())"):
            self.assertIn(marker, recorder, marker)
            self.assert_guarded(recorder, marker)

    def test_default_build_compiles_no_observation_sources(self):
        cmake = read("CMakeLists.txt")
        recorder_sources = cmake.split("set(RECORDER_SOURCES ")[1].split("\n")[0]
        self.assertNotIn("hosted", recorder_sources)
        before_switch, switch_block = cmake.split("if(LVZ_AVZ_HOSTED_SCRIPT)", 1)
        self.assertIn("target_sources(recorder PRIVATE logger/avz/hosted_observation.cpp "
                      "logger/avz/hosted/observe_default.cpp)", switch_block)
        self.assertNotIn("hosted_observation.cpp", before_switch)
        self.assertNotIn("observe_default.cpp", before_switch)
        writer = read("logger/avz/hosted_observation.cpp")
        self.assertIn("bool Enabled() { return false; }", writer.split("#else", 1)[1])

    def test_observation_file_lives_in_the_archive_scoped_decisions_directory(self):
        header = read("logger/avz/hosted_observation.hpp")
        self.assertIn('kRelativePath = "decisions/hosted-script.jsonl"', header)
        documentation = read("docs/avz-script-hosting.md")
        self.assertIn("decisions\\hosted-script.jsonl", documentation)
        self.assertIn("build-hosted.ps1", documentation)
        self.assertIn("archive_policy", read("src/llm_vs_zombies/records.py"))

    def test_probe_publishes_the_counters_the_documented_format_names(self):
        probe = read("logger/avz/hosted/atime_probe.cpp")
        # The wait sequence is what the offline test asserts; the publisher only
        # reports the state it leaves behind.
        self.assertIn("for (int offset : kProbeWaits)", probe)
        self.assertIn("{-599, -549, -499}", read("logger/avz/hosted/atime_probe.hpp"))
        self.assertIn("void Observe(std::string& fields)", probe)
        for member in ("started", "resumes", "resume_wave", "resume_time", "finished", "clock_at_finish"):
            self.assertIn(f"\\\"{member}\\\":", probe)
        self.assertIn("void Observe(std::string& fields);", read("logger/avz/hosted_script.hpp"))
        self.assertIn("__attribute__((weak)) void Observe(std::string&) {}",
                      read("logger/avz/hosted/observe_default.cpp"))

    def test_both_build_modes_of_the_writer_are_tested(self):
        cmake = read("CMakeLists.txt")
        self.assertIn("add_test(NAME hosted_observation COMMAND hosted_observation_tests)", cmake)
        self.assertIn("add_test(NAME hosted_observation_default COMMAND hosted_observation_default_tests)", cmake)
        default_target = cmake.split("add_executable(hosted_observation_default_tests")[1]
        self.assertNotIn("LVZ_AVZ_HOSTED_SCRIPT",
                         default_target.split("add_test(NAME hosted_observation_default")[0])
        build_hosted = read("tools/build-hosted.ps1")
        self.assertIn("-DLVZ_AVZ_HOSTED_SCRIPT=$scriptPath", build_hosted)
        self.assertIn("recorder_sha256", build_hosted)
        self.assertIn("Get-FileHash", build_hosted)


if __name__ == "__main__":
    unittest.main()
