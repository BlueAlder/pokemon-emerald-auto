"""The Agent: one object holding the emulator, state reader, controller,
battle system and the out-of-battle routines the route needs (menus, healing,
shopping, teaching moves, grinding).

Everything here is deterministic code. Jev is plugged in only through the
narrow hooks in `brain.py` (unknown prompts, stuck recovery, close battle calls).
"""
from __future__ import annotations

import logging
import re
import struct
import time

from .battle import Battle
from .controller import Controller, Prompts, Stuck
from .data import GameData
from .emu import Emu
from .game import Game
from .nav import adjacent, at
from .symbols import const, const_names, maps, symbols

log = logging.getLogger("pokeauto")
S = symbols()
C = const

MENU_ACTION = {"POKEDEX": 0, "POKEMON": 1, "BAG": 2, "POKENAV": 3, "PLAYER": 4,
               "SAVE": 5, "OPTION": 6, "EXIT": 7}


class Agent:
    def __init__(self, emu: Emu, brain=None):
        self.emu = emu
        self.game = Game(emu)
        self.data = GameData(emu)
        self.brain = brain
        prompts = Prompts(unknown=brain.yes_no if brain else None)
        self.ctl = Controller(emu, self.game, prompts=prompts)
        self.battle = Battle(self.ctl, self.data, advisor=brain.battle_advice if brain else None)
        self.ctl.battle = self.battle
        self.battle.move_learner = self.choose_move_to_forget
        self.ctl.health_check = self._health_check
        if brain:
            self.ctl.multichoice_handler = brain.multichoice
        self.started = time.time()

    # -- conveniences --------------------------------------------------------------
    @property
    def g(self) -> Game:
        return self.game

    def pump(self):
        self.ctl.pump()

    def goto(self, map_id: str, x: int | None = None, y: int | None = None, **kw):
        self.ctl.goto(at(map_id, x, y), desc=f"{map_id}" + (f"({x},{y})" if x is not None else ""),
                      **kw)

    def talk(self, map_id: str, local_id: int):
        self.ctl.talk(map_id, local_id)

    def goto_puzzle(self, map_id: str, x: int, y: int):
        from .nav import adjacent as _adj
        self.goto(map_id)
        self.ctl.goto_puzzle(_adj(map_id, x, y), desc=f"{map_id}({x},{y})")

    def interact(self, map_id: str, x: int, y: int, direction: str | None = None):
        self.ctl.interact(map_id, x, y, direction)

    # -- boot ---------------------------------------------------------------------------
    def boot(self) -> None:
        """From power-on (or anywhere in the title/intro) to overworld control."""
        for _ in range(4000):
            m = self.game.mode()
            if m.kind in ("overworld", "script"):
                self.pump()
                return
            if m.kind == "naming":
                self.ctl.press("START", release=20)     # keep the default name
                self.ctl.press("A", release=30)
                continue
            if "Task_HandleMainMenuInput" in m.tasks:
                # CONTINUE is first when a save exists, NEW GAME otherwise.
                self.ctl.press("A", release=20)
                continue
            self.ctl.press("A", hold=3, release=20)
        raise Stuck("could not get through the title screen")

    # -- start menu ---------------------------------------------------------------------
    def open_start_menu(self, action: str) -> None:
        self.pump()
        for _ in range(3):
            self.ctl.press("START", release=12)
            if any("StartMenu" in t for t in self.game.active_tasks()):
                break
        n = self.emu.u8(S["sNumStartMenuActions"])
        actions = list(self.emu.read(S["sCurrentStartMenuActions"], n))
        want = MENU_ACTION[action]
        if want not in actions:
            self.ctl.press("B", release=10)
            raise Stuck(f"{action} is not in the start menu yet")
        target = actions.index(want)
        for _ in range(12):
            cur = self.emu.u8(S["sStartMenuCursorPos"])
            if cur == target:
                break
            self.ctl.press("DOWN" if cur < target else "UP", release=4)
        self.ctl.press("A", release=30)

    def set_options(self) -> None:
        """Text speed FAST, battle animations OFF, battle style SET."""
        opts = self.game.options()
        if opts["text_speed"] == 2 and opts["battle_scene_off"] and opts["battle_style_set"]:
            return
        self.open_start_menu("OPTION")
        for _ in range(60):
            if self.game.task_data("Task_OptionMenuProcessInput"):
                break
            self.ctl.idle(4)
        want = {1: 2, 2: 1, 3: 1}      # task data index -> value
        for row, value in ((0, 2), (1, 1), (2, 1)):
            for _ in range(8):
                d = self.game.task_data("Task_OptionMenuProcessInput")
                if d[0] != row:
                    self.ctl.press("DOWN" if d[0] < row else "UP", release=4)
                    continue
                if d[row + 1] == want[row + 1]:
                    break
                self.ctl.press("RIGHT" if d[row + 1] < want[row + 1] else "LEFT", release=4)
        self.ctl.press("B", release=30)
        for _ in range(60):
            if self.game.mode().kind in ("overworld", "script") and not any(
                    "StartMenu" in t for t in self.game.active_tasks()):
                break
            if any("StartMenu" in t for t in self.game.active_tasks()):
                self.ctl.press("B", release=10)
            self.ctl.idle(4)
        self.pump()
        log.info("OPTIONS now %s", self.game.options())

    # -- healing ------------------------------------------------------------------------
    CENTERS = [m for m in maps() if m.endswith("_POKEMON_CENTER_1F")]

    def party_hp(self) -> float:
        party = [m for m in self.game.party() if not m.is_egg]
        if not party:
            return 1.0
        return sum(m.hp for m in party) / max(1, sum(m.max_hp for m in party))

    def needs_heal(self, threshold: float = 0.5) -> bool:
        party = [m for m in self.game.party() if not m.is_egg]
        if not party:
            return False
        lead = party[0]
        low_pp = all(sum(mv.pp for mv in m.moves) < 4 for m in party if not m.fainted)
        return (self.party_hp() < threshold or lead.hp_frac < 0.35 or lead.fainted
                or low_pp)

    def _health_check(self) -> bool:
        """Called between steps of every walk: detour to heal before it is too late."""
        if self.game.party() and self.needs_heal(0.4):
            log.info("HEALTH low (party %.0f%%) - detouring to heal", 100 * self.party_hp())
            self.heal()
            return True
        return False

    def heal(self) -> None:
        """Walk to the nearest reachable Pokemon Center and heal."""
        self.pump()
        start = self.ctl.state()
        goal_maps = set(self.CENTERS)

        def at_center(s):
            return s.map in goal_maps

        self.ctl.goto(at_center, desc="nearest Pokemon Center")
        center = self.game.map_id()
        nurse = next(o for o in maps()[center]["objects"] if "NURSE" in o["gfx"])
        # The nurse stands behind the counter: talk from two tiles below.
        self.goto(center, nurse["x"], nurse["y"] + 2)
        self.ctl.face("up")
        self.ctl.press("A", release=10)
        self.pump()
        log.info("HEALED at %s (party hp %.0f%%)", center, 100 * self.party_hp())
        del start

    # -- shopping -------------------------------------------------------------------------
    MARTS = [m for m in maps() if m.endswith("_MART") or "DEPARTMENT_STORE_2F" in m]

    def shop(self, wants: dict[str, int]) -> None:
        """Walk to the nearest reachable Mart and buy `wants` (capped by money)."""
        from .menus import buy
        wants = {k: v for k, v in wants.items() if v > 0}
        if not wants:
            return
        self.ctl.goto(lambda s: s.map in self.MARTS, desc="nearest Mart")
        mart = self.game.map_id()
        clerk = next((o for o in maps()[mart]["objects"] if "MART_EMPLOYEE" in o["gfx"]), None)
        if clerk is None:
            raise Stuck(f"no clerk in {mart}")
        buy(self.ctl, lambda: self.ctl.talk(mart, clerk["local_id"], pump_after=False), wants)
        self.pump()
        log.info("SHOPPED at %s: %s money now %d", mart, wants, self.game.money())

    def restock(self) -> None:
        """Keep a sensible stock of healing items and balls for the stage we are at."""
        badges = self.game.badges()
        potion = ("ITEM_HYPER_POTION" if badges >= 6 else
                  "ITEM_SUPER_POTION" if badges >= 2 else "ITEM_POTION")
        want = {potion: 10 if badges >= 2 else 5}
        have = self.game.has_item(potion)
        wants = {potion: max(0, want[potion] - have) if have < want[potion] // 2 else 0}
        if badges >= 1 and self.game.has_item("ITEM_POKE_BALL") + self.game.has_item(
                "ITEM_GREAT_BALL") < 5:
            wants["ITEM_GREAT_BALL" if badges >= 3 else "ITEM_POKE_BALL"] = 5
        if sum(wants.values()) and self.game.money() > 1500:
            self.shop(wants)

    # -- move learning ------------------------------------------------------------------
    KEEP_MOVES = {"MOVE_SURF", "MOVE_STRENGTH", "MOVE_CUT", "MOVE_ROCK_SMASH",
                  "MOVE_WATERFALL", "MOVE_DIVE", "MOVE_FLASH", "MOVE_FLY"}

    def move_value(self, mon, move_id: int) -> float:
        """How much we want a move on this mon (code heuristic)."""
        info = self.data.move(move_id)
        name = const_names()["MOVE_"].get(move_id, "")
        if name in self.KEEP_MOVES and getattr(self, "hm_needed", lambda n: False)(name):
            return 1e6
        sp = self.data.species(mon.species)
        eff = const_names()["EFFECT_"].get(info.effect, "")
        if info.power <= 1:
            return 5.0 if eff in ("EFFECT_SLEEP", "EFFECT_PARALYZE", "EFFECT_TOXIC") else 1.0
        power = info.power
        if eff in ("EFFECT_MULTI_HIT",):
            power *= 3
        elif eff in ("EFFECT_DOUBLE_HIT",):
            power *= 2
        if eff in ("EFFECT_SOLAR_BEAM", "EFFECT_RAZOR_WIND", "EFFECT_SKY_ATTACK",
                   "EFFECT_SKULL_BASH", "EFFECT_RECHARGE", "EFFECT_FOCUS_PUNCH"):
            power *= 0.55
        if eff in ("EFFECT_EXPLOSION", "EFFECT_DREAM_EATER", "EFFECT_OHKO", "EFFECT_SNORE"):
            power *= 0.1
        stab = 1.5 if info.type in sp.types else 1.0
        stat = mon.attack if info.type < C("TYPE_MYSTERY") else mon.sp_attack
        return power * stab * (info.accuracy or 100) / 100 * stat / 100

    def hm_moves(self) -> set[int]:
        """Move ids taught by HMs -- these can never be forgotten normally."""
        if not hasattr(self, "_hm_moves"):
            self._hm_moves = {self.emu.u16(S["sTMHMMoves"] + i * 2) for i in range(50, 58)}
        return self._hm_moves

    def choose_move_to_forget(self, mon, new_move: int) -> int | None:
        """Slot to replace with new_move, or None to skip learning it."""
        new_v = self.move_value(mon, new_move)
        vals = []
        for i, mv in enumerate(mon.moves):
            if mv.id in self.hm_moves():
                vals.append(1e9)          # HMs cannot be deleted
                continue
            v = self.move_value(mon, mv.id)
            # keep type coverage: a move that is the only one of its type is worth more
            t = self.data.move(mv.id).type
            if sum(1 for o in mon.moves if self.data.move(o.id).type == t) == 1:
                v *= 1.15
            vals.append(v)
        worst = min(range(len(vals)), key=lambda i: vals[i])
        if new_v > vals[worst] * 1.05:
            log.info("LEARN %s: %s replaces %s", mon.species_name,
                     const_names()["MOVE_"].get(new_move), mon.moves[worst].const)
            return worst
        log.info("LEARN %s: skip %s", mon.species_name, const_names()["MOVE_"].get(new_move))
        return None

    # -- bag ------------------------------------------------------------------------------
    BAG_POCKETS = ["items", "balls", "tmhm", "berries", "key"]   # bag UI order

    def _bag_pos(self) -> dict:
        raw = self.emu.read(S["gBagPosition"], 28)
        cursor = struct.unpack_from("<5H", raw, 8)
        scroll = struct.unpack_from("<5H", raw, 18)
        return {"pocket": raw[5], "index": [c + s for c, s in zip(cursor, scroll)]}

    def _pocket_of(self, item_id: int) -> str:
        for p in self.BAG_POCKETS:
            if item_id in self.game.bag_order(p):
                return p
        raise Stuck(f"item {item_id} is not in the bag")

    def use_item(self, item: str, target_slot: int = 0, forget_slot: int | None = None,
                 teach_ok: bool = True) -> None:
        """Use an item from the bag on a party member (TMs/HMs, potions, ...).

        forget_slot: which known move a TM/HM replaces when the mon already
        knows four (None = let choose_move_to_forget decide).
        """
        iid = const(item)
        pocket = self._pocket_of(iid)
        self.open_start_menu("BAG")
        for _ in range(60):
            if "BagMenu" in S.name_at(self.game.callback2()):
                break
            self.ctl.idle(4)
        self.ctl.idle(20)
        pidx = self.BAG_POCKETS.index(pocket)
        for _ in range(8):
            cur = self._bag_pos()["pocket"]
            if cur == pidx:
                break
            self.ctl.press("RIGHT" if cur < pidx else "LEFT", release=16)
        order = self.game.bag_order(pocket)
        want = order.index(iid)
        for _ in range(80):
            cur = self._bag_pos()["index"][pidx]
            if cur == want:
                break
            self.ctl.press("DOWN" if cur < want else "UP", release=6)
        self.ctl.press("A", release=16)                  # context menu
        self.ctl._menu_select(0)                         # USE
        self._drive_item_flow(target_slot, forget_slot, teach_ok)

    def _drive_item_flow(self, target_slot: int, forget_slot: int | None, teach_ok: bool,
                         max_iters: int = 300) -> None:
        """Answer everything between 'USE' and being back in the field."""
        for _ in range(max_iters):
            m = self.game.mode()
            cb = m.callback2
            tasks = m.tasks
            text = self.game.string_var4().replace("\n", " ")
            log.debug("ITEM FLOW %s %s %r", cb, tasks[-3:], text[-40:])
            if m.kind == "overworld" and self.ctl.free():
                return
            if any("YesNo" in t or "YesOrNo" in t for t in tasks):
                ans = True
                if re.search(r"delete|forget|make room", text, re.I):
                    ans = teach_ok
                elif re.search(r"Stop (trying to teach|learning)|give up", text, re.I):
                    ans = True
                elif re.search(r"Teach", text, re.I):
                    ans = teach_ok
                log.info("ITEM PROMPT %r -> %s", text[-60:], "YES" if ans else "NO")
                if ans:
                    self.ctl._menu_select(0)
                else:
                    self.ctl.press("B", release=12)
                continue
            if any("Task_HandleChooseMonInput" in t for t in tasks):
                addr = S["gPartyMenu"] + 9
                for _ in range(8):
                    if struct.unpack("b", self.emu.read(addr, 1))[0] == target_slot:
                        break
                    self.ctl.press("DOWN", release=5)
                self.ctl.press("A", release=16)
                continue
            if any("ReplaceMove" in t for t in tasks) or "Summary" in cb:
                slot = forget_slot
                if slot is None:
                    mon = self.game.party()[target_slot]
                    slot = self.choose_move_to_forget(mon, self.emu.u16(S["gMoveToLearn"]))
                    slot = 4 if slot is None else slot
                from .menus import summary_select_move
                summary_select_move(self.ctl, slot)
                continue
            if "BagMenu" in cb and "Task_BagMenu_HandleInput" in tasks \
                    and not self.game.text_printing():
                self.ctl.press("B", release=12)          # done: leave the bag
                continue
            if any("StartMenu" in t for t in tasks):
                self.ctl.press("B", release=12)
                continue
            self.ctl.press("A", release=8)               # advance messages
        raise Stuck("item use flow did not return to the field")

    def teach(self, item: str, species_pref: list[str] | None = None,
              replace: str | None = None) -> None:
        """Teach a TM/HM to the best party member that can learn it."""
        from .data import tmhm_index
        idx = tmhm_index(item)
        party = self.game.party()
        move_name = self._tmhm_move(item)
        if any(p.knows(move_name) for p in party):
            return
        able = [p for p in party if not p.is_egg and self.data.can_learn_tmhm(p.species, idx)]
        if species_pref:
            able.sort(key=lambda p: next((i for i, s in enumerate(species_pref)
                                          if s in p.species_name), 99))
        if not able:
            raise Stuck(f"nobody can learn {item}")
        mon = able[0]
        forget = None
        if len(mon.moves) >= 4:
            if replace:
                forget = next(i for i, mv in enumerate(mon.moves) if mv.const == replace)
            else:
                forget = min((i for i in range(4) if mon.moves[i].id not in self.hm_moves()),
                             key=lambda i: self.move_value(mon, mon.moves[i].id))
        log.info("TEACH %s (%s) to %s, replacing slot %s", item, move_name, mon.species_name, forget)
        self.use_item(item, target_slot=mon.slot, forget_slot=forget)

    def _tmhm_move(self, item: str) -> str:
        """ITEM_HM06 -> MOVE_ROCK_SMASH, via the ROM's TM/HM move table."""
        from .data import tmhm_index
        idx = tmhm_index(item)
        mid = self.emu.u16(S["sTMHMMoves"] + idx * 2)
        return const_names()["MOVE_"].get(mid, "")

    # -- milestone hooks ----------------------------------------------------------------
    def before_milestone(self, m) -> None:
        self.battle.policy.important = m.important
        if self.brain:
            self.brain.goal = m.hint or m.name.replace("_", " ")
        self.pump()
        if self.game.party() and m.heal_first and self.needs_heal():
            self.heal()
        if self.game.badges() >= 2 and self.game.money() > 3000:
            try:
                self.restock()
            except Stuck as exc:
                log.info("RESTOCK skipped: %s", exc)
        if m.min_level:
            self.grind_to(m.min_level)

    def recover(self, m, exc) -> None:
        """Get back to a sane state after a Stuck/crash."""
        for _ in range(6):
            self.ctl.press("B", release=10)
        try:
            self.pump()
        except Stuck:
            pass
        if self.brain and exc is not None:
            self.brain.note_stuck(self, m, exc)

    def jev_recover(self, milestone, exc) -> None:
        """Ask Jev which concrete action on this map might unblock `milestone`."""
        if not self.brain:
            return
        self.pump()
        here = self.game.map_id()
        info = maps()[here]
        options: dict[str, str] = {}
        actions: dict[str, object] = {}

        def human(script: str) -> str:
            name = script.split("EventScript_")[-1]
            return re.sub(r"(?<!^)(?=[A-Z])", " ", name)

        for i, w in enumerate(info["warps"]):
            dest = w["dest"][4:].replace("_", " ").title()
            key = f"exit_{i}"
            options[key] = f"go through the exit at ({w['x']},{w['y']}) to {dest}"
            actions[key] = lambda w=w: self.ctl.goto(
                lambda s, w=w: s.map != here, desc=f"exit {w['dest']}")
        for o in info["objects"]:
            if o["script"] in ("0x0", "") or "ItemBall" in o["script"] or "Berry" in o["script"]:
                continue
            if o["flag"] not in ("0", "") and self.game.flag(o["flag"]):
                continue
            key = f"talk_{o['local_id']}"
            options[key] = f"talk to {human(o['script'])} ({o['gfx'][14:].lower()})"
            actions[key] = lambda o=o: self.talk(here, o["local_id"])
        for i, b in enumerate(info["bgs"]):
            if b.get("type") == "sign" and b.get("script"):
                key = f"read_{i}"
                options[key] = f"read/examine {human(b['script'])}"
                actions[key] = lambda b=b: self.interact(here, b["x"], b["y"])
        context = {"my_goal": milestone.hint or milestone.name.replace("_", " "),
                   "where_i_am": here[4:].replace("_", " ").title(),
                   "what_went_wrong": str(exc)[:200],
                   "last_text_on_screen": self.game.string_var4()[-200:]}
        pick = self.brain.recover(context, options)
        if pick and pick in actions:
            try:
                actions[pick]()
                self.pump()
            except Stuck as e:
                log.info("JEV recovery action failed: %s", e)

    # -- grinding -------------------------------------------------------------------------
    def lead(self):
        party = [p for p in self.game.party() if not p.is_egg]
        return party[0] if party else None

    def grind_to(self, level: int, max_battles: int = 400) -> None:
        """Fight wild Pokemon in the nearest grass until the lead reaches `level`."""
        from .mapgrid import MapGrid, has_encounters
        lead = self.lead()
        if lead is None or lead.level >= level:
            return
        log.info("GRIND %s L%d -> L%d", lead.species_name, lead.level, level)
        self.battle.policy.fight_wild = True
        battles = 0
        while self.lead().level < level and battles < max_battles:
            if self.needs_heal(0.55):
                self.heal()
            # nearest encounter tile (grass/cave floor) reachable by walking
            planner = self.ctl.planner

            def in_grass(s):
                g = planner.grid(s.map)
                return g.inside(s.x, s.y) and has_encounters(g.behavior(s.x, s.y)) \
                    and not s.surfing
            self.ctl.goto(in_grass, caps=self.ctl.nav_caps(avoid_grass=0.0), desc="grass")
            # pace back and forth until a battle starts
            grid = MapGrid.from_ram(self.game)
            x, y = self.game.pos()
            dirs = [d for d, (dx, dy) in (("left", (-1, 0)), ("right", (1, 0)),
                                           ("up", (0, -1)), ("down", (0, 1)))
                    if grid.inside(x + dx, y + dy) and has_encounters(grid.behavior(x + dx, y + dy))
                    and not grid.collision(x + dx, y + dy)]
            if not dirs:
                dirs = ["left", "right"]
            d = dirs[0]
            back = {"left": "right", "right": "left", "up": "down", "down": "up"}[d]
            for i in range(200):
                self.ctl._hold_until_moved(d if i % 2 == 0 else back)
                if self.game.mode().kind != "overworld":
                    battles += 1
                    self.pump()
                    break
        log.info("GRIND done: %s", self.lead())

    def fortree_gym(self) -> None:
        from .puzzles import fortree_gym
        fortree_gym(self)

    # -- catching -------------------------------------------------------------------------
    def catch(self, species: str, maps_to_search: list[str], max_battles: int = 150) -> bool:
        """Walk the grass of `maps_to_search` until a `species` is caught."""
        from .mapgrid import MapGrid, has_encounters
        sid = const(species)
        before = sum(1 for p in self.game.party() if p.species == sid)
        self.battle.policy.catch_species = {sid}
        self.battle.policy.fight_wild = False       # run from everything else
        try:
            for _ in range(max_battles):
                if sum(1 for p in self.game.party() if p.species == sid) > before:
                    log.info("CAUGHT %s", species)
                    return True
                if self.game.has_item("ITEM_POKE_BALL") + self.game.has_item(
                        "ITEM_GREAT_BALL") + self.game.has_item("ITEM_ULTRA_BALL") == 0:
                    self.shop({"ITEM_POKE_BALL": 10})
                if self.needs_heal(0.5):
                    self.heal()
                planner = self.ctl.planner

                def in_grass(s):
                    if s.map not in maps_to_search or s.surfing:
                        return False
                    g = planner.grid(s.map)
                    return g.inside(s.x, s.y) and has_encounters(g.behavior(s.x, s.y))
                self.ctl.goto(in_grass, caps=self.ctl.nav_caps(avoid_grass=0.0),
                              desc=f"grass for {species}")
                grid = MapGrid.from_ram(self.game)
                x, y = self.game.pos()
                dirs = [d for d, (dx, dy) in (("left", (-1, 0)), ("right", (1, 0)),
                                               ("up", (0, -1)), ("down", (0, 1)))
                        if grid.inside(x + dx, y + dy)
                        and has_encounters(grid.behavior(x + dx, y + dy))
                        and not grid.collision(x + dx, y + dy)]
                d = dirs[0] if dirs else "left"
                back = {"left": "right", "right": "left", "up": "down", "down": "up"}[d]
                for i in range(300):
                    self.ctl._hold_until_moved(d if i % 2 == 0 else back)
                    if self.game.mode().kind != "overworld":
                        self.pump()
                        break
            return False
        finally:
            self.battle.policy.catch_species = set()
            self.battle.policy.fight_wild = True

    # -- status --------------------------------------------------------------------------
    def status_line(self) -> str:
        party = " | ".join(str(m) for m in self.game.party())
        return (f"{self.game.map_id()} {self.game.pos()} badges={self.game.badges()} "
                f"money={self.game.money()} time={self.game.play_time()} party: {party}")
