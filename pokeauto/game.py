"""Read Emerald's state straight out of RAM, using decomp symbols.

This is the agent's eyes. Nothing here is inferred from behaviour: every value
is the variable the game itself consults. In particular ``Game.mode()`` answers
"what is happening right now" from gMain.callback2, the script context, the
text printers and the task list -- the same things the game's own code checks
before it lets the player move.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .emu import Emu
from .symbols import const, const_names, map_id, symbols

S = symbols()

MAP_OFFSET = 7                     # object coords are map coords + 7

# SaveBlock1 / SaveBlock2 offsets (include/global.h)
SB1_POS, SB1_LOCATION = 0x00, 0x04
SB1_PARTY_COUNT, SB1_MONEY, SB1_COINS = 0x234, 0x490, 0x494
SB1_POCKETS = {                       # offset, capacity
    "items": (0x560, 30), "key": (0x5D8, 30), "balls": (0x650, 16),
    "tmhm": (0x690, 64), "berries": (0x790, 46),
}
SB1_FLAGS, SB1_VARS = 0x1270, 0x139C
SB2_NAME, SB2_GENDER, SB2_PLAYTIME, SB2_OPTIONS, SB2_KEY = 0x00, 0x08, 0x0E, 0x14, 0xAC

TASK_SIZE, NUM_TASKS = 0x28, 16
OBJ_EVENT_SIZE, NUM_OBJ_EVENTS = 0x24, 16
TEXT_PRINTER_SIZE = 0x24
RENDER_STATE_WAIT = 1

DIRS = {1: "down", 2: "up", 3: "left", 4: "right"}

# --------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------

_CHARS: dict[int, str] = {0x00: " ", 0x1B: "é", 0x2D: "&", 0x5C: "(", 0x5D: ")",
                          0xFE: "\n", 0xFA: "\n", 0xFB: "\n\n"}
for _i in range(10):
    _CHARS[0xA1 + _i] = str(_i)
for _i in range(26):
    _CHARS[0xBB + _i] = chr(65 + _i)
    _CHARS[0xD5 + _i] = chr(97 + _i)
_CHARS.update({0xAB: "!", 0xAC: "?", 0xAD: ".", 0xAE: "-", 0xAF: "·", 0xB0: "…",
               0xB1: '"', 0xB2: '"', 0xB3: "'", 0xB4: "'", 0xB5: "♂", 0xB6: "♀",
               0xB7: "$", 0xB8: ",", 0xB9: "×", 0xBA: "/", 0xF0: ":", 0x34: "Lv",
               0x35: "=", 0x36: ";", 0x51: "¿", 0x52: "¡", 0x53: "PK", 0x54: "MN"})
# Number of argument bytes after an 0xFC control code (text.c).
_FC_ARGS = {0x01: 1, 0x02: 1, 0x03: 1, 0x04: 3, 0x05: 1, 0x06: 1, 0x08: 1,
            0x0B: 2, 0x0C: 1, 0x0D: 1, 0x0E: 1, 0x10: 2, 0x11: 1, 0x12: 1,
            0x13: 1, 0x14: 1, 0x15: 0, 0x16: 0, 0x17: 0, 0x18: 0}


def decode_text(raw: bytes) -> str:
    out, i = [], 0
    while i < len(raw):
        b = raw[i]
        if b == 0xFF:
            break
        if b == 0xFC:
            i += 2 + _FC_ARGS.get(raw[i + 1] if i + 1 < len(raw) else 0, 0)
            continue
        if b == 0xFD:              # placeholder (player name etc.)
            out.append("{}")
            i += 2
            continue
        out.append(_CHARS.get(b, ""))
        i += 1
    return "".join(out)


def encode_text(s: str) -> bytes:
    rev = {v: k for k, v in _CHARS.items() if len(v) == 1 and v != "\n"}
    return bytes(rev[c] for c in s) + b"\xFF"


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------

@dataclass
class Move:
    id: int
    pp: int
    name: str = ""

    @property
    def const(self) -> str:
        return const_names()["MOVE_"].get(self.id, str(self.id))


@dataclass
class Mon:
    slot: int
    species: int
    nickname: str
    level: int
    hp: int
    max_hp: int
    attack: int
    defense: int
    speed: int
    sp_attack: int
    sp_defense: int
    status: int
    moves: list[Move]
    item: int = 0
    exp: int = 0
    ability_num: int = 0
    personality: int = 0
    is_egg: bool = False

    @property
    def species_name(self) -> str:
        return const_names()["SPECIES_"].get(self.species, str(self.species))[8:]

    @property
    def fainted(self) -> bool:
        return self.hp == 0

    @property
    def hp_frac(self) -> float:
        return self.hp / self.max_hp if self.max_hp else 0.0

    def knows(self, move: str | int) -> bool:
        mid = const(move) if isinstance(move, str) else move
        return any(m.id == mid for m in self.moves)

    def __str__(self) -> str:
        mv = ",".join(f"{m.const[5:]}({m.pp})" for m in self.moves)
        return f"{self.species_name} L{self.level} {self.hp}/{self.max_hp} [{mv}]"


_SUB_ORDER = ["GAEM", "GAME", "GEAM", "GEMA", "GMAE", "GMEA", "AGEM", "AGME",
              "AEGM", "AEMG", "AMGE", "AMEG", "EGAM", "EGMA", "EAGM", "EAMG",
              "EMGA", "EMAG", "MGAE", "MGEA", "MAGE", "MAEG", "MEGA", "MEAG"]


def decode_party_mon(raw: bytes, slot: int) -> Mon | None:
    personality, ot_id = struct.unpack_from("<II", raw, 0)
    if personality == 0 and ot_id == 0:
        return None
    key = personality ^ ot_id
    block = bytearray(raw[0x20:0x50])
    for i in range(0, 48, 4):
        struct.pack_into("<I", block, i, struct.unpack_from("<I", block, i)[0] ^ key)
    order = _SUB_ORDER[personality % 24]
    sub = {order[i]: bytes(block[i * 12:(i + 1) * 12]) for i in range(4)}
    species, item, exp = struct.unpack_from("<HHI", sub["G"], 0)
    move_ids = struct.unpack_from("<4H", sub["A"], 0)
    pps = sub["A"][8:12]
    misc_iv = struct.unpack_from("<I", sub["M"], 4)[0]
    flags_egg = raw[0x13]
    status, = struct.unpack_from("<I", raw, 0x50)
    level = raw[0x54]
    hp, max_hp, atk, dfn, spe, spa, spd = struct.unpack_from("<7H", raw, 0x56)
    if species == 0:
        return None
    return Mon(slot=slot, species=species, nickname=decode_text(raw[0x08:0x12]),
               level=level, hp=hp, max_hp=max_hp, attack=atk, defense=dfn, speed=spe,
               sp_attack=spa, sp_defense=spd, status=status,
               moves=[Move(m, p) for m, p in zip(move_ids, pps) if m],
               item=item, exp=exp, ability_num=(misc_iv >> 31) & 1,
               personality=personality, is_egg=bool(misc_iv >> 30 & 1) or bool(flags_egg & 4))


@dataclass
class BattleMon:
    species: int
    attack: int
    defense: int
    speed: int
    sp_attack: int
    sp_defense: int
    moves: list[Move]
    stat_stages: list[int]
    ability: int
    types: tuple[int, int]
    hp: int
    level: int
    max_hp: int
    item: int
    nickname: str
    status1: int
    status2: int

    @property
    def species_name(self) -> str:
        return const_names()["SPECIES_"].get(self.species, str(self.species))[8:]

    @property
    def hp_frac(self) -> float:
        return self.hp / self.max_hp if self.max_hp else 0.0


def decode_battle_mon(raw: bytes) -> BattleMon:
    species, atk, dfn, spe, spa, spd = struct.unpack_from("<6H", raw, 0)
    move_ids = struct.unpack_from("<4H", raw, 0x0C)
    pps = raw[0x24:0x28]
    hp, = struct.unpack_from("<H", raw, 0x28)
    max_hp, item = struct.unpack_from("<HH", raw, 0x2C)
    status1, status2 = struct.unpack_from("<II", raw, 0x4C)
    return BattleMon(species=species, attack=atk, defense=dfn, speed=spe,
                     sp_attack=spa, sp_defense=spd,
                     moves=[Move(m, p) for m, p in zip(move_ids, pps) if m],
                     stat_stages=list(raw[0x18:0x20]), ability=raw[0x20],
                     types=(raw[0x21], raw[0x22]), hp=hp, level=raw[0x2A],
                     max_hp=max_hp, item=item, nickname=decode_text(raw[0x30:0x3B]),
                     status1=status1, status2=status2)


@dataclass
class ObjectEvent:
    index: int
    local_id: int
    graphics_id: int
    x: int              # map coordinates
    y: int
    elevation: int
    facing: str
    active: bool
    is_player: bool
    trainer_type: int
    movement_type: int
    invisible: bool
    moving: bool


# --------------------------------------------------------------------------
# Game
# --------------------------------------------------------------------------

@dataclass
class Mode:
    """What the game is doing. The runner dispatches on this."""
    kind: str                       # overworld | script | battle | title | naming | other
    callback2: str
    tasks: list[str] = field(default_factory=list)
    detail: str = ""

    def __str__(self) -> str:
        t = ",".join(self.tasks)
        return f"{self.kind}[{self.callback2}{' ' + self.detail if self.detail else ''}]{{{t}}}"


class Game:
    def __init__(self, emu: Emu):
        self.emu = emu
        self.a_main = S["gMain"]
        self.a_sb1p = S["gSaveBlock1Ptr"]
        self.a_sb2p = S["gSaveBlock2Ptr"]
        self.a_tasks = S["gTasks"]
        self.a_objs = S["gObjectEvents"]
        self.a_avatar = S["gPlayerAvatar"]
        self.a_printers = S["sTextPrinters"]
        self.a_msgbox = S["sFieldMessageBoxMode"]
        self.a_script_status = S["sGlobalScriptContextStatus"]
        self.a_script_ctx = S["sGlobalScriptContext"]
        self.a_lock = S["sLockFieldControls"]
        self.a_fade = S["gPaletteFade"]
        self.cb_overworld = S["CB2_Overworld"]
        self.cb_battle = S["BattleMainCB2"]

    # -- raw ------------------------------------------------------------------
    def sb1(self) -> int:
        return self.emu.u32(self.a_sb1p)

    def sb2(self) -> int:
        return self.emu.u32(self.a_sb2p)

    def object_templates(self) -> dict[int, tuple[int, int]]:
        """local_id -> (x, y) from the save block's copy of this map's templates.

        Scripts and puzzles (setobjectxyperm, Mossdeep's rotating statues)
        move objects by rewriting these, so they beat the ROM positions for
        anything not currently spawned.
        """
        raw = self.emu.read(self.sb1() + 0xC70, 64 * 0x18)
        out = {}
        for i in range(64):
            lid = raw[i * 0x18]
            if lid == 0 and i:
                continue
            x, y = struct.unpack_from("<hh", raw, i * 0x18 + 4)
            out[lid] = (x, y)
        return out

    def flag(self, flag: str | int) -> bool:
        fid = const(flag) if isinstance(flag, str) else flag
        byte = self.emu.u8(self.sb1() + SB1_FLAGS + fid // 8)
        return bool(byte >> (fid % 8) & 1)

    def var(self, var: str | int) -> int:
        vid = const(var) if isinstance(var, str) else var
        if vid >= 0x8000:          # special vars live in their own symbols
            name = {0x800D: "gSpecialVar_Result", 0x8004: "gSpecialVar_0x8004",
                    0x8005: "gSpecialVar_0x8005", 0x8006: "gSpecialVar_0x8006"}[vid]
            return self.emu.u16(S[name])
        return self.emu.u16(self.sb1() + SB1_VARS + (vid - 0x4000) * 2)

    def callback2(self) -> int:
        return self.emu.u32(self.a_main + 4) & ~1

    def callback1(self) -> int:
        return self.emu.u32(self.a_main) & ~1

    def active_tasks(self) -> list[str]:
        raw = self.emu.read(self.a_tasks, TASK_SIZE * NUM_TASKS)
        out = []
        for i in range(NUM_TASKS):
            func, active = struct.unpack_from("<IB", raw, i * TASK_SIZE)
            if active:
                out.append(S.name_at(func))
        return out

    def task_data(self, func_name: str) -> list[int] | None:
        """data[16] of the first active task running `func_name`."""
        target = S[func_name]
        raw = self.emu.read(self.a_tasks, TASK_SIZE * NUM_TASKS)
        for i in range(NUM_TASKS):
            func, active = struct.unpack_from("<IB", raw, i * TASK_SIZE)
            if active and func & ~1 == target:
                return list(struct.unpack_from("<16h", raw, i * TASK_SIZE + 8))
        return None

    # -- mode -----------------------------------------------------------------
    def fields_locked(self) -> bool:
        return bool(self.emu.u8(self.a_lock))

    def script_running(self) -> bool:
        return self.emu.u8(self.a_script_status) != 2

    def script_native(self) -> str:
        """Native function the global script is blocked in ('' if none).

        e.g. WaitForAorBPress (waitbuttonpress), IsFieldMessageBoxHidden
        (waitmessage), WaitForMovementFinish (waitmovement).
        """
        raw = self.emu.read(self.a_script_ctx, 8)
        if raw[1] != 2:            # SCRIPT_MODE_NATIVE
            return ""
        return S.name_at(struct.unpack_from("<I", raw, 4)[0])

    def msgbox_open(self) -> bool:
        return self.emu.u8(self.a_msgbox) != 0

    def fading(self) -> bool:
        # struct PaletteFadeControl: bitfield word at +0x07 has `active` in bit 7
        return bool(self.emu.u8(self.a_fade + 7) & 0x80)

    def printer(self, window: int = 0) -> tuple[bool, int]:
        raw = self.emu.read(self.a_printers + window * TEXT_PRINTER_SIZE, TEXT_PRINTER_SIZE)
        return bool(raw[0x1B]), raw[0x1C]

    def text_waiting(self) -> bool:
        """Any text printer is paused waiting for A/B."""
        raw = self.emu.read(self.a_printers, TEXT_PRINTER_SIZE * 32)
        for w in range(32):
            if raw[w * TEXT_PRINTER_SIZE + 0x1B] and \
               raw[w * TEXT_PRINTER_SIZE + 0x1C] == RENDER_STATE_WAIT:
                return True
        return False

    def text_printing(self) -> bool:
        raw = self.emu.read(self.a_printers, TEXT_PRINTER_SIZE * 32)
        return any(raw[w * TEXT_PRINTER_SIZE + 0x1B] for w in range(32))

    def mode(self) -> Mode:
        cb2 = self.callback2()
        name = S.name_at(cb2)
        tasks = self.active_tasks()
        if cb2 == self.cb_battle or name.startswith(("CB2_InitBattle", "BattleIntro",
                                                      "CB2_HandleStartBattle")):
            return Mode("battle", name, tasks)
        if cb2 == self.cb_overworld:
            if self.script_running() or self.fields_locked() or self.msgbox_open():
                return Mode("script", name, tasks, self.script_native())
            return Mode("overworld", name, tasks)
        if "NamingScreen" in name:
            return Mode("naming", name, tasks)
        if ("Title" in name or "Intro" in name or "MainMenu" in name or "CopyrightScreen" in name
                or any(("TitleScreen" in t or "NewGameBirchSpeech" in t or "MainMenu" in t)
                       for t in tasks)):
            return Mode("title", name, tasks)
        if "Evolution" in name:
            return Mode("evolution", name, tasks)
        return Mode("other", name, tasks)

    # -- player ---------------------------------------------------------------
    def location(self) -> tuple[int, int]:
        """(map group, map number)."""
        raw = self.emu.read(self.sb1() + SB1_LOCATION, 2)
        return raw[0], raw[1]

    def map_id(self) -> str:
        return map_id(*self.location())

    def pos(self) -> tuple[int, int]:
        raw = self.emu.read(self.sb1() + SB1_POS, 4)
        return struct.unpack("<hh", raw)

    def avatar(self) -> dict:
        raw = self.emu.read(self.a_avatar, 0x0C)
        return {"flags": raw[0], "running_state": raw[2], "tile_transition": raw[3],
                "object_id": raw[5], "prevent_step": raw[6], "gender": raw[7]}

    def objects(self) -> list[ObjectEvent]:
        raw = self.emu.read(self.a_objs, OBJ_EVENT_SIZE * NUM_OBJ_EVENTS)
        out = []
        for i in range(NUM_OBJ_EVENTS):
            o = raw[i * OBJ_EVENT_SIZE:(i + 1) * OBJ_EVENT_SIZE]
            if not o[0] & 1:
                continue
            x, y = struct.unpack_from("<hh", o, 0x10)
            out.append(ObjectEvent(
                index=i, local_id=o[8], graphics_id=o[5],
                x=x - MAP_OFFSET, y=y - MAP_OFFSET, elevation=o[0x0B] & 0xF,
                facing=DIRS.get(o[0x18] & 0xF, "?"), active=True,
                is_player=bool(o[2] & 1), trainer_type=o[7], movement_type=o[6],
                invisible=bool(o[1] >> 5 & 1),
                moving=bool(o[0] >> 1 & 1) or bool(o[0] >> 6 & 1)))
        return out

    def player_object(self) -> ObjectEvent | None:
        return next((o for o in self.objects() if o.is_player), None)

    def facing(self) -> str:
        p = self.player_object()
        return p.facing if p else "down"

    def player_elevation(self) -> int:
        p = self.player_object()
        return p.elevation if p else 0

    def surfing(self) -> bool:
        return bool(self.avatar()["flags"] & 0x18)      # SURFING or UNDERWATER (Dive)

    def on_bike(self) -> bool:
        return bool(self.avatar()["flags"] & 0x06)

    # -- trainer card -----------------------------------------------------------
    def player_name(self) -> str:
        return decode_text(self.emu.read(self.sb2() + SB2_NAME, 8))

    def play_time(self) -> str:
        raw = self.emu.read(self.sb2() + SB2_PLAYTIME, 3)
        return f"{struct.unpack_from('<H', raw)[0]}:{raw[2]:02d}"

    def options(self) -> dict:
        v = self.emu.u16(self.sb2() + SB2_OPTIONS)
        return {"text_speed": v & 7, "battle_scene_off": bool(v >> 9 & 1),
                "battle_style_set": bool(v >> 8 & 1)}

    def _key(self) -> int:
        return self.emu.u32(self.sb2() + SB2_KEY)

    def money(self) -> int:
        return self.emu.u32(self.sb1() + SB1_MONEY) ^ self._key()

    def badges(self) -> int:
        first = const("FLAG_BADGE01_GET")
        return sum(self.flag(first + i) for i in range(8))

    # -- party / bag ------------------------------------------------------------
    def party(self) -> list[Mon]:
        count = min(self.emu.u8(S["gPlayerPartyCount"]), 6)
        raw = self.emu.read(S["gPlayerParty"], 100 * 6)
        out = []
        for i in range(count):
            m = decode_party_mon(raw[i * 100:(i + 1) * 100], i)
            if m:
                out.append(m)
        return out

    def enemy_party(self) -> list[Mon]:
        raw = self.emu.read(S["gEnemyParty"], 100 * 6)
        return [m for i in range(6) if (m := decode_party_mon(raw[i * 100:(i + 1) * 100], i))]

    def bag(self, pocket: str = "items") -> dict[int, int]:
        off, cap = SB1_POCKETS[pocket]
        raw = self.emu.read(self.sb1() + off, cap * 4)
        key = self._key() & 0xFFFF
        out = {}
        for i in range(cap):
            item, qty = struct.unpack_from("<HH", raw, i * 4)
            if item:
                out[item] = qty ^ key
        return out

    def bag_order(self, pocket: str = "items") -> list[int]:
        off, cap = SB1_POCKETS[pocket]
        raw = self.emu.read(self.sb1() + off, cap * 4)
        return [struct.unpack_from("<H", raw, i * 4)[0] for i in range(cap)
                if struct.unpack_from("<H", raw, i * 4)[0]]

    def has_item(self, item: str | int) -> int:
        iid = const(item) if isinstance(item, str) else item
        for pocket in SB1_POCKETS:
            q = self.bag(pocket).get(iid)
            if q:
                return q
        return 0

    # -- battle -----------------------------------------------------------------
    def battle_type(self) -> int:
        return self.emu.u32(S["gBattleTypeFlags"])

    def battle_mons(self) -> list[BattleMon]:
        raw = self.emu.read(S["gBattleMons"], 0x58 * 4)
        return [decode_battle_mon(raw[i * 0x58:(i + 1) * 0x58]) for i in range(4)]

    def battlers_count(self) -> int:
        return self.emu.u8(S["gBattlersCount"])

    def battler_party_index(self, battler: int) -> int:
        return self.emu.u16(S["gBattlerPartyIndexes"] + battler * 2)

    def battle_controller(self, battler: int = 0) -> str:
        return S.name_at(self.emu.u32(S["gBattlerControllerFuncs"] + battler * 4))

    def battle_outcome(self) -> int:
        return self.emu.u8(S["gBattleOutcome"])

    # -- text -------------------------------------------------------------------
    def string_var4(self) -> str:
        return decode_text(self.emu.read(S["gStringVar4"], 400))

    def battle_text(self) -> str:
        return decode_text(self.emu.read(S["gDisplayedStringBattle"], 300))

    def summary(self) -> str:
        g, n = self.location()
        x, y = self.pos()
        return (f"{self.map_id()} ({g}.{n}) @({x},{y}) badges={self.badges()} "
                f"mode={self.mode()}")
