"""Deterministic Liangyi gargantuar-hammer scenario for the #51 fork test.

``llm_vs_zombies.evaluation.ScriptStrategy`` loads this file as a plain Python cell
and calls ``decide(observation, context)`` once per decision. Every call must return
exactly ``{"actions": [...], "advance_ticks": int}``, and the action dicts use the
same wire form as ``llm_vs_zombies.client.plant`` / ``llm_vs_zombies.client.spawn``
(numeric AvZ enums, 1-based row/column, no seed-slot index). This file imports
nothing, reads no clock and no random source, and never calls the game API: the whole
run is a function of the tick counter and the tick budget the caller passes in.

Fixed order after the B(0) marker:

1. B(0)     one ``spawn`` of a gargantuar at (GIANT_ROW, GIANT_COL), 0 ticks.
2. APPROACH advance in chunks until APPROACH_TICKS have passed since the spawn.
3. PLANT    one puff-shroom at (GIANT_ROW, PUFF_COL), 0 ticks.
4. SMASH    advance SMASH_TICKS so the hammer's timed event can fire.
5. TAIL     advance TAIL_CHUNK_TICKS per decision until the budget is spent.

``examples/liangyi_gargantuar_b.py`` is this file with the single fork constant
``FORK_EXTRA_PUFF_COL`` set to a column instead of ``None``. Branch B then inserts
exactly one extra puff-shroom request (``advance_ticks=0``, same tick as the shared
plant) after stage 3. Every other request has the same method, action list and
advance budget as its counterpart in the other script, so A and B share one prefix
and diverge by one request only. Observed board versions naturally differ after the
fork because the two branches no longer hold the same state.

Geometry the stage ticks must satisfy (all from ``work/replay-research``, the
binary-verified decompilation of the pinned engine):

* ``Zombie::GetZombieAttackRect`` for a gargantuar is ``Rect(-30, -38, 89, 154)``
  relative to ``mPosX`` (``Lawn_Zombie.cpp:352``), it walks left, and the
  puff-shroom's rect is ``Rect(mX + 10, mY, mWidth - 20, mHeight)``
  (``Lawn_Plant.cpp:5179``, ``mWidth=80``).
* A plant becomes a smash target once ``GetRectOverlap(attack, plant) >= 20``
  (``Zombie::FindPlantTarget``, ``Lawn_Zombie.cpp:6336``), which for a bait cell in
  column C reduces to ``mPosX <= 80 * C + 40`` - that is the right edge of the bait
  cell. The giant then enters ``PHASE_GARGANTUAR_SMASHING`` and squishes the square
  of the first overlapping plant when its reanimation crosses the ``0.64`` timed
  event (``Lawn_Zombie.cpp:2034``).
* ``spawn`` places the giant at ``GridToPixelX(col-1, row-1) - 30``
  (``docs/runtime-protocol.md``), i.e. ``80 * C - 30`` in the requested 1-based
  column C, so column 9 starts at x=650.

APPROACH_TICKS and SMASH_TICKS are therefore placeholders for the first live run:
pick APPROACH_TICKS so the giant is at or just outside the bait cell's right edge
when the puff is planted (a gargantuar walks on the order of 0.2 px/tick, and the
default 400 ticks move it roughly 60-100 px from x=650 towards column 7's edge at
x=600), then pick SMASH_TICKS long enough for the 0.64 smash event to land and
short enough to leave a tail. Both are recorded as ordinary constants so the main
agent can tune them after reading the first run's observation stream.
"""

# AvZ numeric enums. These are engine enums, not seed-slot indexes.
GIANT_TYPE = 23            # AZombieType AGARGANTUAR (白眼巨人)
PUFF_TYPE = 8              # APlantType APUFF_SHROOM (小喷菇)

# Scenario geometry, 1-based lawn coordinates.
GIANT_ROW = 6              # row the gargantuar is spawned into
GIANT_COL = 9              # column the gargantuar appears inside
PUFF_COL = 7               # column of the shared bait puff-shroom

# Fork point. Branch A sends no extra action; branch B plants one more puff-shroom
# at this column and is otherwise request-for-request identical.
FORK_EXTRA_PUFF_COL = 6

# Stage tick counts.
APPROACH_TICKS = 400       # spawn -> puff planted
SMASH_TICKS = 240          # puff planted -> hammer timed event has fired
ADVANCE_CHUNK_TICKS = 40   # per-decision advance inside APPROACH/SMASH
TAIL_CHUNK_TICKS = 100     # per-decision advance after SMASH_TICKS


def _spawn(kind, row, col):
    """Same wire form as llm_vs_zombies.client.spawn()."""
    return {"op": "spawn", "type": kind, "row": row, "col": col}


def _plant(kind, row, col):
    """Same wire form as llm_vs_zombies.client.plant()."""
    return {"op": "plant", "type": kind, "row": row, "col": col}


def _step(want, chunk, remaining):
    """Advance at most one chunk, never past the caller's remaining budget."""
    return max(0, min(want, chunk, remaining))


class GargantuarScenario:
    """Tick-driven state machine.

    Stage transitions read the observation's tick and this object's own stage
    counters only. No board field, clock, or random source is read, so two scripts
    with the same constants produce the same request stream for the same tick
    budget, and a single extra action cannot move the stage boundaries.
    """

    def __init__(self):
        self.spawn_tick = None
        self.plant_tick = None
        self.fork_done = False

    def decide(self, observation, context):
        remaining = context["remaining_ticks"]
        tick = observation["version"]["tick"]
        if self.spawn_tick is None:
            # Stage 1: the first request after B(0) creates the gargantuar in the
            # requested row/column without spending a tick.
            self.spawn_tick = tick
            return {"actions": [_spawn(GIANT_TYPE, GIANT_ROW, GIANT_COL)], "advance_ticks": 0}
        approach = tick - self.spawn_tick
        if approach < APPROACH_TICKS:
            return {"actions": [], "advance_ticks": _step(APPROACH_TICKS - approach, ADVANCE_CHUNK_TICKS, remaining)}
        if self.plant_tick is None:
            # Stage 3: the shared bait lands while the giant is inside the hammer's
            # targeting window for this column.
            self.plant_tick = tick
            return {"actions": [_plant(PUFF_TYPE, GIANT_ROW, PUFF_COL)], "advance_ticks": 0}
        if not self.fork_done:
            self.fork_done = True
            if FORK_EXTRA_PUFF_COL is not None:
                # Fork point: branch B only. Same tick, same row, one extra plant.
                return {"actions": [_plant(PUFF_TYPE, GIANT_ROW, FORK_EXTRA_PUFF_COL)], "advance_ticks": 0}
        smash = tick - self.plant_tick
        if smash < SMASH_TICKS:
            return {"actions": [], "advance_ticks": _step(SMASH_TICKS - smash, ADVANCE_CHUNK_TICKS, remaining)}
        return {"actions": [], "advance_ticks": min(TAIL_CHUNK_TICKS, remaining)}


_scenario = GargantuarScenario()


def decide(observation, context):
    return _scenario.decide(observation, context)
