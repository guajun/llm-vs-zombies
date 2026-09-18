#include "determinism/json_diff.hpp"
#include <fstream>
#include <iostream>
#include <random>
#include <stdexcept>
#include <vector>

using Json=nlohmann::json;
using lvz::determinism::ExactJsonDiff;
namespace {
void Check(bool okay,const char* reason) {if(!okay)throw std::runtime_error(reason);}
void Equivalent(const Json& before,const Json& after) {
    const auto expected=Json::diff(before,after);
    const auto actual=ExactJsonDiff(before,after);
    Check(actual==expected,"JSON Patch operation or order differs from nlohmann reference");
    Check(actual.dump()==expected.dump(),"JSON Patch wire bytes differ from nlohmann reference");
    Check(before.patch(actual)==after,"JSON Patch cannot reconstruct target");
}
Json Random(std::mt19937& rng,int depth) {
    const auto type=rng()%(depth?9:6);
    switch(type) {
    case 0:return nullptr;
    case 1:return (rng()&1)!=0;
    case 2:return uint64_t(rng())<<32|rng();
    case 3:return -int64_t(rng());
    case 4:return double(int32_t(rng()))/16;
    case 5:return std::string(rng()%12,'x')+"~/\n";
    case 6:case 7: {
        auto value=Json::array();for(size_t n=0,limit=rng()%8;n<limit;++n)value.push_back(Random(rng,depth-1));return value;
    }
    default: {
        auto value=Json::object();for(size_t n=0,limit=rng()%8;n<limit;++n)value[std::to_string(rng()%12)+"~/"]=Random(rng,depth-1);return value;
    }
    }
}
void VerifyRecorded(const char* file) {
    std::ifstream input(file);Check(bool(input),"cannot open recorded deltas");
    std::string line;Json state=nullptr;size_t verified=0;
    while(std::getline(input,line)) {
        if(line.empty())continue;const auto record=Json::parse(line);
        if(record.contains("initial")) {state=record.at("initial");continue;}
        const auto& patch=record.at("patch");const auto next=state.patch(patch);
        const auto actual=ExactJsonDiff(state,next);
        Check(actual.dump()==patch.dump(),"optimized patch differs from a recorded canonical patch");
        state=next;++verified;
    }
    Check(verified>0,"recording contains no patches");
    std::cout<<file<<": "<<verified<<" recorded patches match byte-for-byte\n";
}
}
int main(int argc,char** argv) {try {
    const std::vector<Json> values={nullptr,true,false,7,-3,1.25,-0.0,0.0,uint64_t(UINT64_MAX),"text",
        Json::array(),Json::array({1,2,3}),Json::array({0}),Json::object(),
        Json{{"a/b",1},{"~",Json::array({1,2,3})},{"",Json::object()}},
        Json{{"a/b",2},{"~",Json::array({2})},{"new",nullptr}}};
    for(const auto& before:values)for(const auto& after:values)Equivalent(before,after);
    std::mt19937 rng(424242);
    for(int n=0;n<2000;++n)Equivalent(Random(rng,4),Random(rng,4));
    Json before={{"board",{{"clock",0},{"float_bits",uint32_t(0x7fc00001)}}},{"zombies",Json::object()}};
    for(int slot=0;slot<72;++slot) {
        Json fields=Json::object();for(int offset=0;offset<96;++offset)fields[std::to_string(offset)]=uint32_t(slot*96+offset);
        before["zombies"][std::to_string(slot)]={{"id",65536+slot},{"fields",std::move(fields)}};
    }
    auto after=before;after["board"]["clock"]=1;after["board"]["float_bits"]=uint32_t(0x80000000);
    for(int slot=0;slot<72;++slot)for(int offset=0;offset<8;++offset)after["zombies"][std::to_string(slot)]["fields"][std::to_string(offset)]=rng();
    after["zombies"].erase("2");after["zombies"]["72"]={{"id",131074},{"fields",Json::object()}};
    Equivalent(before,after);Equivalent(after,after);Equivalent(after,before);
    for(int n=1;n<argc;++n)VerifyRecorded(argv[n]);
    std::cout<<"JSON diff: type/escape pairs, 2000 randomized trees, dense entity changes passed\n";return 0;
}catch(const std::exception& error){std::cerr<<error.what()<<'\n';return 1;}}
