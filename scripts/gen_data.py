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
    "abilities.h": ("ABILITY_",),
    "battle_move_effects.h": ("EFFECT_",),
    "pokemon.h": ("TYPE_", "STAT_", "GROWTH_"),
    "hold_effects.h": ("HOLD_EFFECT_",),
    "battle.h": ("BATTLE_TYPE_", "B_OUTCOME_", "STATUS1_", "STATUS2_"),
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


NEGATE = {"lt": "ge", "le": "gt", "eq": "ne", "ne": "eq", "ge": "lt", "gt": "le"}


def walk_tile_patches(labels: dict, metatile_ids: dict, consts: dict) -> list:
    """setmetatile lines reached from a map's ON_LOAD / ON_TRANSITION /
    ON_RESUME scripts, each with the flag/var conditions on the way there.
    Anything we cannot evaluate when planning (VAR_RESULT, temp vars/flags,
    other kinds of branches) drops the patch rather than guess."""
    entries = []
    for body in labels.values():
        for line in body:
            m = re.match(r"map_script MAP_SCRIPT_ON_(?:LOAD|TRANSITION|RESUME), (\w+)", line)
            if m:
                entries.append(m.group(1))
    out, seen = [], set()

    def value(tok):
        try:
            return int(tok, 0)
        except ValueError:
            return consts.get(tok)

    def knowable(name):
        return not (name.startswith(("VAR_TEMP", "FLAG_TEMP", "VAR_0x8")) or name == "VAR_RESULT")

    def walk(label, cond, depth=0):
        key = (label, repr(cond))
        if depth > 8 or key in seen:
            return
        seen.add(key)
        for line in labels.get(label, []):
            m = re.match(r"setmetatile (\d+), (\d+), (\w+), (TRUE|FALSE)$", line)
            if m:
                if m.group(3) in metatile_ids:
                    out.append({"x": int(m.group(1)), "y": int(m.group(2)),
                                "metatile": metatile_ids[m.group(3)],
                                "impassable": m.group(4) == "TRUE", "when": list(cond)})
                continue
            m = re.match(r"call (\w+)$", line)
            if m:
                walk(m.group(1), cond, depth + 1)
                continue
            m = re.match(r"goto (\w+)$", line)
            if m:
                walk(m.group(1), cond, depth + 1)
                return
            m = re.match(r"(call|goto)_if_(set|unset) (FLAG_\w+), (\w+)$", line)
            if m:
                if not knowable(m.group(3)):
                    if m.group(1) == "goto":
                        return                 # the rest runs under a condition we cannot know
                    continue
                walk(m.group(4), cond + [["flag", m.group(3), m.group(2) == "set"]], depth + 1)
                if m.group(1) == "goto":
                    cond = cond + [["flag", m.group(3), m.group(2) != "set"]]
                continue
            m = re.match(r"(call|goto)_if_(lt|le|eq|ne|ge|gt) (VAR_\w+), (\w+), (\w+)$", line)
            if m:
                v = value(m.group(4))
                if v is None or not knowable(m.group(3)):
                    if m.group(1) == "goto":
                        return
                    continue
                walk(m.group(5), cond + [["var", m.group(3), m.group(2), v]], depth + 1)
                if m.group(1) == "goto":
                    cond = cond + [["var", m.group(3), NEGATE[m.group(2)], v]]
                continue
            if line.startswith(("goto_if", "call_if", "switch", "case")):
                if line.startswith(("goto_if", "switch", "case")):
                    return                     # unmodelled branch: stop, do not guess
                continue
            if line in ("end", "return"):
                return

    for e in entries:
        walk(e, [])
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

    layouts = json.loads((decomp / "data/layouts/layouts.json").read_text())["layouts"]
    layout_ids = {l["id"]: i + 1 for i, l in enumerate(layouts) if l}   # gMapLayouts[id - 1]
    metatile_ids = {name: int(v, 0) for name, v in re.findall(
        r"#define (METATILE_\w+)\s+(0x[0-9A-Fa-f]+|\d+)",
        (decomp / "include/constants/metatile_labels.h").read_text())}

    # Scripted warps: bg events (signs, doors) whose script ends in a warp,
    # e.g. the Petalburg Gym room doors ("Enter the SPEED room?" -> warpdoor).
    for mname, (g, n) in map_names.items():
        mid = json.loads((decomp / "data/maps" / mname / "map.json").read_text())["id"]
        path = decomp / "data/maps" / mname / "scripts.inc"
        if not path.exists():
            continue
        labels: dict[str, list[str]] = {}
        cur = None
        for line in path.read_text().splitlines():
            m = re.match(r"^(\w+)::?$", line)
            if m:
                cur = m.group(1)
                labels[cur] = []
            elif cur:
                labels[cur].append(line.strip())

        def resolve(label, env, depth=0):
            for line in labels.get(label, []):
                m = re.match(r"setvar (VAR_0x800[89]), (\d+)", line)
                if m:
                    env[m.group(1)] = int(m.group(2))
                m = re.match(r"warp(?:door|silent|mossdeepgym|teleport)?\s+(MAP_\w+),\s*(\w+),\s*(\w+)", line)
                if m:
                    x = env.get(m.group(2), m.group(2))
                    y = env.get(m.group(3), m.group(3))
                    try:
                        return {"dest": m.group(1), "x": int(x), "y": int(y)}
                    except ValueError:
                        return None
                m = re.match(r"goto(?:_if_\w+)?\s+(?:[^,]+,\s*)*(\w+)$", line)
                if m and depth < 4 and m.group(1) in labels and "YES" in line or \
                        (m and line.startswith("goto ") and depth < 4):
                    r = resolve(m.group(1), dict(env), depth + 1)
                    if r:
                        return r
                if line in ("end", "return"):
                    return None
            return None

        # Hole warps (setholewarp: cracked floors / holes drop you a floor) and
        # conditional layout swaps (Sky Pillar is "clean" until Rayquaza wakes).
        for body in labels.values():
            for line in body:
                m = re.match(r"(?:setholewarp|warphole) (MAP_\w+)", line)
                if m:
                    maps[mid]["hole_warp"] = m.group(1)
                m = re.match(r"setdivewarp (MAP_\w+), (\d+), (\d+)", line)
                if m:
                    maps[mid]["dive_warp"] = {"dest": m.group(1), "x": int(m.group(2)),
                                              "y": int(m.group(3))}
        overrides = []
        for body in labels.values():
            for line in body:
                m = re.match(r"call (\w+)$", line)
                if m and m.group(1) in labels:
                    for tl in labels[m.group(1)]:
                        lm = re.match(r"setmaplayoutindex (LAYOUT_\w+)", tl)
                        if lm and lm.group(1) in layout_ids:
                            overrides.append({"op": "always", "var": "", "value": 0,
                                              "layout": layout_ids[lm.group(1)]})
                    continue
                m = re.match(r"call_if_(lt|le|eq|ne|ge|gt) (VAR_\w+), (\w+), (\w+)$", line)
                if not m or m.group(4) not in labels:
                    continue
                for tl in labels[m.group(4)]:
                    lm = re.match(r"setmaplayoutindex (LAYOUT_\w+)", tl)
                    if lm and lm.group(1) in layout_ids:
                        overrides.append({"op": m.group(1), "var": m.group(2),
                                          "value": int(m.group(3), 0),
                                          "layout": layout_ids[lm.group(1)]})
        if overrides:
            maps[mid]["layout_overrides"] = overrides

        # Tiles a map script patches as the map loads (setmetatile), with the
        # story conditions they run under: the static layout never shows them
        # (the Sky Pillar door is shut in it for good, so no route inside).
        patches = walk_tile_patches(labels, metatile_ids, consts)
        if patches:
            maps[mid]["tile_patches"] = patches

        # Marts: which clerk script sells which items (pokemart <list label>).
        marts = []
        for label, body in labels.items():
            for line in body:
                m = re.match(r"pokemart (\w+)", line)
                if m and m.group(1) in labels:
                    items = [re.match(r"\.2byte (ITEM_\w+)", l).group(1)
                             for l in labels[m.group(1)] if re.match(r"\.2byte ITEM_\w+", l)]
                    marts.append({"script": label, "items": items})
        if marts:
            maps[mid]["marts"] = marts

        script_warps = []
        for b in maps[mid]["bgs"]:
            if b.get("type") == "sign" and b.get("script"):
                r = resolve(b["script"], {})
                if r:
                    script_warps.append({"sx": b["x"], "sy": b["y"], **r})
        maps[mid]["script_warps"] = script_warps

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
