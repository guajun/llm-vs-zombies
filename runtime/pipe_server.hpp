#pragma once
#include "controller.hpp"
#include <windows.h>
#include <atomic>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <thread>
namespace lvz::runtime {
class PipeServer {
public:
    struct Ticket { Json request; std::optional<Json> response; std::mutex mutex; std::condition_variable ready; std::atomic<bool> disconnected=false; };
    void Start();
    void Stop();
    void Drain(Controller& controller);
    ~PipeServer() { Stop(); }
private:
    HANDLE stop_=nullptr;
    std::thread accept_;
    struct Worker { std::thread thread; std::shared_ptr<std::atomic<bool>> done; };
    std::vector<Worker> workers_;
    std::mutex queueMutex_;
    std::deque<std::shared_ptr<Ticket>> queue_;
    std::deque<std::string> disconnects_;
    void Accept();
    void Serve(HANDLE pipe);
    bool Transfer(HANDLE pipe,void* data,DWORD size,bool write);
};
}
