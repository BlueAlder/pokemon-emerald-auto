#!/usr/bin/env python3
"""Rebuild data/emerald_world.json from the running ROM.

Only needed once per ROM revision. Walks gMapGroups, reads every map header,
and records each map's size, exits, connections and objects.
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pokeauto import memory as mem
from pokeauto.bridge import Bridge

ROOT = Path(__file__).resolve().parent.parent
BASE = 0x08000000
CONN_DIR = {1: "south", 2: "north", 3: "west", 4: "east", 5: "dive", 6: "emerge"}
MAP_TYPES = {0: "nowhere", 1: "town", 2: "city", 3: "route", 4: "underground",
             5: "underwater", 6: "sea route", 7: "unknown", 8: "indoors",
             9: "secret base"}


def main() -> int:
    addresses = mem.Addresses.load(ROOT / "config.json")
    bridge = Bridge().connect(retries=3, delay=1.0)
    size = bridge.info().rom_size
    print(f"reading {size / 2**20:.0f}MB of ROM...")
    rom = bytearray()
    for off in range(0, size, 0x8000):
        rom += bridge.read(BASE + off, min(0x8000, size - off))
    rom = bytes(rom)
    bridge.close()

    def u32(a): return struct.unpack_from("<I", rom, a - BASE)[0]
    def s32(a): return struct.unpack_from("<i", rom, a - BASE)[0]
    def ok(p): return 0x08000000 <= p < BASE + len(rom)

    groups = []
    for i in range(64):
        p = u32(addresses.map_groups + i * 4)
        if not ok(p):
            break
        groups.append(p)

    counts = []
    for i, p in enumerate(groups):
        if i + 1 < len(groups):
            counts.append(max(0, (groups[i + 1] - p) // 4))
        else:
            n = 0
            while n < 200 and ok(u32(p + n * 4)):
                n += 1
            counts.append(n)

    def section_name(sec: int) -> str:
        np = u32(addresses.region_map_entries + sec * 8 + 4)
        return mem.decode_text(rom[np - BASE:np - BASE + 20]) if ok(np) else f"SEC{sec}"

    maps = {}
    for g, (base, count) in enumerate(zip(groups, counts)):
        for n in range(count):
            hp = u32(base + n * 4)
            if not ok(hp):
                continue
            layout_p, events_p, _s, conn_p = struct.unpack_from("<4I", rom, hp - BASE)
            record = {
                "name": section_name(rom[hp - BASE + 0x14]),
                "type": MAP_TYPES.get(rom[hp - BASE + 0x17], "unknown"),
                "section": rom[hp - BASE + 0x14],
                "width": 0, "height": 0,
                "warps": {}, "connections": {}, "objects": [],
            }
            if ok(layout_p):
                record["width"], record["height"] = s32(layout_p), s32(layout_p + 4)
            if ok(events_p):
                n_obj, n_warp = rom[events_p - BASE], rom[events_p - BASE + 1]
                obj_p, warp_p = struct.unpack_from("<II", rom, events_p - BASE + 4)
                if n_warp and ok(warp_p):
                    for i in range(min(n_warp, 64)):
                        o = warp_p - BASE + i * 8
                        x, y = struct.unpack_from("<hh", rom, o)
                        record["warps"][f"{x},{y}"] = f"{rom[o + 7]}.{rom[o + 6]}"
                if n_obj and ok(obj_p):
                    for i in range(min(n_obj, 64)):
                        o = obj_p - BASE + i * 24
                        if rom[o + 1] >= 0xF0:
                            continue      # dormant script placeholder
                        x, y = struct.unpack_from("<hh", rom, o + 4)
                        record["objects"].append({
                            "x": x, "y": y, "gfx": rom[o + 1],
                            "trainer": struct.unpack_from("<H", rom, o + 12)[0]})
            if ok(conn_p):
                count_c, list_p = s32(conn_p), u32(conn_p + 4)
                if 0 < count_c <= 16 and ok(list_p):
                    for i in range(count_c):
                        o = list_p - BASE + i * 12
                        name = CONN_DIR.get(rom[o])
                        if name:
                            # MapConnection: direction@0, offset@4, group@8, num@9
                            record["connections"][name] = f"{rom[o + 8]}.{rom[o + 9]}"
            maps[f"{g}.{n}"] = record

    out = ROOT / "data" / "emerald_world.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"maps": maps}, indent=0, sort_keys=True))
    print(f"wrote {len(maps)} maps to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
