# The submodule remains pristine. Fail closed if the reviewed upstream sources change.
set(AVZ_SCRIPT "${CMAKE_SOURCE_DIR}/avz/framework/src/avz_script.cpp")
set(AVZ_HOOK "${CMAKE_SOURCE_DIR}/avz/framework/src/avz_hook.cpp")
set(AVZ_CARD "${CMAKE_SOURCE_DIR}/avz/framework/src/avz_card.cpp")
# The transformed script source is the frame entry the resident runtime drives;
# the hosted-script test links the very same generated file.
set(AVZ_SCRIPT_OVERLAY "${CMAKE_CURRENT_BINARY_DIR}/avz_script_overlay.cpp")
file(READ "${AVZ_SCRIPT}" script_source)
file(READ "${AVZ_HOOK}" hook_source)
file(READ "${AVZ_CARD}" card_source)
# Git checkouts can use LF or CRLF. Review the same source bytes after only
# normalizing line endings; do not disable source identity checks for CI.
foreach(source_name script hook card)
  string(REPLACE "\r\n" "\n" ${source_name}_source "${${source_name}_source}")
  string(SHA256 ${source_name}_hash "${${source_name}_source}")
endforeach()
if(NOT script_hash STREQUAL "9457a13e056aa785d8efb5de8616a19c04107b75d60c9607eea9b778d8134d7b" OR
   NOT hook_hash STREQUAL "2376e147e671b6ef8dbbae33811b654f92e29c7f18c4d9989d394cd4c5537134" OR
   NOT card_hash STREQUAL "d377619432d1c1a5bd060d851a3be4f46bc0768094bb2a401fbfcc67a84ff1d0")
  message(FATAL_ERROR "Pinned AvZ hook/script source changed; review runtime boundary overlay before rebuilding")
endif()
string(REPLACE "static ALogger<AMsgBox> logger;" "static lvz::runtime::DiagnosticLogger logger;" script_source "${script_source}")
string(REPLACE "AMsgBox::Show(std::string(\"catch std exception: \") + exce.what() + stopWorkingStr);"
  "aLogger->Error(\"{}\", std::string(\"catch std exception: \") + exce.what() + stopWorkingStr);" script_source "${script_source}")
string(REPLACE "AMsgBox::Show(std::string(\"The script triggered an unknown exception. \") + stopWorkingStr);"
  "aLogger->Error(\"{}\", std::string(\"The script triggered an unknown exception. \") + stopWorkingStr);" script_source "${script_source}")
string(REPLACE "    __APublicAfterScriptHook::RunAll();\n\n    RunTotal();"
  "    __APublicAfterScriptHook::RunAll();\n\n    if (lvz::runtime::Started()) { RunScript(); return; } // Resident runtime must not enter AvZ's recursive lifetime loop.\n    RunTotal();"
  script_source "${script_source}")
string(REPLACE "void __AScriptManager::ScriptHook() {\n    RunTotal();"
  "void __AScriptManager::ScriptHook() {\n    if (!lvz::runtime::BeforeFrame()) return;\n    RunTotal();\n    if (!lvz::runtime::AfterAvzRunTotal()) return;"
  script_source "${script_source}")
set(controlled_call_needle "    AAsm::GameTotalLoop();\n    while (__aGameControllor.isSkipTick()")
string(FIND "${script_source}" "${controlled_call_needle}" controlled_call_position)
if(controlled_call_position LESS 0)
  message(FATAL_ERROR "Reviewed original update call site is missing")
endif()
string(LENGTH "${controlled_call_needle}" controlled_call_length)
math(EXPR controlled_call_end "${controlled_call_position}+${controlled_call_length}")
string(SUBSTRING "${script_source}" ${controlled_call_end} -1 controlled_call_tail)
string(FIND "${controlled_call_tail}" "${controlled_call_needle}" duplicate_controlled_call)
if(NOT duplicate_controlled_call EQUAL -1)
  message(FATAL_ERROR "Reviewed original update call site is not unique")
endif()
string(REPLACE "${controlled_call_needle}"
  "    if (!lvz::runtime::RunOneEngineFrame()) return;\n    if (lvz::runtime::Started()) return; // The controller owns all frame budgets.\n    while (__aGameControllor.isSkipTick()"
  script_source "${script_source}")
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/avz_script_overlay.cpp" "#include \"runtime/runtime.hpp\"\n#include \"runtime/diagnostics.hpp\"\n${script_source}")
string(REPLACE "        __aig.hInstance = hinstDLL;"
  "        if (!lvz::determinism::ValidateTargetImage()) return FALSE;\n        __aig.hInstance = hinstDLL;"
  hook_source "${hook_source}")
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/avz_hook_overlay.cpp" "#include \"determinism/audit.hpp\"\n${hook_source}")
string(REPLACE "    AWaitForFight(selectInterval == 0);" "    if (!lvz::runtime::Started()) AWaitForFight(selectInterval == 0);" card_source "${card_source}")
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/avz_card_overlay.cpp" "#include \"runtime/runtime.hpp\"\n${card_source}")
list(REMOVE_ITEM AVZ_SOURCES "${AVZ_SCRIPT}" "${AVZ_HOOK}" "${AVZ_CARD}")
list(APPEND AVZ_SOURCES "${CMAKE_CURRENT_BINARY_DIR}/avz_script_overlay.cpp" "${CMAKE_CURRENT_BINARY_DIR}/avz_hook_overlay.cpp" "${CMAKE_CURRENT_BINARY_DIR}/avz_card_overlay.cpp")
set(AVZ_SMART "${CMAKE_SOURCE_DIR}/avz/framework/src/avz_smart.cpp")
file(READ "${AVZ_SMART}" smart_source)
string(REPLACE "\r\n" "\n" smart_source "${smart_source}")
string(SHA256 smart_hash "${smart_source}")
if(NOT smart_hash STREQUAL "d863a802196acc25e026c8bbcf2aa78c97d1d0686fb1ee03a977a233c72bc0fe")
  message(FATAL_ERROR "Pinned AvZ collection source changed; review environment assistance before rebuilding")
endif()
string(REPLACE "        ALeftClick(x, y);"
  "        lvz::runtime::RecordEnvironmentCollect(collectItem->Id(), collectItem->Type(), x, y);\n        ALeftClick(x, y);" smart_source "${smart_source}")
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/avz_smart_overlay.cpp" "#include \"runtime/runtime.hpp\"\n${smart_source}")
list(REMOVE_ITEM AVZ_SOURCES "${AVZ_SMART}")
list(APPEND AVZ_SOURCES "${CMAKE_CURRENT_BINARY_DIR}/avz_smart_overlay.cpp")

# Hosted cannon shots (aCobManager.Fire) are direct engine calls: they reach PvZ
# through AAsm::Fire instead of the runtime's request journal, so plant/shovel/
# spawn audit nothing about them (issue #84, open item 2). This overlay adds one
# call to the pinned upstream _BasicFire, immediately after the reviewed
# AAsm::Fire line, and touches nothing else. The insertion is compiled only
# when LVZ_AVZ_HOSTED_FIRE_AUDIT is defined, and the file itself is only used by
# a hosted build; tests/avz_hosted_fire_tests.cpp builds it both ways.
set(AVZ_COB_MANAGER "${CMAKE_SOURCE_DIR}/avz/framework/src/avz_cob_manager.cpp")
if(NOT AVZ_COB_MANAGER IN_LIST AVZ_SOURCES)
  message(FATAL_ERROR "AvZ cob manager source is missing from AVZ_SOURCES")
endif()
set(AVZ_COB_MANAGER_OVERLAY "${CMAKE_CURRENT_BINARY_DIR}/avz_cob_manager_overlay.cpp")
file(READ "${AVZ_COB_MANAGER}" cob_source)
string(REPLACE "\r\n" "\n" cob_normalized "${cob_source}")
string(SHA256 cob_hash "${cob_normalized}")
if(NOT cob_hash STREQUAL "f985ac3366d815a3cdd9268a09e8a6d7134f396c62ab68cd130f440ee8185a21")
  message(FATAL_ERROR "Pinned AvZ cob manager source changed; review the hosted-fire audit overlay before rebuilding")
endif()
set(cob_fire_call "    AAsm::Fire(x, y, cobIdx);")
string(FIND "${cob_source}" "${cob_fire_call}" cob_fire_position)
if(cob_fire_position LESS 0)
  message(FATAL_ERROR "Reviewed cannon call site is missing")
endif()
string(LENGTH "${cob_fire_call}" cob_fire_length)
math(EXPR cob_fire_end "${cob_fire_position}+${cob_fire_length}")
string(SUBSTRING "${cob_source}" ${cob_fire_end} -1 cob_fire_tail)
string(FIND "${cob_fire_tail}" "${cob_fire_call}" duplicate_cob_fire_call)
if(NOT duplicate_cob_fire_call EQUAL -1)
  message(FATAL_ERROR "Reviewed cannon call site is not unique")
endif()
string(REPLACE "${cob_fire_call}"
  "${cob_fire_call}\n#ifdef LVZ_AVZ_HOSTED_FIRE_AUDIT\n    lvz::runtime::RecordHostedFire(cobIdx, plant->Id(), plant->Row() + 1, plant->Col() + 1, dropRow, dropCol);\n#endif"
  cob_source "${cob_source}")
file(WRITE "${AVZ_COB_MANAGER_OVERLAY}"
  "#ifdef LVZ_AVZ_HOSTED_FIRE_AUDIT\n#include \"runtime/hosted_fire.hpp\"\n#endif\n${cob_source}")
# The two lists a test can pick from: the pristine upstream file (what the
# default build compiles) and the same list with the audit overlay swapped in
# (what a hosted build compiles).
set(AVZ_SOURCES_COB_PRISTINE "${AVZ_SOURCES}")
set(AVZ_SOURCES_COB_OVERLAY "${AVZ_SOURCES}")
list(REMOVE_ITEM AVZ_SOURCES_COB_OVERLAY "${AVZ_COB_MANAGER}")
list(APPEND AVZ_SOURCES_COB_OVERLAY "${AVZ_COB_MANAGER_OVERLAY}")
if(LVZ_AVZ_HOSTED_SCRIPT)
  # Declared before this include by CMakeLists.txt: the recorder's source list is
  # fixed by add_library() below, so the overlay decision happens here.
  set(AVZ_SOURCES "${AVZ_SOURCES_COB_OVERLAY}")
endif()
