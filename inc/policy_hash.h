#ifndef POLICY_HASH_H
#define POLICY_HASH_H
#include <cstdint>
#include <cstddef>
// FNV-1a of the stored parameter bytes. A diagnostic fingerprint, not a save file.
inline uint64_t policy_hash_bytes(uint64_t hash, const void* memory, size_t size) {
    const unsigned char* bytes = static_cast<const unsigned char*>(memory);
    for (size_t i = 0; i < size; ++i) { hash ^= bytes[i]; hash *= 1099511628211ULL; }
    return hash;
}
#endif
