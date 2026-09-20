#include <iostream>
#include <vector>

int main() {
    // Allocate a huge array (e.g., 256MB) to exceed LLC capacity
    size_t size = 256 * 1024 * 1024 / sizeof(int); 
    std::vector<int> big_array(size, 0);

    // Stride through the array with a step of 16 (64 bytes / 4 bytes per int)
    // This ensures every access hits a new cache line, maximizing bandwidth usage
    long long dummy = 0;
    for (int iter = 0; iter < 1000; ++iter) {
        for (size_t i = 0; i < size; i += 16) {
            big_array[i] += 1;
            dummy += big_array[i];
        }
    }
    return dummy;
}