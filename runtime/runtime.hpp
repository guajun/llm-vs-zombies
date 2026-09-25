#pragma once
#include "controller.hpp"
#include <filesystem>
namespace lvz::runtime {
void Start(const std::filesystem::path& runDir);
void Shutdown();
// Every runtime instance owns exactly one branch scope (LVZ_BRANCH_ID when the
// orchestrator set it, otherwise a process-instance label derived from the run
// directory). The audit-side lifecycle evidence binds the same value the
// journal uses instead of inventing a second identity.
std::string InstanceBranchScope(const std::filesystem::path& runDir);
bool Started();
// Called by a generated overlay of the pinned AvZ ScriptHook, on its game thread.
bool BeforeFrame();
bool AfterAvzRunTotal();
bool RunOneEngineFrame();
void RecordEnvironmentCollect(uint32_t itemId,int type,int x,int y);
// Boundary version the runtime is at, or a null value when it is not started.
// Used by the audit-only hosted-fire hook (runtime/hosted_fire.cpp) to bind a
// cannon shot to the frame it happened under; see docs/avz-script-hosting.md
// section 8.
nlohmann::json CurrentVersion();
// Marks the run as audited-failed without touching the caller: the audit-only
// hosted-fire hook reports its own faults here instead of letting an exception
// reach the script that fired.
void ReportAuditFault(const std::string& message);
}
