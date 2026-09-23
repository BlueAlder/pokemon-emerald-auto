"""Pokemon Emerald (US / BPEE) memory decoding.

Two classes of address live here:

* **EWRAM/IWRAM symbols** (gPlayerParty, gBattleMons, ...). These are fixed for
  the US retail ROM and are taken from the pokeemerald decompilation. They are
  asserted, not guessed: scripts/doctor.py sanity-checks every one of them
  against live memory, and config.json can override any of them.
* **ROM data tables** (species names, move names, move stats). These are NOT
  hardcoded. We locate them at runtime by searching the ROM for known content
  and verifying neighbouring entries, so a different revision still works.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------
# Gen 3 text encoding
# --------------------------------------------------------------------------

_CHARMAP: dict[int, str] = {0x00: " ", 0x2D: "&", 0xEF: "|", 0xF0: ":", 0xFE: "\n"}
for _i in range(10):
    _CHARMAP[0xA1 + _i] = "0123456789"[_i]
for _i in range(26):
    _CHARMAP[0xBB + _i] = chr(ord("A") + _i)
    _CHARMAP[0xD5 + _i] = chr(ord("a") + _i)
_CHARMAP.update({
    0xAB: "!", 0xAC: "?", 0xAD: ".", 0xAE: "-", 0xAF: "·", 0xB0: "…",
    0xB1: '"', 0xB2: '"', 0xB3: "'", 0xB4: "'", 0xB5: "♂", 0xB6: "♀",
    0xB7: "$", 0xB8: ",", 0xB9: "×", 0xBA: "/",
})
_TERMINATOR = 0xFF
_REVERSE = {v: k for k, v in sorted(_CHARMAP.items(), reverse=True)}


def decode_text(raw: bytes) -> str:
    """Decode a Gen 3 string, stopping at the 0xFF terminator."""
    out: list[str] = []
    for byte in raw:
        if byte == _TERMINATOR:
            break
        out.append(_CHARMAP.get(byte, ""))
    return "".join(out).strip()


def encode_text(text: str) -> bytes:
    """Encode ASCII to Gen 3 bytes. Used to locate ROM tables by content."""
    return bytes(_REVERSE[c] for c in text)


def looks_like_text(s: str, min_len: int = 2) -> bool:
    """Heuristic: did this decode to something a human would recognise?"""
    if len(s) < min_len:
        return False
    printable = sum(c.isalnum() or c in " .,'!?-:\n♂♀" for c in s)
    return printable / len(s) > 0.8


# --------------------------------------------------------------------------
# Types
# --------------------------------------------------------------------------

TYPE_NAMES = [
    "Normal", "Fighting", "Flying", "Poison", "Ground", "Rock", "Bug", "Ghost",
    "Steel", "???", "Fire", "Water", "Grass", "Electric", "Psychic", "Ice",
    "Dragon", "Dark",
]
_T = {name.upper(): i for i, name in enumerate(TYPE_NAMES)}

# Gen 3 type chart. Only non-1.0 matchups are listed.
_CHART: dict[tuple[int, int], float] = {}


def _mk(attacker: str, **rows: float) -> None:
    for defender, mult in rows.items():
        _CHART[(_T[attacker], _T[defender])] = mult


_mk("NORMAL", ROCK=0.5, GHOST=0.0, STEEL=0.5)
_mk("FIGHTING", NORMAL=2, FLYING=0.5, POISON=0.5, ROCK=2, BUG=0.5, GHOST=0.0,
    STEEL=2, PSYCHIC=0.5, ICE=2, DARK=2)
_mk("FLYING", FIGHTING=2, ROCK=0.5, BUG=2, STEEL=0.5, GRASS=2, ELECTRIC=0.5)
_mk("POISON", POISON=0.5, GROUND=0.5, ROCK=0.5, GHOST=0.5, STEEL=0.0, GRASS=2)
_mk("GROUND", FLYING=0.0, POISON=2, ROCK=2, BUG=0.5, STEEL=2, FIRE=2, GRASS=0.5,
    ELECTRIC=2)
_mk("ROCK", FIGHTING=0.5, FLYING=2, GROUND=0.5, BUG=2, STEEL=0.5, FIRE=2, ICE=2)
_mk("BUG", FIGHTING=0.5, FLYING=0.5, POISON=0.5, GHOST=0.5, STEEL=0.5, FIRE=0.5,
    GRASS=2, PSYCHIC=2, DARK=2)
_mk("GHOST", NORMAL=0.0, GHOST=2, STEEL=0.5, PSYCHIC=2, DARK=0.5)
_mk("STEEL", ROCK=2, STEEL=0.5, FIRE=0.5, WATER=0.5, ELECTRIC=0.5, ICE=2)
_mk("FIRE", ROCK=0.5, BUG=2, STEEL=2, FIRE=0.5, WATER=0.5, GRASS=2, ICE=2,
    DRAGON=0.5)
_mk("WATER", GROUND=2, ROCK=2, FIRE=2, WATER=0.5, GRASS=0.5, DRAGON=0.5)
_mk("GRASS", FLYING=0.5, POISON=0.5, GROUND=2, ROCK=2, BUG=0.5, STEEL=0.5,
    FIRE=0.5, WATER=2, GRASS=0.5, DRAGON=0.5)
_mk("ELECTRIC", FLYING=2, GROUND=0.0, WATER=2, GRASS=0.5, ELECTRIC=0.5, DRAGON=0.5)
_mk("PSYCHIC", FIGHTING=2, POISON=2, STEEL=0.5, PSYCHIC=0.5, DARK=0.0)
_mk("ICE", FLYING=2, GROUND=2, STEEL=0.5, FIRE=0.5, WATER=0.5, GRASS=2, ICE=0.5,
    DRAGON=2)
_mk("DRAGON", STEEL=0.5, DRAGON=2)
_mk("DARK", FIGHTING=0.5, GHOST=2, STEEL=0.5, PSYCHIC=2, DARK=0.5)


def effectiveness(move_type: int, def_type1: int, def_type2: int) -> float:
    """Combined type multiplier, exactly as the game computes it."""
    mult = _CHART.get((move_type, def_type1), 1.0)
    if def_type2 != def_type1:
        mult *= _CHART.get((move_type, def_type2), 1.0)
    return mult


def effectiveness_label(mult: float) -> str:
    if mult == 0:
        return "no effect"
    if mult >= 4:
        return "quadruple damage"
    if mult >= 2:
        return "double damage"
    if mult <= 0.25:
        return "quarter damage"
    if mult <= 0.5:
        return "half damage"
    return "normal damage"


STATUS_FLAGS = [
    (0x07, "asleep"), (0x08, "poisoned"), (0x10, "burned"),
    (0x20, "frozen"), (0x40, "paralyzed"), (0x80, "badly poisoned"),
]


def status_label(status1: int) -> str | None:
    for mask, name in STATUS_FLAGS:
        if status1 & mask:
            return name
    return None


# --------------------------------------------------------------------------
# Address map (US retail Emerald, game code BPEE)
# --------------------------------------------------------------------------

@dataclass
class Addresses:
    save_block1_ptr: int = 0x03005D8C
    save_block2_ptr: int = 0x03005D90
    player_party_count: int = 0x020244E9
    player_party: int = 0x020244EC
    enemy_party_count: int = 0x02024741
    enemy_party: int = 0x02024744
    battle_mons: int = 0x02024084
    battle_type_flags: int = 0x02022FEC
    battle_outcome: int = 0x0202433A
    battler_party_indexes: int = 0x02024076
    battlers_count: int = 0x0202406C
    backup_map_layout: int = 0x03005DC0   # {s32 w; s32 h; u16 *grid}
    map_header: int = 0x02037318          # gMapHeader -> events -> warps
    # ROM tables for naming maps. Validated by the doctor, overridable here.
    map_groups: int = 0x08486578          # gMapGroups[group][num] -> MapHeader
    region_map_entries: int = 0x085A147C  # {u8 x,y,w,h; const u8 *name}[]
    string_var4: int = 0x02021FC4
    battle_string: int = 0x02022E2C
    # Offsets inside the save blocks
    sb1_pos_x: int = 0x0000
    sb1_pos_y: int = 0x0002
    sb1_map_group: int = 0x0004
    sb1_map_num: int = 0x0005
    sb1_flags: int = 0x1270
    sb2_player_name: int = 0x0000
    sb2_gender: int = 0x0008
    sb2_play_time_hours: int = 0x000E
    sb2_play_time_minutes: int = 0x0010
    # ROM search window for table calibration
    rom_scan_start: int = 0x08300000
    rom_scan_len: int = 0x00100000

    @classmethod
    def load(cls, path: Path | None) -> "Addresses":
        base = cls()
        if path and path.exists():
            data = json.loads(path.read_text()).get("addresses", {})
            for key, value in data.items():
                if not hasattr(base, key):
                    raise ValueError(f"unknown address override {key!r}")
                setattr(base, key, int(str(value), 0))
        return base


FLAG_BADGE01 = 0x867
BADGE_NAMES = ["Stone", "Knuckle", "Dynamo", "Heat", "Balance", "Feather",
               "Mind", "Rain"]

PARTY_MON_SIZE = 100
BATTLE_MON_SIZE = 88

_SUBSTRUCT_ORDER = [
    "GAEM", "GAME", "GEAM", "GEMA", "GMAE", "GMEA",
    "AGEM", "AGME", "AEGM", "AEMG", "AMGE", "AMEG",
    "EGAM", "EGMA", "EAGM", "EAMG", "EMGA", "EMAG",
    "MGAE", "MGEA", "MAGE", "MAEG", "MEGA", "MEAG",
]


# --------------------------------------------------------------------------
# Decoded records
# --------------------------------------------------------------------------

@dataclass
class Move:
    slot: int
    id: int
    name: str
    pp: int
    max_pp: int
    power: int
    type_id: int
    accuracy: int

    @property
    def type_name(self) -> str:
        return TYPE_NAMES[self.type_id] if self.type_id < len(TYPE_NAMES) else "?"

    @property
    def usable(self) -> bool:
        return self.id != 0 and self.pp > 0


@dataclass
class Mon:
    slot: int
    species_id: int
    species: str
    nickname: str
    level: int
    hp: int
    max_hp: int
    status: str | None
    moves: list[Move] = field(default_factory=list)
    type1: int = 0
    type2: int = 0
    attack: int = 0
    defense: int = 0
    speed: int = 0
    sp_attack: int = 0
    sp_defense: int = 0

    @property
    def fainted(self) -> bool:
        return self.hp <= 0

    @property
    def hp_fraction(self) -> float:
        return self.hp / self.max_hp if self.max_hp else 0.0


# --------------------------------------------------------------------------
# ROM table calibration
# --------------------------------------------------------------------------

# Gen 3 indexes species by INTERNAL id, which is not the National Dex number.
# Indices 1-251 coincide with the Dex, 252-276 are 25 unused "?" placeholder
# slots, and Hoenn begins at 277 (Treecko 277, Torchic 280, Mudkip 283). RAM
# stores internal ids, so lookups are correct -- but never assume a Dex number.
SPECIES_COUNT = 412          # 0..411; 411 (Chimecho) is the last real entry
FIRST_HOENN_SPECIES = 277
SPECIES_NAME_LEN = 11
MOVE_NAME_LEN = 13
BATTLE_MOVE_SIZE = 12


@dataclass
class RomTables:
    species_names: int
    move_names: int
    battle_moves: int

    def to_json(self) -> dict:
        return {k: f"0x{v:08X}" for k, v in self.__dict__.items()}


class RomTableError(RuntimeError):
    pass


def _find_all(haystack: bytes, needle: bytes) -> list[int]:
    hits, start = [], 0
    while (idx := haystack.find(needle, start)) != -1:
        hits.append(idx)
        start = idx + 1
    return hits


def calibrate_rom_tables(rom: bytes, base_addr: int) -> RomTables:
    """Locate ROM data tables by content, then verify with a second entry.

    `rom` is a slice of ROM starting at `base_addr`. Every table is confirmed
    by checking an entry other than the one we searched for, so a coincidental
    byte match cannot pass.
    """

    def find_name_table(first: str, check_index: int, check_name: str,
                        stride: int) -> int:
        needle = encode_text(first) + bytes([_TERMINATOR])
        for hit in _find_all(rom, needle):
            table = hit - stride  # the searched entry is index 1
            if table < 0:
                continue
            off = table + check_index * stride
            if off + stride > len(rom):
                continue
            if decode_text(rom[off:off + stride]) == check_name:
                return base_addr + table
        raise RomTableError(
            f"could not locate the table containing {first!r}. "
            "Is this an English Pokemon Emerald ROM?"
        )

    species = find_name_table("BULBASAUR", 4, "CHARMANDER", SPECIES_NAME_LEN)
    moves = find_name_table("POUND", 2, "KARATE CHOP", MOVE_NAME_LEN)

    # gBattleMoves: matched on stat values rather than names, so it holds even
    # where text differs. Entry layout is
    # [effect, power, type, accuracy, pp, ...]; we anchor on entry 1 (Pound)
    # and confirm with entries 2 (Karate Chop) and 3 (Double-Slap).
    def entry_stats(table: int, index: int) -> tuple[int, int, int, int]:
        o = table + index * BATTLE_MOVE_SIZE
        return rom[o + 1], rom[o + 2], rom[o + 3], rom[o + 4]

    pound = bytes([40, _T["NORMAL"], 100, 35])
    battle_moves = -1
    start = 0
    while (hit := rom.find(pound, start)) != -1:
        start = hit + 1
        table = hit - 1 - BATTLE_MOVE_SIZE  # hit lands on entry 1's power byte
        if table < 0 or table + BATTLE_MOVE_SIZE * 4 > len(rom):
            continue
        if entry_stats(table, 2) == (50, _T["FIGHTING"], 100, 25) \
           and entry_stats(table, 3) == (15, _T["NORMAL"], 85, 10):
            battle_moves = base_addr + table
            break
    if battle_moves < 0:
        raise RomTableError("could not locate the move stat table (gBattleMoves)")

    return RomTables(species, moves, battle_moves)


class RomData:
    """Name and move-stat lookups, backed by calibrated ROM tables."""

    def __init__(self, bridge, tables: RomTables):
        self.bridge, self.tables = bridge, tables
        self._species: dict[int, str] = {}
        self._species_table: dict[int, str] | None = None
        self._move_names: dict[int, str] = {}
        self._move_stats: dict[int, tuple[int, int, int]] = {}

    def species_name(self, species_id: int) -> str:
        if species_id == 0:
            return "-"
        if species_id not in self._species:
            addr = self.tables.species_names + species_id * SPECIES_NAME_LEN
            self._species[species_id] = (
                decode_text(self.bridge.read(addr, SPECIES_NAME_LEN))
                or f"#{species_id}"
            )
        return self._species[species_id]

    def move_name(self, move_id: int) -> str:
        if move_id == 0:
            return "-"
        if move_id not in self._move_names:
            addr = self.tables.move_names + move_id * MOVE_NAME_LEN
            self._move_names[move_id] = (
                decode_text(self.bridge.read(addr, MOVE_NAME_LEN)) or f"#{move_id}"
            )
        return self._move_names[move_id]

    def move_stats(self, move_id: int) -> tuple[int, int, int]:
        """(power, type_id, accuracy) for a move."""
        if move_id == 0:
            return (0, 0, 0)
        if move_id not in self._move_stats:
            addr = self.tables.battle_moves + move_id * BATTLE_MOVE_SIZE
            raw = self.bridge.read(addr, BATTLE_MOVE_SIZE)
            self._move_stats[move_id] = (raw[1], raw[2], raw[3])
        return self._move_stats[move_id]

    def species_table(self) -> dict[int, str]:
        """Read the whole species name table once; cache both directions.

        Cheap (about 4.5 KB in one round trip) and it removes any need to
        hardcode internal species ids, which do not match Dex numbers.
        """
        if self._species_table is None:
            raw = self.bridge.read(self.tables.species_names,
                                   SPECIES_COUNT * SPECIES_NAME_LEN)
            table = {}
            for i in range(SPECIES_COUNT):
                name = decode_text(raw[i * SPECIES_NAME_LEN:(i + 1) * SPECIES_NAME_LEN])
                if name and name != "?":
                    table[i] = name
            self._species_table = table
            self._species.update(table)
        return self._species_table

    def find_species(self, name: str) -> int | None:
        """Internal species id for a name, or None. Case-insensitive."""
        target = name.upper()
        for sid, sname in self.species_table().items():
            if sname.upper() == target:
                return sid
        return None

    def prefetch_moves(self, move_ids: list[int]) -> None:
        """Warm the cache for a set of moves in as few round trips as we can."""
        wanted = sorted({m for m in move_ids if m and m not in self._move_names})
        if not wanted:
            return
        ranges: list[tuple[int, int]] = []
        for mid in wanted:
            ranges.append((self.tables.move_names + mid * MOVE_NAME_LEN, MOVE_NAME_LEN))
            ranges.append((self.tables.battle_moves + mid * BATTLE_MOVE_SIZE, BATTLE_MOVE_SIZE))
        blobs = self.bridge.read_many(ranges)
        for i, mid in enumerate(wanted):
            name_raw, stat_raw = blobs[2 * i], blobs[2 * i + 1]
            self._move_names[mid] = decode_text(name_raw) or f"#{mid}"
            if len(stat_raw) >= 4:
                self._move_stats[mid] = (stat_raw[1], stat_raw[2], stat_raw[3])


# --------------------------------------------------------------------------
# Structure decoding
# --------------------------------------------------------------------------

def decrypt_party_mon(raw: bytes) -> dict:
    """Decode one 100-byte party Pokemon, including its encrypted substructs."""
    personality, ot_id = struct.unpack_from("<II", raw, 0)
    key = personality ^ ot_id
    order = _SUBSTRUCT_ORDER[personality % 24]

    block = bytearray(raw[0x20:0x50])
    for i in range(0, 48, 4):
        word = struct.unpack_from("<I", block, i)[0] ^ key
        struct.pack_into("<I", block, i, word)

    subs = {order[i]: bytes(block[i * 12:(i + 1) * 12]) for i in range(4)}
    growth, attacks = subs["G"], subs["A"]

    species, held_item = struct.unpack_from("<HH", growth, 0)
    moves = list(struct.unpack_from("<4H", attacks, 0))
    pp = list(attacks[8:12])

    status1, = struct.unpack_from("<I", raw, 0x50)
    level = raw[0x54]
    hp, max_hp, atk, dfn, spd, spa, spd_def = struct.unpack_from("<7H", raw, 0x56)

    return {
        "personality": personality, "species_id": species, "held_item": held_item,
        "nickname": decode_text(raw[0x08:0x12]), "moves": moves, "pp": pp,
        "status1": status1, "level": level, "hp": hp, "max_hp": max_hp,
        "attack": atk, "defense": dfn, "speed": spd,
        "sp_attack": spa, "sp_defense": spd_def,
    }


def decode_battle_mon(raw: bytes) -> dict:
    """Decode one 88-byte gBattleMons entry (unencrypted, live battle stats)."""
    species, atk, dfn, spd, spa, spdf = struct.unpack_from("<6H", raw, 0x00)
    moves = list(struct.unpack_from("<4H", raw, 0x0C))
    stat_stages = list(raw[0x18:0x20])
    ability = raw[0x20]
    type1, type2 = raw[0x21], raw[0x22]
    pp = list(raw[0x24:0x28])
    hp, = struct.unpack_from("<H", raw, 0x28)
    level = raw[0x2A]
    max_hp, item = struct.unpack_from("<HH", raw, 0x2C)
    nickname = decode_text(raw[0x30:0x3B])
    status1, = struct.unpack_from("<I", raw, 0x4C)

    return {
        "species_id": species, "attack": atk, "defense": dfn, "speed": spd,
        "sp_attack": spa, "sp_defense": spdf, "moves": moves, "pp": pp,
        "stat_stages": stat_stages, "ability": ability,
        "type1": type1, "type2": type2, "hp": hp, "max_hp": max_hp,
        "level": level, "item": item, "nickname": nickname, "status1": status1,
    }


def plausible_party_mon(mon: dict) -> bool:
    """Cheap sanity check used by the doctor to validate an address."""
    return (
        0 < mon["species_id"] < SPECIES_COUNT
        and 0 < mon["level"] <= 100
        and 0 < mon["max_hp"] <= 999
        and 0 <= mon["hp"] <= mon["max_hp"]
        and any(mon["moves"])
    )
