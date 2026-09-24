"""Puzzles the generic planner cannot see, modelled from the game's own code.

Fortree Gym's rotating gates (rotating_gate.c): gates are sprites whose arms
swing when pushed. Moving into a cell next to a gate hub either rotates the gate
(if the pushed arm exists and its sweep is clear of walls) or is blocked. The
orientations live in VAR_TEMP_0.. bytes, so the solver reads the live puzzle
state and runs a breadth-first search over (position, all gate orientations).
"""
from __future__ import annotations

import logging
from collections import deque

from .mapgrid import DELTA, MapGrid, Pos
from .symbols import const, maps, symbols

log = logging.getLogger("pokeauto")
S = symbols()

ARM_N, ARM_E, ARM_S, ARM_W = 0, 1, 2, 3
ROT_ACW, ROT_CW = 1, 2
NONE = 255


def _rot(direction, arm, long_arm):
    return ((direction & 15) << 4) | ((arm & 7) << 1) | (long_arm & 1)


CW = lambda arm, l: _rot(ROT_CW, arm, l)      # noqa: E731
ACW = lambda arm, l: _rot(ROT_ACW, arm, l)    # noqa: E731

ROTATION_INFO = {
    "up": [NONE] * 4 + [CW(ARM_W, 1), CW(ARM_W, 0), ACW(ARM_E, 0), ACW(ARM_E, 1)] + [NONE] * 8,
    "down": [NONE] * 8 + [ACW(ARM_W, 1), ACW(ARM_W, 0), CW(ARM_E, 0), CW(ARM_E, 1)] + [NONE] * 4,
    "left": [NONE, ACW(ARM_N, 1), NONE, NONE,
             NONE, ACW(ARM_N, 0), NONE, NONE,
             NONE, CW(ARM_S, 0), NONE, NONE,
             NONE, CW(ARM_S, 1), NONE, NONE],
    "right": [NONE, NONE, CW(ARM_N, 1), NONE,
              NONE, NONE, CW(ARM_N, 0), NONE,
              NONE, NONE, ACW(ARM_S, 0), NONE,
              NONE, NONE, ACW(ARM_S, 1), NONE],
}
ARM_POS = {
    ROT_CW: [(0, -1), (1, -2), (0, 0), (1, 0), (-1, 0), (-1, 1), (-1, -1), (-2, -1)],
    ROT_ACW: [(-1, -1), (-1, -2), (0, -1), (1, -1), (0, 0), (0, 1), (-1, 0), (-2, 0)],
}
SHAPES = {  # arm layout: [N short, N long, E short, E long, S short, S long, W, W]
    "L1": [1, 0, 1, 0, 0, 0, 0, 0], "L2": [1, 1, 1, 0, 0, 0, 0, 0],
    "L3": [1, 0, 1, 1, 0, 0, 0, 0], "L4": [1, 1, 1, 1, 0, 0, 0, 0],
    "T1": [1, 0, 1, 0, 1, 0, 0, 0], "T2": [1, 1, 1, 0, 1, 0, 0, 0],
    "T3": [1, 0, 1, 1, 1, 0, 0, 0], "T4": [1, 0, 1, 0, 1, 1, 0, 0],
}
FORTREE_GATES = [(6, 7, "T2", 1), (9, 15, "T2", 2), (3, 19, "T2", 1), (2, 6, "T1", 1),
                 (9, 12, "T1", 0), (6, 23, "T1", 0), (12, 22, "T1", 0), (6, 3, "L4", 2)]


class GatePuzzle:
    def __init__(self, grid: MapGrid, gates=FORTREE_GATES):
        self.grid = grid
        self.gates = gates

    def has_arm(self, gi: int, orient: int, arm_info: int) -> bool:
        arm, long_arm = arm_info // 2, arm_info % 2
        rel = (arm - orient + 4) % 4
        return bool(SHAPES[self.gates[gi][2]][rel * 2 + long_arm])

    def can_rotate(self, gi: int, orient: int, rot: int) -> bool:
        gx, gy, shape, _ = self.gates[gi]
        pos = ARM_POS[rot]
        for i in range(4):
            for j in range(2):
                if SHAPES[shape][2 * i + j]:
                    dx, dy = pos[2 * ((orient + i) % 4) + j]
                    x, y = gx + dx, gy + dy
                    if self.grid.inside(x, y) and self.grid.collision(x, y) == 1:
                        return False
        return True

    def gate_move(self, d: str, x: int, y: int, orients: tuple) -> tuple | None:
        """Moving in direction d onto (x, y): new orientations, or None if blocked."""
        orients = list(orients)
        for gi, (gx, gy, _shape, _o) in enumerate(self.gates):
            if gx - 2 <= x <= gx + 1 and gy - 2 <= y <= gy + 1:
                cx, cy = x - gx + 2, y - gy + 2
                info = ROTATION_INFO[d][cy * 4 + cx]
                if info == NONE:
                    continue
                rot, arm_info = (info & 0xF0) >> 4, info & 0xF
                if self.has_arm(gi, orients[gi], arm_info):
                    if self.can_rotate(gi, orients[gi], rot):
                        orients[gi] = (orients[gi] - 1) % 4 if rot == ROT_ACW else (orients[gi] + 1) % 4
                        return tuple(orients)
                    return None
        return tuple(orients)

    def solve(self, start: tuple[int, int], orients: tuple, goal, blocked: set,
              elev: int = 3, max_nodes: int = 2_000_000) -> list[str] | None:
        from .mapgrid import Caps
        caps = Caps()
        seen = {(start, orients): None}
        q = deque([(start, orients)])
        n = 0
        while q:
            (x, y), o = node = q.popleft()
            if goal(x, y):
                path = []
                while seen[node] is not None:
                    node, d = seen[node]
                    path.append(d)
                return path[::-1]
            n += 1
            if n > max_nodes:
                return None
            for d in DELTA:
                r = self.grid.step(Pos(x, y, elev), d, caps, blocked)
                if r is None or r == "edge" or abs(r.x - x) + abs(r.y - y) != 1:
                    continue
                no = self.gate_move(d, r.x, r.y, o)
                if no is None:
                    continue
                nxt = ((r.x, r.y), no)
                if nxt not in seen:
                    seen[nxt] = (node, d)
                    q.append(nxt)
        return None


def solve_boulders(grid: MapGrid, start: tuple[int, int], boulders: frozenset, goal,
                   walls: set, elev: int = 3, max_nodes: int = 400_000,
                   time_limit: float = 20.0) -> list[str] | None:
    """Sokoban search with the game's Strength rule (TryPushBoulder).

    Walking into a boulder pushes it one tile if the tile beyond is free (map
    collision, elevation and other objects); the player stays where they are.
    State is (player, boulders); returns the direction presses.
    """
    from .mapgrid import Caps, MB
    caps = Caps()
    door = {MB.get("MB_NON_ANIMATED_DOOR"), MB.get("MB_WATER_DOOR"), MB.get("MB_DEEP_SOUTH_WARP")}
    import time as _time
    deadline = _time.process_time() + time_limit
    root = ((start[0], start[1], elev), boulders)
    seen = {root: None}
    q = deque([root])
    n = 0
    while q:
        node = q.popleft()
        (x, y, e), bs = node
        if goal(x, y, bs):
            path = []
            while seen[node] is not None:
                node, d = seen[node]
                path.append(d)
            return path[::-1]
        n += 1
        if n > max_nodes or (n % 2000 == 0 and _time.process_time() > deadline):
            return None
        for d, (dx, dy) in DELTA.items():
            tx, ty = x + dx, y + dy
            if (tx, ty) in bs:
                bx, by = tx + dx, ty + dy
                if not grid.inside(bx, by) or grid.collision(bx, by) or (bx, by) in bs \
                        or (bx, by) in walls or grid.behavior(bx, by) in door:
                    continue
                be = grid.elevation(bx, by)
                if be not in (0, 15) and be != e:
                    continue
                nxt = ((x, y, e), (bs - {(tx, ty)}) | {(bx, by)})
            else:
                # (tx, ty) is not a boulder here; only a jump's landing could be.
                r = grid.step(Pos(x, y, e), d, caps, walls)
                if r is None or r == "edge" or (r.x, r.y) in bs:
                    continue
                nxt = ((r.x, r.y, r.elev), bs)
            if nxt not in seen:
                seen[nxt] = (node, d)
                q.append(nxt)
    return None


def read_orientations(game, count: int) -> tuple:
    return tuple(game.emu.read(game.sb1() + 0x139C + (const("VAR_TEMP_0") - 0x4000) * 2, count))


def fortree_gym(agent) -> None:
    """Walk Fortree Gym's rotating gates to Winona and challenge her."""
    gym = "MAP_FORTREE_CITY_GYM"
    winona = next(o for o in maps()[gym]["objects"] if "Winona" in o["script"])
    wx, wy = winona["x"], winona["y"]
    agent.goto(gym)
    for attempt in range(12):
        agent.pump()
        if agent.game.badges() >= 6:
            return
        x, y = agent.game.pos()
        if abs(x - wx) + abs(y - wy) == 1:
            agent.talk(gym, winona["local_id"])
            continue
        grid = MapGrid.from_ram(agent.game)
        puzzle = GatePuzzle(grid)
        orients = read_orientations(agent.game, len(FORTREE_GATES))
        obs = agent.ctl.planner.obstacles(gym, agent.game.objects())
        blocked = obs.walls - {(wx, wy)}
        path = puzzle.solve((x, y), orients, lambda px, py: abs(px - wx) + abs(py - wy) == 1,
                            blocked | {(wx, wy)})
        if path is None:
            log.warning("FORTREE no gate solution from %s %s", (x, y), orients)
            return
        log.info("FORTREE gate path: %d steps (%s)", len(path), "".join(p[0] for p in path))
        for d in path:
            before = agent.game.pos()
            agent.ctl._hold_until_moved(d)
            if not agent.ctl.free():
                agent.pump()                 # a trainer spotted us; re-solve after
                break
            if agent.game.pos() == before:
                break                        # model disagreed; re-read and re-solve


# -- Rotating tile puzzle (rotating_tile_puzzle.c; Mossdeep Gym) -----------------
#
# Coloured arrow metatiles form 2x2 turntables. Stepping on a floor switch moves
# every object standing on an arrow of that colour one tile along its arrow, so
# the statues on each turntable cycle round it. The model reads the objects'
# saved positions (the puzzle rewrites the save block's templates), simulates
# presses, and flood-fills the walkable area -- same-map warp pads included --
# to find the shortest press sequence that opens a path to the goal.

ROT_TILE_START = 0x250          # METATILE_MossdeepGym_YellowArrow_Right
ROT_COLOURS = ("Yellow", "Blue", "Green", "Purple", "Red")
ROT_DIRS = ((1, 0), (0, 1), (-1, 0), (0, -1))     # right, down, left, up


class RotatingTiles:
    def __init__(self, grid: MapGrid, map_id: str):
        self.grid, self.map_id = grid, map_id
        info = maps()[map_id]
        self.arrows = {}
        for y in range(grid.h):
            for x in range(grid.w):
                k = (grid.raw(x, y) & 0x3FF) - ROT_TILE_START
                if 0 <= k < 8 * len(ROT_COLOURS) and k % 8 < 4:
                    self.arrows[(x, y)] = (k // 8, ROT_DIRS[k % 8])
        self.switches, self.avoid = {}, set()
        for c in info["coords"]:
            if c.get("type") != "trigger":
                continue
            col = next((i for i, n in enumerate(ROT_COLOURS)
                        if f"{n}FloorSwitch" in (c.get("script") or "")), None)
            if col is None:
                self.avoid.add((c["x"], c["y"]))     # e.g. the warp back to the entrance
            else:
                self.switches[(c["x"], c["y"])] = col
        warps = info["warps"]
        self.pads = {}
        for w in warps:
            if w["dest"] == map_id:
                d = warps[int(w["dest_warp"])]
                self.pads[(w["x"], w["y"])] = (d["x"], d["y"])

    def press(self, objs: tuple, colour: int) -> tuple:
        out = []
        for (x, y) in objs:
            a = self.arrows.get((x, y))
            if a and a[0] == colour:
                x, y = x + a[1][0], y + a[1][1]
            out.append((x, y))
        return tuple(out)

    def flood(self, start: Pos, objs: tuple):
        """Tiles reachable without pressing anything, and the switches bordering them."""
        from .mapgrid import Caps
        blocked = set(objs)
        seen = {(start.x, start.y)}
        q = deque([start])
        hits = set()
        while q:
            p = q.popleft()
            for d in DELTA:
                r = self.grid.step(p, d, Caps(), blocked)
                if r is None or r == "edge":
                    continue
                t = (r.x, r.y)
                if t in self.avoid:
                    continue
                if t in self.switches:
                    hits.add(t)
                    continue
                if t in self.pads:
                    dx, dy = self.pads[t]
                    e = self.grid.elevation(dx, dy)
                    r = Pos(dx, dy, r.elev if e in (0, 15) else e)
                    t = (dx, dy)
                if t in seen:
                    continue
                seen.add(t)
                q.append(r)
        return seen, hits

    def solve(self, start: Pos, objs: tuple, goal, limit: int = 20000):
        """Shortest list of switch tiles to press so that goal(x, y) becomes reachable."""
        prev = {(objs, (start.x, start.y)): None}
        q = deque([(objs, start)])
        while q and len(prev) < limit:
            objs, pos = q.popleft()
            seen, hits = self.flood(pos, objs)
            if any(goal(x, y) for (x, y) in seen):
                path, k = [], (objs, (pos.x, pos.y))
                while prev[k] is not None:
                    k, sw = prev[k]
                    path.append(sw)
                return path[::-1]
            if (pos.x, pos.y) in self.switches:
                hits.add((pos.x, pos.y))           # step off and back on
            for sw in sorted(hits):
                nobjs = self.press(objs, self.switches[sw])
                k = (nobjs, sw)
                if k in prev:
                    continue
                prev[k] = ((objs, (pos.x, pos.y)), sw)
                q.append((nobjs, Pos(sw[0], sw[1], self.grid.elevation(*sw) or start.elev)))
        return None


def rotating_tile_gym(agent, map_id: str, leader_pattern: str, badge_count: int) -> None:
    """Solve a rotating-tile gym (Mossdeep) and challenge its leader."""
    from .nav import NavCaps, adjacent, at
    info = maps()[map_id]
    leaders = [o for o in info["objects"] if leader_pattern in o["script"]]
    ids = [o["local_id"] for o in leaders]
    agent.goto(map_id)
    ctl = agent.ctl
    caps = NavCaps(**{**ctl.nav_caps().__dict__, "avoid_triggers": True})
    for attempt in range(40):
        agent.pump()
        if agent.game.badges() >= badge_count:
            return
        if agent.game.map_id() != map_id:
            agent.goto(map_id)            # e.g. after a whiteout
            continue
        # Pushback "lessons" about switch tiles are wrong here: the model knows.
        ctl.planner.learned_blocks = {b for b in ctl.planner.learned_blocks if b[0] != map_id}
        g = agent.game
        spots = {o.local_id: (o.x, o.y) for o in g.objects() if not o.is_player}
        saved = g.object_templates()
        objs = tuple(spots.get(t["local_id"], saved.get(t["local_id"], (t["x"], t["y"])))
                     for t in info["objects"])
        leader_xy = [spots.get(i, saved.get(i)) for i in ids]
        s = ctl.state()
        model = RotatingTiles(MapGrid.from_ram(g), map_id)

        def near(x, y):
            return any(abs(x - lx) + abs(y - ly) == 1 for lx, ly in leader_xy)

        if near(s.x, s.y):
            agent.talk(map_id, ids[0])
            continue
        plan = model.solve(Pos(s.x, s.y, s.elev), objs, near)
        if plan is None:
            raise RuntimeError(f"rotating tiles: no solution from {s}")
        log.info("ROTATING TILES: press %s", plan)
        if not plan:
            agent.talk(map_id, ids[0])
            continue
        t = plan[0]
        if (s.x, s.y) == t:
            ctl.goto(adjacent(map_id, *t), caps=caps, desc="step off switch")
        ctl.goto(at(map_id, *t), caps=caps, desc=f"{ROT_COLOURS[model.switches[t]]} switch")
        agent.pump()
    raise RuntimeError("rotating tiles: gave up")


# -- Thin ice (field_tasks.c SootopolisGymIcePerStepCallback; Sootopolis Gym) -----
#
# Stepping on thin ice cracks it and counts toward VAR_ICE_STEP_COUNT; stepping
# on cracked ice breaks it and drops you a floor. Each room's stairs (slide
# tiles until then) open once every thin tile in the room has been cracked, so
# a room is a Hamiltonian path over its ice that ends beside the closed stairs.

def ice_path(start: tuple[int, int] | None, tiles: set, ends: set,
             first: set | None = None, limit: int = 2_000_000):
    """Visit every tile in `tiles` exactly once, 4-connected, finishing in `ends`.

    From `start` (a tile already stood on, not in `tiles`) or, if None, from
    any tile of `first`. Returns the tile sequence or None.
    """
    nb = {t: [(t[0] + dx, t[1] + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
              if (t[0] + dx, t[1] + dy) in tiles] for t in tiles}
    budget = [limit]

    def connected(rem: set) -> bool:
        if not rem:
            return True
        seed = next(iter(rem))
        seen, stack = {seed}, [seed]
        while stack:
            for n in nb[stack.pop()]:
                if n in rem and n not in seen:
                    seen.add(n)
                    stack.append(n)
        return len(seen) == len(rem)

    def dfs(cur, rem: set, path: list):
        budget[0] -= 1
        if budget[0] < 0:
            return None
        if not rem:
            return path if cur in ends else None
        if not connected(rem):
            return None
        # Tiles with one way in must be the end of the path: at most one.
        dead = [t for t in rem if sum(1 for n in nb[t] if n in rem or n == cur) <= 1]
        if len(dead) > 1 or (dead and dead[0] not in ends):
            return None
        options = [n for n in nb.get(cur, ()) if n in rem] if cur in nb else \
            [(cur[0] + dx, cur[1] + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
             if (cur[0] + dx, cur[1] + dy) in rem]
        options.sort(key=lambda n: sum(1 for m in nb[n] if m in rem))   # Warnsdorff
        for n in options:
            r = dfs(n, rem - {n}, path + [n])
            if r:
                return r
        return None

    if start is not None:
        return dfs(start, set(tiles), [])
    for f in sorted(first or tiles):
        r = dfs(f, set(tiles) - {f}, [f])
        if r:
            return r
    return None


def ice_gym(agent, map_id: str, leader_pattern: str, badge_count: int) -> None:
    """Crack every thin-ice tile room by room, then challenge the leader."""
    from .mapgrid import MB as _MB
    from .nav import at
    from .symbols import const
    THIN, CRACKED = const("MB_THIN_ICE"), const("MB_CRACKED_ICE")
    SLIDE = const("MB_SLIDE_SOUTH")
    info = maps()[map_id]
    leader = next(o for o in info["objects"] if leader_pattern in o["script"])
    ctl = agent.ctl
    for attempt in range(30):
        agent.pump()
        if agent.game.badges() >= badge_count:
            return
        if agent.game.map_id() != map_id:
            agent.goto(map_id)
            continue
        g = MapGrid.from_ram(agent.game)
        ice = {(x, y) for y in range(g.h) for x in range(g.w)
               if g.behavior(x, y) in (THIN, CRACKED)}
        thin = {t for t in ice if g.behavior(*t) == THIN}
        saved = set(ctl.planner.learned_blocks)
        try:
            # Ice is never crossed by the ordinary planner.
            ctl.planner.learned_blocks |= {(map_id, x, y) for x, y in ice}
            lx, ly = leader["x"], leader["y"]
            s = ctl.state()
            # Mid-room means standing on ice with uncracked ice next to us.
            mid_room = (s.x, s.y) in ice and any(
                (s.x + dx, s.y + dy) in thin for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)))
            if not mid_room:
                goal = lambda st: st.map == map_id and abs(st.x - lx) + abs(st.y - ly) == 1
                if ctl.planner.plan(s, goal, ctl.nav_caps(), live=g,
                                    live_objects=agent.game.objects()) is not None:
                    agent.talk(map_id, leader["local_id"])
                    continue
            # Rooms: components of uncracked ice.
            rooms, left = [], set(thin)
            while left:
                seed = left.pop()
                comp, stack = {seed}, [seed]
                while stack:
                    cx, cy = stack.pop()
                    for n in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                        if n in left:
                            left.discard(n)
                            comp.add(n)
                            stack.append(n)
                rooms.append(comp)
            plan_room = None
            for comp in rooms:
                ends = {t for t in comp if any(g.inside(t[0] + dx, t[1] + dy)
                                               and g.behavior(t[0] + dx, t[1] + dy) == SLIDE
                                               for dx, dy in ((0, -1), (0, 1), (1, 0), (-1, 0)))}
                if not ends:
                    continue
                if mid_room:
                    if not any(abs(s.x - x) + abs(s.y - y) == 1 for x, y in comp):
                        continue
                    path = ice_path((s.x, s.y), comp, ends)
                    if path:
                        plan_room = ([], path)
                        break
                    continue
                # Entry: an ice tile next to floor we can walk to.
                firsts = []
                for (x, y) in comp:
                    for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                        f = (x + dx, y + dy)
                        if not g.inside(*f) or f in ice or g.collision(*f) \
                                or g.behavior(*f) == SLIDE:
                            continue
                        p = ctl.planner.plan(s, at(map_id, *f), ctl.nav_caps(), live=g,
                                             live_objects=agent.game.objects())
                        if p is not None:
                            firsts.append(((x, y), f, len(p)))
                if not firsts:
                    continue
                for (t, f, _) in sorted(firsts, key=lambda z: z[2]):
                    path = ice_path(None, comp, ends, first={t})
                    if path:
                        plan_room = ([f], path)
                        break
                if plan_room:
                    break
        finally:
            ctl.planner.learned_blocks.clear()
            ctl.planner.learned_blocks |= saved
        if plan_room is None:
            raise RuntimeError(f"ice gym: no way forward from {ctl.state()}")
        approach, path = plan_room
        log.info("ICE room: %d tiles from %s", len(path), approach or "here")
        if approach:
            saved = set(ctl.planner.learned_blocks)
            try:
                ctl.planner.learned_blocks |= {(map_id, x, y) for x, y in ice}
                ctl.goto(at(map_id, *approach[0]), desc="ice room entrance")
            finally:
                ctl.planner.learned_blocks.clear()
                ctl.planner.learned_blocks |= saved
        for (x, y) in path:
            cx, cy = agent.game.pos()
            d = {(1, 0): "right", (-1, 0): "left", (0, 1): "down", (0, -1): "up"}.get((x - cx, y - cy))
            if d is None:
                break                          # displaced (battle, fall): re-read and re-solve
            ctl._hold_until_moved(d)
            if agent.game.mode().kind != "overworld" or not ctl.free():
                agent.pump()
                break
            if agent.game.pos() != (x, y):
                break
        ctl.idle(60)                           # let the stairs open
    raise RuntimeError("ice gym: gave up")
