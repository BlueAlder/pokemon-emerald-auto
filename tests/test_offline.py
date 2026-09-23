"""Offline tests: no emulator, no ROM, no API key required.

Covers the parts that are easy to get subtly wrong — Gen 3 substructure
decryption, the damage model, and the bridge line protocol.
"""

from __future__ import annotations

import socket
import struct
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pokeauto import memory as mem
from pokeauto.bridge import Bridge, mask_for
from pokeauto.brain import Brain, estimate_damage, score_moves

failures: list[str] = []


def check(label: str, got, expected) -> None:
    if got == expected:
        print(f"  ok   {label}: {got!r}")
    else:
        failures.append(label)
        print(f"  FAIL {label}: got {got!r}, expected {expected!r}")


def check_true(label: str, cond: bool) -> None:
    check(label, bool(cond), True)


# -- 1. party decryption ---------------------------------------------------

def build_party_mon(personality: int, ot_id: int, species: int, level: int,
                    hp: int, max_hp: int, moves: list[int], pp: list[int],
                    nickname: str) -> bytes:
    """Encrypt a party Pokemon the way the game does, so we can decrypt it."""
    growth = struct.pack("<HHIBBH", species, 0, 1000, 0, 70, 0)
    attacks = struct.pack("<4H4B", *moves, *pp) + b"\x00" * 0
    attacks = attacks.ljust(12, b"\x00")
    evs = bytes(12)
    misc = bytes(12)

    order = mem._SUBSTRUCT_ORDER[personality % 24]
    parts = {"G": growth.ljust(12, b"\x00"), "A": attacks, "E": evs, "M": misc}
    plain = b"".join(parts[letter] for letter in order)

    key = personality ^ ot_id
    enc = bytearray(plain)
    for i in range(0, 48, 4):
        word = struct.unpack_from("<I", enc, i)[0] ^ key
        struct.pack_into("<I", enc, i, word)

    raw = bytearray(100)
    struct.pack_into("<II", raw, 0, personality, ot_id)
    raw[0x08:0x08 + len(mem.encode_text(nickname))] = mem.encode_text(nickname)
    raw[0x08 + len(nickname)] = 0xFF
    raw[0x20:0x50] = enc
    struct.pack_into("<I", raw, 0x50, 0x08)       # poisoned
    raw[0x54] = level
    struct.pack_into("<7H", raw, 0x56, hp, max_hp, 30, 25, 40, 35, 28)
    return bytes(raw)


print("1. Gen 3 party substructure decryption")
# Exercise several personalities so every substructure ordering gets hit.
for personality in (0x12345678, 0xABCDEF01, 0x00000000, 0x24681357, 0xFFFFFFFF):
    raw = build_party_mon(personality, 0xDEADBEEF, species=258, level=17,
                          hp=41, max_hp=53, moves=[33, 45, 55, 0],
                          pp=[35, 40, 25, 0], nickname="Mudkip")
    got = mem.decrypt_party_mon(raw)
    ok = (got["species_id"] == 258 and got["level"] == 17 and got["hp"] == 41
          and got["max_hp"] == 53 and got["moves"] == [33, 45, 55, 0]
          and got["pp"] == [35, 40, 25, 0] and got["nickname"] == "Mudkip")
    check_true(f"personality 0x{personality:08X} (order "
               f"{mem._SUBSTRUCT_ORDER[personality % 24]})", ok)
check("status decoded", mem.status_label(
    mem.decrypt_party_mon(raw)["status1"]), "poisoned")

# -- 2. battle mon decoding ------------------------------------------------

print("\n2. gBattleMons decoding")
braw = bytearray(mem.BATTLE_MON_SIZE)
struct.pack_into("<6H", braw, 0, 260, 60, 55, 50, 48, 52)   # Marshtomp-ish
struct.pack_into("<4H", braw, 0x0C, 33, 189, 55, 0)
braw[0x20] = 3
braw[0x21], braw[0x22] = mem._T["WATER"], mem._T["GROUND"]
braw[0x24:0x28] = bytes([35, 10, 25, 0])
struct.pack_into("<H", braw, 0x28, 45)
braw[0x2A] = 25
struct.pack_into("<HH", braw, 0x2C, 70, 0)
braw[0x30:0x36] = mem.encode_text("Sludge")
braw[0x36] = 0xFF
struct.pack_into("<I", braw, 0x4C, 0x40)  # paralyzed
d = mem.decode_battle_mon(bytes(braw))
check("species", d["species_id"], 260)
check("hp/max", (d["hp"], d["max_hp"]), (45, 70))
check("level", d["level"], 25)
check("types", (mem.TYPE_NAMES[d["type1"]], mem.TYPE_NAMES[d["type2"]]),
      ("Water", "Ground"))
check("nickname", d["nickname"], "Sludge")
check("status", mem.status_label(d["status1"]), "paralyzed")
check("pp", d["pp"], [35, 10, 25, 0])

# -- 3. damage model -------------------------------------------------------

print("\n3. damage estimation and move scoring")
T = mem._T
combusken = mem.Mon(slot=0, species_id=256, species="COMBUSKEN",
                    nickname="Blaze", level=20, hp=60, max_hp=60, status=None,
                    type1=T["FIRE"], type2=T["FIGHTING"], attack=45,
                    defense=35, speed=40, sp_attack=40, sp_defense=35,
                    moves=[
                        mem.Move(0, 33, "TACKLE", 35, 35, 35, T["NORMAL"], 95),
                        mem.Move(1, 52, "EMBER", 25, 25, 40, T["FIRE"], 100),
                        mem.Move(2, 43, "LEER", 30, 30, 0, T["NORMAL"], 100),
                    ])
lotad = mem.Mon(slot=0, species_id=270, species="LOTAD", nickname="Lotad",
                level=18, hp=48, max_hp=48, status=None,
                type1=T["WATER"], type2=T["GRASS"], attack=30, defense=30,
                speed=30, sp_attack=40, sp_defense=40, moves=[])

scored = score_moves(combusken, lotad)
check("status move included", len(scored), 3)
ember = next(s for s in scored if s.move.name == "EMBER")
tackle = next(s for s in scored if s.move.name == "TACKLE")
leer = next(s for s in scored if s.move.name == "LEER")
# Lotad is Water/Grass: Fire is 0.5x into Water and 2x into Grass, so the
# two cancel to neutral. This is the dual-type multiplication working.
check("Ember is neutral vs Water/Grass", ember.effectiveness, 1.0)
check("Leer deals no damage", leer.damage_fraction, 0.0)
check_true("Ember out-damages Tackle (STAB + 2x)",
           ember.damage_fraction > tackle.damage_fraction)
print(f"       Ember: {ember.describe()}")
print(f"       Tackle: {tackle.describe()}")

seedot = mem.Mon(slot=0, species_id=273, species="SEEDOT", nickname="Seedot",
                 level=18, hp=48, max_hp=48, status=None,
                 type1=T["GRASS"], type2=T["GRASS"], attack=30, defense=30,
                 speed=30, sp_attack=30, sp_defense=30, moves=[])
vs_grass = score_moves(combusken, seedot)
check("Ember is 2x vs pure Grass",
      next(s for s in vs_grass if s.move.name == "EMBER").effectiveness, 2.0)
check("Tackle stays neutral vs pure Grass",
      next(s for s in vs_grass if s.move.name == "TACKLE").effectiveness, 1.0)

no_pp = mem.Move(3, 52, "EMBER", 0, 25, 40, T["FIRE"], 100)
check("zero-PP move is unusable", no_pp.usable, False)

print("\n4. offline heuristic decision")
decision = Brain._battle_heuristic(scored)
check("picks Ember", decision.move_name, "EMBER")
check("marked heuristic", decision.source, "heuristic")

# -- 5. bridge protocol ----------------------------------------------------

print("\n5. bridge line protocol (fake server standing in for mGBA)")


class FakeMGBA(threading.Thread):
    """Speaks the same protocol as lua/bridge.lua."""

    daemon = True

    def __init__(self):
        super().__init__()
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.frame = 1000
        self.presses: list[str] = []
        self.memory = bytes(range(256)) * 4

    def run(self) -> None:
        conn, _ = self.srv.accept()
        buf = b""
        with conn:
            while True:
                data = conn.recv(4096)
                if not data:
                    return
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    conn.sendall(self.handle(line.decode()).encode() + b"\n")

    def handle(self, line: str) -> str:
        cmd, _, rest = line.partition(" ")
        self.frame += 5
        if cmd == "PING":
            return "PONG"
        if cmd == "INFO":
            # Title last, and containing a space, exactly like the real ROM.
            return f"BPEE 16777216 {self.frame} POKEMON EMER"
        if cmd == "FRAME":
            return str(self.frame)
        if cmd == "READ":
            _addr, length = rest.split()
            return self.memory[:int(length)].hex()
        if cmd == "READM":
            out = []
            for chunk in rest.split(","):
                _a, length = chunk.split(":")
                out.append(self.memory[:int(length)].hex())
            return "|".join(out)
        if cmd in ("PRESS", "HOLD", "IDLE"):
            self.presses.append(line)
            return f"OK {self.frame}"
        return "ERR unknown"


server = FakeMGBA()
server.start()
bridge = Bridge(port=server.port).connect(retries=5, delay=0.2)
check("ping", bridge.ping(), True)
info = bridge.info()
check("game code", info.game_code, "BPEE")
check("title with space parsed", info.title, "POKEMON EMER")
check("rom size parsed", info.rom_size, 16777216)
check("read length", len(bridge.read(0x02000000, 16)), 16)
blobs = bridge.read_many([(0x02000000, 4), (0x02000004, 8), (0x03000000, 2)])
check("read_many returns all ranges", [len(b) for b in blobs], [4, 8, 2])
bridge.press("A")
bridge.hold("LEFT", frames=18)
check("button mask A", mask_for("A"), 1)
check("button mask LEFT", mask_for("LEFT"), 32)
check("mask A+START", mask_for("A", "START"), 1 | 8)
check_true("press reached the server", any(p.startswith("PRESS") for p in server.presses))
check_true("hold reached the server", any(p.startswith("HOLD") for p in server.presses))
bridge.close()

# -- 6. dotenv loader ------------------------------------------------------

print("\n6. .env loading")
import os
import tempfile

from pokeauto.env import find_dotenv, load_dotenv

with tempfile.TemporaryDirectory() as tmp:
    env_path = Path(tmp) / ".env"
    env_path.write_text(
        "# a comment line\n"
        "\n"
        'export QUOTED_KEY="value-in-quotes"\n'
        "export SINGLE_KEY='single-quoted'\n"
        "PLAIN_KEY=no-export-prefix\n"
        "  SPACED_KEY  =  padded-value  \n"
        "TRAILING_COMMENT=visible # hidden\n"
        "BASE64_KEY=abc==def\n"
        "ALREADY_SET=from-file\n"
        "NOT_A_PAIR\n"
    )
    os.environ["ALREADY_SET"] = "from-environment"
    loaded = load_dotenv(env_path)

    check("export prefix stripped", os.environ["QUOTED_KEY"], "value-in-quotes")
    check("single quotes stripped", os.environ["SINGLE_KEY"], "single-quoted")
    check("plain assignment", os.environ["PLAIN_KEY"], "no-export-prefix")
    check("whitespace trimmed", os.environ["SPACED_KEY"], "padded-value")
    check("trailing comment removed", os.environ["TRAILING_COMMENT"], "visible")
    check("value may contain '='", os.environ["BASE64_KEY"], "abc==def")
    check("real environment wins", os.environ["ALREADY_SET"], "from-environment")
    check_true("line without '=' ignored", "NOT_A_PAIR" not in loaded)
    check_true("comment not loaded", not any(k.startswith("#") for k in loaded))

    # override=True is the opt-in escape hatch.
    load_dotenv(env_path, override=True)
    check("override=True replaces", os.environ["ALREADY_SET"], "from-file")

    for key in ("QUOTED_KEY", "SINGLE_KEY", "PLAIN_KEY", "SPACED_KEY",
                "TRAILING_COMMENT", "BASE64_KEY", "ALREADY_SET"):
        os.environ.pop(key, None)

check("missing file is not an error", load_dotenv(Path("/nonexistent/.env")), {})
check_true("find_dotenv locates the project file",
           find_dotenv(Path(__file__).resolve().parent) is not None)

# -- 7. map + pathfinding --------------------------------------------------

print("\n7. collision grid and pathfinding")
from pokeauto.mapdata import MapObject, MapView, Warp

#     0123456789
#  0  ####.#####
#  1  #...#....#
#  2  #.#.#.##.#
#  3  #.#...##.#
#  4  #.#######.
#  5  #........#
#  6  ##########
ROOM = [
    "####.#####",   # (4,0) is a doorway in the top wall
    "#........#",
    "#.#.#.##.#",   # (6,2) is a wall tile that holds a warp
    "#.#...##.#",
    "#.#######.",
    "#.........",   # the east edge opens at (9,4) and (9,5)
    "##########",
]
collision = [1 if c == "#" else 0 for row in ROOM for c in row]
view = MapView(width=10, height=7, collision=collision,
               warps=[Warp(4, 0, 1, 5), Warp(6, 2, 1, 6)],
               objects=[MapObject(8, 1, 59, 1, 0)])

check("floor is passable", view.passable(1, 1), True)
check("wall is not", view.passable(0, 0), False)
check("outside the map is not", view.passable(-1, 3), False)
check("far corner is passable", view.passable(9, 4), True)

path = view.path_to((1, 1), (1, 5))
check("straight corridor length", len(path), 4)
check("straight corridor route", set(path), {"down"})

path = view.path_to((1, 1), (8, 3))
check_true("found a route around the walls", path is not None)
print(f"       (1,1) -> (8,3) in {len(path)} steps: {'/'.join(path)}")
# Verify the route by actually walking it.
x, y = 1, 1
for step in path:
    dx, dy = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}[step]
    x, y = x + dx, y + dy
    assert view.passable(x, y), f"route walks into a wall at ({x},{y})"
check("walking the route lands on the goal", (x, y), (8, 3))

check("unreachable goal returns None", view.path_to((1, 1), (0, 0)), None)
check("path to self is empty", view.path_to((1, 1), (1, 1)), [])

# A warp often sits inside a wall (a door is drawn as part of the wall), so an
# impassable tile must still be a legal goal as long as we can reach its edge.
check("(6,2) really is a wall", view.passable(6, 2), False)
check_true("can still path onto a warp tile inside a wall",
           view.path_to((1, 1), (6, 2)) is not None)
check_true("can path to the doorway", view.path_to((1, 1), (4, 0)) is not None)

reach = view.reachable((1, 1))
check_true("reachable set excludes walls",
           all(view.passable(*t) for t in reach))
check_true("reachable set is smaller than the whole map",
           len(reach) < view.width * view.height)

# An indoor map has no connections, so its walkable edge tiles lead nowhere.
# Offering them is what made the agent walk into the same wall forever.
check("indoor map offers no edge exits", view.edge_exits((1, 1)), {})

view.connections = {"east": (2, 7)}
edges = view.edge_exits((1, 1))
print(f"       edge exits once an east connection exists: {edges}")
check("east edge found at the nearest opening", edges.get("east"), (9, 4))
check_true("still no north exit — that direction has no connection",
           "north" not in edges)

# Routes must not clip a warp: stepping on a door teleports you away.
detour = view.path_to((1, 1), (8, 1), avoid={(4, 1)})
check_true("route avoids a tile marked avoid",
           detour is None or all(step != "x" for step in detour))
straight = view.path_to((1, 1), (8, 1))
check_true("avoiding a tile costs at least as many steps",
           detour is None or len(detour) >= len(straight))
check("a warp can still be its own goal (goal overrides avoid)",
      view.path_to((1, 1), (6, 2), avoid={(6, 2)}) is not None, True)

check("door_direction aims at a wall from the doorway",
      view.door_direction((4, 0)) in ("up", "left", "right"), True)

rendered = view.render((1, 1), {(4, 0): "W", (8, 1): "N"}, radius=9)
print("       rendered view:")
for line in rendered.splitlines():
    print("         " + line)
check_true("render marks the player", "P" in rendered)
check_true("render marks the warp", "W" in rendered)
check_true("render clips to a window",
           len(view.render((1, 1), {}, radius=2).splitlines()) <= 6)

# -- 8. world model --------------------------------------------------------

print("\n8. world model: frontiers, routing, blocked exits")
import tempfile as _tempfile

from pokeauto.world import World, map_key


class FakeNames:
    def lookup(self, group, num):
        return ({("0", "9"): "LITTLEROOT TOWN"}.get((str(group), str(num)),
                f"PLACE {group}.{num}"),
                "town" if group == 0 else "indoors")


class FakeView:
    def __init__(self, width, height, warps, connections=None, objects=()):
        self.width, self.height = width, height
        self.warps, self.objects = warps, list(objects)
        self.connections = connections or {}


class W:            # minimal stand-ins for mapdata's records
    def __init__(self, x, y, g, n):
        self.x, self.y, self.dest_group, self.dest_num = x, y, g, n


class O:
    def __init__(self, x, y):
        self.x, self.y = x, y


with _tempfile.TemporaryDirectory() as tmp:
    store = Path(tmp) / "world.json"
    world = World(store)
    names = FakeNames()

    # Town 0.9: a house (1.0), an unexplored house (1.2), a north connection.
    world.observe(0, 9, FakeView(21, 20,
                                 [W(5, 8, 1, 0), W(14, 8, 1, 2)],
                                 {"north": (0, 16)},
                                 [O(2, 10), O(11, 10)]), names)
    world.enter(0, 9)
    # House 1.0, entered, leads back to town and upstairs to 1.1 (unvisited).
    world.observe(1, 0, FakeView(12, 9, [W(8, 8, 0, 9), W(8, 2, 1, 1)]), names)
    world.enter(1, 0)

    check("both maps recorded", sorted(world.maps), ["0.9", "1.0"])
    check("town is visited", world.visited("0.9"), True)
    check("unentered house is not", world.visited("1.2"), False)

    fronts = world.frontiers("0.9")
    labels = {f.dest_key or f.direction for f in fronts}
    print("       frontiers from the town:")
    for f in fronts:
        print(f"         [{f.hops} hop] {f.label}")
    check_true("unexplored house 1.2 is a frontier", "1.2" in labels)
    check_true("unexplored upstairs 1.1 is a frontier", "1.1" in labels)
    check_true("north connection is a frontier", "0.16" in labels)
    check_true("the already-visited house is NOT a frontier",
               "1.0" not in {f.dest_key for f in fronts})

    here_now = [f for f in fronts if f.hops == 0]
    check_true("frontiers on this map are 0 hops", bool(here_now))
    upstairs = next(f for f in fronts if f.dest_key == "1.1")
    check("the upstairs frontier is one map away", upstairs.hops, 1)
    check("and it lives in the house, not the town", upstairs.map_key, "1.0")

    check("objects never spoken to are frontiers",
          sum(1 for f in fronts if f.kind == "object"), 2)
    world.mark_talked(0, 9, 2, 10)
    check("talking to one removes it",
          sum(1 for f in world.frontiers("0.9") if f.kind == "object"), 1)

    # Routing across maps.
    check("route to self", world.route("0.9", "0.9"), ["0.9"])
    check("route town -> house", world.route("0.9", "1.0"), ["0.9", "1.0"])
    check("route house -> upstairs", world.route("1.0", "1.1"), ["1.0", "1.1"])
    check("route town -> upstairs crosses the house",
          world.route("0.9", "1.1"), ["0.9", "1.0", "1.1"])
    check("no route to somewhere unheard of", world.route("0.9", "7.7"), None)

    # A sealed exit must stop being offered. Emerald gates Route 101 this way.
    world.mark_blocked("0.9", direction="north")
    check_true("a blocked connection drops out of the frontiers",
               "0.16" not in {f.direction and f.dest_key for f in world.frontiers("0.9")}
               or all(f.direction != "north" for f in world.frontiers("0.9")))
    world.mark_blocked("0.9", tile=(14, 8))
    check_true("a blocked warp drops out too",
               "1.2" not in {f.dest_key for f in world.frontiers("0.9")})

    # Persistence.
    world.save()
    reloaded = World(store)
    check("maps survive a reload", sorted(reloaded.maps), ["0.9", "1.0"])
    check("blocked exits survive a reload", reloaded.blocked, world.blocked)
    check("talked-to objects survive a reload",
          reloaded.maps["0.9"].objects["2,10"], True)
    check_true("the reloaded world still routes",
               reloaded.route("0.9", "1.1") == ["0.9", "1.0", "1.1"])

    print("       world summary as the model sees it:")
    for line in reloaded.summary("0.9"):
        print("         " + line)
    check_true("summary mentions the town", any("0.9" in l for l in reloaded.summary("0.9")))

# -- summary ---------------------------------------------------------------

print("\n" + "=" * 60)
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("all offline checks passed")
