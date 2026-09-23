// Weak default for the hosted observability contract declared in
// ../hosted_script.hpp: a hosted script that does not define
// lvz::hosted::Observe publishes nothing, so it still links and its run
// produces the activation line alone.
//
// A strong definition in the hosted source itself (for example
// ../hosted/atime_probe.cpp) wins at link time; the hosted build compiles this
// file only together with such a source, and
// tests/avz_hosted_script_tests.cpp asserts the override by calling Observe
// in a binary that links both definitions.
#include "../hosted_script.hpp"

namespace lvz::hosted {
__attribute__((weak)) void Observe(std::string&) {}
} // namespace lvz::hosted
