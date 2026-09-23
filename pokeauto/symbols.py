"""Addresses from the pokeemerald decompilation's symbol map.

The US Emerald ROM (SHA1 f3ae0881...) is byte-identical to what pokeemerald
builds, so every RAM variable and ROM function in data/pokeemerald.sym is at
exactly the listed address. That replaces the guessed addresses and heuristics
earlier versions relied on ("is text changing?") with the game's own state.

Function symbols also give us a free debugger: gMain.callback2 and each active
task's function pointer can be named, so "what is on screen" is a lookup.
"""
from __future__ import annotations

import bisect
import json
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"


class Symbols:
    def __init__(self, path: Path = DATA / "pokeemerald.sym"):
        self.by_name: dict[str, list[tuple[int, int]]] = {}
        funcs: list[tuple[int, str]] = []
        for line in path.read_text().splitlines():
            parts = line.split()
            if len(parts) != 4:
                continue
            addr, _scope, size, name = int(parts[0], 16), parts[1], int(parts[2], 16), parts[3]
            self.by_name.setdefault(name, []).append((addr, size))
            if 0x08000000 <= addr < 0x0A000000 and size:
                funcs.append((addr, name))
        funcs.sort()
        self._faddr = [a for a, _ in funcs]
        self._fname = [n for _, n in funcs]

    def __getitem__(self, name: str) -> int:
        entries = self.by_name[name]
        if len(entries) != 1:
            raise KeyError(f"{name} is ambiguous ({len(entries)} definitions); use all()")
        return entries[0][0]

    def all(self, name: str) -> list[int]:
        return [a for a, _ in self.by_name.get(name, [])]

    def size(self, name: str) -> int:
        return self.by_name[name][0][1]

    def name_at(self, addr: int) -> str:
        """Name of the ROM symbol containing addr (thumb bit ignored)."""
        addr &= ~1
        i = bisect.bisect_right(self._faddr, addr) - 1
        if i < 0:
            return f"0x{addr:08X}"
        base = self._faddr[i]
        return self._fname[i] if addr == base else f"{self._fname[i]}+0x{addr - base:X}"


@lru_cache(maxsize=1)
def symbols() -> Symbols:
    return Symbols()


@lru_cache(maxsize=1)
def constants() -> dict[str, int]:
    return json.loads((DATA / "constants.json").read_text())


def const(name: str) -> int:
    return constants()[name]


@lru_cache(maxsize=1)
def const_names() -> dict[str, dict[int, str]]:
    """Reverse lookup per prefix: const_names()['SPECIES_'][283] == 'SPECIES_MUDKIP'."""
    out: dict[str, dict[int, str]] = {}
    for name, value in constants().items():
        prefix = name.split("_", 1)[0] + "_"
        out.setdefault(prefix, {}).setdefault(value, name)
    return out


@lru_cache(maxsize=1)
def maps() -> dict[str, dict]:
    return json.loads((DATA / "maps.json").read_text())


@lru_cache(maxsize=1)
def map_by_id() -> dict[tuple[int, int], str]:
    return {(m["group"], m["num"]): mid for mid, m in maps().items()}


def map_id(group: int, num: int) -> str:
    return map_by_id().get((group, num), f"MAP_{group}_{num}")
