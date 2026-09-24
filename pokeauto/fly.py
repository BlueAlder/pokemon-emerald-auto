"""Fly: where it can take us, and how to work the menus to get there.

The destination table comes from the ROM (region_map.c): for each town
map section, sMapHealLocations names the heal location we land on, and
sRegionMap_MapSectionLayout says which cells of the region map select it.
Only towns whose FLAG_VISITED_* is set are selectable (GetMapsecType).
"""
from __future__ import annotations

import logging
import struct
from functools import lru_cache

from .symbols import maps, map_id, symbols

log = logging.getLogger("pokeauto")
S = symbols()

LAYOUT_W, LAYOUT_H = 28, 15              # MAP_WIDTH, MAP_HEIGHT
CURSOR_X0, CURSOR_Y0 = 1, 2              # MAPCURSOR_X_MIN, MAPCURSOR_Y_MIN
MAPSECTYPE_CITY_CANFLY = 2
FLY_MAP_TYPES = ("MAP_TYPE_TOWN", "MAP_TYPE_CITY", "MAP_TYPE_ROUTE", "MAP_TYPE_OCEAN_ROUTE")
MENU_FIELD_MOVES, FIELD_MOVE_FLY = 19, 5
# Opening the menus, the take-off and the landing: about as long as walking
# this many tiles.
FLY_COST = 45.0


@lru_cache(maxsize=1)
def destinations(emu) -> tuple[dict, ...]:
    """Every town Fly can reach: landing map/tile, region-map cell, visit flag."""
    heal = emu.read(S["sHealLocations"], 0xB0)
    mhl = emu.read(S["sMapHealLocations"], 0x96)
    layout = emu.read(S["sRegionMap_MapSectionLayout"], LAYOUT_W * LAYOUT_H)
    out = []
    # Map sections 0-15 are the towns and cities, Littleroot first. Littleroot
    # lands inside the player's house by gender; nothing on the route needs it.
    for sec in range(1, 16):
        group, num, hl = mhl[sec * 3:sec * 3 + 3]
        if not hl:
            continue
        g, n, x, y = struct.unpack_from("<bbHH", heal, (hl - 1) * 8)
        town = map_id(group, num)
        land = map_id(g, n)
        cells = [(i % LAYOUT_W + CURSOR_X0, i // LAYOUT_W + CURSOR_Y0)
                 for i, v in enumerate(layout) if v == sec]
        if not cells or land not in maps():
            continue
        # Ever Grande's top cell is the Pokemon League once it is known:
        # the bottom one always lands in the city.
        cell = cells[-1]
        out.append({"town": town, "map": land, "x": x, "y": y, "cell": cell,
                    "flag": "FLAG_VISITED_" + town[len("MAP_"):]})
    return tuple(out)


def flyable_map(map_id_: str) -> bool:
    return maps()[map_id_].get("type") in FLY_MAP_TYPES


def fly_to(agent, dest_map: str) -> bool:
    """Fly to the town whose landing map is dest_map. True once we are there."""
    from .controller import Stuck
    game, ctl, emu = agent.game, agent.ctl, agent.emu
    dest = next(d for d in destinations(emu) if d["map"] == dest_map)
    flyer = next((m for m in game.party() if m.knows("MOVE_FLY") and not m.fainted), None)
    if flyer is None:
        return False
    agent.open_start_menu("POKEMON")
    slot_addr = S["gPartyMenu"] + 9

    def wait(pred, frames: int, what: str) -> None:
        for _ in range(frames // 4):
            if pred():
                return
            ctl.idle(4)
        raise Stuck(f"fly: never reached {what}")

    def task(name):
        return lambda: any(name in t for t in game.active_tasks()) and not game.fading()

    wait(task("Task_HandleChooseMonInput"), 240, "the party menu")
    for _ in range(10):
        cur = struct.unpack("b", emu.read(slot_addr, 1))[0]
        if cur == flyer.slot:
            break
        ctl.press("DOWN" if cur < flyer.slot or cur > 5 else "UP", release=5)
    ctl.press("A", release=16)
    wait(task("Task_HandleSelectionMenuInput"), 120, "the Pokemon's menu")
    internal = emu.u32(S["sPartyMenuInternal"])
    n = emu.u8(internal + 23)
    actions = list(emu.read(internal + 15, n))
    if MENU_FIELD_MOVES + FIELD_MOVE_FLY not in actions:
        for _ in range(3):
            ctl.press("B", release=12)
        agent.pump()
        return False
    ctl._menu_select(actions.index(MENU_FIELD_MOVES + FIELD_MOVE_FLY))

    # The region map: sFlyMap -> {callback, u16 state, u16 mapSecId, RegionMap}.
    def fly_map() -> int:
        p = emu.u32(S["sFlyMap"])
        return p if p and "FlyMap" in game.mode().callback2 and not game.fading() else 0
    wait(lambda: fly_map() and emu.u16(fly_map() + 4) == 0, 600, "the fly map")
    ctl.idle(20)
    tx, ty = dest["cell"]
    for _ in range(80):
        p = fly_map()
        cx, cy = emu.u16(p + 8 + 0x54), emu.u16(p + 8 + 0x56)
        if (cx, cy) == (tx, ty):
            break
        d = ("RIGHT" if cx < tx else "LEFT" if cx > tx else "DOWN" if cy < ty else "UP")
        ctl.press(d, hold=2, release=10)
    p = fly_map()
    if emu.u8(p + 8 + 2) != MAPSECTYPE_CITY_CANFLY:
        log.info("FLY cursor at %s is not a fly destination; cancelling", dest["town"])
        ctl.press("B", release=30)
        agent.pump()
        return False
    ctl.press("A", release=10)
    for _ in range(300):
        ctl.idle(4)
        if game.map_id() == dest["map"] and ctl.free():
            break
    agent.pump()
    ok = game.map_id() == dest["map"]
    log.info("FLEW to %s %s", dest["town"], "ok" if ok else f"-- ended on {game.map_id()}")
    return ok
