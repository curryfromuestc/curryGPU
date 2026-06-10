#include "memory_space.h"

#include <cstdint>
#include <iostream>
#include <vector>

int main() {
    currygpu::iss::memory_space_impl memory;
    if (memory.read_byte(123) != 0) {
        std::cerr << "uninitialized read was not zero\n";
        return 1;
    }
    if (!memory.snapshot_blocks().empty()) {
        std::cerr << "read allocated a block\n";
        return 1;
    }

    memory.write_byte(17, 0xAA);
    auto first = memory.snapshot_blocks();
    if (first.size() != 1 || first.at(0).size() != currygpu::iss::memory_space_impl::block_size || first.at(0)[17] != 0xAA) {
        std::cerr << "single-byte write did not serialize as expected\n";
        return 1;
    }

    memory.write_byte(17, 0);
    if (!memory.snapshot_blocks().empty()) {
        std::cerr << "all-zero block was serialized\n";
        return 1;
    }

    memory.write_bytes(currygpu::iss::memory_space_impl::block_size + 2, std::vector<std::uint8_t>{1, 2, 3});
    auto second = memory.snapshot_blocks();
    if (second.size() != 1 || second.at(1)[2] != 1 || second.at(1)[4] != 3) {
        std::cerr << "multi-byte write crossed the wrong block or offset\n";
        return 1;
    }
    if (memory.read_bytes(currygpu::iss::memory_space_impl::block_size + 2, 3) != std::vector<std::uint8_t>({1, 2, 3})) {
        std::cerr << "multi-byte read did not preserve little byte order\n";
        return 1;
    }

    return 0;
}
