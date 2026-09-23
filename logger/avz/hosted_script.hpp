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
#include <string>

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

// ---------------------------------------------------------------------------
// Hosted observability
//
// The script's state is only readable inside the process, and a headless live
// run has nobody to read it there. A hosted script that wants its progress in
// the run directory therefore also defines:
//
//   namespace lvz::hosted { void Observe(std::string& fields); }
//
// The recorder calls it on the game thread once per granted frame, after AvZ's
// RunScript() has had its chance to advance the coroutine, and writes the text
// to <run>/decisions/hosted-script.jsonl - one line per distinct text. `fields`
// is the current state as JSON object members without the outer braces, e.g.
// "\"resumes\":2,\"finished\":0" - the recorder adds the `script` member, so a
// script must not publish one. It must not contain a newline and must be built
// from stable numbers/strings (std::to_string), because the recorder compares
// it byte-wise with the previous frame to decide whether to write.
//
// Observe must read only the script's own state: the recorder may call it on
// any granted frame, including frames outside a fight, and must not be made to
// touch game memory that is absent there.
//
// logger/avz/hosted/observe_default.cpp supplies a weak definition that
// publishes nothing, so a hosted script without state to report still links
// and its file then holds the activation line alone. See
// docs/avz-script-hosting.md section 7 for the file format and the live run.
void Observe(std::string& fields);
} // namespace lvz::hosted
#endif
