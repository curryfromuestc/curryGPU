#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <algorithm>
#include <array>
#include <bit>
#include <cstdint>
#include <cstdio>
#include <map>
#include <memory>
#include <set>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "decoded_inst.gen.h"
#include "atomic_ops.h"
#include "cta_barrier.h"
#include "memory_space.h"
#include "race_shadow.h"

namespace py = pybind11;

namespace {

constexpr int kLanes = 32;
constexpr int kDefaultGprs = 256;
constexpr std::uint32_t kMask32 = 0xFFFFFFFFu;
constexpr std::uint64_t kSharedWindowBase = 0x1000000000000000ull;
constexpr std::uint64_t kLocalWindowBase = 0x2000000000000000ull;

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

struct GlobalAllocation {
    std::uint64_t base = 0;
    std::uint64_t size = 0;
};

struct LaunchConfig {
    int num_warps = 1;
    std::string warp_sched_order = "warp_round_robin";
    std::array<int, 3> ntid = {32, 1, 1};
    std::array<int, 3> nctaid = {1, 1, 1};
    std::array<int, 3> ctaid = {0, 0, 0};
    std::uint64_t shared_mem_bytes = 49152;
    std::uint64_t local_mem_bytes = 16384;
    std::vector<GlobalAllocation> global_allocations;
    std::map<int, std::vector<std::uint8_t>> const_banks;
    bool race_check = false;
};

enum class SchedOrder {
    MinPcFirst,
    MaxPcFirst,
    RoundRobin,
    OldestGroupFirst,
};

enum class WarpSchedOrder {
    RoundRobin,
    MinIdFirst,
    MaxIdFirst,
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

std::array<int, 3> parse_dim3(const py::object& value, const char* name) {
    if (value.is_none()) {
        return {0, 0, 0};
    }
    py::sequence seq = value.cast<py::sequence>();
    if (seq.size() != 3) {
        throw std::invalid_argument(std::string(name) + " must be a 3-tuple");
    }
    std::array<int, 3> result{};
    for (int index = 0; index < 3; ++index) {
        result[index] = py::reinterpret_borrow<py::object>(seq[index]).cast<int>();
        if (result[index] <= 0) {
            throw std::invalid_argument(std::string(name) + " dimensions must be positive");
        }
    }
    return result;
}

int dim_product(const std::array<int, 3>& dim) {
    return dim[0] * dim[1] * dim[2];
}

py::dict serialize_memory_space(const currygpu::iss::memory_space_impl& memory) {
    py::dict out;
    for (const auto& [block_id, bytes] : memory.snapshot_blocks()) {
        py::list data;
        for (std::uint8_t byte : bytes) {
            data.append(byte);
        }
        char key[32];
        std::snprintf(key, sizeof(key), "0x%llx", static_cast<unsigned long long>(block_id));
        out[py::str(key)] = data;
    }
    return out;
}

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
        if (text == "RZ") {
            return 255;
        }
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
    if (py::isinstance<py::int_>(value)) {
        const int selector = value.cast<int>();
        static const std::array<const char*, 15> names = {
            "SR_LANEID",
            "SR_TID.X",
            "SR_TID.Y",
            "SR_TID.Z",
            "SR_NTID.X",
            "SR_NTID.Y",
            "SR_NTID.Z",
            "SR_CTAID.X",
            "SR_CTAID.Y",
            "SR_CTAID.Z",
            "SR_NCTAID.X",
            "SR_NCTAID.Y",
            "SR_NCTAID.Z",
            "SR_WARPID",
            "SR_NWARPID",
        };
        if (selector >= 0 && selector < static_cast<int>(names.size())) {
            return names[selector];
        }
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

std::string sreg_symbol(std::int64_t value) {
    return sreg_name(py::int_(value));
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

WarpSchedOrder parse_warp_sched_order(const std::string& value) {
    if (value == "warp_round_robin") {
        return WarpSchedOrder::RoundRobin;
    }
    if (value == "warp_min_id_first") {
        return WarpSchedOrder::MinIdFirst;
    }
    if (value == "warp_max_id_first") {
        return WarpSchedOrder::MaxIdFirst;
    }
    throw std::invalid_argument("unknown warp_sched_order: " + value);
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
        if (data.contains("modifiers")) {
            py::dict modifiers = py::reinterpret_borrow<py::object>(data["modifiers"]).cast<py::dict>();
            for (auto item : modifiers) {
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

std::string width_symbol(std::int64_t value) {
    static const std::array<const char*, 8> names = {"u8", "s8", "u16", "s16", "32", "64", "128", "256"};
    if (value < 0 || value >= static_cast<std::int64_t>(names.size())) {
        throw std::invalid_argument("decoded memory width is invalid");
    }
    return names[static_cast<std::size_t>(value)];
}

py::dict address_operand(const std::map<std::string, std::int64_t>& fields) {
    py::dict addr;
    addr["base"] = py::str(reg_name(fields.at("addr_base")));
    addr["ur"] = fields.at("addr_ur") == 255 ? py::str("URZ") : py::str("UR" + std::to_string(fields.at("addr_ur")));
    addr["imm"] = py::int_(fields.at("addr_imm"));
    return addr;
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
        if (fields["sr"] < 0 || fields["sr"] >= 15) {
            inst.decode_ok = false;
            inst.decode_trap = "sreg";
            return inst;
        }
        inst.operands["rd"] = py::str(reg_name(fields["rd"]));
        inst.operands["sr"] = py::str(sreg_symbol(fields["sr"]));
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
    } else if (inst.op == "LDG" || inst.op == "LDS" || inst.op == "LDL" || inst.op == "LD") {
        inst.operands["rd"] = py::str(reg_name(fields["rd"]));
        inst.operands["addr"] = address_operand(fields);
        inst.operands["width"] = py::str(width_symbol(fields["width"]));
    } else if (inst.op == "STG" || inst.op == "STS" || inst.op == "STL" || inst.op == "ST") {
        inst.operands["src"] = py::str(reg_name(fields["src"]));
        inst.operands["addr"] = address_operand(fields);
        inst.operands["width"] = py::str(width_symbol(fields["width"]));
    } else if (inst.op == "LDC") {
        inst.operands["rd"] = py::str(reg_name(fields["rd"]));
        inst.operands["bank"] = py::int_(fields["bank"]);
        inst.operands["addr"] = address_operand(fields);
        inst.operands["width"] = py::str(width_symbol(fields["width"]));
    } else if (inst.op == "ATOM" || inst.op == "ATOMG" || inst.op == "ATOMS" || inst.op == "RED" || inst.op == "REDG" || inst.op == "REDS") {
        static const std::array<const char*, 10> op_names = {"ADD", "MIN", "MAX", "INC", "DEC", "AND", "OR", "XOR", "EXCH", "CAS"};
        std::int64_t op = fields["op"];
        if (op < 0 || op >= static_cast<std::int64_t>(op_names.size())) {
            inst.decode_ok = false;
            inst.decode_trap = "modifier";
            return inst;
        }
        if (inst.op == "ATOM" || inst.op == "ATOMG" || inst.op == "ATOMS") {
            inst.operands["rd"] = py::str(reg_name(fields["rd"]));
        }
        inst.operands["src"] = py::str(reg_name(fields["src"]));
        inst.operands["cmp"] = py::str(reg_name(fields["cmp"]));
        inst.operands["addr"] = address_operand(fields);
        inst.operands["op"] = py::str(op_names[op]);
    } else if (inst.op == "BAR") {
        static const std::array<const char*, 2> mode_names = {"SYNC", "ARV"};
        std::int64_t mode = fields["mode"];
        if (mode < 0 || mode >= static_cast<std::int64_t>(mode_names.size())) {
            inst.decode_ok = false;
            inst.decode_trap = "modifier";
            return inst;
        }
        inst.operands["bar"] = py::str("B" + std::to_string(fields["bar"]));
        inst.operands["count"] = py::int_(fields["count"]);
        inst.operands["mode"] = py::str(mode_names[mode]);
    } else if (inst.op == "MEMBAR" || inst.op == "FENCE") {
    } else if (inst.op == "CVTA") {
        static const std::array<const char*, 6> direction_names = {"TO_GLOBAL", "TO_SHARED", "TO_LOCAL", "FROM_GLOBAL", "FROM_SHARED", "FROM_LOCAL"};
        std::int64_t direction = fields["direction"];
        if (direction < 0 || direction >= static_cast<std::int64_t>(direction_names.size())) {
            inst.decode_ok = false;
            inst.decode_trap = "modifier";
            return inst;
        }
        inst.operands["rd"] = py::str(reg_name(fields["rd"]));
        inst.operands["src"] = py::str(reg_name(fields["src"]));
        inst.operands["direction"] = py::str(direction_names[direction]);
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

LaunchConfig parse_launch_config(int num_warps,
                                 const std::string& warp_sched_order,
                                 const py::object& ntid,
                                 const py::object& nctaid,
                                 std::uint64_t shared_mem_bytes,
                                 std::uint64_t local_mem_bytes,
                                 const py::object& global_allocations,
                                 bool race_check) {
    LaunchConfig config;
    if (num_warps <= 0) {
        throw std::invalid_argument("num_warps must be positive");
    }
    config.num_warps = num_warps;
    config.warp_sched_order = warp_sched_order;
    if (config.warp_sched_order != "warp_round_robin" && config.warp_sched_order != "warp_min_id_first" && config.warp_sched_order != "warp_max_id_first") {
        throw std::invalid_argument("unknown warp_sched_order: " + warp_sched_order);
    }
    config.ntid = ntid.is_none() ? std::array<int, 3>{32 * num_warps, 1, 1} : parse_dim3(ntid, "ntid");
    config.nctaid = nctaid.is_none() ? std::array<int, 3>{1, 1, 1} : parse_dim3(nctaid, "nctaid");
    if (dim_product(config.ntid) != num_warps * kLanes) {
        throw std::invalid_argument("prod(ntid) must equal num_warps * 32");
    }
    config.shared_mem_bytes = shared_mem_bytes;
    config.local_mem_bytes = local_mem_bytes;
    config.race_check = race_check;
    if (global_allocations.is_none()) {
        return config;
    }
    for (py::handle item : global_allocations) {
        std::uint64_t base = 0;
        std::uint64_t size = 0;
        py::object object = py::reinterpret_borrow<py::object>(item);
        if (py::isinstance<py::dict>(object)) {
            py::dict data = object.cast<py::dict>();
            base = py::reinterpret_borrow<py::object>(data["base"]).cast<std::uint64_t>();
            size = py::reinterpret_borrow<py::object>(data["size"]).cast<std::uint64_t>();
        } else {
            py::sequence pair = object.cast<py::sequence>();
            if (pair.size() != 2) {
                throw std::invalid_argument("global allocation must be a (base, size) pair");
            }
            base = py::reinterpret_borrow<py::object>(pair[0]).cast<std::uint64_t>();
            size = py::reinterpret_borrow<py::object>(pair[1]).cast<std::uint64_t>();
        }
        if (size == 0 || base + size < base) {
            throw std::invalid_argument("global allocation must have a nonzero non-overflowing size");
        }
        config.global_allocations.push_back(GlobalAllocation{base, size});
    }
    std::sort(config.global_allocations.begin(), config.global_allocations.end(), [](const GlobalAllocation& left, const GlobalAllocation& right) {
        return left.base < right.base;
    });
    for (std::size_t index = 1; index < config.global_allocations.size(); ++index) {
        const auto& previous = config.global_allocations[index - 1];
        const auto& current = config.global_allocations[index];
        if (current.base < previous.base + previous.size) {
            throw std::invalid_argument("global allocation ranges must not overlap");
        }
    }
    return config;
}

void parse_const_banks_into(LaunchConfig& config, const py::object& const_banks) {
    if (const_banks.is_none()) {
        return;
    }
    py::dict banks = const_banks.cast<py::dict>();
    for (auto item : banks) {
        const int bank = py::reinterpret_borrow<py::object>(item.first).cast<int>();
        if (bank < 0 || bank > 255) {
            throw std::invalid_argument("const bank index out of range");
        }
        py::object value = py::reinterpret_borrow<py::object>(item.second);
        std::vector<std::uint8_t> bytes;
        if (py::isinstance<py::bytes>(value)) {
            std::string raw = value.cast<std::string>();
            bytes.assign(raw.begin(), raw.end());
        } else if (py::isinstance<py::bytearray>(value)) {
            py::bytes raw_bytes(value);
            std::string raw = raw_bytes;
            bytes.assign(raw.begin(), raw.end());
        } else {
            for (py::handle byte : value) {
                const int parsed = py::reinterpret_borrow<py::object>(byte).cast<int>();
                if (parsed < 0 || parsed > 255) {
                    throw std::invalid_argument("const bank byte out of range");
                }
                bytes.push_back(static_cast<std::uint8_t>(parsed));
            }
        }
        config.const_banks[bank] = std::move(bytes);
    }
}

LaunchConfig parse_launch_config(int num_warps,
                                 const std::string& warp_sched_order,
                                 const py::object& ntid,
                                 const py::object& nctaid,
                                 std::uint64_t shared_mem_bytes,
                                 std::uint64_t local_mem_bytes,
                                 const py::object& global_allocations,
                                 const py::object& const_banks,
                                 bool race_check) {
    LaunchConfig config = parse_launch_config(num_warps, warp_sched_order, ntid, nctaid, shared_mem_bytes, local_mem_bytes, global_allocations, race_check);
    parse_const_banks_into(config, const_banks);
    return config;
}

class NativeWarp {
public:
    explicit NativeWarp(const py::object& program,
                        int num_gprs = kDefaultGprs,
                        SchedOrder order = SchedOrder::MinPcFirst,
                        bool debug_checks = false,
                        LaunchConfig config = {},
                        int warp_id = 0,
                        currygpu::iss::memory_space_impl* global_memory = nullptr,
                        currygpu::iss::memory_space_impl* shared_memory = nullptr,
                        currygpu::iss::race_shadow_state* race_shadow = nullptr)
        : program_(adapt_program(program)),
          vgpr_(num_gprs, std::array<std::uint32_t, kLanes>{}),
          order_(order),
          debug_checks_(debug_checks),
          config_(std::move(config)),
          warp_id_(warp_id),
          global_memory_external_(global_memory),
          shared_memory_external_(shared_memory),
          race_shadow_(race_shadow) {
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
        cta_blocked_on_.fill(-1);
        cta_blocked_resume_pc_.fill(0);
    }

    explicit NativeWarp(std::vector<Instruction> program,
                        int num_gprs = kDefaultGprs,
                        SchedOrder order = SchedOrder::MinPcFirst,
                        bool debug_checks = false,
                        LaunchConfig config = {},
                        int warp_id = 0,
                        currygpu::iss::memory_space_impl* global_memory = nullptr,
                        currygpu::iss::memory_space_impl* shared_memory = nullptr,
                        currygpu::iss::race_shadow_state* race_shadow = nullptr)
        : program_(std::move(program)),
          vgpr_(num_gprs, std::array<std::uint32_t, kLanes>{}),
          order_(order),
          debug_checks_(debug_checks),
          config_(std::move(config)),
          warp_id_(warp_id),
          global_memory_external_(global_memory),
          shared_memory_external_(shared_memory),
          race_shadow_(race_shadow) {
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
        cta_blocked_on_.fill(-1);
        cta_blocked_resume_pc_.fill(0);
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
        while (!done() && trap_.kind == "none") {
            if (issued >= max_steps) {
                set_trap("max_steps", "budget_exhausted", first_runnable_pc_or_zero(), py::dict());
                break;
            }
            if (!step_one_group()) {
                break;
            }
            ++issued;
        }
        return snapshot();
    }

    bool step_one_group() {
        pending_cta_barrier_action_ = currygpu::iss::cta_barrier_action{};
        if (done() || trap_.kind != "none") {
            return false;
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
            return false;
        }
        ExecutionGroup group = select_group(groups);
        int pc = group.pc;
        if (pc >= static_cast<int>(program_.size())) {
            set_trap("execute", "illegal_pc", pc, py::dict());
            return false;
        }
        const Instruction& inst = program_[pc];
        if (!inst.decode_ok) {
            py::dict detail;
            detail["trap"] = inst.decode_trap;
            set_trap("decode", inst.decode_trap.empty() ? "decode_failure" : inst.decode_trap, pc, detail);
            return false;
        }
        std::array<bool, kLanes> lane_mask;
        try {
            lane_mask = guard_mask(inst.guard, group.lanes);
        } catch (const std::exception& exc) {
            py::dict detail;
            detail["message"] = exc.what();
            set_trap("decode", "decode_failure", pc, detail);
            return false;
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
            } else if (inst.op == "LDG" || inst.op == "LDS" || inst.op == "LDL" || inst.op == "LD") {
                exec_load(inst, lane_mask);
            } else if (inst.op == "LDC") {
                exec_ldc(inst, lane_mask);
            } else if (inst.op == "STG" || inst.op == "STS" || inst.op == "STL" || inst.op == "ST") {
                exec_store(inst, lane_mask);
            } else if (inst.op == "ATOM" || inst.op == "ATOMG" || inst.op == "ATOMS" || inst.op == "RED" || inst.op == "REDG" || inst.op == "REDS") {
                exec_atomic(inst, lane_mask);
            } else if (inst.op == "BAR") {
                exec_bar(inst, lane_mask, next_pc);
            } else if (inst.op == "MEMBAR" || inst.op == "FENCE") {
                exec_fence_noop();
            } else if (inst.op == "CVTA") {
                exec_cvta(inst, lane_mask);
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
            return false;
        }
        if (splits_active_pc(issued_mask, next_pc)) {
            counters_.divergence_events += 1;
        }
        for (int lane = 0; lane < kLanes; ++lane) {
            if (issued_mask[lane] && lane_state_[lane] != "blocked") {
                pc_[lane] = next_pc[lane];
            }
        }
        return true;
    }
    bool is_done() const {
        return done() || trap_.kind != "none";
    }

    bool is_runnable() const {
        if (done() || trap_.kind != "none") {
            return false;
        }
        for (int lane = 0; lane < kLanes; ++lane) {
            if (active_mask_[lane] && lane_state_[lane] == "active" && cta_blocked_on_[lane] < 0) {
                return true;
            }
        }
        return false;
    }

    bool prepare_for_block_schedule() {
        if (done() || trap_.kind != "none") {
            return false;
        }
        if (is_runnable()) {
            return true;
        }
        try_fire_barriers();
        if (is_runnable()) {
            return true;
        }
        promote_yielded_lanes();
        return is_runnable();
    }

    bool detect_warp_deadlock() {
        if (done() || trap_.kind != "none") {
            return false;
        }
        if (!is_runnable() && has_blocked_lanes()) {
            set_convergence_trap("deadlock_no_progress", first_runnable_pc_or_zero(), -1);
            return true;
        }
        return false;
    }

    const Trap& trap() const {
        return trap_;
    }

    const currygpu::iss::cta_barrier_action& pending_cta_barrier_action() const {
        return pending_cta_barrier_action_;
    }

    void clear_pending_cta_barrier_action() {
        pending_cta_barrier_action_ = currygpu::iss::cta_barrier_action{};
    }

    int warp_id() const {
        return warp_id_;
    }

    void release_cta_barrier(int bar_id, const std::array<std::uint32_t, 32>& waiting_masks, const std::array<int, 32>& resume_pc) {
        if (warp_id_ < 0 || warp_id_ >= static_cast<int>(waiting_masks.size())) {
            return;
        }
        const std::uint32_t waiting = waiting_masks[warp_id_];
        for (int lane = 0; lane < kLanes; ++lane) {
            if ((waiting & (std::uint32_t{1} << lane)) == 0 || cta_blocked_on_[lane] != bar_id) {
                continue;
            }
            cta_blocked_on_[lane] = -1;
            cta_blocked_resume_pc_[lane] = 0;
            pc_[lane] = resume_pc[warp_id_];
        }
    }

    std::uint32_t active_lane_mask() const {
        std::uint32_t mask = 0;
        for (int lane = 0; lane < kLanes; ++lane) {
            if (active_mask_[lane]) {
                mask |= std::uint32_t{1} << lane;
            }
        }
        return mask;
    }

    bool is_cta_blocked() const {
        return has_cta_blocked_lanes();
    }

    py::dict snapshot() const {
        py::dict out;
        py::list active;
        py::list pc;
        py::list lane_state;
        for (int lane = 0; lane < kLanes; ++lane) {
            active.append(active_mask_[lane]);
            pc.append(pc_[lane]);
            lane_state.append(cta_blocked_on_[lane] >= 0 ? py::str("cta_blocked") : py::str(lane_state_[lane]));
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
        memory["global"] = serialize_memory_space(global_memory());
        memory["shared"] = serialize_memory_space(shared_memory());
        py::dict local;
        for (int lane = 0; lane < kLanes; ++lane) {
            py::dict lane_memory = serialize_memory_space(local_memory_[lane]);
            if (lane_memory.size() != 0) {
                local[py::str(std::to_string(lane))] = lane_memory;
            }
        }
        memory["local"] = local;
        out["memory"] = memory;

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

        py::list cta_barriers;
        for (int index = 0; index < 16; ++index) {
            py::dict barrier;
            barrier["phase"] = "inactive";
            barrier["arrived_count"] = 0;
            barrier["expected_count"] = 0;
            barrier["phase_parity"] = 0;
            cta_barriers.append(barrier);
        }
        out["cta_barriers"] = cta_barriers;
        return out;
    }

private:
    bool done() const {
        return std::none_of(active_mask_.begin(), active_mask_.end(), [](bool active) { return active; });
    }

    bool has_cta_blocked_lanes() const {
        for (int lane = 0; lane < kLanes; ++lane) {
            if (active_mask_[lane] && cta_blocked_on_[lane] >= 0) {
                return true;
            }
        }
        return false;
    }

    bool has_blocked_lanes() const {
        for (int lane = 0; lane < kLanes; ++lane) {
            if (active_mask_[lane] && (lane_state_[lane] == "blocked" || cta_blocked_on_[lane] >= 0)) {
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
            if (!active_mask_[lane] || lane_state_[lane] != "active" || cta_blocked_on_[lane] >= 0) {
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

    int linear_thread_id(int lane) const {
        return warp_id_ * kLanes + lane;
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

    void write_gpr_index(int reg, int lane, std::uint32_t value) {
        if (reg == 255) {
            return;
        }
        if (reg < 0 || reg >= static_cast<int>(vgpr_.size())) {
            throw std::invalid_argument("GPR index out of range");
        }
        vgpr_[reg][lane] = value;
    }

    std::uint32_t read_gpr_index(int reg, int lane) const {
        if (reg == 255) {
            return 0;
        }
        if (reg < 0 || reg >= static_cast<int>(vgpr_.size())) {
            throw std::invalid_argument("GPR index out of range");
        }
        return vgpr_[reg][lane];
    }

    std::uint64_t read_reg_pair(int reg, int lane) const {
        if (reg == 255) {
            return 0;
        }
        if (reg % 2 != 0 || reg + 1 >= static_cast<int>(vgpr_.size())) {
            throw std::invalid_argument("64-bit register pair must be even aligned");
        }
        return static_cast<std::uint64_t>(read_gpr_index(reg, lane)) | (static_cast<std::uint64_t>(read_gpr_index(reg + 1, lane)) << 32);
    }

    void write_reg_pair(int reg, int lane, std::uint64_t value) {
        if (reg == 255) {
            return;
        }
        if (reg % 2 != 0 || reg + 1 >= static_cast<int>(vgpr_.size())) {
            throw std::invalid_argument("64-bit register pair must be even aligned");
        }
        write_gpr_index(reg, lane, static_cast<std::uint32_t>(value & 0xFFFFFFFFull));
        write_gpr_index(reg + 1, lane, static_cast<std::uint32_t>(value >> 32));
    }

    static std::string memory_width(const Instruction& inst) {
        return upper(operand(inst, {"width"}, py::str("32")).cast<std::string>());
    }

    static int memory_width_bytes(const std::string& width) {
        if (width == "U8" || width == "S8") {
            return 1;
        }
        if (width == "U16" || width == "S16") {
            return 2;
        }
        if (width == "32") {
            return 4;
        }
        if (width == "64") {
            return 8;
        }
        if (width == "128") {
            return 16;
        }
        if (width == "256") {
            return 32;
        }
        throw std::invalid_argument("unsupported memory width");
    }

    static bool memory_width_signed(const std::string& width) {
        return width == "S8" || width == "S16";
    }

    static int register_group_words(int byte_width) {
        if (byte_width <= 4) {
            return 1;
        }
        return byte_width / 4;
    }

    void validate_register_group(int reg, int words, const std::string& reason) {
        if (reg == 255) {
            return;
        }
        if (reg % words != 0) {
            py::dict detail = memory_detail(reason, trap_pc_or_zero(), 0, "register", words * 4, -1);
            detail["register"] = reg;
            set_trap("memory", reason, trap_pc_or_zero(), detail);
        }
    }

    std::uint64_t address_value(const py::object& addr, int lane) const {
        if (py::isinstance<py::dict>(addr)) {
            py::dict data = addr.cast<py::dict>();
            py::object base = py::reinterpret_borrow<py::object>(data["base"]);
            std::uint64_t value = read_word(base, lane);
            if (py::isinstance<py::str>(base)) {
                const int base_reg = reg_index(base);
                if (base_reg != 255 && base_reg % 2 == 0 && base_reg + 1 < static_cast<int>(vgpr_.size())) {
                    value |= static_cast<std::uint64_t>(read_gpr_index(base_reg + 1, lane)) << 32;
                }
            }
            if (data.contains("imm")) {
                value = static_cast<std::uint64_t>(static_cast<std::int64_t>(value) + py::reinterpret_borrow<py::object>(data["imm"]).cast<std::int64_t>());
            }
            return value;
        }
        return read_word(addr, lane);
    }

    py::dict memory_detail(const std::string& reason, int pc, std::uint64_t address, const std::string& space, int width, int lane) const {
        py::dict detail;
        detail["trap_reason"] = reason;
        detail["pc"] = pc;
        detail["address"] = py::int_(address);
        detail["space"] = space;
        detail["width"] = width;
        if (lane >= 0) {
            detail["thread_id"] = lane;
        }
        return detail;
    }

    py::dict race_detail(const currygpu::iss::race_conflict& conflict) const {
        py::dict detail;
        detail["trap_reason"] = "data_race";
        detail["pc"] = conflict.pc;
        detail["address"] = py::int_(conflict.address);
        detail["space"] = conflict.space;
        py::list racing_lanes;
        racing_lanes.append(conflict.thread_ids[0]);
        racing_lanes.append(conflict.thread_ids[1]);
        detail["racing_lanes"] = racing_lanes;
        py::list access_kinds;
        access_kinds.append(currygpu::iss::race_access_kind_name(conflict.access_kinds[0]));
        access_kinds.append(currygpu::iss::race_access_kind_name(conflict.access_kinds[1]));
        detail["access_kinds"] = access_kinds;
        return detail;
    }

    bool validate_memory_access(std::uint64_t address, const std::string& space, int byte_width, int lane, bool atomic = false) {
        const std::string misaligned_reason = atomic ? "atomic_misaligned" : "misaligned_address";
        if (byte_width > 1 && address % static_cast<std::uint64_t>(byte_width) != 0) {
            set_trap("memory", misaligned_reason, trap_pc_or_zero(), memory_detail(misaligned_reason, trap_pc_or_zero(), address, space, byte_width, lane));
            return false;
        }
        if (space == "shared" && address + static_cast<std::uint64_t>(byte_width) > config_.shared_mem_bytes) {
            py::dict detail = memory_detail("shared_oob", trap_pc_or_zero(), address, space, byte_width, lane);
            detail["bound"] = py::int_(config_.shared_mem_bytes);
            set_trap("memory", "shared_oob", trap_pc_or_zero(), detail);
            return false;
        }
        if (space == "local" && address + static_cast<std::uint64_t>(byte_width) > config_.local_mem_bytes) {
            py::dict detail = memory_detail("local_oob", trap_pc_or_zero(), address, space, byte_width, lane);
            detail["bound"] = py::int_(config_.local_mem_bytes);
            set_trap("memory", "local_oob", trap_pc_or_zero(), detail);
            return false;
        }
        if (space == "global" && !config_.global_allocations.empty() && !global_in_bounds(address, byte_width)) {
            set_trap("memory", "global_oob", trap_pc_or_zero(), memory_detail("global_oob", trap_pc_or_zero(), address, space, byte_width, lane));
            return false;
        }
        return true;
    }

    bool global_in_bounds(std::uint64_t address, int byte_width) const {
        const std::uint64_t end = address + static_cast<std::uint64_t>(byte_width);
        if (end < address) {
            return false;
        }
        for (const GlobalAllocation& allocation : config_.global_allocations) {
            const std::uint64_t alloc_end = allocation.base + allocation.size;
            if (address >= allocation.base && end <= alloc_end) {
                return true;
            }
        }
        return false;
    }

    currygpu::iss::memory_space_impl& memory_for_space(const std::string& space, int lane) {
        if (space == "global") {
            return global_memory();
        }
        if (space == "shared") {
            return shared_memory();
        }
        if (space == "local") {
            return local_memory_[lane];
        }
        throw std::invalid_argument("unsupported memory space");
    }

    std::string direct_space_for_op(const std::string& op) const {
        if (op == "LDG" || op == "STG") {
            return "global";
        }
        if (op == "LDS" || op == "STS") {
            return "shared";
        }
        if (op == "LDL" || op == "STL") {
            return "local";
        }
        return "generic";
    }

    std::pair<std::string, std::uint64_t> resolve_memory_address(const Instruction& inst, int lane) {
        const std::string op = inst.op;
        std::string space = direct_space_for_op(op);
        py::object addr = operand(inst, {"addr", "address"});
        std::uint64_t address = address_value(addr, lane);
        if (space == "generic") {
            if (address >= kSharedWindowBase && address < kSharedWindowBase + config_.shared_mem_bytes) {
                return {"shared", address - kSharedWindowBase};
            }
            if (address >= kLocalWindowBase && address < kLocalWindowBase + config_.local_mem_bytes) {
                return {"local", address - kLocalWindowBase};
            }
            return {"global", address};
        }
        return {space, address};
    }

    std::pair<std::string, std::uint64_t> resolve_atomic_address(const Instruction& inst, int lane) {
        std::string space = "generic";
        if (inst.op == "ATOMG" || inst.op == "REDG") {
            space = "global";
        } else if (inst.op == "ATOMS" || inst.op == "REDS") {
            space = "shared";
        }
        py::object addr = operand(inst, {"addr", "address"});
        std::uint64_t address = address_value(addr, lane);
        if (space == "generic") {
            if (address >= kSharedWindowBase && address < kSharedWindowBase + config_.shared_mem_bytes) {
                return {"shared", address - kSharedWindowBase};
            }
            if (address >= kLocalWindowBase && address < kLocalWindowBase + config_.local_mem_bytes) {
                return {"local", address - kLocalWindowBase};
            }
            return {"global", address};
        }
        return {space, address};
    }

    std::uint64_t load_little(const currygpu::iss::memory_space_impl& memory, std::uint64_t address, int byte_width) const {
        std::uint64_t value = 0;
        for (int byte = 0; byte < byte_width; ++byte) {
            value |= static_cast<std::uint64_t>(memory.read_byte(address + byte)) << (byte * 8);
        }
        return value;
    }

    void store_word_little(currygpu::iss::memory_space_impl& memory, std::uint64_t address, std::uint32_t value, int byte_width) {
        for (int byte = 0; byte < byte_width; ++byte) {
            memory.write_byte(address + byte, static_cast<std::uint8_t>((value >> (byte * 8)) & 0xFFu));
        }
    }

    bool record_race_access(const std::string& space, std::uint64_t address, int byte_width, int lane, currygpu::iss::race_access_kind kind) {
        if (race_shadow_ == nullptr) {
            return true;
        }
        currygpu::iss::race_conflict conflict = race_shadow_->record(space, address, byte_width, linear_thread_id(lane), kind, trap_pc_or_zero());
        if (conflict.found) {
            set_trap("memory", "data_race", trap_pc_or_zero(), race_detail(conflict));
            return false;
        }
        return true;
    }

    void exec_load(const Instruction& inst, const std::array<bool, kLanes>& mask) {
        const std::string width = memory_width(inst);
        const int byte_width = memory_width_bytes(width);
        const int words = register_group_words(byte_width);
        const int dst = reg_index(operand(inst, {"dst", "rd"}));
        validate_register_group(dst, words, "misaligned_address");
        if (trap_.kind != "none") {
            return;
        }
        for (int lane = 0; lane < kLanes; ++lane) {
            if (!mask[lane]) {
                continue;
            }
            auto [space, address] = resolve_memory_address(inst, lane);
            if (!validate_memory_access(address, space, byte_width, lane)) {
                return;
            }
            if (!record_race_access(space, address, byte_width, lane, currygpu::iss::race_access_kind::Read)) {
                return;
            }
            const auto& memory = memory_for_space(space, lane);
            if (byte_width <= 4) {
                std::uint64_t raw = load_little(memory, address, byte_width);
                if (memory_width_signed(width)) {
                    if (byte_width == 1 && (raw & 0x80u) != 0) {
                        raw |= 0xFFFFFF00ull;
                    } else if (byte_width == 2 && (raw & 0x8000u) != 0) {
                        raw |= 0xFFFF0000ull;
                    }
                }
                write_gpr_index(dst, lane, static_cast<std::uint32_t>(raw));
            } else {
                for (int word = 0; word < words; ++word) {
                    std::uint32_t value = static_cast<std::uint32_t>(load_little(memory, address + static_cast<std::uint64_t>(word * 4), 4));
                    write_gpr_index(dst + word, lane, value);
                }
            }
            counters_.mem_ops += 1;
        }
    }

    void exec_store(const Instruction& inst, const std::array<bool, kLanes>& mask) {
        const std::string width = memory_width(inst);
        const int byte_width = memory_width_bytes(width);
        const int words = register_group_words(byte_width);
        const int src = reg_index(operand(inst, {"src", "rs"}));
        validate_register_group(src, words, "misaligned_address");
        if (trap_.kind != "none") {
            return;
        }
        for (int lane = 0; lane < kLanes; ++lane) {
            if (!mask[lane]) {
                continue;
            }
            auto [space, address] = resolve_memory_address(inst, lane);
            if (!validate_memory_access(address, space, byte_width, lane)) {
                return;
            }
            if (!record_race_access(space, address, byte_width, lane, currygpu::iss::race_access_kind::Write)) {
                return;
            }
            auto& memory = memory_for_space(space, lane);
            if (byte_width <= 4) {
                store_word_little(memory, address, read_gpr_index(src, lane), byte_width);
            } else {
                for (int word = 0; word < words; ++word) {
                    store_word_little(memory, address + static_cast<std::uint64_t>(word * 4), read_gpr_index(src + word, lane), 4);
                }
            }
            counters_.mem_ops += 1;
        }
    }

    void exec_atomic(const Instruction& inst, const std::array<bool, kLanes>& mask) {
        const std::string atomic_op = upper(operand(inst, {"op"}, py::str("ADD")).cast<std::string>());
        if (!currygpu::iss::is_supported_atomic_op(atomic_op)) {
            py::dict detail = memory_detail("atomic_unsupported_op", trap_pc_or_zero(), 0, "atomic", 4, -1);
            detail["op"] = atomic_op;
            set_trap("memory", "atomic_unsupported_op", trap_pc_or_zero(), detail);
            return;
        }
        const bool is_red = inst.op == "RED" || inst.op == "REDG" || inst.op == "REDS";
        if (is_red) {
            auto rd_iter = inst.operands.find("rd");
            if (rd_iter == inst.operands.end()) {
                rd_iter = inst.operands.find("dst");
            }
            if (rd_iter != inst.operands.end() && reg_index(rd_iter->second) != 255) {
                py::dict detail = memory_detail("red_has_destination", trap_pc_or_zero(), 0, "atomic", 4, -1);
                detail["op"] = atomic_op;
                set_trap("memory", "red_has_destination", trap_pc_or_zero(), detail);
                return;
            }
        }
        const int rd = is_red ? 255 : reg_index(operand(inst, {"rd", "dst"}, py::str("RZ")));
        const int src = reg_index(operand(inst, {"src", "rs"}));
        const int cmp = reg_index(operand(inst, {"cmp"}, py::str("RZ")));
        for (int lane = 0; lane < kLanes; ++lane) {
            if (!mask[lane]) {
                continue;
            }
            auto [space, address] = resolve_atomic_address(inst, lane);
            if (space == "local") {
                py::dict detail = memory_detail("atomic_on_local_unsupported", trap_pc_or_zero(), address, space, 4, lane);
                detail["op"] = atomic_op;
                set_trap("memory", "atomic_on_local_unsupported", trap_pc_or_zero(), detail);
                return;
            }
            if (!validate_memory_access(address, space, 4, lane, true)) {
                return;
            }
            auto& memory = memory_for_space(space, lane);
            const std::uint32_t old_value = static_cast<std::uint32_t>(load_little(memory, address, 4));
            const currygpu::iss::atomic_rmw_result result = currygpu::iss::apply_atomic_rmw(
                atomic_op,
                old_value,
                read_gpr_index(src, lane),
                read_gpr_index(cmp, lane));
            store_word_little(memory, address, result.new_value, 4);
            if (!is_red) {
                write_gpr_index(rd, lane, result.old_value);
            }
            counters_.mem_ops += 1;
        }
    }

    void exec_ldc(const Instruction& inst, const std::array<bool, kLanes>& mask) {
        const std::string width = memory_width(inst);
        const int byte_width = memory_width_bytes(width);
        const int words = register_group_words(byte_width);
        const int dst = reg_index(operand(inst, {"dst", "rd"}));
        validate_register_group(dst, words, "misaligned_address");
        if (trap_.kind != "none") {
            return;
        }
        const int bank = operand(inst, {"bank"}).cast<int>();
        const auto bank_iter = config_.const_banks.find(bank);
        for (int lane = 0; lane < kLanes; ++lane) {
            if (!mask[lane]) {
                continue;
            }
            const std::uint64_t address = address_value(operand(inst, {"addr", "address"}), lane);
            if (byte_width > 1 && address % static_cast<std::uint64_t>(byte_width) != 0) {
                set_trap("memory", "misaligned_address", trap_pc_or_zero(), memory_detail("misaligned_address", trap_pc_or_zero(), address, "const", byte_width, lane));
                return;
            }
            if (bank_iter == config_.const_banks.end() || address + static_cast<std::uint64_t>(byte_width) > bank_iter->second.size()) {
                py::dict detail = memory_detail("const_oob", trap_pc_or_zero(), address, "const", byte_width, lane);
                detail["bank"] = bank;
                set_trap("memory", "const_oob", trap_pc_or_zero(), detail);
                return;
            }
            if (byte_width <= 4) {
                std::uint64_t raw = 0;
                for (int byte = 0; byte < byte_width; ++byte) {
                    raw |= static_cast<std::uint64_t>(bank_iter->second[static_cast<std::size_t>(address) + byte]) << (byte * 8);
                }
                if (memory_width_signed(width)) {
                    if (byte_width == 1 && (raw & 0x80u) != 0) {
                        raw |= 0xFFFFFF00ull;
                    } else if (byte_width == 2 && (raw & 0x8000u) != 0) {
                        raw |= 0xFFFF0000ull;
                    }
                }
                write_gpr_index(dst, lane, static_cast<std::uint32_t>(raw));
            } else {
                for (int word = 0; word < words; ++word) {
                    std::uint32_t value = 0;
                    const std::uint64_t word_address = address + static_cast<std::uint64_t>(word * 4);
                    for (int byte = 0; byte < 4; ++byte) {
                        value |= static_cast<std::uint32_t>(bank_iter->second[static_cast<std::size_t>(word_address) + byte]) << (byte * 8);
                    }
                    write_gpr_index(dst + word, lane, value);
                }
            }
            counters_.mem_ops += 1;
        }
    }

    void exec_cvta(const Instruction& inst, const std::array<bool, kLanes>& mask) {
        const int dst = reg_index(operand(inst, {"dst", "rd"}));
        const int src = reg_index(operand(inst, {"src"}));
        const std::string direction = upper(operand(inst, {"direction"}, py::str("TO_GLOBAL")).cast<std::string>());
        for (int lane = 0; lane < kLanes; ++lane) {
            if (!mask[lane]) {
                continue;
            }
            std::uint64_t value = read_reg_pair(src, lane);
            if (direction == "TO_SHARED") {
                value += kSharedWindowBase;
            } else if (direction == "TO_LOCAL") {
                value += kLocalWindowBase;
            } else if (direction == "FROM_SHARED") {
                value -= kSharedWindowBase;
            } else if (direction == "FROM_LOCAL") {
                value -= kLocalWindowBase;
            } else if (direction == "TO_GLOBAL" || direction == "FROM_GLOBAL") {
            } else {
                throw std::invalid_argument("unsupported CVTA direction");
            }
            write_reg_pair(dst, lane, value);
        }
    }

    void exec_fence_noop() const {}

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
        py::object dst = operand(inst, {"dst", "rd"});
        for (int lane = 0; lane < kLanes; ++lane) {
            if (mask[lane]) {
                write_gpr(dst, lane, s2r_value(selector, lane));
            }
        }
    }

    std::uint32_t s2r_value(const std::string& selector, int lane) const {
        const int linear_tid = warp_id_ * kLanes + lane;
        const int ntid_x = config_.ntid[0];
        const int ntid_y = config_.ntid[1];
        if (selector == "SR_LANEID") {
            return static_cast<std::uint32_t>(lane);
        }
        if (selector == "SR_TID.X") {
            return static_cast<std::uint32_t>(linear_tid % ntid_x);
        }
        if (selector == "SR_TID.Y") {
            return static_cast<std::uint32_t>((linear_tid / ntid_x) % ntid_y);
        }
        if (selector == "SR_TID.Z") {
            return static_cast<std::uint32_t>(linear_tid / (ntid_x * ntid_y));
        }
        if (selector == "SR_NTID.X") {
            return static_cast<std::uint32_t>(config_.ntid[0]);
        }
        if (selector == "SR_NTID.Y") {
            return static_cast<std::uint32_t>(config_.ntid[1]);
        }
        if (selector == "SR_NTID.Z") {
            return static_cast<std::uint32_t>(config_.ntid[2]);
        }
        if (selector == "SR_CTAID.X") {
            return static_cast<std::uint32_t>(config_.ctaid[0]);
        }
        if (selector == "SR_CTAID.Y") {
            return static_cast<std::uint32_t>(config_.ctaid[1]);
        }
        if (selector == "SR_CTAID.Z") {
            return static_cast<std::uint32_t>(config_.ctaid[2]);
        }
        if (selector == "SR_NCTAID.X") {
            return static_cast<std::uint32_t>(config_.nctaid[0]);
        }
        if (selector == "SR_NCTAID.Y") {
            return static_cast<std::uint32_t>(config_.nctaid[1]);
        }
        if (selector == "SR_NCTAID.Z") {
            return static_cast<std::uint32_t>(config_.nctaid[2]);
        }
        if (selector == "SR_WARPID") {
            return static_cast<std::uint32_t>(warp_id_);
        }
        if (selector == "SR_NWARPID") {
            return static_cast<std::uint32_t>(config_.num_warps);
        }
        throw std::invalid_argument("unsupported special register selector");
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

    void exec_bar(const Instruction& inst, const std::array<bool, kLanes>& mask, std::array<int, kLanes>& next_pc) {
        const int bar_id = barrier_index(operand(inst, {"bar", "barrier"}));
        if (bar_id < 0 || bar_id >= 16) {
            py::dict detail = synchronization_detail("barrier_id_out_of_range", trap_pc_or_zero(), bar_id);
            set_trap("synchronization", "barrier_id_out_of_range", trap_pc_or_zero(), detail);
            return;
        }
        const int count = operand(inst, {"count"}, py::int_(0)).cast<int>();
        if (count < 0 || count % kLanes != 0) {
            py::dict detail = synchronization_detail("barrier_count_not_warp_multiple", trap_pc_or_zero(), bar_id);
            detail["expected_count"] = count;
            set_trap("synchronization", "barrier_count_not_warp_multiple", trap_pc_or_zero(), detail);
            return;
        }
        const std::string mode = upper(operand(inst, {"mode"}, py::str("SYNC")).cast<std::string>());
        if (mode != "SYNC" && mode != "ARV") {
            py::dict detail = synchronization_detail("barrier_id_out_of_range", trap_pc_or_zero(), bar_id);
            detail["mode"] = mode;
            set_trap("synchronization", "barrier_id_out_of_range", trap_pc_or_zero(), detail);
            return;
        }

        currygpu::iss::cta_barrier_action action;
        action.valid = true;
        action.warp_id = warp_id_;
        action.bar_id = bar_id;
        action.expected_count = count == 0 ? dim_product(config_.ntid) : count;
        action.explicit_count = count != 0;
        action.blocking = mode == "SYNC";
        action.resume_pc = trap_pc_or_zero() + 1;
        action.pc = trap_pc_or_zero();
        action.lanes = mask;
        pending_cta_barrier_action_ = action;

        if (action.blocking) {
            for (int lane = 0; lane < kLanes; ++lane) {
                if (mask[lane]) {
                    cta_blocked_on_[lane] = bar_id;
                    cta_blocked_resume_pc_[lane] = action.resume_pc;
                    next_pc[lane] = pc_[lane];
                }
            }
        }
    }

    bool splits_active_pc(const std::array<bool, kLanes>& issued_mask, const std::array<int, kLanes>& next_pc) const {
        int seen = -1;
        for (int lane = 0; lane < kLanes; ++lane) {
            if (!issued_mask[lane] || !active_mask_[lane] || lane_state_[lane] != "active" || cta_blocked_on_[lane] >= 0) {
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
                cta_blocked_on_[lane] = -1;
                cta_blocked_resume_pc_[lane] = 0;
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

    py::dict synchronization_detail(const std::string& reason, int pc, int bar_id) const {
        py::dict detail;
        detail["trap_reason"] = reason;
        detail["pc"] = pc;
        detail["bar_id"] = bar_id;
        detail["warp_id"] = warp_id_;
        return detail;
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

    currygpu::iss::memory_space_impl& global_memory() {
        return global_memory_external_ == nullptr ? global_memory_owned_ : *global_memory_external_;
    }

    const currygpu::iss::memory_space_impl& global_memory() const {
        return global_memory_external_ == nullptr ? global_memory_owned_ : *global_memory_external_;
    }

    currygpu::iss::memory_space_impl& shared_memory() {
        return shared_memory_external_ == nullptr ? shared_memory_owned_ : *shared_memory_external_;
    }

    const currygpu::iss::memory_space_impl& shared_memory() const {
        return shared_memory_external_ == nullptr ? shared_memory_owned_ : *shared_memory_external_;
    }

    std::vector<Instruction> program_;
    std::vector<std::array<std::uint32_t, kLanes>> vgpr_;
    std::map<std::string, std::array<bool, kLanes>> predicates_;
    std::array<bool, kLanes> active_mask_{};
    std::array<int, kLanes> pc_{};
    std::array<std::string, kLanes> lane_state_{};
    std::array<Barrier, 16> bx_{};
    std::array<int, kLanes> blocked_on_{};
    std::array<int, kLanes> cta_blocked_on_{};
    std::array<int, kLanes> cta_blocked_resume_pc_{};
    std::array<BarrierStatus, 16> barrier_status_{};
    currygpu::iss::memory_space_impl global_memory_owned_;
    currygpu::iss::memory_space_impl shared_memory_owned_;
    currygpu::iss::memory_space_impl* global_memory_external_ = nullptr;
    currygpu::iss::memory_space_impl* shared_memory_external_ = nullptr;
    currygpu::iss::race_shadow_state* race_shadow_ = nullptr;
    std::array<currygpu::iss::memory_space_impl, kLanes> local_memory_{};
    LaunchConfig config_;
    int warp_id_ = 0;
    SchedOrder order_ = SchedOrder::MinPcFirst;
    bool debug_checks_ = false;
    int issue_pc_ = -1;
    int round_robin_cursor_ = 0;
    std::uint64_t next_group_seq_ = 0;
    std::map<int, std::uint64_t> group_seq_;
    Counters counters_;
    Trap trap_;
    currygpu::iss::cta_barrier_action pending_cta_barrier_action_;
};

#include "block_state.h"

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
    py::class_<NativeBlock>(module, "NativeBlock")
        .def("step", &NativeBlock::step)
        .def("snapshot", &NativeBlock::snapshot);
    py::class_<NativeGrid>(module, "NativeGrid")
        .def("step", &NativeGrid::step)
        .def("snapshot", &NativeGrid::snapshot);

    module.def("launch", [](const py::object& program,
                             int num_gprs,
                             const std::string& sched_order,
                             bool debug_checks,
                             std::uint64_t shared_mem_bytes,
                             std::uint64_t local_mem_bytes,
                             const py::object& global_allocations,
                             const py::object& const_banks,
                             int num_warps,
                             const std::string& warp_sched_order,
                             const py::object& ntid,
                             const py::object& nctaid,
                             bool race_check) {
        ++g_boundary_calls;
        return std::make_unique<NativeGrid>(program, num_gprs, parse_sched_order(sched_order), debug_checks, parse_launch_config(num_warps, warp_sched_order, ntid, nctaid, shared_mem_bytes, local_mem_bytes, global_allocations, const_banks, race_check));
    }, py::arg("program"),
       py::arg("num_gprs") = kDefaultGprs,
       py::arg("sched_order") = "min_pc_first",
       py::arg("debug_checks") = false,
       py::arg("shared_mem_bytes") = 49152,
       py::arg("local_mem_bytes") = 16384,
       py::arg("global_allocations") = py::none(),
       py::arg("const_banks") = py::none(),
       py::arg("num_warps") = 1,
       py::arg("warp_sched_order") = "warp_round_robin",
       py::arg("ntid") = py::none(),
       py::arg("nctaid") = py::none(),
       py::arg("race_check") = false);
    module.def("launch_words", [](const py::object& words,
                                   int num_gprs,
                                   const std::string& sched_order,
                                   bool debug_checks,
                                   std::uint64_t shared_mem_bytes,
                                   std::uint64_t local_mem_bytes,
                                   const py::object& global_allocations,
                                   const py::object& const_banks,
                                   int num_warps,
                                   const std::string& warp_sched_order,
                                   const py::object& ntid,
                                   const py::object& nctaid,
                                   bool race_check) {
        ++g_boundary_calls;
        return std::make_unique<NativeGrid>(adapt_words(words), num_gprs, parse_sched_order(sched_order), debug_checks, parse_launch_config(num_warps, warp_sched_order, ntid, nctaid, shared_mem_bytes, local_mem_bytes, global_allocations, const_banks, race_check));
    }, py::arg("words"),
       py::arg("num_gprs") = kDefaultGprs,
       py::arg("sched_order") = "min_pc_first",
       py::arg("debug_checks") = false,
       py::arg("shared_mem_bytes") = 49152,
       py::arg("local_mem_bytes") = 16384,
       py::arg("global_allocations") = py::none(),
       py::arg("const_banks") = py::none(),
       py::arg("num_warps") = 1,
       py::arg("warp_sched_order") = "warp_round_robin",
       py::arg("ntid") = py::none(),
       py::arg("nctaid") = py::none(),
       py::arg("race_check") = false);
    module.def("step", [](NativeBlock& block, int max_steps) {
        return block.step(max_steps);
    });
    module.def("step", [](NativeGrid& grid, int max_steps) {
        return grid.step(max_steps);
    });
    module.def("state_diff", [](const py::object& left, const py::object& right) {
        ++g_boundary_calls;
        return diff_value(left, right, "$");
    });
    module.def("boundary_calls", []() { return g_boundary_calls; });
    module.def("reset_boundary_calls", []() { g_boundary_calls = 0; });
}
