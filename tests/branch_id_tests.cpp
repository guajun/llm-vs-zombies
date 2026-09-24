#include "launcher/branch_id.hpp"
#include <iostream>
#include <stdexcept>
#include <string>

namespace {
int failures=0;
void Check(bool ok,const std::string& why) {
    if(!ok) { ++failures; std::cout<<"FAIL "<<why<<"\n"; }
}
void Accept(const std::string& value,const char* why) {
    Check(lvz::branch::Valid(value),std::string("rejected a valid branch id (" )+why+"): "+value);
}
void Reject(const std::string& value,const char* why) {
    Check(!lvz::branch::Valid(value),std::string("accepted an invalid branch id (" )+why+"): "+value);
}
void RequireRejects(const std::string& value,const std::string& expected) {
    try { lvz::branch::Require(value); }
    catch(const std::exception& error) {
        Check(std::string(error.what()).find(expected)!=std::string::npos,
              "rejection message lacks '"+expected+"': "+error.what());
        return;
    }
    Check(false,"Require accepted "+value);
}
}

// The launcher, the runtime (RequestJournal::ValidBranch) and the evidence tree
// must agree on one grammar: 1-64 characters of [A-Za-z0-9._:-], starting
// alphanumeric. This test pins the launcher side; tests/test_launcher.py pins
// the Python side to client.branch_scope_id and evidence_tree.branch_id.
int main() {
    Accept("a","single character");
    Accept("0","numbers may start an id");
    Accept("m1-par-d-s42-c0","run directory name");
    Accept("branch.a_b:c-d","every allowed separator");
    Accept(std::string(1,'a')+std::string(lvz::branch::MaxId-1,'b'),"exactly 64 characters");
    Reject("","empty");
    Reject(std::string(1,'a')+std::string(lvz::branch::MaxId,'b'),"65 characters");
    Reject("-leading","separator first");
    Reject("_leading","underscore first");
    Reject(".leading","dot first");
    Reject(":leading","colon first");
    Reject("has space","space");
    Reject("bang!","illegal character");
    Reject("a/b","path separator");
    Reject(std::string("caf")+"\xc3\xa9","non-ASCII byte");
    RequireRejects("", "must not be empty");
    RequireRejects(std::string(1,'a')+std::string(lvz::branch::MaxId,'b'),"longer than 64 characters: 65");
    RequireRejects("bad!","must be 1-64 characters");
    lvz::branch::Require("A-valid.id");  // Must not throw.
    if(failures) { std::cout<<failures<<" branch id check(s) failed\n"; return 1; }
    std::cout<<"branch id grammar ok\n";
    return 0;
}
