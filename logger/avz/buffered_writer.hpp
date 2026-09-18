#pragma once
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <string>
#include <string_view>

namespace lvz {
inline std::string Quote(std::string_view value) {
    std::string out = "\"";
    constexpr char hex[] = "0123456789abcdef";
    for (unsigned char c : value) {
        if (c == '"' || c == '\\') { out += '\\'; out += char(c); }
        else if (c < 32) { out += "\\u00"; out += hex[c >> 4]; out += hex[c & 15]; }
        else out += char(c);
    }
    return out + '"';
}

// Game-thread writer: bounded batch writes, no per-entity flush, no worker touching game memory.
class BufferedWriter {
    std::ofstream stream;
    std::string pending;
public:
    void Open(const std::filesystem::path& file) {
        if (std::filesystem::exists(file)) throw std::runtime_error("events.jsonl already exists; create a new run");
        stream.open(file, std::ios::binary | std::ios::out);
        if (!stream) throw std::runtime_error("Cannot open events.jsonl");
        pending.reserve(131072);
    }
    void Append(const std::string& line) {
        if (!stream.is_open()) throw std::runtime_error("Recorder is not open");
        pending += line;
        pending += '\n';
        if (pending.size() >= 65536) Flush();
    }
    void Flush() {
        if (!stream.is_open()) return;
        if (!pending.empty()) stream.write(pending.data(), static_cast<std::streamsize>(pending.size()));
        stream.flush();
        if (!stream) throw std::runtime_error("Telemetry write failed; recording is incomplete");
        pending.clear();
    }
    void Close() { Flush(); stream.close(); }
};
}
