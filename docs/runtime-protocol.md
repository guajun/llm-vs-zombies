# Runtime protocol v1

Implementation contract for the native runtime, Python client, replay and tests.

Runtimes declaring `game.fixed_fp.mode=fixed_owner_fp_v1` activate the declared
owner-thread floating-point controls exactly once during `initialize`, after
parameter/title validation and before scenario seeding/entry. The successful
initialize result includes `fixed_fp` activation evidence; `audit_snapshot`
adds a top-level `fixed_fp` activation/health block without altering comparable
game state. Persistent drift stops updates while retaining control/close IPC.
The mode requires native activation, same-sample per-boundary raw evidence and
closed health. Older undeclared records keep their existing contract; see
[fixed owner FP mode](fixed-owner-fp.md) for scope and failure semantics.

- Endpoint: local named pipe `\\.\pipe\llm-vs-zombies-<pid>`.
- Frame: 4-byte little-endian unsigned payload byte length, then UTF-8 JSON; maximum 4 MiB. One request/response per connection at a time. Partial reads/writes must be handled.
- Request: `{ "protocol": 1, "request_id": "unique-string", "method": "observe", "params": {} }`. Mutating methods also send `expect: {"epoch": 1, "tick": 0, "revision": 0}` at the top level.
- Response: `{ "protocol": 1, "request_id": "same-string", "ok": true, "result": {} }`, or `ok:false` and `error:{code,message}`. A completed response describes actual execution, not only queue acceptance.
- `hello`: result includes session, PID, protocol, build/game identity, capability booleans. Unsupported methods fail explicitly.
- `observe`: result is an observation containing `version:{epoch,tick,revision}`, `game_clock`, `game_ui`, `scene`, `wave`, `sun`, `plants`, `zombies`, `seeds`. Preserve exact types and stable object IDs. Controller tick is independent of native GameClock.
- `initialize`: params `{game_mode:13,cards:[...]}`, with `expect`; valid from the supported title/main menu without a Board. Supply exactly all available seed slots. The immediate result confirms configuration acceptance only; poll `observe.initialization.state` for `ready` or `error`. A request to enter a level is not proof that the level loaded or that its scene is correct. Only accepted initialization releases a terminal freeze; a rejected request leaves it frozen. `capabilities.card_resubmit_mid_run` reports whether the runtime can be handed a fresh card selection after a round ends; it is false today, because cards are submitted only while an `initialize` request is pending, so a finished round returns to the next round's card-select screen with no continuation (#97, see [evaluation](evaluation.md)).
- `commit`: params `{actions:[{op:"plant",type:8,row:2,col:5}],advance_ticks:1}`. Shovel action: `{op:"shovel",row:2,col:5,target_type:-1}`. Spawn action: `{op:"spawn",type:1,row:3,col:9}`. Rows/columns use 1-based AvZ coordinates. Check version, execute in order, stop on first failure and skip advancement. Earlier successes do not roll back. Result includes `action_results`, `requested_ticks`, `executed_ticks`, `stop_reason`, `observation`.
- `advance`: params `{max_ticks:100,until:{event:"wave_changed"}}`; until is optional. Result includes requested/executed ticks, stop_reason, observation. Bounded maximum must be documented. No Python callbacks execute inside C++.
- `pause`, `status`, `cancel`: bounded control/status at stable game boundaries. `pause/cancel` may omit `expect` so a second connection can interrupt an active advance. No blocking pipe read on the game thread.
- `audit_snapshot`: returns `{state,version}` from the stable game-thread boundary. This is diagnostic state, not an arbitrary checkpoint restore format.
- `rng_seed`: params `{seed:12345}`, where `seed` is an integer in `0..4294967295` (booleans/floats rejected). Requires exact `expect`, paused active fight, no pending advance, and open recording. It seeds only the target adapter's declared RNG instances. Success increments revision once and records `rng_seeded` with the actual seed.
- `rng_restore`: params `{snapshot:{...}}`, with the same pause/version restrictions. Success increments revision and records `rng_restored`. It restores known RNG instances, not the Board.
- `clock_restore`: params `{snapshot:{...}}`, with the same pause/version restrictions. The target backend validates the entire clock sidecar before writing. Success increments revision and records `clocks_restored`; controller native-clock bookkeeping is synchronized so this explicit clock initialization does not trigger an accidental epoch reset. Controller tick, Board object state, wave timers and object ages are not restored. Use this before the experiment's B(0) marker.
- `prepare_render`: params `{}`, exact `expect`, paused ready fight at tick zero, and no previously completed warm draw. Requires the negotiated `deterministic_draw_schedule_v1` capability. After `rng_seed`, optional `clock_restore`, and verification of the seeded snapshot, it performs exactly one original engine warm draw and creates the initial frame cache. Native code independently verifies all 624 MT words/cursor and the game-thread CRT against the last explicit paused-fight seed **before** drawing. It increments revision and returns `{prepared:true,render:<receipt>,observation}`. Capture the experiment B(0) marker after preparation. A failed warm draw freezes the run; repeated identical request IDs recover the prior result without drawing again. Unprepared `advance`/`commit` returns `render_not_prepared`.
- `b0_normalization`: params `{"entries":[{"field":"/sound_effects/app_update_count","target":2048,"reason":"..."}, ...]}`, exact `expect`, paused ready fight at tick zero, a bound sound-counter origin and no entered engine call. Requires the negotiated `initial_b0_normalization_v1` + `b0_normalization` capabilities. The table is one to two declared JSON Pointer leaves in the runtime's closed field set; `/app/mj_clock` must follow `/sound_effects/app_update_count`. It writes every entry in the declared order — even a value that already equals its target — consumes exactly one revision, records `b0_normalized` with one `lvz.b0-normalization.v1` receipt (`requested`/`before`/`after`/`before_state`/`after_state` plus the controller's `before_version`/`after_version`), and returns `{normalized:true,normalization:<receipt>,observation}`. A shape, range, duplicate-field or ordering error is a precondition rejection (no write, no revision); a post-write verification failure keeps the partial write, increments revision, records `b0_normalization_failed` and freezes the run. Declaring this capability retires the legacy `app_update_anchor`/`mj_clock_anchor` paths for the process (`legacy_anchor_retired`); one run never mixes the two shapes.
- `stop_recording`: requires exact `expect` and no pending advancement; flushes/closes native recording. Further state mutations and captures are rejected, and the engine will not resume automatically.
- Mutations deduplicate same epoch/request_id and identical request content. Changed payload with reused ID fails. New epoch on Board/readiness transitions or an external backward native clock; stale observations fail. RNG and clock sidecar operations retain the epoch and explicitly increment revision. State-changing same-tick actions increment revision.
- Every action, including failed attempts, goes through the same executor for REPL and replay. Preserve request/execution boundaries and ordinal in logs. Strict determinism and checkpoints remain false until independently verified.

## Spawn action

`{op:"spawn",type:<AvZ zombie enum>,row:<1..5|6>,col:<1..9>}` creates one
zombie through AvZ's original primitive `AAsm::PutZombie(row,col,type)`
(`avz/framework/inc/avz_asm.h:106-109`, `avz/framework/src/avz_asm.cpp:371-384`),
which forwards to the locked engine's `Challenge::IZombiePlaceZombie` at
`0x42A0F0` (`work/replay-research/Lawn_Challenge.cpp:4376`). `capabilities.spawn_action`
advertises the op.

- The AvZ/engine primitive takes **0-based** grid indices (`PutZombie(1,1,APJ_0)`
  means row 2 column 2), while the wire keeps the ordinary 1-based AvZ
  coordinates used by `plant`/`shovel`; the runtime subtracts one. The zombie's
  engine row is the requested row, and its x position is
  `GridToPixelX(col-1,row-1) - 30`, so it appears inside the requested column
  instead of at the natural off-screen spawn x.
- `type` is the numeric AvZ `AZombieType` (`0`..`32`, `AZOMBIE`..`AGIGA_GARGANTUAR`).
  It is not a seed-slot index and it is not the plant enum. Booleans, floats and
  out-of-range values fail.
- No seed packet, sun, cooldown or selection state is involved, and
  `ASetZombies` is **not** required: the primitive creates the zombie directly
  in the running fight. `ASetZombies` rewrites the level's wave list and is
  unrelated to this op; the runtime never calls it.
- An ordinary spawn consumes the engine's global RNG (the variant draw in
  `Board::AddZombieInRow` plus the initialized zombie's own draws), so a spawn
  is a state change like any other and must stay inside the append-only action
  order. `ZOMBIE_BOBSLED_TEAM` (13) additionally creates its three riders, and
  type-specific initialization may draw more.
- `action_results[i]` is `{ok:true,zombie_id,type,row,col,engine_row,engine_col}`
  or `{ok:false,error:<code>}`. Failure codes are `invalid_spawn_type` (missing,
  non-integer or outside `0..32`), `invalid_spawn_position` (row/column missing,
  non-integral or outside the scene's rows / `1..9`), `spawn_row_unavailable`
  (the engine's `Board::RowCanHaveZombies` rejects the row, for example row 6 of
  the ordinary five-row lawn, which is dirt) and `zombie_pool_full` (the pool
  cannot hold the zombie the op would create). A rejected action stops the request with
  `stop_reason:"action_failed"` and still appears in the audit.
- `zombie_pool_full` is not cosmetic: `Board::AddZombieInRow` returns null once
  `mSize >= mMaxSize - 1` and the primitive dereferences that null pointer
  immediately. The runtime therefore refuses the action whenever the pool
  cannot hold the new zombie plus the engine's reserved last slot (plus three
  more for a bobsled team) instead of letting the game thread crash.
- Audit: every attempt is one `action` audit record with the request id, ordinal,
  submitted action and result, exactly like `plant`/`shovel`. The native run log
  also gains an `action` event whose `op` is `spawn`, and the installed
  `ZombieInitialize` exit hook records the real birth (`zombie_initialized`)
  with the measured row/type/position, so a claimed spawn can be compared with
  the engine's own evidence.
- A successful spawn returns the new object id. A client must not infer success
  from the IPC response alone; it is the `action_results` entry that is
  authoritative.
- `spawn` has no journal of its own: it is an ordinary `commit` action, so it
  keeps the exact `expect` check, epoch/revision handling, same-ID dedup and
  ordering rules of `plant`/`shovel`. Two spawns with the same `request_id` and
  identical content replay the stored result instead of creating a second
  zombie; reusing the ID with different content fails.

## Branch scope and dedup namespaces

A runtime instance owns exactly one **branch scope**. The dedup namespace is
`(branch_id, epoch, request_id)`, and a version triple is only comparable inside
one scope: the full version namespace is `(branch_id, epoch, tick, revision)`.
This exists because a cloned process inherits the parent's journal, epoch, tick
and revision; without a scope it could answer with a result it never executed.

- `hello.branch` is `{schema:"lvz.branch-scope.v1", mode:"branch"|"unscoped",
  branch_id, parent_branch_id, origin:"session"|"adopted"|"rebound",
  dedup_key}`. `dedup_key` is `branch_id+epoch+request_id` for a scoped runtime
  and `epoch+request_id` for an unscoped one. `capabilities.branch_scope_v1` is
  always true; `capabilities.branch_rebind` advertises the control below.
  `status` repeats the same block, and `status` with `params.request_id`
  additionally reports `request_state:"foreign_branch"` plus `foreign_branch_id`
  when the ID exists only in another scope: a foreign record is evidence, never
  an answer, and `response` is never returned for it.
- A request may carry a top-level `branch`. It is a namespace claim, not
  content: the runtime strips it before canonicalizing, so the same logical
  request is byte-identical in every branch. Omitting it keeps the pre-branch
  contract (the runtime's own scope). A declaration that is not a valid scope id
  is `invalid_request`; a declaration that differs from the runtime's own scope
  is `branch_scope_mismatch` with `details.request_branch`,
  `details.runtime_branch` and, when the ID exists in the declared scope,
  `details.foreign_record_branch` / `details.foreign_record_content_matches`.
  Such a request is never executed, never journaled and never answered from the
  foreign scope.
- A same-ID record in another scope is never a hit. Identical content (after the
  scope declaration is stripped) is re-executed and bound to this branch, which
  is what lets two siblings inject the same `request_id` and each receive its
  own result. Different content returns `cross_branch_request_id_conflict` with
  `details.foreign_branch_id`; refusing is deliberate, because one ID must not
  mean two things inside one tree and a foreign result must never be served.
  Same-scope retries keep exactly the old behavior and codes
  (`request_id_conflict`, `dedup_storage_failed`, `dedup_capacity`).
- `branch_rebind` takes `{branch_id, from_branch_id, parent_branch_id?}`.
  `from_branch_id` must equal the scope this runtime currently owns, so a clone
  states what it is leaving instead of silently re-labelling history. It
  requires a stable boundary with no pending work, an open recording and a
  healthy journal; otherwise it returns `busy`, `recording_closed`,
  `branch_rebind_rejected` or `dedup_storage_failed`. The RAM-side caches
  (hot results, capture cache, bounded terminal reply) move with the identity;
  every journal record keeps the scope it was admitted under. Repeating the
  current scope is a no-op success with `rebound:false`.
- Identity comes from `LVZ_BRANCH_ID` when the orchestrator sets it. Otherwise
  the runtime derives a process-instance label (session directory plus PID), so
  two live processes never share a scope by accident. `LVZ_BRANCH_PARENT_ID`
  records the scope a clone was taken from.
- The public launcher sets `LVZ_BRANCH_ID` before the engine's primary thread
  resumes: the default is the run directory name, the pre-resume native receipt
  records the injected id, and the launcher refuses a run whose `hello.branch`
  names a different scope (see `docs/launcher.md` §分支身份).
- Nothing about the branch enters comparable state: the native audit manifest,
  state digests, draw receipts and `engine_call` evidence stay unchanged, so old
  and new recordings remain comparable. The scope is recorded in `hello` (and
  therefore in a run manifest's `branch` block), in the trajectory initial
  marker's optional `branch` field, and in a replay report's `branch_scope`
  block, which reports `runtime_branch_id`, `source_branch_id` and the
  `same_branch`/`other_branch`/`unknown_source_branch` relation.

## Verified terminal boundaries

A fight ending during a requested update is not automatically an unmeasured tick. When the same non-null Board survives and its actual GameClock delta is exactly one, the controller advances its old epoch/tick, writes `post_step`, then writes:

```json
{"kind":"terminal_transition","payload":{"request_id":"...","native_tick_delta":1,"tick_delta_verified":true,"board_identity_preserved":true}}
```

The surrounding audit envelope retains the old epoch's measured final tick. `request_completed` follows with `stop_reason:"scene_changed"`, the actual executed count, and the final observation at that same version. Only after completing the response does `Boundary()` change the epoch and reset controller tick/revision for the new scene. Clients must `observe()` again before a new mutation such as `stop_recording`.

If the Board was destroyed/replaced, `terminal_transition` instead records `native_tick_delta:null`, `tick_delta_verified:false`, and `board_identity_preserved:false`; no `post_step` is invented from a different Board's clock. A retained Board with a zero/negative/multiple delta records the actual diagnostic delta with `tick_delta_verified:false`. Strict replay must reject both uncertain cases.

After a measured or uncertain terminal transition, `status.state` is `terminal_frozen`. Engine/menu/card-selection updates remain stopped; IPC and observations remain available. Startup outside a fight can still run to initialize. Accepted `initialize` releases the terminal freeze; a new active fight again pauses at its first stable boundary. A frozen replacement Board cannot accept an advance that would remain pending forever.

## Original-frame capture

`capture_frame` accepts backend capture options in `params` and requires exact `expect`, a paused active fight, no pending advancement, and open recording. The controller supplies `result.version`. Successful capture contains `capture_ok:true`, `source:"original_game_frame"`, image dimensions, `pixel_format:"bgr24"`, row stride, origin, method, and `pixels_base64`, plus available render/state-check metadata. Unsupported/unsuitable capture can return a successful protocol envelope with `capture_ok:false` and a reason; this is not a usable image.

Capturing without a game-state change does not increment revision. If the backend explicitly reports changed known RNG or clocks, or the controller independently detects a changed native clock/Board/readiness, the image is rejected as `capture_changed_game_state`, its pixels are removed, and revision increments. A backend exception also invalidates the old revision because the render may have partially executed. Call `observe()` after a rejected/uncertain capture before considering further commands. Native audit files never contain pixel/base64 payloads.

Frame responses have a separate dedup cache so video cannot consume the mutation journal's byte budget. Only the latest **four** capture responses are retained, with up to 100,000 request-ID tombstones and 16 MiB of capture request metadata per epoch. A capture request is at most 4096 bytes; each response obeys the ordinary 4 MiB frame bound. An identical expired request ID returns `request_result_expired` and never renders again. Reusing an ID with changed content or another method remains `request_id_conflict`. These limits appear in `hello.limits`. A new epoch clears both action and capture dedup domains.

In `deterministic_draw_schedule_v1`, capture only copies the latest CPU-owned original-frame cache. Success requires `method:"cached_controlled_engine_frame"`, `forced_render:false`, the negotiated `mode`, and `frame_version` exactly equal to the current epoch/tick/revision. It never triggers a draw or flush. RNG/clock mutations, attempted actions, epoch/terminal changes and failed rendering invalidate the cache. An action-only zero-tick command therefore cannot immediately produce a fresh picture; capture returns `capture_ok:false` and `frame_cache_stale` or `frame_cache_unavailable: <reason>` until a verified update renders a new frame. The older mode that forced drawing on capture is an intervention and is not interchangeable with this mode.

This contract may gain methods through capability negotiation. It does not itself certify exact stepping or deterministic playback.

## Controlled original drawing

`hello.game.draw_schedule` and `audit/manifest.json.draw_schedule` describe `mode:"deterministic_draw_schedule_v1"`, `installed`, `original_engine_bitwise_unmodified:false`, `target_entry_rva:1281712`, `autonomous_fight_draws:false`, `capture:"cached_bgr24_only"`, `rng_restore_after_draw:false`, and the fixed schedule. `live_verified:false` is not a live acceptance claim. The separate particle-shake execution mode remains in force.

An exact-signature hook at original `WidgetManager::DrawScreen` (0x538eb0, stack argument and `ret 4`) permits initialization UI drawing until the first ready fight. At that moment it latches: automatic drawing is denied during play, pause, disconnect, faults and after recording closure. Only the owning game thread's explicit controlled-render scope may call the original trampoline. Wrong-thread, reentrant or modified-hook calls fail closed. This controls original engine drawing without window/input automation.

Every successful nonterminal native update is `pre_step audit → original update → one full original draw/flush/read → post_step audit`. The last three operations happen before delivering a completed advance. Drawing's actual RNG consumption and rendering-state writes remain in state; RNG is never restored to conceal them. App/Board/manager/DD/image/surface identity and all three clocks are checked around drawing. Every update reads one fixed 800×600 BGR24 frame into a bounded 1,440,000-byte latest-frame buffer even when video is disabled, so video sampling does not alter the simulation schedule. Rendering and pixel-read cost is paid once per update; live overhead requires measurement.

The **pre_step hook boundary stays active through update and drawing**, including original zombie initializers and particle-shake calls. The controller's post version is used to stamp pixels, but no intervening `Audit` call relabels hook events. Only the existing `post_step` drains events and clears the boundary. Its payload contains a `render` receipt:

```json
{
  "schema":"lvz.controlled-render.v1", "mode":"deterministic_draw_schedule_v1",
  "phase":"step", "frame_version":{"epoch":2,"tick":1,"revision":0},
  "native_clock":3152, "width":800, "height":600, "pixel_format":"bgr24",
  "clocks_before":{}, "clocks_after":{}, "rng_before":{}, "rng_after":{},
  "rng_unchanged":true, "rng_restored":false,
  "counts":{"mode":"deterministic_draw_schedule_v1","warm_frames":1,"step_frames":1}
}
```

The clock objects use the complete `CaptureClocks` schema; before and after must be identical. RNG objects contain the existing FNV-1a-64 canonical-JSON `Digests` of `CaptureRng().instances`, including `global_mt`, `game_thread_crt` and `all`. `rng_unchanged` can be false: readers compare the actual state rather than undoing it. Receipts contain no timing or pixel data.

Warm preparation emits `render_preparing`, performs the draw with initialization-labelled hooks, then emits `render_prepared` with `{request_id,render}`. Its receipt uses `phase:"warm"` and adds `seed_readback:{seed,global_mt_words:624,global_mt_cursor:624,game_thread_crt:<seed>,verified_before_draw:true}`. All warm initialization events must precede the first controlled pre-step. `state.draw_schedule` retains `mode,warm_frames,step_frames`; B(0) has 1/0, and each normal update increments only `step_frames`.

A same-Board terminal update with verified delta1 invalidates the cache without drawing the now-non-ready scene. Its post receipt is exactly the terminal variant: `schema`, `mode`, `phase:"terminal"`, `skipped:true`, `reason:"left_ready_fight"`, `frame_version`, `native_clock`, `cache_invalidated:true`. It does not increment draw counts and is valid only with the immediate corresponding verified `terminal_transition`, `scene_changed` completion, and no later updates/actions. Delta0, negative/multiple delta or a different Board still cannot certify a simulated tick.

If rendering fails after an update, `render_failed` records the actual execution count, no successful post snapshot/frame is invented, and the pending request returns the controller fault with actual `executed_ticks`. Closure remains available. Successful close order is `recording_closed → draw_schedule_closed → particle_shake_closed → spawn_hook_closed`. Drawing health requires `installed,latched,sealed,healthy:true`, `active:false`, `faults=wrong_thread_calls=0`, and `controlled_calls=warm_frames+step_frames`. `automatic_allowed` and `automatic_denied` are retained diagnostic counts driven by outer-pump timing; they do not enter state digests or cross-run semantic equality. The gate stays installed after close until the owned process exits.

## Long-session mutation deduplication

Completed mutation responses are stored in `decisions/runtime-requests.bin`, not retained as an ever-growing tree of observations in the 32-bit process. The in-memory index is exactly 65,536 bucket heads (512 KiB). Bucket chains, exact canonical requests, and complete original responses live in the append-only file. Lookup validates record headers, scopes, IDs, and bodies before returning an old result; a damaged or truncated record freezes advancement instead of treating that ID as new. Same-ID retries and `status` with `params.request_id` can recover the exact original response throughout the current epoch and scope, including after recording closes; a record from another scope is reported but never returned. Epoch changes reset the index namespace while preserving the earlier file evidence.

Default ordinary limits are **262,144 admitted IDs per epoch** and **64 GiB of journal bytes**, permitting 200,000 single-tick requests plus initialization and controls without retaining their observations in RAM. These are explicit bounds, not a promise that an unavailable/full disk can accept every possible 4 MiB response. Admission failure happens before the action. Ordinary quota exhaustion reports `dedup_capacity`.

Before admitting the first mutation, the journal physically allocates a separate **32 MiB** `.bin.reserve` file. It allows up to **128 additional control IDs** when ordinary storage is unavailable or exhausted; it also holds an already admitted request's completion if the ordinary write fails. `pause`, `cancel`, and `stop_recording` may use this reserve. The last ID slot and **12 MiB + 16 KiB** are reserved exclusively for close and saving outstanding final results, so pause traffic cannot consume the closing path. Control requests are limited to 4096 bytes. Reserve quotas remain strict. An I/O fault freezes further state changes and appears as `status.state:dedup_storage_failed`. If both writes fail after an action, the controller retains its exact actual result in bounded live memory so retry/status does not execute it again. While a RAM-only final result exists, new pause/cancel IDs are rejected; the simulation is already frozen. A later successful close must persist those outstanding results before sealing. Failure of the actual emergency disk path is reported; it is not converted into a successful close.

`decisions/runtime-requests.bin.lock` prevents premature archive finalization. Closing first closes native evidence, then writes the actual final response, flushes/seals both journal files, removes its own lock, and only then acknowledges success. If this last stage fails, `recording_close_incomplete` identifies that native recording may already be closed while the journal remains unsealed; the lock stays present. After successful close, every new mutation/control ID is rejected without a journal write. Existing IDs and status lookups remain read-only.

The journal is scoped to a running process/session. Completed records are synchronously written to the OS file cache, and final seal uses `FlushFileBuffers`; this does **not** implement restart after a process crash or promise survival of an unsealed power loss. An interrupted run retains its lock and failure evidence. Both files are included in the ordinary archive inventory and SHA-256 seal; deterministic replay continues to use the authoritative native audit and session trace. Journal storage and status counters do not enter game-state digests.

The binary format begins with a 64-byte `LVZREQ02` header: the magic at offset 0 and a little-endian uint32 sealed state at offset 8 (`0` open, `1` sealed). Each little-endian 72-byte record header contains `next`, `request`, `epoch`, `bodyHash`, `idHash`, `scopeHash` (six uint64 values), `kind`, `idBytes`, `bodyBytes`, `scopeBytes` (four uint32 values), and a uint64 header checksum. It is followed by the branch-scope bytes, the UTF-8 ID bytes and the canonical request or response body; the canonical body excludes the request's `branch` declaration, which is namespace metadata rather than content. Kind 1 reserves an ID, kind 3 reserves a closing ID, and kind 2 points to its reservation and stores its response. Offset bit 63 selects the reserve file. A completion must repeat its reservation's scope. Record integrity uses FNV-1a-64; archive authenticity/integrity uses the enclosing SHA-256 inventory.

`LVZREQ01` files predate branch scope. They remain valid archive evidence and are never adopted, reopened or rewritten in place. `JournalOptions.adopt` (environment `LVZ_JOURNAL_ADOPT=1`, with `LVZ_JOURNAL_EPOCH` selecting the inherited epoch, default 1) reopens an **unsealed** journal whose previous owner released it: it refuses `LVZREQ01`, sealed, truncated, damaged and still-locked files, rebuilds the bucket index from the validated records of that epoch, keeps older records as unreachable evidence, and requires the preallocated emergency reserve tail to be untouched zeros so a torn append fails closed. Adopted records keep their original scope, which is why an inherited same-ID record becomes foreign evidence for the adopting branch. No existing recording is automatically reopened or resumed.
# Controlled original-call boundaries (issue #11)

New recordings explicitly negotiate `game.engine_call_boundary.mode` and
`capabilities.controlled_engine_call_v1`. The mode is
`controlled_engine_call_v1`; it requires `deterministic_draw_schedule_v1`.
The pinned AvZ overlay routes the original `0x452650` update through one checked
wrapper. A completed engine call means that this invocation entered and
returned. It does not claim that all internal Board subsystems advanced.
Initialization, warm drawing, RPCs and action-only requests consume no call IDs.
The uint64 call IDs increase across controller epochs in the same process.

`pre_step.payload.engine_call` has schema `lvz.engine-call.v1` and:

- `engine_call_id`, `request_call_index` (1-based), and `pre_version`;
- `lifecycle: "prepared"`, `engine_call_entered: false`,
  `engine_call_completed: false`;
- `native_clock_before`, `native_clock_after: null`, `native_tick_delta: null`,
  `clock_delta_measured: false`, `board_identity_preserved: null`;
- `ready_before: true`, `game_ui_before: 3`, `clocks_before` (full native
  game/effect/MJ clock snapshot), `clocks_after: null`;
- `entered_calls_total` and `returned_calls_total` before this invocation.

The matching post keeps the same ID, request index, pre-version and before
facts. It reports lifecycle `returned`, both invocation booleans true, actual
after clocks, ready/UI values, measured delta, Board identity predicate, updated
global counts and `transition_kind`. No Audit event changes hook labels between
pre, original update, and the fixed ordinary draw. Spawn and particle semantic
payloads add `engine_call_id`; their raw evidence contains the same value.
Initialization/warm/action/request-start payloads carry null. This identity is
excluded from the existing canonical particle scalar digest and verified
separately.

The required `audit/engine-call-raw.jsonl` stores one record per pre/post:
schema `lvz.engine-call-raw.v1`, matching `seq`, `kind`, `version`, top-level
`engine_call_id`, and payload `{board_address_before, board_address_after}`.
Pre after-address is null; a verified post has the same nonzero before/after
address. These addresses are evidence within a process, not cross-process
semantic state. The internal transport key `_engine_call_raw` is stripped from
the authoritative semantic events/checksums/deltas and logger records.

Ordinary same-Board ready updates require GameClock delta1 (`clock_step`).
Same-Board terminal delta1 retains `terminal_clock_step` and one measured tick.
The new zero-clock admission is narrowly UI3→UI4, same nonnull Board pointer,
delta0 and a proven completed call with remaining request budget. It records
`terminal_zero_clock_update`, a complete post state and RNG at the unchanged
controller tick/revision, followed by `terminal_transition` and a single
`request_completed` with `stop_reason: "scene_changed"`. The transition reports
`clock_delta_measured: true`, `tick_delta_verified: false`,
`terminal_call_verified: true`. A pointer match is not an invented allocator
generation. Other zero-clock UI pairs remain unsupported.

Both admitted terminal cases use the explicit skipped render receipt (phase
`terminal`, reason `left_ready_fight`, cache invalidated); no draw count or image
is fabricated. Further updates/actions stay frozen. Ordinary delta0, negative or
multiple ticks, changed/null Boards, unmatched calls and invocation faults are
diagnostic failures. Old recordings, including 025, do not gain this contract
retroactively.
If Board/readiness/clock changes outside a measured call while a request is
pending, the runtime records `engine_call_fault` and returns an error retaining
actual prior work; it cannot certify a successful terminal call from that change.

Every completed advance/commit result adds `executed_engine_calls`,
`terminal_zero_clock_calls` and `last_engine_call_id` (null when that request
returned zero calls). Terminal completions also include `terminal_kind`.
`executed_ticks` remains measured GameClock progress: E ticks plus one zero-clock
terminal means E+1 calls. Exact retries/status recover the original journaled
result and never invoke the engine. Error details following entered/returned
work retain the actual counts. A nested callback is rejected before scheduling,
IPC or another update; its fault is deferred until the outer call returns and
its clocks have been measured. Pre-audit failure does not count an entry, and an
exception without normal return counts an aborted call rather than a return.
The run's single successful terminal completion additionally keeps its exact
canonical request and serialized reply in one bounded recovery slot (each at
most the journal's 4 MiB body limit). Thus terminal retries/status still work
after the terminal epoch change and after sealing; conflicting content is
rejected and no journal write occurs. Other IDs retain their epoch scope.

`audit_snapshot.engine_call` provides actual tracker health outside the game
state/digest. A fresh initial marker requires all counters zero and no active
call. The close tail is `recording_closed`, `engine_call_closed`,
`draw_schedule_closed`, `particle_shake_closed`, `spawn_hook_closed`.
Engine-call health reports `reserved_calls`, `entered_calls`, `returned_calls`,
`written_post_boundaries`, `verified_clock_steps`,
`verified_terminal_zero_calls`, `terminal_clock_steps`, `active_call_id`,
`faults`, `reentrant_calls`, `wrong_thread_calls`, `aborted_calls`, and `healthy`.
A healthy strict run requires reserved=entered=returned=written posts,
returned=clock steps+terminal-zero calls, terminal-zero count at most1, no active
call and zero faults. Full boundary count is 2×returned calls; draw step count
is verified clock steps minus terminal clock steps. Wall-clock timing is not a
substitute for any counter.
