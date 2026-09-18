"""Stateful, resource-respecting Liangyi baseline for evaluation.decide.

Based on the pinned AvZ Liangyi demonstration, with explicit approximations.
No live success is claimed. No game API, filesystem, network or RNG is called.
Only returned plant/shovel actions can change the game.
"""
import math

LILY, PUMPKIN, ICE, WHITE_ICE, DOOM = 16, 30, 14, 63, 15
CHERRY, JALAPENO, SQUASH, PUFF, BLOVER = 2, 20, 17, 8, 27
COST = {LILY: 25, PUMPKIN: 125, ICE: 75, WHITE_ICE: 75, DOOM: 125,
        CHERRY: 150, JALAPENO: 125, SQUASH: 50, PUFF: 0, BLOVER: 100}
CORE = {(2, 2), (5, 2), (3, 6), (4, 6), (2, 1), (5, 1),
        (3, 1), (3, 3), (4, 1), (4, 3), (3, 2), (4, 2)}
PUMPKINS = ((2, 1), (2, 2), (3, 6), (4, 6), (5, 1), (5, 2))
CHERRY_WAVES = {1, 3, 5, 7, 9, 10, 12, 14, 16, 18}
DOOM_WAVES = {2, 4, 6, 8, 11, 13, 15, 17, 19, 20}
DOOM_PREFERENCE = {2: (4, 8), 4: (3, 7), 6: (4, 7), 8: (3, 5), 10: (3, 4),
                   11: (3, 7), 13: (4, 8), 15: (4, 7), 17: (3, 5), 19: (4, 5), 20: (3, 7)}


def _plant(kind, row, col):
    return {"op": "plant", "type": kind, "row": row, "col": col}


def _shovel(kind, row, col):
    return {"op": "shovel", "target_type": kind, "row": row, "col": col}


class View:
    def __init__(self, observation):
        self.raw = observation
        self.clock = observation["game_clock"]
        self.wave = observation["wave"]
        self.sun = observation["sun"]
        self.plants = observation["plants"]
        self.zombies = [z for z in observation["zombies"] if z["hp"] > 0 and z.get("state") not in (1, 2, 3)]
        self.grid = {}
        for plant in self.plants:
            self.grid.setdefault((plant["row"], plant["col"]), []).append(plant)
        self.seeds = {(s["imitator_type"] + 49 if s["type"] == 48 else s["type"]): s for s in observation["seeds"]}

    def usable(self, kind):
        return self.seeds.get(kind, {}).get("usable") is True and self.sun >= COST[kind]

    def at(self, grid, kind=None):
        return [p for p in self.grid.get(grid, []) if kind is None or p["type"] == kind]

    def legal(self, kind, grid):
        # Runtime maps are authoritative for craters, ice trails and terrain.
        table = self.raw.get("plantable", {}).get(str(kind))
        if table is None:
            table = self.seeds.get(kind, {}).get("plantable")
        if table is not None:
            return bool(table[grid[0]-1][grid[1]-1])
        return None

    def imminent_hazard(self, grid, *, fodder=False):
        row, col = grid
        x, y = (col-1)*80+40, (row-1)*85+80
        for z in self.zombies:
            if z["type"] == 15 and z.get("state") == 16:
                yd = max(y-60-z["y"], z["y"]-(y+20), 0)
                if yd <= 100:
                    reach = math.sqrt(max(0, 10000-yd*yd))
                    if x-50-reach <= z["x"] <= x+10+reach:
                        return True
            if fodder and z["row"] == row:
                if z["type"] == 12 and z["x"] < x+40:
                    return True
                # Without animation progress, do not insert bait into an
                # already swinging giant's hitbox. Never guess the hammer frame.
                if z["type"] in (23, 32) and z.get("state") == 70:
                    if max(z["x"]-30, x+30) <= min(z["x"]+59, x+50):
                        return True
        return False


class LiangyiBaseline:
    def __init__(self):
        self.epoch = None
        self.wave = -1
        self.wave_start = 0
        self.events = []
        self.serial = 0
        self.next_white = True
        self.jalapeno_row = 1
        self.cherry_half = 5
        self.pending = None
        self.blocked = {}
        self.craters = {}
        self.hold_until = 0
        self.recovery_until = 0
        self.opening_done = False
        self.confirmed = {}
        self.failures = 0
        self.missing_core = set()

    def _schedule(self, due, kind, wave):
        self.serial += 1
        self.events.append({"id": self.serial, "due": due, "kind": kind, "wave": wave})

    def _observe(self, view):
        current_epoch = view.raw["version"]["epoch"]
        if self.epoch is None:
            self.epoch = current_epoch
        elif self.epoch != current_epoch:
            # New cold starts get a new strategy instance. A mid-run board epoch
            # is not silently treated as the old board with its old schedule.
            raise RuntimeError("baseline observed a new board epoch; finish the experiment or create a new policy instance")
        if self.pending is not None:
            pending = self.pending
            kind, grid = pending["kind"], pending["grid"]
            base_kind = kind-49 if kind >= 49 else kind
            accepted_types = (base_kind, 48) if kind == WHITE_ICE else (base_kind,)
            placed = any(p["type"] in accepted_types and p["id"] not in pending["old_ids"] for p in view.at(grid))
            if kind == PUMPKIN:
                placed = placed or any(p["hp"] > pending["old_hp"] for p in view.at(grid, PUMPKIN))
            # A mushroom can be destroyed before the next observation. A real
            # cooldown starting also proves its card was spent; a mere false usable
            # flag could just mean insufficient sun and is not accepted.
            # Original SeedPacket::Update (0x48728C) counts UP from zero and
            # resets to zero only when recharged (0x487298).
            current_seed = view.seeds.get(kind, {})
            current_cd = current_seed.get("cd_raw", pending["old_cd"])
            spent = (current_seed.get("usable") is False and type(current_cd) is int and current_cd > 0
                     and (pending["old_cd"] == 0 or current_cd < pending["old_cd"]))
            if placed or spent:
                self.confirmed[kind] = self.confirmed.get(kind, 0) + 1
                if pending["event"] is not None:
                    self.events = [e for e in self.events if e["id"] != pending["event"]]
                if pending["opening"]:
                    self.opening_done = True
                if kind == DOOM:
                    # Fallback only: runtime legal maps override this estimate.
                    self.craters[grid] = pending["clock"] + 18100
                if kind in (CHERRY, DOOM, JALAPENO, ICE, BLOVER, SQUASH):
                    self.hold_until = max(self.hold_until, pending["clock"] + (51 if kind == BLOVER else 101))
            else:
                self.failures += 1
                self.blocked[(kind, grid)] = view.clock + 100
                self.recovery_until = view.clock + 1
            self.pending = None
        present = {(p["row"], p["col"]) for p in view.plants if p["type"] in (37, 41, 42)}
        self.missing_core |= CORE-present
        if view.wave != self.wave:
            self.wave = view.wave
            wave_time = view.raw.get("wave_time")
            exact_wave_time = type(wave_time) is int and -10000 < wave_time < 200000
            self.wave_start = view.clock-wave_time if exact_wave_time else view.clock
            if 1 <= view.wave <= 20:
                start, wave = self.wave_start, view.wave
                self._schedule(start+480, "ice_call", wave)
                if wave in CHERRY_WAVES:
                    self._schedule(start+900, "split", wave)
                    self._schedule(start+2200, "cherry", wave)
                if wave in DOOM_WAVES:
                    self._schedule(start+266, "jalapeno", wave)
                    self._schedule(start+2200, "doom", wave)
                if wave in (9, 19, 20):
                    self._schedule(start+2500, "ice_call", wave)
                if wave == 9:
                    self._schedule(start+2500, "jalapeno", wave)
                if wave == 10:
                    self._schedule(start+295, "doom", wave)
                if wave == 20:
                    self._schedule(start, "split", wave)
                    self._schedule(start+295, "flag_cherry", wave)
            print("[liangyi]", {"wave": view.wave, "clock": view.clock, "sun": view.sun,
                                 "wave_time_exact": exact_wave_time,
                                 "missing_core": sorted(self.missing_core), "failed_attempts": self.failures,
                                 "confirmed_cards": dict(sorted(self.confirmed.items()))})
        # Convert calls in chronological order. This preserves the ice toggle
        # across the extra w9/w19/w20 calls; it is not simply odd/even waves.
        for event in sorted(self.events, key=lambda e: (e["due"], e["id"])):
            if event["due"] > view.clock:
                break
            if event["kind"] == "ice_call":
                event["kind"] = "white_ice" if self.next_white else "ice"
                if not self.next_white:
                    event["due"] += 320
                self.next_white = not self.next_white
            elif event["kind"] == "split":
                self._split(view)
                event["kind"] = "done"
        self.events = [e for e in self.events if e["kind"] != "done"]

    @staticmethod
    def _move_time(zombie, distance):
        if distance <= 0:
            return 0.0
        speed = max(0.19, float(zombie.get("speed", 0.19))*0.8)
        frozen = max(0, zombie.get("freeze", 0))
        slowed = max(0, zombie.get("slow", 0)-frozen)
        slow_distance = min(distance, slowed*speed*0.5)
        return frozen + slow_distance/(speed*0.5) + (distance-slow_distance)/speed

    def _io_dead(self, view, zombie):
        # Conservative version of the official empirical IO estimate. It is
        # disabled when a supporting core is missing and keeps a 35% margin.
        if self.missing_core or zombie["type"] not in (23, 32):
            return False
        x = zombie["x"]
        t = lambda bound: self._move_time(zombie, x-bound)
        if zombie["row"] in (1, 6):
            damage = (t(-67)-t(296))*0.4
        else:
            damage = (t(296)-t(616))*0.4 + (t(253)-t(296))*0.8
        return damage*0.65 > zombie["hp"]

    def _danger(self, view):
        return sorted((z for z in view.zombies if z["type"] in (23, 32) and z["row"] in (1, 2, 5, 6)
                       and not self._io_dead(view, z)),
                      key=lambda z: (z["x"]-(230 if z["row"] in (2, 5) else 40), z["id"]))

    def _split(self, view):
        dangerous = self._danger(view)
        if view.wave == 1 or not dangerous:
            self.jalapeno_row, self.cherry_half = 1, 5
            return
        for z in dangerous:
            if z["row"] in (2, 5) and z["x"] < 310:
                self.jalapeno_row = 6 if z["row"] == 2 else 1
                self.cherry_half = 1 if z["row"] == 2 else 5
                return
            if z["row"] in (1, 6) and z["x"] < 331:
                self.jalapeno_row = z["row"]
                self.cherry_half = 5 if z["row"] == 1 else 1
                return
        top = sum(z["hp"] for z in dangerous if z["row"] <= 2)
        bottom = sum(z["hp"] for z in dangerous if z["row"] >= 5)
        self.jalapeno_row, self.cherry_half = (6, 1) if top > bottom else (1, 5)

    def _actions(self, view, kind, grid, *, allow_lily=False, fodder=False):
        row, col = grid
        if not (1 <= row <= 6 and 1 <= col <= 9) or not view.usable(kind):
            return None
        if kind != PUMPKIN and grid in CORE:
            return None
        if self.blocked.get((kind, grid), 0) > view.clock or view.imminent_hazard(grid, fodder=fodder):
            return None
        occupants = view.at(grid)
        legal = view.legal(kind, grid)
        if kind == PUMPKIN:
            if not any(p["type"] in (37, 41, 42) for p in occupants):
                return None
            existing = view.at(grid, PUMPKIN)
            if legal is True:
                return [_plant(kind, row, col)]
            # Repair without granting Wall-nut First Aid to the profile. Both
            # operations execute at the same simulation boundary; never shovel
            # the underlying permanent plant.
            return ([_shovel(PUMPKIN, row, col)] if existing else []) + [_plant(kind, row, col)]
        solids = [p for p in occupants if p["type"] not in (LILY, PUMPKIN)]
        if solids and any(p["type"] != PUFF for p in solids):
            return None
        if fodder and solids:
            return None
        actions = [_shovel(PUFF, row, col)] if solids else []
        water = row in (3, 4)
        if water and not view.at(grid, LILY):
            if not allow_lily or solids or not view.usable(LILY) or view.sun < COST[LILY]+COST[kind]:
                return None
            if view.legal(LILY, grid) is False:
                return None
            if view.legal(LILY, grid) is None and self.craters.get(grid, 0) > view.clock:
                return None
            actions.append(_plant(LILY, row, col))
        elif legal is False and not solids:
            return None
        elif legal is None and self.craters.get(grid, 0) > view.clock:
            return None
        actions.append(_plant(kind, row, col))
        return actions

    def _cast(self, view, kind, grids, *, event=None, opening=False, allow_lily=False, fodder=False):
        for grid in grids:
            actions = self._actions(view, kind, grid, allow_lily=allow_lily, fodder=fodder)
            if actions:
                self.pending = {"kind": kind, "grid": grid, "event": event["id"] if event else None,
                                "opening": opening, "clock": view.clock,
                                "old_ids": {p["id"] for p in view.at(grid)},
                                "old_hp": max((p["hp"] for p in view.at(grid, kind)), default=0),
                                "old_cd": view.seeds[kind].get("cd_raw", 0)}
                return actions
        return None

    def _temporary(self, view, fallback_all=True):
        result = []
        for z in self._danger(view):
            col = max(1, min(9, int((z["x"]-11)//80)+1))
            minimum = 3 if z["row"] in (2, 5) else 1
            result.extend((z["row"], c) for c in range(col, minimum-1, -1))
        result.extend(((2, 4), (5, 4), (2, 3), (5, 3)))
        if fallback_all:
            result.extend((row, col) for col in range(1, 10) for row in (1, 6, 2, 5))
        return list(dict.fromkeys(result))

    @staticmethod
    def _weight(zombie):
        x = zombie["x"]
        edge = 230 if zombie["row"] in (2, 5) else 0
        urgency = 1+max(0, 400-(x-edge))/150
        body = zombie["hp"]+zombie.get("armor1", 0)+zombie.get("armor2", 0)
        return min(1800, body)*urgency*(1.5 if zombie["type"] in (15, 23, 32) else 1)

    def _cherry_grids(self, view, half=None):
        rows = (half, half+1) if half in (1, 5) else (1, 2, 5, 6)
        def score(grid):
            row, col = grid
            left, right = 274+(col-6)*80, 612+(col-6)*80
            return sum(self._weight(z) for z in view.zombies if abs(z["row"]-row) <= 1 and left <= z["x"] <= right)
        return sorted(((r, c) for r in rows for c in range(2, 9)), key=lambda g: (-score(g), abs(g[1]-6), g))

    def _emergency_cherry_grids(self, view, target, half=None):
        # Keep the crowd score, but do not spend an emergency cherry on a
        # distant crowd that misses the zombie which triggered the response.
        # 1.0.0.1051 DoSpecial (0x4666A0) uses radius 115. Aim at a conservative
        # ground-body interior point, not the wider giant envelope used by
        # the crowd score. In particular, a risen digger's mirrored rectangle
        # spans x+42..70 (GetZombieRect 0x5320B0), so x+60 is inside it.
        # This is a placement guard, not a forecast of motion/height at +100cs.
        x, y = int(target["x"])+60, int(target["y"])+70
        return [(row, col) for row, col in self._cherry_grids(view, half)
                if abs(row-target["row"]) <= 1
                and (80*col-x)**2 + (85*(row-1)+120-y)**2 <= 115**2]

    def _doom_grids(self, view, wave):
        preferred = DOOM_PREFERENCE.get(wave, (4 if self.jalapeno_row == 1 else 3, 7))
        if wave == 13:
            preferred = (4 if self.jalapeno_row == 1 else 3, 8)
        # Keep the primary imitator-ice station usable. A fallback doom there
        # would destroy its lily and leave an 18,000-tick crater. The planned
        # w10 (3,4) doom remains intentional; this rule only excludes (4,4).
        alternatives = [(r, c) for r in (3, 4) for c in (4, 5, 7, 8, 9) if (r, c) != (4, 4)]
        def score(grid):
            row, col = grid
            x = (col-1)*80+80
            return sum(self._weight(z) for z in view.zombies if ((z["x"]-x)**2+((z["row"]-row)*85)**2) <= 240**2)
        return [preferred] + sorted((g for g in alternatives if g != preferred), key=lambda g: (-score(g), g))

    def _scheduled(self, view):
        for event in sorted(self.events, key=lambda e: (e["due"], e["id"])):
            if event["due"] > view.clock:
                break
            kind = event["kind"]
            if kind == "white_ice":
                actions = self._cast(view, WHITE_ICE, [(4, 4), (3, 4)], event=event, allow_lily=True)
            elif kind == "ice":
                actions = self._cast(view, ICE, self._temporary(view), event=event, fodder=True)
            elif kind == "jalapeno":
                row = self.jalapeno_row
                candidates = [g for g in self._temporary(view) if g[0] == row]
                actions = self._cast(view, JALAPENO, candidates, event=event)
            elif kind == "doom":
                actions = self._cast(view, DOOM, self._doom_grids(view, event["wave"]), event=event, allow_lily=True)
            elif kind in ("cherry", "flag_cherry"):
                candidates = [(3, 4), (4, 4)] if kind == "flag_cherry" else self._cherry_grids(view, self.cherry_half)
                if event["wave"] == 1 and kind == "cherry":
                    candidates.insert(0, (6, 7))
                actions = self._cast(view, CHERRY, candidates, event=event, allow_lily=kind == "flag_cherry")
            else:
                continue
            if actions:
                return actions
        return None

    def _repair(self, view, urgent_only=False):
        candidates = []
        for grid in PUMPKINS:
            current = view.at(grid, PUMPKIN)
            hp = current[0]["hp"] if current else 0
            if hp <= (1000 if urgent_only else 2200):
                priority = hp-(800 if grid in ((2, 2), (5, 2)) else 0)
                candidates.append((priority, grid))
        return self._cast(view, PUMPKIN, [grid for _, grid in sorted(candidates)])

    def decide(self, observation, context):
        remaining = context["remaining_ticks"]
        if type(remaining) is not int or remaining < 0:
            raise ValueError("remaining_ticks must be a nonnegative integer")
        if observation.get("game_ui") != 3 or remaining == 0:
            return {"actions": [], "advance_ticks": 0}
        if observation.get("scene") != 3:
            raise ValueError("Liangyi baseline requires the six-row Scene 3 fog board")
        view = View(observation)
        self._observe(view)
        if view.clock < self.recovery_until:
            return {"actions": [], "advance_ticks": min(1, remaining)}
        actions = None
        # Balloon phases 74/75 are popping/walking; they cannot be blown away.
        balloons = [z for z in view.zombies if z["type"] == 16 and z.get("state") not in (74, 75)]
        if balloons and min(z["x"] for z in balloons) < 180 and not any(p["type"] == BLOVER for p in view.plants):
            # The main blow must survive its 51-tick startup. Prefer another
            # safe cell over placing it inside an existing hammer/crusher.
            actions = self._cast(view, BLOVER, self._temporary(view), fodder=True)
        if not actions:
            actions = self._repair(view, urgent_only=True)
        if not actions and not self.opening_done and view.wave <= 1:
            actions = self._cast(view, SQUASH, [(6, 9)], opening=True)
        if not actions:
            actions = self._scheduled(view)
        dangerous = self._danger(view)
        urgent = [z for z in dangerous if z["x"] < (345 if z["row"] in (2, 5) else 160)]
        if not actions and urgent and view.clock >= self.hold_until:
            grids = [(z["row"], max(3 if z["row"] in (2, 5) else 1, min(9, int((z["x"]-11)//80)+1))) for z in urgent]
            actions = self._cast(view, SQUASH, grids)
            critical = [z for z in urgent if z["x"] < (285 if z["row"] in (2, 5) else 90) and z.get("freeze", 0) < 100]
            if not actions and critical:
                actions = self._cast(view, ICE, self._temporary(view), fodder=True)
                if not actions:
                    actions = self._cast(view, CHERRY, self._emergency_cherry_grids(view, critical[0]))
                if not actions:
                    row = urgent[0]["row"]
                    actions = self._cast(view, JALAPENO, [g for g in self._temporary(view) if g[0] == row])
        fast_threats = sorted((z for z in view.zombies if z["type"] not in (23, 32)
                              and (z["type"] != 16 or z.get("state") == 75)
                              # ZombieInitialize's 1.0.0.1051 digger branch at
                              # 0x522C74 writes phase 32 (tunneling). Its underground
                              # passage is not an imminent house-entry event;
                              # risen/walking miners still use this response.
                              and (z["type"] != 17 or z.get("state") != 32)
                              and z["row"] in (1, 2, 5, 6)
                              and z["x"] < ((390 if z["type"] == 12 else 250) if z["row"] in (2, 5) else 120)),
                             key=lambda z: (z["x"]-(230 if z["row"] in (2, 5) else 0), z["id"]))
        if not actions and fast_threats and view.clock >= self.hold_until:
            row = fast_threats[0]["row"]
            actions = self._cast(view, JALAPENO, [g for g in self._temporary(view) if g[0] == row])
            if not actions:
                actions = self._cast(view, CHERRY, self._emergency_cherry_grids(view, fast_threats[0], 1 if row <= 2 else 5))
        if not actions:
            actions = self._repair(view)
        # Preserve the official processor's short ash window, but balloon
        # emergencies and repairs remain active. It never pauses the game.
        ash_soon = any(e["kind"] in ("doom", "cherry", "flag_cherry") and 0 <= e["due"]-view.clock <= 200 for e in self.events)
        if not actions and view.clock >= self.hold_until and not ash_soon:
            actions = self._cast(view, PUFF, self._temporary(view, fallback_all=False), fodder=True)
        if actions:
            return {"actions": actions, "advance_ticks": min(1, remaining)}
        step = 1 if any(z.get("state") == 70 for z in urgent) else 5 if urgent else 20
        if not view.zombies and view.wave == 0:
            step = 50
        future = [e["due"]-view.clock for e in self.events if e["due"] > view.clock]
        if future:
            step = min(step, min(future))
        refresh = view.raw.get("refresh_countdown", 0)
        if type(refresh) is int and 0 < refresh < step:
            step = refresh
        return {"actions": [], "advance_ticks": min(step, remaining)}


_baseline = LiangyiBaseline()


def decide(observation, context):
    return _baseline.decide(observation, context)
