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
    // Branch-scoped dedup (issue #32). ``branch`` is this instance's scope id;
    // an empty value keeps the pre-branch epoch+request_id namespace, which is
    // exactly the old client/old recording behaviour. ``adopt`` opens an
    // existing unsealed journal left behind by a parent instance and rebuilds
    // the bucket index from its records: the sibling/fork path. ``adoptEpoch``
    // selects which inherited epoch becomes the live index.
    std::string branch;
    std::string parentBranch;
    bool adopt = false;
    uint64_t adoptEpoch = 1;
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
    static constexpr size_t MaxScope = 64;
    static constexpr uint64_t ReserveBit=uint64_t(1)<<63;
    static constexpr uint64_t Start=64;
    struct Found { uint64_t token=0; std::string payload; std::optional<std::string> response; };
    // A same-ID record owned by another branch scope. It is never a hit; it only
    // proves that this ID already denotes something in the journal's lineage.
    struct Foreign { std::string branch; std::string payload; };
    struct Lookup { std::optional<Found> hit; std::optional<Foreign> foreign; };
    // Same grammar as the evidence tree's branch_id: 1-64 characters of
    // [A-Za-z0-9._:-] starting alphanumeric.
    static bool ValidBranch(const std::string& value) {
        auto alnum=[](char c){return (c>='0'&&c<='9')||(c>='A'&&c<='Z')||(c>='a'&&c<='z');};
        if(value.empty()||value.size()>MaxScope||!alnum(value[0])) return false;
        for(char c:value) if(!alnum(c)&&c!='.'&&c!='_'&&c!=':'&&c!='-') return false;
        return true;
    }
    explicit RequestJournal(JournalOptions options={}) : options_(std::move(options)), heads_(BucketCount,0) { scope_=options_.branch; }
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
    const std::string& Branch() const { return scope_; }
    const std::string& Fault() const { return fault_; }
    uint64_t Bytes() const { return used_[0]+used_[1]; }
    uint32_t Entries() const { return normalCount_+reserveCount_; }
    bool Sealed() const { return sealed_; }
    uint64_t CloseReserveBytes() const { return std::min<uint64_t>(options_.reserveBytes/4*3,(uint64_t(12)<<20)+16384); }
    const std::filesystem::path& Path() const { return path_; }
    // Scope changes never rewrite history: existing records keep the scope they
    // were admitted under, so every ID stays ambiguous only inside one lineage.
    void Rebind(const std::string& branch) {
        if(!ValidBranch(branch)) throw JournalError("request journal branch scope is invalid");
        scope_=branch;
    }
#ifdef LVZ_REQUEST_JOURNAL_TESTING
    void TestingWrite(unsigned file,uint64_t offset,const std::string& bytes) { Seek(file,offset);Write(file,bytes.data(),bytes.size()); }
    void TestingTruncate(unsigned file,uint64_t offset) { Seek(file,offset);if(!SetEndOfFile(files_[file])) throw JournalError("fixture truncate failed"); }
    static constexpr size_t TestingFileHeaderBytes=Start;
    static constexpr size_t TestingRecordHeaderBytes() { return sizeof(Header); }
#endif

    Lookup Find(const std::string& id) {
        Lookup result;
        uint64_t next=heads_[Bucket(id)];
        // Each admitted ID has at most its reservation and its completion.
        const uint64_t bound=uint64_t(normalCount_+reserveCount_)*2+1;
        for(uint64_t visits=0;next;++visits) {
            if(visits>=bound) Corrupt("request journal bucket chain is cyclic");
            auto h=HeaderAt(next);auto storedId=Id(next,h);
            if(storedId==id) {
                auto branch=Scope(next,h);
                if(h.kind!=2) {
                    auto payload=Body(next,h);
                    if(branch==scope_) { if(!result.hit) result.hit=Found{next,std::move(payload),std::nullopt}; }
                    else if(!result.foreign) result.foreign=Foreign{branch,std::move(payload)};
                } else {
                    auto request=HeaderAt(h.request);
                    if((request.kind!=1&&request.kind!=3)||request.epoch!=epoch_||Id(h.request,request)!=id)
                        Corrupt("request journal completion has no matching reservation");
                    if(Scope(h.request,request)!=branch) Corrupt("request journal completion scope mismatch");
                    auto payload=Body(h.request,request);
                    if(branch==scope_) { if(!result.hit) result.hit=Found{h.request,std::move(payload),Body(next,h)}; }
                    else if(!result.foreign) result.foreign=Foreign{branch,std::move(payload)};
                }
            }
            next=h.next;
        }
        return result;
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
            // Mark the file as sealed before flushing so an unsealed parent
            // stays adoptable while a finished archive can never be reopened.
            uint32_t closed=1;Seek(file,8);Write(file,&closed,sizeof(closed));
            Seek(file,used_[file]);
            if(!SetEndOfFile(files_[file])||!FlushFileBuffers(files_[file])) throw JournalError("cannot durably seal request journal");
        }
        if(lock_!=INVALID_HANDLE_VALUE) { CloseHandle(lock_);lock_=INVALID_HANDLE_VALUE; }
        if(!DeleteFileW(LockPath().c_str())) throw JournalError("cannot remove closed request journal lock");
        sealed_=true;
    }
private:
#pragma pack(push,1)
    struct Header {
        uint64_t next=0,request=0,epoch=0,bodyHash=0,idHash=0;
        uint64_t scopeHash=0;
        uint32_t kind=0,idBytes=0,bodyBytes=0,scopeBytes=0;
        uint64_t checksum=0;
    };
#pragma pack(pop)
    static_assert(sizeof(Header)==72);
    JournalOptions options_;
    std::vector<uint64_t> heads_;
    HANDLE files_[2]={INVALID_HANDLE_VALUE,INVALID_HANDLE_VALUE};
    HANDLE lock_=INVALID_HANDLE_VALUE;
    std::filesystem::path path_;
    std::string scope_;
    uint64_t epoch_=1,used_[2]={Start,Start};
    uint32_t normalCount_=0,reserveCount_=0;
    bool temporary_=false,sealed_=false;
    std::string fault_;
    std::filesystem::path ReservePath()const{return path_.wstring()+L".reserve";}
    std::filesystem::path LockPath()const{return path_.wstring()+L".lock";}
    static constexpr char Magic[9]="LVZREQ02";
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
        return ReadRange(file,offset+extra,size);
    }
    std::string ReadRange(unsigned file,uint64_t offset,size_t size) {
        Io("read");Seek(file,offset);std::string value(size,'\0');size_t done=0;
        while(done<size) {
            DWORD got=0;
            if(!ReadFile(files_[file],value.data()+done,static_cast<DWORD>(size-done),&got,nullptr)||!got)
                Corrupt("request journal read failed");
            done+=got;
        }
        return value;
    }
    Header HeaderAt(uint64_t token) {
        auto h=HeaderAtRaw(token);
        if(h.epoch!=epoch_) Corrupt("request journal header checksum/schema mismatch");
        return h;
    }
    Header HeaderAtRaw(uint64_t token) {
        auto bytes=Read(token,0,sizeof(Header));Header h{};std::memcpy(&h,bytes.data(),sizeof(h));
        unsigned file=(token&ReserveBit)?1:0;
        if(!HeaderValid(h,used_[file]-(token&~ReserveBit))) Corrupt("request journal header checksum/schema mismatch");
        auto scope=Read(token,sizeof(Header),h.scopeBytes);
        if(Hash(scope.data(),scope.size())!=h.scopeHash) Corrupt("request journal scope checksum mismatch");
        return h;
    }
    static bool HeaderValid(const Header& h,uint64_t remaining) {
        return h.checksum==Hash(&h,sizeof(h)-sizeof(h.checksum))
            &&(h.kind==1||h.kind==2||h.kind==3)&&h.idBytes&&h.idBytes<=256&&h.bodyBytes<=MaxBody&&h.scopeBytes<=MaxScope
            &&sizeof(Header)+uint64_t(h.scopeBytes)+h.idBytes+h.bodyBytes<=remaining;
    }
    std::string Body(uint64_t token,const Header& h) {
        auto body=Read(token,sizeof(Header)+h.scopeBytes+h.idBytes,h.bodyBytes);
        if(Hash(body.data(),body.size())!=h.bodyHash) Corrupt("request journal body checksum mismatch");
        return body;
    }
    std::string Id(uint64_t token,const Header& h) {
        auto id=Read(token,sizeof(Header)+h.scopeBytes,h.idBytes);
        if(Hash(id.data(),id.size())!=h.idHash) Corrupt("request journal ID checksum mismatch");
        return id;
    }
    std::string Scope(uint64_t token,const Header& h) {
        if(!h.scopeBytes) return std::string();
        return Read(token,sizeof(Header),h.scopeBytes);
    }
    uint64_t Append(unsigned file,const std::string& id,const std::string& body,uint32_t kind,uint64_t request,bool closing=false) {
        uint64_t size=sizeof(Header)+scope_.size()+id.size()+body.size();auto limit=file?options_.reserveBytes:options_.normalBytes;
        if(file&&!closing) limit-=CloseReserveBytes();
        if(used_[file]>limit||size>limit-used_[file]) throw JournalCapacity(file?"emergency request journal byte reserve exhausted":"ordinary request journal byte quota reached");
        Header h{};h.next=heads_[Bucket(id)];h.request=request;h.epoch=epoch_;h.bodyHash=Hash(body.data(),body.size());
        h.idHash=Hash(id.data(),id.size());h.scopeHash=Hash(scope_.data(),scope_.size());
        h.kind=kind;h.idBytes=static_cast<uint32_t>(id.size());h.bodyBytes=static_cast<uint32_t>(body.size());
        h.scopeBytes=static_cast<uint32_t>(scope_.size());
        h.checksum=Hash(&h,sizeof(h)-sizeof(h.checksum));
        Io(file?"write_reserve":"write_normal");Seek(file,used_[file]);
        Write(file,&h,sizeof(h));Write(file,scope_.data(),scope_.size());
        Write(file,id.data(),id.size());Write(file,body.data(),body.size());
        auto token=used_[file]|(file?ReserveBit:0);used_[file]+=size;heads_[Bucket(id)]=token;
        return token;
    }
    void Open() {
        if(files_[0]!=INVALID_HANDLE_VALUE&&files_[1]!=INVALID_HANDLE_VALUE) return;
        if(!fault_.empty()) throw JournalError(fault_);
        try {
            if(options_.adopt) { Adopt(); return; }
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
            std::array<char,Start> header{};std::memcpy(header.data(),Magic,8);
            Write(0,header.data(),header.size());Write(1,header.data(),header.size());
            if(options_.reserveBytes<Start||options_.normalBytes<Start) throw JournalError("journal quota smaller than file header");
            std::array<char,65536> zeros{};
            for(uint64_t left=options_.reserveBytes-Start;left;) {
                size_t count=static_cast<size_t>(std::min<uint64_t>(left,zeros.size()));Write(1,zeros.data(),count);left-=count;
            }
            if(!FlushFileBuffers(files_[1])) throw JournalError("cannot preallocate emergency request journal reserve");
        } catch(const std::exception& e) { fault_=e.what();throw JournalError(fault_); }
    }
    // Sibling path: the parent instance released an unsealed journal and this
    // instance continues from it under its own branch scope. The index is
    // rebuilt from the records so inherited same-epoch IDs stay visible as
    // foreign evidence instead of silently missing (which would hide reuse).
    void Adopt() {
        path_=options_.path;
        if(path_.empty()) throw JournalError("adopting a request journal requires an explicit path");
        if(!std::filesystem::exists(path_)) throw JournalError("no request journal to adopt");
        std::filesystem::create_directories(path_.parent_path());
        lock_=CreateFileW(LockPath().c_str(),GENERIC_WRITE,0,nullptr,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,nullptr);
        if(lock_==INVALID_HANDLE_VALUE) throw JournalError("request journal is locked by a live or interrupted owner");
        try {
            OpenAdopted();
        } catch(...) {
            // A refused adoption must not leave a lock behind: the parent's
            // journal stays exactly as adoptable (or as final) as it was.
            if(lock_!=INVALID_HANDLE_VALUE) { CloseHandle(lock_);lock_=INVALID_HANDLE_VALUE;DeleteFileW(LockPath().c_str()); }
            throw;
        }
    }
    void OpenAdopted() {
        files_[0]=CreateFileW(path_.c_str(),GENERIC_READ|GENERIC_WRITE,FILE_SHARE_READ,nullptr,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,nullptr);
        files_[1]=CreateFileW(ReservePath().c_str(),GENERIC_READ|GENERIC_WRITE,FILE_SHARE_READ,nullptr,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,nullptr);
        if(files_[0]==INVALID_HANDLE_VALUE||files_[1]==INVALID_HANDLE_VALUE) throw JournalError("adopted request journal is incomplete");
        for(unsigned file=0;file<2;++file) {
            LARGE_INTEGER size{};if(!GetFileSizeEx(files_[file],&size)) throw JournalError("cannot size the adopted request journal");
            if(size.QuadPart<static_cast<LONGLONG>(Start)) throw JournalError("adopted request journal is truncated");
            used_[file]=static_cast<uint64_t>(size.QuadPart);
            auto header=ReadRange(file,0,Start);
            if(std::memcmp(header.data(),Magic,8)) {
                if(!std::memcmp(header.data(),"LVZREQ01",8))
                    throw JournalError("an LVZREQ01 request journal predates branch scope; it stays archive evidence and is never adopted");
                throw JournalError("adopted request journal header is invalid");
            }
            uint32_t closed=0;std::memcpy(&closed,header.data()+8,sizeof(closed));
            if(closed) throw JournalError("a sealed request journal is final evidence and is never adopted");
        }
        // Rebuild the bucket index the parent held in RAM: append order per
        // file, newest indexed record per bucket. The recorded ``next`` links
        // stay authoritative for the walk itself.
        for(unsigned file=0;file<2;++file) {
            uint64_t at=Start,end=used_[file];
            for(;;) {
                Header h{};std::string id;
                if(!AdoptedRecord(file,at,end,h,id)) break;
                if(h.epoch==options_.adoptEpoch) {
                    if(file) ++reserveCount_; else ++normalCount_;
                    heads_[Bucket(id)]=at|(file?ReserveBit:0);
                }
                at+=sizeof(Header)+h.scopeBytes+h.idBytes+h.bodyBytes;
            }
            if(!file) {
                // The ordinary file grows only by appends, so every byte up to
                // its end must be a validated record.
                if(at!=end) Corrupt("request journal record length does not reach the adopted file end");
            } else {
                // The emergency reserve is preallocated, so its committed end is
                // the first non-record. The untouched tail must still be zeros:
                // that fails closed on a torn or damaged append instead of
                // adopting it as free space.
                if(!ZeroRange(file,at,end)) Corrupt("adopted request journal reserve tail is not empty");
                used_[file]=at;
            }
        }
    }
    // One validated record of an adopted file, or false at the first byte that
    // no longer belongs to a complete record.
    bool AdoptedRecord(unsigned file,uint64_t at,uint64_t end,Header& h,std::string& id) {
        if(end-at<sizeof(Header)) return false;
        auto bytes=ReadRange(file,at,sizeof(Header));std::memcpy(&h,bytes.data(),sizeof(h));
        if(!HeaderValid(h,end-at)) return false;
        auto scope=ReadRange(file,at+sizeof(Header),h.scopeBytes);
        if(Hash(scope.data(),scope.size())!=h.scopeHash) return false;
        auto rawId=ReadRange(file,at+sizeof(Header)+h.scopeBytes,h.idBytes);
        if(Hash(rawId.data(),rawId.size())!=h.idHash) return false;
        auto body=ReadRange(file,at+sizeof(Header)+h.scopeBytes+h.idBytes,h.bodyBytes);
        if(Hash(body.data(),body.size())!=h.bodyHash) return false;
        id=std::move(rawId);
        return true;
    }
    bool ZeroRange(unsigned file,uint64_t offset,uint64_t end) {
        std::array<char,65536> chunk{};
        while(offset<end) {
            auto count=static_cast<size_t>(std::min<uint64_t>(end-offset,chunk.size()));
            auto bytes=ReadRange(file,offset,count);
            if(std::any_of(bytes.begin(),bytes.end(),[](char c){return c!='\0';})) return false;
            offset+=count;
        }
        return true;
    }
};
}
