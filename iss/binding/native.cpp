#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <algorithm>
#include <array>
#include <cstdint>
#include <map>
#include <set>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "decoded_inst.gen.h"

namespace py = pybind11;

namespace {

constexpr int kLanes = 32;
constexpr int kDefaultGprs = 256;
constexpr std::uint32_t kMask32 = 0xFFFFFFFFu;

struct Guard {
    std::string pred = "PT";
    bool negate = false;
};

struct Instruction {
    std::string op;
    std::map<std::string, py::object> operands;
    Guard guard;
    bool decode_ok = true;
    std::string decode_trap;
};

struct Trap {
    std::string kind = "none";
    std::string reason;
    int pc = -1;
    py::dict detail;
};

struct Counters {
    std::uint64_t instructions = 0;
    std::uint64_t warp_instructions = 0;
    std::uint64_t mem_ops = 0;
    std::uint64_t divergence_events = 0;
};

enum class SchedOrder {
    MinPcFirst,
    MaxPcFirst,
    RoundRobin,
    OldestGroupFirst,
};

struct ExecutionGroup {
    int pc = 0;
    std::array<bool, kLanes> lanes{};
    std::uint64_t seq = 0;
};

struct Barrier {
    std::uint32_t participation_mask = 0;
    int reconv_pc = 0;
    bool valid = false;
};

enum class BarrierStatus {
    Unarmed,
    Armed,
    Dissolved,
};

std::uint64_t g_boundary_calls = 0;

std::string upper(std::string value) {
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char ch) {
        return static_cast<char>(std::toupper(ch));
    });
    return value;
}

std::string pred_name(const py::object& value) {
    std::string text = upper(py::str(value).cast<std::string>());
    if (!text.empty() && text[0] == '@') {
        text.erase(text.begin());
    }
    if (text == "TRUE") {
        return "PT";
    }
    return text;
}

bool is_predicate_name(const std::string& name) {
    if (name == "PT") {
        return true;
    }
    return name.size() == 2 && name[0] == 'P' && name[1] >= '0' && name[1] <= '6';
}

int reg_index(const py::object& value) {
    if (py::isinstance<py::int_>(value)) {
        return value.cast<int>();
    }
    if (py::isinstance<py::str>(value)) {
        std::string text = upper(value.cast<std::string>());
        if (text.size() > 1 && text[0] == 'R') {
            return std::stoi(text.substr(1));
        }
    }
    if (py::isinstance<py::dict>(value)) {
        py::dict data = value.cast<py::dict>();
        if (data.contains("index")) {
            return py::reinterpret_borrow<py::object>(data["index"]).cast<int>();
        }
        if (data.contains("name")) {
            return reg_index(py::reinterpret_borrow<py::object>(data["name"]));
        }
    }
    throw std::invalid_argument("unsupported register operand");
}

int barrier_index(const py::object& value) {
    if (py::isinstance<py::int_>(value)) {
        return value.cast<int>();
    }
    if (py::isinstance<py::str>(value)) {
        std::string text = upper(value.cast<std::string>());
        if (text.size() > 1 && text[0] == 'B') {
            return std::stoi(text.substr(1));
        }
    }
    if (py::isinstance<py::dict>(value)) {
        py::dict data = value.cast<py::dict>();
        if (data.contains("index")) {
            return py::reinterpret_borrow<py::object>(data["index"]).cast<int>();
        }
        if (data.contains("name")) {
            return barrier_index(py::reinterpret_borrow<py::object>(data["name"]));
        }
    }
    throw std::invalid_argument("unsupported barrier operand");
}

std::string sreg_name(const py::object& value) {
    if (py::isinstance<py::str>(value)) {
        return upper(value.cast<std::string>());
    }
    if (py::isinstance<py::int_>(value) && value.cast<int>() == 0) {
        return "SR_LANEID";
    }
    if (py::isinstance<py::dict>(value)) {
        py::dict data = value.cast<py::dict>();
        if (data.contains("name")) {
            return sreg_name(py::reinterpret_borrow<py::object>(data["name"]));
        }
        if (data.contains("selector")) {
            return sreg_name(py::reinterpret_borrow<py::object>(data["selector"]));
        }
    }
    throw std::invalid_argument("unsupported special register operand");
}

SchedOrder parse_sched_order(const std::string& value) {
    const std::string name = upper(value);
    if (name == "MIN_PC_FIRST") {
        return SchedOrder::MinPcFirst;
    }
    if (name == "MAX_PC_FIRST") {
        return SchedOrder::MaxPcFirst;
    }
    if (name == "ROUND_ROBIN") {
        return SchedOrder::RoundRobin;
    }
    if (name == "OLDEST_GROUP_FIRST") {
        return SchedOrder::OldestGroupFirst;
    }
    throw std::invalid_argument("unknown scheduling order: " + value);
}

py::object operand(const Instruction& inst, std::initializer_list<const char*> names, py::object fallback = py::none()) {
    for (const char* name : names) {
        auto iter = inst.operands.find(name);
        if (iter != inst.operands.end()) {
            return iter->second;
        }
    }
    if (!fallback.is_none()) {
        return fallback;
    }
    throw std::invalid_argument(inst.op + " is missing operand");
}

Guard parse_guard(const py::object& inst) {
    Guard guard;
    py::dict data = inst.cast<py::dict>();
    if (!data.contains("guard") && !data.contains("predicate")) {
        return guard;
    }
    py::object value = py::reinterpret_borrow<py::object>(data.contains("guard") ? data["guard"] : data["predicate"]);
    if (py::isinstance<py::str>(value)) {
        std::string text = value.cast<std::string>();
        if (!text.empty() && text[0] == '@') {
            text.erase(text.begin());
        }
        if (!text.empty() && text[0] == '!') {
            guard.negate = true;
            text.erase(text.begin());
        }
        guard.pred = pred_name(py::str(text));
        return guard;
    }
    py::dict mapping = value.cast<py::dict>();
    if (mapping.contains("predicate")) {
        guard.pred = pred_name(py::reinterpret_borrow<py::object>(mapping["predicate"]));
    } else if (mapping.contains("pred")) {
        guard.pred = pred_name(py::reinterpret_borrow<py::object>(mapping["pred"]));
    }
    if (mapping.contains("negated")) {
        guard.negate = py::reinterpret_borrow<py::object>(mapping["negated"]).cast<bool>();
    } else if (mapping.contains("negate")) {
        guard.negate = py::reinterpret_borrow<py::object>(mapping["negate"]).cast<bool>();
    }
    return guard;
}

std::vector<Instruction> adapt_program(const py::object& program) {
    std::vector<Instruction> result;
    for (py::handle item : program) {
        py::dict data = py::reinterpret_borrow<py::object>(item).cast<py::dict>();
        Instruction inst;
        py::object op = py::none();
        for (const char* key : {"op", "opcode", "mnemonic", "name", "inst"}) {
            if (data.contains(key)) {
                op = py::reinterpret_borrow<py::object>(data[key]);
                break;
            }
        }
        if (op.is_none()) {
            throw std::invalid_argument("decoded instruction is missing an opcode name");
        }
        inst.op = upper(op.cast<std::string>());
        if (data.contains("operands")) {
            py::dict operands = py::reinterpret_borrow<py::object>(data["operands"]).cast<py::dict>();
            for (auto item : operands) {
                inst.operands.emplace(py::str(item.first).cast<std::string>(), py::reinterpret_borrow<py::object>(item.second));
            }
        }
        inst.guard = parse_guard(py::reinterpret_borrow<py::object>(item));
        result.push_back(std::move(inst));
    }
    return result;
}

unsigned __int128 py_to_u128(const py::object& value) {
    py::int_ integer(value);
    if (integer.attr("__lt__")(0).cast<bool>() || integer.attr("bit_length")().cast<int>() > 128) {
        throw std::invalid_argument("encoded word must fit 128 bits");
    }
    py::bytes bytes = integer.attr("to_bytes")(16, "little");
    std::string raw = bytes;
    std::uint64_t low = 0;
    std::uint64_t high = 0;
    for (int index = 0; index < 8; ++index) {
        low |= static_cast<std::uint64_t>(static_cast<unsigned char>(raw[index])) << (index * 8);
        high |= static_cast<std::uint64_t>(static_cast<unsigned char>(raw[index + 8])) << (index * 8);
    }
    return (static_cast<unsigned __int128>(high) << 64) | low;
}

std::map<std::string, std::int64_t> decoded_fields(const currygpu::isa::decoded_inst& decoded) {
    std::map<std::string, std::int64_t> fields;
    for (int index = 0; index < decoded.field_count; ++index) {
        fields.emplace(decoded.fields[index].name, decoded.fields[index].value);
    }
    return fields;
}

std::string reg_name(std::int64_t value) {
    if (value == 255) {
        return "RZ";
    }
    return "R" + std::to_string(value);
}

std::string pred_symbol(std::int64_t value) {
    if (value == 7) {
        return "PT";
    }
    return "P" + std::to_string(value);
}

Instruction instruction_from_word(unsigned __int128 word) {
    currygpu::isa::decoded_inst decoded = currygpu::isa::decode_once(word);
    Instruction inst;
    if (!decoded.ok) {
        inst.decode_ok = false;
        inst.decode_trap = decoded.trap;
        return inst;
    }
    auto fields = decoded_fields(decoded);
    inst.op = decoded.name;
    inst.guard.pred = pred_symbol(fields["guard_pred"]);
    inst.guard.negate = fields["guard_neg"] != 0;
    if (inst.op == "IADD3") {
        inst.operands["rd"] = py::str(reg_name(fields["rd"]));
        inst.operands["src_a"] = py::str(reg_name(fields["src_a"]));
        inst.operands["src_b"] = py::str(reg_name(fields["src_b"]));
        inst.operands["src_c"] = py::int_(fields["src_c"]);
    } else if (inst.op == "ISETP") {
        static const std::array<const char*, 6> cmp_names = {"EQ", "NE", "LT", "LE", "GT", "GE"};
        std::int64_t cmp = fields["cmp"];
        if (cmp < 0 || cmp >= static_cast<std::int64_t>(cmp_names.size())) {
            inst.decode_ok = false;
            inst.decode_trap = "modifier";
            return inst;
        }
        inst.operands["pd"] = py::str(pred_symbol(fields["pd"]));
        inst.operands["src_a"] = py::str(reg_name(fields["src_a"]));
        inst.operands["src_b"] = py::str(reg_name(fields["src_b"]));
        inst.operands["cmp"] = py::str(cmp_names[cmp]);
    } else if (inst.op == "LOP3") {
        inst.operands["rd"] = py::str(reg_name(fields["rd"]));
        inst.operands["src_a"] = py::str(reg_name(fields["src_a"]));
        inst.operands["src_b"] = py::str(reg_name(fields["src_b"]));
        inst.operands["src_c"] = py::str(reg_name(fields["src_c"]));
        inst.operands["lut"] = py::int_(fields["lut"]);
    } else if (inst.op == "BRA") {
        inst.operands["target"] = py::int_(fields["target"] / 16);
    } else if (inst.op == "S2R") {
        if (fields["sr"] != 0) {
            inst.decode_ok = false;
            inst.decode_trap = "sreg";
            return inst;
        }
        inst.operands["rd"] = py::str(reg_name(fields["rd"]));
        inst.operands["sr"] = py::str("SR_LANEID");
    } else if (inst.op == "BSSY") {
        inst.operands["bar"] = py::str("B" + std::to_string(fields["bar"]));
        inst.operands["target"] = py::int_(fields["target"] / 16);
    } else if (inst.op == "BSYNC") {
        inst.operands["bar"] = py::str("B" + std::to_string(fields["bar"]));
    } else if (inst.op == "BREAK") {
        inst.operands["bar"] = py::str("B" + std::to_string(fields["bar"]));
    } else if (inst.op == "ELECT") {
        inst.operands["pd"] = py::str(pred_symbol(fields["pd"]));
        inst.operands["membermask"] = py::int_(fields["membermask"]);
    } else if (inst.op == "VOTE") {
        static const std::array<const char*, 4> mode_names = {"ANY", "ALL", "EQ", "BALLOT"};
        std::int64_t mode = fields["mode"];
        if (mode < 0 || mode >= static_cast<std::int64_t>(mode_names.size())) {
            inst.decode_ok = false;
            inst.decode_trap = "modifier";
            return inst;
        }
        inst.operands["pd"] = py::str(pred_symbol(fields["pd"]));
        inst.operands["src"] = py::str(pred_symbol(fields["src"]));
        inst.operands["membermask"] = py::int_(fields["membermask"]);
        inst.operands["rd"] = py::str(reg_name(fields["rd"]));
        inst.operands["mode"] = py::str(mode_names[mode]);
    } else if (inst.op == "YIELD") {
    } else if (inst.op != "EXIT") {
        inst.decode_ok = false;
        inst.decode_trap = "unknown";
    }
    return inst;
}

std::vector<Instruction> adapt_words(const py::object& words) {
    std::vector<Instruction> program;
    for (py::handle item : words) {
        program.push_back(instruction_from_word(py_to_u128(py::reinterpret_borrow<py::object>(item))));
    }
    return program;
}

class NativeWarp {
public:
    explicit NativeWarp(const py::object& program, int num_gprs = kDefaultGprs, SchedOrder order = SchedOrder::MinPcFirst, bool debug_checks = false)
        : program_(adapt_program(program)),
          vgpr_(num_gprs, std::array<std::uint32_t, kLanes>{}),
          order_(order),
          debug_checks_(debug_checks) {
        for (auto& values : vgpr_) {
            values.fill(0);
        }
        for (int index = 0; index <= 6; ++index) {
            predicates_["P" + std::to_string(index)].fill(false);
        }
        predicates_["PT"].fill(true);
        active_mask_.fill(true);
        pc_.fill(0);
        lane_state_.fill("active");
        blocked_on_.fill(-1);
    }

    explicit NativeWarp(std::vector<Instruction> program, int num_gprs = kDefaultGprs, SchedOrder order = SchedOrder::MinPcFirst, bool debug_checks = false)
        : program_(std::move(program)),
          vgpr_(num_gprs, std::array<std::uint32_t, kLanes>{}),
          order_(order),
          debug_checks_(debug_checks) {
        for (auto& values : vgpr_) {
            values.fill(0);
        }
        for (int index = 0; index <= 6; ++index) {
            predicates_["P" + std::to_string(index)].fill(false);
        }
        predicates_["PT"].fill(true);
        active_mask_.fill(true);
        pc_.fill(0);
        lane_state_.fill("active");
        blocked_on_.fill(-1);
    }

    py::dict step(int max_steps) {
        ++g_boundary_calls;
        if (max_steps < 0) {
            throw std::invalid_argument("max_steps must be non-negative");
        }
        int issued = 0;
        while (!done() && trap_.kind == "none") {
            if (issued >= max_steps) {
                set_trap("max_steps", "budget_exhausted", first_runnable_pc_or_zero(), py::dict());
                break;
            }
            std::vector<ExecutionGroup> groups = build_groups();
            if (groups.empty()) {
                try_fire_barriers();
                groups = build_groups();
            }
            if (groups.empty()) {
                promote_yielded_lanes();
                groups = build_groups();
            }
            if (groups.empty()) {
                if (has_blocked_lanes()) {
                    set_convergence_trap("deadlock_no_progress", first_runnable_pc_or_zero(), -1);
                }
                break;
            }
            ExecutionGroup group = select_group(groups);
            int pc = group.pc;
            if (pc >= static_cast<int>(program_.size())) {
                set_trap("execute", "illegal_pc", pc, py::dict());
                break;
            }
            const Instruction& inst = program_[pc];
            if (!inst.decode_ok) {
                py::dict detail;
                detail["trap"] = inst.decode_trap;
                set_trap("decode", inst.decode_trap.empty() ? "decode_failure" : inst.decode_trap, pc, detail);
                break;
            }
            std::array<bool, kLanes> lane_mask;
            try {
                lane_mask = guard_mask(inst.guard, group.lanes);
            } catch (const std::exception& exc) {
                py::dict detail;
                detail["message"] = exc.what();
                set_trap("decode", "decode_failure", pc, detail);
                break;
            }
            std::array<bool, kLanes> issued_mask = group.lanes;
            counters_.warp_instructions += 1;
            counters_.instructions += static_cast<std::uint64_t>(std::count(lane_mask.begin(), lane_mask.end(), true));
            std::array<int, kLanes> next_pc = fallthrough();
            issue_pc_ = pc;
            try {
                if (inst.op == "IADD3") {
                    exec_iadd3(inst, lane_mask);
                } else if (inst.op == "ISETP") {
                    exec_isetp(inst, lane_mask);
                } else if (inst.op == "LOP3") {
                    exec_lop3(inst, lane_mask);
                } else if (inst.op == "BRA") {
                    next_pc = exec_bra(inst, lane_mask);
                } else if (inst.op == "S2R") {
                    exec_s2r(inst, lane_mask);
                } else if (inst.op == "BSSY") {
                    exec_bssy(inst, group.lanes);
                } else if (inst.op == "BSYNC") {
                    exec_bsync(inst, group.lanes, next_pc);
                } else if (inst.op == "BREAK") {
                    exec_break(inst, lane_mask);
                } else if (inst.op == "YIELD") {
                    exec_yield(lane_mask);
                } else if (inst.op == "ELECT") {
                    exec_elect(inst, lane_mask);
                } else if (inst.op == "VOTE") {
                    exec_vote(inst, lane_mask);
                } else if (inst.op == "EXIT") {
                    exec_exit(lane_mask);
                } else {
                    py::dict detail;
                    detail["op"] = inst.op;
                    set_trap("decode", "unknown_instruction", pc, detail);
                }
            } catch (const std::exception& exc) {
                py::dict detail;
                detail["message"] = exc.what();
                detail["op"] = inst.op;
                set_trap("decode", "decode_failure", pc, detail);
            }
            issue_pc_ = -1;
            if (trap_.kind != "none") {
                break;
            }
            if (splits_active_pc(issued_mask, next_pc)) {
                counters_.divergence_events += 1;
            }
            for (int lane = 0; lane < kLanes; ++lane) {
                if (issued_mask[lane] && lane_state_[lane] != "blocked") {
                    pc_[lane] = next_pc[lane];
                }
            }
            ++issued;
        }
        return snapshot();
    }

    py::dict snapshot() const {
        py::dict out;
        py::list active;
        py::list pc;
        py::list lane_state;
        for (int lane = 0; lane < kLanes; ++lane) {
            active.append(active_mask_[lane]);
            pc.append(pc_[lane]);
            lane_state.append(lane_state_[lane]);
        }
        out["active_mask"] = active;
        out["pc"] = pc;
        out["lane_state"] = lane_state;

        py::dict counters;
        counters["instructions"] = counters_.instructions;
        counters["warp_instructions"] = counters_.warp_instructions;
        counters["mem_ops"] = counters_.mem_ops;
        counters["divergence_events"] = counters_.divergence_events;
        out["counters"] = counters;

        py::dict trap;
        trap["kind"] = trap_.kind;
        trap["reason"] = trap_.reason;
        trap["pc"] = trap_.pc < 0 ? py::object(py::none()) : py::object(py::int_(trap_.pc));
        trap["detail"] = trap_.detail;
        out["trap"] = trap;

        py::dict predicates;
        for (const auto& [name, values] : predicates_) {
            py::list lanes;
            for (bool value : values) {
                lanes.append(value);
            }
            predicates[py::str(name)] = lanes;
        }
        out["predicates"] = predicates;

        py::dict vgpr;
        for (std::size_t reg = 0; reg < vgpr_.size(); ++reg) {
            py::list lanes;
            for (std::uint32_t value : vgpr_[reg]) {
                lanes.append(value);
            }
            vgpr[py::str(std::to_string(reg))] = lanes;
        }
        out["vgpr"] = vgpr;

        py::list uregs;
        for (int index = 0; index < 64; ++index) {
            uregs.append(0);
        }
        out["uniform_registers"] = uregs;

        py::dict memory;
        memory["global"] = py::dict();
        memory["shared"] = py::dict();
        memory["local"] = py::dict();
        out["memory"] = memory;

        py::list barriers;
        for (int index = 0; index < 16; ++index) {
            py::dict barrier;
            barrier["participation_mask"] = bx_[index].participation_mask;
            barrier["reconv_pc"] = bx_[index].reconv_pc;
            barrier["valid"] = bx_[index].valid;
            barriers.append(barrier);
        }
        py::dict bx;
        bx["barriers"] = barriers;
        out["bx"] = bx;
        return out;
    }

private:
    bool done() const {
        return std::none_of(active_mask_.begin(), active_mask_.end(), [](bool active) { return active; });
    }

    bool has_blocked_lanes() const {
        for (int lane = 0; lane < kLanes; ++lane) {
            if (active_mask_[lane] && lane_state_[lane] == "blocked") {
                return true;
            }
        }
        return false;
    }

    void promote_yielded_lanes() {
        for (int lane = 0; lane < kLanes; ++lane) {
            if (active_mask_[lane] && lane_state_[lane] == "yielded") {
                lane_state_[lane] = "active";
            }
        }
    }

    std::vector<ExecutionGroup> build_groups() {
        std::map<int, std::array<bool, kLanes>> lanes_by_pc;
        for (int lane = 0; lane < kLanes; ++lane) {
            if (!active_mask_[lane] || lane_state_[lane] != "active") {
                continue;
            }
            auto [iter, inserted] = lanes_by_pc.emplace(pc_[lane], std::array<bool, kLanes>{});
            if (inserted) {
                iter->second.fill(false);
            }
            iter->second[lane] = true;
        }
        std::set<int> live_pcs;
        for (const auto& [pc, _] : lanes_by_pc) {
            live_pcs.insert(pc);
            if (!group_seq_.contains(pc)) {
                group_seq_[pc] = next_group_seq_++;
            }
        }
        for (auto iter = group_seq_.begin(); iter != group_seq_.end();) {
            if (live_pcs.contains(iter->first)) {
                ++iter;
            } else {
                iter = group_seq_.erase(iter);
            }
        }
        std::vector<ExecutionGroup> groups;
        for (const auto& [pc, lanes] : lanes_by_pc) {
            groups.push_back(ExecutionGroup{pc, lanes, group_seq_[pc]});
        }
        return groups;
    }

    ExecutionGroup select_group(const std::vector<ExecutionGroup>& groups) {
        if (groups.empty()) {
            return ExecutionGroup{};
        }
        if (order_ == SchedOrder::MaxPcFirst) {
            return groups.back();
        }
        if (order_ == SchedOrder::RoundRobin) {
            auto iter = std::find_if(groups.begin(), groups.end(), [this](const ExecutionGroup& group) {
                return group.pc >= round_robin_cursor_;
            });
            if (iter == groups.end()) {
                iter = groups.begin();
            }
            round_robin_cursor_ = iter->pc + 1;
            return *iter;
        }
        if (order_ == SchedOrder::OldestGroupFirst) {
            return *std::min_element(groups.begin(), groups.end(), [](const ExecutionGroup& left, const ExecutionGroup& right) {
                if (left.seq != right.seq) {
                    return left.seq < right.seq;
                }
                return left.pc < right.pc;
            });
        }
        return groups.front();
    }

    int first_runnable_pc_or_zero() const {
        int value = 0;
        bool found = false;
        for (int lane = 0; lane < kLanes; ++lane) {
            if (!active_mask_[lane] || lane_state_[lane] != "active") {
                continue;
            }
            if (!found || pc_[lane] < value) {
                value = pc_[lane];
                found = true;
            }
        }
        return value;
    }

    int trap_pc_or_zero() const {
        return issue_pc_ >= 0 ? issue_pc_ : first_runnable_pc_or_zero();
    }

    bool is_default_guard(const Guard& guard) const {
        return guard.pred == "PT" && !guard.negate;
    }

    std::array<bool, kLanes> guard_mask(const Guard& guard, const std::array<bool, kLanes>& group) const {
        auto iter = predicates_.find(guard.pred);
        if (iter == predicates_.end()) {
            throw std::invalid_argument("invalid predicate: " + guard.pred);
        }
        std::array<bool, kLanes> result{};
        for (int lane = 0; lane < kLanes; ++lane) {
            bool pred = iter->second[lane];
            result[lane] = group[lane] && (guard.negate ? !pred : pred);
        }
        return result;
    }

    std::array<int, kLanes> fallthrough() const {
        std::array<int, kLanes> next{};
        for (int lane = 0; lane < kLanes; ++lane) {
            next[lane] = pc_[lane] + 1;
        }
        return next;
    }

    std::uint32_t read_word(const py::object& value, int lane) const {
        if (py::isinstance<py::int_>(value)) {
            return static_cast<std::uint32_t>(value.cast<std::int64_t>());
        }
        if (py::isinstance<py::str>(value)) {
            std::string text = upper(value.cast<std::string>());
            if (text == "RZ") {
                return 0;
            }
            if (text.size() > 1 && text[0] == 'R') {
                int reg = std::stoi(text.substr(1));
                if (reg < 0 || reg >= static_cast<int>(vgpr_.size())) {
                    throw std::invalid_argument("GPR index out of range");
                }
                return vgpr_[reg][lane];
            }
        }
        if (!py::isinstance<py::str>(value) && py::isinstance<py::sequence>(value)) {
            py::sequence values = value.cast<py::sequence>();
            if (values.size() != kLanes) {
                throw std::invalid_argument("per-lane operand must have 32 values");
            }
            return static_cast<std::uint32_t>(py::reinterpret_borrow<py::object>(values[lane]).cast<std::int64_t>());
        }
        throw std::invalid_argument("unsupported operand");
    }

    void write_gpr(const py::object& dst, int lane, std::uint32_t value) {
        if (py::isinstance<py::str>(dst) && upper(dst.cast<std::string>()) == "RZ") {
            return;
        }
        int reg = reg_index(dst);
        if (reg < 0 || reg >= static_cast<int>(vgpr_.size())) {
            throw std::invalid_argument("GPR index out of range");
        }
        vgpr_[reg][lane] = value;
    }

    void exec_iadd3(const Instruction& inst, const std::array<bool, kLanes>& mask) {
        py::object dst = operand(inst, {"dst", "rd"});
        py::object src0 = operand(inst, {"src0", "src_a", "a"});
        py::object src1 = operand(inst, {"src1", "src_b", "b"}, py::str("RZ"));
        py::object src2 = operand(inst, {"src2", "src_c", "c"}, py::str("RZ"));
        for (int lane = 0; lane < kLanes; ++lane) {
            if (mask[lane]) {
                write_gpr(dst, lane, (read_word(src0, lane) + read_word(src1, lane) + read_word(src2, lane)) & kMask32);
            }
        }
    }

    void exec_isetp(const Instruction& inst, const std::array<bool, kLanes>& mask) {
        std::string dst = pred_name(operand(inst, {"dst", "dst_pred", "pd"}));
        if (dst == "PT" || !is_predicate_name(dst)) {
            throw std::invalid_argument("invalid predicate destination");
        }
        py::object src0 = operand(inst, {"src0", "src_a", "a"});
        py::object src1 = operand(inst, {"src1", "src_b", "b"});
        std::string cmp = upper(operand(inst, {"cmp", "compare", "op"}, py::str("EQ")).cast<std::string>());
        for (int lane = 0; lane < kLanes; ++lane) {
            if (mask[lane]) {
                std::uint32_t left = read_word(src0, lane);
                std::uint32_t right = read_word(src1, lane);
                if (cmp == "EQ") {
                    predicates_[dst][lane] = left == right;
                } else if (cmp == "NE") {
                    predicates_[dst][lane] = left != right;
                } else if (cmp == "GT") {
                    predicates_[dst][lane] = static_cast<std::int32_t>(left) > static_cast<std::int32_t>(right);
                } else if (cmp == "GE") {
                    predicates_[dst][lane] = static_cast<std::int32_t>(left) >= static_cast<std::int32_t>(right);
                } else if (cmp == "LT") {
                    predicates_[dst][lane] = static_cast<std::int32_t>(left) < static_cast<std::int32_t>(right);
                } else if (cmp == "LE") {
                    predicates_[dst][lane] = static_cast<std::int32_t>(left) <= static_cast<std::int32_t>(right);
                } else {
                    throw std::invalid_argument("unsupported predicate comparison");
                }
            }
        }
    }

    void exec_lop3(const Instruction& inst, const std::array<bool, kLanes>& mask) {
        py::object dst = operand(inst, {"dst", "rd"});
        py::object src0 = operand(inst, {"src0", "src_a", "a"});
        py::object src1 = operand(inst, {"src1", "src_b", "b"});
        py::object src2 = operand(inst, {"src2", "src_c", "c"}, py::str("RZ"));
        int lut = operand(inst, {"lut", "imm", "truth_table"}).cast<int>();
        if (lut < 0 || lut > 0xFF) {
            py::dict detail;
            detail["lut"] = lut;
            set_trap("decode", "invalid_lop3_lut", trap_pc_or_zero(), detail);
            return;
        }
        for (int lane = 0; lane < kLanes; ++lane) {
            if (!mask[lane]) {
                continue;
            }
            std::uint32_t a = read_word(src0, lane);
            std::uint32_t b = read_word(src1, lane);
            std::uint32_t c = read_word(src2, lane);
            std::uint32_t result = 0;
            for (int bit = 0; bit < 32; ++bit) {
                int index = ((a >> bit) & 1u) | static_cast<int>(((b >> bit) & 1u) << 1) | static_cast<int>(((c >> bit) & 1u) << 2);
                result |= static_cast<std::uint32_t>((lut >> index) & 1) << bit;
            }
            write_gpr(dst, lane, result);
        }
    }

    std::array<int, kLanes> exec_bra(const Instruction& inst, const std::array<bool, kLanes>& mask) {
        int target = operand(inst, {"target", "pc"}).cast<int>();
        if (target < 0 || target >= static_cast<int>(program_.size())) {
            py::dict detail;
            detail["target"] = target;
            set_trap("execute", "illegal_branch_target", trap_pc_or_zero(), detail);
            return fallthrough();
        }
        std::array<int, kLanes> next = fallthrough();
        for (int lane = 0; lane < kLanes; ++lane) {
            if (mask[lane]) {
                next[lane] = target;
            }
        }
        return next;
    }

    void exec_s2r(const Instruction& inst, const std::array<bool, kLanes>& mask) {
        std::string selector = sreg_name(operand(inst, {"sr", "sreg"}));
        if (selector != "SR_LANEID") {
            throw std::invalid_argument("unsupported special register selector");
        }
        py::object dst = operand(inst, {"dst", "rd"});
        for (int lane = 0; lane < kLanes; ++lane) {
            if (mask[lane]) {
                write_gpr(dst, lane, static_cast<std::uint32_t>(lane));
            }
        }
    }

    void exec_bssy(const Instruction& inst, const std::array<bool, kLanes>& group_mask) {
        if (!is_default_guard(inst.guard)) {
            set_convergence_trap("predicated_barrier_unsupported", trap_pc_or_zero(), -1);
            return;
        }
        int index = barrier_index(operand(inst, {"bar", "barrier"}));
        if (index < 0 || index >= static_cast<int>(bx_.size())) {
            set_convergence_trap("barrier_slots_exhausted", trap_pc_or_zero(), index);
            return;
        }
        int target = operand(inst, {"target", "pc"}).cast<int>();
        if (target < 0 || target >= static_cast<int>(program_.size())) {
            py::dict detail = convergence_detail("illegal_reconv_pc", trap_pc_or_zero(), index);
            detail["target"] = target;
            set_trap("convergence", "illegal_reconv_pc", trap_pc_or_zero(), detail);
            return;
        }
        if (barrier_status_[index] == BarrierStatus::Armed) {
            set_convergence_trap("bssy_clobbers_live_barrier", trap_pc_or_zero(), index);
            return;
        }
        bx_[index].participation_mask = lane_mask_bits(group_mask);
        bx_[index].reconv_pc = target;
        bx_[index].valid = true;
        barrier_status_[index] = BarrierStatus::Armed;
    }

    void exec_bsync(const Instruction& inst, const std::array<bool, kLanes>& group_mask, std::array<int, kLanes>& next_pc) {
        if (!is_default_guard(inst.guard)) {
            set_convergence_trap("predicated_barrier_unsupported", trap_pc_or_zero(), -1);
            return;
        }
        int index = barrier_index(operand(inst, {"bar", "barrier"}));
        if (index < 0 || index >= static_cast<int>(bx_.size()) || barrier_status_[index] == BarrierStatus::Unarmed) {
            set_convergence_trap("bsync_invalid_barrier", trap_pc_or_zero(), index);
            return;
        }
        if (barrier_status_[index] == BarrierStatus::Dissolved) {
            return;
        }
        for (int lane = 0; lane < kLanes; ++lane) {
            if (group_mask[lane]) {
                lane_state_[lane] = "blocked";
                blocked_on_[lane] = index;
            }
        }
        try_fire_barrier(index, &next_pc);
    }

    void exec_break(const Instruction& inst, const std::array<bool, kLanes>& mask) {
        int index = barrier_index(operand(inst, {"bar", "barrier"}));
        if (index < 0 || index >= static_cast<int>(bx_.size())) {
            set_convergence_trap("bsync_invalid_barrier", trap_pc_or_zero(), index);
            return;
        }
        if (barrier_status_[index] != BarrierStatus::Armed) {
            return;
        }
        for (int lane = 0; lane < kLanes; ++lane) {
            if (mask[lane]) {
                bx_[index].participation_mask &= ~(std::uint32_t{1} << lane);
            }
        }
        if (bx_[index].participation_mask == 0) {
            dissolve_barrier(index);
        }
    }

    void exec_yield(const std::array<bool, kLanes>& mask) {
        for (int lane = 0; lane < kLanes; ++lane) {
            if (mask[lane]) {
                lane_state_[lane] = "yielded";
                blocked_on_[lane] = -1;
            }
        }
    }

    void exec_elect(const Instruction& inst, const std::array<bool, kLanes>& participant_mask) {
        const std::uint32_t membermask = read_membermask(inst);
        if (!validate_membermask(membermask, participant_mask)) {
            return;
        }
        const int leader = first_member_lane(membermask);
        if (leader < 0) {
            set_convergence_trap("elect_not_unique", trap_pc_or_zero(), -1);
            return;
        }
        std::string dst = pred_name(operand(inst, {"dst", "dst_pred", "pd"}));
        if (dst == "PT" || !is_predicate_name(dst)) {
            throw std::invalid_argument("invalid predicate destination");
        }
        int true_count = 0;
        for (int lane = 0; lane < kLanes; ++lane) {
            if ((membermask & (std::uint32_t{1} << lane)) == 0 || !participant_mask[lane]) {
                continue;
            }
            const bool elected = lane == leader;
            predicates_[dst][lane] = elected;
            if (elected) {
                true_count += 1;
            }
        }
        if (true_count != 1) {
            set_convergence_trap("elect_not_unique", trap_pc_or_zero(), -1);
        }
    }

    void exec_vote(const Instruction& inst, const std::array<bool, kLanes>& participant_mask) {
        const std::uint32_t membermask = read_membermask(inst);
        if (!validate_membermask(membermask, participant_mask)) {
            return;
        }
        std::string dst = pred_name(operand(inst, {"dst", "dst_pred", "pd"}));
        if (dst == "PT" || !is_predicate_name(dst)) {
            throw std::invalid_argument("invalid predicate destination");
        }
        std::string src = pred_name(operand(inst, {"src", "src_pred", "psrc"}));
        auto src_iter = predicates_.find(src);
        if (src_iter == predicates_.end()) {
            throw std::invalid_argument("invalid predicate source");
        }
        std::string mode = upper(operand(inst, {"mode"}, py::str("ANY")).cast<std::string>());
        bool result = false;
        if (mode == "ANY" || mode == "BALLOT") {
            result = false;
            for (int lane = 0; lane < kLanes; ++lane) {
                if ((membermask & (std::uint32_t{1} << lane)) != 0 && participant_mask[lane]) {
                    result = result || src_iter->second[lane];
                }
            }
        } else if (mode == "ALL") {
            result = true;
            for (int lane = 0; lane < kLanes; ++lane) {
                if ((membermask & (std::uint32_t{1} << lane)) != 0 && participant_mask[lane]) {
                    result = result && src_iter->second[lane];
                }
            }
        } else if (mode == "EQ") {
            int first_lane = first_member_lane(membermask);
            bool first = src_iter->second[first_lane];
            result = true;
            for (int lane = 0; lane < kLanes; ++lane) {
                if ((membermask & (std::uint32_t{1} << lane)) != 0 && participant_mask[lane]) {
                    result = result && (src_iter->second[lane] == first);
                }
            }
        } else {
            throw std::invalid_argument("unsupported vote mode");
        }
        std::uint32_t ballot = 0;
        for (int lane = 0; lane < kLanes; ++lane) {
            if ((membermask & (std::uint32_t{1} << lane)) == 0 || !participant_mask[lane]) {
                continue;
            }
            predicates_[dst][lane] = result;
            if (src_iter->second[lane]) {
                ballot |= std::uint32_t{1} << lane;
            }
        }
        if (mode == "BALLOT") {
            py::object dst_reg = operand(inst, {"rd", "dst_reg"}, py::str("RZ"));
            for (int lane = 0; lane < kLanes; ++lane) {
                if ((membermask & (std::uint32_t{1} << lane)) != 0 && participant_mask[lane]) {
                    write_gpr(dst_reg, lane, ballot);
                }
            }
        }
    }

    bool splits_active_pc(const std::array<bool, kLanes>& issued_mask, const std::array<int, kLanes>& next_pc) const {
        int seen = -1;
        for (int lane = 0; lane < kLanes; ++lane) {
            if (!issued_mask[lane] || !active_mask_[lane] || lane_state_[lane] != "active") {
                continue;
            }
            if (seen < 0) {
                seen = next_pc[lane];
            } else if (seen != next_pc[lane]) {
                return true;
            }
        }
        return false;
    }

    std::uint32_t lane_mask_bits(const std::array<bool, kLanes>& mask) const {
        std::uint32_t bits = 0;
        for (int lane = 0; lane < kLanes; ++lane) {
            if (mask[lane]) {
                bits |= std::uint32_t{1} << lane;
            }
        }
        return bits;
    }

    std::uint32_t read_membermask(const Instruction& inst) const {
        py::object value = operand(inst, {"membermask", "mask"});
        if (!py::isinstance<py::int_>(value)) {
            throw std::invalid_argument("membermask must be an integer");
        }
        std::int64_t parsed = value.cast<std::int64_t>();
        if (parsed < 0 || parsed > static_cast<std::int64_t>(kMask32)) {
            throw std::invalid_argument("membermask out of range");
        }
        return static_cast<std::uint32_t>(parsed);
    }

    int first_member_lane(std::uint32_t membermask) const {
        for (int lane = 0; lane < kLanes; ++lane) {
            if ((membermask & (std::uint32_t{1} << lane)) != 0) {
                return lane;
            }
        }
        return -1;
    }

    bool validate_membermask(std::uint32_t membermask, const std::array<bool, kLanes>& participant_mask) {
        if (membermask == 0) {
            set_convergence_trap("self_not_in_membermask", trap_pc_or_zero(), -1);
            return false;
        }
        const std::uint32_t participants = lane_mask_bits(participant_mask);
        if ((membermask & ~participants) != 0) {
            py::dict detail = convergence_detail("membermask_not_subset", trap_pc_or_zero(), -1);
            detail["membermask"] = membermask;
            detail["participant_mask"] = participants;
            set_trap("convergence", "membermask_not_subset", trap_pc_or_zero(), detail);
            return false;
        }
        return true;
    }

    void dissolve_barrier(int index) {
        bx_[index].participation_mask = 0;
        bx_[index].reconv_pc = 0;
        bx_[index].valid = false;
        barrier_status_[index] = BarrierStatus::Dissolved;
    }

    void reset_barrier(int index) {
        bx_[index].participation_mask = 0;
        bx_[index].reconv_pc = 0;
        bx_[index].valid = false;
        barrier_status_[index] = BarrierStatus::Unarmed;
    }

    bool barrier_ready(int index) const {
        const std::uint32_t participation = bx_[index].participation_mask;
        if (participation == 0) {
            return true;
        }
        for (int lane = 0; lane < kLanes; ++lane) {
            if ((participation & (std::uint32_t{1} << lane)) == 0) {
                continue;
            }
            const bool lane_blocked_here = active_mask_[lane] && lane_state_[lane] == "blocked" && blocked_on_[lane] == index;
            const bool lane_exited = !active_mask_[lane] || lane_state_[lane] == "exited";
            if (!lane_blocked_here && !lane_exited) {
                return false;
            }
        }
        return true;
    }

    bool try_fire_barriers() {
        bool fired = false;
        for (int index = 0; index < static_cast<int>(bx_.size()); ++index) {
            fired = try_fire_barrier(index, nullptr) || fired;
        }
        return fired;
    }

    bool try_fire_barrier(int index, std::array<int, kLanes>* next_pc) {
        if (barrier_status_[index] != BarrierStatus::Armed || !barrier_ready(index)) {
            return false;
        }
        const std::uint32_t participation = bx_[index].participation_mask;
        const std::uint32_t expected_resume = blocked_member_mask(index, participation);
        const int reconv_pc = bx_[index].reconv_pc;
        const int resume_pc = reconv_pc + 1;
        std::uint32_t resumed = 0;
        for (int lane = 0; lane < kLanes; ++lane) {
            if ((participation & (std::uint32_t{1} << lane)) == 0 || blocked_on_[lane] != index) {
                continue;
            }
            resumed |= std::uint32_t{1} << lane;
            lane_state_[lane] = "active";
            blocked_on_[lane] = -1;
            pc_[lane] = resume_pc;
            if (next_pc != nullptr) {
                (*next_pc)[lane] = resume_pc;
            }
        }
        if (debug_checks_ && resumed != expected_resume) {
            py::dict detail = convergence_detail("debug_bsync_resume_mismatch", trap_pc_or_zero(), index);
            detail["expected_mask"] = expected_resume;
            detail["resumed_mask"] = resumed;
            set_trap("convergence", "debug_bsync_resume_mismatch", trap_pc_or_zero(), detail);
            return false;
        }
        reset_barrier(index);
        return true;
    }

    std::uint32_t blocked_member_mask(int index, std::uint32_t participation) const {
        std::uint32_t mask = 0;
        for (int lane = 0; lane < kLanes; ++lane) {
            if ((participation & (std::uint32_t{1} << lane)) != 0 && blocked_on_[lane] == index) {
                mask |= std::uint32_t{1} << lane;
            }
        }
        return mask;
    }

    void exec_exit(const std::array<bool, kLanes>& mask) {
        for (int lane = 0; lane < kLanes; ++lane) {
            if (mask[lane]) {
                active_mask_[lane] = false;
                lane_state_[lane] = "exited";
                blocked_on_[lane] = -1;
                for (int index = 0; index < static_cast<int>(bx_.size()); ++index) {
                    if (barrier_status_[index] == BarrierStatus::Armed) {
                        bx_[index].participation_mask &= ~(std::uint32_t{1} << lane);
                        if (bx_[index].participation_mask == 0) {
                            dissolve_barrier(index);
                        }
                    }
                }
            }
        }
        try_fire_barriers();
    }

    void set_trap(std::string kind, std::string reason, int pc, py::dict detail) {
        trap_.kind = std::move(kind);
        trap_.reason = std::move(reason);
        trap_.pc = pc;
        trap_.detail = std::move(detail);
    }

    py::dict convergence_detail(const std::string& reason, int pc, int barrier_index) const {
        py::dict detail;
        detail["trap_reason"] = reason;
        detail["pc"] = pc;
        if (barrier_index >= 0) {
            detail["barrier_index"] = barrier_index;
        }
        return detail;
    }

    void set_convergence_trap(const std::string& reason, int pc, int barrier_index) {
        set_trap("convergence", reason, pc, convergence_detail(reason, pc, barrier_index));
    }

    std::vector<Instruction> program_;
    std::vector<std::array<std::uint32_t, kLanes>> vgpr_;
    std::map<std::string, std::array<bool, kLanes>> predicates_;
    std::array<bool, kLanes> active_mask_{};
    std::array<int, kLanes> pc_{};
    std::array<std::string, kLanes> lane_state_{};
    std::array<Barrier, 16> bx_{};
    std::array<int, kLanes> blocked_on_{};
    std::array<BarrierStatus, 16> barrier_status_{};
    SchedOrder order_ = SchedOrder::MinPcFirst;
    bool debug_checks_ = false;
    int issue_pc_ = -1;
    int round_robin_cursor_ = 0;
    std::uint64_t next_group_seq_ = 0;
    std::map<int, std::uint64_t> group_seq_;
    Counters counters_;
    Trap trap_;
};

py::list diff_value(const py::object& left, const py::object& right, const std::string& path) {
    py::list diffs;
    if (!py::type::of(left).is(py::type::of(right))) {
        py::dict diff;
        diff["path"] = path;
        diff["left"] = left;
        diff["right"] = right;
        diffs.append(diff);
        return diffs;
    }
    if (py::isinstance<py::dict>(left)) {
        py::dict left_dict = left.cast<py::dict>();
        py::dict right_dict = right.cast<py::dict>();
        std::vector<std::string> keys;
        for (auto item : left_dict) {
            keys.push_back(py::str(item.first).cast<std::string>());
        }
        for (auto item : right_dict) {
            std::string key = py::str(item.first).cast<std::string>();
            if (!left_dict.contains(py::str(key))) {
                keys.push_back(key);
            }
        }
        std::sort(keys.begin(), keys.end());
        keys.erase(std::unique(keys.begin(), keys.end()), keys.end());
        for (const auto& key : keys) {
            py::str py_key(key);
            py::object l = left_dict.contains(py_key) ? py::reinterpret_borrow<py::object>(left_dict[py_key]) : py::none();
            py::object r = right_dict.contains(py_key) ? py::reinterpret_borrow<py::object>(right_dict[py_key]) : py::none();
            py::list child = diff_value(l, r, path + "." + key);
            for (py::handle item : child) {
                diffs.append(item);
            }
        }
        return diffs;
    }
    if (py::isinstance<py::list>(left)) {
        py::list left_list = left.cast<py::list>();
        py::list right_list = right.cast<py::list>();
        std::size_t count = std::max(left_list.size(), right_list.size());
        for (std::size_t index = 0; index < count; ++index) {
            py::object l = index < left_list.size() ? py::reinterpret_borrow<py::object>(left_list[index]) : py::none();
            py::object r = index < right_list.size() ? py::reinterpret_borrow<py::object>(right_list[index]) : py::none();
            py::list child = diff_value(l, r, path + "[" + std::to_string(index) + "]");
            for (py::handle item : child) {
                diffs.append(item);
            }
        }
        return diffs;
    }
    if (!PyObject_RichCompareBool(left.ptr(), right.ptr(), Py_EQ)) {
        py::dict diff;
        diff["path"] = path;
        diff["left"] = left;
        diff["right"] = right;
        diffs.append(diff);
    }
    return diffs;
}

}  // namespace

PYBIND11_MODULE(_native, module) {
    py::class_<NativeWarp>(module, "NativeWarp")
        .def("step", &NativeWarp::step)
        .def("snapshot", &NativeWarp::snapshot);

    module.def("launch", [](const py::object& program, int num_gprs, const std::string& sched_order, bool debug_checks) {
        ++g_boundary_calls;
        return NativeWarp(program, num_gprs, parse_sched_order(sched_order), debug_checks);
    }, py::arg("program"), py::arg("num_gprs") = kDefaultGprs, py::arg("sched_order") = "min_pc_first", py::arg("debug_checks") = false);
    module.def("launch_words", [](const py::object& words, int num_gprs, const std::string& sched_order, bool debug_checks) {
        ++g_boundary_calls;
        return NativeWarp(adapt_words(words), num_gprs, parse_sched_order(sched_order), debug_checks);
    }, py::arg("words"), py::arg("num_gprs") = kDefaultGprs, py::arg("sched_order") = "min_pc_first", py::arg("debug_checks") = false);
    module.def("step", [](NativeWarp& warp, int max_steps) {
        return warp.step(max_steps);
    });
    module.def("state_diff", [](const py::object& left, const py::object& right) {
        ++g_boundary_calls;
        return diff_value(left, right, "$");
    });
    module.def("boundary_calls", []() { return g_boundary_calls; });
    module.def("reset_boundary_calls", []() { g_boundary_calls = 0; });
}
