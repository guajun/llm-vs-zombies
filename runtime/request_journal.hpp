#pragma once
#include <windows.h>
#include <algorithm>
#include <array>
#include <atomic>
#include <cstring>
#include <cstdint>
#include <filesystem>
#include <functional>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

namespace lvz::runtime {
struct JournalOptions {
    std::filesystem::path path;
    uint64_t normalBytes = uint64_t(64) << 30;
    uint32_t normalEntries = 262144;
    uint64_t reserveBytes = uint64_t(32) << 20;
    uint32_t reserveEntries = 128;
    // Dependency injection for deterministic disk-failure tests; never exposed over IPC.
    std::function<void(const char*)> beforeIo;
};
struct JournalError : std::runtime_error { using std::runtime_error::runtime_error; };
struct JournalCapacity : JournalError { using JournalError::JournalError; };

// Process/session scoped, append-only exact request/result storage. The fixed
// bucket heads are the entire ID index in RAM; chains and payloads live on disk.
// It is deliberately not a process-crash resume mechanism.
class RequestJournal {
public:
    static constexpr size_t BucketCount = 65536;
    static constexpr uint32_t MaxBody = 4*1024*1024;
    struct Found { uint64_t token=0; std::string payload; std::optional<std::string> response; };
    explicit RequestJournal(JournalOptions options={}) : options_(std::move(options)), heads_(BucketCount,0) {}
    RequestJournal(const RequestJournal&)=delete;
    RequestJournal& operator=(const RequestJournal&)=delete;
    ~RequestJournal() {
        for(auto h:files_) if(h!=INVALID_HANDLE_VALUE) CloseHandle(h);
        if(lock_!=INVALID_HANDLE_VALUE) CloseHandle(lock_);
        if(temporary_) {
            DeleteFileW(path_.c_str());DeleteFileW(ReservePath().c_str());DeleteFileW(LockPath().c_str());
            RemoveDirectoryW(path_.parent_path().c_str());
        }
    }
    void Epoch(uint64_t epoch) {
        epoch_=epoch; std::fill(heads_.begin(),heads_.end(),0);normalCount_=reserveCount_=0;
    }
    const JournalOptions& Limits() const { return options_; }
    const std::string& Fault() const { return fault_; }
    uint64_t Bytes() const { return used_[0]+used_[1]; }
    uint32_t Entries() const { return normalCount_+reserveCount_; }
    bool Sealed() const { return sealed_; }
    uint64_t CloseReserveBytes() const { return std::min<uint64_t>(options_.reserveBytes/4*3,(uint64_t(12)<<20)+16384); }
    const std::filesystem::path& Path() const { return path_; }
#ifdef LVZ_REQUEST_JOURNAL_TESTING
    void TestingWrite(unsigned file,uint64_t offset,const std::string& bytes) { Seek(file,offset);Write(file,bytes.data(),bytes.size()); }
    void TestingTruncate(unsigned file,uint64_t offset) { Seek(file,offset);if(!SetEndOfFile(files_[file])) throw JournalError("fixture truncate failed"); }
#endif

    std::optional<Found> Find(const std::string& id) {
        uint64_t next=heads_[Bucket(id)];
        // Each admitted ID has at most its reservation and its completion.
        const uint64_t bound=uint64_t(normalCount_+reserveCount_)*2+1;
        for(uint64_t visits=0;next;++visits) {
            if(visits>=bound) Corrupt("request journal bucket chain is cyclic");
            auto h=HeaderAt(next);auto storedId=Id(next,h);
            if(storedId==id) {
                if(h.kind!=2) return Found{next,Body(next,h),std::nullopt};
                auto request=HeaderAt(h.request);
                if((request.kind!=1&&request.kind!=3)||request.epoch!=epoch_||Id(h.request,request)!=id)
                    Corrupt("request journal completion has no matching reservation");
                return Found{h.request,Body(h.request,request),Body(next,h)};
            }
            next=h.next;
        }
        return std::nullopt;
    }
    uint64_t Reserve(const std::string& id,const std::string& payload,bool control,bool closing=false) {
        Open();
        if(sealed_) throw JournalError("request journal is sealed");
        if(id.empty()||id.size()>256||payload.size()>MaxBody) throw JournalCapacity("request exceeds journal frame bounds");
        if(fault_.empty()&&normalCount_<options_.normalEntries) {
            try {
                auto token=Append(0,id,payload,closing?3:1,0);++normalCount_;return token;
            } catch(const JournalCapacity&) { if(!control) throw; }
              catch(const JournalError& e) { fault_=e.what();if(!control) throw; }
        } else if(!control) {
            if(!fault_.empty()) throw JournalError(fault_);
            throw JournalCapacity("ordinary request journal ID quota reached");
        }
        auto entryLimit=closing?options_.reserveEntries:(options_.reserveEntries?options_.reserveEntries-1:0);
        if(reserveCount_>=entryLimit) throw JournalCapacity("emergency control ID reserve exhausted; the final slot is reserved for close");
        auto token=Append(1,id,payload,closing?3:1,0,closing);++reserveCount_;return token;
    }
    void Complete(uint64_t token,const std::string& id,const std::string& response,bool closingRecovery=false) {
        auto request=HeaderAt(token);
        if((request.kind!=1&&request.kind!=3)||Id(token,request)!=id)
            Corrupt("completion request identity mismatch");
        if(response.size()>MaxBody) throw JournalCapacity("response exceeds journal frame bounds");
        if((token&ReserveBit)==0&&fault_.empty()) {
            try { Append(0,id,response,2,token);return; }
            catch(const JournalError& e) { fault_=e.what(); }
        }
        // This space was physically written before admitting any mutation.
        // It can hold an in-flight completion plus bounded pause/cancel/close.
        Append(1,id,response,2,token,request.kind==3||closingRecovery);
    }
    void Seal() {
        if(sealed_) return;
        Open();
        Io("seal");
        for(unsigned file=0;file<2;++file) {
            Seek(file,used_[file]);
            if(!SetEndOfFile(files_[file])||!FlushFileBuffers(files_[file])) throw JournalError("cannot durably seal request journal");
        }
        if(lock_!=INVALID_HANDLE_VALUE) { CloseHandle(lock_);lock_=INVALID_HANDLE_VALUE; }
        if(!DeleteFileW(LockPath().c_str())) throw JournalError("cannot remove closed request journal lock");
        sealed_=true;
    }
private:
    static constexpr uint64_t ReserveBit=uint64_t(1)<<63;
    static constexpr uint64_t Start=64;
#pragma pack(push,1)
    struct Header {
        uint64_t next=0,request=0,epoch=0,bodyHash=0,idHash=0;
        uint32_t kind=0,idBytes=0,bodyBytes=0,reserved=0;
        uint64_t checksum=0;
    };
#pragma pack(pop)
    static_assert(sizeof(Header)==64);
    JournalOptions options_;
    std::vector<uint64_t> heads_;
    HANDLE files_[2]={INVALID_HANDLE_VALUE,INVALID_HANDLE_VALUE};
    HANDLE lock_=INVALID_HANDLE_VALUE;
    std::filesystem::path path_;
    uint64_t epoch_=1,used_[2]={Start,Start};
    uint32_t normalCount_=0,reserveCount_=0;
    bool temporary_=false,sealed_=false;
    std::string fault_;
    std::filesystem::path ReservePath()const{return path_.wstring()+L".reserve";}
    std::filesystem::path LockPath()const{return path_.wstring()+L".lock";}
    static uint64_t Hash(const void* data,size_t size) {
        auto* bytes=static_cast<const unsigned char*>(data);uint64_t h=14695981039346656037ull;
        for(size_t i=0;i<size;++i) h=(h^bytes[i])*1099511628211ull;
        return h;
    }
    size_t Bucket(const std::string& id)const{return Hash(id.data(),id.size())%BucketCount;}
    void Io(const char* operation) { if(options_.beforeIo) options_.beforeIo(operation); }
    [[noreturn]] void Corrupt(const std::string& message) { fault_=message;throw JournalError(message); }
    void Seek(unsigned file,uint64_t offset) {
        LARGE_INTEGER at;at.QuadPart=static_cast<LONGLONG>(offset);
        if(!SetFilePointerEx(files_[file],at,nullptr,FILE_BEGIN)) throw JournalError("request journal seek failed");
    }
    void Write(unsigned file,const void* data,size_t size) {
        auto* bytes=static_cast<const char*>(data);
        while(size) {
            DWORD written=0;
            if(!WriteFile(files_[file],bytes,static_cast<DWORD>(size),&written,nullptr)||!written)
                throw JournalError("request journal write failed");
            bytes+=written;size-=written;
        }
    }
    std::string Read(uint64_t token,uint64_t extra,size_t size) {
        unsigned file=(token&ReserveBit)?1:0;auto offset=token&~ReserveBit;
        if(offset<Start||extra>used_[file]||offset>used_[file]-extra||size>used_[file]-offset-extra)
            Corrupt("request journal read outside committed records");
        Io("read");Seek(file,offset+extra);std::string value(size,'\0');size_t done=0;
        while(done<size) {
            DWORD got=0;
            if(!ReadFile(files_[file],value.data()+done,static_cast<DWORD>(size-done),&got,nullptr)||!got)
                Corrupt("request journal read failed");
            done+=got;
        }
        return value;
    }
    Header HeaderAt(uint64_t token) {
        auto bytes=Read(token,0,sizeof(Header));Header h{};std::memcpy(&h,bytes.data(),sizeof(h));
        if(h.checksum!=Hash(&h,sizeof(h)-sizeof(h.checksum))||h.epoch!=epoch_
            ||(h.kind!=1&&h.kind!=2&&h.kind!=3)||!h.idBytes||h.idBytes>256||h.bodyBytes>MaxBody||h.reserved)
            Corrupt("request journal header checksum/schema mismatch");
        return h;
    }
    std::string Body(uint64_t token,const Header& h) {
        auto body=Read(token,sizeof(Header)+h.idBytes,h.bodyBytes);
        if(Hash(body.data(),body.size())!=h.bodyHash) Corrupt("request journal body checksum mismatch");
        return body;
    }
    std::string Id(uint64_t token,const Header& h) {
        auto id=Read(token,sizeof(Header),h.idBytes);
        if(Hash(id.data(),id.size())!=h.idHash) Corrupt("request journal ID checksum mismatch");
        return id;
    }
    uint64_t Append(unsigned file,const std::string& id,const std::string& body,uint32_t kind,uint64_t request,bool closing=false) {
        uint64_t size=sizeof(Header)+id.size()+body.size();auto limit=file?options_.reserveBytes:options_.normalBytes;
        if(file&&!closing) limit-=CloseReserveBytes();
        if(used_[file]>limit||size>limit-used_[file]) throw JournalCapacity(file?"emergency request journal byte reserve exhausted":"ordinary request journal byte quota reached");
        Header h{};h.next=heads_[Bucket(id)];h.request=request;h.epoch=epoch_;h.bodyHash=Hash(body.data(),body.size());
        h.idHash=Hash(id.data(),id.size());
        h.kind=kind;h.idBytes=static_cast<uint32_t>(id.size());h.bodyBytes=static_cast<uint32_t>(body.size());
        h.checksum=Hash(&h,sizeof(h)-sizeof(h.checksum));
        Io(file?"write_reserve":"write_normal");Seek(file,used_[file]);
        Write(file,&h,sizeof(h));Write(file,id.data(),id.size());Write(file,body.data(),body.size());
        auto token=used_[file]|(file?ReserveBit:0);used_[file]+=size;heads_[Bucket(id)]=token;
        return token;
    }
    void Open() {
        if(files_[0]!=INVALID_HANDLE_VALUE&&files_[1]!=INVALID_HANDLE_VALUE) return;
        if(!fault_.empty()) throw JournalError(fault_);
        try {
            path_=options_.path;
            if(path_.empty()) {
                wchar_t temp[MAX_PATH];if(!GetTempPathW(MAX_PATH,temp)) throw JournalError("temporary journal directory unavailable");
                static std::atomic<unsigned> serial{0};
                auto directory=std::filesystem::path(temp)/(L"lvz-request-journal-"+std::to_wstring(GetCurrentProcessId())+L"-"+std::to_wstring(GetTickCount64())+L"-"+std::to_wstring(serial++));
                if(!std::filesystem::create_directory(directory)) throw JournalError("temporary journal directory already exists");
                temporary_=true;path_=directory/L"requests.bin";
            }
            std::filesystem::create_directories(path_.parent_path());
            lock_=CreateFileW(LockPath().c_str(),GENERIC_WRITE,0,nullptr,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,nullptr);
            if(lock_==INVALID_HANDLE_VALUE) throw JournalError("request journal is already locked");
            files_[0]=CreateFileW(path_.c_str(),GENERIC_READ|GENERIC_WRITE,FILE_SHARE_READ,nullptr,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,nullptr);
            files_[1]=CreateFileW(ReservePath().c_str(),GENERIC_READ|GENERIC_WRITE,FILE_SHARE_READ,nullptr,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,nullptr);
            if(files_[0]==INVALID_HANDLE_VALUE||files_[1]==INVALID_HANDLE_VALUE) throw JournalError("cannot create fresh request journal files");
            std::array<char,Start> header{};std::memcpy(header.data(),"LVZREQ01",8);
            Write(0,header.data(),header.size());Write(1,header.data(),header.size());
            if(options_.reserveBytes<Start||options_.normalBytes<Start) throw JournalError("journal quota smaller than file header");
            std::array<char,65536> zeros{};
            for(uint64_t left=options_.reserveBytes-Start;left;) {
                size_t count=static_cast<size_t>(std::min<uint64_t>(left,zeros.size()));Write(1,zeros.data(),count);left-=count;
            }
            if(!FlushFileBuffers(files_[1])) throw JournalError("cannot preallocate emergency request journal reserve");
        } catch(const std::exception& e) { fault_=e.what();throw JournalError(fault_); }
    }
};
}
