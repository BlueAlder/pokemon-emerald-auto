"""The loaded map: collision grid, warps, and objects.

This is what turns blind tile-by-tile wandering into navigation. Gen 3 packs
collision straight into each map-grid entry, so the agent can know what is
walkable without bumping into it:

    bits 0-9   metatile id
    bits 10-11 collision   (non-zero == impassable)
    bits 12-15 elevation

Both structures were located empirically and verified against live play:
gBackupMapLayout's grid agreed with every tile the agent had walked on, and
gMapHeader's single warp for the upstairs bedroom, (7,1) -> map 1.0, is
exactly the staircase the agent fell through.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# Every loaded map is padded with a 7-tile border, so map (0,0) sits at grid
# (7,7). Real map size is the grid size minus twice that.
MAP_OFFSET = 7
COLLISION_SHIFT, COLLISION_MASK = 10, 0x3
METATILE_MASK = 0x03FF
WARP_SIZE = 8
OBJECT_SIZE = 24
MAX_GRID_ENTRIES = 0x2800     # MAX_MAP_DATA_SIZE in the decomp

# Graphics ids 0xF0-0xFF are OBJ_EVENT_GFX_VAR_0..F: dormant slots a script
# fills in later, parked in a column at x=0 until then. They are real table
# entries but not real objects, so they must not be offered as destinations.
FIRST_VAR_GRAPHICS_ID = 0xF0

DIRECTION_DELTA = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}

# gMapHeader->connections says which map borders actually lead somewhere. It is
# NULL for indoor maps, so their edges are painted floor that goes nowhere --
# offering them as destinations produces a step that silently fails forever.
CONNECTION_SIZE = 12
CONNECTION_DIRECTIONS = {1: "south", 2: "north", 3: "west", 4: "east"}

# MapHeader field offsets we care about.
HEADER_REGION_SECTION = 0x14
HEADER_MAP_TYPE = 0x17
REGION_ENTRY_SIZE = 8

MAP_TYPES = {
    0: "nowhere", 1: "town", 2: "city", 3: "route", 4: "underground",
    5: "underwater", 6: "sea route", 7: "unknown", 8: "indoors",
    9: "secret base",
}


@dataclass(frozen=True)
class Warp:
    x: int
    y: int
    dest_group: int
    dest_num: int

    @property
    def label(self) -> str:
        return f"exit at ({self.x},{self.y}) leading to map {self.dest_group}.{self.dest_num}"


@dataclass(frozen=True)
class MapObject:
    x: int
    y: int
    graphics_id: int
    local_id: int
    trainer_type: int

    @property
    def is_trainer(self) -> bool:
        return self.trainer_type != 0

    @property
    def label(self) -> str:
        kind = "trainer" if self.is_trainer else "person or object"
        return f"{kind} at ({self.x},{self.y})"


@dataclass
class MapView:
    width: int
    height: int
    collision: list[int]              # row-major, real map coordinates
    warps: list[Warp] = field(default_factory=list)
    objects: list[MapObject] = field(default_factory=list)
    connections: dict[str, tuple[int, int]] = field(default_factory=dict)

    # -- queries -----------------------------------------------------------

    def in_bounds(self, x: int, y: int) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height

    def passable(self, x: int, y: int) -> bool:
        if not self.in_bounds(x, y):
            return False
        return self.collision[y * self.width + x] == 0

    def neighbours(self, x: int, y: int):
        for name, (dx, dy) in DIRECTION_DELTA.items():
            nx, ny = x + dx, y + dy
            if self.passable(nx, ny):
                yield name, nx, ny

    # -- pathfinding -------------------------------------------------------

    def path_to(self, start: tuple[int, int], goal: tuple[int, int],
                avoid: set[tuple[int, int]] | None = None) -> list[str] | None:
        """Shortest walk from start to goal as direction names, or None.

        Breadth-first over the collision grid: the map is at most a few
        thousand tiles, so this is instant and exact. Pathfinding is code's
        job; the model only chooses where to go.

        `avoid` is for tiles that are walkable but must not be crossed --
        chiefly other warps. Walking over a door teleports you, so a route
        that clips one never reaches its actual goal.
        """
        if start == goal:
            return []
        avoid = (avoid or set()) - {goal}
        # The goal itself may be impassable: a door is usually drawn as part
        # of the wall, so allow the final step onto it regardless.
        frontier = [start]
        came: dict[tuple[int, int], tuple[tuple[int, int], str]] = {start: (start, "")}
        while frontier:
            nxt = []
            for cur in frontier:
                cx, cy = cur
                for name, (dx, dy) in DIRECTION_DELTA.items():
                    step = (cx + dx, cy + dy)
                    if step in came or not self.in_bounds(*step):
                        continue
                    if step != goal and (step in avoid or not self.passable(*step)):
                        continue
                    came[step] = (cur, name)
                    if step == goal:
                        return self._unwind(came, start, goal)
                    nxt.append(step)
            frontier = nxt
        return None

    def warp_tiles(self) -> set[tuple[int, int]]:
        return {(w.x, w.y) for w in self.warps}

    def door_direction(self, tile: tuple[int, int], prefer: str | None = None,
                       exclude: set[str] | None = None) -> str | None:
        """Which way to press when already standing on a warp.

        Stairs and doors usually need one more step *into* the wall they sit
        against before they fire, so aim at an impassable neighbour.
        """
        x, y = tile
        exclude = exclude or set()
        options = [name for name, (dx, dy) in DIRECTION_DELTA.items()
                   if not self.passable(x + dx, y + dy) and name not in exclude]
        if not options:
            return None
        if prefer in options:
            return prefer
        return options[0]

    @staticmethod
    def _unwind(came, start, goal) -> list[str]:
        steps: list[str] = []
        node = goal
        while node != start:
            node, name = came[node]
            steps.append(name)
        return list(reversed(steps))

    def reachable(self, start: tuple[int, int]) -> set[tuple[int, int]]:
        seen = {start}
        frontier = [start]
        while frontier:
            nxt = []
            for cx, cy in frontier:
                for _, nx, ny in self.neighbours(cx, cy):
                    if (nx, ny) not in seen:
                        seen.add((nx, ny))
                        nxt.append((nx, ny))
            frontier = nxt
        return seen

    def edge_exits(self, start: tuple[int, int]) -> dict[str, tuple[int, int]]:
        """Border tiles that genuinely lead to another map.

        Outdoor maps connect by walking off the edge rather than through a
        warp, so the border is a destination in its own right -- but only in
        directions gMapHeader->connections actually lists. An indoor map has
        no connections at all, and its edge tiles are dead ends however
        walkable they look.
        """
        reach = self.reachable(start)
        sx, sy = start
        out: dict[str, tuple[int, int]] = {}
        borders = {
            "north": [(x, 0) for x in range(self.width)],
            "south": [(x, self.height - 1) for x in range(self.width)],
            "west": [(0, y) for y in range(self.height)],
            "east": [(self.width - 1, y) for y in range(self.height)],
        }
        for name, tiles in borders.items():
            if name not in self.connections:
                continue
            candidates = [t for t in tiles if t in reach]
            if candidates:
                out[name] = min(candidates,
                                key=lambda t: abs(t[0] - sx) + abs(t[1] - sy))
        return out

    # -- rendering ---------------------------------------------------------

    def render(self, player: tuple[int, int], marks: dict[tuple[int, int], str],
               radius: int = 9) -> str:
        """ASCII view around the player, clipped to a readable window.

        Outdoor maps run to thousands of tiles, far too many to hand a model,
        so only the neighbourhood is drawn and the interesting destinations
        are listed separately with their walking distances.
        """
        px, py = player
        x0, x1 = max(0, px - radius), min(self.width, px + radius + 1)
        y0, y1 = max(0, py - radius), min(self.height, py + radius + 1)

        lines = ["    " + "".join(str(x % 10) for x in range(x0, x1))]
        for y in range(y0, y1):
            row = ""
            for x in range(x0, x1):
                if (x, y) == player:
                    row += "P"
                elif (x, y) in marks:
                    row += marks[(x, y)]
                else:
                    row += "." if self.passable(x, y) else "#"
            lines.append(f"{y:3d} {row}")
        return "\n".join(lines)


class MapNames:
    """Resolves (map group, map number) to a human place name.

    Every map header lives in ROM behind gMapGroups, and carries a region-map
    section id that indexes a table of name strings. Without this the model
    only ever sees "map 0.9", which is meaningless next to an objective that
    talks about Littleroot Town and Route 101.
    """

    def __init__(self, bridge, addresses):
        self.bridge = bridge
        self.addr = addresses
        self._cache: dict[tuple[int, int], tuple[str, str]] = {}

    def _header(self, group: int, num: int) -> int | None:
        try:
            group_ptr = struct.unpack("<I", self.bridge.read(
                self.addr.map_groups + group * 4, 4))[0]
            if not 0x08000000 <= group_ptr < 0x09000000:
                return None
            header = struct.unpack("<I", self.bridge.read(
                group_ptr + num * 4, 4))[0]
            return header if 0x08000000 <= header < 0x09000000 else None
        except Exception:
            return None

    def lookup(self, group: int, num: int) -> tuple[str, str]:
        """(place name, map type). Falls back to the raw id when unknown."""
        key = (group, num)
        if key in self._cache:
            return self._cache[key]

        fallback = (f"map {group}.{num}", "unknown")
        header = self._header(group, num)
        if header is None:
            self._cache[key] = fallback
            return fallback
        try:
            raw = self.bridge.read(header, 0x18)
            section = raw[HEADER_REGION_SECTION]
            map_type = MAP_TYPES.get(raw[HEADER_MAP_TYPE], "unknown")
            name_ptr = struct.unpack("<I", self.bridge.read(
                self.addr.region_map_entries + section * REGION_ENTRY_SIZE + 4, 4))[0]
            if not 0x08000000 <= name_ptr < 0x09000000:
                self._cache[key] = fallback
                return fallback
            from .memory import decode_text
            name = decode_text(self.bridge.read(name_ptr, 20)) or fallback[0]
            self._cache[key] = (name, map_type)
            return self._cache[key]
        except Exception:
            self._cache[key] = fallback
            return fallback

    def describe(self, group: int, num: int) -> str:
        name, map_type = self.lookup(group, num)
        # Many maps share a name (every Littleroot building is LITTLEROOT
        # TOWN), so keep the id alongside it to stay unambiguous.
        return f"{name} ({map_type}, map {group}.{num})"


class MapReader:
    """Reads MapView snapshots over the bridge."""

    def __init__(self, bridge, addresses):
        self.bridge = bridge
        self.addr = addresses

    def read(self) -> MapView | None:
        a = self.addr
        grid_w, grid_h, data_ptr = struct.unpack(
            "<iiI", self.bridge.read(a.backup_map_layout, 12))

        width, height = grid_w - 2 * MAP_OFFSET, grid_h - 2 * MAP_OFFSET
        if not (0 < width <= 256 and 0 < height <= 256):
            return None
        if grid_w * grid_h > MAX_GRID_ENTRIES or not data_ptr:
            return None

        raw = self.bridge.read(data_ptr, grid_w * grid_h * 2)
        if len(raw) < grid_w * grid_h * 2:
            return None
        grid = struct.unpack(f"<{grid_w * grid_h}H", raw)

        collision = [0] * (width * height)
        for y in range(height):
            base = (y + MAP_OFFSET) * grid_w + MAP_OFFSET
            for x in range(width):
                entry = grid[base + x]
                collision[y * width + x] = (entry >> COLLISION_SHIFT) & COLLISION_MASK

        view = MapView(width=width, height=height, collision=collision)
        self._read_events(view)
        self._read_connections(view)
        return view

    def _read_connections(self, view: MapView) -> None:
        try:
            header = self.bridge.read(self.addr.map_header, 16)
            conn_ptr = struct.unpack_from("<I", header, 12)[0]
            if not 0x08000000 <= conn_ptr < 0x09000000:
                return      # NULL: an indoor map with no border connections
            count, list_ptr = struct.unpack("<iI", self.bridge.read(conn_ptr, 8))
            if not (0 < count <= 8 and 0x08000000 <= list_ptr < 0x09000000):
                return
            raw = self.bridge.read(list_ptr, count * CONNECTION_SIZE)
            for i in range(count):
                off = i * CONNECTION_SIZE
                name = CONNECTION_DIRECTIONS.get(raw[off])
                if name:
                    view.connections[name] = (raw[off + 9], raw[off + 8])
        except Exception:
            return

    def _read_events(self, view: MapView) -> None:
        """Warps and object events, from gMapHeader -> MapEvents."""
        try:
            header = self.bridge.read(self.addr.map_header, 16)
            events_ptr = struct.unpack_from("<I", header, 4)[0]
            if not 0x08000000 <= events_ptr < 0x09000000:
                return
            head = self.bridge.read(events_ptr, 16)
            n_objects, n_warps = head[0], head[1]
            objects_ptr, warps_ptr = struct.unpack_from("<II", head, 4)

            if n_warps and 0x08000000 <= warps_ptr < 0x09000000:
                raw = self.bridge.read(warps_ptr, min(n_warps, 32) * WARP_SIZE)
                for i in range(min(n_warps, 32)):
                    x, y = struct.unpack_from("<hh", raw, i * WARP_SIZE)
                    _elev, _wid, num, group = raw[i * WARP_SIZE + 4:i * WARP_SIZE + 8]
                    if view.in_bounds(x, y):
                        view.warps.append(Warp(x, y, group, num))

            if n_objects and 0x08000000 <= objects_ptr < 0x09000000:
                count = min(n_objects, 64)
                raw = self.bridge.read(objects_ptr, count * OBJECT_SIZE)
                for i in range(count):
                    off = i * OBJECT_SIZE
                    local_id, graphics_id = raw[off], raw[off + 1]
                    x, y = struct.unpack_from("<hh", raw, off + 4)
                    trainer_type = struct.unpack_from("<H", raw, off + 12)[0]
                    if graphics_id >= FIRST_VAR_GRAPHICS_ID:
                        continue     # dormant script placeholder, not an object
                    if view.in_bounds(x, y):
                        view.objects.append(
                            MapObject(x, y, graphics_id, local_id, trainer_type))
        except Exception:
            # Events are a bonus; a map without them still navigates fine.
            return
