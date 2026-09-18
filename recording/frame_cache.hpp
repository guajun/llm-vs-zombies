#pragma once
#include "native_capture.hpp"
#include "draw_gate.hpp"
#include <nlohmann/json.hpp>
#include <utility>

namespace lvz::recording {
// One CPU-owned frame: the swap chain/surface can change while the outer pump
// processes messages. A stamp is never updated without replacing its pixels.
class FrameCache {
public:
    void Invalidate(const std::string& reason) {valid_=false;reason_=reason;frame_.pixels.clear();}
    void Store(nlohmann::json version,uintptr_t board,int clock,CaptureResult frame) {
        if(!frame.ok||frame.width!=800||frame.height!=600||frame.rowStride!=2400||frame.pixels.size()!=1440000)
            throw std::runtime_error("Cannot cache an incomplete original engine frame");
        version_=std::move(version);board_=board;clock_=clock;frame_=std::move(frame);valid_=true;reason_.clear();
    }
    const CaptureResult* Find(const nlohmann::json& version,uintptr_t board,int clock)const {
        return valid_&&version==version_&&board==board_&&clock==clock_?&frame_:nullptr;
    }
    const nlohmann::json& Version()const {return version_;}
    std::string Reason()const {return valid_?"frame_cache_stale":("frame_cache_unavailable: "+reason_);}
private:
    nlohmann::json version_=nullptr;
    uintptr_t board_=0;int clock_=0;bool valid_=false;
    std::string reason_="no controlled frame has been rendered";
    CaptureResult frame_;
};
}
