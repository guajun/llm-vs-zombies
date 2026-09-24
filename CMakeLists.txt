cmake_minimum_required(VERSION 3.24)
project(llm_vs_zombies LANGUAGES C CXX)
if(NOT WIN32 OR NOT CMAKE_SIZEOF_VOID_P EQUAL 4)
  message(FATAL_ERROR "Use the project LLVM-MinGW i686 toolchain: tools/build-avz.ps1")
endif()
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
file(GLOB AVZ_SOURCES CONFIGURE_DEPENDS avz/framework/src/*.cpp avz/framework/src/minhook/*.cpp)
# Optional AvZ script hosted by the resident runtime. The named source must
# define `ACoroutine lvz::hosted::Script()` (logger/avz/hosted_script.hpp); the
# recorder launches it from inside AScript(), so frame ownership, pause and the
# engine-call boundary stay with the runtime. Empty by default: no script is
# linked and the recorded behaviour is identical to the current build.
# Example: -DLVZ_AVZ_HOSTED_SCRIPT=logger/avz/hosted/atime_probe.cpp
#
# Declared before runtime/avz_overlay.cmake because a hosted build replaces the
# pinned AvZ cob manager with its audit-only overlay (hosted cannon shots are
# direct engine calls and never reach the request journal; see section 8 of
# docs/avz-script-hosting.md) and the overlay include owns that source list.
set(LVZ_AVZ_HOSTED_SCRIPT "" CACHE FILEPATH "Optional AvZ script compiled into the resident runtime")
include(runtime/avz_overlay.cmake)
set(RECORDER_SOURCES logger/avz/recorder.cpp runtime/controller.cpp runtime/runtime.cpp runtime/pipe_server.cpp determinism/audit.cpp determinism/fp_environment.cpp determinism/app_update_anchor.cpp determinism/mj_clock_anchor.cpp determinism/b0_normalization.cpp determinism/sound_counter_origin.cpp determinism/silent_audio_audit.cpp determinism/spawn_hook.cpp determinism/reanimation_audit.cpp determinism/particle_shake.cpp determinism/foley_trace.cpp recording/native_capture.cpp recording/draw_gate.cpp ${AVZ_SOURCES})
add_library(recorder SHARED ${RECORDER_SOURCES})
target_include_directories(recorder PRIVATE ${CMAKE_SOURCE_DIR} avz/framework/inc logger/avz runtime/vendor)
target_compile_options(recorder PRIVATE -Wall -Wextra -Werror=return-type -Werror=unused-result $<$<COMPILE_LANGUAGE:CXX>:-fexperimental-library>)
target_link_options(recorder PRIVATE -static)
target_link_libraries(recorder PRIVATE gdi32 dbghelp crypt32 bcrypt)
# Where recorder.dll lands. The default is the historical build/ path every
# tool expects; tools/build-hosted.ps1 points it elsewhere so a hosted DLL can
# be produced without touching the default one.
set(LVZ_RECORDER_OUTPUT_DIR "${CMAKE_SOURCE_DIR}/build" CACHE PATH "Directory recorder.dll is written to")
set_target_properties(recorder PROPERTIES PREFIX "" RUNTIME_OUTPUT_DIRECTORY "${LVZ_RECORDER_OUTPUT_DIR}" LIBRARY_OUTPUT_DIRECTORY "${LVZ_RECORDER_OUTPUT_DIR}")
if(LVZ_AVZ_HOSTED_SCRIPT)
  if(NOT IS_ABSOLUTE "${LVZ_AVZ_HOSTED_SCRIPT}")
    get_filename_component(LVZ_AVZ_HOSTED_SCRIPT "${CMAKE_SOURCE_DIR}/${LVZ_AVZ_HOSTED_SCRIPT}" ABSOLUTE)
  endif()
  if(NOT EXISTS "${LVZ_AVZ_HOSTED_SCRIPT}")
    message(FATAL_ERROR "LVZ_AVZ_HOSTED_SCRIPT does not exist: ${LVZ_AVZ_HOSTED_SCRIPT}")
  endif()
  get_filename_component(LVZ_AVZ_HOSTED_SCRIPT_NAME "${LVZ_AVZ_HOSTED_SCRIPT}" NAME)
  target_sources(recorder PRIVATE "${LVZ_AVZ_HOSTED_SCRIPT}")
  # The observation writer and the weak default publisher exist only in a
  # hosted build; the name below is what the activation line in
  # <run>/decisions/hosted-script.jsonl reports.
  # hosted_crash_probe.cpp is the same shape: it is the first-chance exception
  # log (issue #88), and a default build links neither it nor its handler.
  target_sources(recorder PRIVATE logger/avz/hosted_observation.cpp logger/avz/hosted_crash_probe.cpp
    logger/avz/hosted/observe_default.cpp)
  target_compile_definitions(recorder PRIVATE LVZ_AVZ_HOSTED_SCRIPT=1)
  target_compile_definitions(recorder PRIVATE "LVZ_AVZ_HOSTED_SCRIPT_NAME=\"${LVZ_AVZ_HOSTED_SCRIPT_NAME}\"")
  # Audit-only hosted-cannon policy: the overlay of the pinned cob manager plus
  # the record/state module. Both exist only here, so a default build compiles
  # neither (tests/test_avz_hosted_script.py asserts that wiring, and
  # tests/avz_hosted_fire_tests.cpp is built both ways).
  target_sources(recorder PRIVATE runtime/hosted_fire.cpp determinism/hosted_fire.cpp)
  target_compile_definitions(recorder PRIVATE LVZ_AVZ_HOSTED_FIRE_AUDIT=1)
  message(STATUS "Hosting AvZ script: ${LVZ_AVZ_HOSTED_SCRIPT}")
endif()
# Explicitly requested lifecycle probe; never built by the default all target
# and never contains/replaces the production recorder artifact.
add_library(recorder_flag_fixture SHARED EXCLUDE_FROM_ALL ${RECORDER_SOURCES} tests/flag_drop_live.cpp)
target_compile_definitions(recorder_flag_fixture PRIVATE LVZ_FLAG_DROP_LIVE_FIXTURE)
target_include_directories(recorder_flag_fixture PRIVATE ${CMAKE_SOURCE_DIR} avz/framework/inc logger/avz runtime/vendor)
target_compile_options(recorder_flag_fixture PRIVATE -Wall -Wextra -Werror=return-type -Werror=unused-result $<$<COMPILE_LANGUAGE:CXX>:-fexperimental-library>)
target_link_options(recorder_flag_fixture PRIVATE -static)
target_link_libraries(recorder_flag_fixture PRIVATE gdi32 dbghelp crypt32 bcrypt)
set_target_properties(recorder_flag_fixture PROPERTIES PREFIX "" RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build" LIBRARY_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_executable(logger_smoke tests/logger_smoke.cpp)
target_include_directories(logger_smoke PRIVATE logger/avz)
target_link_options(logger_smoke PRIVATE -static)
set_target_properties(logger_smoke PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_executable(runtime_tests tests/runtime_tests.cpp runtime/controller.cpp runtime/pipe_server.cpp)
target_include_directories(runtime_tests PRIVATE runtime runtime/vendor)
target_link_options(runtime_tests PRIVATE -static)
set_target_properties(runtime_tests PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
enable_testing()
add_test(NAME runtime_protocol COMMAND runtime_tests)
add_executable(runtime_spawn_action_tests tests/runtime_spawn_action.cpp)
target_include_directories(runtime_spawn_action_tests PRIVATE runtime runtime/vendor)
target_link_options(runtime_spawn_action_tests PRIVATE -static)
set_target_properties(runtime_spawn_action_tests PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_test(NAME runtime_spawn_action COMMAND runtime_spawn_action_tests)
add_executable(runtime_journal_tests tests/runtime_journal_tests.cpp runtime/controller.cpp)
target_compile_definitions(runtime_journal_tests PRIVATE LVZ_REQUEST_JOURNAL_TESTING)
target_include_directories(runtime_journal_tests PRIVATE runtime runtime/vendor)
target_link_options(runtime_journal_tests PRIVATE -static)
target_link_libraries(runtime_journal_tests PRIVATE psapi)
set_target_properties(runtime_journal_tests PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_test(NAME runtime_request_journal COMMAND runtime_journal_tests)
add_executable(runtime_branch_scope_tests tests/runtime_branch_scope_tests.cpp runtime/controller.cpp)
target_include_directories(runtime_branch_scope_tests PRIVATE runtime runtime/vendor)
target_link_options(runtime_branch_scope_tests PRIVATE -static)
set_target_properties(runtime_branch_scope_tests PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_test(NAME runtime_branch_scope COMMAND runtime_branch_scope_tests)
# The launcher-side branch identity grammar, pinned to the same rule the
# runtime (RequestJournal::ValidBranch) and the evidence tree use.
add_executable(branch_id_tests tests/branch_id_tests.cpp)
target_include_directories(branch_id_tests PRIVATE ${CMAKE_SOURCE_DIR} runtime/vendor)
target_link_options(branch_id_tests PRIVATE -static)
set_target_properties(branch_id_tests PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_test(NAME launcher_branch_id COMMAND branch_id_tests)
add_executable(determinism_model tests/determinism_model.cpp)
target_include_directories(determinism_model PRIVATE ${CMAKE_SOURCE_DIR} runtime/vendor)
target_link_options(determinism_model PRIVATE -static)
set_target_properties(determinism_model PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_test(NAME determinism_model COMMAND determinism_model)
add_executable(determinism_json_diff tests/determinism_json_diff.cpp)
target_include_directories(determinism_json_diff PRIVATE ${CMAKE_SOURCE_DIR} runtime/vendor)
target_link_options(determinism_json_diff PRIVATE -static)
set_target_properties(determinism_json_diff PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_test(NAME determinism_json_diff COMMAND determinism_json_diff)
file(GLOB SPAWN_TEST_MINHOOK_SOURCES CONFIGURE_DEPENDS avz/framework/src/minhook/*.cpp)
add_executable(determinism_spawn_hook tests/determinism_spawn_hook.cpp determinism/spawn_hook.cpp ${SPAWN_TEST_MINHOOK_SOURCES})
target_compile_definitions(determinism_spawn_hook PRIVATE LVZ_SPAWN_HOOK_TESTING)
target_include_directories(determinism_spawn_hook PRIVATE ${CMAKE_SOURCE_DIR} runtime/vendor avz/framework/inc)
target_link_options(determinism_spawn_hook PRIVATE -static)
set_target_properties(determinism_spawn_hook PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_test(NAME determinism_spawn_hook COMMAND determinism_spawn_hook)
add_executable(determinism_reanimation tests/determinism_reanimation.cpp determinism/reanimation_audit.cpp)
target_include_directories(determinism_reanimation PRIVATE ${CMAKE_SOURCE_DIR} runtime/vendor)
target_link_options(determinism_reanimation PRIVATE -static)
set_target_properties(determinism_reanimation PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_test(NAME determinism_reanimation COMMAND determinism_reanimation)
add_executable(flag_fixture_tests tests/flag_drop_fixture_tests.cpp)
target_include_directories(flag_fixture_tests PRIVATE ${CMAKE_SOURCE_DIR} runtime/vendor)
target_link_options(flag_fixture_tests PRIVATE -static)
set_target_properties(flag_fixture_tests PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_test(NAME flag_fixture_boundary_abi COMMAND flag_fixture_tests)
add_executable(determinism_particle_shake tests/determinism_particle_shake.cpp determinism/particle_shake.cpp)
target_compile_definitions(determinism_particle_shake PRIVATE LVZ_PARTICLE_SHAKE_TESTING)
target_include_directories(determinism_particle_shake PRIVATE ${CMAKE_SOURCE_DIR} runtime/vendor)
target_link_options(determinism_particle_shake PRIVATE -static)
set_target_properties(determinism_particle_shake PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_test(NAME determinism_particle_shake COMMAND determinism_particle_shake)
add_executable(native_capture_tests tests/native_capture_tests.cpp recording/native_capture.cpp recording/draw_gate.cpp ${SPAWN_TEST_MINHOOK_SOURCES})
target_include_directories(native_capture_tests PRIVATE ${CMAKE_SOURCE_DIR} runtime/vendor avz/framework/inc)
target_link_options(native_capture_tests PRIVATE -static)
set_target_properties(native_capture_tests PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_test(NAME native_capture COMMAND native_capture_tests)
add_executable(draw_gate_tests tests/draw_gate_tests.cpp recording/draw_gate.cpp ${SPAWN_TEST_MINHOOK_SOURCES})
target_compile_definitions(draw_gate_tests PRIVATE LVZ_DRAW_GATE_TESTING)
target_include_directories(draw_gate_tests PRIVATE ${CMAKE_SOURCE_DIR} runtime/vendor avz/framework/inc)
target_link_options(draw_gate_tests PRIVATE -static)
set_target_properties(draw_gate_tests PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_test(NAME native_draw_gate COMMAND draw_gate_tests)
add_executable(runtime_render_tests tests/runtime_render_tests.cpp runtime/controller.cpp)
target_include_directories(runtime_render_tests PRIVATE ${CMAKE_SOURCE_DIR} runtime runtime/vendor)
target_link_options(runtime_render_tests PRIVATE -static)
set_target_properties(runtime_render_tests PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_test(NAME runtime_render_schedule COMMAND runtime_render_tests)
add_executable(runtime_engine_call_tests tests/runtime_engine_call_tests.cpp runtime/controller.cpp)
target_compile_definitions(runtime_engine_call_tests PRIVATE LVZ_REQUEST_JOURNAL_TESTING)
target_include_directories(runtime_engine_call_tests PRIVATE ${CMAKE_SOURCE_DIR} runtime runtime/vendor)
target_link_options(runtime_engine_call_tests PRIVATE -static)
set_target_properties(runtime_engine_call_tests PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_test(NAME runtime_engine_call_boundary COMMAND runtime_engine_call_tests)
add_executable(foley_trace_tests tests/foley_trace_tests.cpp determinism/foley_trace.cpp ${SPAWN_TEST_MINHOOK_SOURCES})
target_compile_definitions(foley_trace_tests PRIVATE LVZ_FOLEY_TRACE_TESTING)
target_include_directories(foley_trace_tests PRIVATE ${CMAKE_SOURCE_DIR} runtime/vendor avz/framework/inc)
target_link_options(foley_trace_tests PRIVATE -static)
set_target_properties(foley_trace_tests PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_test(NAME native_foley_trace COMMAND foley_trace_tests)

add_executable(silent_audio_tests tests/silent_audio.cpp launcher/silent_audio.cpp determinism/silent_audio_audit.cpp determinism/sound_counter_origin.cpp determinism/app_update_anchor.cpp)
target_compile_definitions(silent_audio_tests PRIVATE LVZ_SILENT_AUDIO_TESTING)
target_include_directories(silent_audio_tests PRIVATE ${CMAKE_SOURCE_DIR} runtime/vendor)
target_link_libraries(silent_audio_tests PRIVATE bcrypt)
target_link_options(silent_audio_tests PRIVATE -static)
set_target_properties(silent_audio_tests PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_test(NAME silent_audio_contract COMMAND silent_audio_tests)

add_executable(app_update_anchor_tests tests/app_update_anchor.cpp determinism/app_update_anchor.cpp runtime/controller.cpp)
target_include_directories(app_update_anchor_tests PRIVATE ${CMAKE_SOURCE_DIR} runtime runtime/vendor)
target_link_options(app_update_anchor_tests PRIVATE -static)
set_target_properties(app_update_anchor_tests PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_test(NAME initial_app_update_anchor COMMAND app_update_anchor_tests)

add_executable(mj_clock_anchor_tests tests/mj_clock_anchor.cpp determinism/mj_clock_anchor.cpp determinism/app_update_anchor.cpp runtime/controller.cpp)
target_include_directories(mj_clock_anchor_tests PRIVATE ${CMAKE_SOURCE_DIR} runtime runtime/vendor)
target_link_options(mj_clock_anchor_tests PRIVATE -static)
set_target_properties(mj_clock_anchor_tests PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_test(NAME initial_mj_clock_anchor COMMAND mj_clock_anchor_tests)

add_executable(sound_counter_origin_tests tests/sound_counter_origin.cpp determinism/sound_counter_origin.cpp determinism/app_update_anchor.cpp runtime/controller.cpp)
target_include_directories(sound_counter_origin_tests PRIVATE ${CMAKE_SOURCE_DIR} runtime runtime/vendor)
target_link_options(sound_counter_origin_tests PRIVATE -static)
set_target_properties(sound_counter_origin_tests PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_test(NAME sound_counter_origin COMMAND sound_counter_origin_tests)

add_executable(b0_normalization_tests tests/b0_normalization.cpp determinism/b0_normalization.cpp determinism/app_update_anchor.cpp runtime/controller.cpp)
target_include_directories(b0_normalization_tests PRIVATE ${CMAKE_SOURCE_DIR} runtime runtime/vendor)
target_link_options(b0_normalization_tests PRIVATE -static)
set_target_properties(b0_normalization_tests PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_test(NAME initial_b0_normalization COMMAND b0_normalization_tests)

add_executable(fp_environment_tests tests/fp_environment_tests.cpp determinism/fp_environment.cpp runtime/controller.cpp)
target_include_directories(fp_environment_tests PRIVATE ${CMAKE_SOURCE_DIR} runtime runtime/vendor)
target_link_options(fp_environment_tests PRIVATE -static)
set_target_properties(fp_environment_tests PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_test(NAME fixed_owner_fp COMMAND fp_environment_tests)

# Frame ownership of a hosted AvZ script: the generated overlay of the pinned
# avz_script.cpp, AvZ's coroutine/time-queue/tick-runner TUs and the hosted
# probe script run against a fake PvZ image, with the runtime frame gate as the
# only test double on the boundary. See tests/avz_hosted_script_tests.cpp.
add_executable(avz_hosted_script_tests
  tests/avz_hosted_script_tests.cpp
  logger/avz/hosted/atime_probe.cpp
  logger/avz/hosted/observe_default.cpp
  ${AVZ_SOURCES})
target_compile_definitions(avz_hosted_script_tests PRIVATE LVZ_AVZ_HOSTED_SCRIPT=1)
target_include_directories(avz_hosted_script_tests PRIVATE ${CMAKE_SOURCE_DIR} avz/framework/inc logger/avz runtime/vendor)
target_compile_options(avz_hosted_script_tests PRIVATE -Wall -Wextra -Werror=return-type -Werror=unused-result $<$<COMPILE_LANGUAGE:CXX>:-fexperimental-library>)
# AvZ reads the App pointer at the fixed address 0x6a9ec0 and saves original
# game bytes at a fixed RVA above the image base. The test binary therefore
# keeps its natural image base (no ASLR) and pads its image past the App cell
# (kAppCellImagePadding in the test), so that cell is this module's own page
# whatever the optimized link happens to be.
target_link_options(avz_hosted_script_tests PRIVATE -static -Wl,--disable-dynamicbase)
target_link_libraries(avz_hosted_script_tests PRIVATE gdi32 dbghelp)
set_target_properties(avz_hosted_script_tests PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_test(NAME avz_hosted_script COMMAND avz_hosted_script_tests)

# Hosted-script observability (<run>/decisions/hosted-script.jsonl). The same
# source is built twice: with the build switch (the translation unit a hosted
# recorder.dll links) and without it (a default build), so one test asserts the
# file's line format and change-only rule and the other asserts that a default
# build creates no file at all.
add_executable(hosted_observation_tests tests/hosted_observation_tests.cpp logger/avz/hosted_observation.cpp)
target_compile_definitions(hosted_observation_tests PRIVATE LVZ_AVZ_HOSTED_SCRIPT=1)
target_include_directories(hosted_observation_tests PRIVATE logger/avz)
target_compile_options(hosted_observation_tests PRIVATE -Wall -Wextra -Werror=return-type -Werror=unused-result $<$<COMPILE_LANGUAGE:CXX>:-fexperimental-library>)
target_link_options(hosted_observation_tests PRIVATE -static)
set_target_properties(hosted_observation_tests PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_test(NAME hosted_observation COMMAND hosted_observation_tests)

add_executable(hosted_observation_default_tests tests/hosted_observation_tests.cpp logger/avz/hosted_observation.cpp)
target_include_directories(hosted_observation_default_tests PRIVATE logger/avz)
target_compile_options(hosted_observation_default_tests PRIVATE -Wall -Wextra -Werror=return-type -Werror=unused-result $<$<COMPILE_LANGUAGE:CXX>:-fexperimental-library>)
target_link_options(hosted_observation_default_tests PRIVATE -static)
set_target_properties(hosted_observation_default_tests PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_test(NAME hosted_observation_default COMMAND hosted_observation_default_tests)

# Hosted first-chance exception log (<run>/decisions/hosted-crash-probe.log).
# Same two-way build as the observation writer: the hosted target raises a real
# access violation in a child process and reads the line it left behind, the
# default target asserts the file is never created.
add_executable(hosted_crash_probe_tests tests/hosted_crash_probe_tests.cpp logger/avz/hosted_crash_probe.cpp)
target_compile_definitions(hosted_crash_probe_tests PRIVATE LVZ_AVZ_HOSTED_SCRIPT=1)
target_include_directories(hosted_crash_probe_tests PRIVATE logger/avz)
target_compile_options(hosted_crash_probe_tests PRIVATE -Wall -Wextra -Werror=return-type -Werror=unused-result $<$<COMPILE_LANGUAGE:CXX>:-fexperimental-library>)
target_link_options(hosted_crash_probe_tests PRIVATE -static)
set_target_properties(hosted_crash_probe_tests PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_test(NAME hosted_crash_probe COMMAND hosted_crash_probe_tests)

add_executable(hosted_crash_probe_default_tests tests/hosted_crash_probe_tests.cpp logger/avz/hosted_crash_probe.cpp)
target_include_directories(hosted_crash_probe_default_tests PRIVATE logger/avz)
target_compile_options(hosted_crash_probe_default_tests PRIVATE -Wall -Wextra -Werror=return-type -Werror=unused-result $<$<COMPILE_LANGUAGE:CXX>:-fexperimental-library>)
target_link_options(hosted_crash_probe_default_tests PRIVATE -static)
set_target_properties(hosted_crash_probe_default_tests PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_test(NAME hosted_crash_probe_default COMMAND hosted_crash_probe_default_tests)
# Hosted cannon shots (issue #84, open item 2). aCobManager.Fire is a direct
# engine call, so the runtime's request journal never sees it; the audit-only
# overlay of the pinned AvZ cob manager adds one record per shot instead. Two
# targets are built from one test source:
#   * avz_hosted_fire_tests         - the overlay plus the real record path
#     (runtime/hosted_fire.cpp, determinism/hosted_fire.cpp), i.e. a hosted
#     recorder.dll. It must produce exactly one audit record per accepted shot
#     and the same engine call sequence as the default build.
#   * avz_hosted_fire_default_tests - the pristine upstream cob manager and the
#     same record module without LVZ_AVZ_HOSTED_FIRE_AUDIT, i.e. a default
#     recorder.dll. It writes the reference engine trace the hosted build
#     compares against and must produce no audit record at all.
# The engine entry points are redirected with -Wl,--wrap because the real ones
# are inline-asm calls into PvZ at fixed RVAs that exist only in the game.
# See tests/avz_hosted_fire_tests.cpp and docs/avz-script-hosting.md section 8.
set(LVZ_FIRE_TRACE_ROOT "${CMAKE_BINARY_DIR}/avz-hosted-fire")
set(LVZ_FIRE_TEST_FLAGS -static -Wl,--disable-dynamicbase
  -Wl,--wrap=_ZN4AAsm4FireEiii -Wl,--wrap=_ZN4AAsm12ReleaseMouseEv
  -Wl,--wrap=_ZN4AAsm14GridToOrdinateEii)
add_executable(avz_hosted_fire_tests
  tests/avz_hosted_fire_tests.cpp
  runtime/hosted_fire.cpp
  determinism/hosted_fire.cpp
  ${AVZ_SOURCES_COB_OVERLAY})
target_compile_definitions(avz_hosted_fire_tests PRIVATE LVZ_AVZ_HOSTED_FIRE_AUDIT=1
  "LVZ_FIRE_TRACE_ROOT=\"${LVZ_FIRE_TRACE_ROOT}\"")
target_include_directories(avz_hosted_fire_tests PRIVATE ${CMAKE_SOURCE_DIR} avz/framework/inc logger/avz runtime/vendor)
target_compile_options(avz_hosted_fire_tests PRIVATE -Wall -Wextra -Werror=return-type -Werror=unused-result $<$<COMPILE_LANGUAGE:CXX>:-fexperimental-library>)
target_link_options(avz_hosted_fire_tests PRIVATE ${LVZ_FIRE_TEST_FLAGS})
target_link_libraries(avz_hosted_fire_tests PRIVATE gdi32 dbghelp)
set_target_properties(avz_hosted_fire_tests PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_test(NAME avz_hosted_fire COMMAND avz_hosted_fire_tests)

add_executable(avz_hosted_fire_default_tests
  tests/avz_hosted_fire_tests.cpp
  runtime/hosted_fire.cpp
  determinism/hosted_fire.cpp
  ${AVZ_SOURCES_COB_PRISTINE})
target_compile_definitions(avz_hosted_fire_default_tests PRIVATE
  "LVZ_FIRE_TRACE_ROOT=\"${LVZ_FIRE_TRACE_ROOT}\"")
target_include_directories(avz_hosted_fire_default_tests PRIVATE ${CMAKE_SOURCE_DIR} avz/framework/inc logger/avz runtime/vendor)
target_compile_options(avz_hosted_fire_default_tests PRIVATE -Wall -Wextra -Werror=return-type -Werror=unused-result $<$<COMPILE_LANGUAGE:CXX>:-fexperimental-library>)
target_link_options(avz_hosted_fire_default_tests PRIVATE ${LVZ_FIRE_TEST_FLAGS})
target_link_libraries(avz_hosted_fire_default_tests PRIVATE gdi32 dbghelp)
set_target_properties(avz_hosted_fire_default_tests PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build")
add_test(NAME avz_hosted_fire_default COMMAND avz_hosted_fire_default_tests)
# The hosted test compares its engine trace against the default build's, so the
# reference has to exist first (ctest honours DEPENDS even with -j).
set_tests_properties(avz_hosted_fire PROPERTIES DEPENDS avz_hosted_fire_default)
