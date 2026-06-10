#pragma once

#include <array>
#include <cstdint>
#include <map>
#include <string>

namespace currygpu::iss {

enum class race_access_kind {
    Read,
    Write,
};

struct race_conflict {
    bool found = false;
    std::string space;
    std::uint64_t address = 0;
    std::array<int, 2> thread_ids{};
    std::array<race_access_kind, 2> access_kinds{};
    int pc = 0;
};

const char* race_access_kind_name(race_access_kind kind);

class race_shadow_state {
public:
    race_conflict record(const std::string& space,
                         std::uint64_t address,
                         int byte_width,
                         int thread_id,
                         race_access_kind kind,
                         int pc);
    void advance_epoch();

private:
    struct byte_key {
        std::string space;
        std::uint64_t address = 0;

        bool operator<(const byte_key& other) const {
            if (space != other.space) {
                return space < other.space;
            }
            return address < other.address;
        }
    };

    struct byte_access {
        int thread_id = -1;
        race_access_kind kind = race_access_kind::Read;
        int pc = 0;
    };

    static bool conflicts(const byte_access& previous, int thread_id, race_access_kind kind);

    std::map<byte_key, byte_access> bytes_;
};

}  // namespace currygpu::iss
