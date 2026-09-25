#pragma once
#include <windows.h>
#include <bcrypt.h>
#include <array>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <string>
#include <vector>

// Incremental SHA-256 for native evidence integrity. The recorder already
// links bcrypt; the Python reader recomputes the same digest with hashlib
// over the exact file bytes, so a receipt cannot outlive a changed file.
namespace lvz::determinism {
class Sha256 {
public:
    Sha256() {
        if (BCryptOpenAlgorithmProvider(&algorithm_, BCRYPT_SHA256_ALGORITHM, nullptr, 0) < 0)
            throw std::runtime_error("SHA-256 algorithm unavailable");
        if (BCryptCreateHash(algorithm_, &hash_, nullptr, 0, nullptr, 0, 0) < 0) {
            BCryptCloseAlgorithmProvider(algorithm_, 0);
            algorithm_ = nullptr;
            throw std::runtime_error("SHA-256 state unavailable");
        }
    }
    ~Sha256() {
        if (hash_) BCryptDestroyHash(hash_);
        if (algorithm_) BCryptCloseAlgorithmProvider(algorithm_, 0);
    }
    Sha256(const Sha256&) = delete;
    Sha256& operator=(const Sha256&) = delete;

    void Update(const void* bytes, size_t length) {
        if (!length) return;
        if (BCryptHashData(hash_, static_cast<PUCHAR>(const_cast<void*>(bytes)),
                           static_cast<ULONG>(length), 0) < 0)
            throw std::runtime_error("SHA-256 update failed");
    }
    std::string HexDigest() {
        std::array<uint8_t, 32> digest{};
        if (BCryptFinishHash(hash_, digest.data(), digest.size(), 0) < 0)
            throw std::runtime_error("SHA-256 finalize failed");
        static constexpr char hex[] = "0123456789abcdef";
        std::string result;
        result.reserve(64);
        for (auto byte : digest) {
            result.push_back(hex[byte >> 4]);
            result.push_back(hex[byte & 15]);
        }
        return result;
    }

private:
    BCRYPT_ALG_HANDLE algorithm_ = nullptr;
    BCRYPT_HASH_HANDLE hash_ = nullptr;
};

inline std::string FileSha256(const std::filesystem::path& path) {
    std::ifstream stream(path, std::ios::in | std::ios::binary);
    if (!stream) throw std::runtime_error("Evidence file is unreadable: " + path.string());
    Sha256 hasher;
    std::vector<char> buffer(1 << 16);
    while (stream) {
        stream.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
        const auto got = stream.gcount();
        if (got > 0) hasher.Update(buffer.data(), static_cast<size_t>(got));
    }
    if (!stream.eof()) throw std::runtime_error("Evidence file read failed: " + path.string());
    return hasher.HexDigest();
}
}
