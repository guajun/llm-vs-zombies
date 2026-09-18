#pragma once
#include <avz.h>

namespace lvz {
// Route all future agent actions through these wrappers to retain exact action order.
APlant* Plant(APlantType type, int row, float col);
bool Shovel(int row, float col, int targetType = -1);
void Note(const std::string& text);
}
