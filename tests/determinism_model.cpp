#include "determinism/model.hpp"
#include "determinism/memory.hpp"
#include <bit>
#include <iostream>
#include <random>
#include <iomanip>
#include <sstream>
#include <fstream>

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
void CheckDigests(const Json& state) {
    auto actual=Digests(state);
    Check(actual.at("all")==Digest(state.dump()),"Combined digest encoding changed");
    for(auto it=state.begin();it!=state.end();++it)
        if(it.key()!="all") Check(actual.at(it.key())==Digest(it.value().dump()),"Component digest changed");
}
int main(int argc,char** argv) {
    try {
        std::mt19937 formatting(784);
        for(int i=0;i<10000;++i) {
            const auto value=static_cast<uint32_t>(formatting());
            std::ostringstream reference;reference<<std::hex<<std::setfill('0')<<std::setw(8)<<value;
            Check(Hex(value)==reference.str(),"Fixed-width hex formatting changed");
        }
        Check(Hex(0)=="00000000"&&Hex(UINT32_MAX)=="ffffffff","Hex endpoint mismatch");
        Check(Digest("")=="cbf29ce484222325"&&Digest("hello")=="a430d84680aabd0b","FNV standard vectors changed");
        CheckDigests(Json::object());
        CheckDigests({{"quote\"\\/\n",Json::array({nullptr,true,false,-42,1.25,uint64_t(UINT64_MAX),"\xe9\x9b\xbe"})},{"all",Json::object()}});
        for(int file=1;file<argc;++file) {
            std::ifstream input(argv[file]);Check(bool(input),"Recorded audit input could not open");
            std::string line;Json state;size_t records=0;
            while(std::getline(input,line)) {
                auto record=Json::parse(line);
                if(record.contains("initial")) state=record.at("initial");
                else state=state.patch(record.at("patch"));
                CheckDigests(state);++records;
            }
            Check(records>0,"Recorded audit input was empty");
            std::cout<<records<<" recorded state digests matched reference encoding\n";
        }
        auto page=VirtualAlloc(nullptr,4096,MEM_RESERVE|MEM_COMMIT,PAGE_READWRITE);
        Check(page!=nullptr,"Test page allocation failed");
        *static_cast<uint32_t*>(page)=42;
        const auto address=reinterpret_cast<uintptr_t>(page);
        {ReadScope reads;Check(Read<uint32_t>(address)==42,"Scoped snapshot read failed");}
        DWORD old=0;Check(VirtualProtect(page,4096,PAGE_NOACCESS,&old)!=0,"Test page protection failed");
        {ReadScope reads;uint32_t value=0;Check(!TryRead(address,value),"Read permission cache crossed snapshot boundary");}
        Check(VirtualFree(page,0,MEM_RELEASE)!=0,"Test page release failed");
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
