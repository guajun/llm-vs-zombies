# Resident runtime

`recorder.dll` now contains the AvZ adapter, local named-pipe server, single-threaded
controller, logger and deterministic audit adapter. The entry point is
`\\.\pipe\llm-vs-zombies-<pid>`. See `docs/runtime-protocol.md` for the JSON envelope.

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
Deduplication retains responses for the current epoch; there is no cross-process
exactly-once claim. The cache rejects new mutations at 100,000 entries or roughly
64 MiB rather than evicting IDs and risking duplicate actions.

Each actual game update is surrounded by `pre_step` and `post_step` audit calls.
The generated AvZ overlay bypasses both AvZ scheduling and `GameTotalLoop` while
waiting in a fight. It does not use advanced pause. An unexpected native
`GameClock` delta stops the budget with `no_game_tick` or `step_count_mismatch` and
returns `ok:false` with the observed result under `error.details` (including any
unexpected overshoot). `exact_step_live_validated` and `strict_determinism`
remain false until independent acceptance. Hidden-window update pumping is a
separate launcher concern, and must be checked on the real engine.

The overlay also removes AvZ's recursive script-lifetime wait and makes card
selection non-blocking. The upstream submodule is untouched; CMake checks SHA-256
for every transformed source. The deterministic adapter verifies target image
signatures before installing AvZ's update hook. Runtime startup pins the DLL, so
hot unloading is deliberately unavailable. Workers stop on the game thread via
explicit shutdown; no worker join is performed by this project's DllMain code.
AvZ's default modal diagnostics are redirected to `build/runtime-diagnostics.log`.

Additional negotiated methods:

- `initialize`: `{game_mode: 13, cards: [...]}`, exact expect, main menu only.
  Completes applying configuration and returns `state: initializing`; this does
  **not** mean loading is finished. Poll `observe.initialization.state` for
  `ready`/`error`. Every card slot must be specified; random autofill is refused.
  Existing saves are loaded through AvZ's normal enter-game path.
- `audit_snapshot`: returns `{state, version}` with the audit adapter's state and
  known RNG representations. This is a diagnostic snapshot, not a restorable
  complete checkpoint.
- `rng_restore`: `{snapshot: ...}`, exact expect, paused fight only. Restores only
  the RNG state recognized by the audit adapter; changes revision. It does not
  rewind the Board or restore all independent RNG objects.
- `stop_recording`: exact expect and no active advancement. Flushes/closes logs,
  removes `capture.lock`, writes `capture.closed`, and leaves IPC observations
  available. Further mutations are rejected. Terminate this isolated process and
  start a new run before further experiments.

Build: `tools/build-avz.ps1`. Native verification:
`ctest --test-dir build/cmake --output-on-failure`. Tests cover actual fragmented
Windows-pipe transfers and disconnect cancellation, deduplication, stale
observations, partial action failures, epoch changes, one-vs-many step budgets,
native clock mismatches, event stops, and closing the recording. These tests use
a fake game backend and do not substitute for live-engine acceptance.

JSON parsing uses nlohmann/json v3.12.0, vendored under `vendor/nlohmann` with its
MIT license. Upstream source:
https://github.com/nlohmann/json/tree/v3.12.0 ; json.hpp SHA-256:
`aaf127c04cb31c406e5b04a63f1ae89369fccde6d8fa7cdda1ed4f32dfc5de63`.
