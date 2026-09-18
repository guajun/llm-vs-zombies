#pragma once
#include <algorithm>
#include <string>
#include <nlohmann/json.hpp>

namespace lvz::determinism {
namespace json_diff_detail {
inline std::string EscapePointerToken(const std::string& value) {
    std::string out;
    for(char c:value) {
        if(c=='~') out+="~0";
        else if(c=='/') out+="~1";
        else out+=c;
    }
    return out;
}

// Preserve nlohmann::json::diff ordering and number/type comparisons. Append
// directly into the final patch, and build paths only for changed children.
// The ordinary implementation allocates an empty patch/path for every visited
// child and copies nonempty temporary patches at every level of recursion.
inline void Append(const nlohmann::json& source,const nlohmann::json& target,
                   nlohmann::json& out,const std::string& path) {
    if(source==target) return;
    if(source.type()!=target.type()||!source.is_structured()) {
        out.push_back({{"op","replace"},{"path",path},{"value",target}});
        return;
    }
    if(source.is_object()) {
        for(auto it=source.begin();it!=source.end();++it) {
            const auto found=target.find(it.key());
            if(found==target.end())
                out.push_back({{"op","remove"},{"path",path+"/"+EscapePointerToken(it.key())}});
            else if(it.value()!=*found)
                Append(it.value(),*found,out,path+"/"+EscapePointerToken(it.key()));
        }
        for(auto it=target.begin();it!=target.end();++it)
            if(!source.contains(it.key()))
                out.push_back({{"op","add"},{"path",path+"/"+EscapePointerToken(it.key())},{"value",it.value()}});
        return;
    }
    const auto common=std::min(source.size(),target.size());
    for(size_t index=0;index<common;++index)
        if(source[index]!=target[index])
            Append(source[index],target[index],out,path+"/"+std::to_string(index));
    // Removals must be descending so application cannot shift a later index.
    for(size_t index=source.size();index>common;--index)
        out.push_back({{"op","remove"},{"path",path+"/"+std::to_string(index-1)}});
    for(size_t index=common;index<target.size();++index)
        out.push_back({{"op","add"},{"path",path+"/-"},{"value",target[index]}});
}
}

// Same JSON Patch and operation order as nlohmann::json::diff, including root
// replacement, escaped object keys, array additions/removals and raw uint bits.
inline nlohmann::json ExactJsonDiff(const nlohmann::json& source,const nlohmann::json& target) {
    auto out=nlohmann::json::array();
    json_diff_detail::Append(source,target,out,"");
    return out;
}
}
