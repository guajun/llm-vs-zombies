#pragma once
#include <cstddef>
#include <stdexcept>
#include <string>

// Branch identity is a namespace label, not free text. The launcher, the
// resident runtime (RequestJournal::ValidBranch) and the evidence tree's
// branch_id all accept exactly the same grammar: 1-64 characters of
// [A-Za-z0-9._:-] starting with an alphanumeric character.
//
// Header-only so the native launcher and its unit test compile one predicate:
// launcher/native_launcher.cpp must reject an unusable id *before* it creates
// the suspended engine, because a silent fallback would leave the run with a
// runtime identity that does not match the evidence tree's branch name.
namespace lvz::branch {
inline constexpr std::size_t MaxId = 64;

inline bool Valid(const std::string& value) {
    auto alnum=[](char c){return (c>='0'&&c<='9')||(c>='A'&&c<='Z')||(c>='a'&&c<='z');};
    if(value.empty()||value.size()>MaxId||!alnum(value[0])) return false;
    for(char c:value) if(!alnum(c)&&c!='.'&&c!='_'&&c!=':'&&c!='-') return false;
    return true;
}

// Fail closed with a message that names the actual problem. Never rewrite an
// illegal value into a legal one: the id is evidence, not a presentation
// detail.
inline void Require(const std::string& value) {
    if(Valid(value)) return;
    if(value.empty()) throw std::runtime_error("branch id must not be empty");
    if(value.size()>MaxId)
        throw std::runtime_error("branch id is longer than 64 characters: "+std::to_string(value.size()));
    throw std::runtime_error("branch id must be 1-64 characters of [A-Za-z0-9._:-] starting alphanumeric");
}
}
