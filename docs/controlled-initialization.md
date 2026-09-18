# Controlled drawing: Python initialization and capture

The runtime mode `deterministic_draw_schedule_v1` changes rendering execution:
one explicit warm draw before B0, then one original draw per simulation update.
Python does not reseed or restore RNG after drawing. This is an explicit execution
mode, not a claim that the original drawing schedule is unchanged or that all
randomness is captured. Native live validation and closed-audit health remain
required.

`initialization.apply_recipe` is shared by the launcher, REPL, evaluation and cold
replay. Existing imports from `evaluation` continue to work:

```python
from llm_vs_zombies.evaluation import apply_recipe

# The process is already initialized into the paused scenario.
recipe = apply_recipe(client, seed=42)
# A separate cold process restores the source's PRE-warm anchor.
replay_recipe = apply_recipe(replay_client, seed=42, anchor=recipe["clock_anchor"])
```

New-mode preparation follows this order:

1. Negotiate the complete `hello.game.draw_schedule` specification and both
   `prepare_render` / `deterministic_draw_schedule_v1` capabilities. Verify paused
   fight readiness, tick0 and `render_prepared:false`.
2. Read the current audit snapshot. A source uses its clock anchor; a replay uses
   the supplied source anchor. Both execute exactly one `rng_seed` and one
   `clock_restore`, so initialization mutation counts are equal.
3. Read an audit snapshot and verify all624 MT seed words, cursor624, CRT seed and
   restored clocks. Seed0 uses the original MT fallback4357 while CRT stays0.
4. Call `prepare_render` once with exact epoch/tick/revision expectation. Check
   revision increased by1, tick stayed0, the successful warm receipt, and counters.
5. Capture the postwarm audit snapshot. Its actual RNG state becomes part of B0;
   it may differ from the seed. Require warm_frames1, step_frames0 and unchanged
   native clocks. Read the final observation at that exact version.

A duplicate recipe call after preparation fails before reseeding. A failed warm
draw is never retried. An uncertain mutation outcome still requires explicit
inspection/reconnect. Unknown or partially advertised new modes fail negotiation.

The recipe's `execution_mode` is a string. New mode also retains the stable native
draw specification, prewarm `clock_anchor`, `postwarm_clock`, warm/step counts, and
SHA256 of the complete pre/postwarm RNG instances encoded as sorted compact JSON.
Full warm receipts, including actual frame versions, remain in the session trace
and `initialization-evidence.json`; they are not discarded or inserted into fields
that must compare unchanged across process identities. The native audit reader
independently verifies its native digest format and final draw health.

## Launcher and REPL behavior

An ordinary `launcher.start(...)` prepares the scenario automatically before
returning. The default REPL prepares an attached, ready, unprepared new-mode game
using `--seed` (default0). Reconnecting to a prepared process does not draw again
or reseed it.

Evaluation and cold replay must defer the launcher's automatic preparation:

```python
state = start(root, run, seed=42, defer_preparation=True)
# Connect, then run apply_recipe with the intended source/replay anchor once.
```

`evaluation.live_session` already passes that option and associates its client with
the run so the recipe writes initialization evidence. CLI equivalents are
`python -m llm_vs_zombies.launcher start ... --defer-preparation` and
`python -m llm_vs_zombies.repl ... --defer-preparation`. A deferred launcher stores
`observations/scenario-ready.json`; it does not mark a prewarm observation as a
captured B0 in the run manifest.

Persisted initialization records are `initialization-recipe.json`,
`initialization-evidence.json`, `observations/initial.json` and
`observations/initial-audit.json`. The manifest binds their hashes, native mode and
runtime capability claims. It marks final draw health as pending the native
`draw_schedule_closed` record; successful warming is not a live acceptance result.
Finalized runs are rejected before any initialization mutation or artifact rewrite.

Runtimes such as EB40 that do not advertise this mode remain
`legacy_autonomous_draw_schedule`. Their existing seed/readback path works, but
they receive no `prepare_render` request and no controlled-draw label.

## Cached original frames

`client.capture_frame()` reads the current cached native frame in new mode. It
validates `forced_render:false`, `method:cached_controlled_engine_frame`, matching
mode, unchanged known RNG, and exact `frame_version`/`version` against the request's
epoch/tick/revision. It cannot create a draw. Cache reads are classified as read-only
only after this mode is negotiated; old capture retains its prior mutation handling.

Build a provider from the actual hello result:

```python
provider = RemoteGameFrameProvider.from_hello(
    lambda method, params, expect: client.request(method, params, expect=expect),
    client.hello_result,
)
frame = provider.capture(client.observation)
```

Action-only commits can invalidate the cache at the same tick with a new revision.
The provider reports `frame_cache_stale`/unavailable explicitly. It may refresh the
observation, but it never retries capture, prepares again, advances, or relabels the
old frame. A subsequent caller-requested update produces a new cached frame. Video
sidecars retain its actual cache version and rendering metadata, and reject frames
that lack the negotiated cache evidence. Video sampling frequency does not select
the game's drawing frequency.

The dedicated tests use protocol fixtures to check ordering, one-time behavior,
seed corruption, source/replay equivalence, default/deferred entry points, legacy
negotiation, cache invalidation and read-only capture failures. They do not replace
native or original-engine acceptance tests.
