#include "determinism/reanimation_audit.hpp"
#include "determinism/model.hpp"
#include <iostream>
#include <utility>

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
Json PlantOwner(uint32_t handle,bool dead=false,bool squished=false) {
    Json fields={{Hex(0x24),8},{Hex(0x3c),0},{Hex(0x4c),squished?500:200},
        {Hex(0x134),0},{Hex(0x141),dead?1u:0u},{Hex(0x142),squished?1u:0u},{Hex(0x143),0}};
    for(unsigned at=0x94;at<=0xac;at+=4) fields[Hex(at)]=handle;
    return {{"plants",{{"slots",{{"0",{{"id_or_free_next",0x10000u},{"fields",fields}}}}}}}};
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
        // A real full frame is mostly unrelated Board/RNG/entity data. Moving
        // a capture must retain that subtree allocation, not deep-copy it.
        ReanimationAuditor copying,moving;
        for(const auto handles: {std::pair{a,a},std::pair{a,0u},std::pair{b,b},std::pair{0u,0u},std::pair{a,a}}) {
            auto frame=Owner(handles.first,false,handles.second);
            frame["unrelated_raw_words"]=Json::array();
            for(uint32_t i=0;i<4096;++i) frame["unrelated_raw_words"].push_back(i^0xff123400u);
            const auto original=frame;
            const auto expected=copying.Normalize(frame,Pool(handles.first));
            Check(frame==original,"Value-argument normalization mutated an lvalue input");
            const auto* storage=frame["unrelated_raw_words"].get_ptr<const Json::array_t*>();
            const auto moved=moving.Normalize(std::move(frame),Pool(handles.first));
            Check(moved.comparable["unrelated_raw_words"].get_ptr<const Json::array_t*>()==storage,
                "Moved full-frame subtree was deep-copied");
            Check(moved.comparable==expected.comparable&&moved.raw==expected.raw
                &&moved.coverage==expected.coverage&&moved.valid==expected.valid,
                "Moving changed semantic state, raw evidence, aliasing or lifecycle");
        }
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

        // Original Plant::Squish clears effects before setting mDead hundreds
        // of ticks later. Only its distinct +142 flag permits this retirement.
        left.Reset();right.Reset();
        auto crushed=left.Normalize(PlantOwner(a,false,true),Pool(0));
        auto otherCrushed=right.Normalize(PlantOwner(b,false,true),Pool(0));
        Check(crushed.valid&&otherCrushed.valid&&crushed.comparable==otherCrushed.comparable,
            "Squished plant effects did not expire independently of raw generation");
        Check(crushed.raw!=otherCrushed.raw,"Squished raw generation evidence was discarded");
        Check(crushed.raw["links"].size()==7,"Plant retirement did not cover its seven effects");
        for(const auto& link:crushed.raw["links"]) {
            Check(link["owner_dead"]==false&&link["owner_squished"]==true,
                "Squished plant was falsely recorded as mDead");
            Check(link["normalized_reference"]["status"]=="expired", "Squished effect is not expired");
            Check(link["retirement_reason"]=="plant_squished_remove_effects", "Squished retirement basis missing");
        }
        Check(crushed.comparable["plants"]["slots"]["0"]["fields"][Hex(0x142)]==1,
            "Squished flag disappeared from comparable state");
        Check(!left.Normalize(PlantOwner(a),Pool(0)).valid,"Ordinary live plant dangling references accepted");
        Check(!left.Normalize(PlantOwner(a),Pool(b)).valid,"Live plant generation mismatch accepted");
        auto inactivePlant=PlantOwner(a);auto& inactiveFields=inactivePlant["plants"]["slots"]["0"]["fields"];
        inactiveFields[Hex(0x134)]=2;inactiveFields[Hex(0x143)]=1;
        Check(!left.Normalize(inactivePlant,Pool(0)).valid,"Bungee/asleep status incorrectly retires effects");
        auto notAPlant=Owner(a);notAPlant["zombies"]["slots"]["0"]["fields"][Hex(0x142)]=1;
        Check(!left.Normalize(notAPlant,Pool(0)).valid,"Plant-only squish rule applied to zombie field");
        auto stillAllocated=left.Normalize(PlantOwner(a,false,true),Pool(a));
        Check(stillAllocated.valid&&stillAllocated.comparable["reanimations"]["nodes"].size()==1,
            "Squished plant's still allocated animation was dropped");
        auto notSquished=left.Normalize(PlantOwner(a),Pool(a));
        Check(Digests(stillAllocated.comparable)!=Digests(notSquished.comparable),
            "Squished-flag change disappeared from state checksums");
        for(const auto& link:stillAllocated.raw["links"])
            Check(!link.contains("retirement_reason"),"Allocated squished animation was labeled expired");
        auto deadAndSquished=left.Normalize(PlantOwner(a,true,true),Pool(0));
        Check(deadAndSquished.raw["links"][0]["retirement_reason"]=="owner_dead",
            "mDead must take precedence when both retirement flags are set");
        auto reusedSlot=left.Normalize(PlantOwner(a,false,true),Pool(b));
        Check(reusedSlot.valid&&reusedSlot.raw["links"][0]["lookup_failure"]=="generation_mismatch",
            "Squished expiry lost actual slot-reuse evidence");
        inactivePlant=PlantOwner(a);inactivePlant["plants"]["slots"]["0"]["fields"].erase(Hex(0x142));
        Reject([&]{left.Normalize(inactivePlant,Pool(a));});

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
