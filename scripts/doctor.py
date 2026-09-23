#!/usr/bin/env python3
"""Verify every assumption poke_auto makes, before you let it play.

Checks the bridge, the ROM, the calibrated data tables, each hardcoded RAM
address (against live memory, not against a comment), and the TypeSafe key.
Run this first, and run it again if the agent starts behaving oddly.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pokeauto import memory as mem
from pokeauto.env import find_dotenv, load_dotenv
from pokeauto.bridge import Bridge, BridgeError
from pokeauto.observer import Observer

ROOT = Path(__file__).resolve().parent.parent
OK, WARN, BAD = "  ok  ", " warn ", " FAIL "
_results: list[tuple[str, str]] = []


def report(level: str, message: str) -> None:
    _results.append((level, message))
    print(f"[{level}] {message}")


def main() -> int:
    print("poke_auto doctor\n" + "=" * 60)
    dotenv = find_dotenv(ROOT)
    loaded = load_dotenv(dotenv)
    if dotenv:
        report(OK, f"loaded {len(loaded)} variable(s) from {dotenv}"
                   + (f": {', '.join(loaded)}" if loaded else
                      " (all already set in the environment)"))

    # 1. Bridge ------------------------------------------------------------
    bridge = Bridge()
    try:
        bridge.connect(retries=2, delay=1.0)
        bridge.ping()
        report(OK, "mGBA bridge is reachable on 127.0.0.1:8888")
    except BridgeError as exc:
        report(BAD, str(exc))
        return 1

    # 2. ROM ---------------------------------------------------------------
    info = bridge.info()
    report(OK, f"ROM loaded: code={info.game_code} title={info.title!r} "
               f"size={info.rom_size / 2**20:.0f}MB frame={info.frame}")
    # mGBA reports the full header code ("AGB-BPEE"); compare the bare tail.
    if info.short_code != "BPEE":
        report(WARN, f"expected product code BPEE (Pokemon Emerald US), got "
                     f"{info.short_code!r}. RAM addresses are very likely wrong; "
                     f"override them in config.json.")

    # 3. Emulation actually running ---------------------------------------
    f0 = bridge.frame()
    bridge.idle(10)
    if bridge.frame() > f0:
        report(OK, "emulation is advancing")
    else:
        report(BAD, "frame counter is not moving — is mGBA paused?")

    # 4. ROM table calibration --------------------------------------------
    addresses = mem.Addresses.load(ROOT / "config.json")
    observer = Observer(bridge, addresses, cache_path=ROOT / "runs" / "rom_tables.json")
    try:
        tables = observer.calibrate(force=True)
        report(OK, f"located ROM tables: species_names=0x{tables.species_names:08X} "
                   f"move_names=0x{tables.move_names:08X} "
                   f"battle_moves=0x{tables.battle_moves:08X}")
    except Exception as exc:
        report(BAD, f"ROM table calibration failed: {exc}")
        return 1

    rom = observer.rom

    # Species are indexed by INTERNAL id, not National Dex number: 1-251
    # coincide, 252-276 are unused "?" slots, and Hoenn starts at 277. So look
    # each name up in the table rather than asserting a Dex number.
    table = rom.species_table()
    report(OK, f"species table holds {len(table)} named entries "
               f"(highest id {max(table)})")
    if table.get(1) == "BULBASAUR":
        report(OK, "species #1 reads 'BULBASAUR'")
    else:
        report(BAD, f"species #1 reads {table.get(1)!r}, expected 'BULBASAUR'")

    for name, expected_id in (("TREECKO", 277), ("TORCHIC", 280),
                              ("MUDKIP", 283), ("RAYQUAZA", 406)):
        found = rom.find_species(name)
        if found is None:
            report(BAD, f"{name} is missing from the species table")
        elif found == expected_id:
            report(OK, f"{name} found at internal id {found} (Gen 3 numbering)")
        else:
            report(WARN, f"{name} found at internal id {found}, expected "
                         f"{expected_id} — unusual but not fatal, lookups use "
                         f"whatever id RAM reports")
    for mid, expected in {1: "POUND", 33: "TACKLE", 85: "THUNDERBOLT"}.items():
        got = rom.move_name(mid)
        power, type_id, acc = rom.move_stats(mid)
        report(OK if got == expected else WARN,
               f"move #{mid} reads {got!r} (expected {expected!r}) "
               f"power={power} type={mem.TYPE_NAMES[type_id]} acc={acc}")

    # 5. Save block pointers ----------------------------------------------
    sb1, sb2 = observer._save_blocks()
    for name, ptr in (("SaveBlock1", sb1), ("SaveBlock2", sb2)):
        if 0x02000000 <= ptr < 0x02040000:
            report(OK, f"{name} pointer resolves to 0x{ptr:08X}")
        else:
            report(BAD, f"{name} pointer is 0x{ptr:08X}, outside EWRAM. "
                        f"Either no save is loaded yet, or the pointer address "
                        f"is wrong for this ROM.")

    # 6. Live state --------------------------------------------------------
    try:
        snap = observer.snapshot()
    except Exception as exc:
        report(BAD, f"snapshot failed: {exc}")
        return 1

    report(OK, f"player={snap.player_name!r} play_time={snap.play_time} "
               f"map={snap.map_group}.{snap.map_num} pos=({snap.x},{snap.y})")
    if not snap.player_name:
        report(WARN, "player name is empty — start or load a save file, then rerun")

    if snap.party:
        report(OK, f"party has {len(snap.party)} Pokemon:")
        for m in snap.party:
            moves = ", ".join(f"{mv.name}({mv.pp}pp)" for mv in m.moves) or "none"
            types = mem.TYPE_NAMES[m.type1]
            print(f"         - {m.nickname} ({m.species}) lvl {m.level} "
                  f"{m.hp}/{m.max_hp} HP{' ' + m.status if m.status else ''}")
            print(f"           moves: {moves}")
            bad = [
                lbl for lbl, cond in (
                    ("species out of range", not 0 < m.species_id < 440),
                    ("level out of range", not 0 < m.level <= 100),
                    ("hp exceeds max", m.hp > m.max_hp),
                    ("no moves", not m.moves),
                ) if cond
            ]
            if bad:
                report(BAD, f"{m.nickname}: {'; '.join(bad)} — "
                            f"player_party address is probably wrong")
    else:
        count = bridge.read(addresses.player_party_count, 1)[0]
        if count == 0:
            report(OK, f"party is empty and the count byte agrees (reads 0) — "
                       f"consistent with not having a starter yet. "
                       f"Play time is {snap.play_time}; rerun this once you "
                       f"have a Pokemon to fully validate player_party.")
        else:
            report(BAD, f"party count byte reads {count} but no Pokemon "
                        f"decoded — player_party (0x{addresses.player_party:08X}) "
                        f"is wrong for this ROM")

    report(OK, f"badges: {', '.join(snap.badges) if snap.badges else 'none'}")
    report(OK, f"in_battle={snap.in_battle}")
    if snap.in_battle:
        report(OK, f"  active={snap.active.nickname} {snap.active.hp}/{snap.active.max_hp}")
        report(OK, f"  opponent={snap.opponent.nickname} lvl {snap.opponent.level}")

    # 7. Map data ----------------------------------------------------------
    from pokeauto.mapdata import MapNames, MapReader

    names = MapNames(bridge, addresses)
    if names.lookup(0, 9)[0] == "LITTLEROOT TOWN":
        report(OK, "map name tables resolve: map 0.9 is LITTLEROOT TOWN")
    else:
        report(WARN, f"map 0.9 resolves to {names.lookup(0, 9)[0]!r}, expected "
                     f"LITTLEROOT TOWN — map_groups or region_map_entries is "
                     f"wrong for this ROM; names will degrade to raw ids")
    report(OK, f"you are in {names.describe(snap.map_group, snap.map_num)}")

    view = MapReader(bridge, addresses).read()
    if view is None:
        report(WARN, "no map layout readable right now (normal during a "
                     "cutscene or transition; rerun while walking around)")
    else:
        report(OK, f"map layout: {view.width}x{view.height}, "
                   f"{len(view.warps)} warp(s), {len(view.objects)} object(s), "
                   f"connections: {', '.join(view.connections) or 'none (indoor)'}")
        if view.passable(snap.x, snap.y):
            report(OK, "the tile you are standing on reads as walkable")
        else:
            report(BAD, f"({snap.x},{snap.y}) reads as a WALL — "
                        f"backup_map_layout is wrong for this ROM")
        walkable = len(view.reachable((snap.x, snap.y)))
        report(OK, f"{walkable} tile(s) reachable from where you stand")
        for w in view.warps:
            report(OK, f"  exit ({w.x},{w.y}) -> "
                       f"{names.describe(w.dest_group, w.dest_num)}")

    # 8. TypeSafe ----------------------------------------------------------
    if not os.environ.get("TYPESAFE_API_KEY"):
        report(WARN, "TYPESAFE_API_KEY is not set. Put it in .env "
                     f"({ROOT / '.env'}) or export it in your shell; "
                     "until then the agent can only run with --offline")
    else:
        try:
            from typesafe_sdk import Noul, TypeSafeClient
            client = TypeSafeClient()
            resp = client.system_one(
                state="A level 5 Torchic with 3 HP left is facing a level 30 Gyarados.",
                questions={"losing": Noul(instructions="The trainer is in trouble.")},
                model="jev-latest",
            )
            report(OK, f"TypeSafe reachable; test noul returned "
                       f"{resp.answers['losing'].noul:.3f}")
        except ImportError:
            report(BAD, "typesafe-sdk is not installed (pip install typesafe-sdk)")
        except Exception as exc:
            report(BAD, f"TypeSafe call failed: {exc}")

    # Summary --------------------------------------------------------------
    print("=" * 60)
    fails = sum(1 for lvl, _ in _results if lvl == BAD)
    warns = sum(1 for lvl, _ in _results if lvl == WARN)
    print(f"{len(_results)} checks: {fails} failed, {warns} warnings")
    if fails:
        print("\nFix the failures before running scripts/play.py.")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
