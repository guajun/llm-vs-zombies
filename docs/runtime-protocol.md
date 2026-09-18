# Runtime protocol v1

Implementation contract for the native runtime, Python client, replay and tests.

- Endpoint: local named pipe `\\.\pipe\llm-vs-zombies-<pid>`.
- Frame: 4-byte little-endian unsigned payload byte length, then UTF-8 JSON; maximum 4 MiB. One request/response per connection at a time. Partial reads/writes must be handled.
- Request: `{ "protocol": 1, "request_id": "unique-string", "method": "observe", "params": {} }`. Mutating methods also send `expect: {"epoch": 1, "tick": 0, "revision": 0}` at the top level.
- Response: `{ "protocol": 1, "request_id": "same-string", "ok": true, "result": {} }`, or `ok:false` and `error:{code,message}`. A completed response describes actual execution, not only queue acceptance.
- `hello`: result includes session, PID, protocol, build/game identity, capability booleans. Unsupported methods fail explicitly.
- `observe`: result is an observation containing `version:{epoch,tick,revision}`, `game_clock`, `game_ui`, `scene`, `wave`, `sun`, `plants`, `zombies`, `seeds`. Preserve exact types and stable object IDs. Controller tick is independent of native GameClock.
- `initialize`: params `{game_mode:13,cards:[...]}`, with `expect`; valid from the supported title/main menu without a Board. Supply exactly all available seed slots. The immediate result confirms configuration acceptance only; poll `observe.initialization.state` for `ready` or `error`. A request to enter a level is not proof that the level loaded or that its scene is correct. Only accepted initialization releases a terminal freeze; a rejected request leaves it frozen.
- `commit`: params `{actions:[{op:"plant",type:8,row:2,col:5}],advance_ticks:1}`. Shovel action: `{op:"shovel",row:2,col:5,target_type:-1}`. Rows/columns use 1-based AvZ coordinates. Check version, execute in order, stop on first failure and skip advancement. Earlier successes do not roll back. Result includes `action_results`, `requested_ticks`, `executed_ticks`, `stop_reason`, `observation`.
- `advance`: params `{max_ticks:100,until:{event:"wave_changed"}}`; until is optional. Result includes requested/executed ticks, stop_reason, observation. Bounded maximum must be documented. No Python callbacks execute inside C++.
- `pause`, `status`, `cancel`: bounded control/status at stable game boundaries. `pause/cancel` may omit `expect` so a second connection can interrupt an active advance. No blocking pipe read on the game thread.
- `audit_snapshot`: returns `{state,version}` from the stable game-thread boundary. This is diagnostic state, not an arbitrary checkpoint restore format.
- `rng_seed`: params `{seed:12345}`, where `seed` is an integer in `0..4294967295` (booleans/floats rejected). Requires exact `expect`, paused active fight, no pending advance, and open recording. It seeds only the target adapter's declared RNG instances. Success increments revision once and records `rng_seeded` with the actual seed.
- `rng_restore`: params `{snapshot:{...}}`, with the same pause/version restrictions. Success increments revision and records `rng_restored`. It restores known RNG instances, not the Board.
- `clock_restore`: params `{snapshot:{...}}`, with the same pause/version restrictions. The target backend validates the entire clock sidecar before writing. Success increments revision and records `clocks_restored`; controller native-clock bookkeeping is synchronized so this explicit clock initialization does not trigger an accidental epoch reset. Controller tick, Board object state, wave timers and object ages are not restored. Use this before the experiment's B(0) marker.
- `stop_recording`: requires exact `expect` and no pending advancement; flushes/closes native recording. Further state mutations and captures are rejected, and the engine will not resume automatically.
- Mutations deduplicate same epoch/request_id and identical request content. Changed payload with reused ID fails. New epoch on Board/readiness transitions or an external backward native clock; stale observations fail. RNG and clock sidecar operations retain the epoch and explicitly increment revision. State-changing same-tick actions increment revision.
- Every action, including failed attempts, goes through the same executor for REPL and replay. Preserve request/execution boundaries and ordinal in logs. Strict determinism and checkpoints remain false until independently verified.

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

Frame responses have a separate dedup cache so video cannot consume the action cache's 64 MiB budget. Only the latest **four** capture responses are retained, with up to 100,000 request-ID tombstones and 16 MiB of capture request metadata per epoch. A capture request is at most 4096 bytes; each response obeys the ordinary 4 MiB frame bound. An identical expired request ID returns `request_result_expired` and never renders again. Reusing an ID with changed content or another method remains `request_id_conflict`. These limits appear in `hello.limits`. A new epoch clears both action and capture dedup domains.

Original-frame capture is a real engine intervention when it forces drawing. A recording/replay implementation must preserve and evaluate this intervention; it must not silently treat every render as a proven read-only operation merely because controller tick did not change.

This contract may gain methods through capability negotiation. It does not itself certify exact stepping or deterministic playback.
