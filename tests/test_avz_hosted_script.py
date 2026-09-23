"""AvZ script hosting stays compile-time, opt-in and on the runtime frame path (#82)."""
# The offline proof that a hosted coroutine advances lives in
# tests/avz_hosted_script_tests.cpp; these checks keep the wiring it depends on
# from silently drifting: the script entry stays a link-time symbol, the overlay
# keeps RunScript() on the frame path, and the hosted launch stays behind the
# build switch. tests/test_avz_hosted_script.py also guards the live
# observability path (issue #84) the same way: <run>/decisions/hosted-script.jsonl
# may only be touched by a build compiled with LVZ_AVZ_HOSTED_SCRIPT.
from __future__ import annotations

import hashlib
import re
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
        self.assertIn("logger/avz/hosted_observation.cpp", switch_block)
        self.assertIn("logger/avz/hosted_crash_probe.cpp", switch_block)
        self.assertIn("logger/avz/hosted/observe_default.cpp", switch_block)
        self.assertNotIn("hosted_observation.cpp", before_switch)
        self.assertNotIn("hosted_crash_probe.cpp", before_switch)
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


class HostedCrashProbeTests(unittest.TestCase):
    """The first-chance exception log exists only in a hosted build (issue #88)."""

    def assert_guarded(self, text: str, marker: str):
        index = text.index(marker)
        guard = text.rindex("#ifdef LVZ_AVZ_HOSTED_SCRIPT", 0, index)
        self.assertNotIn("#endif", text[guard:index], f"{marker} is outside the build switch")

    def test_recorder_uses_the_crash_probe_only_under_the_switch(self):
        recorder = read("logger/avz/recorder.cpp")
        for marker in ("hosted_crash_probe::Open(", "hosted_crash_probe::Context(",
                       "hosted_crash_probe::Close()", '#include "hosted_crash_probe.hpp"'):
            self.assertIn(marker, recorder, marker)
            if not marker.startswith("#include"):
                self.assert_guarded(recorder, marker)

    def test_the_handler_never_handles_the_exception(self):
        probe = read("logger/avz/hosted_crash_probe.cpp")
        # A vectored handler that returned anything but CONTINUE_SEARCH would
        # change the game's and AvZ's handling; the probe only witnesses.
        self.assertIn("return EXCEPTION_CONTINUE_SEARCH;", probe)
        self.assertNotIn("CONTINUE_EXECUTION", probe)
        self.assertNotIn("EXCEPTION_EXECUTE_HANDLER", probe)
        self.assertIn("AddVectoredExceptionHandler(1, Handler)", probe)

    def test_crash_log_lives_in_the_archive_scoped_decisions_directory(self):
        header = read("logger/avz/hosted_crash_probe.hpp")
        self.assertIn('kRelativePath = "decisions/hosted-crash-probe.log"', header)
        self.assertIn("bool Enabled() { return false; }",
                      read("logger/avz/hosted_crash_probe.cpp").split("#else", 1)[1])

    def test_both_build_modes_of_the_probe_are_tested(self):
        cmake = read("CMakeLists.txt")
        self.assertIn("add_test(NAME hosted_crash_probe COMMAND hosted_crash_probe_tests)", cmake)
        self.assertIn("add_test(NAME hosted_crash_probe_default COMMAND hosted_crash_probe_default_tests)", cmake)
        default_target = cmake.split("add_executable(hosted_crash_probe_default_tests")[1]
        self.assertNotIn("LVZ_AVZ_HOSTED_SCRIPT",
                         default_target.split("add_test(NAME hosted_crash_probe_default")[0])
class HostedFireAuditWiringTests(unittest.TestCase):
    """Hosted cannon shots are audited only in a hosted build (issue #84)."""

    def assert_guarded(self, text: str, marker: str):
        index = text.index(marker)
        guard = text.rindex("#ifdef LVZ_AVZ_HOSTED_FIRE_AUDIT", 0, index)
        self.assertNotIn("#endif", text[guard:index], f"{marker} is outside the build switch")

    def test_overlay_is_one_guarded_insertion_at_the_reviewed_anchor(self):
        overlay = read("runtime/avz_overlay.cmake")
        self.assertIn("    AAsm::Fire(x, y, cobIdx);", overlay)
        # One call, immediately after the reviewed engine call, compiled only
        # when the switch is defined.
        self.assertIn(r"${cob_fire_call}\n#ifdef LVZ_AVZ_HOSTED_FIRE_AUDIT\n"
                      r"    lvz::runtime::RecordHostedFire(cobIdx, plant->Id(), plant->Row() + 1, "
                      r"plant->Col() + 1, dropRow, dropCol);\n#endif", overlay)
        self.assertIn(r"#ifdef LVZ_AVZ_HOSTED_FIRE_AUDIT\n#include \"runtime/hosted_fire.hpp\"\n"
                      r"#endif\n${cob_source}", overlay)
        # The anchor must be present and unique, or cmake fails before building.
        self.assertIn('string(FIND "${cob_source}" "${cob_fire_call}" cob_fire_position)', overlay)
        self.assertIn("Reviewed cannon call site is missing", overlay)
        self.assertIn("Reviewed cannon call site is not unique", overlay)
        self.assertIn("Pinned AvZ cob manager source changed", overlay)

    def test_pinned_cob_manager_hash_matches_the_reviewed_file(self):
        overlay = read("runtime/avz_overlay.cmake")
        pin = re.search(r'cob_hash STREQUAL "([0-9a-f]{64})"', overlay).group(1)
        # Same three steps as runtime/avz_overlay.cmake and prepare-checkout:
        # read, normalise CRLF, SHA256.
        source = (ROOT / "avz/framework/src/avz_cob_manager.cpp").read_bytes().replace(b"\r\n", b"\n")
        self.assertEqual(hashlib.sha256(source).hexdigest(), pin)

    def test_the_overlay_replaces_the_source_only_under_the_switch(self):
        overlay = read("runtime/avz_overlay.cmake")
        marker = 'set(AVZ_SOURCES "${AVZ_SOURCES_COB_OVERLAY}")'
        guard = overlay.index("if(LVZ_AVZ_HOSTED_SCRIPT)")
        self.assertIn(marker, overlay[guard:])
        self.assertNotIn("endif()", overlay[guard:overlay.index(marker)])
        self.assertIn("list(REMOVE_ITEM AVZ_SOURCES_COB_OVERLAY", overlay)
        self.assertIn("list(APPEND AVZ_SOURCES_COB_OVERLAY", overlay)

    def test_default_build_compiles_no_fire_sources(self):
        cmake = read("CMakeLists.txt")
        recorder_sources = cmake.split("set(RECORDER_SOURCES ")[1].split("\n")[0]
        self.assertNotIn("hosted_fire", recorder_sources)
        before_switch, rest = cmake.split("if(LVZ_AVZ_HOSTED_SCRIPT)", 1)
        # The top-level endif() is the only one at column zero; nested conditions
        # inside the switch are indented.
        switch_block = "\n".join(rest.split("\n")[:next(index for index, line in enumerate(rest.split("\n"))
                                                      if line == "endif()")])
        self.assertIn("target_sources(recorder PRIVATE runtime/hosted_fire.cpp determinism/hosted_fire.cpp)",
                      switch_block)
        self.assertIn("target_compile_definitions(recorder PRIVATE LVZ_AVZ_HOSTED_FIRE_AUDIT=1)", switch_block)
        self.assertNotIn("hosted_fire", before_switch)

    def test_both_fire_build_modes_are_tested(self):
        cmake = read("CMakeLists.txt")
        self.assertIn("add_test(NAME avz_hosted_fire COMMAND avz_hosted_fire_tests)", cmake)
        self.assertIn("add_test(NAME avz_hosted_fire_default COMMAND avz_hosted_fire_default_tests)", cmake)
        self.assertIn("set_tests_properties(avz_hosted_fire PROPERTIES DEPENDS avz_hosted_fire_default)", cmake)
        default_target = cmake.split("add_executable(avz_hosted_fire_default_tests")[1].split(
            "add_test(NAME avz_hosted_fire_default")[0]
        self.assertNotIn("LVZ_AVZ_HOSTED_FIRE_AUDIT", default_target)
        self.assertIn("${AVZ_SOURCES_COB_PRISTINE}", default_target)
        hosted_target = cmake.split("add_executable(avz_hosted_fire_tests")[1].split(
            "add_test(NAME avz_hosted_fire ")[0]
        self.assertIn("LVZ_AVZ_HOSTED_FIRE_AUDIT=1", hosted_target)
        self.assertIn("${AVZ_SOURCES_COB_OVERLAY}", hosted_target)
        # The engine entry points only exist inside the game, so the test keeps
        # counting doubles on that boundary.
        flags = cmake.split("set(LVZ_FIRE_TEST_FLAGS")[1].split(")")[0]
        for wrapped in ("_ZN4AAsm4FireEiii", "_ZN4AAsm12ReleaseMouseEv", "_ZN4AAsm14GridToOrdinateEii"):
            self.assertIn(f"-Wl,--wrap={wrapped}", flags)
        self.assertIn("${LVZ_FIRE_TEST_FLAGS}", hosted_target)

    def test_writer_reader_and_documentation_agree_on_the_declared_mode(self):
        audit = read("determinism/audit.cpp")
        for marker in ("DrainHostedFires();", 'state["hosted_fire"]=HostedFireState();',
                       'result["hosted_fire"]=HostedFireManifest();'):
            self.assertIn(marker, audit)
            self.assert_guarded(audit, marker)
        # A recording fault invalidates the run through the runtime instead of
        # throwing into the coroutine that fired; both halves sit in the switch.
        runtime = read("runtime/runtime.cpp")
        for marker in ("Json CurrentVersion() { return controller?controller->Version():Json(); }",
                       "void ReportAuditFault(const std::string& message) { if(controller) controller->Fail(message); }"):
            self.assertIn(marker, runtime)
            self.assert_guarded(runtime, marker)
        hook = read("runtime/hosted_fire.cpp")
        self.assertIn("ReportAuditFault(error.what());", hook)
        self.assertIn("catch (...)", hook)
        module = read("determinism/hosted_fire.cpp")
        default_branch = module.split("#else", 1)[1]
        self.assertIn("bool HostedFireEnabled() { return false; }", default_branch)
        self.assertIn("nlohmann::json HostedFireManifest() { return nlohmann::json::object(); }", default_branch)
        for key in ("mode", "installed", "kind", "source", "hook",
                    "original_engine_bitwise_unmodified", "semantic_change", "boundary"):
            self.assertIn(f'{{"{key}"', module)
        reader = read("src/llm_vs_zombies/audit_compare.py")
        self.assertIn("hosted_fire_mode(manifest)", reader)
        self.assertIn('raise EvidenceError("hosted fire record lacks its declared audit mode")', reader)
        self.assertIn('raise EvidenceError("hosted fire state requires an explicit audit mode")', reader)
        self.assertIn('raise EvidenceError("hosted fire record has no following audited boundary")', reader)
        documentation = read("docs/avz-script-hosting.md")
        self.assertIn("## 8.", documentation)
        self.assertIn("hosted_fire", documentation)
        self.assertIn("不是** `action`", documentation)


if __name__ == "__main__":
    unittest.main()
