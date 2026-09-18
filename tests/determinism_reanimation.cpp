#include "determinism/reanimation_audit.hpp"
#include "determinism/model.hpp"
#include <iostream>

using namespace lvz::determinism;
namespace {
void Check(bool value,const char* message) {if(!value) throw std::runtime_error(message);}
template<class F> void Reject(F fn) {
    try {fn();} catch(const std::exception&) {return;}
    throw std::runtime_error("Malformed animation snapshot was accepted");
}
Json Owner(uint32_t body,bool dead=false,uint32_t head=0) {
    return {{"zombies",{{"slots",{{"0",{{"id_or_free_next",0x10000u},
        {"fields",{{Hex(0xec),dead?1u:0u},{Hex(0x118),body},{Hex(0x144),head}}}}}}}}}};
}
ReanimationSample Sample(uint32_t id) {
    return {id,false,true,{{"type",2},{"anim_time_bits",0x3e800000u},
        {"anim_rate_bits",0x41c00000u},{"loop_type",1},{"dead",false},
        {"frame_start",20},{"frame_count",18},{"loop_count",0},{"last_frame_time_bits",0x3e700000u}}};
}
ReanimationPoolSnapshot Pool(uint32_t first,uint32_t second=0) {
    ReanimationPoolSnapshot pool;pool.capacity=64;pool.used=64;pool.freeHead=2;pool.nextKey=first>>16;
    if(first) pool.slots.emplace(first&0xffffu,Sample(first));
    if(second) pool.slots.emplace(second&0xffffu,Sample(second));
    pool.count=static_cast<uint32_t>(pool.slots.size());return pool;
}
const Json& Ref(const ReanimationAudit& out,unsigned offset=0x118) {
    return out.comparable.at("zombies").at("slots").at("0").at("fields").at(Hex(offset));
}
}
int main() {
    try {
        Check(!ValidateReanimationTarget(),"Non-game test executable accepted as target image");
        constexpr uint32_t a=0xd57b003bu,b=0xd577003bu;
        ReanimationAuditor left,right;
        auto owner=Owner(a);auto untouched=owner;
        auto l=left.Normalize(owner,Pool(a)),r=right.Normalize(Owner(b),Pool(b));
        Check(l.valid&&r.valid&&l.comparable==r.comparable,"Equivalent generations did not normalize equally");
        Check(l.raw!=r.raw,"Raw generation evidence was lost");
        Check(l.raw["links"][0]["raw_handle"]==a,"Raw handle absent from sidecar");
        Check(owner==untouched,"Normalizer changed input state");
        Check(left.Normalize(owner,Pool(a)).comparable==l.comparable,"Repeated same-boundary capture advanced identity");
        auto expiredL=left.Normalize(Owner(a,true),Pool(0));
        auto expiredR=right.Normalize(Owner(b,true),Pool(0));
        Check(expiredL.valid&&expiredR.valid&&expiredL.comparable==expiredR.comparable,"Expired dead references retain allocation history");
        Check(Ref(expiredL)["status"]=="expired"&&expiredL.raw!=expiredR.raw,"Dead expiry evidence missing");
        Check(right.Normalize(Owner(b,true),Pool(a)).comparable==expiredL.comparable,
            "Unrelated slot reuse changed expired dead reference semantics");
        l=left.Normalize(Owner(a),Pool(b));r=right.Normalize(Owner(b),Pool(a));
        Check(!l.valid&&!r.valid&&l.comparable!=r.comparable,"Dangling live generations were hidden");
        Check(Ref(l)["raw_handle"]==a&&Ref(l)["actual_slot_id"]==b,"Dangling reference must keep both generations");
        left.Reset();right.Reset();
        l=left.Normalize(Owner(a),Pool(a));r=right.Normalize(Owner(b),Pool(b));
        auto changed=Pool(b);changed.slots[b&0xffffu].semantic["anim_time_bits"]=0x3e800001u;
        Check(right.Normalize(Owner(b),changed).comparable!=l.comparable,"One-bit animation progress change disappeared");
        changed=Pool(b);changed.slots[b&0xffffu].semantic["last_frame_time_bits"]=0x3e700001u;
        Check(right.Normalize(Owner(b),changed).comparable!=l.comparable,"Timed-event previous progress change disappeared");
        changed=Pool(b);changed.slots[b&0xffffu].semantic["loop_count"]=1;
        Check(right.Normalize(Owner(b),changed).comparable!=l.comparable,"Loop change disappeared");
        changed=Pool(b);changed.slots[b&0xffffu].definitionValid=false;
        Check(!right.Normalize(Owner(b),changed).valid,"Wrong definition/type association accepted");
        changed=Pool(b);changed.slots[b&0xffffu].dead=true;
        changed.slots[b&0xffffu].semantic["dead"]=true;
        auto retiring=right.Normalize(Owner(b,true),changed);
        Check(Ref(retiring)["status"]=="retiring"&&retiring.comparable["reanimations"]["nodes"].size()==1,"Valid dead animation treated as expired");

        // Equal payloads do not make aliasing or swapping existing objects equal.
        constexpr uint32_t x=0x10001u,y=0x20002u;
        left.Reset();auto two=left.Normalize(Owner(x,false,y),Pool(x,y));
        auto swapped=left.Normalize(Owner(y,false,x),Pool(x,y));
        Check(two.comparable!=swapped.comparable,"Swapping valid equal animations erased object identity");
        auto alias=left.Normalize(Owner(x,false,x),Pool(x,y));
        Check(alias.comparable["reanimations"]["nodes"].size()==1,"Aliased animation duplicated");
        Check(Ref(alias)["node"]==Ref(alias,0x144)["node"],"Alias association lost");
        Check(alias.comparable["reanimations"]["nodes"].begin().value()["owners"].size()==2,"Alias owners omitted");

        left.Reset();auto old=left.Normalize(Owner(x),Pool(x));
        auto replacement=left.Normalize(Owner(0x30001u),Pool(0x30001u));
        Check(Ref(old)["node"]!=Ref(replacement)["node"],"Valid generation replacement across boundaries hidden");
        left.Reset();old=left.Normalize(Owner(x),Pool(x));
        left.Normalize(Owner(0),Pool(0));
        auto wrapped=left.Normalize(Owner(x),Pool(x));
        Check(Ref(old)["node"]!=Ref(wrapped)["node"],"Observed ID retirement/wrap lost lifetime");
        left.Reset();Check(left.Normalize(Owner(x),Pool(x)).comparable==old.comparable,"Run reset did not restore canonical origin");

        auto malformed=Pool(x);malformed.count=2;Reject([&]{left.Normalize(Owner(x),malformed);});
        malformed=Pool(x);malformed.slots[1].id=0x10002u;Reject([&]{left.Normalize(Owner(x),malformed);});
        auto badOwner=Owner(x);badOwner["zombies"]["slots"]["0"]["id_or_free_next"]=0x10001u;
        Reject([&]{left.Normalize(badOwner,Pool(x));});
        badOwner=Owner(x);badOwner["zombies"]["slots"]["0"]["fields"][Hex(0x118)]=-1;
        Reject([&]{left.Normalize(badOwner,Pool(x));});
        std::cout<<"Reanimation association, raw evidence, generation, alias and timed-progress tests passed\n";
        return 0;
    } catch(const std::exception& e) {std::cerr<<e.what()<<'\n';return 1;}
}
