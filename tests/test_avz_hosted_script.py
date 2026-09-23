"""AvZ script hosting stays compile-time, opt-in and on the runtime frame path (#82)."""
# The offline proof that a hosted coroutine advances lives in
# tests/avz_hosted_script_tests.cpp; these checks keep the wiring it depends on
# from silently drifting: the script entry stays a link-time symbol, the overlay
# keeps RunScript() on the frame path, and the hosted launch stays behind the
# build switch.
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


if __name__ == "__main__":
    unittest.main()
