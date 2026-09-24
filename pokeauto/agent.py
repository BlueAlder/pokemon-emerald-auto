"""The Agent: one object holding the emulator, state reader, controller,
battle system and the out-of-battle routines the route needs (menus, healing,
shopping, teaching moves, grinding).

Everything here is deterministic code.
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
    def __init__(self, emu: Emu):
        self.emu = emu
        self.game = Game(emu)
        self.data = GameData(emu)
        prompts = Prompts()
        self.ctl = Controller(emu, self.game, prompts=prompts)
        self.battle = Battle(self.ctl, self.data)
        self.ctl.battle = self.battle
        self.battle.move_learner = self.choose_move_to_forget
        self.ctl.health_check = self._health_check
        self.ctl.fly_hook = self._fly
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
    def _fly(self, dest_map: str) -> bool:
        """Planner step 'fly'. A town the game refuses is never planned again."""
        from .fly import fly_to
        try:
            ok = fly_to(self, dest_map)
        except Stuck as e:
            log.info("FLY failed: %s", e)
            for _ in range(4):
                self.ctl.press("B", release=12)
            self.pump()
            ok = False
        if not ok and self.game.map_id() != dest_map:
            self.ctl.fly_banned.add(dest_map)
        return ok

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

    def party_swap(self, a: int, b: int) -> None:
        """Swap party slots a and b with the field party menu's SWITCH."""
        if a == b:
            return
        want = [m.personality for m in self.game.party()]
        want[a], want[b] = want[b], want[a]
        self.open_start_menu("POKEMON")
        slot_addr = S["gPartyMenu"] + 9

        def wait(task: str, frames: int = 240) -> None:
            for _ in range(frames // 4):
                if any(task in t for t in self.game.active_tasks()) and not self.game.fading():
                    self.ctl.idle(4)
                    return
                self.ctl.idle(4)
            raise Stuck(f"party menu: never reached {task}")

        def press_until(button: str, task: str) -> None:
            for _ in range(4):
                self.ctl.press(button, release=16)
                try:
                    wait(task, 60)
                    return
                except Stuck:
                    pass
            raise Stuck(f"party menu: never reached {task}")

        def cursor_to(slot: int, addr: int = slot_addr) -> None:
            for _ in range(10):
                cur = struct.unpack("b", self.emu.read(addr, 1))[0]
                if cur == slot:
                    return
                self.ctl.press("DOWN" if cur < slot or cur > 5 and slot == 0 else "UP", release=5)
            raise Stuck(f"party menu: cursor never reached slot {slot}")

        wait("Task_HandleChooseMonInput")
        cursor_to(a)
        press_until("A", "Task_HandleSelectionMenuInput")
        internal = self.emu.u32(S["sPartyMenuInternal"])
        n = self.emu.u8(internal + 23)
        actions = list(self.emu.read(internal + 15, n))
        MENU_SWITCH = 1
        self.ctl._menu_select(actions.index(MENU_SWITCH))
        wait("Task_HandleChooseMonInput")
        cursor_to(b, slot_addr + 1)           # slotId2: the switch-target cursor
        self.ctl.press("A", release=16)
        wait("Task_HandleChooseMonInput", 600)      # after the slide animation
        for _ in range(6):
            if self.game.mode().kind == "overworld" and self.ctl.free():
                break
            self.ctl.press("B", release=20)
        got = [m.personality for m in self.game.party()]
        if got != want:
            raise Stuck(f"party swap {a}<->{b} did not take")
        log.info("PARTY swapped slots %d and %d: %s", a, b,
                 [m.species_name for m in self.game.party()])

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

    def low_attack_pp(self, frac: float = 0.5) -> bool:
        """The lead has under half its attacking PP left (a boss fight ahead
        should not start with Surf at 0 -- that cost a whiteout once)."""
        lead = self.lead()
        if lead is None:
            return False
        moves = [(mv, self.data.move(mv.id)) for mv in lead.moves if mv.id]
        have = sum(mv.pp for mv, info in moves if info.power)
        full = sum(info.pp for mv, info in moves if info.power)
        return full > 0 and have < full * frac

    # Rooms that lock behind you: no way back to a Pokemon Center.
    NO_CENTER_MAPS = ("MAP_EVER_GRANDE_CITY_SIDNEYS_ROOM", "MAP_EVER_GRANDE_CITY_PHOEBES_ROOM",
                      "MAP_EVER_GRANDE_CITY_GLACIAS_ROOM", "MAP_EVER_GRANDE_CITY_DRAKES_ROOM",
                      "MAP_EVER_GRANDE_CITY_CHAMPIONS_ROOM", "MAP_EVER_GRANDE_CITY_HALL")

    def _health_check(self) -> bool:
        """Called between steps of every walk: detour to heal before it is too late."""
        if self.__dict__.get("_healing"):
            return False               # already walking to a Pokemon Center
        if self.game.party() and self.needs_heal(0.4):
            if self.game.map_id().startswith(self.NO_CENTER_MAPS):
                self.heal_with_items()
                return False
            log.info("HEALTH low (party %.0f%%) - detouring to heal", 100 * self.party_hp())
            self.heal()
            return True
        return False

    def heal_with_items(self, min_frac: float = 0.8) -> None:
        """Revive and top up the party from the bag (inside the Elite Four)."""
        for mon in self.game.party():
            if mon.is_egg:
                continue
            slot = mon.slot
            if mon.fainted:
                item = next((n for n in ("ITEM_MAX_REVIVE", "ITEM_REVIVE")
                             if self.game.has_item(n)), None)
                if item is None:
                    continue
                self.use_item(item, slot)
                mon = self.game.party()[slot]
            if mon.fainted:
                continue
            status = mon.status & 0xFF
            if mon.hp_frac < min_frac or status:
                missing = mon.max_hp - mon.hp
                choices = (["ITEM_FULL_RESTORE", "ITEM_FULL_HEAL"] if status else []) + (
                    ["ITEM_HYPER_POTION"] if missing <= 200 else []) + [
                    "ITEM_MAX_POTION", "ITEM_FULL_RESTORE", "ITEM_HYPER_POTION"]
                item = next((n for n in choices if self.game.has_item(n)), None)
                if item and (mon.hp_frac < min_frac or item in ("ITEM_FULL_HEAL",
                                                                "ITEM_FULL_RESTORE")):
                    self.use_item(item, slot)
        log.info("ITEM HEAL done: %s", self.status_line())

    def heal(self) -> None:
        """Walk to the nearest reachable Pokemon Center and heal."""
        self.pump()
        start = self.ctl.state()
        goal_maps = set(self.CENTERS)

        def at_center(s):
            return s.map in goal_maps

        self._healing = True
        try:
            self.ctl.goto(at_center, desc="nearest Pokemon Center")
            center = self.game.map_id()
            nurse = next(o for o in maps()[center]["objects"] if "NURSE" in o["gfx"])
            # The nurse stands behind the counter: talk from two tiles below.
            self.goto(center, nurse["x"], nurse["y"] + 2)
        finally:
            self._healing = False
        self.ctl.face("up")
        self.ctl.press("A", release=10)
        self.pump()
        log.info("HEALED at %s (party hp %.0f%%)", center, 100 * self.party_hp())
        del start

    # -- shopping -------------------------------------------------------------------------

    MART_EXCLUDE = ("BATTLE_FRONTIER", "TRAINER_HILL")

    def shop(self, wants: dict[str, int]) -> None:
        """Buy `wants` (capped by money) at the nearest Marts that stock them."""
        from .menus import buy
        wants = {k: v for k, v in wants.items() if v > 0}
        stock = [(mid, m["script"], set(m["items"])) for mid, info in maps().items()
                 if not any(x in mid for x in self.MART_EXCLUDE)
                 for m in info.get("marts", ())]
        for _ in range(4):
            if not wants:
                return
            best = max(len(set(wants) & items) for _, _, items in stock)
            if best == 0:
                log.info("SHOP nobody sells %s", sorted(wants))
                return
            places = {mid for mid, _, items in stock if len(set(wants) & items) == best}

            def at_mart(st, places=places):
                return st.map in places
            at_mart.maps = places
            self.ctl.goto(at_mart, desc="nearest Mart with " + ",".join(sorted(wants)))
            here = self.game.map_id()
            script, items = max(((sc, it) for mid, sc, it in stock if mid == here),
                                key=lambda z: len(set(wants) & z[1]))
            clerk = next((o for o in maps()[here]["objects"] if o["script"] == script), None)
            if clerk is None:
                raise Stuck(f"no clerk for {script} in {here}")
            basket = {k: v for k, v in wants.items() if k in items}
            buy(self.ctl, lambda: self.ctl.talk(here, clerk["local_id"], pump_after=False), basket)
            self.pump()
            log.info("SHOPPED at %s: %s money now %d", here, basket, self.game.money())
            wants = {k: v for k, v in wants.items() if k not in basket}

    def league_supplies(self) -> None:
        """Stock up for the Elite Four: no Pokemon Center between the five fights."""
        budget = self.game.money() - 2000
        wants = {}
        for item, price, n in (("ITEM_FULL_RESTORE", 3000, 12), ("ITEM_REVIVE", 1500, 8),
                               ("ITEM_MAX_POTION", 2500, 8), ("ITEM_FULL_HEAL", 600, 5)):
            k = max(0, n - self.game.has_item(item))
            k = min(k, max(0, budget) // price)
            if k:
                wants[item] = k
                budget -= k * price
        log.info("LEAGUE supplies: %s", wants)
        self.shop(wants)

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
        last_prompt = None
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
                if text != last_prompt:
                    log.info("ITEM PROMPT %r -> %s", text[-60:], "YES" if ans else "NO")
                    last_prompt = text
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
                forget = next((i for i, mv in enumerate(mon.moves) if mv.const == replace), None)
            if forget is None:
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
        self.pump()
        if self.game.party() and m.heal_first and (
                self.needs_heal() or (m.important and self.low_attack_pp())):
            self.heal()
        if self.game.badges() >= 2 and self.game.money() > 3000 \
                and not self.game.map_id().startswith(self.NO_CENTER_MAPS):
            try:
                self.restock()
            except Stuck as exc:
                log.info("RESTOCK skipped: %s", exc)
        if m.min_level:
            self.grind_to(m.min_level)
        if m.team_level:
            self.train(m.team_level, members=m.team_size)

    def recover(self, m, exc) -> None:
        """Get back to a sane state after a Stuck/crash."""
        for _ in range(6):
            self.ctl.press("B", release=10)
        try:
            self.pump()
        except Stuck:
            pass

    # -- grinding -------------------------------------------------------------------------
    def lead(self):
        party = [p for p in self.game.party() if not p.is_egg]
        return party[0] if party else None

    # Places a grinding session should never pick: no battles (Safari Zone),
    # tide/one-way layouts, or story-locked towers.
    GRIND_EXCLUDE = ("SAFARI_ZONE", "SHOAL_CAVE", "SKY_PILLAR", "MIRAGE", "ARTISAN_CAVE",
                     "DESERT_UNDERPASS", "ALTERING_CAVE", "TERRA_CAVE", "MARINE_CAVE",
                     "SOUTHERN_ISLAND", "NAVEL_ROCK", "BIRTH_ISLAND", "FARAWAY_ISLAND",
                     "SEAFLOOR_CAVERN", "CAVE_OF_ORIGIN", "MAGMA_HIDEOUT",
                     "SCORCHED_SLAB", "ABANDONED_SHIP", "NEW_MAUVILLE", "METEOR_FALLS_B1F_2R",
                     "METEOR_FALLS_STEVENS_CAVE")

    def _grind_usable_maps(self) -> set[str]:
        """Maps worth pacing in: they have a wild table, are not on the
        exclusion list, and have not already failed to produce encounters."""
        dead = self.__dict__.get("_grind_dead", set())
        return {m for m in self.data.wild_land()
                if not any(x in m for x in self.GRIND_EXCLUDE) and m not in dead}

    def grind_maps(self, level: int) -> set[str]:
        """Maps whose wild levels suit a L`level` trainee: tough enough to pay,
        weak enough to win against. Falls back to the strongest easy ones."""
        caps = self.ctl.nav_caps()
        key = (caps.surf, caps.waterfall, caps.dive, caps.strength)
        blocked = getattr(self, "_grind_blocked", {}).get(key, set())
        usable = self._grind_usable_maps()
        table = {m: v for m, v in self.data.wild_land().items()
                 if m in usable and m not in blocked}
        good = {m for m, mons in table.items()
                if max(hi for _, hi, _ in mons) <= level + 2
                and max(hi for _, hi, _ in mons) >= level - 10}
        if good:
            return good
        easy = {m: max(hi for _, hi, _ in mons) for m, mons in table.items()
                if max(hi for _, hi, _ in mons) <= level + 2}
        if not easy:
            return set()
        top = max(easy.values())
        return {m for m, v in easy.items() if v >= top - 3}

    def grind_to(self, level: int, max_battles: int = 400) -> None:
        """Fight wild Pokemon until the lead reaches `level`.

        Picks the nearest encounter tiles whose wild levels suit the lead
        (grind_maps), then paces there battle after battle. It only searches
        again after a heal, when the lead outgrows the area, or when pacing
        stops producing encounters.
        """
        from .mapgrid import MapGrid, has_encounters
        lead = self.lead()
        if lead is None or lead.level >= level:
            return
        log.info("GRIND %s L%d -> L%d", lead.species_name, lead.level, level)
        self.battle.policy.fight_wild = True
        planner = self.ctl.planner
        battles = 0
        spot = None            # (map, x, y, direction, back)
        fallback = False       # spot is "any grass" because the ideal maps are out of reach
        dry = 0                # consecutive spots without an encounter
        spots: set[str] = set()
        while self.lead().level < level and battles < max_battles:
            if self.needs_heal(0.55):
                self.heal()
                spot = None
            want = self.grind_maps(self.lead().level)
            if spot is not None and want and spot[0] not in want and not fallback:
                spot = None    # outgrew this area
            if spot is not None and (self.game.map_id(), *self.game.pos()) != spot[:3]:
                try:
                    self.ctl.goto(at(*spot[:3]), desc="back to grinding spot")
                except Stuck:
                    spot = None
            if spot is None:
                spots = set(want)

                usable = self._grind_usable_maps()

                def in_grass(s):
                    if spots and s.map not in spots:
                        return False
                    if s.map not in usable:
                        return False       # excluded, no wild table, or no encounters seen
                    g = planner.grid(s.map)
                    return g.inside(s.x, s.y) and has_encounters(g.behavior(s.x, s.y)) \
                        and not s.surfing
                in_grass.maps = spots or None
                fallback = False
                try:
                    # One bounded search first: a failing goto runs the whole
                    # fallback chain, which is far too slow to learn "no".
                    if spots and self.ctl.planner.plan(
                            self.ctl.state(), in_grass, self.ctl.nav_caps(avoid_grass=0.0),
                            live=MapGrid.from_ram(self.game),
                            live_objects=self.game.objects()) is None:
                        raise Stuck("no grinding spot in range")
                    self.ctl.goto(in_grass, caps=self.ctl.nav_caps(avoid_grass=0.0),
                                  desc=f"grass (L{self.lead().level} spots)")
                except Stuck:
                    fallback = True
                    if not spots:
                        raise
                    # Unreachable with today's HMs: never search for them again.
                    caps = self.ctl.nav_caps()
                    key = (caps.surf, caps.waterfall, caps.dive, caps.strength)
                    if not hasattr(self, "_grind_blocked"):
                        self._grind_blocked = {}
                    self._grind_blocked.setdefault(key, set()).update(spots)
                    log.info("GRIND spots unreachable for now: %s", sorted(spots))
                    spots.clear()      # take any encounter tile
                    in_grass.maps = None
                    self.ctl.goto(in_grass, caps=self.ctl.nav_caps(avoid_grass=0.0),
                                  desc="grass")
                grid = MapGrid.from_ram(self.game)
                x, y = self.game.pos()
                dirs = [d for d, (dx, dy) in (("left", (-1, 0)), ("right", (1, 0)),
                                               ("up", (0, -1)), ("down", (0, 1)))
                        if grid.inside(x + dx, y + dy)
                        and has_encounters(grid.behavior(x + dx, y + dy))
                        and not grid.collision(x + dx, y + dy)]
                d = dirs[0] if dirs else "left"
                back = {"left": "right", "right": "left", "up": "down", "down": "up"}[d]
                spot = (self.game.map_id(), x, y, d, back)
                log.info("GRIND spot %s (%d,%d) pacing %s/%s", spot[0], x, y, d, back)
            # pace back and forth until a battle starts
            fought, moved = False, 0
            for i in range(240):
                before = self.game.pos()
                self.ctl._hold_until_moved(spot[3] if i % 2 == 0 else spot[4])
                if self.game.mode().kind != "overworld":
                    battles += 1
                    self.pump()
                    fought = True
                    break
                if self.game.pos() != before:
                    moved += 1
                elif i - moved > 6:
                    break               # we are not actually moving: pick a new spot
            if not fought:
                log.info("GRIND no encounter at %s after %d moves; never grinding there again",
                         spot[:3], moved)
                self.__dict__.setdefault("_grind_dead", set()).add(spot[0])
                spot = None
                dry += 1
                if dry > 8:
                    raise Stuck("grinding: nowhere left that produces encounters")
            else:
                dry = 0
        log.info("GRIND done: %s after %d battles", self.lead(), battles)

    def train(self, level: int, members: int = 2) -> None:
        """Bring the `members` strongest Pokemon up to `level`, one at a time.

        Double battles (Tate & Liza) and the Elite Four punish a one-Pokemon
        team. Each trainee is moved to the front so it earns the whole share
        of experience, then the original order is restored.
        """
        party = [m for m in self.game.party() if not m.is_egg]
        ranked = sorted(party, key=lambda m: -m.level)[:members]
        order = [m.personality for m in party]
        for mon in ranked:
            cur = next((m for m in self.game.party() if m.personality == mon.personality), None)
            if cur is None or cur.level >= level:
                continue
            slot = [m.personality for m in self.game.party()].index(mon.personality)
            self.party_swap(0, slot)
            try:
                self.grind_to(level)
            finally:
                self.pump()
                now = [m.personality for m in self.game.party()]
                if now != order:
                    self.party_swap(0, slot)

    def fortree_gym(self) -> None:
        from .puzzles import fortree_gym
        fortree_gym(self)

    def ice_gym(self, map_id: str, leader_pattern: str, badge_count: int) -> None:
        from .puzzles import ice_gym
        ice_gym(self, map_id, leader_pattern, badge_count)

    def rotating_tile_gym(self, map_id: str, leader_pattern: str, badge_count: int) -> None:
        from .puzzles import rotating_tile_gym
        rotating_tile_gym(self, map_id, leader_pattern, badge_count)

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
