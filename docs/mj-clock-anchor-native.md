# Fixed MJ clock anchor

`initial_mj_clock_anchor_v1` is an independent initialization capability. It is
advertised only with the explicitly installed `sound_effects_allocation_none_v1`
experiment, exactly like the App update anchor. Ordinary audio does not expose
it, and no older specification, archive or receipt changes meaning.

The trigger was the offline C/D parallel-world comparison: at B(0) the complete
state differed only at `/app/mj_clock` (1307 versus 1340) besides the already
anchored `/sound_effects/app_update_count`, and the 33-count offset moved real
dancer phase transitions by 33 ticks from tick 1200 on. That report keeps its
own evidence and its [待核实] marker for the canonical field name; this document
closes that marker with the actual target-image disassembly and does not rewrite
the report.

## Field identity: `mAppCounter` is `LawnApp+0x838`

Target image `game/local-engine/PlantsVsZombies.exe`, SHA-256
`f9669af338964787a3785a7895791297d599295b8bb669b0db49443f736a1322` (the locked
1.0.0.1051 engine; `determinism/evidence.json` records the same file and the
20-byte `game_total_loop` signature at `0x452650`).

`Zombie::GetDancerFrame` at `0x52DF90` reads the app pointer and then the
absolute counter:

```text
  52dfc4: 8b 00                 mov  eax, dword ptr [eax]        ; Zombie+0x0 = mApp
  52dfc6: 0f af ce              imul ecx, esi                    ; aFrameLength * aFramesCount
  52dfc9: 8b 80 38 08 00 00     mov  eax, dword ptr [eax + 0x838]
  52dfcf: 99                    cdq
  52dfd0: f7 f9                 idiv ecx                         ; % (aFrameLength*aFramesCount)
  52dfd2: 8b c2                 mov  eax, edx
  52dfd4: 99                    cdq
  52dfd5: f7 fe                 idiv esi                         ; / aFrameLength
```

with `esi = 20` and `ecx = 23` for every phase except
`PHASE_DANCER_DANCING_IN` (`cmp dword ptr [eax + 0x28], 0x28`), which uses
`10 / 11`. That is exactly `(mApp->mAppCounter % 460) / 20` from the candidate
decompilation (`Lawn_Zombie.cpp:6071-6096`, function address `0x52DF90` in its
comment) and `Zombie::GetDancerPhase` at `0x52DFE0` is its immediate caller.
The early returns (`mFromWave == -3`, immobilizing counters at `+0xb4/+0xb0`)
explain why the counter is only read for a live dancer/backup-dancer fight,
matching the C/D mechanism check (95,960 observations, zero counterexamples).

The same four bytes are the field the game itself increments once per App update
loop:

```text
  4526e6: 01 af 38 08 00 00     add dword ptr [edi + 0x838], ebp   ; LawnApp::UpdateFrames
  44ec2a: 89 9e 38 08 00 00     mov dword ptr [esi + 0x838], ebx   ; LawnApp constructor: mAppCounter = 0
```

and the decompilation names that member `mAppCounter`
(`LawnApp.cpp:108 mAppCounter = 0;`, `LawnApp.cpp:1617 mAppCounter++;` while
`Lawn_Zombie.cpp:6091` reads `mApp->mAppCounter`). The AvZ framework used by this
project declares the same offset as `MjClock()` (`avz/framework/inc/avz_pvz_struct.h`,
`MRef<int>(0x838)`), which is where the adapter's `mj_clock` name comes from.

A full scan of the image finds two more readers of the field, `0x465080` and
`0x465cd4` (the latter selects between `Board+0x5568` and `App+0x838` as an
absolute time base). There is therefore **one** field to anchor; the dancer path
and the other readers all observe the same anchored value.

## Relation to the existing clock contract

`CaptureClocks()` / `RestoreClocks()` in `determinism/audit.cpp` have always
included `mj_clock` as the third field, and `clock_restore` is the same-world
recovery step described in [determinism.md](determinism.md) ("record the three
B(0) values and restore them with the same initialization flow"). Both
operations address the same four bytes and both are recorded with real values;
they do not conflict:

- `clock_restore` (epoch 3, revision 2 in the C/D runs) restores the value the
  recording itself captured, so a cold replay reproduces its own world.
- `mj_clock_anchor` runs later, after the explicit seed, after the App update
  anchor, and before the warm draw. It writes the fixed target that the
  experiment declares, so two worlds that declare the same target agree.

The recipe keeps `clock_anchor` and `postwarm_clock` at their existing
positions; after this operation they carry the anchored value, and the warm
draw still requires `clocks_before == clocks_after == recorded`. The remaining
two clock fields, the state comparator and the three-field snapshot format are
unchanged. Nothing is deleted from the comparison: `/app/mj_clock` stays a
compared field in every boundary, and no offset is subtracted anywhere.

## World-internal reproducibility and cross-world consistency

These are separate claims, and this change only addresses the second one:

- **World-internal** (source versus cold replay of the same world): unchanged.
  The recorded target is the same on both sides, so the existing App-anchor
  style evidence and all later boundaries still compare exactly.
- **Cross-world**: before the write the two worlds may hold genuinely different
  counters (1307 versus 1340). Those real values stay in the receipts and in the
  reports; `after` must equal the one declared target. The anchor never derives
  its target from the run's own current value, and `apply_recipe` refuses to
  start on a runtime that declares the capability without an explicit fixed
  target.

Matching this field is a necessary condition for tick-by-tick comparability, not
proof of complete determinism, and it says nothing about the remaining C/D
divergence chain. The failed historical runs are not rewritten or re-certified.

## Native contract and event

The RPC is `mj_clock_anchor` with exactly `{"mj_clock": N}`, integer
`0 <= N <= 2147483647`, plus the exact current epoch/tick/revision `expect`.
The range matches `RestoreClocks`, which rejects values above `INT32_MAX`.
Native preconditions are a paused ready fight, controller tick zero, no entered
controlled engine call, an unused anchor in this epoch, the explicit
allocation-none audio mode, actual seeded RNG and empty Foley/channel readback,
a completed App update anchor, and no warm draw yet. The App anchor must precede
this operation and the warm draw must follow it, so each initialization stage
consumes exactly one revision in a fixed order:
`rng_seed` → (`clock_restore`) → (`sound_counter_origin`) → `app_update_anchor`
→ `mj_clock_anchor` → `prepare_render`.

On the owning game thread, native code captures the full state, writes exactly
four bytes at the actual `LawnApp+0x838`, and captures the full state again.
Every field other than `/app/mj_clock` must compare equal, the pre-write
readback must have matched the snapshot, and the post-write readback must equal
the target. The `lvz.mj-clock-anchor.v1` receipt contains `schema`, `mode`,
`requested`, actual `before`/`after`, `before_state`/`after_state`; the
controller adds the actual `before_version`/`after_version`, increments the
revision exactly once, and writes `mj_clock_anchored` with
`{request_id, anchor}` at that new revision. Audit output is flushed before the
successful acknowledgement.

If post-write capture or verification fails, native code preserves the attempted
counter value and the available before/after evidence, bumps the revision,
records `mj_clock_anchor_failed` and freezes the controller. It never rolls back
or retries the write to manufacture a successful receipt. Precondition
rejection performs no field write. Identical request-ID retries are resolved by
the existing disk journal and return the original response; a new ID cannot
repeat a successful anchor, and conflicting payloads keep the usual
request-ID conflict rejection. `rng_seed`, `rng_restore` and `clock_restore` are
sealed after the anchor.

## Reader validation rules

The Python reader (`src/llm_vs_zombies/mj_clock_anchor.py`) independently
requires:

- hello/game declarations and both capabilities (`initial_mj_clock_anchor_v1`,
  `mj_clock_anchor`) to be present and exactly equal; a recipe may not upgrade
  an archive that lacks the declaration, and a declaration may not appear
  without its recipe block.
- the recipe block to be exactly `{"configuration", "mj_clock"}` with the
  declared specification and an in-range integer target.
- the anchor event to be unique, after the native `rng_seeded` event, before any
  warm draw or simulation frame, and to be the immediate next revision after
  the App anchor, starting from that anchor's complete after-state.
- the receipt to keep `0 <= before <= 2147483647`, `after == requested`, the
  actual readback equal to the target, both full states at ready UI 3 with the
  prewarm draw counts, the RNG equal to the actual seeded instances, and the two
  states to differ only at `/app/mj_clock`.
- the warm draw to be the immediate next revision, and B0 to confirm
  `mj_clock_anchored:true` with the actual `/app/mj_clock` equal to the recipe
  target.

Missing, duplicate, late, out-of-range or failed events, a changed seed/clocks
after anchoring, a mismatched target or a state change outside the declared
field all fail strict evidence. Across source and cold replay the raw `before`
values are reported individually and never forced equal; `requested`, `after`,
the complete after-state and the mapped versions must be equal. Because the App
anchor is the immediate predecessor of this write, its intermediate after-state
comparison masks only `/app/mj_clock` (the field the next declared operation
pins) when this capability is declared; the MJ receipt and every B0/step state
still compare it strictly.

## Experiment declaration

`Plan.mj_clock` (evaluation plan JSON) declares the fixed target, the recipe
stores it as `mj_clock_anchor: {configuration, mj_clock}`, and the cold replay
reads the same value back from that recipe. Parallel worlds must declare the
same value in their own plans; the launcher CLI accepts `--mj-clock` for the
single-process path. A runtime that declares the capability without a declared
target fails closed before any initialization request is sent.

## Tests

`tests/test_mj_clock_anchor.py` is an offline simulator: two worlds with
different real counters and one declared target reach identical B(0) states and
replay equally (full replay and seek 0/2), while out-of-range targets,
tampered receipts (RNG, audio calls, other clocks, neighbors, versions, shape),
readback differences, missing/duplicate/failed events, late anchors, post-anchor
reseeding, chain tampering, capability mismatches and old archives without the
declaration are rejected.

`tests/mj_clock_anchor.cpp` is a native fixture over a fake four-byte field with
sentinel neighbors: it exercises the actual write, the real before/after
receipt, cross-run targets 1307/1340 → 2048, precondition rejection without a
write, ordering (before the App anchor, before the warm draw), duplicate and
retry rules, post-anchor sealing, signed-range edges, partial-write fault
preservation and ordinary-audio compatibility. It does not run the original
game. `live_verified:false` stays in the specification until a real source and
cold-replay validation is documented; matching this counter alone does not prove
complete game determinism.
