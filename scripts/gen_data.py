#!/usr/bin/env python3
"""Compile the pokeemerald decompilation into the small JSON files we ship.

    python scripts/gen_data.py /path/to/pokeemerald

Outputs (committed, so the runtime never needs the decomp):

  data/constants.json   FLAG_*, VAR_*, ITEM_*, SPECIES_*, MOVE_*, TRAINER_*,
                        MB_* (metatile behaviours) -> numeric value
  data/maps.json        every map: group/num, layout, type, connections, object
                        events (with local ids, scripts, hide-flags, trainer
                        info), warps, coord triggers and bg events (signs,
                        hidden items)

The ROM this matches has SHA1 f3ae088181bf583e55daf962a92bb46f4f1d07b7
(Pokemon Emerald US), which is exactly what pokeemerald builds.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

HEADERS = {
    "flags.h": ("FLAG_", "TRAINER_FLAGS_", "SYSTEM_FLAGS", "DAILY_FLAGS"),
    "vars.h": ("VAR_", "VARS_", "SPECIAL_VARS_"),
    "items.h": ("ITEM_", "ITEMS_", "FIRST_", "LAST_", "NUM_"),
    "species.h": ("SPECIES_", "NUM_SPECIES"),
    "moves.h": ("MOVE_", "MOVES_COUNT"),
    "opponents.h": ("TRAINER_", "TRAINERS_COUNT", "MAX_TRAINERS_COUNT"),
    "event_objects.h": ("OBJ_EVENT_GFX_",),
    "trainer_types.h": ("TRAINER_TYPE_",),
    "event_object_movement.h": ("MOVEMENT_TYPE_",),
}

DEFINE = re.compile(r"^\s*#define\s+(\w+)\s+(.+?)\s*(//.*)?$")


def parse_defines(paths: list[Path]) -> dict[str, int]:
    raw: dict[str, str] = {}
    for p in paths:
        for line in p.read_text().splitlines():
            m = DEFINE.match(line)
            if m and "(" not in m.group(1):
                raw[m.group(1)] = m.group(2).split("//")[0].strip()

    out: dict[str, int] = {}

    def resolve(name: str, depth=0):
        if name in out:
            return out[name]
        if name not in raw or depth > 40:
            return None
        expr = raw[name]
        for tok in set(re.findall(r"\b[A-Za-z_]\w*", expr)):
            val = resolve(tok, depth + 1)
            if val is None:
                return None
            expr = re.sub(rf"\b{tok}\b", str(val), expr)
        try:
            v = int(eval(expr, {"__builtins__": {}}))  # arithmetic only
        except Exception:
            return None
        out[name] = v
        return v

    for name in raw:
        resolve(name)
    return out


def parse_enum(path: Path, prefix: str, known: dict[str, int] | None = None
               ) -> dict[str, int]:
    """Values of every `prefix`-named member of every enum in a header."""
    body = re.sub(r"/\*.*?\*/", "", re.sub(r"//.*", "", path.read_text()), flags=re.S)
    env = dict(known or {})
    out = {}
    for m in re.finditer(r"enum\s*\w*\s*\{(.*?)\}", body, re.S):
        i = 0
        for item in m.group(1).split(","):
            item = item.strip()
            if not item:
                continue
            if "=" in item:
                name, val = (s.strip() for s in item.split("=", 1))
                try:
                    i = int(eval(val, {"__builtins__": {}}, env))
                except Exception:
                    continue
            else:
                name = item
            env[name] = i
            if name.startswith(prefix):
                out[name] = i
            i += 1
    return out


def main() -> int:
    decomp = Path(sys.argv[1] if len(sys.argv) > 1 else "pokeemerald")
    cdir = decomp / "include" / "constants"

    consts = parse_defines([cdir / h for h in HEADERS] + [cdir / "global.h"])
    consts = {k: v for k, v in consts.items()
              if any(k.startswith(p) for pre in HEADERS.values() for p in pre)}
    consts.update(parse_enum(cdir / "metatile_behaviors.h", "MB_"))
    consts.update(parse_enum(cdir / "items.h", "ITEM_", consts))

    # Map ids come from the group order in map_groups.json.
    groups = json.loads((decomp / "data/maps/map_groups.json").read_text())
    map_names: dict[str, tuple[int, int]] = {}
    for g, gname in enumerate(groups["group_order"]):
        for n, mname in enumerate(groups[gname]):
            map_names[mname] = (g, n)

    maps = {}
    for mname, (g, n) in map_names.items():
        j = json.loads((decomp / "data/maps" / mname / "map.json").read_text())
        mid = j["id"]
        consts[mid] = g << 8 | n
        objects = []
        for i, o in enumerate(j.get("object_events") or []):
            if o.get("type") == "clone":
                continue
            objects.append({
                "local_id": i + 1,
                "name": o.get("local_id", ""),
                "gfx": o["graphics_id"],
                "x": o["x"], "y": o["y"], "elevation": o.get("elevation", 0),
                "movement": o.get("movement_type", ""),
                "trainer_type": o.get("trainer_type", ""),
                "sight": o.get("trainer_sight_or_berry_tree_id", "0"),
                "script": o.get("script", ""),
                "flag": o.get("flag", "0"),
            })
        maps[mid] = {
            "name": mname, "group": g, "num": n,
            "layout": j.get("layout"),
            "type": j.get("map_type"),
            "mapsec": j.get("region_map_section"),
            "requires_flash": j.get("requires_flash", False),
            "allow_cycling": j.get("allow_cycling", False),
            "connections": [
                {"map": c["map"], "offset": c["offset"], "direction": c["direction"]}
                for c in (j.get("connections") or [])],
            "objects": objects,
            "warps": [{"x": w["x"], "y": w["y"], "elevation": w.get("elevation", 0),
                       "dest": w["dest_map"], "dest_warp": int(w["dest_warp_id"], 0)
                       if str(w["dest_warp_id"]).lstrip("-").isdigit()
                       else w["dest_warp_id"]}
                      for w in (j.get("warp_events") or [])],
            "coords": [{k: c.get(k) for k in
                        ("type", "x", "y", "elevation", "var", "var_value", "script")}
                       for c in (j.get("coord_events") or [])],
            "bgs": [{k: b.get(k) for k in
                     ("type", "x", "y", "elevation", "script", "item", "flag",
                      "player_facing_dir")}
                    for b in (j.get("bg_events") or [])],
        }

    # Per-behaviour tile flags (surfable, has encounters, ...) from
    # sTileBitAttributes in metatile_behavior.c, keyed by the numeric MB_ value.
    src = (decomp / "src" / "metatile_behavior.c").read_text()
    flag_vals = {m.group(1): int(m.group(2), 0) for m in re.finditer(
        r"#define (TILE_FLAG_\w+)\s+\(1 << (\d+)\)", src)}
    tile_flags = {}
    for m in re.finditer(r"\[(MB_\w+)\]\s*=\s*([\w| ]+),", src):
        bits = sum(1 << flag_vals[f.strip()] for f in m.group(2).split("|")
                   if f.strip() in flag_vals)
        tile_flags[consts[m.group(1)]] = bits
    consts.update({f: 1 << v for f, v in flag_vals.items()})

    data = ROOT / "data"
    (data / "tile_flags.json").write_text(json.dumps(tile_flags, sort_keys=True))
    (data / "constants.json").write_text(json.dumps(consts, indent=0, sort_keys=True))
    (data / "maps.json").write_text(json.dumps(maps, indent=0, sort_keys=True))
    print(f"{len(consts)} constants, {len(maps)} maps")
    return 0


if __name__ == "__main__":
    sys.exit(main())
