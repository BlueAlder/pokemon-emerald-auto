"""A persistent model of the world, accumulated as the agent explores.

Without this the agent has no memory beyond the room it is standing in. Every
decision is made from a menu of the current map's doors, so it can never aim at
anywhere it cannot currently see, and it paces between buildings.

The world model fixes the two things that actually cause the wandering:

* **Memory.** Every map that has been entered is recorded -- its name, size,
  exits and where each one leads, and its objects. That persists to disk, so
  knowledge accumulates across runs instead of resetting.

* **Frontiers.** An exit whose destination has never been visited is a frontier:
  a concrete, named place the agent knows exists but has not been. These are the
  only things worth walking toward, and they are computed over the WHOLE known
  world, not just the current map.

Code then routes across maps to whichever frontier the model picks. The model
never has to do geometry; it decides where the agent should be going.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field, asdict
from pathlib import Path


def map_key(group: int, num: int) -> str:
    return f"{group}.{num}"


def parse_key(key: str) -> tuple[int, int]:
    group, num = key.split(".")
    return int(group), int(num)


@dataclass
class MapRecord:
    key: str
    name: str = ""
    map_type: str = ""
    width: int = 0
    height: int = 0
    visits: int = 0
    # tile -> destination map key
    warps: dict[str, str] = field(default_factory=dict)
    # direction -> destination map key
    connections: dict[str, str] = field(default_factory=dict)
    # "x,y" -> True once talked to
    objects: dict[str, bool] = field(default_factory=dict)

    @property
    def described(self) -> str:
        return f"{self.name} ({self.map_type}, map {self.key})"

    def exits(self) -> dict[str, str]:
        """Every way out: warp tiles and border connections alike."""
        out = dict(self.warps)
        for direction, dest in self.connections.items():
            out[f"edge:{direction}"] = dest
        return out


@dataclass
class Frontier:
    kind: str                 # "exit" | "edge" | "object"
    map_key: str              # where the frontier physically is
    tile: tuple[int, int] | None
    direction: str | None     # for edge frontiers
    dest_key: str | None
    label: str
    hops: int                 # maps to cross to reach the frontier's map

    @property
    def is_here(self) -> bool:
        return self.hops == 0


class World:
    """Everything the agent has learned about the map graph."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self.maps: dict[str, MapRecord] = {}
        # Exits that look walkable but refuse to be walked. Emerald gates
        # progress with invisible script blockers -- Route 101 is sealed until
        # the intro is finished -- and the collision grid says nothing about
        # them. Without remembering these the agent retries the same sealed
        # door forever.
        self.blocked: set[str] = set()
        self.load()

    # -- persistence -------------------------------------------------------

    def load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        for key, data in raw.get("maps", {}).items():
            self.maps[key] = MapRecord(**data)
        self.blocked = set(raw.get("blocked", []))

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"maps": {k: asdict(v) for k, v in self.maps.items()},
                   "blocked": sorted(self.blocked)}
        self.path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")

    # -- recording ---------------------------------------------------------

    def observe(self, group: int, num: int, view, names) -> MapRecord:
        """Fold a freshly read MapView into the model."""
        key = map_key(group, num)
        record = self.maps.get(key)
        if record is None:
            record = MapRecord(key=key)
            self.maps[key] = record

        record.name, record.map_type = names.lookup(group, num)
        record.width, record.height = view.width, view.height
        for warp in view.warps:
            record.warps[f"{warp.x},{warp.y}"] = map_key(warp.dest_group,
                                                         warp.dest_num)
        for direction, dest in view.connections.items():
            record.connections[direction] = map_key(*dest)
        for obj in view.objects:
            record.objects.setdefault(f"{obj.x},{obj.y}", False)
        return record

    def enter(self, group: int, num: int) -> None:
        record = self.maps.get(map_key(group, num))
        if record:
            record.visits += 1

    def mark_talked(self, group: int, num: int, x: int, y: int) -> None:
        record = self.maps.get(map_key(group, num))
        if record:
            record.objects[f"{x},{y}"] = True

    @staticmethod
    def block_id(map_key_: str, tile=None, direction: str | None = None) -> str:
        where = f"{tile[0]},{tile[1]}" if tile else f"edge:{direction}"
        return f"{map_key_}@{where}"

    def mark_blocked(self, map_key_: str, tile=None,
                     direction: str | None = None) -> None:
        self.blocked.add(self.block_id(map_key_, tile, direction))

    def is_blocked(self, map_key_: str, tile=None,
                   direction: str | None = None) -> bool:
        return self.block_id(map_key_, tile, direction) in self.blocked

    def visited(self, key: str) -> bool:
        record = self.maps.get(key)
        return bool(record and record.visits)

    # -- graph -------------------------------------------------------------

    def route(self, start: str, goal: str) -> list[str] | None:
        """Sequence of map keys from start to goal, inclusive of both.

        Only traverses exits whose destination has actually been visited --
        an unvisited destination is a frontier, not a road we know.
        """
        if start == goal:
            return [start]
        seen = {start: None}
        queue = deque([start])
        while queue:
            current = queue.popleft()
            record = self.maps.get(current)
            if not record:
                continue
            for dest in set(record.exits().values()):
                if dest in seen:
                    continue
                seen[dest] = current
                if dest == goal:
                    chain = [dest]
                    while seen[chain[-1]] is not None:
                        chain.append(seen[chain[-1]])
                    return list(reversed(chain))
                if self.visited(dest):
                    queue.append(dest)
        return None

    def hops_from(self, start: str) -> dict[str, int]:
        """Map key -> number of maps to cross, over visited ground."""
        distances = {start: 0}
        queue = deque([start])
        while queue:
            current = queue.popleft()
            record = self.maps.get(current)
            if not record:
                continue
            for dest in set(record.exits().values()):
                if dest in distances:
                    continue
                distances[dest] = distances[current] + 1
                if self.visited(dest):
                    queue.append(dest)
        return distances

    # -- frontiers ---------------------------------------------------------

    def frontiers(self, here: str) -> list[Frontier]:
        """Everywhere worth going, ranked by how far away it is.

        Two kinds matter: an exit into a map never entered, and an object
        never spoken to. Both are computed across the whole known world, so
        the agent can aim at somewhere it is not currently standing.
        """
        distances = self.hops_from(here)
        found: list[Frontier] = []

        for key, record in self.maps.items():
            hops = distances.get(key)
            if hops is None:
                continue        # not reachable over ground we know

            for tile, dest in record.warps.items():
                if self.visited(dest):
                    continue
                x, y = (int(v) for v in tile.split(","))
                if self.is_blocked(key, (x, y)):
                    continue
                dest_record = self.maps.get(dest)
                dest_name = dest_record.described if dest_record else "somewhere new"
                found.append(Frontier(
                    kind="exit", map_key=key, tile=(x, y), direction=None,
                    dest_key=dest, hops=hops,
                    label=f"unexplored exit at ({x},{y}) in {record.described}"
                          f" leading to {dest_name}"))

            for direction, dest in record.connections.items():
                if self.visited(dest) or self.is_blocked(key, direction=direction):
                    continue
                dest_record = self.maps.get(dest)
                dest_name = dest_record.described if dest_record else "an unvisited area"
                found.append(Frontier(
                    kind="edge", map_key=key, tile=None, direction=direction,
                    dest_key=dest, hops=hops,
                    label=f"the unexplored {direction} edge of {record.described}"
                          f" leading to {dest_name}"))

            for tile, talked in record.objects.items():
                if talked:
                    continue
                x, y = (int(v) for v in tile.split(","))
                found.append(Frontier(
                    kind="object", map_key=key, tile=(x, y), direction=None,
                    dest_key=None, hops=hops,
                    label=f"a person or object at ({x},{y}) in "
                          f"{record.described} never spoken to"))

        found.sort(key=lambda f: (f.hops, f.kind != "exit", f.kind != "edge"))
        return found

    # -- reporting ---------------------------------------------------------

    def summary(self, here: str, limit: int = 14) -> list[str]:
        """Compact human/model-readable picture of what is known."""
        distances = self.hops_from(here)
        lines = []
        for key, record in sorted(self.maps.items(),
                                  key=lambda kv: distances.get(kv[0], 99)):
            if not self.visited(key):
                continue
            hops = distances.get(key)
            where = "here" if key == here else f"{hops} map(s) away"
            unexplored = sum(1 for d in record.exits().values()
                             if not self.visited(d))
            untalked = sum(1 for done in record.objects.values() if not done)
            lines.append(
                f"{record.described}: {where}, {len(record.exits())} exit(s), "
                f"{unexplored} of them unexplored, {untalked} object(s) not yet "
                f"spoken to")
            if len(lines) >= limit:
                break
        return lines
