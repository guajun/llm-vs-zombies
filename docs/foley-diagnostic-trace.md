# Foley branch diagnostic

`foley_branch_trace_v1` is an optional observation mode for the locked original engine. It does not restore RNG, change a game branch, call audio-status queries an extra time, or supply synthetic audio results. Native hooks and evidence I/O perturb wall time; this is declared in the manifest and requires a distinct runtime identity from any earlier strict replay.

Default: disabled, no Foley or app-counter patches. Before launching an isolated process, set:

```powershell
$env:LVZ_FOLEY_TRACE = '1'
$env:LVZ_FOLEY_TRACE_BEGIN = '0'
$env:LVZ_FOLEY_TRACE_END = '5000'
```

The interval uses the controlled pre-tick, half-open. Hooks are installed only when enabled and capture starts at the first warm preparation or pre-step, after app/Foley roots are validated. Unlike the earlier MT diagnostic, this mode does not hook every MT extraction or dump 32 stack words for every particle call.

`hello.game.foley_trace` and `audit/manifest.json.foley_trace` contain the mode, enabled/installed state, interval, fixed queue capacity 16384 and nesting limit 16. The manifest explicitly says diagnostic-only, RNG-unmodified and timing-perturbing, and lists all twelve exact-signature hook addresses.

The original sites are PlayFoleyPitch entry (`0x515020`); IsPlaying call preparation and original return (`0x514F9C`, `0x514FA5`); original recent-play result (`0x515050`); variation Rand before/after (`0x515188`, `0x51518D`); actual GetSoundInstance return (`0x5151B5`); the full/reuse/common original return epilogues (`0x5150A4`, `0x5150C0`, `0x51522D`); and immediately before/after the original app mUpdateCount increment (`0x54B983`, `0x54B98A`). Adjacent patches are signature-checked before any are enabled. Register/flags/FX/LastError preservation and matching real callbacks are covered by native fixtures.

Evidence files:

* `audit/foley-events.jsonl`: ordered original callback observations and stable-boundary counter samples (`lvz.foley-event.v1`). Every row has a monotonically increasing ordinal. Callback rows retain phase/version, actual engine call ID when inside the original update/draw, owner thread, invocation ID, actual app update count and global MT cursor. Original result values are not inferred from a later branch.
* `audit/foley-baseline.json`: `snapshots.before_warm` and `snapshots.B0_after_warm` (`lvz.foley-baseline.v1`). These contain actual app/Board/effect/MJ counters, the complete bounded Foley table and params. Actual instance pointers, refcounts, pause flags, start counters, pause offsets and last variations are retained. Resource pointer words and the integers they reference are both preserved. Initial counts and histories are not reset or normalized.
* `audit/foley-trace-health.json`: final receipt (`lvz.foley-health.v1`) and configuration. Complete closure requires enabled/sealed/healthy true, installed false; entered=returned, queries_begun=queries_returned, variations_begun=variations_returned, increments_begun=increments_returned; and zero queued/depth/fault/overflow/wrong-thread counts. `written` equals JSONL lines including `boundary` rows; `boundaries_written` counts that subset. Actual files are included in normal archive hashes.

Slot `creation_generation` is explicitly a diagnostic counter: the number of observed nonnull original GetSoundInstance returns for that type/slot since activation. Baseline instances have generation zero. It is not an undocumented original-game generation field. Its evidence includes the actual allocation return event and retained raw pointer.

The common return site may mean recent-play rejection, failed instance allocation, or successful play. Its meaning must be checked against the recorded original recent AL, LOOP flag, actual variation pair, actual allocation result and after-slots. `recent=true` with LOOP set is allowed to continue. Full/reuse are distinct actual original return sites. The before/after variation pair includes the actual candidate count/list and actual returned index, and verifies exactly one global cursor extraction including twist. A null allocation still follows an actual variation draw.

The app counter trace records the real `add [ESI+0x484],1` instruction and checks that boundary-to-boundary count changes equal the observed increments. The known path is inside AAsm::GameTotalLoop: original LawnApp UpdateFrames `0x452650` calls `0x5D59A0`, which calls `0x54B980`. A normal paused runtime bypasses that update. Initial absolute app counts and initial sound histories can still differ between processes and must be compared as evidence.

An `environment_collect` phase refers to the existing synchronous AvZ collector before `pre_step`. Its original internal game click can play collection sounds without incrementing Board time. That phase is not evidence of an asynchronous paused update. The probe does not move or suppress collection.

Hooks use bounded scalar storage, do not allocate or serialize within an original call, and fail closed for unmatched entries, ownership changes, invalid roots, foreign-thread callbacks or overflow. Existing control RPC service can still close a faulted observation stream with truthful unhealthy health. A file or unhook failure retains the normal recording lock instead of fabricating complete evidence. The next comparison must inspect first branch/input differences; a matched probe pair alone does not solve an earlier uninstrumented divergence.
