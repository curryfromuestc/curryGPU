#pragma once

#include <array>
#include <cstdint>
#include <optional>
#include <string>

namespace currygpu::iss {

constexpr int kCtaBarrierLanes = 32;
constexpr int kCtaBarrierSlots = 16;
constexpr int kCtaBarrierMaxWarps = 32;

struct cta_barrier_action {
    bool valid = false;
    int warp_id = 0;
    int bar_id = 0;
    int expected_count = 0;
    bool explicit_count = false;
    bool blocking = true;
    int resume_pc = 0;
    int pc = 0;
    std::array<bool, kCtaBarrierLanes> lanes{};
};

struct cta_barrier_release {
    bool fired = false;
    int bar_id = 0;
    std::array<std::uint32_t, kCtaBarrierMaxWarps> waiting_masks{};
    std::array<int, kCtaBarrierMaxWarps> resume_pc{};
};

struct cta_barrier_error {
    bool failed = false;
    std::string reason;
    int pc = 0;
    int bar_id = 0;
    int arrived = 0;
    int expected_count = 0;
    int new_expected_count = 0;
};

struct cta_barrier_apply_result {
    cta_barrier_error error;
    cta_barrier_release release;
};

struct cta_barrier_snapshot_slot {
    bool active = false;
    int arrived_count = 0;
    int expected_count = 0;
    int phase_parity = 0;
};

class cta_barrier_state {
public:
    cta_barrier_state(int num_warps = 1, int max_threads = kCtaBarrierLanes);

    cta_barrier_apply_result apply(const cta_barrier_action& action, int non_exited_thread_count);
    std::optional<cta_barrier_release> try_fire(int bar_id, int non_exited_thread_count);
    std::array<cta_barrier_snapshot_slot, kCtaBarrierSlots> snapshot(int non_exited_thread_count) const;

    bool any_active() const;
    int first_active_bar_id() const;
    int arrived_count(int bar_id) const;
    int expected_count(int bar_id, int non_exited_thread_count) const;

private:
    struct slot {
        bool active = false;
        int expected_count = 0;
        bool explicit_count = false;
        std::array<std::uint32_t, kCtaBarrierMaxWarps> arrived_masks{};
        std::array<std::uint32_t, kCtaBarrierMaxWarps> waiting_masks{};
        std::array<int, kCtaBarrierMaxWarps> resume_pc{};
        int phase_parity = 0;
    };

    int arrived_count(const slot& value) const;
    int effective_expected_count(const slot& value, int non_exited_thread_count) const;
    bool is_valid_expected_count(int expected_count) const;

    int num_warps_ = 1;
    int max_threads_ = kCtaBarrierLanes;
    std::array<slot, kCtaBarrierSlots> slots_{};
};

}  // namespace currygpu::iss
