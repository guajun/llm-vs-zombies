# Native sound allocation counter origin

`sound_effects_counter_origin_v1` declares an additional, explicit interpretation
of `/sound_effects/calls`. It is available only with the installed allocation-none
audio experiment and the independent App update anchor. Neither older audio/App
mode specification changes. Original-audio sessions omit this new declaration,
state field and sidecar. Older archives retain their absolute counter meaning.

The 039 cold-start comparison differed in the bootstrap diagnostic allocation
total (14 versus 16), after the real App counter had been explicitly anchored.
This operation establishes a new experiment counter origin; it never resets the
bootstrap total and never writes game memory, RNG, Foley histories or parameters.
It does not certify exhaustive determinism or silently upgrade the older run.

Initialization is seed/clock restore and actual full RNG readback, then
`sound_counter_origin {}`, then the existing App anchor, then one warm draw.
The origin requires a paused UI3 fight, tick zero, no entered engine call, no
warm draw, no App anchor and no prior origin in this recording. Native code
reuses the full 624-word MT/CRT and 110-by-eight Foley/32-channel checks used by
the App anchor. Owner-thread and resident-patch validation precede capture.

The object persists for the recording and has no epoch reset. A guard rejection
does not consume it; after binding begins a failure is latched. Exact request-ID
retries return the recorded journal reply, while new IDs cannot rebind. Successful
binding seals later RNG/clock initialization. The App operation still remains
available and requires the bound origin; warming requires both operations.

Before binding, each Snapshot has `counter_scope:"bootstrap_lifetime"` and the
absolute bootstrap total in `calls`. Binding stores the actual total N in recorder
memory. Subsequent snapshots have `counter_scope:"experiment"` and `calls=R-N`.
Unsigned 32-bit overflow or any absolute regression is an error. Warm-draw
allocations and all later allocations remain counted, even when no game tick
advances. No absolute value is smuggled into the comparable state.

The `lvz.sound-counter-origin.v1` receipt has exactly `schema`, `mode`,
`origin_raw_calls`, `raw_before`, `raw_after`, `before_state`, `after_state`,
`before_version`, and `after_version`. Both raw values retain the complete
activation-receipt shape, including process addresses, thread, patch bytes,
module identity and absolute count. They must be equal. Full before/after game
states must differ only in `calls:N -> 0` and scope lifetime -> experiment.
The controller increments revision once and flushes `sound_counter_origin_bound`
with `{request_id,counter_origin}` before acknowledging success.

The origin's after-state still contains the actual unanchored App count. Each
run links it exactly to its subsequent App anchor's before-state. Cross-run
complete state comparison takes place after the App anchor and at B0, when the
App count has actually been made equal. The counter operation never adjusts it.

Each authoritative pre/post audit boundary writes `sound-counter-raw.jsonl` from
the **same successful Snapshot sample** used in its checksum and state delta.
There is no second status query at sidecar-writing time. Each entry has exactly
`schema:"lvz.sound-counter-raw.v1"`, `seq`, `kind`, `version`, `engine_call_id`,
`raw_calls`, `origin_raw_calls`, and `experiment_calls`. Sequence, boundary kind,
version and actual engine call ID match their authoritative record. Zero-step
recordings create an empty file. The stream is flushed and closed with the audit;
I/O failure prevents a successful close acknowledgement.

Healthy sound closure retains its existing health fields and uses the relative
`calls`, adding `counter_scope:"experiment"`, `origin_raw_calls`, and `raw_calls`.
The absolute total continues through closure and must match the same final
snapshot sample. Cross-run health compares relative values after independently
validating both absolute values. Missing origin, counter regression, ownership
failure or partial binding yields an unhealthy footer with actual bounded raw
status and the latched failure; it never reuses a stale healthy snapshot.

The native fixture exercises differing absolute origins with equal actual
relative states, retained warm allocations, exact receipt/sidecar identities,
seed/empty-audio/thread/range guards, retries, initialization sealing, epoch
lifetimes, raw arithmetic failures, wrap/regression and post-bind fault closure.
Tests operate on controlled fixture captures and do not run the original game.
`live_verified:false` remains in the declaration pending independent real source
and cold-replay validation.
