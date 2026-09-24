"""Fast checks of the models and solvers against the real ROM data.

Needs roms/emerald.gba (the pokeemerald-identical US ROM); skipped without it.
Run:  .venv/bin/python -m pytest tests/ -q   (or just python tests/test_offline.py)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ROM = ROOT / "roms" / "emerald.gba"

try:
    import pytest
    skip = pytest.mark.skipif(not ROM.exists(), reason="ROM not present")
except ImportError:                                   # plain `python tests/...`
    pytest = None

    def skip(f):
        return f

_EMU = None


def emu():
    global _EMU
    if _EMU is None:
        from pokeauto.emu import HeadlessEmu
        _EMU = HeadlessEmu(str(ROM))
    return _EMU


@skip
def test_ice_gym_rooms_have_hamiltonian_paths():
    from pokeauto.mapgrid import MapGrid
    from pokeauto.puzzles import ice_path
    from pokeauto.symbols import const
    g = MapGrid.from_rom(emu(), "MAP_SOOTOPOLIS_CITY_GYM_1F")
    thin, slide = const("MB_THIN_ICE"), const("MB_SLIDE_SOUTH")
    ice = {(x, y) for y in range(g.h) for x in range(g.w) if g.behavior(x, y) == thin}
    for (top, bottom), entry in (((17, 19), (8, 19)), ((12, 14), (8, 14)), ((6, 9), (8, 9))):
        room = {t for t in ice if top <= t[1] <= bottom}
        ends = {t for t in room if g.behavior(t[0], t[1] - 1) == slide}
        path = ice_path(None, room, ends, first={entry})
        assert path and len(path) == len(room) and set(path) == room
        assert all(abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1 for a, b in zip(path, path[1:]))


@skip
def test_sky_pillar_route_uses_the_cracked_floor_drop():
    from pokeauto.agent import Agent
    from pokeauto.nav import State, at
    a = Agent(emu())
    start = State("MAP_SKY_PILLAR_1F", 6, 12, 3)
    plan = a.ctl.planner.plan(start, at("MAP_SKY_PILLAR_TOP"), a.ctl.nav_caps())
    assert plan is not None
    maps_seen = [st.expect.map for st in plan]
    # 4F -> fall to 3F -> back up to 4F's other side
    assert "MAP_SKY_PILLAR_3F" in maps_seen[maps_seen.index("MAP_SKY_PILLAR_4F"):]


@skip
def test_wild_tables_and_mart_inventories():
    from pokeauto.data import GameData
    from pokeauto.symbols import maps
    d = GameData(emu())
    wild = d.wild_land()
    assert "MAP_ROUTE101" in wild and all(len(v) == 12 for v in wild.values())
    lo, hi, _ = wild["MAP_ROUTE101"][0]
    assert 2 <= lo <= hi <= 5
    league = maps()["MAP_EVER_GRANDE_CITY_POKEMON_LEAGUE_1F"]["marts"][0]["items"]
    assert "ITEM_FULL_RESTORE" in league


def test_rotating_tiles_press_cycles_a_turntable():
    from pokeauto.puzzles import RotatingTiles
    rt = RotatingTiles.__new__(RotatingTiles)
    # green 2x2 turntable: (0,0)v (0,1)> (1,1)^ (1,0)<
    rt.arrows = {(0, 0): (2, (0, 1)), (0, 1): (2, (1, 0)), (1, 1): (2, (0, -1)),
                 (1, 0): (2, (-1, 0))}
    objs = ((0, 1), (1, 1), (1, 0))
    once = rt.press(objs, 2)
    assert sorted(once) == [(0, 0), (1, 0), (1, 1)]          # the gap moved round
    assert rt.press(objs, 0) == objs                          # other colours: no-op
    four = objs
    for _ in range(4):
        four = rt.press(four, 2)
    assert sorted(four) == sorted(objs)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
