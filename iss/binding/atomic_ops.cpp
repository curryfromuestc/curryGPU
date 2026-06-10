#include "atomic_ops.h"

#include <algorithm>
#include <stdexcept>

namespace currygpu::iss {

bool is_supported_atomic_op(const std::string& op) {
    return op == "ADD" || op == "MIN" || op == "MAX" || op == "INC" || op == "DEC" ||
           op == "AND" || op == "OR" || op == "XOR" || op == "EXCH" || op == "CAS";
}

atomic_rmw_result apply_atomic_rmw(const std::string& op, std::uint32_t old_value, std::uint32_t src, std::uint32_t cmp) {
    if (op == "ADD") {
        return {old_value, old_value + src};
    }
    if (op == "MIN") {
        return {old_value, std::min(old_value, src)};
    }
    if (op == "MAX") {
        return {old_value, std::max(old_value, src)};
    }
    if (op == "INC") {
        return {old_value, old_value >= src ? 0u : old_value + 1u};
    }
    if (op == "DEC") {
        return {old_value, (old_value == 0u || old_value > src) ? src : old_value - 1u};
    }
    if (op == "AND") {
        return {old_value, old_value & src};
    }
    if (op == "OR") {
        return {old_value, old_value | src};
    }
    if (op == "XOR") {
        return {old_value, old_value ^ src};
    }
    if (op == "EXCH") {
        return {old_value, src};
    }
    if (op == "CAS") {
        return {old_value, old_value == cmp ? src : old_value};
    }
    throw std::invalid_argument("unsupported atomic operation");
}

}  // namespace currygpu::iss
