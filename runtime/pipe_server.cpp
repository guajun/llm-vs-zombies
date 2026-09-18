#include "pipe_server.hpp"
#include <array>
#include <chrono>
namespace lvz::runtime {
namespace { constexpr DWORD MaxMessage=4*1024*1024; }
void PipeServer::Start() {
    if(stop_) return;
    stop_=CreateEventW(nullptr,TRUE,FALSE,nullptr);
    if(!stop_) throw std::runtime_error("Cannot create pipe stop event");
    accept_=std::thread([this]{Accept();});
}
void PipeServer::Stop() {
    if(!stop_) return;
    SetEvent(stop_);
    if(accept_.joinable()) accept_.join();
    for(auto& worker:workers_) if(worker.thread.joinable()) worker.thread.join();
    workers_.clear(); CloseHandle(stop_); stop_=nullptr;
    std::lock_guard lock(queueMutex_); queue_.clear();disconnects_.clear();
}
bool PipeServer::Transfer(HANDLE pipe,void* buffer,DWORD size,bool write) {
    auto* bytes=static_cast<unsigned char*>(buffer);
    while(size) {
        OVERLAPPED op{}; op.hEvent=CreateEventW(nullptr,TRUE,FALSE,nullptr);
        if(!op.hEvent) return false;
        DWORD transferred=0;
        BOOL success=write?WriteFile(pipe,bytes,size,&transferred,&op):ReadFile(pipe,bytes,size,&transferred,&op);
        if(!success && GetLastError()==ERROR_IO_PENDING) {
            HANDLE events[]={stop_,op.hEvent};
            if(WaitForMultipleObjects(2,events,FALSE,INFINITE)!=WAIT_OBJECT_0+1) {
                CancelIoEx(pipe,&op); GetOverlappedResult(pipe,&op,&transferred,TRUE); CloseHandle(op.hEvent); return false;
            }
            success=GetOverlappedResult(pipe,&op,&transferred,FALSE);
        }
        CloseHandle(op.hEvent);
        if(!success||transferred==0) return false;
        bytes+=transferred; size-=transferred;
    }
    return true;
}
void PipeServer::Accept() {
    std::wstring name=L"\\\\.\\pipe\\llm-vs-zombies-"+std::to_wstring(GetCurrentProcessId());
    while(WaitForSingleObject(stop_,0)!=WAIT_OBJECT_0) {
        for(auto it=workers_.begin();it!=workers_.end();) {
            if(it->done->load()) { it->thread.join(); it=workers_.erase(it); } else ++it;
        }
        if(workers_.size()>=8) { WaitForSingleObject(stop_,10); continue; }
        HANDLE pipe=CreateNamedPipeW(name.c_str(),PIPE_ACCESS_DUPLEX|FILE_FLAG_OVERLAPPED,
            PIPE_TYPE_BYTE|PIPE_READMODE_BYTE|PIPE_WAIT|PIPE_REJECT_REMOTE_CLIENTS,8,65536,65536,0,nullptr);
        if(pipe==INVALID_HANDLE_VALUE) { WaitForSingleObject(stop_,20); continue; }
        OVERLAPPED op{}; op.hEvent=CreateEventW(nullptr,TRUE,FALSE,nullptr);
        BOOL connected=ConnectNamedPipe(pipe,&op);
        DWORD error=GetLastError();
        if(!connected && error==ERROR_IO_PENDING) {
            HANDLE events[]={stop_,op.hEvent}; DWORD ignored=0;
            if(WaitForMultipleObjects(2,events,FALSE,INFINITE)==WAIT_OBJECT_0+1) connected=GetOverlappedResult(pipe,&op,&ignored,FALSE);
            else { CancelIoEx(pipe,&op); GetOverlappedResult(pipe,&op,&ignored,TRUE); }
        } else if(!connected&&error==ERROR_PIPE_CONNECTED) connected=TRUE;
        CloseHandle(op.hEvent);
        if(!connected) { CloseHandle(pipe); continue; }
        auto done=std::make_shared<std::atomic<bool>>(false);
        workers_.push_back({std::thread([this,pipe,done]{Serve(pipe);DisconnectNamedPipe(pipe);CloseHandle(pipe);done->store(true);}),done});
    }
}
void PipeServer::Serve(HANDLE pipe) {
    for(;;) {
        std::array<unsigned char,4> header{};
        if(!Transfer(pipe,header.data(),4,false)) return;
        DWORD length=DWORD(header[0])|(DWORD(header[1])<<8)|(DWORD(header[2])<<16)|(DWORD(header[3])<<24);
        if(!length||length>MaxMessage) return;
        std::string body(length,'\0'); if(!Transfer(pipe,body.data(),length,false)) return;
        auto ticket=std::make_shared<Ticket>();
        try { ticket->request=Json::parse(body); }
        catch(...) { ticket->response=Error("","invalid_json","Payload is not valid UTF-8 JSON"); }
        std::string requestId;
        if(ticket->request.is_object()&&ticket->request.contains("request_id")&&ticket->request["request_id"].is_string()) requestId=ticket->request["request_id"].get<std::string>();
        if(!ticket->response) {
            std::lock_guard lock(queueMutex_);
            if(queue_.size()>=1024) ticket->response=Error(requestId,"queue_full","Runtime queue is full");
            else queue_.push_back(ticket);
        }
        std::unique_lock lock(ticket->mutex);
        while(!ticket->response) {
            if(WaitForSingleObject(stop_,0)==WAIT_OBJECT_0) return;
            ticket->ready.wait_for(lock,std::chrono::milliseconds(20));
            if(!ticket->response && !PeekNamedPipe(pipe,nullptr,0,nullptr,nullptr,nullptr)) {
                ticket->disconnected=true;
                {std::lock_guard queueLock(queueMutex_);disconnects_.push_back(requestId);}
                return;
            }
        }
        std::string response=ticket->response->dump(); lock.unlock();
        if(response.size()>MaxMessage) response=Error(requestId,"response_too_large","Response exceeds 4 MiB").dump();
        length=static_cast<DWORD>(response.size());
        for(int i=0;i<4;++i) header[i]=static_cast<unsigned char>(length>>(8*i));
        if(!Transfer(pipe,header.data(),4,true)||!Transfer(pipe,response.data(),length,true)) return;
    }
}
void PipeServer::Drain(Controller& controller) {
    std::deque<std::string> disconnected;
    {std::lock_guard lock(queueMutex_);disconnected.swap(disconnects_);}
    for(const auto& id:disconnected) controller.Disconnect(id);
    for(int count=0;count<32;++count) {
        std::shared_ptr<Ticket> ticket;
        { std::lock_guard lock(queueMutex_); if(queue_.empty()) return; ticket=queue_.front(); queue_.pop_front(); }
        if(ticket->disconnected) continue;
        controller.Request(ticket->request,[ticket](Json response) {
            { std::lock_guard lock(ticket->mutex); ticket->response=std::move(response); }
            ticket->ready.notify_all();
        });
    }
}
}
