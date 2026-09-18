#pragma once
#include "controller.hpp"
#include <exception>

namespace lvz::runtime {
// A permanent native gate fault must freeze all engine work while leaving
// status, pause and evidence closure available on the same owner thread.
// The healthy path does not drain: normal Boundary processing still comes first.
template<class Check,class Drain>
bool CheckPumpGate(Controller& controller,Check&& check,Drain&& drain) {
    try {check();return true;}
    catch(const std::exception& error) {
        controller.Fail(error.what());drain();return false;
    }
}
}
