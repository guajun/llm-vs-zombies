#pragma once
#include "controller.hpp"
#include <filesystem>
namespace lvz::runtime {
void Start(const std::filesystem::path& runDir);
void Shutdown();
bool Started();
// Called by a generated overlay of the pinned AvZ ScriptHook, on its game thread.
bool BeforeFrame();
bool RunOneEngineFrame();
void RecordEnvironmentCollect(uint32_t itemId,int type,int x,int y);
}
