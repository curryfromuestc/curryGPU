#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <map>
#include <unordered_map>
#include <vector>

namespace currygpu::iss {

class memory_space {
public:
    virtual ~memory_space() = default;
    virtual std::uint8_t read_byte(std::uint64_t address) const = 0;
    virtual void write_byte(std::uint64_t address, std::uint8_t value) = 0;
};

class memory_space_impl final : public memory_space {
public:
    static constexpr std::uint64_t block_size = 4096;
    using block_type = std::array<std::uint8_t, block_size>;

    std::uint8_t read_byte(std::uint64_t address) const override;
    void write_byte(std::uint64_t address, std::uint8_t value) override;

    std::vector<std::uint8_t> read_bytes(std::uint64_t address, std::size_t width) const;
    void write_bytes(std::uint64_t address, const std::vector<std::uint8_t>& bytes);

    std::map<std::uint64_t, std::vector<std::uint8_t>> snapshot_blocks() const;

private:
    static std::uint64_t block_id(std::uint64_t address);
    static std::size_t block_offset(std::uint64_t address);

    std::unordered_map<std::uint64_t, block_type> blocks_;
};

}  // namespace currygpu::iss
