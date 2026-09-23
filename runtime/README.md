# Resident runtime

`recorder.dll` contains the AvZ adapter, local named-pipe server, single-threaded
controller, logger, deterministic audit adapter and controlled original-frame
capture. The entry point is `\\.\pipe\llm-vs-zombies-<pid>`. See the
[runtime protocol](../docs/runtime-protocol.md) for the JSON envelope and exact
capability contracts.

Only the game thread calls AvZ or reads game memory. A dedicated accept thread and
up to eight connection workers handle overlapped I/O, with a 4 MiB frame limit.
Partial headers/payloads are supported. Disconnects are checked every 20 ms while
waiting for execution; cancellation then takes effect at the next game-thread
boundary. A disconnected queued request is discarded before execution. A
completed request remains queryable with `status` and its request ID.

Mutating requests carry an exact `expect` version. `pause` and `cancel` may omit it
so a second connection can stop a running request. `commit` stops at the first
failed action, retains earlier successes, and performs no requested advancement
after a failure. Responses to `commit` and `advance` describe completed execution.

Completed mutation requests and their exact responses are stored in the
append-only `decisions/runtime-requests.bin` journal. Its in-memory index is
512 KiB; the ordinary limits are **262,144 admitted IDs per epoch** and **64 GiB
of journal bytes**, advertised by `hello.limits`. Lookup checks record integrity;
corruption or I/O failure freezes advancement instead of executing an old ID
again. Ordinary quota exhaustion returns `dedup_capacity` before admitting a new
action. These are capacity bounds, not a guarantee that a full disk can accept
every response.

Every runtime instance owns exactly one **branch scope** (`hello.branch`,
`status.branch`). The dedup namespace is `(branch_id, epoch, request_id)`, and
each journal record stores the scope it was admitted under, so a cloned or
rebound sibling can never answer with the parent's cached result. A request may
declare the scope it belongs to; the declaration is stripped before
canonicalizing, so one logical request stays byte-identical in every branch. A
foreign scope is always an explicit error (`branch_scope_mismatch`), a foreign
record with different content is `cross_branch_request_id_conflict`, and an
inherited record with identical content is re-executed here instead of being
served. A clone changes scope through `branch_rebind`; a fresh process may adopt
the parent's unsealed journal (`LVZ_JOURNAL_ADOPT=1`), whose index is rebuilt
from the records. Legacy `LVZREQ01` files and sealed journals are never adopted.

A physically allocated **32 MiB** reserve supports up to **128 additional control
IDs** and failed completion writes. Its final ID slot and **12 MiB + 16 KiB** are
reserved for closing and outstanding results. If both disk paths fail after an
action, bounded live memory retains the actual result for retry/status while
advancement stays frozen. `stop_recording` must persist outstanding results,
seal both files and remove `runtime-requests.bin.lock` before acknowledging
success; `recording_close_incomplete` leaves failure evidence and the lock.

Exact retries and `status` recover current-epoch results, including after close.
An epoch change resets the ordinary lookup namespace, preserving the old file
evidence; the single verified terminal completion additionally has a bounded
recovery slot across that transition. Changed content with a reused ID is
rejected. Capture pixels use a separate four-response cache: older capture IDs
retain tombstones and return `request_result_expired`, never a fresh capture.
The journal is process/session scoped; it does not implement crash restart or
cross-process exactly-once execution. See the protocol's
[journal contract](../docs/runtime-protocol.md#long-session-mutation-deduplication).

The `controlled_engine_call_v1` wrapper measures entry and return of each original
update and binds its `pre_step/post_step`, hooks and raw Board evidence to a
monotonic call ID. Ordinary steps require the same Board and native `GameClock`
delta one. A verified same-Board terminal delta one remains one tick; a completed
same-Board UI3-to-UI4 terminal call with delta zero is recorded separately without
inventing a tick. Results distinguish `executed_ticks` from
`executed_engine_calls`. Other unverified transitions, clock changes or invocation
faults stop execution with the actual evidence retained. Missing post boundaries
are not fabricated. Terminal execution freezes until a permitted initialization.

The AvZ overlay bypasses scheduling and `GameTotalLoop` while waiting in a fight;
it does not use advanced pause. In `deterministic_draw_schedule_v1`, an explicit
warm draw precedes B(0), each verified ordinary update has one original draw,
and pause/closure deny autonomous fight drawing. Terminal calls have an explicit
skipped-draw receipt. `capture_frame` reads only the current version's cache and
does not render or advance the game. See [video](../docs/video.md) and
[engine replay](../docs/engine-replay.md) for evidence and replay rules.

The broad `exact_step_live_validated`, `strict_determinism` and `checkpoints`
capability flags remain false in the current hello response. Independent live
evidence now includes the 044 5,000-tick cold replay, but full two-flag and ten
cold-start acceptance is incomplete. Hidden-window pumping and observation are
launcher concerns, not a pure windowless-server claim; the measured scope and
remaining gates are in [live validation](../docs/headless-validation.md).

The overlay also removes AvZ's recursive script-lifetime wait and makes card
selection non-blocking. The upstream submodule is untouched; CMake checks SHA-256
for every transformed source. The deterministic adapter verifies target image
signatures before installing AvZ's update hook. Runtime startup pins the DLL, so
hot unloading is deliberately unavailable. Workers stop on the game thread via
explicit shutdown; no worker join is performed by this project's DllMain code.
AvZ's default modal diagnostics are redirected to `runtime-diagnostics.log`
beside the loaded DLL (normally in the run's private module directory).

Additional negotiated methods:

- `branch_rebind`: `{branch_id, from_branch_id, parent_branch_id?}` for a cloned
  or restored process. `from_branch_id` must name the scope this runtime
  currently owns; a paused boundary without pending work and an open, healthy
  recording are required. Records already admitted keep their original scope,
  so history stays unambiguous while later records use the new scope. See the
  [branch scope contract](../docs/runtime-protocol.md#branch-scope-and-dedup-namespaces).
- `initialize`: `{game_mode: 13, cards: [...]}`, exact expect, supported title/main
  menu without a Board only.
  Completes applying configuration and returns `state: initializing`; this does
  **not** mean loading is finished. Poll `observe.initialization.state` for
  `ready`/`error`. Every card slot must be specified; random autofill is refused.
  Existing saves are loaded through AvZ's normal enter-game path.
- `audit_snapshot`: returns `state` and `version`, plus negotiated original-call
  tracker evidence. This captures the adapter's known state and RNG, not a
  restorable complete process checkpoint.
- `rng_seed`, `rng_restore`, `clock_restore`: explicit paused-fight initialization
  operations with exact expect and revision changes. They affect only declared
  RNG instances or clock fields, not the whole Board. The explicit sound-counter
  origin/App anchor seals further RNG/clock initialization in that mode.
- `sound_counter_origin`, `app_update_anchor` and `mj_clock_anchor`: negotiated
  only with the explicit allocation-none sound mode. The first binds the native
  diagnostic counter's experiment origin while preserving absolute raw evidence;
  the second sets the actual initial App update count; the third writes
  `LawnApp+0x838` (the absolute counter `Zombie::GetDancerFrame` reads) to the
  fixed target the experiment declares. All three are guarded one-shot prewarm
  operations with full before/after receipts. Use the shared
  [initialization helper](../src/llm_vs_zombies/initialization.py) for their required
  order (counter origin → App anchor → fixed MJ clock anchor → warm draw);
  contracts are in [counter origin](../docs/sound-counter-origin-native.md),
  [App anchor](../docs/app-update-anchor-native.md) and
  [fixed MJ clock anchor](../docs/mj-clock-anchor-native.md).
- `b0_normalization`: the unified one-table form of the two anchors above.
  Params `{"entries":[{"field":...,"target":...,"reason":...}, ...]}` with the
  declared field set (`/sound_effects/app_update_count`, then `/app/mj_clock`),
  exact `expect`, a bound counter origin and no warm draw yet. The whole table
  is written once in the declared order, consumes exactly one revision and
  records one `lvz.b0-normalization.v1` receipt (`b0_normalized`; failures keep
  the partial write and record `b0_normalization_failed`). A runtime that
  declares it rejects the legacy per-field RPCs (`legacy_anchor_retired`).
  Contract and the read-only recipe completeness checker:
  [B(0) normalization](../docs/b0-normalization-native.md).
- `prepare_render`: exact expect, seeded ready fight at tick zero; verifies RNG
  and performs the one warm draw. Capture B(0) after this succeeds. Actions and
  advancement before preparation are rejected.
- `capture_frame`: exact expect, paused ready fight, open recording and no pending
  advancement; copies the matching cached original BGR24 frame. Mutations can
  invalidate that cache, so an action-only request does not guarantee a new image.
- `stop_recording`: exact expect and no active advancement. Flushes/closes logs,
  removes `capture.lock`, writes `capture.closed`, then completes and seals the
  request journal. IPC observations and existing result lookups remain available;
  new mutations and captures are rejected. Terminate this isolated process and
  start a new run before further experiments.

Build: `tools/build-avz.ps1`. Native verification:
`ctest --test-dir build/cmake --output-on-failure`. Tests cover actual fragmented
Windows-pipe transfers and disconnect cancellation, deduplication, stale
observations, partial action failures, epoch changes, one-vs-many step budgets,
native clock mismatches, measured terminal calls, draw scheduling, storage failure,
event stops and closing the recording. Controller tests use fake game backends;
these and the native fixtures do not substitute for live-engine acceptance.

JSON parsing uses nlohmann/json v3.12.0, vendored under
[runtime/vendor/nlohmann](vendor/nlohmann) with its MIT license. Upstream source:
https://github.com/nlohmann/json/tree/v3.12.0 ; json.hpp SHA-256:
`aaf127c04cb31c406e5b04a63f1ae89369fccde6d8fa7cdda1ed4f32dfc5de63`.
