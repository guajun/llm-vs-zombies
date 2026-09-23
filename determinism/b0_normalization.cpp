#include "b0_normalization.hpp"
#include "app_update_anchor.hpp"
#include "memory.hpp"
#include "launcher/silent_audio.hpp"
#include <algorithm>
#include <cstring>
#include <limits>
#include <vector>
namespace lvz::determinism {
using Json=nlohmann::json;
namespace {
struct Field {const char* pointer;const char* native;uintptr_t offset;};
// The closed field set. A plan naming anything else is rejected before any
// request is sent, and the declared order is the execution order.
const Field Fields[]={
    {"/sound_effects/app_update_count","LawnApp+0x484",0x484},
    {"/app/mj_clock","LawnApp+0x838",0x838}};
constexpr size_t FieldCount=sizeof(Fields)/sizeof(Fields[0]);
bool U32(const Json& value){return value.is_number_integer()&&(value.is_number_unsigned()?value.get<uint64_t>()<=UINT32_MAX
    :value.get<int64_t>()>=0&&value.get<int64_t>()<=UINT32_MAX);}
bool Signed(const Json& value){return value.is_number_integer()&&(value.is_number_unsigned()?value.get<uint64_t>()<=uint32_t(INT32_MAX)
    :value.get<int64_t>()>=0&&value.get<int64_t>()<=INT32_MAX);}
const Field* Lookup(const std::string& pointer){
    for(const auto& field:Fields)if(pointer==field.pointer)return &field;
    return nullptr;
}
const Json& ValueAt(const Json& state,const Field& field){
    const std::string pointer=field.pointer;
    return pointer=="/app/mj_clock"?state.at("app").at("mj_clock"):state.at("sound_effects").at("app_update_count");
}
void WriteValue(Json& state,const Field& field,const Json& value){
    const std::string pointer=field.pointer;
    if(pointer=="/app/mj_clock")state["app"]["mj_clock"]=value;else state["sound_effects"]["app_update_count"]=value;
}
void NoDemo(const Json& demo){
    if(!demo.is_object()||demo.size()!=5)throw std::runtime_error("B(0) normalization missing demo guard evidence");
    for(auto key:{"00000510","00000511","00000578","0000049c","000004a0"})
        if(!demo.contains(key)||!U32(demo.at(key)))throw std::runtime_error("B(0) normalization invalid actual demo field");
    if(demo.at("000004a0").get<uint32_t>()>255)throw std::runtime_error("B(0) normalization demo marker is not a byte");
    if(demo.at("00000510")!=0||demo.at("00000511")!=0)
        throw std::runtime_error("B(0) normalization rejects original demo recording or playback");
}
}  // namespace
Json B0NormalizationManifest(){
    Json fields=Json::array();
    fields.push_back({{"field",Fields[0].pointer},{"native",Fields[0].native},{"value_range",{0,INT32_MAX}},
        {"requires_events",{"rng_seeded","sound_counter_origin_bound"}}});
    fields.push_back({{"field",Fields[1].pointer},{"native",Fields[1].native},{"value_range",{0,INT32_MAX}},
        {"requires_events",{"rng_seeded"}},{"requires_entries",{Fields[0].pointer}}});
    return {{"mode",B0NormalizationMode},{"available",true},{"phase","after_seed_before_warm"},
        {"requires_audio_mode",lvz::silentaudio::Mode},{"once_per_epoch",true},{"ordering","declared_order"},
        {"fields",std::move(fields)},{"original_engine_bitwise_unmodified",false},{"live_verified",false},
        {"receipt_event","b0_normalized"}};
}
Json InitialB0Normalization::Apply(uintptr_t app,const Json& entries,uint32_t seed,bool audioEnabled,bool counterBound,
                                   bool seeded,bool warmed,const std::function<Json()>& capture,
                                   const std::function<Json()>& captureDemo){
    Json receipt={{"schema","lvz.b0-normalization.v1"},{"mode",B0NormalizationMode},{"requested",entries},
        {"before",Json::array()},{"after",Json::array()},{"before_state",nullptr},{"after_state",nullptr}};
    bool wrote=false;
    try {
        if(!audioEnabled||!counterBound||!seeded||warmed||attempted_)
            throw std::runtime_error("B(0) normalization requires allocation-none audio, a bound counter origin, "
                                     "an explicit seed, no warm and an unused table");
        if(!entries.is_array()||entries.empty()||entries.size()>FieldCount)
            throw std::runtime_error("the B(0) table must be a nonempty ordered array of declared entries");
        std::vector<const Field*> table;
        std::vector<uint32_t> targets;
        for(const auto& item:entries) {
            if(!item.is_object()||item.size()!=3||!item.contains("field")||!item.contains("target")||!item.contains("reason"))
                throw std::runtime_error("each B(0) table entry requires exactly field, target and reason");
            if(!item["field"].is_string())throw std::runtime_error("a B(0) field must be a JSON Pointer string");
            const Field* field=Lookup(item["field"].get<std::string>());
            if(!field)throw std::runtime_error("a B(0) field is outside the declared closed field set");
            if(std::find(table.begin(),table.end(),field)!=table.end())
                throw std::runtime_error("a B(0) field appears more than once in the table");
            if(!Signed(item["target"]))throw std::runtime_error("a B(0) target must be an integer in 0..2147483647");
            if(!item["reason"].is_string())throw std::runtime_error("a B(0) reason must be a string");
            const auto text=item["reason"].get<std::string>();
            if(text.empty()||text.size()>200)throw std::runtime_error("a B(0) reason must be 1..200 characters");
            for(unsigned char character:text)if(character<0x20||character==0x7f)
                throw std::runtime_error("a B(0) reason must not contain control characters");
            table.push_back(field);targets.push_back(item["target"].get<uint32_t>());
        }
        // The declared order has to satisfy the declared entries dependency.
        if(table.size()>1&&table[0]==&Fields[1])
            throw std::runtime_error("/app/mj_clock must follow /sound_effects/app_update_count in the declared order");
        const auto demo=captureDemo();NoDemo(demo);
        auto before=capture();receipt["before_state"]=before;
        ValidateEmptyInitialAudio(before);ValidateSeededInitialRng(before,seed);
        for(size_t index=0;index<table.size();++index) {
            const auto& value=ValueAt(before,*table[index]);
            if(!U32(value)||value.get<uint32_t>()>uint32_t(INT32_MAX))
                throw std::runtime_error("a B(0) original value is outside the signed range");
            receipt["before"].push_back({{"field",table[index]->pointer},{"value",value}});
            const uintptr_t address=app+table[index]->offset;
            if(!Accessible(address,4,true)||Read<uint32_t>(address)!=value.get<uint32_t>())
                throw std::runtime_error("a B(0) writable target differs from the actual snapshot");
        }
        wrote=true;
        // Unconditional real writes in the declared order; even a value that
        // already equals its target is written, so no world can consume a
        // different number of one-shot operations.
        for(size_t index=0;index<table.size();++index) {
            const uint32_t target=targets[index];
            std::memcpy(reinterpret_cast<void*>(app+table[index]->offset),&target,4);
        }
        auto after=capture();receipt["after_state"]=after;
        auto expected=before;
        for(size_t index=0;index<table.size();++index) {
            const Json value=ValueAt(after,*table[index]);
            receipt["after"].push_back({{"field",table[index]->pointer},{"value",value}});
            WriteValue(expected,*table[index],targets[index]);
        }
        if(after!=expected)throw std::runtime_error("B(0) normalization changed captured state outside the declared table");
        for(size_t index=0;index<table.size();++index) {
            const auto& value=ValueAt(after,*table[index]);
            if(!U32(value)||value.get<uint32_t>()!=targets[index]||Read<uint32_t>(app+table[index]->offset)!=targets[index])
                throw std::runtime_error("the actual B(0) readback differs from the declared target");
        }
        ValidateEmptyInitialAudio(after);ValidateSeededInitialRng(after,seed);
        applied_=true;
        return {{"ok",true},{"write_attempted",true},{"normalization",std::move(receipt)}};
    } catch(const std::exception& error) {
        return {{"ok",false},{"write_attempted",wrote},{"error",error.what()},{"normalization",std::move(receipt)}};
    }
}
}
