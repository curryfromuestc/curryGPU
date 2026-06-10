#pragma once

#include <cstdint>
#include <string>

namespace currygpu::iss {

struct atomic_rmw_result {
    std::uint32_t old_value = 0;
    std::uint32_t new_value = 0;
};

bool is_supported_atomic_op(const std::string& op);
atomic_rmw_result apply_atomic_rmw(const std::string& op, std::uint32_t old_value, std::uint32_t src, std::uint32_t cmp);

}  // namespace currygpu::iss
