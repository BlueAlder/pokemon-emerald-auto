"""End-to-end test against a simulated Emerald.

Stands up a fake mGBA speaking the bridge protocol over a real socket, backed
by a synthetic ROM and EWRAM laid out the way the game lays them out. Exercises
the full stack — bridge, ROM calibration, observer, brain, agent, menu
navigation — with no emulator, no ROM, and no API key.
"""

from __future__ import annotations

import socket
import struct
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pokeauto import memory as mem
from pokeauto.agent import Agent
from pokeauto.brain import Brain
from pokeauto.bridge import Bridge
from pokeauto.observer import Observer

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_offline import build_party_mon  # reuse the encryptor

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"  ok   {label}: {got!r}")
    else:
        failures.append(label)
        print(f"  FAIL {label}: got {got!r}, expected {expected!r}")


def check_true(label, cond):
    check(label, bool(cond), True)


# -- synthetic cartridge ---------------------------------------------------

ROM_BASE = 0x08300000
ROM_LEN = 0x00100000
SPECIES_OFF, MOVES_OFF, BATTLE_MOVES_OFF = 0x18000, 0x19000, 0x1C000

# Internal Gen 3 species ids, NOT National Dex numbers (Hoenn starts at 277).
SPECIES = {1: "BULBASAUR", 4: "CHARMANDER", 277: "TREECKO", 280: "TORCHIC",
           281: "COMBUSKEN", 283: "MUDKIP", 284: "MARSHTOMP", 295: "LOTAD"}
MOVES = {1: "POUND", 2: "KARATE CHOP", 3: "DOUBLESLAP", 33: "TACKLE",
         43: "LEER", 45: "GROWL", 52: "EMBER", 55: "WATER GUN",
         85: "THUNDERBOLT", 189: "MUD-SLAP"}
T = mem._T
MOVE_STATS = {  # power, type, accuracy, pp
    1: (40, T["NORMAL"], 100, 35), 2: (50, T["FIGHTING"], 100, 25),
    3: (15, T["NORMAL"], 85, 10), 33: (35, T["NORMAL"], 95, 35),
    43: (0, T["NORMAL"], 100, 30), 45: (0, T["NORMAL"], 100, 40),
    52: (40, T["FIRE"], 100, 25), 55: (40, T["WATER"], 100, 25),
    85: (95, T["ELECTRIC"], 100, 15), 189: (20, T["GROUND"], 100, 10),
}


def build_rom() -> bytearray:
    rom = bytearray(ROM_LEN)
    for sid, name in SPECIES.items():
        off = SPECIES_OFF + sid * mem.SPECIES_NAME_LEN
        raw = mem.encode_text(name) + b"\xff"
        rom[off:off + len(raw)] = raw
    for mid, name in MOVES.items():
        off = MOVES_OFF + mid * mem.MOVE_NAME_LEN
        raw = mem.encode_text(name) + b"\xff"
        rom[off:off + len(raw)] = raw
    for mid, (power, type_id, acc, pp) in MOVE_STATS.items():
        off = BATTLE_MOVES_OFF + mid * mem.BATTLE_MOVE_SIZE
        rom[off + 1], rom[off + 2], rom[off + 3], rom[off + 4] = power, type_id, acc, pp
    return rom


SB1, SB2 = 0x02025734, 0x02024EA4
A = mem.Addresses()


def build_ewram(in_battle: bool, sb1: int = SB1, sb2: int = SB2) -> dict[int, bytes]:
    """Sparse EWRAM/IWRAM: address -> bytes."""
    blocks: dict[int, bytes] = {}
    blocks[A.save_block1_ptr] = struct.pack("<I", sb1)
    blocks[A.save_block2_ptr] = struct.pack("<I", sb2)

    # Party: a Combusken and a Marshtomp.
    party = bytearray()
    party += build_party_mon(0x12345678, 0xCAFEBABE, 281, 20, 60, 60,
                             [33, 52, 43, 0], [35, 25, 30, 0], "Blaze")
    party += build_party_mon(0xABCDEF01, 0xCAFEBABE, 284, 22, 70, 70,
                             [33, 55, 189, 0], [35, 25, 10, 0], "Sludge")
    party += bytes(4 * mem.PARTY_MON_SIZE)
    blocks[A.player_party_count] = bytes([2])
    blocks[A.player_party] = bytes(party)

    # Battle state.
    def battle_mon(species, level, hp, max_hp, t1, t2, moves, pp, name):
        raw = bytearray(mem.BATTLE_MON_SIZE)
        struct.pack_into("<6H", raw, 0, species, 45, 35, 40, 40, 35)
        struct.pack_into("<4H", raw, 0x0C, *moves)
        raw[0x21], raw[0x22] = t1, t2
        raw[0x24:0x28] = bytes(pp)
        struct.pack_into("<H", raw, 0x28, hp)
        raw[0x2A] = level
        struct.pack_into("<HH", raw, 0x2C, max_hp, 0)
        enc = mem.encode_text(name) + b"\xff"
        raw[0x30:0x30 + len(enc)] = enc
        return bytes(raw)

    if in_battle:
        mons = battle_mon(281, 20, 60, 60, T["FIRE"], T["FIGHTING"],
                          [33, 52, 43, 0], [35, 25, 30, 0], "Blaze")
        mons += battle_mon(295, 18, 48, 48, T["WATER"], T["GRASS"],
                           [33, 45, 0, 0], [35, 40, 0, 0], "Lotad")
        blocks[A.battle_type_flags] = struct.pack("<I", 0x01)
    else:
        mons = bytes(2 * mem.BATTLE_MON_SIZE)
        blocks[A.battle_type_flags] = struct.pack("<I", 0)
    blocks[A.battle_mons] = mons
    blocks[A.battle_outcome] = bytes([0])

    blocks[sb1 + A.sb1_pos_x] = struct.pack("<hhBB", 12, 7, 3, 5)
    # Badges are consecutive flags from FLAG_BADGE01 (0x867), whose bit offset
    # within its byte is 7 -- so Stone is bit 7 and Knuckle is bit 8 of the
    # little-endian word at that byte.
    blocks[sb1 + A.sb1_flags + mem.FLAG_BADGE01 // 8] = bytes([0x80, 0x01])
    blocks[sb2 + A.sb2_player_name] = mem.encode_text("SAM") + b"\xff"
    blocks[sb2 + A.sb2_play_time_hours] = struct.pack("<HBB", 4, 37, 0)
    blocks[A.string_var4] = mem.encode_text("PROF. BIRCH: Please help me!") + b"\xff"
    blocks[A.battle_string] = mem.encode_text("Wild LOTAD appeared!") + b"\xff"
    return blocks


class FakeEmerald(threading.Thread):
    daemon = True

    def __init__(self, in_battle: bool = False):
        super().__init__()
        self.in_battle = in_battle
        self.sb1, self.sb2 = SB1, SB2
        self.rom = build_rom()
        self.blocks = build_ewram(in_battle)

        self.frame = 5000
        self.presses: list[tuple[int, int, int]] = []
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]

    def relocate_save_blocks(self, delta: int = -0x4C) -> tuple[int, int]:
        """Emerald moves its save blocks; reproduce that."""
        self.sb1, self.sb2 = SB1 + delta, SB2 + delta
        self.blocks = build_ewram(self.in_battle, self.sb1, self.sb2)
        return self.sb1, self.sb2

    def read(self, addr: int, length: int) -> bytes:
        if ROM_BASE <= addr < ROM_BASE + ROM_LEN:
            off = addr - ROM_BASE
            return bytes(self.rom[off:off + length])
        out = bytearray(length)
        for base, data in self.blocks.items():
            lo, hi = max(addr, base), min(addr + length, base + len(data))
            if lo < hi:
                out[lo - addr:hi - addr] = data[lo - base:hi - base]
        return bytes(out)

    def run(self):
        conn, _ = self.srv.accept()
        buf = b""
        with conn:
            while True:
                data = conn.recv(65536)
                if not data:
                    return
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    conn.sendall(self.handle(line.decode()).encode() + b"\n")

    def handle(self, line: str) -> str:
        cmd, _, rest = line.partition(" ")
        self.frame += 3
        if cmd == "PING":
            return "PONG"
        if cmd == "INFO":
            return f"BPEE 16777216 {self.frame} POKEMON EMER"
        if cmd == "FRAME":
            return str(self.frame)
        if cmd == "READ":
            a, n = rest.split()
            return self.read(int(a, 16), int(n)).hex()
        if cmd == "READM":
            parts = []
            for chunk in rest.split(","):
                a, n = chunk.split(":")
                parts.append(self.read(int(a, 16), int(n)).hex())
            return "|".join(parts)
        if cmd == "PRESS":
            m, h, g = (int(v) for v in rest.split())
            self.presses.append((m, h, g))
            return f"OK {self.frame}"
        if cmd in ("HOLD", "IDLE"):
            self.presses.append(tuple(int(v) for v in rest.split()))
            return f"OK {self.frame}"
        return "ERR unknown"


# -- run -------------------------------------------------------------------

print("1. ROM table calibration against a synthetic cartridge")
server = FakeEmerald(in_battle=False)
server.start()
bridge = Bridge(port=server.port).connect(retries=5, delay=0.2)
observer = Observer(bridge, mem.Addresses(), cache_path=None)
tables = observer.calibrate(force=True)
check("species table found", tables.species_names, ROM_BASE + SPECIES_OFF)
check("move name table found", tables.move_names, ROM_BASE + MOVES_OFF)
check("move stat table found", tables.battle_moves, ROM_BASE + BATTLE_MOVES_OFF)
check("species lookup", observer.rom.species_name(283), "MUDKIP")
check("reverse lookup by name", observer.rom.find_species("LOTAD"), 295)
check("move lookup", observer.rom.move_name(85), "THUNDERBOLT")
check("move stats", observer.rom.move_stats(85)[:2], (95, T["ELECTRIC"]))

print("\n2. overworld snapshot")
snap = observer.snapshot()
check("player name", snap.player_name, "SAM")
check("play time", snap.play_time, "4:37")
check("position", (snap.map_group, snap.map_num, snap.x, snap.y), (3, 5, 12, 7))
check("badges", snap.badges, ["Stone", "Knuckle"])
check("not in battle", snap.in_battle, False)
check("party size", len(snap.party), 2)
check("first mon", (snap.party[0].nickname, snap.party[0].level,
                    snap.party[0].hp, snap.party[0].max_hp), ("Blaze", 20, 60, 60))
check("moves decoded",
      [m.name for m in snap.party[0].moves], ["TACKLE", "EMBER", "LEER"])
check("dialogue decoded", snap.overworld_text, "PROF. BIRCH: Please help me!")

print("\n3. battle snapshot and decision")
bridge.close()
server2 = FakeEmerald(in_battle=True)
server2.start()
bridge2 = Bridge(port=server2.port).connect(retries=5, delay=0.2)
observer2 = Observer(bridge2, mem.Addresses(), cache_path=None)
observer2.calibrate(force=True)
snap2 = observer2.snapshot()
check("in battle", snap2.in_battle, True)
check("active", snap2.active.nickname, "Blaze")
check("opponent", (snap2.opponent.nickname, snap2.opponent.level), ("Lotad", 18))
check("opponent typing",
      (mem.TYPE_NAMES[snap2.opponent.type1], mem.TYPE_NAMES[snap2.opponent.type2]),
      ("Water", "Grass"))
check("battle text", snap2.battle_text, "Wild LOTAD appeared!")
check_true("battle signature is stable across reads",
           snap2.battle_signature() == observer2.snapshot().battle_signature())

print("\n4. agent drives menus (offline brain, no API calls)")
brain = Brain(offline=True)
agent = Agent(bridge2, observer2, brain)
from pokeauto.bridge import mask_for

# Blaze is Fire/Fighting facing Water/Grass Lotad. Ember is only neutral
# (0.5x into Water, 2x into Grass) but gets STAB and more power than Tackle,
# so the correct pick is Ember -- move slot 1, reached with a RIGHT press.
server2.presses.clear()
agent._battle_step(snap2)
masks = [p[0] for p in server2.presses]
check("battle step picks Ember (slot 1)", masks,
      [mask_for("B"), mask_for("UP"), mask_for("LEFT"), mask_for("A"),
       mask_for("UP"), mask_for("LEFT"), mask_for("RIGHT"), mask_for("A")])

server2.presses.clear()
agent._use_move(0)
check("menu sequence for move slot 0", [p[0] for p in server2.presses],
      [mask_for("B"), mask_for("UP"), mask_for("LEFT"), mask_for("A"),
       mask_for("UP"), mask_for("LEFT"), mask_for("A")])

server2.presses.clear()
agent._use_move(3)
masks = [p[0] for p in server2.presses]
check("menu sequence for move slot 3", masks,
      [mask_for("B"), mask_for("UP"), mask_for("LEFT"), mask_for("A"),
       mask_for("UP"), mask_for("LEFT"), mask_for("RIGHT"), mask_for("DOWN"),
       mask_for("A")])

server2.presses.clear()
agent._flee()
check("RUN is bottom-right", [p[0] for p in server2.presses],
      [mask_for("B"), mask_for("UP"), mask_for("LEFT"),
       mask_for("RIGHT"), mask_for("DOWN"), mask_for("A")])

print("\n5. call gating: an unchanged battle costs nothing")
agent._last_battle_sig = snap2.battle_signature()
before = brain.calls
server2.presses.clear()
agent._battle_step(snap2)
check("no decision made on identical signature", brain.calls, before)
check("advanced with a single A press", [p[0] for p in server2.presses],
      [mask_for("A")])

print("\n6. bounded run loop")
agent2 = Agent(bridge2, observer2, Brain(offline=True))
stats = agent2.run(max_steps=6)
check("loop completed requested steps", stats.steps, 6)
check_true("battle was detected", stats.battles_seen >= 1)

print("\n7. regression: a stuck agent must not press A forever")
# The failure this guards against: gStringVar4 held stale text, so the model
# was told a text box was open, said "press A", A changed nothing, and the
# loop repeated indefinitely. FakeEmerald never changes position, so this is
# exactly that situation -- with a brain that always votes to interact.
from pokeauto.brain import DIRECTIONS, OverworldDecision
from pokeauto.bridge import KEY_BITS

class AlwaysInteractBrain:
    calls = 0
    input_tokens = output_tokens = 0
    offline = False

    def decide_overworld(self, snap, objective, recent, blocked, **kwargs):
        self.calls += 1
        return OverworldDecision(
            "interact", direction="up", confidence=0.9,
            interact_probability=0.99,
            probabilities={d: 0.25 for d in DIRECTIONS}, source="stub")

    @staticmethod
    def _overworld_heuristic(blocked):
        return OverworldDecision("interact", direction="up", source="stub")

server3 = FakeEmerald(in_battle=False)
server3.start()
bridge3 = Bridge(port=server3.port).connect(retries=5, delay=0.2)
observer3 = Observer(bridge3, mem.Addresses(), cache_path=None)
observer3.calibrate(force=True)

stub = AlwaysInteractBrain()
agent3 = Agent(bridge3, observer3, stub)
server3.presses.clear()
agent3.run(max_steps=30)

bit_to_name = {v: k for k, v in KEY_BITS.items()}
pressed = [bit_to_name[m.bit_length() - 1] for m, *_ in server3.presses if m]
a_presses = pressed.count("A")
walks = [p for p in pressed if p in ("UP", "DOWN", "LEFT", "RIGHT")]

print(f"       over 30 steps: {a_presses} A-presses, {len(walks)} movement presses")
check_true("agent stopped mashing A and started walking", len(walks) > 0)
check_true("A-presses are bounded well below one per step", a_presses < 30)
check_true("tried more than one direction", len(set(walks)) > 1)

longest = cur = 0
for name in pressed:
    cur = cur + 1 if name == "A" else 0
    longest = max(longest, cur)
print(f"       longest unbroken run of A-presses: {longest}")
check_true("no unbounded A-press streak", longest <= Agent.MAX_CONSECUTIVE_INTERACTS + 1)

bridge3.close()
bridge2.close()

print("\n8. regression: save blocks that move must be followed")
# Emerald relocates SaveBlock1/SaveBlock2 (observed shifting by -0x4C on a
# real cartridge). Caching the pointer once made every later read garbage,
# showing up as map 0.0 at position (0,0).
server4 = FakeEmerald(in_battle=False)
server4.start()
bridge4 = Bridge(port=server4.port).connect(retries=5, delay=0.2)
observer4 = Observer(bridge4, mem.Addresses(), cache_path=None)
observer4.calibrate(force=True)

before = observer4.snapshot()
check("position before relocation", (before.map_group, before.map_num,
                                     before.x, before.y), (3, 5, 12, 7))
check("player before relocation", before.player_name, "SAM")

new_sb1, new_sb2 = server4.relocate_save_blocks()
after = observer4.snapshot()
check("position survives relocation", (after.map_group, after.map_num,
                                       after.x, after.y), (3, 5, 12, 7))
check("player survives relocation", after.player_name, "SAM")
check("badges survive relocation", after.badges, ["Stone", "Knuckle"])
check("observer followed the pointer", observer4._sb1, new_sb1)
check_true("did not read garbage (map 0.0 at 0,0 was the symptom)",
           (after.map_group, after.map_num, after.x, after.y) != (0, 0, 0, 0))
bridge4.close()

print("\n9. regression: being stuck must not burn API calls")
# 166 calls in 1.7 minutes came from re-asking every step while pinned in
# place. A stuck agent should probe in code, not pay for the same answer.
class CountingBrain(AlwaysInteractBrain):
    def decide_overworld(self, snap, objective, recent, blocked, **kwargs):
        self.calls += 1
        return OverworldDecision(
            "move", direction="up", confidence=0.5,
            probabilities={d: 0.25 for d in DIRECTIONS}, source="stub")

server5 = FakeEmerald(in_battle=False)   # position never changes: always stuck
server5.start()
bridge5 = Bridge(port=server5.port).connect(retries=5, delay=0.2)
observer5 = Observer(bridge5, mem.Addresses(), cache_path=None)
observer5.calibrate(force=True)

counter = CountingBrain()
agent5 = Agent(bridge5, observer5, counter)
agent5.run(max_steps=40)
print(f"       40 steps while permanently stuck -> {counter.calls} API call(s)")
check_true("stuck agent does not call once per step", counter.calls < 10)
check_true("but it does still make some decisions", counter.calls >= 1)
bridge5.close()

print("\n10. regression: a dead-end corridor must not cause oscillation")
# The observed bug: model says "up", agent walks (9,4)->(9,3), "up" is a wall
# there, the probe picks "down" as the first untried direction, walks straight
# back to (9,4), and the pair repeats forever. Per-tile wall memory plus
# refusing to prefer the reverse direction should break it.

class CorridorEmerald(FakeEmerald):
    """(9,3) is a dead end; the only ways on from (9,4) are left and right."""

    WALKABLE = {(9, 4), (9, 3), (8, 4), (7, 4), (10, 4), (11, 4)}
    DELTA = {64: (0, -1), 128: (0, 1), 32: (-1, 0), 16: (1, 0)}  # up down left right

    def __init__(self):
        super().__init__(in_battle=False)
        self.x, self.y = 9, 4
        self.visited = {(9, 4)}
        self._write_position()

    def _write_position(self):
        self.blocks[self.sb1 + A.sb1_pos_x] = struct.pack(
            "<hhBB", self.x, self.y, 1, 0)

    def handle(self, line: str) -> str:
        cmd, _, rest = line.partition(" ")
        if cmd == "HOLD":
            mask = int(rest.split()[0])
            dx, dy = self.DELTA.get(mask, (0, 0))
            target = (self.x + dx, self.y + dy)
            if target in self.WALKABLE:
                self.x, self.y = target
                self.visited.add(target)
                self._write_position()
            self._write_position()
        return super().handle(line)

server6 = CorridorEmerald()
server6.start()
bridge6 = Bridge(port=server6.port).connect(retries=5, delay=0.2)
observer6 = Observer(bridge6, mem.Addresses(), cache_path=None)
observer6.calibrate(force=True)

class UpOnlyBrain(AlwaysInteractBrain):
    """Stubbornly votes 'up' forever, like the real run did."""

    def decide_overworld(self, snap, objective, recent, blocked, **kwargs):
        self.calls += 1
        return OverworldDecision(
            "move", direction="up", confidence=0.4,
            probabilities={"up": 0.7, "down": 0.1, "left": 0.1, "right": 0.1},
            source="stub")

agent6 = Agent(bridge6, observer6, UpOnlyBrain())
agent6.run(max_steps=40)

print(f"       tiles reached: {sorted(server6.visited)}")
check_true("escaped the two-tile loop", len(server6.visited) > 2)
check_true("reached a tile beyond the corridor mouth",
           bool(server6.visited & {(8, 4), (10, 4), (7, 4), (11, 4)}))
check_true("learned that up is a wall at (9,3)",
           "up" in agent6._walls.get((1, 0, 9, 3), set()))
bridge6.close()

print("\n11. regression: an accidentally opened menu must be escaped with B")
# The observed bug: the agent pressed A on the bedroom PC, which opened a
# menu. In a menu the D-pad moves a cursor, not the player, so every direction
# read as a wall -- and the response was to press A again, navigating deeper.
# 262 A-presses over 450 steps, visiting the same tile 299 times.

class MenuEmerald(CorridorEmerald):
    """A PC at (9,4): pressing A opens a menu that only B closes."""

    def __init__(self):
        super().__init__()
        self.menu_depth = 0
        self.max_depth_seen = 0
        self.a_presses_in_menu = 0
        self.opens = 0

    PC_TILE = (9, 4)      # the only tile with something to open

    def handle(self, line: str) -> str:
        cmd, _, rest = line.partition(" ")
        if cmd == "PRESS":
            mask = int(rest.split()[0])
            if mask == 1:                              # A
                if self.menu_depth > 0:
                    self.menu_depth += 1               # digs deeper
                    self.a_presses_in_menu += 1
                elif (self.x, self.y) == self.PC_TILE:
                    self.menu_depth = 1
                    self.opens += 1
                self.max_depth_seen = max(self.max_depth_seen, self.menu_depth)
            elif mask == 2:                            # B
                self.menu_depth = max(0, self.menu_depth - 1)
        if cmd == "HOLD" and self.menu_depth > 0:
            # A menu swallows the D-pad: the player does not move.
            return FakeEmerald.handle(self, line)
        return super().handle(line)

server7 = MenuEmerald()
server7.start()
bridge7 = Bridge(port=server7.port).connect(retries=5, delay=0.2)
observer7 = Observer(bridge7, mem.Addresses(), cache_path=None)
observer7.calibrate(force=True)

class InteractBrain(AlwaysInteractBrain):
    """Votes to press A at first, exactly as the real model did at the PC."""

    def decide_overworld(self, snap, objective, recent, blocked, **kwargs):
        self.calls += 1
        return OverworldDecision(
            "interact", direction="up", confidence=0.8,
            interact_probability=0.95,
            probabilities={d: 0.25 for d in DIRECTIONS}, source="stub")

agent7 = Agent(bridge7, observer7, InteractBrain())
agent7.run(max_steps=40)

print(f"       menu opened {server7.opens}x, deepest {server7.max_depth_seen}, "
      f"final depth {server7.menu_depth}, "
      f"A-presses while already inside: {server7.a_presses_in_menu}")
check("ended outside the menu", server7.menu_depth, 0)
check_true("did not burrow deep into the menu", server7.max_depth_seen <= 2)
check_true("stopped confirming once inside", server7.a_presses_in_menu <= 2)
check_true("stopped re-opening the same menu", server7.opens <= 2)
check_true("marked the tile as do-not-interact",
           len(agent7._no_interact_at) >= 1)
bridge7.close()

print("\n" + "=" * 60)
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("integration: all checks passed")
