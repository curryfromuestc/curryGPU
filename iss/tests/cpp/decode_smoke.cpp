#include <cstdint>
#include <string>

#include "decoded_inst.gen.h"

int main() {
    using currygpu::isa::decode_once;

    const unsigned __int128 iadd3 =
        static_cast<unsigned __int128>(0x11) |
        (static_cast<unsigned __int128>(0xFFFFF) << 36) |
        (static_cast<unsigned __int128>(17) << 118);
    const unsigned __int128 exit_word = static_cast<unsigned __int128>(0x7F);
    const unsigned __int128 exit_reserved = exit_word | (static_cast<unsigned __int128>(1) << 12);
    const unsigned __int128 unknown = static_cast<unsigned __int128>(0xFE);

    const auto decoded_iadd3 = decode_once(iadd3);
    const auto decoded_exit = decode_once(exit_word);
    const auto decoded_reserved = decode_once(exit_reserved);
    const auto decoded_unknown = decode_once(unknown);

    if (!decoded_iadd3.ok || !decoded_exit.ok || decoded_reserved.ok || decoded_unknown.ok) {
        return 1;
    }
    if (decoded_iadd3.field_count != 7 || decoded_exit.field_count != 2) {
        return 2;
    }
    if (decoded_iadd3.control.wait_mask != 17) {
        return 3;
    }
    bool saw_signed_src_c = false;
    for (int index = 0; index < decoded_iadd3.field_count; ++index) {
        if (std::string(decoded_iadd3.fields[index].name) == "src_c" && decoded_iadd3.fields[index].value == -1) {
            saw_signed_src_c = true;
        }
    }
    if (!saw_signed_src_c) {
        return 4;
    }
    if (std::string(decoded_reserved.trap) != "reserved" || std::string(decoded_unknown.trap) != "unknown") {
        return 5;
    }
    return 0;
}
