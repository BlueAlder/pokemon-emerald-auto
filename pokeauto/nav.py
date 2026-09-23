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
                      has_encounters, MB)
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
    avoid_grass: float = 0.0    # extra cost per tall-grass step (encounters)
    avoid_triggers: bool = True

    def grid_caps(self) -> Caps:
        return Caps(surf=self.surf, waterfall=self.waterfall)


@dataclass
class Obstacles:
    walls: set = field(default_factory=set)       # impassable (x, y)
    trees: set = field(default_factory=set)       # cuttable
    rocks: set = field(default_factory=set)       # smashable
    triggers: set = field(default_factory=set)    # active coord scripts (soft)


class Planner:
    def __init__(self, game):
        self.game = game
        self.emu = game.emu

    # -- per-map data -----------------------------------------------------------
    def grid(self, map_id: str, live: MapGrid | None = None) -> MapGrid:
        if live is not None and live.map_id == map_id:
            return live
        return MapGrid.from_rom(self.emu, map_id)

    def obstacles(self, map_id: str, live_objects=None) -> Obstacles:
        """Objects in the way on `map_id`.

        live_objects (Game.objects() on the current map) wins for anything it
        covers; templates fill in objects not currently spawned.
        """
        info = maps()[map_id]
        obs = Obstacles()
        spawned = set()
        if live_objects is not None:
            for o in live_objects:
                if o.is_player or o.invisible:
                    continue
                spawned.add(o.local_id)
                target = (obs.trees if o.graphics_id == GFX_TREE else
                          obs.rocks if o.graphics_id == GFX_ROCK else obs.walls)
                target.add((o.x, o.y))
        for t in info["objects"]:
            if t["local_id"] in spawned:
                continue
            flag = t["flag"]
            if flag and flag != "0" and self._flag(flag):
                continue            # hidden by its flag
            gfx = const(t["gfx"]) if t["gfx"] in _CONSTS else -1
            mv = const(t["movement"]) if t["movement"] in _CONSTS else 0
            if gfx == GFX_TREE:
                obs.trees.add((t["x"], t["y"]))
            elif gfx == GFX_ROCK:
                obs.rocks.add((t["x"], t["y"]))
            elif mv in STATIONARY or gfx == GFX_BOULDER:
                if live_objects is None or not self._in_view(t["x"], t["y"]):
                    obs.walls.add((t["x"], t["y"]))
        for c in info["coords"]:
            if c.get("type") == "trigger" and c.get("var") and c.get("var") in _CONSTS:
                try:
                    if self.game.var(c["var"]) == int(str(c["var_value"]), 0):
                        obs.triggers.add((c["x"], c["y"]))
                except (ValueError, KeyError):
                    pass
        return obs

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

    def arrive(self, warp: dict) -> State | None:
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
        if g.inside(x, y) and g.behavior(x, y) == DOOR:
            y += 1                       # walk out of the doorway
        elev = g.elevation(x, y) if g.inside(x, y) else 3
        return State(dest, x, y, elev if elev not in (0, 15) else 3, False)

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

    # -- search -------------------------------------------------------------------
    def plan(self, start: State, goal, caps: NavCaps, live: MapGrid | None = None,
             live_objects=None, max_nodes: int = 400_000) -> list[Step] | None:
        """Cheapest action sequence from start to a state satisfying goal(State)."""
        grids: dict[str, MapGrid] = {}
        obst: dict[str, Obstacles] = {}

        def G(m):
            if m not in grids:
                grids[m] = self.grid(m, live)
            return grids[m]

        def O(m):
            if m not in obst:
                obst[m] = self.obstacles(m, live_objects if (live and m == live.map_id) else None)
            return obst[m]

        gcaps = caps.grid_caps()
        tie = itertools.count()
        dist = {start: 0.0}
        prev: dict[State, tuple[State, Step]] = {}
        heap = [(0.0, next(tie), start)]
        expanded = 0
        while heap:
            cost, _, s = heapq.heappop(heap)
            if cost > dist.get(s, 1e18):
                continue
            if goal(s):
                return self._unwind(prev, start, s)
            expanded += 1
            if expanded > max_nodes:
                return None
            g, ob = G(s.map), O(s.map)
            here_b = g.behavior(s.x, s.y) if g.inside(s.x, s.y) else 0
            for d in DELTA:
                nxt: list[tuple[State, str, float]] = []
                # Arrow warps fire when pressing their direction on them.
                if here_b in ARROW_WARPS[d]:
                    w = self.warp_at(s.map, s.x, s.y)
                    if w and (a := self.arrive(w)):
                        nxt.append((a, "arrow", 2.0))
                        for item in nxt:
                            self._relax(item, s, d, dist, prev, heap, tie, cost)
                        continue
                dx, dy = DELTA[d]
                tx, ty = s.x + dx, s.y + dy
                # Doors: press north into them from the tile below.
                if d == "up" and g.inside(tx, ty) and g.behavior(tx, ty) == DOOR:
                    w = self.warp_at(s.map, tx, ty)
                    if w and (a := self.arrive(w)):
                        nxt.append((a, "door", 2.0))
                blocked = ob.walls | ob.trees | ob.rocks
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
                    if action == "surf":
                        step_cost += 6
                    tb = g.behavior(r.x, r.y)
                    w = self.warp_at(s.map, r.x, r.y) if tb in STEP_WARPS else None
                    if w:
                        a = self.arrive(w)
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

    @staticmethod
    def _relax(item, s, d, dist, prev, heap, tie, cost):
        ns, action, c = item
        nc = cost + c
        if nc < dist.get(ns, 1e18):
            dist[ns] = nc
            prev[ns] = (s, Step(action, d, ns))
            heapq.heappush(heap, (nc, next(tie), ns))

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
    return goal


def adjacent(map_id: str, x: int, y: int):
    """Goal: stand next to (x, y) -- to talk to someone or face something."""
    def goal(s: State) -> bool:
        return s.map == map_id and abs(s.x - x) + abs(s.y - y) == 1
    goal.__name__ = f"adjacent({map_id},{x},{y})"
    return goal


def facing_dir(sx: int, sy: int, tx: int, ty: int) -> str:
    if tx > sx:
        return "right"
    if tx < sx:
        return "left"
    return "down" if ty > sy else "up"
