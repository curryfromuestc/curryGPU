#pragma once

class NativeBlock {
public:
    NativeBlock(const py::object& program,
                int num_gprs,
                SchedOrder sched_order,
                bool debug_checks,
                LaunchConfig config,
                currygpu::iss::memory_space_impl* global_memory = nullptr)
        : config_(std::move(config)),
          warp_sched_order_(parse_warp_sched_order(config_.warp_sched_order)),
          global_memory_external_(global_memory),
          cta_barriers_(config_.num_warps, dim_product(config_.ntid)) {
        for (int warp = 0; warp < config_.num_warps; ++warp) {
            warps_.emplace_back(program, num_gprs, sched_order, debug_checks, config_, warp, &this->global_memory(), &shared_memory_, race_shadow_ptr());
        }
    }

    NativeBlock(std::vector<Instruction> program,
                int num_gprs,
                SchedOrder sched_order,
                bool debug_checks,
                LaunchConfig config,
                currygpu::iss::memory_space_impl* global_memory = nullptr)
        : config_(std::move(config)),
          warp_sched_order_(parse_warp_sched_order(config_.warp_sched_order)),
          global_memory_external_(global_memory),
          cta_barriers_(config_.num_warps, dim_product(config_.ntid)) {
        for (int warp = 0; warp < config_.num_warps; ++warp) {
            warps_.emplace_back(program, num_gprs, sched_order, debug_checks, config_, warp, &this->global_memory(), &shared_memory_, race_shadow_ptr());
        }
    }

    py::dict step(int max_steps) {
        ++g_boundary_calls;
        return step_no_boundary(max_steps);
    }

    py::dict step_no_boundary(int max_steps) {
        if (max_steps < 0) {
            throw std::invalid_argument("max_steps must be non-negative");
        }
        int issued = 0;
        while (!done()) {
            if (issued >= max_steps) {
                set_block_trap("max_steps", "budget_exhausted", 0, py::dict());
                break;
            }
            int warp = select_warp();
            if (warp < 0) {
                detect_cta_barrier_deadlock();
                if (block_trap_.kind != "none") {
                    break;
                }
                detect_warp_deadlocks();
                break;
            }
            warps_[warp].step_one_group();
            apply_cta_barrier_action(warps_[warp]);
            if (warps_[warp].trap().kind != "none") {
                block_trap_ = warps_[warp].trap();
                break;
            }
            ++issued;
        }
        detect_cta_barrier_deadlock();
        return snapshot();
    }

    py::dict snapshot() const {
        if (config_.num_warps == 1) {
            py::dict out = warps_[0].snapshot();
            out["cta_barriers"] = cta_barrier_snapshot();
            if (block_trap_.kind != "none") {
                py::dict trap;
                trap["kind"] = block_trap_.kind;
                trap["reason"] = block_trap_.reason;
                trap["pc"] = block_trap_.pc < 0 ? py::object(py::none()) : py::object(py::int_(block_trap_.pc));
                trap["detail"] = block_trap_.detail;
                out["trap"] = trap;
            }
            return out;
        }
        py::dict out;
        py::list warps;
        Counters total;
        py::dict local_memory;
        for (std::size_t index = 0; index < warps_.size(); ++index) {
            py::dict warp_snapshot = warps_[index].snapshot();
            py::dict counters = warp_snapshot["counters"].cast<py::dict>();
            total.instructions += counters["instructions"].cast<std::uint64_t>();
            total.warp_instructions += counters["warp_instructions"].cast<std::uint64_t>();
            total.mem_ops += counters["mem_ops"].cast<std::uint64_t>();
            total.divergence_events += counters["divergence_events"].cast<std::uint64_t>();
            py::dict memory = warp_snapshot["memory"].cast<py::dict>();
            py::dict warp_local = memory["local"].cast<py::dict>();
            for (auto item : warp_local) {
                const int local_lane = std::stoi(py::str(item.first).cast<std::string>());
                const int linear_lane = static_cast<int>(index) * kLanes + local_lane;
                local_memory[py::str(std::to_string(linear_lane))] = py::reinterpret_borrow<py::object>(item.second);
            }
            PyDict_DelItemString(warp_snapshot.ptr(), "memory");
            PyDict_DelItemString(warp_snapshot.ptr(), "trap");
            PyDict_DelItemString(warp_snapshot.ptr(), "counters");
            PyDict_DelItemString(warp_snapshot.ptr(), "cta_barriers");
            warps.append(warp_snapshot);
        }
        out["warps"] = warps;
        py::dict memory;
        memory["global"] = serialize_memory_space(global_memory());
        memory["shared"] = serialize_memory_space(shared_memory_);
        memory["local"] = local_memory;
        out["memory"] = memory;
        py::dict counters;
        counters["instructions"] = total.instructions;
        counters["warp_instructions"] = total.warp_instructions;
        counters["mem_ops"] = total.mem_ops;
        counters["divergence_events"] = total.divergence_events;
        out["counters"] = counters;
        py::dict trap;
        trap["kind"] = block_trap_.kind;
        trap["reason"] = block_trap_.reason;
        trap["pc"] = block_trap_.pc < 0 ? py::object(py::none()) : py::object(py::int_(block_trap_.pc));
        trap["detail"] = block_trap_.detail;
        out["trap"] = trap;
        out["cta_barriers"] = cta_barrier_snapshot();
        if (!config_.const_banks.empty()) {
            py::dict const_memory;
            for (const auto& [bank, bytes] : config_.const_banks) {
                py::list data;
                for (std::uint8_t byte : bytes) {
                    data.append(byte);
                }
                const_memory[py::str(std::to_string(bank))] = data;
            }
            out["const_memory"] = const_memory;
        }
        return out;
    }

private:
    bool done() const {
        return std::all_of(warps_.begin(), warps_.end(), [](const NativeWarp& warp) {
            return warp.is_done();
        }) || block_trap_.kind != "none";
    }

    py::list cta_barrier_snapshot() const {
        py::list cta_barriers;
        const auto snapshot = cta_barriers_.snapshot(non_exited_thread_count());
        for (const auto& slot : snapshot) {
            py::dict barrier;
            barrier["phase"] = slot.active ? py::str("gathering") : py::str("inactive");
            barrier["arrived_count"] = slot.arrived_count;
            barrier["expected_count"] = slot.expected_count;
            barrier["phase_parity"] = slot.phase_parity;
            cta_barriers.append(barrier);
        }
        return cta_barriers;
    }

    void apply_cta_barrier_action(NativeWarp& warp) {
        const currygpu::iss::cta_barrier_action action = warp.pending_cta_barrier_action();
        warp.clear_pending_cta_barrier_action();
        if (!action.valid || block_trap_.kind != "none") {
            return;
        }
        currygpu::iss::cta_barrier_apply_result result = cta_barriers_.apply(action, non_exited_thread_count());
        if (result.error.failed) {
            py::dict detail;
            detail["trap_reason"] = result.error.reason;
            detail["pc"] = result.error.pc;
            detail["bar_id"] = result.error.bar_id;
            detail["expected_count"] = result.error.expected_count;
            if (result.error.arrived != 0) {
                detail["arrived"] = result.error.arrived;
            }
            if (result.error.new_expected_count != 0) {
                detail["new_expected_count"] = result.error.new_expected_count;
            }
            set_block_trap("synchronization", result.error.reason, result.error.pc, detail);
            return;
        }
        if (result.release.fired) {
            apply_cta_barrier_release(result.release);
            if (config_.race_check) {
                race_shadow_.advance_epoch();
            }
        }
    }

    void apply_cta_barrier_release(const currygpu::iss::cta_barrier_release& release) {
        for (NativeWarp& warp : warps_) {
            warp.release_cta_barrier(release.bar_id, release.waiting_masks, release.resume_pc);
        }
    }

    int non_exited_thread_count() const {
        int count = 0;
        for (const NativeWarp& warp : warps_) {
            count += std::popcount(warp.active_lane_mask());
        }
        return count;
    }

    bool any_warp_cta_blocked() const {
        return std::any_of(warps_.begin(), warps_.end(), [](const NativeWarp& warp) {
            return warp.is_cta_blocked();
        });
    }

    bool any_active_cta_barrier() const {
        return cta_barriers_.any_active();
    }

    void detect_cta_barrier_deadlock() {
        if (block_trap_.kind != "none" || !any_active_cta_barrier() || !any_warp_cta_blocked()) {
            return;
        }
        const bool has_runnable_warp = std::any_of(warps_.begin(), warps_.end(), [](const NativeWarp& warp) {
            return warp.is_runnable();
        });
        if (has_runnable_warp) {
            return;
        }
        const int bar_id = cta_barriers_.first_active_bar_id();
        py::dict detail;
        detail["trap_reason"] = "barrier_deadlock";
        detail["pc"] = 0;
        detail["bar_id"] = bar_id;
        detail["arrived"] = cta_barriers_.arrived_count(bar_id);
        detail["expected_count"] = cta_barriers_.expected_count(bar_id, non_exited_thread_count());
        set_block_trap("synchronization", "barrier_deadlock", 0, detail);
    }

    void detect_warp_deadlocks() {
        if (block_trap_.kind != "none") {
            return;
        }
        for (NativeWarp& warp : warps_) {
            if (warp.detect_warp_deadlock()) {
                block_trap_ = warp.trap();
                return;
            }
        }
    }

    int select_warp() {
        if (warps_.empty()) {
            return -1;
        }
        if (warp_sched_order_ == WarpSchedOrder::MinIdFirst) {
            for (std::size_t index = 0; index < warps_.size(); ++index) {
                if (warps_[index].prepare_for_block_schedule()) {
                    return static_cast<int>(index);
                }
            }
            return -1;
        }
        if (warp_sched_order_ == WarpSchedOrder::MaxIdFirst) {
            for (std::size_t offset = 0; offset < warps_.size(); ++offset) {
                std::size_t index = warps_.size() - 1 - offset;
                if (warps_[index].prepare_for_block_schedule()) {
                    return static_cast<int>(index);
                }
            }
            return -1;
        }
        for (std::size_t count = 0; count < warps_.size(); ++count) {
            const int index = (warp_round_robin_cursor_ + static_cast<int>(count)) % static_cast<int>(warps_.size());
            if (warps_[index].prepare_for_block_schedule()) {
                warp_round_robin_cursor_ = (index + 1) % static_cast<int>(warps_.size());
                return index;
            }
        }
        return -1;
    }

    void set_block_trap(std::string kind, std::string reason, int pc, py::dict detail) {
        block_trap_.kind = std::move(kind);
        block_trap_.reason = std::move(reason);
        block_trap_.pc = pc;
        block_trap_.detail = std::move(detail);
    }

    currygpu::iss::memory_space_impl& global_memory() {
        return global_memory_external_ == nullptr ? global_memory_owned_ : *global_memory_external_;
    }

    const currygpu::iss::memory_space_impl& global_memory() const {
        return global_memory_external_ == nullptr ? global_memory_owned_ : *global_memory_external_;
    }

    currygpu::iss::race_shadow_state* race_shadow_ptr() {
        return config_.race_check ? &race_shadow_ : nullptr;
    }

    LaunchConfig config_;
    WarpSchedOrder warp_sched_order_ = WarpSchedOrder::RoundRobin;
    int warp_round_robin_cursor_ = 0;
    currygpu::iss::memory_space_impl global_memory_owned_;
    currygpu::iss::memory_space_impl* global_memory_external_ = nullptr;
    currygpu::iss::memory_space_impl shared_memory_;
    currygpu::iss::race_shadow_state race_shadow_;
    std::vector<NativeWarp> warps_;
    currygpu::iss::cta_barrier_state cta_barriers_;
    Trap block_trap_;
};

class NativeGrid {
public:
    NativeGrid(const py::object& program, int num_gprs, SchedOrder sched_order, bool debug_checks, LaunchConfig config)
        : config_(std::move(config)) {
        const int cta_count = dim_product(config_.nctaid);
        for (int index = 0; index < cta_count; ++index) {
            LaunchConfig cta_config = config_;
            cta_config.ctaid = ctaid_for_index(index);
            ctas_.push_back(std::make_unique<NativeBlock>(program, num_gprs, sched_order, debug_checks, cta_config, &global_memory_));
        }
    }

    NativeGrid(std::vector<Instruction> program, int num_gprs, SchedOrder sched_order, bool debug_checks, LaunchConfig config)
        : config_(std::move(config)) {
        const int cta_count = dim_product(config_.nctaid);
        for (int index = 0; index < cta_count; ++index) {
            LaunchConfig cta_config = config_;
            cta_config.ctaid = ctaid_for_index(index);
            ctas_.push_back(std::make_unique<NativeBlock>(program, num_gprs, sched_order, debug_checks, cta_config, &global_memory_));
        }
    }

    py::dict step(int max_steps) {
        ++g_boundary_calls;
        if (max_steps < 0) {
            throw std::invalid_argument("max_steps must be non-negative");
        }
        while (current_cta_ < ctas_.size() && trap_.kind == "none") {
            py::dict cta_snapshot = ctas_[current_cta_]->step_no_boundary(max_steps);
            py::dict trap = cta_snapshot["trap"].cast<py::dict>();
            if (trap["kind"].cast<std::string>() != "none") {
                trap_.kind = trap["kind"].cast<std::string>();
                trap_.reason = trap["reason"].cast<std::string>();
                py::object pc = py::reinterpret_borrow<py::object>(trap["pc"]);
                trap_.pc = pc.is_none() ? -1 : pc.cast<int>();
                trap_.detail = trap["detail"].cast<py::dict>();
                break;
            }
            ++current_cta_;
        }
        return snapshot();
    }

    py::dict snapshot() const {
        if (ctas_.size() == 1) {
            return ctas_[0]->snapshot();
        }
        py::dict out;
        py::list ctas;
        Counters total;
        for (const auto& cta : ctas_) {
            py::dict cta_snapshot = cta->snapshot();
            py::dict counters = cta_snapshot["counters"].cast<py::dict>();
            total.instructions += counters["instructions"].cast<std::uint64_t>();
            total.warp_instructions += counters["warp_instructions"].cast<std::uint64_t>();
            total.mem_ops += counters["mem_ops"].cast<std::uint64_t>();
            total.divergence_events += counters["divergence_events"].cast<std::uint64_t>();
            py::dict memory = cta_snapshot["memory"].cast<py::dict>();
            PyDict_DelItemString(memory.ptr(), "global");
            ctas.append(cta_snapshot);
        }
        out["ctas"] = ctas;
        py::dict memory;
        memory["global"] = serialize_memory_space(global_memory_);
        out["memory"] = memory;
        py::dict counters;
        counters["instructions"] = total.instructions;
        counters["warp_instructions"] = total.warp_instructions;
        counters["mem_ops"] = total.mem_ops;
        counters["divergence_events"] = total.divergence_events;
        out["counters"] = counters;
        py::dict trap;
        trap["kind"] = trap_.kind;
        trap["reason"] = trap_.reason;
        trap["pc"] = trap_.pc < 0 ? py::object(py::none()) : py::object(py::int_(trap_.pc));
        trap["detail"] = trap_.detail;
        out["trap"] = trap;
        if (!config_.const_banks.empty()) {
            py::dict const_memory;
            for (const auto& [bank, bytes] : config_.const_banks) {
                py::list data;
                for (std::uint8_t byte : bytes) {
                    data.append(byte);
                }
                const_memory[py::str(std::to_string(bank))] = data;
            }
            out["const_memory"] = const_memory;
        }
        return out;
    }

private:
    std::array<int, 3> ctaid_for_index(int index) const {
        const int nx = config_.nctaid[0];
        const int ny = config_.nctaid[1];
        return {index % nx, (index / nx) % ny, index / (nx * ny)};
    }

    LaunchConfig config_;
    currygpu::iss::memory_space_impl global_memory_;
    std::vector<std::unique_ptr<NativeBlock>> ctas_;
    std::size_t current_cta_ = 0;
    Trap trap_;
};
