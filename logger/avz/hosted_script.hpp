#pragma once
// Contract between the resident runtime and an optional hosted AvZ script.
//
// The runtime owns the script entry: logger/avz/recorder.cpp defines
// `void AScript()` for the recording bootstrap (run lock, events.jsonl, stop
// key). An upstream AvZ tutorial script defines `AScript()` itself through the
// `ACoScript()` macro, so an unmodified tutorial source cannot be linked next
// to the recorder. A hosted script therefore exposes only its coroutine body
// under the name below; the recorder launches it from inside `AScript()` with
// `ACoLaunch`, which keeps the runtime's frame gate, pause and audit path.
//
// Enable with `-DLVZ_AVZ_HOSTED_SCRIPT=<source>`; see
// docs/avz-script-hosting.md for the exact build line, the verified script
// shape and what the offline test does and does not prove.
#ifdef LVZ_AVZ_HOSTED_SCRIPT
#include <avz.h>

namespace lvz::hosted {
// Entry coroutine of the hosted script. `ACoroutine` is AvZ's own coroutine
// type, so `co_await ATime(...)` stays bound to AvZ's time queue and this
// coroutine only ever resumes while the runtime drives AvZ's frame path.
ACoroutine Script();

// The one call a hosted build adds to AScript(). Kept here so the recorder and
// the offline frame-ownership test launch the script through the same line.
inline void Launch() {
    ACoLaunch(Script);
}
} // namespace lvz::hosted
#endif
