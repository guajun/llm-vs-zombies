#pragma once
#include "controller.hpp"
#include <exception>
namespace lvz::runtime {
// Call only after the original callback returns and before the wrapper marks
// Returned. Fail is deferred while InFlight, preserving measured work.
template<class Check> void CheckReturnedFloatingPoint(Controller& controller,Check&& check) {
    try {check();}
    catch(const std::exception& error){controller.Fail(error.what());}
}
}
