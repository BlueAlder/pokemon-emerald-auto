"""Turn Emerald's raw memory into a compact, model-readable snapshot.

The snapshot is what we hand to TypeSafe as `state`. It is deliberately small
and named: every field is something a decision actually depends on. Raw
addresses, encryption, and struct layout stay behind this boundary.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .bridge import Bridge
from . import memory as mem

log = logging.getLogger("pokeauto.observer")

BATTLER_PLAYER = 0
BATTLER_ENEMY = 1


@dataclass
class Snapshot:
    frame: int
    in_battle: bool
    player_name: str = ""
    play_time: str = ""
    map_group: int = 0
    map_num: int = 0
    x: int = 0
    y: int = 0
    badges: list[str] = field(default_factory=list)
    party: list[mem.Mon] = field(default_factory=list)
    active: mem.Mon | None = None
    opponent: mem.Mon | None = None
    battle_text: str = ""
    overworld_text: str = ""

    @property
    def location_key(self) -> tuple[int, int, int, int]:
        return (self.map_group, self.map_num, self.x, self.y)

    @property
    def healthy_party(self) -> list[mem.Mon]:
        return [m for m in self.party if not m.fainted]

    def battle_signature(self) -> tuple:
        """Identity of a battle 'moment'. Stable == game waiting on us."""
        if not self.in_battle or not self.active or not self.opponent:
            return ("no-battle",)
        return (
            self.active.species_id, self.active.hp, self.active.status,
            self.opponent.species_id, self.opponent.hp,
            tuple(m.pp for m in self.active.moves),
        )


class Observer:
    """Reads and decodes game state. Owns ROM-table calibration and caching."""

    def __init__(self, bridge: Bridge, addresses: mem.Addresses,
                 cache_path: Path | None = None):
        self.bridge = bridge
        self.addr = addresses
        self.cache_path = cache_path
        self._sb1: int | None = None
        self._sb2: int | None = None
        self.rom: mem.RomData | None = None

    # -- setup -------------------------------------------------------------

    def calibrate(self, force: bool = False) -> mem.RomTables:
        """Locate ROM data tables, using a cached result when possible."""
        info = self.bridge.info()
        cached = self._read_cache(info.game_code) if not force else None
        if cached:
            tables = mem.RomTables(**{k: int(v, 0) for k, v in cached.items()})
        else:
            rom = self._read_rom_window()
            tables = mem.calibrate_rom_tables(rom, self.addr.rom_scan_start)
            self._write_cache(info.game_code, tables)
        self.rom = mem.RomData(self.bridge, tables)
        return tables

    def _read_rom_window(self, chunk: int = 0x8000) -> bytes:
        start, total = self.addr.rom_scan_start, self.addr.rom_scan_len
        out = bytearray()
        for off in range(0, total, chunk):
            out += self.bridge.read(start + off, min(chunk, total - off))
        return bytes(out)

    def _read_cache(self, game_code: str) -> dict | None:
        if not self.cache_path or not self.cache_path.exists():
            return None
        try:
            return json.loads(self.cache_path.read_text()).get(game_code)
        except (OSError, json.JSONDecodeError):
            return None

    def _write_cache(self, game_code: str, tables: mem.RomTables) -> None:
        if not self.cache_path:
            return
        data = {}
        if self.cache_path.exists():
            try:
                data = json.loads(self.cache_path.read_text())
            except (OSError, json.JSONDecodeError):
                data = {}
        data[game_code] = tables.to_json()
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(data, indent=2) + "\n")

    def _save_blocks(self) -> tuple[int, int]:
        """Current SaveBlock1/SaveBlock2 addresses.

        These MOVE. Emerald relocates its save blocks (observed shifting by
        -0x4C between a fresh boot and later play), so caching them once and
        reading forever from the old address yields garbage -- a dead giveaway
        being map 0.0 at position (0,0). snapshot() re-validates them on every
        read; this only seeds the first value.
        """
        if self._sb1 is None or self._sb2 is None:
            blobs = self.bridge.read_many([
                (self.addr.save_block1_ptr, 4),
                (self.addr.save_block2_ptr, 4),
            ])
            self._sb1 = int.from_bytes(blobs[0], "little")
            self._sb2 = int.from_bytes(blobs[1], "little")
        return self._sb1, self._sb2

    def _save_block_dependent_ranges(self, sb1: int, sb2: int) -> list[tuple[int, int]]:
        a = self.addr
        return [
            (sb1 + a.sb1_pos_x, 6),                            # x, y, group, num
            (sb1 + a.sb1_flags + mem.FLAG_BADGE01 // 8, 2),    # badge flags
            (sb2 + a.sb2_player_name, 8),
            (sb2 + a.sb2_play_time_hours, 4),
        ]

    # -- observation -------------------------------------------------------

    def snapshot(self) -> Snapshot:
        if self.rom is None:
            raise RuntimeError("call calibrate() before snapshot()")
        sb1, sb2 = self._save_blocks()
        a = self.addr

        blobs = self.bridge.read_many([
            (a.player_party_count, 1),                 # 0
            (a.player_party, 6 * mem.PARTY_MON_SIZE),  # 1
            (a.battle_type_flags, 4),                  # 2
            (a.battle_outcome, 1),                     # 3
            (a.battle_mons, 2 * mem.BATTLE_MON_SIZE),  # 4
            (a.string_var4, 160),                      # 5
            (a.battle_string, 160),                    # 6
            (a.save_block1_ptr, 4),                    # 7  re-read every time
            (a.save_block2_ptr, 4),                    # 8
        ] + self._save_block_dependent_ranges(sb1, sb2))   # 9, 10, 11, 12

        # The save blocks may have been relocated since the last snapshot. If
        # so, everything we just read from them is garbage -- re-read it from
        # the new addresses. Steady state costs nothing; only a relocation
        # pays for a second round trip.
        fresh_sb1 = int.from_bytes(blobs[7], "little")
        fresh_sb2 = int.from_bytes(blobs[8], "little")
        if (fresh_sb1, fresh_sb2) != (sb1, sb2):
            log.info("save blocks relocated: SaveBlock1 0x%08X -> 0x%08X, "
                     "SaveBlock2 0x%08X -> 0x%08X",
                     sb1, fresh_sb1, sb2, fresh_sb2)
            self._sb1, self._sb2 = sb1, sb2 = fresh_sb1, fresh_sb2
            blobs[9:] = self.bridge.read_many(
                self._save_block_dependent_ranges(sb1, sb2))

        pos_raw, badge_raw, name_raw, time_raw = blobs[9], blobs[10], blobs[11], blobs[12]

        frame = self.bridge.frame()
        party = self._decode_party(blobs[0], blobs[1])

        flags = int.from_bytes(blobs[2], "little")
        outcome = blobs[3][0] if blobs[3] else 0
        active, opponent = self._decode_battlers(blobs[4])
        in_battle = bool(
            flags and outcome == 0 and active and opponent
            and active.species_id and opponent.species_id
            and active.max_hp and opponent.max_hp
        )

        x = int.from_bytes(pos_raw[0:2], "little", signed=True)
        y = int.from_bytes(pos_raw[2:4], "little", signed=True)

        return Snapshot(
            frame=frame,
            in_battle=in_battle,
            player_name=mem.decode_text(name_raw),
            play_time=self._play_time(time_raw),
            map_group=pos_raw[4], map_num=pos_raw[5], x=x, y=y,
            badges=self._decode_badges(badge_raw),
            party=party,
            active=active if in_battle else (party[0] if party else None),
            opponent=opponent if in_battle else None,
            battle_text=self._text(blobs[6]),
            overworld_text=self._text(blobs[5]),
        )

    # -- decoding helpers --------------------------------------------------

    def _decode_party(self, count_raw: bytes, party_raw: bytes) -> list[mem.Mon]:
        count = min(count_raw[0] if count_raw else 0, 6)
        mons: list[mem.Mon] = []
        raws = []
        for i in range(count):
            chunk = party_raw[i * mem.PARTY_MON_SIZE:(i + 1) * mem.PARTY_MON_SIZE]
            if len(chunk) < mem.PARTY_MON_SIZE:
                break
            raws.append(mem.decrypt_party_mon(chunk))

        self.rom.prefetch_moves([m for r in raws for m in r["moves"]])
        for i, r in enumerate(raws):
            if r["species_id"] == 0:
                continue
            mons.append(self._to_mon(i, r, party_style=True))
        return mons

    def _decode_battlers(self, raw: bytes) -> tuple[mem.Mon | None, mem.Mon | None]:
        out: list[mem.Mon | None] = [None, None]
        decoded = []
        for i in (BATTLER_PLAYER, BATTLER_ENEMY):
            chunk = raw[i * mem.BATTLE_MON_SIZE:(i + 1) * mem.BATTLE_MON_SIZE]
            decoded.append(mem.decode_battle_mon(chunk) if len(chunk) == mem.BATTLE_MON_SIZE else None)

        self.rom.prefetch_moves(
            [m for d in decoded if d for m in d["moves"]]
        )
        for i, d in enumerate(decoded):
            if d and d["species_id"]:
                out[i] = self._to_mon(i, d, party_style=False)
        return out[0], out[1]

    def _to_mon(self, slot: int, r: dict, party_style: bool) -> mem.Mon:
        moves: list[mem.Move] = []
        for mi, move_id in enumerate(r["moves"]):
            if not move_id:
                continue
            power, type_id, accuracy = self.rom.move_stats(move_id)
            moves.append(mem.Move(
                slot=mi, id=move_id, name=self.rom.move_name(move_id),
                pp=r["pp"][mi] if mi < len(r["pp"]) else 0,
                max_pp=r["pp"][mi] if mi < len(r["pp"]) else 0,
                power=power, type_id=type_id, accuracy=accuracy,
            ))
        species = self.rom.species_name(r["species_id"])
        return mem.Mon(
            slot=slot, species_id=r["species_id"], species=species,
            nickname=r.get("nickname") or species,
            level=r["level"], hp=r["hp"], max_hp=r["max_hp"],
            status=mem.status_label(r["status1"]),
            moves=moves,
            type1=r.get("type1", 0), type2=r.get("type2", 0),
            attack=r["attack"], defense=r["defense"], speed=r["speed"],
            sp_attack=r["sp_attack"], sp_defense=r["sp_defense"],
        )

    @staticmethod
    def _decode_badges(raw: bytes) -> list[str]:
        """Decode the 8 badge flags.

        `raw` is the 2 bytes at the byte containing FLAG_BADGE01. The eight
        badges are consecutive flags starting at bit FLAG_BADGE01 % 8, which
        spans a byte boundary, hence reading two.
        """
        if len(raw) < 2:
            return []
        value = int.from_bytes(raw, "little")
        first_bit = mem.FLAG_BADGE01 % 8
        return [name for i, name in enumerate(mem.BADGE_NAMES)
                if value >> (first_bit + i) & 1]

    @staticmethod
    def _play_time(raw: bytes) -> str:
        if len(raw) < 4:
            return "0:00"
        hours = int.from_bytes(raw[0:2], "little")
        minutes = raw[2]
        return f"{hours}:{minutes:02d}"

    @staticmethod
    def _text(raw: bytes) -> str:
        text = mem.decode_text(raw).replace("\n", " ").strip()
        return text if mem.looks_like_text(text, min_len=4) else ""
