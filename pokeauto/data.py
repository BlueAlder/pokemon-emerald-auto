"""Game data tables read from ROM, and the Gen 3 damage formula.

Species base stats/types/abilities, move data, the type chart and trainer
parties are read straight out of the cartridge at their decomp addresses, so
the battle AI works from the exact numbers the game uses.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from functools import lru_cache

from .symbols import const, const_names, symbols

S = symbols()
C = const

TYPE_NAMES = ["NORMAL", "FIGHTING", "FLYING", "POISON", "GROUND", "ROCK", "BUG", "GHOST",
              "STEEL", "MYSTERY", "FIRE", "WATER", "GRASS", "ELECTRIC", "PSYCHIC", "ICE",
              "DRAGON", "DARK"]


def is_physical(type_id: int) -> bool:
    return type_id < C("TYPE_MYSTERY")


@dataclass(frozen=True)
class SpeciesInfo:
    id: int
    hp: int
    atk: int
    df: int
    spe: int
    spa: int
    spd: int
    types: tuple[int, int]
    catch_rate: int
    exp_yield: int
    growth: int
    abilities: tuple[int, int]

    @property
    def name(self) -> str:
        return const_names()["SPECIES_"].get(self.id, str(self.id))[8:]


@dataclass(frozen=True)
class MoveInfo:
    id: int
    effect: int
    power: int
    type: int
    accuracy: int
    pp: int
    secondary_chance: int
    target: int
    priority: int
    flags: int

    @property
    def name(self) -> str:
        return const_names()["MOVE_"].get(self.id, str(self.id))[5:]


class GameData:
    def __init__(self, emu):
        self.emu = emu
        self._species: dict[int, SpeciesInfo] = {}
        self._moves: dict[int, MoveInfo] = {}
        self._chart: dict[tuple[int, int], float] | None = None

    def species(self, sid: int) -> SpeciesInfo:
        if sid not in self._species:
            raw = self.emu.read(S["gSpeciesInfo"] + sid * 0x1C, 0x1C)
            self._species[sid] = SpeciesInfo(
                id=sid, hp=raw[0], atk=raw[1], df=raw[2], spe=raw[3], spa=raw[4], spd=raw[5],
                types=(raw[6], raw[7]), catch_rate=raw[8], exp_yield=raw[9],
                growth=raw[0x13], abilities=(raw[0x16], raw[0x17]))
        return self._species[sid]

    def move(self, mid: int) -> MoveInfo:
        if mid not in self._moves:
            raw = self.emu.read(S["gBattleMoves"] + mid * 12, 12)
            self._moves[mid] = MoveInfo(
                id=mid, effect=raw[0], power=raw[1], type=raw[2], accuracy=raw[3],
                pp=raw[4], secondary_chance=raw[5], target=raw[6],
                priority=struct.unpack("b", raw[7:8])[0], flags=raw[8])
        return self._moves[mid]

    def wild_land(self) -> dict[str, list[tuple[int, int, int]]]:
        """map id -> [(min_level, max_level, species)] for grass/cave encounters."""
        if getattr(self, "_wild", None) is None:
            from .symbols import maps
            by_num = {(v["group"], v["num"]): k for k, v in maps().items()}
            out: dict[str, list[tuple[int, int, int]]] = {}
            base = S["gWildMonHeaders"]
            for i in range(S.size("gWildMonHeaders") // 20):
                grp, num, land = struct.unpack("<BBxxI", self.emu.read(base + i * 20, 8))
                if grp == 0xFF:
                    break
                mid = by_num.get((grp, num))
                if not land or mid is None:
                    continue
                mons = self.emu.u32(land + 4)
                raw = self.emu.read(mons, 12 * 4)
                out[mid] = [struct.unpack_from("<BBH", raw, j * 4) for j in range(12)]
            self._wild = out
        return self._wild

    def type_chart(self) -> dict[tuple[int, int], float]:
        """(attacking type, defending type) -> multiplier, from gTypeEffectiveness."""
        if self._chart is None:
            chart = {}
            raw = self.emu.read(S["gTypeEffectiveness"], S.size("gTypeEffectiveness"))
            for i in range(0, len(raw) - 2, 3):
                a, d, m = raw[i], raw[i + 1], raw[i + 2]
                if a == 0xFF:
                    break
                if a == 0xFE:           # TYPE_FORESIGHT separator
                    continue
                chart[(a, d)] = m / 10
            self._chart = chart
        return self._chart

    def effectiveness(self, move_type: int, def_types: tuple[int, int]) -> float:
        chart = self.type_chart()
        mult = chart.get((move_type, def_types[0]), 1.0)
        if def_types[1] != def_types[0]:
            mult *= chart.get((move_type, def_types[1]), 1.0)
        return mult

    def exp_for_level(self, growth: int, level: int) -> int:
        return struct.unpack("<I", self.emu.read(
            S["gExperienceTables"] + (growth * 101 + level) * 4, 4))[0]

    def level_up_moves(self, sid: int) -> list[tuple[int, int]]:
        """[(level, move)] from gLevelUpLearnsets (u16: level<<9 | move)."""
        ptr = struct.unpack("<I", self.emu.read(S["gLevelUpLearnsets"] + sid * 4, 4))[0]
        out = []
        for i in range(64):
            v = struct.unpack("<H", self.emu.read(ptr + i * 2, 2))[0]
            if v == 0xFFFF:
                break
            out.append((v >> 9, v & 0x1FF))
        return out

    def can_learn_tmhm(self, sid: int, tmhm_index: int) -> bool:
        """tmhm_index: 0..49 for TM01..TM50, 50..57 for HM01..HM08."""
        raw = struct.unpack("<Q", self.emu.read(S["gTMHMLearnsets"] + sid * 8, 8))[0]
        return bool(raw >> tmhm_index & 1)

    def trainer(self, tid: int) -> dict:
        base = S["gTrainers"] + tid * 0x28
        raw = self.emu.read(base, 0x28)
        flags, size = raw[0], raw[0x20]
        party_ptr = struct.unpack_from("<I", raw, 0x24)[0]
        custom, item = bool(flags & 1), bool(flags & 2)
        # struct TrainerMon* sizes: {iv u16, lvl u8, species u16[, item u16][, moves u16[4]]}
        stride = {(False, False): 6, (False, True): 8, (True, False): 14, (True, True): 16}[
            (custom, item)]
        mons = []
        for i in range(size):
            m = self.emu.read(party_ptr + i * stride, stride)
            iv, lvl, species = struct.unpack_from("<HBxH", m, 0)
            entry = {"level": lvl, "species": species, "iv": iv}
            if item:
                entry["item"] = struct.unpack_from("<H", m, 6)[0]
            if custom:
                entry["moves"] = list(struct.unpack_from("<4H", m, 8 if item else 6))
            mons.append(entry)
        return {"double": bool(raw[0x18]), "party": mons, "class": raw[1]}


# ---------------------------------------------------------------------------
# Damage
# ---------------------------------------------------------------------------

STAGE_RATIOS = [(10, 40), (10, 35), (10, 30), (10, 25), (10, 20), (10, 15), (10, 10),
                (15, 10), (20, 10), (25, 10), (30, 10), (35, 10), (40, 10)]


def _stage(stat: int, stage: int) -> int:
    n, d = STAGE_RATIOS[max(0, min(12, stage))]
    return stat * n // d


@dataclass
class Combatant:
    """What the damage model needs about one side, in or out of battle."""
    species: int
    level: int
    hp: int
    max_hp: int
    atk: int
    df: int
    spa: int
    spd: int
    spe: int
    types: tuple[int, int]
    ability: int = 0
    stages: tuple[int, ...] = (6, 6, 6, 6, 6, 6, 6, 6)   # HP ATK DEF SPE SPA SPD ACC EVA
    status1: int = 0
    item: int = 0


FIXED_EFFECTS = {"EFFECT_LEVEL_DAMAGE", "EFFECT_DRAGON_RAGE", "EFFECT_SONICBOOM",
                 "EFFECT_SUPER_FANG", "EFFECT_PSYWAVE"}


def estimate_damage(gd: GameData, atk: Combatant, dfn: Combatant, move: MoveInfo,
                    weather: str = "", roll: float = 0.925, badge_boosts=(False,) * 3
                    ) -> float:
    """Expected damage in HP (not accounting for accuracy or crits)."""
    eff_name = const_names()["EFFECT_"].get(move.effect, "")
    mtype = move.type
    power = move.power
    if eff_name == "EFFECT_LEVEL_DAMAGE":
        return float(atk.level) if gd.effectiveness(mtype, dfn.types) else 0.0
    if eff_name == "EFFECT_DRAGON_RAGE":
        return 40.0
    if eff_name == "EFFECT_SONICBOOM":
        return 20.0 if gd.effectiveness(mtype, dfn.types) else 0.0
    if eff_name == "EFFECT_SUPER_FANG":
        return dfn.hp / 2
    if power <= 1 or eff_name in ("EFFECT_OHKO",):
        return 0.0

    ab = const_names()["ABILITY_"]
    d_ab = ab.get(dfn.ability, "")
    a_ab = ab.get(atk.ability, "")
    # Immunity abilities
    if d_ab == "ABILITY_LEVITATE" and mtype == C("TYPE_GROUND"):
        return 0.0
    if d_ab in ("ABILITY_VOLT_ABSORB",) and mtype == C("TYPE_ELECTRIC"):
        return 0.0
    if d_ab in ("ABILITY_WATER_ABSORB",) and mtype == C("TYPE_WATER"):
        return 0.0
    if d_ab == "ABILITY_FLASH_FIRE" and mtype == C("TYPE_FIRE"):
        return 0.0
    if d_ab == "ABILITY_WONDER_GUARD" and gd.effectiveness(mtype, dfn.types) <= 1:
        return 0.0

    a_atk, a_spa = atk.atk, atk.spa
    d_df, d_spd = dfn.df, dfn.spd
    if a_ab in ("ABILITY_HUGE_POWER", "ABILITY_PURE_POWER"):
        a_atk *= 2
    if a_ab == "ABILITY_HUSTLE":
        a_atk = a_atk * 3 // 2
    if a_ab == "ABILITY_GUTS" and atk.status1:
        a_atk = a_atk * 3 // 2
    if d_ab == "ABILITY_THICK_FAT" and mtype in (C("TYPE_FIRE"), C("TYPE_ICE")):
        a_spa //= 2
    if d_ab == "ABILITY_MARVEL_SCALE" and dfn.status1:
        d_df = d_df * 3 // 2
    low_hp = atk.hp <= atk.max_hp // 3
    pinch = {"ABILITY_OVERGROW": "TYPE_GRASS", "ABILITY_BLAZE": "TYPE_FIRE",
             "ABILITY_TORRENT": "TYPE_WATER", "ABILITY_SWARM": "TYPE_BUG"}
    if low_hp and a_ab in pinch and mtype == C(pinch[a_ab]):
        power = power * 3 // 2
    if eff_name == "EFFECT_EXPLOSION":
        d_df //= 2
    if eff_name == "EFFECT_FACADE" and atk.status1:
        power *= 2
    if eff_name in ("EFFECT_ERUPTION",):
        power = max(1, 150 * atk.hp // max(1, atk.max_hp))
    if eff_name == "EFFECT_RETURN":
        power = 70         # friendship unknown to us; middling estimate
    if eff_name == "EFFECT_MAGNITUDE":
        power = 71
    if eff_name == "EFFECT_LOW_KICK":
        power = 60

    if is_physical(mtype):
        a = _stage(a_atk, atk.stages[1])
        d = _stage(d_df, dfn.stages[2])
    else:
        a = _stage(a_spa, atk.stages[4])
        d = _stage(d_spd, dfn.stages[5])
    d = max(1, d)
    dmg = a * power * (2 * atk.level // 5 + 2) // d // 50
    if is_physical(mtype) and atk.status1 & C("STATUS1_BURN") and a_ab != "ABILITY_GUTS":
        dmg //= 2
    if weather == "rain":
        if mtype == C("TYPE_WATER"):
            dmg = dmg * 3 // 2
        elif mtype == C("TYPE_FIRE"):
            dmg //= 2
    elif weather == "sun":
        if mtype == C("TYPE_FIRE"):
            dmg = dmg * 3 // 2
        elif mtype == C("TYPE_WATER"):
            dmg //= 2
    dmg += 2
    if mtype in atk.types:
        dmg = dmg * 15 // 10
    dmg *= gd.effectiveness(mtype, dfn.types)
    dmg *= roll
    # Multi-hit and two-turn moves
    if eff_name in ("EFFECT_MULTI_HIT",):
        dmg *= 3
    elif eff_name in ("EFFECT_DOUBLE_HIT", "EFFECT_TWINEEDLE"):
        dmg *= 2
    elif eff_name in ("EFFECT_TRIPLE_KICK",):
        dmg *= 2
    return float(dmg)


def combatant_from_battle(gd: GameData, bm) -> Combatant:
    return Combatant(species=bm.species, level=bm.level, hp=bm.hp, max_hp=bm.max_hp,
                     atk=bm.attack, df=bm.defense, spa=bm.sp_attack, spd=bm.sp_defense,
                     spe=bm.speed, types=bm.types, ability=bm.ability,
                     stages=tuple(bm.stat_stages), status1=bm.status1, item=bm.item)


def combatant_from_party(gd: GameData, mon) -> Combatant:
    info = gd.species(mon.species)
    return Combatant(species=mon.species, level=mon.level, hp=mon.hp, max_hp=mon.max_hp,
                     atk=mon.attack, df=mon.defense, spa=mon.sp_attack, spd=mon.sp_defense,
                     spe=mon.speed, types=info.types,
                     ability=info.abilities[mon.ability_num] or info.abilities[0],
                     status1=mon.status, item=mon.item)


def estimate_stats(gd: GameData, species: int, level: int, iv: int = 0) -> Combatant:
    """Stats of a trainer's mon from base stats (EVs 0, neutral nature)."""
    info = gd.species(species)
    ivv = iv * 31 // 255

    def st(base):
        return (2 * base + ivv) * level // 100 + 5
    hp = (2 * info.hp + ivv) * level // 100 + level + 10
    return Combatant(species=species, level=level, hp=hp, max_hp=hp, atk=st(info.atk),
                     df=st(info.df), spa=st(info.spa), spd=st(info.spd), spe=st(info.spe),
                     types=info.types, ability=info.abilities[0])


@lru_cache(maxsize=None)
def tmhm_index(item_const: str) -> int:
    """ITEM_TM26 -> 25, ITEM_HM03 -> 52."""
    v = C(item_const)
    return v - C("ITEM_TM01")
