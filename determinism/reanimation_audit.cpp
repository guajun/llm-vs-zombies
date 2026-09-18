#include "reanimation_audit.hpp"
#include "memory.hpp"
#include "model.hpp"
#include <algorithm>
#include <array>
#include <vector>

namespace lvz::determinism {
namespace {
struct Role {unsigned offset;const char* name;};
constexpr Role zombieRoles[]={{0x118,"body"},{0x140,"boss_fireball"},{0x144,"special_head"},{0x150,"mowered"}};
constexpr Role plantRoles[]={{0x94,"body"},{0x98,"head"},{0x9c,"head2"},{0xa0,"head3"},
    {0xa4,"blink"},{0xa8,"light"},{0xac,"sleeping"}};
constexpr Role mowerRoles[]={{0x1c,"body"}};
constexpr Role gridRoles[]={{0x34,"body"}};
struct Binding {std::string path,anchor;uint32_t handle;bool ownerDead;};
uint32_t U32(const Json& value) {
    if(!value.is_number_integer() || (value.is_number_integer()&&!value.is_number_unsigned()&&value.get<int64_t>()<0)
        || value.get<uint64_t>()>UINT32_MAX) throw std::runtime_error("Expected an unsigned raw handle/field");
    return value.get<uint32_t>();
}
template<size_t N> void OwnerBindings(std::vector<Binding>& bindings,const Json& state,
    const char* pool,unsigned deadOffset,const Role (&roles)[N]) {
    if(!state.contains(pool)) return;
    const auto& slots=state.at(pool).at("slots");
    for(const auto& item:slots.items()) {
        const auto& entry=item.value();
        if(!entry.contains("fields")) continue;
        const auto id=U32(entry.at("id_or_free_next"));
        if(!(id&0xffff0000u)||std::to_string(id&0xffffu)!=item.key())
            throw std::runtime_error("Owner slot/generation mismatch");
        const auto& fields=entry.at("fields");
        const bool dead=U32(fields.at(Hex(deadOffset)))!=0;
        for(const auto& role:roles) {
            auto key=Hex(role.offset);
            if(!fields.contains(key)) continue;
            bindings.push_back({std::string("/")+pool+"/slots/"+item.key()+"/fields/"+key,
                std::string(pool)+"/"+Hex(id)+"/"+role.name,U32(fields.at(key)),dead});
        }
    }
}
std::vector<Binding> Bindings(const Json& state) {
    std::vector<Binding> bindings;
    OwnerBindings(bindings,state,"zombies",0xec,zombieRoles);
    OwnerBindings(bindings,state,"plants",0x141,plantRoles);
    OwnerBindings(bindings,state,"mowers",0x30,mowerRoles);
    OwnerBindings(bindings,state,"grid_items",0x20,gridRoles);
    if(state.contains("board")&&state["board"].is_object()) {
        for(unsigned offset=0x5624;offset<0x5744;offset+=4) {
            auto key=Hex(offset);
            if(state["board"].contains(key)) bindings.push_back({"/board/"+key,
                "board/fwoosh/"+std::to_string((offset-0x5624)/4),U32(state["board"][key]),false});
        }
    }
    std::sort(bindings.begin(),bindings.end(),[](const Binding& a,const Binding& b){return a.anchor<b.anchor;});
    return bindings;
}
}
bool ValidateReanimationTarget() noexcept {
    // InitializeType: type * 16 + the global definition table, store type, then
    // call Initialize. Verified against the unmodified loaded engine image.
    static constexpr uint8_t bytes[]={0x8b,0xc6,0xd9,0x44,0x24,0x10,0xc1,0xe0,
        0x04,0x03,0x05,0xe8,0x9e,0x6a,0x00,0xd9,0x1c,0x24,0x57,0x89,0x37,
        0xe8,0x75,0x00,0x00,0x00};
    return Accessible(0x471a71,sizeof(bytes))
        &&std::memcmp(reinterpret_cast<const void*>(0x471a71),bytes,sizeof(bytes))==0;
}
Json ReanimationCoverage() {
    return {{"schema","lvz.reanimation-links.v1"},{"complete_animation_state",false},
        {"normalize_scope","semantic identity equivalence for verified animation links; raw handles are not bitwise-compared"},
        {"raw_handle_evidence",{{"path","audit/reanimation-handles.jsonl"},
            {"encoding","initial_plus_json_patch"},{"binding",{"seq","kind","version"}},{"required",true}}},
        {"normalization","verified generation lookup plus persistent owner/role identity and alias graph"},
        {"covered",{"type","anim_time_bits","anim_rate_bits","loop_type","dead","frame_start","frame_count",
            "frame_base_pose","loop_count","last_frame_time_bits","overlay_matrix_bits","track_blend_and_shake_scalars",
            "zombie_plant_mower_grid_board_fwoosh_links"}},
        {"uncovered",{"unreferenced animation semantics","attachment effect graphs","image/font/text override identity",
            "particle/trail pools","reanimation allocations entirely between sampled boundaries"}},
        {"allocation_history","raw allocated IDs, pool headers/free head/next key retained as evidence, not semantic checksum"},
        {"live_validated",false}};
}
namespace {
ReanimationSample ReadSample(uintptr_t address,uint32_t actualId,uint32_t defCount,uintptr_t defs) {
    ReanimationSample result;result.id=actualId;
    result.dead=Read<uint8_t>(address+0x14)!=0;
    const auto type=Read<int32_t>(address);
    const auto definition=Read<uint32_t>(address+0xc);
    result.definitionValid=type>=0&&static_cast<uint32_t>(type)<defCount&&defs
        && definition==defs+static_cast<uintptr_t>(type)*0x10;
    auto& out=result.semantic;
    out={{"type",type},{"anim_time_bits",Read<uint32_t>(address+4)},
        {"anim_rate_bits",Read<uint32_t>(address+8)},{"loop_type",Read<uint32_t>(address+0x10)},
        {"dead",result.dead},{"frame_start",Read<uint32_t>(address+0x18)},
        {"frame_count",Read<uint32_t>(address+0x1c)},{"frame_base_pose",Read<uint32_t>(address+0x20)},
        {"loop_count",Read<uint32_t>(address+0x5c)},{"last_frame_time_bits",Read<uint32_t>(address+0x94)},
        {"is_attachment",Read<uint8_t>(address+0x64)},{"render_order",Read<uint32_t>(address+0x68)},
        {"filter",Read<uint32_t>(address+0x98)},{"definition_valid",result.definitionValid}};
    out["overlay_matrix_bits"]=Json::array();
    for(unsigned at=0x24;at<0x48;at+=4) out["overlay_matrix_bits"].push_back(Read<uint32_t>(address+at));
    out["color_override"]=Json::array();
    out["extra_additive_color"]=Json::array();
    out["extra_overlay_color"]=Json::array();
    for(unsigned at=0;at<16;at+=4) {
        out["color_override"].push_back(Read<uint32_t>(address+0x48+at));
        out["extra_additive_color"].push_back(Read<uint32_t>(address+0x6c+at));
        out["extra_overlay_color"].push_back(Read<uint32_t>(address+0x80+at));
    }
    out["enable_extra_additive_draw"]=Read<uint8_t>(address+0x7c);
    out["enable_extra_overlay_draw"]=Read<uint8_t>(address+0x90);
    out["track_scalars"]=Json::array();
    if(!result.definitionValid) return result;
    const auto trackCount=Read<uint32_t>(definition+4);
    const auto tracks=Read<uint32_t>(address+0x58);
    if(trackCount>4096 || (trackCount&&!Accessible(tracks,size_t(trackCount)*0x60)))
        throw std::runtime_error("Invalid referenced reanimation track storage");
    out["definition_fps_bits"]=Read<uint32_t>(definition+8);
    for(uint32_t index=0;index<trackCount;++index) {
        auto at=tracks+index*0x60;
        Json track=Json::array();
        // blend counters, eight transform floats, shake scalars and render group.
        // Pointer fields 0x28/2c/30/44 and opaque AttachmentID 0x40 are excluded.
        for(unsigned offset=0;offset<0x28;offset+=4) track.push_back(Read<uint32_t>(at+offset));
        for(unsigned offset=0x34;offset<0x40;offset+=4) track.push_back(Read<uint32_t>(at+offset));
        track.push_back(Read<uint32_t>(at+0x48));
        for(unsigned offset=0x4c;offset<0x5c;offset+=4) track.push_back(Read<uint32_t>(at+offset));
        for(unsigned offset=0x5c;offset<0x60;++offset) track.push_back(Read<uint8_t>(at+offset));
        out["track_scalars"].push_back(std::move(track));
    }
    return result;
}
}

void ReanimationAuditor::Reset() {identities_.clear();nextLifetime_.clear();}
ReanimationAudit ReanimationAuditor::Normalize(const Json& ownerState,const ReanimationPoolSnapshot& pool) {
    if(pool.used>pool.capacity || pool.capacity>65536 || pool.count!=pool.slots.size())
        throw std::runtime_error("Invalid reanimation pool snapshot");
    for(const auto& [slot,sample]:pool.slots)
        if(slot>=pool.used || !(sample.id&0xffff0000u) || (sample.id&0xffffu)!=slot)
            throw std::runtime_error("Reanimation DataArray slot/ID mismatch");
    ReanimationAudit result;result.comparable=ownerState;result.coverage=ReanimationCoverage();
    result.raw={{"schema","lvz.reanimation-raw.v1"},{"pool",{{"capacity",pool.capacity},{"used",pool.used},
        {"count",pool.count},{"free_head",pool.freeHead},{"next_key",pool.nextKey}}},
        {"actual_slot_ids",Json::object()},{"links",Json::array()}};
    std::set<uint32_t> currentIds;
    for(const auto& [slot,sample]:pool.slots) {
        currentIds.insert(sample.id);result.raw["actual_slot_ids"][std::to_string(slot)]=sample.id;
    }
    // A vanished ID is retired. If the finite generation space later wraps,
    // seeing the same raw number again still creates a new logical lifetime.
    for(auto& [id,identity]:identities_) if(!currentIds.contains(id)) identity.present=false;
    Json nodes=Json::object(),issues=Json::array();
    for(const auto& binding:Bindings(ownerState)) {
        const auto slot=binding.handle&0xffffu;
        auto found=pool.slots.find(slot);
        const bool validHandle=binding.handle!=0 && found!=pool.slots.end() && found->second.id==binding.handle;
        Json reference;
        Json evidence={{"path",binding.path},{"anchor",binding.anchor},{"raw_handle",binding.handle},
            {"slot",slot},{"owner_dead",binding.ownerDead},{"lookup_matches",validHandle},
            {"actual_slot_id",found==pool.slots.end()?Json(nullptr):Json(found->second.id)}};
        if(!binding.handle) reference={{"status","null"}};
        else if(!validHandle) {
            const char* reason=slot>=pool.capacity?"out_of_range":(found==pool.slots.end()?"not_allocated":"generation_mismatch");
            reference={{"status",binding.ownerDead?"expired":"dangling"}};
            evidence["lookup_failure"]=reason;
            if(!binding.ownerDead) {
                // Keep the raw bad generation in the comparable state: two
                // invalid handles must not become equal through normalization.
                reference["raw_handle"]=binding.handle;
                reference["reason"]=reason;
                reference["actual_slot_id"]=evidence["actual_slot_id"];
                result.valid=false;issues.push_back({{"path",binding.path},{"problem","live_owner_dangling_handle"}});
            }
        } else {
            auto& identity=identities_[binding.handle];
            if(!identity.present) {
                identity.logical=binding.anchor+"#"+std::to_string(nextLifetime_[binding.anchor]++);
                identity.present=true;
            }
            const auto& sample=found->second;
            reference={{"status",sample.dead?"retiring":"live"},{"node",identity.logical}};
            if(!nodes.contains(identity.logical)) nodes[identity.logical]={{"state",sample.semantic},{"owners",Json::array()}};
            nodes[identity.logical]["owners"].push_back(binding.anchor);
            evidence["logical_node"]=identity.logical;
            if(!sample.definitionValid) {
                result.valid=false;issues.push_back({{"path",binding.path},{"problem","definition_type_pointer_mismatch"}});
            }
        }
        evidence["normalized_reference"]=reference;result.raw["links"].push_back(std::move(evidence));
        result.comparable[Json::json_pointer(binding.path)]=std::move(reference);
    }
    result.comparable["reanimations"]={{"schema","lvz.reanimation-links.v1"},{"valid",result.valid},
        {"nodes",std::move(nodes)},{"issues",std::move(issues)}};
    return result;
}
ReanimationAudit ReanimationAuditor::Capture(const Json& ownerState) {
    ReadScope snapshotReads;
    if(!ValidateReanimationTarget()) throw std::runtime_error("Unsupported reanimation target image");
    const auto app=Read<uint32_t>(0x6a9ec0);
    if(!app) throw std::runtime_error("No LawnApp for reanimation audit");
    const auto effects=Read<uint32_t>(app+0x820);
    const auto holder=effects?Read<uint32_t>(effects+8):0;
    if(!holder) throw std::runtime_error("No reanimation holder");
    ReanimationPoolSnapshot pool;
    const auto block=Read<uint32_t>(holder);
    pool.used=Read<uint32_t>(holder+4);pool.capacity=Read<uint32_t>(holder+8);
    pool.freeHead=Read<uint32_t>(holder+0xc);pool.count=Read<uint32_t>(holder+0x10);pool.nextKey=Read<uint32_t>(holder+0x14);
    if(pool.used>pool.capacity||pool.capacity>65536||(pool.used&&!Accessible(block,size_t(pool.used)*0xa0)))
        throw std::runtime_error("Unreadable reanimation DataArray");
    std::set<uint32_t> referenced;
    for(const auto& binding:Bindings(ownerState)) if(binding.handle) referenced.insert(binding.handle);
    const auto defCount=Read<uint32_t>(0x6a9ee4),defs=Read<uint32_t>(0x6a9ee8);
    if(defCount>4096 || (defCount&&!Accessible(defs,size_t(defCount)*0x10)))
        throw std::runtime_error("Invalid reanimation definition array");
    for(uint32_t slot=0;slot<pool.used;++slot) {
        const auto address=block+slot*0xa0;
        const auto id=Read<uint32_t>(address+0x9c);
        if(!(id&0xffff0000u)) continue;
        ReanimationSample sample;sample.id=id;
        if(referenced.contains(id)) sample=ReadSample(address,id,defCount,defs);
        pool.slots.emplace(slot,std::move(sample));
    }
    return Normalize(ownerState,pool);
}
}
