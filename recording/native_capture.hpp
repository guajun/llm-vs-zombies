#pragma once
#include <cstddef>
#include <cstdint>
#include <span>
#include <string>
#include <vector>

namespace lvz::recording {
struct PixelLayout {
    uint32_t width=0, height=0, bitsPerPixel=0;
    int32_t pitch=0;
    uint32_t redMask=0, greenMask=0, blueMask=0;
};
// `memory` starts at the lowest address of the locked pixel region. For negative
// pitch the logical first row is therefore the last stored row. Output is BGR24.
std::vector<uint8_t> ConvertPixels(std::span<const uint8_t> memory,const PixelLayout& layout);

struct CaptureResult {
    bool ok=false;
    std::string error;
    std::string method="engine_widget_draw_to_directdraw_surface";
    std::string origin="top_left";
    uint32_t width=0, height=0, rowStride=0;
    std::vector<uint8_t> pixels;
    bool forcedRender=false;
    bool used3D=false;
    bool knownRngUnchanged=false;
    int gameClockBefore=0, gameClockAfter=0;
};

// Checks this exact 1.0.0.1051 image and the functions/field-access instructions
// used here. This is not permission to capture from an arbitrary thread.
bool ValidateCaptureTarget() noexcept;
// Internal render stage only: this changes original engine rendering state and
// may consume RNG. Never call this API from capture_frame. The draw gate must
// already be installed, and the caller retains all resulting state in audit.
// Pass the previously established game thread ID, not a new worker's ID.
// No HWND operations, foreground changes, desktop capture or input injection.
CaptureResult CaptureOriginalFrame(uint32_t gameThreadId);
}
