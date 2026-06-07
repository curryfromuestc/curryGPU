#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <algorithm>
#include <array>
#include <cstdint>
#include <map>
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
    explicit NativeWarp(const py::object& program, int num_gprs = kDefaultGprs)
        : program_(adapt_program(program)),
          vgpr_(num_gprs, std::array<std::uint32_t, kLanes>{}) {
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
    }

    explicit NativeWarp(std::vector<Instruction> program, int num_gprs = kDefaultGprs)
        : program_(std::move(program)),
          vgpr_(num_gprs, std::array<std::uint32_t, kLanes>{}) {
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
    }

    py::dict step(int max_steps) {
        ++g_boundary_calls;
        if (max_steps < 0) {
            throw std::invalid_argument("max_steps must be non-negative");
        }
        int issued = 0;
        while (!done() && trap_.kind == "none") {
            if (issued >= max_steps) {
                set_trap("max_steps", "budget_exhausted", current_pc_or_zero(), py::dict());
                break;
            }
            int pc = current_pc();
            if (pc < 0) {
                set_trap("execute", "non_uniform_pc", -1, py::dict());
                break;
            }
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
                lane_mask = guard_mask(inst.guard);
            } catch (const std::exception& exc) {
                py::dict detail;
                detail["message"] = exc.what();
                set_trap("decode", "decode_failure", pc, detail);
                break;
            }
            std::array<bool, kLanes> issued_mask = active_mask_;
            counters_.warp_instructions += 1;
            counters_.instructions += static_cast<std::uint64_t>(std::count(lane_mask.begin(), lane_mask.end(), true));
            std::array<int, kLanes> next_pc = fallthrough();
            try {
                if (inst.op == "IADD3") {
                    exec_iadd3(inst, lane_mask);
                } else if (inst.op == "ISETP") {
                    exec_isetp(inst, lane_mask);
                } else if (inst.op == "LOP3") {
                    exec_lop3(inst, lane_mask);
                } else if (inst.op == "BRA") {
                    next_pc = exec_bra(inst, lane_mask);
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
            if (trap_.kind != "none") {
                break;
            }
            for (int lane = 0; lane < kLanes; ++lane) {
                if (issued_mask[lane]) {
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
            barrier["participation_mask"] = 0;
            barrier["reconv_pc"] = 0;
            barrier["valid"] = false;
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

    int current_pc() const {
        int value = -1;
        for (int lane = 0; lane < kLanes; ++lane) {
            if (!active_mask_[lane]) {
                continue;
            }
            if (value < 0) {
                value = pc_[lane];
            } else if (value != pc_[lane]) {
                return -1;
            }
        }
        return value < 0 ? 0 : value;
    }

    int current_pc_or_zero() const {
        int pc = current_pc();
        return pc < 0 ? 0 : pc;
    }

    std::array<bool, kLanes> guard_mask(const Guard& guard) const {
        auto iter = predicates_.find(guard.pred);
        if (iter == predicates_.end()) {
            throw std::invalid_argument("invalid predicate: " + guard.pred);
        }
        std::array<bool, kLanes> result{};
        for (int lane = 0; lane < kLanes; ++lane) {
            bool pred = iter->second[lane];
            result[lane] = active_mask_[lane] && (guard.negate ? !pred : pred);
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
            set_trap("decode", "invalid_lop3_lut", current_pc_or_zero(), detail);
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
            set_trap("execute", "illegal_branch_target", current_pc_or_zero(), detail);
            return fallthrough();
        }
        std::array<int, kLanes> next = fallthrough();
        for (int lane = 0; lane < kLanes; ++lane) {
            if (mask[lane]) {
                next[lane] = target;
            }
        }
        int seen = -1;
        for (int lane = 0; lane < kLanes; ++lane) {
            if (!active_mask_[lane]) {
                continue;
            }
            if (seen < 0) {
                seen = next[lane];
            } else if (seen != next[lane]) {
                set_trap("execute", "non_uniform_branch", current_pc_or_zero(), py::dict());
                break;
            }
        }
        return next;
    }

    void exec_exit(const std::array<bool, kLanes>& mask) {
        for (int lane = 0; lane < kLanes; ++lane) {
            if (mask[lane]) {
                active_mask_[lane] = false;
                lane_state_[lane] = "exited";
            }
        }
    }

    void set_trap(std::string kind, std::string reason, int pc, py::dict detail) {
        trap_.kind = std::move(kind);
        trap_.reason = std::move(reason);
        trap_.pc = pc;
        trap_.detail = std::move(detail);
    }

    std::vector<Instruction> program_;
    std::vector<std::array<std::uint32_t, kLanes>> vgpr_;
    std::map<std::string, std::array<bool, kLanes>> predicates_;
    std::array<bool, kLanes> active_mask_{};
    std::array<int, kLanes> pc_{};
    std::array<std::string, kLanes> lane_state_{};
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

    module.def("launch", [](const py::object& program, int num_gprs) {
        ++g_boundary_calls;
        return NativeWarp(program, num_gprs);
    }, py::arg("program"), py::arg("num_gprs") = kDefaultGprs);
    module.def("launch_words", [](const py::object& words, int num_gprs) {
        ++g_boundary_calls;
        return NativeWarp(adapt_words(words), num_gprs);
    }, py::arg("words"), py::arg("num_gprs") = kDefaultGprs);
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
