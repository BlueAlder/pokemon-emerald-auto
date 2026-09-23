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
                   walls: set, elev: int = 3, max_nodes: int = 400_000) -> list[str] | None:
    """Sokoban search with the game's Strength rule (TryPushBoulder).

    Walking into a boulder pushes it one tile if the tile beyond is free (map
    collision, elevation and other objects); the player stays where they are.
    State is (player, boulders); returns the direction presses.
    """
    from .mapgrid import Caps, MB
    caps = Caps()
    door = {MB.get("MB_NON_ANIMATED_DOOR"), MB.get("MB_WATER_DOOR"), MB.get("MB_DEEP_SOUTH_WARP")}
    root = (start, boulders)
    seen = {root: None}
    q = deque([root])
    n = 0
    while q:
        node = q.popleft()
        (x, y), bs = node
        if goal(x, y, bs):
            path = []
            while seen[node] is not None:
                node, d = seen[node]
                path.append(d)
            return path[::-1]
        n += 1
        if n > max_nodes:
            return None
        for d, (dx, dy) in DELTA.items():
            tx, ty = x + dx, y + dy
            if (tx, ty) in bs:
                bx, by = tx + dx, ty + dy
                if not grid.inside(bx, by) or grid.collision(bx, by) or (bx, by) in bs \
                        or (bx, by) in walls or grid.behavior(bx, by) in door:
                    continue
                be = grid.elevation(bx, by)
                if be not in (0, 15) and be != elev:
                    continue
                nxt = ((x, y), (bs - {(tx, ty)}) | {(bx, by)})
            else:
                r = grid.step(Pos(x, y, elev), d, caps, walls | bs)
                if r is None or r == "edge":
                    continue
                nxt = ((r.x, r.y), bs)
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
