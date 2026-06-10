#include "cta_barrier.h"

#include <algorithm>
#include <bit>

namespace currygpu::iss {

cta_barrier_state::cta_barrier_state(int num_warps, int max_threads)
    : num_warps_(num_warps), max_threads_(max_threads) {}

cta_barrier_apply_result cta_barrier_state::apply(const cta_barrier_action& action, int non_exited_thread_count) {
    cta_barrier_apply_result result;
    if (!action.valid) {
        return result;
    }
    if (action.bar_id < 0 || action.bar_id >= kCtaBarrierSlots || !is_valid_expected_count(action.expected_count)) {
        result.error.failed = true;
        result.error.reason = "barrier_count_not_warp_multiple";
        result.error.pc = action.pc;
        result.error.bar_id = action.bar_id;
        result.error.expected_count = action.expected_count;
        return result;
    }

    slot& current = slots_[action.bar_id];
    if (!current.active) {
        current.active = true;
        current.expected_count = action.expected_count;
        current.explicit_count = action.explicit_count;
        current.arrived_masks.fill(0);
        current.waiting_masks.fill(0);
        current.resume_pc.fill(0);
    } else if (current.expected_count != action.expected_count || current.explicit_count != action.explicit_count) {
        result.error.failed = true;
        result.error.reason = "barrier_count_not_warp_multiple";
        result.error.pc = action.pc;
        result.error.bar_id = action.bar_id;
        result.error.arrived = arrived_count(current);
        result.error.expected_count = current.expected_count;
        result.error.new_expected_count = action.expected_count;
        return result;
    }

    const int warp_id = action.warp_id;
    if (warp_id < 0 || warp_id >= num_warps_) {
        return result;
    }
    for (int lane = 0; lane < kCtaBarrierLanes; ++lane) {
        if (action.lanes[lane]) {
            current.arrived_masks[warp_id] |= std::uint32_t{1} << lane;
            if (action.blocking) {
                current.waiting_masks[warp_id] |= std::uint32_t{1} << lane;
                current.resume_pc[warp_id] = action.resume_pc;
            }
        }
    }
    if (auto release = try_fire(action.bar_id, non_exited_thread_count)) {
        result.release = *release;
    }
    return result;
}

std::optional<cta_barrier_release> cta_barrier_state::try_fire(int bar_id, int non_exited_thread_count) {
    if (bar_id < 0 || bar_id >= kCtaBarrierSlots) {
        return std::nullopt;
    }
    slot& current = slots_[bar_id];
    if (!current.active || arrived_count(current) < effective_expected_count(current, non_exited_thread_count)) {
        return std::nullopt;
    }
    cta_barrier_release release;
    release.fired = true;
    release.bar_id = bar_id;
    release.waiting_masks = current.waiting_masks;
    release.resume_pc = current.resume_pc;
    current.active = false;
    current.expected_count = 0;
    current.explicit_count = false;
    current.arrived_masks.fill(0);
    current.waiting_masks.fill(0);
    current.resume_pc.fill(0);
    current.phase_parity ^= 1;
    return release;
}

std::array<cta_barrier_snapshot_slot, kCtaBarrierSlots> cta_barrier_state::snapshot(int non_exited_thread_count) const {
    std::array<cta_barrier_snapshot_slot, kCtaBarrierSlots> out{};
    for (int index = 0; index < kCtaBarrierSlots; ++index) {
        const slot& current = slots_[index];
        out[index].active = current.active;
        out[index].arrived_count = arrived_count(current);
        out[index].expected_count = current.active ? effective_expected_count(current, non_exited_thread_count) : 0;
        out[index].phase_parity = current.phase_parity;
    }
    return out;
}

bool cta_barrier_state::any_active() const {
    return std::any_of(slots_.begin(), slots_.end(), [](const slot& current) {
        return current.active;
    });
}

int cta_barrier_state::first_active_bar_id() const {
    for (int index = 0; index < kCtaBarrierSlots; ++index) {
        if (slots_[index].active) {
            return index;
        }
    }
    return 0;
}

int cta_barrier_state::arrived_count(int bar_id) const {
    if (bar_id < 0 || bar_id >= kCtaBarrierSlots) {
        return 0;
    }
    return arrived_count(slots_[bar_id]);
}

int cta_barrier_state::expected_count(int bar_id, int non_exited_thread_count) const {
    if (bar_id < 0 || bar_id >= kCtaBarrierSlots) {
        return 0;
    }
    return effective_expected_count(slots_[bar_id], non_exited_thread_count);
}

int cta_barrier_state::arrived_count(const slot& value) const {
    int count = 0;
    for (int warp = 0; warp < num_warps_; ++warp) {
        count += std::popcount(value.arrived_masks[warp]);
    }
    return count;
}

int cta_barrier_state::effective_expected_count(const slot& value, int non_exited_thread_count) const {
    if (value.explicit_count) {
        return value.expected_count;
    }
    return std::min(value.expected_count, non_exited_thread_count);
}

bool cta_barrier_state::is_valid_expected_count(int expected_count) const {
    return expected_count > 0 && expected_count % kCtaBarrierLanes == 0 && expected_count <= max_threads_;
}

}  // namespace currygpu::iss
