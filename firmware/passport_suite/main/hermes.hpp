#pragma once
#include <cstdint>
#include <cstring>

namespace passport {
// Replies fit one service packet; complete history remains in the Mac's Hermes session.
struct HermesView {
    char heard[384]{}, answer[1153]{};
    uint32_t turn{};
    uint16_t scroll{};
    bool busy{}, outgoing{}, reset{}, truncated{}, remote{}, locked{}, next{};
};
inline void hermesText(char* out, unsigned size, const char* text) {
    unsigned used=0;
    for(const auto* p=reinterpret_cast<const unsigned char*>(text);*p;){
        unsigned n=*p<0x80?1:*p<0xe0?2:*p<0xf0?3:4;
        if(used+n>=size)break;
        unsigned k=0;while(k<n&&p[k])++k;
        if(k!=n)break;
        std::memcpy(out+used,p,n);used+=n;p+=n;
    }
    out[used]=0;
}
}
