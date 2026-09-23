"""Tile grids and the game's own movement rules.

A MapGrid is one map's tiles: metatile id, collision bit, elevation and
metatile behaviour. It can be read two ways:

* ``MapGrid.from_rom(map_id)`` -- the static layout, for planning routes over
  maps we are not standing in.
* ``MapGrid.from_ram(game)`` -- the live grid (gBackupMapLayout), which also
  reflects in-game edits: opened doors, smashed ice, moved boulders.

``step()`` reproduces field_player_avatar.c/event_object_movement.c:
collision bits, directional one-way tiles, elevation mismatch, ledge jumps,
and getting on/off Surf. Keeping it faithful to the source is the whole point:
the previous agent guessed at walls and oscillated forever.
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from functools import lru_cache

from .symbols import DATA, const, maps, symbols

S = symbols()
MAP_OFFSET = 7
NUM_PRIMARY_METATILES = 512

DELTA = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
OPPOSITE = {"up": "down", "down": "up", "left": "right", "right": "left"}

ELEV_TRANSITION, ELEV_MULTI, ELEV_WATER, ELEV_DEFAULT = 0, 15, 1, 3


@lru_cache(maxsize=1)
def _tile_flags() -> dict[int, int]:
    return {int(k): v for k, v in json.loads((DATA / "tile_flags.json").read_text()).items()}


def _mb(*names: str) -> frozenset[int]:
    return frozenset(const(n) for n in names)


MB = {n: const(n) for n in (
    "MB_NORMAL", "MB_TALL_GRASS", "MB_LONG_GRASS", "MB_ANIMATED_DOOR", "MB_LADDER",
    "MB_NON_ANIMATED_DOOR", "MB_WATER_DOOR", "MB_DEEP_SOUTH_WARP", "MB_WATERFALL",
    "MB_ICE", "MB_CRACKED_FLOOR", "MB_CRACKED_FLOOR_HOLE", "MB_MUDDY_SLOPE",
    "MB_PC", "MB_COUNTER")}

# MetatileBehavior_IsJump* only test the pure cardinal values.
JUMP = {"down": _mb("MB_JUMP_SOUTH"), "up": _mb("MB_JUMP_NORTH"),
        "left": _mb("MB_JUMP_WEST"), "right": _mb("MB_JUMP_EAST")}

_N = _mb("MB_IMPASSABLE_NORTH", "MB_IMPASSABLE_NORTHEAST", "MB_IMPASSABLE_NORTHWEST",
         "MB_IMPASSABLE_SOUTH_AND_NORTH")
_S = _mb("MB_IMPASSABLE_SOUTH", "MB_IMPASSABLE_SOUTHEAST", "MB_IMPASSABLE_SOUTHWEST",
         "MB_IMPASSABLE_SOUTH_AND_NORTH")
_E = _mb("MB_IMPASSABLE_EAST", "MB_IMPASSABLE_NORTHEAST", "MB_IMPASSABLE_SOUTHEAST",
         "MB_IMPASSABLE_WEST_AND_EAST", "MB_SECRET_BASE_BREAKABLE_DOOR")
_W = _mb("MB_IMPASSABLE_WEST", "MB_IMPASSABLE_NORTHWEST", "MB_IMPASSABLE_SOUTHWEST",
         "MB_IMPASSABLE_WEST_AND_EAST", "MB_SECRET_BASE_BREAKABLE_DOOR")
# gDirectionBlockedMetatileFuncs (target tile) / gOpposite... (current tile)
ENTER_BLOCKED = {"down": _N, "up": _S, "left": _E, "right": _W}
LEAVE_BLOCKED = {"down": _S, "up": _N, "left": _W, "right": _E}

STEP_WARPS = _mb("MB_ANIMATED_DOOR", "MB_LADDER", "MB_NON_ANIMATED_DOOR", "MB_WATER_DOOR",
                 "MB_DEEP_SOUTH_WARP", "MB_UP_ESCALATOR", "MB_DOWN_ESCALATOR",
                 "MB_LAVARIDGE_GYM_B1F_WARP", "MB_LAVARIDGE_GYM_1F_WARP",
                 "MB_AQUA_HIDEOUT_WARP", "MB_MT_PYRE_HOLE", "MB_MOSSDEEP_GYM_WARP")
ARROW_WARPS = {"up": _mb("MB_NORTH_ARROW_WARP", "MB_STAIRS_OUTSIDE_ABANDONED_SHIP"),
               "down": _mb("MB_SOUTH_ARROW_WARP"),
               "left": _mb("MB_WEST_ARROW_WARP"),
               "right": _mb("MB_EAST_ARROW_WARP")}

# Tiles that move the player on their own. Unsafe for the generic pathfinder;
# the few puzzles that need them are scripted.
FORCED = frozenset(
    list(range(const("MB_WALK_EAST"), const("MB_TRICK_HOUSE_PUZZLE_8_FLOOR") + 1))
    + list(range(const("MB_EASTWARD_CURRENT"), const("MB_SOUTHWARD_CURRENT") + 1))
    + [const("MB_MUDDY_SLOPE"), const("MB_CRACKED_FLOOR"), const("MB_WATERFALL"),
       const("MB_ICE"), const("MB_SECRET_BASE_JUMP_MAT"), const("MB_SECRET_BASE_SPIN_MAT")])


def surfable(behavior: int) -> bool:
    return bool(_tile_flags().get(behavior, 0) & 2)


def has_encounters(behavior: int) -> bool:
    return bool(_tile_flags().get(behavior, 0) & 1)


@dataclass(frozen=True)
class Caps:
    """Field abilities available right now."""
    surf: bool = False
    waterfall: bool = False
    allow_forced: bool = False


@dataclass(frozen=True)
class Pos:
    x: int
    y: int
    elev: int
    surfing: bool = False


class MapGrid:
    def __init__(self, width: int, height: int, tiles: list[int],
                 behaviors: list[int], map_id: str = ""):
        self.w, self.h = width, height
        self.tiles = tiles          # raw u16 per tile, row-major
        self.beh = behaviors        # metatile behaviour per tile
        self.map_id = map_id

    # -- construction ----------------------------------------------------------
    @staticmethod
    def _attrs(rom_read, layout_ptr: int) -> tuple[list[int], list[int]]:
        prim_ts, sec_ts = struct.unpack("<II", rom_read(layout_ptr + 0x10, 8))
        out = []
        for ts, count in ((prim_ts, NUM_PRIMARY_METATILES), (sec_ts, 512)):
            if not ts:
                out.append([0] * count)
                continue
            attr_ptr = struct.unpack("<I", rom_read(ts + 0x10, 4))[0]
            raw = rom_read(attr_ptr, count * 2)
            n = len(raw) // 2
            out.append([v & 0xFF for v in struct.unpack(f"<{n}H", raw)])
        return out[0], out[1]

    @classmethod
    def from_rom(cls, emu, map_id: str) -> "MapGrid":
        return _static_grid(emu, map_id)

    @classmethod
    def from_ram(cls, game) -> "MapGrid":
        emu = game.emu
        gw, gh, ptr = struct.unpack("<iiI", emu.read(S["gBackupMapLayout"], 12))
        w, h = gw - 15, gh - 14
        raw = emu.read(ptr, gw * gh * 2)
        grid = struct.unpack(f"<{gw * gh}H", raw)
        header_layout = emu.u32(S["gMapHeader"])
        prim, sec = cls._attrs(emu.read, header_layout)
        tiles, beh = [], []
        for y in range(h):
            row = grid[(y + MAP_OFFSET) * gw + MAP_OFFSET:(y + MAP_OFFSET) * gw + MAP_OFFSET + w]
            tiles.extend(row)
        for t in tiles:
            mid = t & 0x3FF
            beh.append(prim[mid] if mid < NUM_PRIMARY_METATILES
                       else sec[mid - NUM_PRIMARY_METATILES] if mid - NUM_PRIMARY_METATILES < len(sec) else 0)
        return cls(w, h, tiles, beh, game.map_id())

    # -- tile queries ----------------------------------------------------------
    def inside(self, x: int, y: int) -> bool:
        return 0 <= x < self.w and 0 <= y < self.h

    def raw(self, x: int, y: int) -> int:
        return self.tiles[y * self.w + x]

    def collision(self, x: int, y: int) -> int:
        return (self.raw(x, y) >> 10) & 3

    def elevation(self, x: int, y: int) -> int:
        return self.raw(x, y) >> 12

    def behavior(self, x: int, y: int) -> int:
        return self.beh[y * self.w + x]

    def metatile(self, x: int, y: int) -> int:
        return self.raw(x, y) & 0x3FF

    # -- movement --------------------------------------------------------------
    def step(self, p: Pos, d: str, caps: Caps, blocked: set | frozenset = frozenset()
             ) -> Pos | str | None:
        """Result of pressing direction d at p.

        Returns the new Pos, the string "edge" if the step leaves the map
        (a connection may take it), or None if the move is refused.
        `blocked` holds (x, y) of objects standing in the way.
        """
        dx, dy = DELTA[d]
        tx, ty = p.x + dx, p.y + dy
        if not self.inside(tx, ty):
            return "edge"
        cur_b = self.behavior(p.x, p.y) if self.inside(p.x, p.y) else 0
        tb = self.behavior(tx, ty)
        # Ledge jumps are checked before anything else and ignore collision.
        if not p.surfing and tb in JUMP[d]:
            lx, ly = tx + dx, ty + dy
            if not self.inside(lx, ly) or (lx, ly) in blocked:
                return None
            if self.collision(lx, ly):
                return None
            return Pos(lx, ly, p.elev, False)
        if self.collision(tx, ty) or tb in ENTER_BLOCKED[d] or cur_b in LEAVE_BLOCKED[d]:
            return None
        if (tx, ty) in blocked:
            return None
        if tb in FORCED and not caps.allow_forced:
            if not (tb == MB["MB_WATERFALL"] and caps.waterfall):
                return None
        te = self.elevation(tx, ty)
        mismatch = (p.elev != ELEV_TRANSITION and te not in (ELEV_TRANSITION, ELEV_MULTI)
                    and te != p.elev)
        if mismatch:
            if p.surfing and te == ELEV_DEFAULT:
                return Pos(tx, ty, te, False)                    # hop off Surf
            if not p.surfing and caps.surf and surfable(tb):
                return Pos(tx, ty, te, True)                     # start surfing
            return None
        cur_e = self.elevation(p.x, p.y) if self.inside(p.x, p.y) else te
        new_elev = p.elev if (te == ELEV_MULTI or cur_e == ELEV_MULTI) else te
        return Pos(tx, ty, new_elev, p.surfing)

    def render(self, marks: dict[tuple[int, int], str] | None = None,
               window: tuple[int, int, int, int] | None = None) -> str:
        marks = marks or {}
        x0, y0, x1, y1 = window or (0, 0, self.w, self.h)
        lines = []
        for y in range(max(0, y0), min(self.h, y1)):
            row = []
            for x in range(max(0, x0), min(self.w, x1)):
                if (x, y) in marks:
                    row.append(marks[(x, y)])
                    continue
                b = self.behavior(x, y)
                if b in STEP_WARPS:
                    row.append("D")
                elif any(b in s for s in JUMP.values()):
                    row.append("v")
                elif self.collision(x, y):
                    row.append("#")
                elif surfable(b):
                    row.append("~")
                elif has_encounters(b):
                    row.append('"')
                elif b in FORCED:
                    row.append("!")
                else:
                    row.append(".")
            lines.append(f"{y:3d} " + "".join(row))
        return "\n".join(lines)


_STATIC: dict[str, MapGrid] = {}


def _static_grid(emu, map_id: str) -> MapGrid:
    if map_id in _STATIC:
        return _STATIC[map_id]
    info = maps()[map_id]
    g, n = info["group"], info["num"]
    group_ptr = emu.u32(S["gMapGroups"] + g * 4)
    header = emu.u32(group_ptr + n * 4)
    layout = emu.u32(header)
    w, h, _border, data = struct.unpack("<iiII", emu.read(layout, 16))
    tiles = list(struct.unpack(f"<{w * h}H", emu.read(data, w * h * 2)))
    prim, sec = MapGrid._attrs(emu.read, layout)
    beh = []
    for t in tiles:
        mid = t & 0x3FF
        beh.append(prim[mid] if mid < NUM_PRIMARY_METATILES
                   else sec[mid - NUM_PRIMARY_METATILES] if mid - NUM_PRIMARY_METATILES < len(sec) else 0)
    grid = MapGrid(w, h, tiles, beh, map_id)
    _STATIC[map_id] = grid
    return grid
