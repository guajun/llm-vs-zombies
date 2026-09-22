# Explicit allocation-none sound effects experiment

`audio_mode: "original"` is the default. The opt-in value is
`sound_effects_allocation_none_v1`. This is an experimental engine execution
mode, not an original-audio demo and not an assertion of complete determinism.
It requires a new source recording and a matching cold-start initialization.
Old recordings cannot be replayed with this mode and called equivalent.

The native bootstrap replaces the locked original SoundManager
`GetSoundInstance` entry with a null-return implementation **before the
launcher resumes the original primary thread**. The game retains its original
pitch and variation code, including calls made before the allocation attempt.
Later Foley admission changes because no sound instances exist. Consequently,
RNG draw counts and later gameplay can differ from ordinary audio. There is no
RNG restoration, fabricated IsPlaying return, resource grant, or entity change.

Music/BASS is not virtualized. This mode's acceptance scope is the specified
fog-survival experiment. Store dialogue timing depends on sound-playing state
and can differ; unchanged behavior in every game mode is not promised.

## Activation and ownership

The native launch command accepts the mode as its optional last argument;
without it, the child environment is explicitly set to `original`, regardless
of the caller's inherited environment. Python binds the experiment config,
launcher receipt, resident hello and recipe. Unknown modes and an old
bootstrap lacking the required exports fail startup. Neither recording nor
replay silently downgrades to ordinary audio.

Bootstrap validates the exact engine file SHA-256, I386 PE image base/size,
GetSoundInstance prologue, both ret-4 exits, original virtual slot, and Foley's
allocation/null branch. Both original global App roots must still be null.
It then pins its module for the process lifetime and patches the complete
six-byte instruction prefix. The thunk preserves flags, every register except
its specified EAX=null result, stack cleanup and the floating-point state.
It counts actual calls, foreign-thread calls and overflow without invoking
original random functions or modifying simulation memory.

An unused patch may be rolled back only before the pre-resume seal. After
seal, neither cleanup nor recorder shutdown unhooks it. The owned process is
terminated at the end; its bootstrap is never unloaded while referenced.
The launcher verifies the exported status, writes and closes its receipt
successfully, and only then resumes the original primary thread. A failure
terminates that still-owned child. No late StopAllSounds/ReleaseChannels or
manual Foley reset is implemented.

The runtime independently verifies actual jump bytes, replacement module
ownership, bootstrap SHA-256 and the pre-resume receipt. Raw activation
addresses are evidence, not cross-process state identities.

## State and evidence

Enabled hello/game and native audit manifests have `sound_effects` with the
same stable specification, including the bootstrap hash, original engine
hash, target RVA, activation stage and explicit
`original_engine_bitwise_unmodified:false`. The capability is true only for
an activated mode; disabled runs omit the game/state declaration.

Every audit snapshot and pre/post boundary reads:

- All 110 Foley histories, including last variation and eight slots each.
  Slot values are actual instance pointer, refcount, paused byte, start count
  and pause offset; padding is not read.
- The active Foley parameters, raw pitch bits, flags, fixed-image resource
  RVAs and actual sound IDs.
- All 32 manager channel pointers and actual App `mUpdateCount`.
- Actual cumulative allocation calls/errors and current patch ownership.

All instance pointers/refcounts/channel pointers must actually be zero.
The implementation rejects a surviving or bypassed instance; it never zeroes
one. Parameters and histories are retained rather than inferred from the
mode. App update count is read without resetting or normalizing it. Its B0
origin must match during strict cold replay.

`audit/audio-activation.json` binds the real pre-resume receipt to the recorder
attachment status. It is required and hash-bound in the trajectory/archive.
The initialization recipe binds the specification and SHA-256 of the complete
B0 sound-effects state. Full state comparisons include the histories and
counter; matching a mode string alone is insufficient.

A `sound_effects_closed` native event records final health and counters.
Semantic failures latch: even if a later snapshot happens to look healthy,
the run remains unhealthy. A persistent failure still produces a truthful
unhealthy close with a bounded raw status/reason, so failure evidence can be
archived normally. Strict readers reject that evidence. IO failures still
prevent a successful recording close.

## Verified static evidence

Original engine SHA-256:
`f9669af338964787a3785a7895791297d599295b8bb669b0db49443f736a1322`.
All addresses below are VAs in its image at `0x400000`. Its real PE
`SizeOfImage` is `0x394000`. The older private memory dump covered only
`0x35e000` bytes, ending at the start of `.rsrc`; its length is not an exact
PE identity. Sound ID globals are separately restricted to the actual `.data`
RVA interval `[0x299000, 0x35dc1c)`, including zero-fill, with room for a whole
four-byte word. The old scalar adapter's `>=0x35e000` check remains a minimum
extent, not an exact-size check.

`build/silent_audio_tests.exe --verify-pe game/local-engine/PlantsVsZombies.exe`
verifies the locked file SHA-256, maps its sections in an ordinary byte vector
without loading or executing the game, invokes the compiled bootstrap image
validator, and emits the PE headers/section extents and all Foley sound-ID
pointer range checks as JSON. This catches header/dump-length confusion that
an invented matching PE fixture alone cannot detect.

| Evidence | Address / bytes |
| --- | --- |
| GetSoundInstance entry | `0x5c7650`: `55 8b ec 83 e4 c0 6a ff 68 d6 f9 63 00` |
| Callee stack cleanup | `0x5c772f`, `0x5c77aa`: `c2 04 00` |
| Original manager vtable slot +0x20 | `0x675ff4` contains `0x5c7650` |
| Foley allocation/null-result branch | `0x5151b3`: `ff d0 8b f0 85 f6 74 72` |
| Original variation RNG call | `0x515188` calls `0x5af400`, before allocation |
| Foley last-variation store | `0x51519b`, before allocation |
| Original per-type / per-slot strides | `0xa4` / `0x14` |

Reference sources were pinned to
`8a2d121899ba5cb4df644cd7d2e4c1aaf88dd238`. The binary, not the reference
source, determines target addresses. TodFoley.cpp SHA-256 is
`43632c83c3873313b3e37509e594ef092420741ffb15e24a02e35ade1642a954`;
DSoundManager.cpp is
`8ea69d8c96c32fa3e22bc783fe973212e1c8ca73bf25e994cb18037222002c55`.
Only bounded signatures and independently written adapters are published;
no game binary or full decompilation accompanies this implementation.

## Validation status

The native fixture tests actual x86 calling convention/ret-4, preserved
registers/flags/FP state, counters, wrong-thread detection, signature/PE
rejection, default-off behavior, one-shot install/seal/rollback, safe inspection
of an unmapped patch, persistent unhealthy close and fault latching. Python
fixtures validate mode negotiation and required state/receipt/health evidence.
These fixtures do not substitute for a live game experiment.

`live_verified:false` remains intentional. Real hidden launch, empty B0,
new-mode cold replay with wall delays, terminal behavior and full two-flag
policy acceptance require separate recorded experiments. The ordinary-audio
Foley probe established that an actual IsPlaying return can control a variation
RNG call; that finding does not by itself prove every source of randomness is
covered by this mode.

## Related mode

`virtual_audio_tick_v1` (issue #37,
`docs/虚拟音频设备与离线音轨.md`) keeps this mode's simulation-side profile
unchanged -- the same null-return `GetSoundInstance` replacement, so the Foley
admission trace, variation draws and slot bookkeeping stay frame-identical --
and adds a tick-driven virtual mixer that answers `IsPlaying` /
`GetCurrentPosition` from the game tick and the sound duration instead of a
sound card. The mixer is read-only for the simulation and produces an offline,
tick-aligned audio track; the audio-only admission (ten-update rejection,
one-at-a-time reuse, eight live slots) is re-derived on the rendering side.

Like this mode, the new one changes experimental semantics, declares
`original_engine_bitwise_unmodified:false`, requires new source recordings, and
does not make older recordings replayable under it.
