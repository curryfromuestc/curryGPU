#include "memory_space.h"

#include <algorithm>

namespace currygpu::iss {

std::uint64_t memory_space_impl::block_id(std::uint64_t address) {
    return address / block_size;
}

std::size_t memory_space_impl::block_offset(std::uint64_t address) {
    return static_cast<std::size_t>(address % block_size);
}

std::uint8_t memory_space_impl::read_byte(std::uint64_t address) const {
    const auto iter = blocks_.find(block_id(address));
    if (iter == blocks_.end()) {
        return 0;
    }
    return iter->second[block_offset(address)];
}

void memory_space_impl::write_byte(std::uint64_t address, std::uint8_t value) {
    auto& block = blocks_[block_id(address)];
    block[block_offset(address)] = value;
}

std::vector<std::uint8_t> memory_space_impl::read_bytes(std::uint64_t address, std::size_t width) const {
    std::vector<std::uint8_t> bytes;
    bytes.reserve(width);
    for (std::size_t index = 0; index < width; ++index) {
        bytes.push_back(read_byte(address + index));
    }
    return bytes;
}

void memory_space_impl::write_bytes(std::uint64_t address, const std::vector<std::uint8_t>& bytes) {
    for (std::size_t index = 0; index < bytes.size(); ++index) {
        write_byte(address + index, bytes[index]);
    }
}

std::map<std::uint64_t, std::vector<std::uint8_t>> memory_space_impl::snapshot_blocks() const {
    std::map<std::uint64_t, std::vector<std::uint8_t>> snapshot;
    for (const auto& [id, block] : blocks_) {
        const bool nonzero = std::any_of(block.begin(), block.end(), [](std::uint8_t value) {
            return value != 0;
        });
        if (!nonzero) {
            continue;
        }
        snapshot.emplace(id, std::vector<std::uint8_t>(block.begin(), block.end()));
    }
    return snapshot;
}

}  // namespace currygpu::iss
