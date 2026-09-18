#include "determinism/model.hpp"
#include <bit>
#include <iostream>
#include <random>

using namespace lvz::determinism;
namespace {
void Check(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}
template<class Fn> void Reject(Fn fn) {
    try { fn(); } catch (const std::exception&) { return; }
    throw std::runtime_error("Malformed snapshot was accepted");
}
uint32_t Next(MtState& state) {
    if(state.cursor>=624) {
        for(size_t i=0;i<624;++i) {
            auto merged=(state.words[i]&0x80000000u)|(state.words[(i+1)%624]&0x7fffffffu);
            state.words[i]=state.words[(i+397)%624]^(merged>>1)^((merged&1)?0x9908b0dfu:0u);
        }
        state.cursor=0;
    }
    auto result=state.words[state.cursor++];
    result^=result>>11;result^=(result<<7)&0x9d2c5680u;
    result^=(result<<15)&0xefc60000u;result^=result>>18;
    return result&0x7fffffffu;
}
}
int main() {
    try {
        // Independent standard-library generator checks the test stream before
        // testing snapshot continuation, including two MT twist boundaries.
        auto state=SeedMt(5489); std::mt19937 reference(5489);
        Check(SeedMt(0)==SeedMt(4357),"Original zero-seed special case lost");
        for(unsigned i=0;i<1200;++i) Check(Next(state)==(reference()&0x7fffffffu),"Reference stream mismatch");
        const auto wire=EncodeMt(state).dump();
        auto resumed=DecodeMt(Json::parse(wire));
        Check(resumed==state,"Snapshot lost MT words or cursor");
        for(unsigned i=0;i<2000;++i) Check(Next(state)==Next(resumed),"Restored stream diverged");
        auto bad=Json::parse(wire); bad.erase("cursor"); Reject([&]{DecodeMt(bad);});
        bad=Json::parse(wire);bad["cursor"]=uint32_t(626);Reject([&]{DecodeMt(bad);});
        bad=Json::parse(wire);bad["cursor"]=-1;Reject([&]{DecodeMt(bad);});
        bad=Json::parse(wire);bad["words"][15]=uint64_t(UINT32_MAX)+1;Reject([&]{DecodeMt(bad);});
        bad=Json::parse(wire);bad["words"].erase(15);Reject([&]{DecodeMt(bad);});
        bad=Json::parse(wire);bad["words"][0]=1.0;Reject([&]{DecodeMt(bad);});
        Json before={{"zombies",{{"1",{{"x_bits",std::bit_cast<uint32_t>(0.0f)}}}}},
                     {"rng",EncodeMt(resumed)}};
        Json after=before;after["zombies"]["1"]["x_bits"]=std::bit_cast<uint32_t>(-0.0f);
        Check(Digests(before)["all"]!=Digests(after)["all"],"Float sign bit lost");
        Check(Digests(before)["rng"]==Digests(after)["rng"],"Unchanged component digest changed");
        const auto patch=Json::diff(before,after);
        Check(before.patch(patch)==after,"Delta cannot reconstruct state");
        after["zombies"]["1"]["x_bits"]=uint32_t(0x7fc00001u);
        Check(Json::parse(after.dump())==after,"NaN payload raw bits lost");
        std::cout<<"determinism model: snapshot continuation, validation, raw float bits, component digests, deltas passed\n";
        return 0;
    } catch(const std::exception& exception) {
        std::cerr<<exception.what()<<'\n';return 1;
    }
}
