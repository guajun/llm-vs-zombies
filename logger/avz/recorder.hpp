#pragma once
#include <avz.h>

namespace lvz {
// Route all future agent actions through these wrappers to retain exact action order.
APlant* Plant(APlantType type, int row, float col);
bool Shovel(int row, float col, int targetType = -1);
// Spawn one zombie with the original AvZ primitive AAsm::PutZombie.
// row/col are the protocol's 1-based lawn coordinates; the engine primitive
// takes 0-based grid indices. Returns null instead of running a spawn the
// zombie pool cannot hold, because the engine dereferences the null result.
AZombie* SpawnZombie(AZombieType type, int row, int col);
void Note(const std::string& text);
void RecordRuntime(const std::string& kind, const std::string& jsonPayload);
void CloseRecording();
}
