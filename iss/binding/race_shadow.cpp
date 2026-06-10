#include "race_shadow.h"

namespace currygpu::iss {

const char* race_access_kind_name(race_access_kind kind) {
    switch (kind) {
        case race_access_kind::Read:
            return "read";
        case race_access_kind::Write:
            return "write";
    }
    return "unknown";
}

race_conflict race_shadow_state::record(const std::string& space,
                                        std::uint64_t address,
                                        int byte_width,
                                        int thread_id,
                                        race_access_kind kind,
                                        int pc) {
    race_conflict conflict;
    if (byte_width <= 0) {
        return conflict;
    }
    for (int byte = 0; byte < byte_width; ++byte) {
        byte_key key{space, address + static_cast<std::uint64_t>(byte)};
        auto iter = bytes_.find(key);
        if (iter != bytes_.end() && conflicts(iter->second, thread_id, kind)) {
            conflict.found = true;
            conflict.space = space;
            conflict.address = key.address;
            conflict.thread_ids = {iter->second.thread_id, thread_id};
            conflict.access_kinds = {iter->second.kind, kind};
            conflict.pc = pc;
            return conflict;
        }
    }
    for (int byte = 0; byte < byte_width; ++byte) {
        byte_key key{space, address + static_cast<std::uint64_t>(byte)};
        bytes_[key] = byte_access{thread_id, kind, pc};
    }
    return conflict;
}

void race_shadow_state::advance_epoch() {
    bytes_.clear();
}

bool race_shadow_state::conflicts(const byte_access& previous, int thread_id, race_access_kind kind) {
    return previous.thread_id != thread_id && (previous.kind == race_access_kind::Write || kind == race_access_kind::Write);
}

}  // namespace currygpu::iss
