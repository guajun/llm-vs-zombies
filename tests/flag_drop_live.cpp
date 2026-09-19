// Compiled ONLY into the explicitly requested recorder_flag_fixture target.
#include "flag_drop_live.hpp"
#include "determinism/memory.hpp"
#include "determinism/reanimation_audit.hpp"
#include <avz.h>
#include <fstream>
#include <windows.h>

namespace lvz::flagfixture {
namespace {
using lvz::determinism::Read;
using lvz::determinism::Accessible;
std::ofstream output;
std::filesystem::path lockPath;
HANDLE lockHandle=INVALID_HANDLE_VALUE;
uint32_t ownerThread=0,zombieId=0,flagId=0;
uintptr_t boardIdentity=0;
uint64_t ordinal=0,inCall=0,completed=0;
bool closed=false;
std::string fault;
void Require(bool value,const char* text){if(!value)throw std::runtime_error(text);}
void Emit(const char* kind,const Json& version,uint64_t call,const Json& payload) {
    output<<Json({{"schema","lvz.flag-drop-fixture.v1"},{"mode",Mode},{"ordinal",ordinal++},
        {"kind",kind},{"engine_call_id",call?Json(call):Json(nullptr)},
        {"version",version},{"payload",payload}}).dump()<<'\n';
    output.flush();Require(bool(output),"Cannot persist fixture evidence");
}
uintptr_t Board() {
    const auto app=Read<uint32_t>(0x6a9ec0);
    Require(app&&Read<uint32_t>(app+0x7fc)==3,"Fixture requires real UI3");
    auto board=Read<uint32_t>(app+0x768);
    Require(board!=0,"Fixture has no Board");
    return board;
}
Json Animation(uint32_t handle) {
    const auto app=Read<uint32_t>(0x6a9ec0),effects=Read<uint32_t>(app+0x820);
    const auto holder=Read<uint32_t>(effects+8),block=Read<uint32_t>(holder);
    const auto used=Read<uint32_t>(holder+4),capacity=Read<uint32_t>(holder+8),slot=handle&0xffffu;
    Require(used<=capacity&&capacity<=65536,"Fixture animation pool invalid");
    Json result={{"raw_handle",handle},{"slot",slot},{"pool_used",used},{"pool_capacity",capacity},
        {"actual_slot_id",nullptr},{"matches",false}};
    if(slot<used) {
        const auto at=block+slot*0xa0,actual=Read<uint32_t>(at+0x9c);
        if(actual&0xffff0000u)result["actual_slot_id"]=actual;
        if(handle&&actual==handle)result.update({{"matches",true},{"type",Read<uint32_t>(at)},
            {"dead",Read<uint8_t>(at+0x14)!=0}});
    }
    return result;
}
uintptr_t Zombie() {
    const auto board=Board();Require(board==boardIdentity,"Fixture Board changed");
    const auto used=Read<uint32_t>(board+0x94),capacity=Read<uint32_t>(board+0x98),slot=zombieId&0xffffu;
    Require((zombieId&0xffff0000u)&&used<=capacity&&capacity<=1024&&slot<used,"Fixture zombie pool/ID invalid");
    const auto address=Read<uint32_t>(board+0x90)+slot*0x15c;
    Require(Read<uint32_t>(address+0x158)==zombieId,"Fixture zombie generation changed");
    Require(Read<uint32_t>(address+0x24)==1&&Read<uint32_t>(address+0x1c)==2,"Fixture zombie identity/type/row changed");
    Require(Read<uint8_t>(address+0xec)==0,"Fixture owner unexpectedly dead");
    return address;
}
Json Snapshot() {
    const auto address=Zombie();
    return {{"owner_id",zombieId},{"owner_address",address},{"board_address",boardIdentity},
        {"game_clock",Read<uint32_t>(boardIdentity+0x5568)},
        {"owner_type",Read<uint32_t>(address+0x24)},{"owner_has_head",Read<uint8_t>(address+0xba)},
        {"owner_has_object",Read<uint8_t>(address+0xbc)},{"owner_dead",Read<uint8_t>(address+0xec)},
        {"owner_hp",Read<uint32_t>(address+0xc8)},
        {"body",Animation(Read<uint32_t>(address+0x118))},{"flag",Animation(Read<uint32_t>(address+0x144))}};
}
void VerifyTarget() {
    static constexpr uint8_t dropHead[]={0x55,0x8b,0xec,0x83,0xe4,0xf8,0x83,0xec,0x2c,0x53,0x8b,0x5d,0x08};
    static constexpr uint8_t putZombie[]={0x53,0x55,0x8b,0x6c,0x24,0x0c,0x56,0x57,0x8b,0xf0,0x8b,0xf9};
    Require(lvz::determinism::ValidateReanimationTarget(),"Fixture reanimation target signature mismatch");
    Require(Accessible(0x529a30,sizeof(dropHead))&&!std::memcmp(reinterpret_cast<void*>(0x529a30),dropHead,sizeof(dropHead)),"Fixture DropHead signature mismatch");
    Require(Accessible(0x42a0f0,sizeof(putZombie))&&!std::memcmp(reinterpret_cast<void*>(0x42a0f0),putZombie,sizeof(putZombie)),"Fixture PutZombie signature mismatch");
}
void VerifyBody(const Json& snapshot) {
    Require(snapshot.at("owner_dead")==0&&snapshot.at("body").at("matches")==true
        &&snapshot.at("body").at("dead")==false,"Fixture live body must remain allocated");
    Require(snapshot.at("flag").at("raw_handle")==flagId,"Original flag handle changed");
}
}
void Initialize(const std::filesystem::path& run,uint32_t thread) {
    Require(ownerThread==0&&thread==GetCurrentThreadId(),"Fixture initialized twice/off owner thread");
    ownerThread=thread;
    const auto path=run/"decisions/flag-drop-fixture.jsonl";
    Require(!std::filesystem::exists(path),"Fixture output already exists");
    lockPath=path;lockPath+=".lock";
    lockHandle=CreateFileW(lockPath.c_str(),GENERIC_WRITE,0,nullptr,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,nullptr);
    Require(lockHandle!=INVALID_HANDLE_VALUE,"Cannot create fixture writer lock");
    output.open(path,std::ios::out|std::ios::binary);
    Require(bool(output),"Cannot open fixture evidence");
    Emit("initialized",nullptr,0,Manifest());
}
void BeforeOriginalUpdate(const Json& health,const Json& version,bool ready) {
    try {
        const auto call=ControlledCall(health,ready,ownerThread,GetCurrentThreadId());
        if(!call)return; // Original startup updates have no controlled call ID.
        Require(!closed&&fault.empty(),"Fixture is closed/faulted");
        if(call>3){Require(completed==3,"Fixture calls were skipped");return;}
        Require(!inCall&&call==completed+1,"Fixture original callback reentered/skipped");
        VerifyTarget();inCall=call;
        if(call==1) {
            boardIdentity=Board();const auto block=Read<uint32_t>(boardIdentity+0x90);
            const auto beforeUsed=Read<uint32_t>(boardIdentity+0x94),capacity=Read<uint32_t>(boardIdentity+0x98);
            const auto free=Read<uint32_t>(boardIdentity+0x9c),count=Read<uint32_t>(boardIdentity+0xa0);
            Require(beforeUsed<=capacity&&capacity<=1024&&count<capacity&&free<capacity,"Fixture has no safe zombie allocation slot");
            const auto address=block+free*0x15c;
            const auto old=free<beforeUsed?Read<uint32_t>(address+0x158):0;
            Require(!(old&0xffff0000u),"Fixture candidate slot already allocated");
            auto* created=AAsm::PutZombie(2,8,static_cast<AZombieType>(1));
            Require(reinterpret_cast<uintptr_t>(created)==address&&Read<uint32_t>(boardIdentity+0xa0)==count+1,
                "PutZombie did not allocate exactly the predicted owned slot");
            zombieId=Read<uint32_t>(address+0x158);
            Require(zombieId!=old&&(zombieId&0xffff0000u)&&(zombieId&0xffffu)==free,"PutZombie new full generation invalid");
            flagId=Read<uint32_t>(Zombie()+0x144);
            auto snapshot=Snapshot();VerifyBody(snapshot);
            Require(snapshot["owner_has_head"]==1&&snapshot["owner_has_object"]==1
                &&snapshot["flag"]["matches"]==true&&snapshot["flag"]["type"]==142&&snapshot["flag"]["dead"]==false,
                "PutZombie did not create a carried live flag");
            Emit("after_original_put_zombie",version,call,snapshot);
        } else if(call==2) {
            auto before=Snapshot();VerifyBody(before);
            Require(before["owner_has_head"]==1&&before["owner_has_object"]==1&&before["flag"]["matches"]==true,
                "Fixture target lost head/flag before original DropHead");
            Emit("before_original_drop_head",version,call,before);
            InvokeDropHead(reinterpret_cast<DropHeadFn>(0x529a30),reinterpret_cast<void*>(Zombie()));
            auto after=Snapshot();VerifyBody(after);
            Require(after["owner_has_head"]==0&&after["owner_has_object"]==0
                &&after["flag"]["matches"]==true&&after["flag"]["dead"]==true,
                "Original DropHead did not mark carried flag retiring");
            Emit("after_original_drop_head_before_update",version,call,after);
        } else Emit("before_verification_update",version,call,Snapshot());
    } catch(const std::exception& error){fault=error.what();throw;}
}
void AfterOriginalUpdate(const Json& health,const Json& version,bool ready) {
    try {
        const auto call=ControlledCall(health,ready,ownerThread,GetCurrentThreadId());
        if(!call||call>3)return;
        Require(inCall==call,"Fixture original callback return mismatched");
        auto snapshot=Snapshot();VerifyBody(snapshot);
        if(call==1)Require(snapshot["owner_has_object"]==1&&snapshot["flag"]["matches"]==true,
            "Flag disappeared during creation update");
        else Require(snapshot["owner_has_head"]==0&&snapshot["owner_has_object"]==0&&snapshot["flag"]["matches"]==false,
            "Original update did not retire/reclaim flag while preserving owner");
        Emit("after_original_update",version,call,snapshot);completed=call;inCall=0;
    } catch(const std::exception& error){fault=error.what();throw;}
}
void Close() {
    if(closed)return;
    Require(ownerThread==GetCurrentThreadId(),"Fixture close off owner thread");
    Emit("closed",nullptr,0,{{"healthy",fault.empty()&&completed==3&&!inCall},
        {"completed_fixture_calls",completed},{"active_call",inCall},{"error",fault}});
    output.close();Require(!output.fail(),"Fixture evidence close failed");
    Require(CloseHandle(lockHandle)!=0,"Fixture lock close failed");lockHandle=INVALID_HANDLE_VALUE;
    Require(DeleteFileW(lockPath.c_str())!=0,"Fixture lock unlink failed");closed=true;
}
}
