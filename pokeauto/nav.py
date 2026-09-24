"""Route planning across the whole region.

One Dijkstra search over (map, x, y, elevation, surfing) states. Within a map
it uses MapGrid.step() -- the game's own movement rule -- and between maps it
follows exactly the two mechanisms the engine has:

* warps: step onto a ladder/stairs/doorway tile, press north into a door, or
  press an arrow mat's direction; you arrive on the destination warp tile
  (and a door arrival walks you one tile south).
* connections: walk off an edge that the map header connects, landing at the
  neighbouring map's matching coordinate.

The planner therefore routes Littleroot -> Rustboro, or into the Mauville Gym,
without anyone writing down directions. The route script only names places.

Obstacles are what is actually there: live object events on the current map,
and for other maps the stationary objects whose hide-flag is not set.
Cuttable trees and smashable rocks are passable at a cost when we have the
move; the executor clears them on arrival.
"""
from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field

from .mapgrid import (ARROW_WARPS, DELTA, STEP_WARPS, Caps, MapGrid, Pos,
                      has_encounters, surfable, MB)
from .symbols import const, constants, maps

_CONSTS = constants()
STATIONARY = frozenset(v for k, v in _CONSTS.items()
                       if k.startswith("MOVEMENT_TYPE_") and any(
                           t in k for t in ("FACE_", "LOOK_AROUND", "NONE", "INVISIBLE",
                                            "IN_PLACE", "BERRY_TREE", "DISGUISE", "BURIED")))
GFX_TREE = const("OBJ_EVENT_GFX_CUTTABLE_TREE")
GFX_ROCK = const("OBJ_EVENT_GFX_BREAKABLE_ROCK")
GFX_BOULDER = const("OBJ_EVENT_GFX_PUSHABLE_BOULDER")

DOOR = MB["MB_ANIMATED_DOOR"]
EXIT_SOUTH = frozenset(const(n) for n in ("MB_ANIMATED_DOOR", "MB_NON_ANIMATED_DOOR",
                                          "MB_WATER_DOOR", "MB_DEEP_SOUTH_WARP"))
DIVEABLE = frozenset(const(n) for n in ("MB_INTERIOR_DEEP_WATER", "MB_DEEP_WATER",
                                        "MB_SOOTOPOLIS_DEEP_WATER"))
NO_EMERGE = frozenset(const(n) for n in ("MB_NO_SURFACING", "MB_SEAWEED_NO_SURFACING"))

# Scripted transports: talk to an NPC, say yes, arrive somewhere else. The
# engine does these with specials (CableCarWarp), so map data cannot show them.
TRANSPORTS = [
    {"map": "MAP_ROUTE112_CABLE_CAR_STATION", "npc": 1,
     "dest": ("MAP_MT_CHIMNEY_CABLE_CAR_STATION", 6, 7), "cost": 60.0},
    {"map": "MAP_MT_CHIMNEY_CABLE_CAR_STATION", "npc": 1,
     "dest": ("MAP_ROUTE112_CABLE_CAR_STATION", 6, 7), "cost": 60.0},
]


@dataclass(frozen=True)
class State:
    map: str
    x: int
    y: int
    elev: int
    surfing: bool = False

    @property
    def pos(self) -> Pos:
        return Pos(self.x, self.y, self.elev, self.surfing)


@dataclass
class Step:
    """One button action of a plan and where it should leave us."""
    action: str                 # walk | jump | surf | cut | smash | warp | edge | door | arrow
    direction: str
    expect: State


@dataclass
class NavCaps:
    surf: bool = False
    cut: bool = False
    smash: bool = False
    waterfall: bool = False
    strength: bool = False
    dive: bool = False
    ignore_boulders: bool = False  # plan as if boulders could be shoved aside
    avoid_grass: float = 0.0    # extra cost per tall-grass step (encounters)
    avoid_triggers: bool = True
    triggers_block: bool = False  # treat every coord trigger as a wall (puzzles)
    active_triggers_block: bool = True  # triggers that would fire now are walls
    trigger_exempt_map: str = ""        # ...except on this map
    ignore_story_objects: bool = False  # plan through NPCs that a script may move away

    def grid_caps(self) -> Caps:
        return Caps(surf=self.surf, waterfall=self.waterfall)


@dataclass
class Obstacles:
    walls: set = field(default_factory=set)       # impassable (x, y)
    trees: set = field(default_factory=set)       # cuttable
    rocks: set = field(default_factory=set)       # smashable
    triggers: set = field(default_factory=set)    # active coord scripts (soft)
    boulders: set = field(default_factory=set)    # Strength boulders
    soft: set = field(default_factory=set)        # NPCs placed by map data only


FALL_TILES = {MB["MB_CRACKED_FLOOR"], MB["MB_CRACKED_FLOOR_HOLE"]}


class Planner:
    def __init__(self, game):
        self.game = game
        self.emu = game.emu
        # Tiles whose trigger script turned us back ("the sandstorm is too
        # strong"). Learned during play; cleared when a milestone completes.
        self.learned_blocks: set[tuple[str, int, int]] = set()

    # -- per-map data -----------------------------------------------------------
    def grid(self, map_id: str, live: MapGrid | None = None) -> MapGrid:
        if live is not None and live.map_id == map_id:
            return live
        # Layouts a map script swaps in on entry (Sky Pillar's clean floors).
        for o in maps()[map_id].get("layout_overrides", ()):
            if o["var"] == "VAR_RESULT":
                continue
            v = self.game.var(o["var"])
            if {"lt": v < o["value"], "le": v <= o["value"], "eq": v == o["value"],
                    "ne": v != o["value"], "ge": v >= o["value"], "gt": v > o["value"]}[o["op"]]:
                from .mapgrid import layout_grid
                return layout_grid(self.emu, o["layout"], map_id)
        return MapGrid.from_rom(self.emu, map_id)

    def obstacles(self, map_id: str, live_objects=None, ignore_story: bool = False) -> Obstacles:
        """Objects in the way on `map_id`.

        live_objects (Game.objects() on the current map) wins for anything it
        covers; templates fill in objects not currently spawned.
        """
        info = maps()[map_id]
        obs = Obstacles()
        spawned = set()
        # "Story" objects: NPCs a script can hide or move. Never boulders,
        # rocks or trees -- those are physical and handled by their own rules.
        story = {t["local_id"] for t in info["objects"]
                 if t["flag"] not in ("0", "")
                 and _CONSTS.get(t["gfx"], -1) not in (GFX_TREE, GFX_ROCK, GFX_BOULDER)}
        if live_objects is not None:
            for o in live_objects:
                if o.is_player:            # invisible objects (Kecleon) still block
                    continue
                spawned.add(o.local_id)
                if ignore_story and o.local_id in story:
                    continue
                target = (obs.trees if o.graphics_id == GFX_TREE else
                          obs.rocks if o.graphics_id == GFX_ROCK else
                          obs.boulders if o.graphics_id == GFX_BOULDER else obs.walls)
                target.add((o.x, o.y))
        moved = self.game.object_templates() if live_objects is not None else {}
        for t in info["objects"]:
            if t["local_id"] in spawned:
                continue
            if t["local_id"] in moved:
                t = {**t, "x": moved[t["local_id"]][0], "y": moved[t["local_id"]][1]}
            flag = t["flag"]
            if flag and flag != "0" and (self._flag(flag)
                                         or (ignore_story and t["local_id"] in story)):
                continue            # hidden by its flag (or may be moved by a script)
            gfx = const(t["gfx"]) if t["gfx"] in _CONSTS else -1
            mv = const(t["movement"]) if t["movement"] in _CONSTS else 0
            if gfx == GFX_TREE:
                obs.trees.add((t["x"], t["y"]))
            elif gfx == GFX_ROCK:
                obs.rocks.add((t["x"], t["y"]))
            elif gfx == GFX_BOULDER:
                if live_objects is None or not self._in_view(t["x"], t["y"]):
                    obs.boulders.add((t["x"], t["y"]))
            elif mv in STATIONARY:
                if live_objects is None:
                    # A map we are not on: scripts move people around at load
                    # time (setobjectxyperm), so template spots are guesses.
                    obs.soft.add((t["x"], t["y"]))
                elif not self._in_view(t["x"], t["y"]):
                    obs.walls.add((t["x"], t["y"]))
        for c in info["coords"]:
            if c.get("type") == "trigger" and c.get("var") and c.get("var") in _CONSTS:
                try:
                    # VAR_TEMP_* are reset and rewritten by scripts as you walk
                    # (Route 111's sun/sandstorm pair), so a snapshot of them
                    # says nothing about the moment we arrive: assume active.
                    if (c["var"].startswith("VAR_TEMP_")
                            or self.game.var(c["var"]) == int(str(c["var_value"]), 0)):
                        obs.triggers.add((c["x"], c["y"]))
                except (ValueError, KeyError):
                    pass
        obs.walls |= {(x, y) for (m, x, y) in self.learned_blocks if m == map_id}
        return obs

    @staticmethod
    def _all_triggers(map_id: str) -> set:
        return {(c["x"], c["y"]) for c in maps()[map_id]["coords"] if c.get("type") == "trigger"}

    def _in_view(self, x: int, y: int) -> bool:
        px, py = self.game.pos()
        return abs(px - x) <= 8 and abs(py - y) <= 6

    def _flag(self, name: str) -> bool:
        try:
            return self.game.flag(name)
        except KeyError:
            return False

    # -- transitions ------------------------------------------------------------
    @staticmethod
    def warp_at(map_id: str, x: int, y: int) -> dict | None:
        for w in maps()[map_id]["warps"]:
            if w["x"] == x and w["y"] == y:
                return w
        return None

    def arrive(self, warp: dict, facing: str = "down") -> State | None:
        dest, dest_warp = warp["dest"], warp["dest_warp"]
        if dest == "MAP_DYNAMIC":
            # SaveBlock1.dynamicWarp: s8 group, s8 num, s8 warpId, pad, s16 x, s16 y
            import struct
            from .symbols import map_id
            raw = self.emu.read(self.game.sb1() + 0x14, 8)
            dest = map_id(raw[0], raw[1])
            dest_warp = raw[2] if raw[2] != 0xFF else None
            if dest_warp is None:
                x, y = struct.unpack_from("<hh", raw, 4)
                if dest not in maps():
                    return None
                g = self.grid(dest)
                return State(dest, x, y, g.elevation(x, y) if g.inside(x, y) else 3)
        if dest not in maps() or not isinstance(dest_warp, int):
            return None
        dws = maps()[dest]["warps"]
        if dest_warp >= len(dws):
            return None
        dw = dws[dest_warp]
        g = self.grid(dest)
        x, y = dw["x"], dw["y"]
        if g.inside(x, y):
            b = g.behavior(x, y)
            # Task_ExitDoor walks south; Task_ExitNonAnimDoor walks one step in
            # the direction the player faced when entering (if it can).
            step = "down" if b == DOOR else facing if b in EXIT_SOUTH else None
            if step:
                dx, dy = DELTA[step]
                if g.inside(x + dx, y + dy) and not g.collision(x + dx, y + dy):
                    x, y = x + dx, y + dy
        elev = g.elevation(x, y) if g.inside(x, y) else 3
        # Arriving on an elevation-0 (transition) tile leaves the player at 0,
        # free to step onto any level; 15 (multi-level) keeps the default.
        # Through a water door you arrive still surfing.
        wet = g.inside(x, y) and (surfable(g.behavior(x, y)) or elev == 1)
        return State(dest, x, y, 3 if elev == 15 else elev, wet)

    def edge(self, s: State, d: str) -> State | None:
        info = maps()[s.map]
        for c in info["connections"]:
            if c["direction"] != d or c["map"] not in maps():
                continue
            ng = self.grid(c["map"])
            off = c["offset"]
            if d in ("up", "down"):
                nx, ny = s.x - off, (ng.h - 1 if d == "up" else 0)
            else:
                nx, ny = (ng.w - 1 if d == "left" else 0), s.y - off
            if not ng.inside(nx, ny) or ng.collision(nx, ny):
                continue
            te = ng.elevation(nx, ny)
            if s.elev not in (0,) and te not in (0, 15) and te != s.elev:
                continue
            return State(c["map"], nx, ny, s.elev if te in (0, 15) else te, s.surfing)
        return None

    # -- map graph ----------------------------------------------------------------
    _map_graph: dict[str, set[str]] | None = None

    @classmethod
    def map_graph(cls) -> dict[str, set[str]]:
        """Static map adjacency (warps + connections), for goal-directed search."""
        if cls._map_graph is None:
            g: dict[str, set[str]] = {}
            for mid, info in maps().items():
                out = g.setdefault(mid, set())
                for w in info["warps"]:
                    if w["dest"] in maps():
                        out.add(w["dest"])
                for c in info["connections"]:
                    if c["map"] in maps():
                        out.add(c["map"])
                for sw in info.get("script_warps", ()):
                    out.add(sw["dest"])
            for tr in TRANSPORTS:
                g.setdefault(tr["map"], set()).add(tr["dest"][0])
            cls._map_graph = g
        return cls._map_graph

    @classmethod
    def map_distances(cls, goals: set[str]) -> dict[str, int]:
        """Hops from every map to the nearest goal map (reverse BFS)."""
        rev: dict[str, set[str]] = {}
        for a, outs in cls.map_graph().items():
            for b in outs:
                rev.setdefault(b, set()).add(a)
        dist = {g: 0 for g in goals}
        frontier = list(goals)
        while frontier:
            nxt = []
            for m in frontier:
                for p in rev.get(m, ()):
                    if p not in dist:
                        dist[p] = dist[m] + 1
                        nxt.append(p)
            frontier = nxt
        return dist

    # -- search -------------------------------------------------------------------
    def plan(self, start: State, goal, caps: NavCaps, live: MapGrid | None = None,
             live_objects=None, max_nodes: int = 800_000) -> list[Step] | None:
        """Cheapest action sequence from start to a state satisfying goal(State).

        Searches a corridor of maps around the shortest map path first and
        widens it if that fails (one-way ledges and transports can make the
        real route longer than the map graph suggests)."""
        for slack in (2, 6, None):
            r = self._plan(start, goal, caps, live, live_objects, max_nodes, slack)
            if r is not None or not getattr(goal, "maps", None):
                return r
        return None

    def _plan(self, start: State, goal, caps: NavCaps, live, live_objects, max_nodes,
              slack) -> list[Step] | None:
        grids: dict[str, MapGrid] = {}
        obst: dict[str, Obstacles] = {}

        def G(m):
            if m not in grids:
                grids[m] = self.grid(m, live)
            return grids[m]

        def O(m):
            if m not in obst:
                obst[m] = self.obstacles(m, live_objects if (live and m == live.map_id) else None,
                                         ignore_story=caps.ignore_story_objects)
            return obst[m]

        gcaps = caps.grid_caps()
        goal_tiles = getattr(goal, "tiles", set())
        goal_maps = getattr(goal, "maps", None)
        # Goal-directed: map hops to the goal give an admissible heuristic
        # (each hop costs at least one step) and let us skip maps that lead
        # away from it -- without this, Surf opens the whole sea to the search.
        hops = self.map_distances(goal_maps) if goal_maps else None
        limit = (hops.get(start.map, 99) + slack) if (hops and slack is not None) else None

        def h(st: State) -> float:
            return float(hops.get(st.map, 50)) if hops else 0.0

        self._h = h
        tie = itertools.count()
        dist = {start: 0.0}
        prev: dict[State, tuple[State, Step]] = {}
        heap = [(h(start), next(tie), start)]
        expanded = 0
        while heap:
            _f, _, s = heapq.heappop(heap)
            cost = dist.get(s, 1e18)
            if limit is not None and hops.get(s.map, 99) > limit:
                continue
            if goal(s):
                return self._unwind(prev, start, s)
            expanded += 1
            if expanded > max_nodes:
                return None
            g, ob = G(s.map), O(s.map)
            here_b = g.behavior(s.x, s.y) if g.inside(s.x, s.y) else 0
            # Dive / emerge: press A on the current tile (TrySetDiveWarp).
            if caps.dive and s.surfing:
                for c in maps()[s.map]["connections"]:
                    if c["direction"] == "dive" and here_b in DIVEABLE and c["map"] in maps():
                        dg = G(c["map"])
                        if dg.inside(s.x, s.y) and not dg.collision(s.x, s.y):
                            self._relax((State(c["map"], s.x, s.y, dg.elevation(s.x, s.y), True),
                                         "dive", 10.0), s, "up", dist, prev, heap, tie, cost)
                    if c["direction"] == "emerge" and here_b not in NO_EMERGE and c["map"] in maps():
                        eg = G(c["map"])
                        if eg.inside(s.x, s.y) and not eg.collision(s.x, s.y):
                            ee = eg.elevation(s.x, s.y)
                            self._relax((State(c["map"], s.x, s.y, 3 if ee == 15 else ee, True),
                                         "dive", 10.0), s, "up", dist, prev, heap, tie, cost)
            for d in DELTA:
                nxt: list[tuple[State, str, float]] = []
                # Arrow warps fire when pressing their direction on them.
                if here_b in ARROW_WARPS[d]:
                    w = self.warp_at(s.map, s.x, s.y)
                    if w and (a := self.arrive(w, d)):
                        nxt.append((a, "arrow", 2.0))
                        for item in nxt:
                            self._relax(item, s, d, dist, prev, heap, tie, cost)
                        continue
                dx, dy = DELTA[d]
                tx, ty = s.x + dx, s.y + dy
                # Scripted transports (cable car): talk to the attendant.
                for tr in TRANSPORTS:
                    if tr["map"] != s.map:
                        continue
                    npc = next(o for o in maps()[s.map]["objects"] if o["local_id"] == tr["npc"])
                    if (npc["x"], npc["y"]) == (tx, ty) or (
                            (npc["x"], npc["y"]) == (tx + dx, ty + dy) and g.inside(tx, ty)
                            and g.behavior(tx, ty) == MB["MB_COUNTER"]):
                        dm, dxx, dyy = tr["dest"]
                        nxt.append((State(dm, dxx, dyy, 3), "transport", tr["cost"]))
                # Scripted doors (signs whose script warps): face, press A, confirm.
                for sw in maps()[s.map].get("script_warps", ()):
                    if (sw["sx"], sw["sy"]) == (tx, ty) and sw["dest"] in maps():
                        dg = self.grid(sw["dest"])
                        de = dg.elevation(sw["x"], sw["y"]) if dg.inside(sw["x"], sw["y"]) else 3
                        nxt.append((State(sw["dest"], sw["x"], sw["y"],
                                          de if de not in (0, 15) else 3), "bgwarp", 8.0))
                # Doors: press north into them from the tile below.
                if d == "up" and g.inside(tx, ty) and g.behavior(tx, ty) == DOOR:
                    w = self.warp_at(s.map, tx, ty)
                    if w and (a := self.arrive(w, "up")):
                        nxt.append((a, "door", 2.0))
                blocked = ob.walls | ob.trees | ob.rocks
                if not caps.ignore_boulders:
                    blocked = blocked | ob.boulders
                if caps.triggers_block:
                    blocked = blocked | ob.triggers | self._all_triggers(s.map)
                elif caps.active_triggers_block and s.map != caps.trigger_exempt_map:
                    blocked = blocked | ob.triggers
                if goal_tiles:
                    blocked = blocked - {(gx, gy) for (gm, gx, gy) in goal_tiles if gm == s.map}
                hole = maps()[s.map].get("hole_warp")
                if (hole and hole in maps() and not s.surfing and g.inside(tx, ty)
                        and g.behavior(tx, ty) in FALL_TILES and not g.collision(tx, ty)
                        and (tx, ty) not in blocked):
                    # Cracked floor / hole (walking pace): we drop to the same
                    # spot a floor down (setholewarp). Sky Pillar needs this.
                    dg = self.grid(hole)
                    de = dg.elevation(tx, ty) if dg.inside(tx, ty) else 3
                    nxt.append((State(hole, tx, ty, de if de not in (0, 15) else 3),
                                "warp", 4.0))
                    for item in nxt:
                        self._relax(item, s, d, dist, prev, heap, tie, cost)
                    continue
                r = g.step(s.pos, d, gcaps, blocked)
                if r == "edge":
                    e = self.edge(s, d)
                    if e:
                        nxt.append((e, "edge", 1.0))
                elif r is not None:
                    ns = State(s.map, r.x, r.y, r.elev, r.surfing)
                    action = ("jump" if abs(r.x - s.x) + abs(r.y - s.y) == 2 else
                              "surf" if r.surfing and not s.surfing else "walk")
                    step_cost = 1.0
                    if caps.avoid_grass and has_encounters(g.behavior(r.x, r.y)):
                        step_cost += caps.avoid_grass
                    if caps.avoid_triggers and (r.x, r.y) in ob.triggers:
                        step_cost += 40
                    if (r.x, r.y) in ob.soft:
                        step_cost += 25
                    if action == "surf":
                        step_cost += 6
                    tb = g.behavior(r.x, r.y)
                    w = self.warp_at(s.map, r.x, r.y) if tb in STEP_WARPS else None
                    if w:
                        a = self.arrive(w, d)
                        if a:
                            nxt.append((a, "warp", step_cost + 2))
                        # stepping on a warp tile always warps; no plain state
                    else:
                        nxt.append((ns, action, step_cost))
                elif g.inside(tx, ty):
                    # Something clearable in the way?
                    if (tx, ty) in ob.trees and caps.cut and not s.surfing:
                        nxt.append((State(s.map, tx, ty, s.elev), "cut", 12.0))
                    elif (tx, ty) in ob.rocks and caps.smash and not s.surfing:
                        nxt.append((State(s.map, tx, ty, s.elev), "smash", 12.0))
                for item in nxt:
                    self._relax(item, s, d, dist, prev, heap, tie, cost)
        return None

    def _relax(self, item, s, d, dist, prev, heap, tie, cost):
        ns, action, c = item
        nc = cost + c
        if nc < dist.get(ns, 1e18):
            dist[ns] = nc
            prev[ns] = (s, Step(action, d, ns))
            heapq.heappush(heap, (nc + self._h(ns), next(tie), ns))

    _h = staticmethod(lambda st: 0.0)

    @staticmethod
    def _unwind(prev, start, end) -> list[Step]:
        out = []
        s = end
        while s != start:
            p, step = prev[s]
            out.append(step)
            s = p
        return out[::-1]


# -- goal helpers ------------------------------------------------------------------

def at(map_id: str, x: int | None = None, y: int | None = None):
    """Goal: be on map_id (optionally at a tile)."""
    def goal(s: State) -> bool:
        return s.map == map_id and (x is None or (s.x == x and s.y == y))
    goal.__name__ = f"at({map_id},{x},{y})"
    # A goal tile may itself be a story trigger we mean to step on.
    goal.tiles = {(map_id, x, y)} if x is not None else set()
    goal.maps = {map_id}
    return goal


def at_any(map_id: str, tiles):
    """Goal: stand on any of `tiles` of map_id (e.g. any tile of a trigger)."""
    tiles = {tuple(t) for t in tiles}

    def goal(s: State) -> bool:
        return s.map == map_id and (s.x, s.y) in tiles
    goal.__name__ = f"at_any({map_id},{sorted(tiles)})"
    goal.tiles = {(map_id, x, y) for x, y in tiles}
    goal.maps = {map_id}
    return goal


def adjacent(map_id: str, x: int, y: int):
    """Goal: stand next to (x, y) -- to talk to someone or face something."""
    def goal(s: State) -> bool:
        return s.map == map_id and abs(s.x - x) + abs(s.y - y) == 1
    goal.__name__ = f"adjacent({map_id},{x},{y})"
    goal.maps = {map_id}
    return goal


def facing_dir(sx: int, sy: int, tx: int, ty: int) -> str:
    if tx > sx:
        return "right"
    if tx < sx:
        return "left"
    return "down" if ty > sy else "up"
