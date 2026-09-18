# The submodule remains pristine. Fail closed if the reviewed upstream sources change.
set(AVZ_SCRIPT "${CMAKE_SOURCE_DIR}/avz/framework/src/avz_script.cpp")
set(AVZ_HOOK "${CMAKE_SOURCE_DIR}/avz/framework/src/avz_hook.cpp")
set(AVZ_CARD "${CMAKE_SOURCE_DIR}/avz/framework/src/avz_card.cpp")
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
  "void __AScriptManager::ScriptHook() {\n    if (!lvz::runtime::BeforeFrame()) return;\n    RunTotal();"
  script_source "${script_source}")
string(REPLACE "    AAsm::GameTotalLoop();\n    while (__aGameControllor.isSkipTick()"
  "    if (!lvz::runtime::BeforeEngineFrame()) return;\n    AAsm::GameTotalLoop();\n    lvz::runtime::AfterEngineFrame();\n    if (lvz::runtime::Started()) return; // The controller owns all frame budgets.\n    while (__aGameControllor.isSkipTick()"
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
