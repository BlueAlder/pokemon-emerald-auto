"""The complete static map graph, extracted from the ROM.

Every one of Emerald's 552 maps, its exits, and where each one leads — read
straight out of the cartridge rather than discovered by walking. This is what
makes directed travel possible: "go to Rustboro City" becomes a breadth-first
search, not a hope.

    LITTLEROOT TOWN -> ROUTE 101 -> OLDALE TOWN -> ROUTE 102
                    -> PETALBURG CITY -> ROUTE 104 -> RUSTBORO CITY

Regenerate with scripts/extract_world.py if you use a different ROM revision.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "emerald_world.json"


@dataclass(frozen=True)
class StaticMap:
    key: str
    name: str
    map_type: str
    width: int
    height: int
    warps: dict            # "x,y" -> destination key
    connections: dict      # direction -> destination key
    objects: tuple

    @property
    def described(self) -> str:
        return f"{self.name} ({self.map_type}, map {self.key})"

    @property
    def is_outdoor(self) -> bool:
        return self.map_type in ("town", "city", "route", "sea route")

    def exits(self) -> dict:
        out = dict(self.warps)
        for direction, dest in self.connections.items():
            out[f"edge:{direction}"] = dest
        return out


class WorldGraph:
    def __init__(self, path: Path = DATA):
        self.maps: dict[str, StaticMap] = {}
        if not path.exists():
            return
        raw = json.loads(path.read_text())["maps"]
        for key, record in raw.items():
            self.maps[key] = StaticMap(
                key=key, name=record["name"], map_type=record["type"],
                width=record["width"], height=record["height"],
                warps=record["warps"], connections=record["connections"],
                objects=tuple(record.get("objects", ())),
            )

    def __bool__(self) -> bool:
        return bool(self.maps)

    def get(self, key: str) -> StaticMap | None:
        return self.maps.get(key)

    def describe(self, key: str) -> str:
        record = self.maps.get(key)
        return record.described if record else f"map {key}"

    @lru_cache(maxsize=4096)
    def route(self, start: str, goal: str) -> tuple[str, ...] | None:
        """Shortest sequence of maps from start to goal, inclusive."""
        if start == goal:
            return (start,)
        if start not in self.maps or goal not in self.maps:
            return None
        came = {start: None}
        queue = deque([start])
        while queue:
            current = queue.popleft()
            record = self.maps.get(current)
            if record is None:
                continue
            for dest in set(record.exits().values()):
                if dest in came or dest not in self.maps:
                    continue
                came[dest] = current
                if dest == goal:
                    chain = [dest]
                    while came[chain[-1]] is not None:
                        chain.append(came[chain[-1]])
                    return tuple(reversed(chain))
                queue.append(dest)
        return None

    def exits_toward(self, here: str, next_key: str) -> list[str]:
        """The tiles or edges on `here` that lead to `next_key`."""
        record = self.maps.get(here)
        if record is None:
            return []
        return [where for where, dest in record.exits().items() if dest == next_key]

    def find(self, needle: str) -> list[str]:
        """Map keys whose place name contains `needle` (case-insensitive)."""
        needle = needle.upper()
        return [k for k, m in self.maps.items() if needle in m.name.upper()]
