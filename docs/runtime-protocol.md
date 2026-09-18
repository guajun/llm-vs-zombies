# Runtime protocol v1

Implementation contract for the native runtime, Python client, replay and tests.

- Endpoint: local named pipe `\\.\pipe\llm-vs-zombies-<pid>`.
- Frame: 4-byte little-endian unsigned payload byte length, then UTF-8 JSON; maximum 4 MiB. One request/response per connection at a time. Partial reads/writes must be handled.
- Request: `{ "protocol": 1, "request_id": "unique-string", "method": "observe", "params": {} }`. Mutating methods also send `expect: {"epoch": 1, "tick": 0, "revision": 0}` at the top level.
- Response: `{ "protocol": 1, "request_id": "same-string", "ok": true, "result": {} }`, or `ok:false` and `error:{code,message}`. A completed response describes actual execution, not only queue acceptance.
- `hello`: result includes session, PID, protocol, build/game identity, capability booleans. Unsupported methods fail explicitly.
- `observe`: result is an observation containing `version:{epoch,tick,revision}`, `game_clock`, `game_ui`, `scene`, `wave`, `sun`, `plants`, `zombies`, `seeds`. Preserve exact types and stable object IDs. Controller tick is independent of native GameClock.
- `commit`: params `{actions:[{op:"plant",type:8,row:2,col:5}],advance_ticks:1}`. Shovel action: `{op:"shovel",row:2,col:5,target_type:-1}`. Rows/columns use 1-based AvZ coordinates. Check version, execute in order, stop on first failure and skip advancement. Earlier successes do not roll back. Result includes `action_results`, `requested_ticks`, `executed_ticks`, `stop_reason`, `observation`.
- `advance`: params `{max_ticks:100,until:{event:"wave_changed"}}`; until is optional. Result includes requested/executed ticks, stop_reason, observation. Bounded maximum must be documented. No Python callbacks execute inside C++.
- `pause`, `status`, `cancel`: bounded control/status at stable game boundaries. `pause/cancel` may omit `expect` so a second connection can interrupt an active advance. No blocking pipe read on the game thread.
- Mutations deduplicate same epoch/request_id and identical request content. Changed payload with reused ID fails. New epoch on reset/restore; stale observations fail. State-changing same-tick actions increment revision.
- Every action, including failed attempts, goes through the same executor for REPL and replay. Preserve request/execution boundaries and ordinal in logs. Strict determinism and checkpoints remain false until independently verified.

This contract may gain methods through capability negotiation. It does not itself certify exact stepping or deterministic playback.
