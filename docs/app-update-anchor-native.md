# Initial App update counter anchor

`initial_app_update_anchor_v1` is an independent initialization capability.
It is advertised only with the explicitly installed
`sound_effects_allocation_none_v1` experiment. The existing sound-effects
specification and the three-field `CaptureClocks` / `clock_restore` contract
are unchanged. Ordinary audio does not expose this write operation.

The trigger was the first cold replay of the 5,000-tick 037 source: full B0
state comparison differed only at `/sound_effects/app_update_count`, with
source 1295 and cold start 1294. No replay action had run. The failed source
comparison remains evidence; this change does not retroactively validate it.

`app_update_anchor` takes exactly `{"app_update_count": N}` with integer
`0 <= N <= 2147483647`, plus the exact current epoch/tick/revision `expect`.
The experiment declares one fixed common target in its plan/recipe; the source
and every cold initialization write that declared value once and must read it
back. A run's own observed counter is never accepted as the target: anchoring
each world to its own value keeps every world internally consistent and still
leaves the cross-world counter different. There is no subtraction of an offset
in the state comparator and no removal of the original counter.

Native preconditions are a paused ready fight, controller tick zero, no
entered controlled engine call, an unused anchor in this epoch, completed
explicit seed, and no warm draw yet. The native capture must prove all 110
Foley histories/eight slots and all 32 channels exist and every instance,
refcount and channel is actually empty. It independently checks all 624 MT
seed words, cursor 624 and the game-thread CRT seed. A boolean saying that
seeding once happened is insufficient if the current RNG readback changed.

On the owning game thread, native code captures full state, writes exactly
four bytes at actual LawnApp +0x484, then captures full state again. Every
field other than `/sound_effects/app_update_count` must compare equal,
including RNG, sound histories/parameters/allocation counts, ordinary clocks,
entities and animations. A source no-op write consumes the same one-time
operation and records equal before/after states.

The game also uses `mUpdateCount` in its original demo feature. Verified
recording/playing flags at App +0x510/+0x511 must both be zero. Receipt fields
`demo_before` and `demo_after` contain these two actual bytes plus the raw
32-bit values at +0x578/+0x49c and the single byte at +0x4a0. The marker
is a byte (verified `cmp byte` at 0x551b41); +0x4a1 is another flag and
+0x4a2/+0x4a3 are padding, so no four-byte read is made at +0x4a0. These dictionaries must be exactly
equal; the implementation never offsets auxiliary demo clocks or Foley start
times. Binary evidence includes playback checks at 0x54b9fc and 0x54ba54,
recording gating at 0x54ba9e and update-delta construction at 0x54ec60.

A successful response is `{anchored:true, anchor:<receipt>, observation}`.
The `lvz.app-update-anchor.v1` receipt contains `mode`, `requested`, actual
`before`/`after`, `before_state`/`after_state`, `demo_before`/`demo_after`, and
the controller's actual `before_version`/`after_version`. The controller
increments revision exactly once and writes `app_update_anchored` with
`{request_id, anchor}` at that new version. Audit output is flushed before
the successful RPC acknowledgement.

Warm drawing requires the completed anchor for this capability. Subsequent
`rng_seed`, `rng_restore` and `clock_restore` requests are rejected after the
anchor, sealing initialization. This leaves ordinary-audio behavior intact.
Identical request-ID retries are resolved by the existing disk journal before
these guards and return the original response without writing again. A new
ID cannot repeat a successful anchor; conflicting payloads retain the usual
request-ID conflict rejection.

If post-write capture or full-state verification fails, native code preserves
the attempted counter value and available full before/after evidence, bumps
the revision, records `app_update_anchor_failed` and freezes the controller.
It never rolls back or retries the write to manufacture a successful receipt.
The recording can still be closed to retain failure evidence; strict replay
must reject it. Precondition rejection performs no field write.

## Experiment declaration

`Plan.app_update_count` (evaluation plan JSON) declares the fixed common
target; `evaluation plan --app-update-count N` writes it, and the launcher and
REPL single-process paths accept the same value. The recipe stores it as
`app_update_anchor: {configuration, app_update_count}`, and cold replay reads
the same value back from that recipe instead of recording anything new.
Parallel worlds must declare the same value in their own plans. A runtime that
declares the capability without a declared target fails closed in
`initialization.apply_recipe` before any initialization request is sent, with
an error naming the field to declare. Archives recorded before this change —
whose recipe target was the source's own observed value, or which never
declared the capability at all — keep reading and replaying under their
original semantics and are never rewritten.

## Tests

`tests/app_update_anchor.cpp` is a native fixture over a fake four-byte field
with sentinel neighbors: it exercises the actual write, the real before/after
receipt, cross-run declared targets 1395/1422 → 1500, source no-op and
signed-range boundaries, complete-state evidence, seed/empty-slot/demo
rejection, exact-version and ID retry rules, warm/step and duplicate
rejection, post-anchor initializer sealing, partial-write fault preservation,
and successful failure-record closure. It does not run the original game.

`tests/test_app_update_anchor.py` is the offline simulator: two worlds with
different real counters and one declared target reach identical B(0) states
and replay equally (full replay and seek 0/2), while out-of-range targets,
tampered receipts, readback differences, missing/duplicate/late/failed events,
post-anchor reseeding, capability mismatches and archives without the
declaration are rejected; a declared runtime that gets no declared target
fails closed before any initialization request. These offline checks are not
live proof. `live_verified:false` remains in the specification until
separately documented real-source/cold-replay validation; matching this
initial counter alone does not prove complete game determinism.
