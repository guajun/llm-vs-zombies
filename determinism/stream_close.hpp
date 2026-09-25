#pragma once
#include <fstream>
#include <string>

namespace lvz::determinism {
// Close an optional output stream and report its failure. `is_open()` becomes
// false after close() even when close/finalization set failbit, so the caller
// must capture whether the stream was open before closing and check fail() in
// the same branch; this helper does exactly that.
inline std::string CloseOptionalStream(std::ofstream& stream, const char* name) {
    const bool wasOpen = stream.is_open();
    if (!wasOpen) return {};
    stream.close();
    if (stream.fail()) return std::string(name) + " close failed";
    return {};
}
}
