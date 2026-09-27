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
from .mapgrid import MapGrid
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
        self.ctl.bike_hook = self.ride
        self.ctl.repel_hook = self.maybe_repel
        self.ctl.repel_ready = lambda: not self.wild_wanted() and (
            self.game.var("VAR_REPEL_STEP_COUNT") > 0
            or any(self.game.has_item(n) for n, _ in self.REPELS))
        self.grinding: dict | None = None      # who/what level while grind_to runs (TUI)
        self.training: dict | None = None      # team target while train() runs (TUI)
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

    # Gym leaders and the story bosses you talk to: whoever's script this is
    # gets a full team (from the bag) right before the conversation starts.
    BOSS_SCRIPT = re.compile(r"EventScript_(Roxanne|Brawly|Wattson|Flannery|Norman|Winona|"
                             r"TateAndLiza|Juan|Maxie|Archie|Matt|Tabitha|Shelly)\b")

    def talk(self, map_id: str, local_id: int):
        obj = next((o for o in maps()[map_id]["objects"] if o["local_id"] == local_id), None)
        boss = bool(obj and self.BOSS_SCRIPT.search(obj.get("script") or "")) \
            and map_id not in ("MAP_MAUVILLE_CITY", "MAP_SOOTOPOLIS_CITY")   # talks, not fights
        self.ctl.talk(map_id, local_id,
                      before_press=(lambda: self.heal_before_boss(obj["script"])) if boss else None)

    RESETTING_GYMS = ("MAP_FORTREE_CITY_GYM", "MAP_SOOTOPOLIS_CITY_GYM_1F",
                      "MAP_SOOTOPOLIS_CITY_GYM_B1F", "MAP_MOSSDEEP_CITY_GYM")

    def attack_budget(self, mon) -> float:
        """Attacking PP left, weighted by power (Muddy Water's PP matter more
        than Rock Smash's)."""
        moves = [(mv, self.data.move(mv.id)) for mv in mon.moves if mv.id]
        full = sum(info.power * info.pp for mv, info in moves if info.power)
        return sum(info.power * mv.pp for mv, info in moves if info.power) / full if full else 1.0

    def heal_before_boss(self, who: str) -> None:
        """Revive, cure and top up the fighters from the bag, standing in front
        of a boss: the gym's trainers on the way used to leave the team at half
        HP for the leader, and a loss costs a trip back plus levels of grinding.
        Only the fighters (the first two -- doubles -- and the trained team):
        HM carriers do not need potions."""
        from .emerald import team_pref
        team = team_pref(self)
        lead = self.lead()
        # Potions cannot give PP back: Swampert met Norman with Muddy Water at
        # 6/10 and lost. Where walking out keeps the gym's progress (beaten
        # trainers stay beaten; not the puzzles that reset or are solved
        # live), a Pokemon Center first, then the milestone runs again.
        # Gyms only (their Center is next door) and once per boss: out of the
        # Magma Hideout and back twice cost 47 minutes, and the way back
        # through a dungeon spends the PP again.
        here = self.game.map_id()
        trips = self.__dict__.setdefault("_boss_center_trips", set())
        if lead and "_GYM" in here and here not in self.RESETTING_GYMS \
                and who not in trips and self.attack_budget(lead) < 0.6:
            trips.add(who)
            log.info("BOSS %s ahead: %s low on PP (%.0f%%), to a Pokemon Center first",
                     who.split("EventScript_")[-1], lead.species_name,
                     100 * self.attack_budget(lead))
            self.heal()
            # Back the way the milestone knows (Norman's doors open by its own
            # routine): walking straight back could not get through them.
            raise Stuck("healed before the boss; walking back")
        fighters = {m.slot for m in self.game.party()
                    if not m.is_egg and (m.slot < 2 or m.species_name in team)}
        low = [m for m in self.game.party() if m.slot in fighters
               and (m.fainted or m.hp_frac < 0.9 or m.status & 0xFF)]
        if not low:
            return
        log.info("BOSS %s ahead: healing %s", who.split("EventScript_")[-1],
                 ", ".join(f"{m.species_name} {m.hp}/{m.max_hp}" for m in low))
        self.heal_with_items(min_frac=0.9, slots=fighters)

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

    def new_game(self) -> None:
        """Start over even if the cartridge holds a save (mGBA keeps the .sav
        next to the ROM, and beating the Elite Four saves the game): soft
        reset with A+B+SELECT+START, pick NEW GAME, then boot as usual."""
        log.info("NEW GAME: soft reset to the title screen")
        self.ctl.press("A", "B", "SELECT", "START", hold=12, release=60)
        for _ in range(3000):
            if "Task_HandleMainMenuInput" in self.game.active_tasks():
                break
            self.ctl.press("A", hold=3, release=20)     # through the intro and title
        else:
            raise Stuck("new game: never reached the title menu")
        # CONTINUE is listed first when a save exists; NEW GAME is next.
        want = 1 if self.emu.u16(S["gSaveFileStatus"]) == 1 else 0
        for _ in range(6):
            data = self.game.task_data("Task_HandleMainMenuInput")
            if data is None or data[1] == want:
                break
            self.ctl.press("DOWN", release=10)
        self.ctl.press("A", release=30)
        self.boot()

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
    CENTERS = [m for m in maps() if m.endswith("_POKEMON_CENTER_1F")] + [
        "MAP_EVER_GRANDE_CITY_POKEMON_LEAGUE_1F"]    # has its own nurse

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

    def low_attack_pp(self, frac: float = 0.5, mon=None) -> bool:
        """The lead (or `mon`) has under half its attacking PP left (a boss
        fight ahead should not start with Surf at 0 -- that cost a whiteout)."""
        lead = mon or self.lead()
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
            if "_GYM" in self.game.map_id():
                # Leaving a gym midway loses its progress: Norman's rooms are
                # re-entered by another door, Sootopolis's ice floors reset.
                self.heal_with_items()
                if not self.needs_heal(0.4):
                    return False
            log.info("HEALTH low (party %.0f%%) - detouring to heal", 100 * self.party_hp())
            self.heal()
            return True
        return False

    def _ace_to_front(self) -> None:
        """Put the route's first TEAM_PREF member back in front (rotate_lead
        moves others there for single fights)."""
        from .emerald import TEAM_PREF
        party = self.game.party()
        ace = min(party, key=lambda m: TEAM_PREF.index(m.species_name)
                  if m.species_name in TEAM_PREF else len(TEAM_PREF))
        if ace.slot != 0 and ace.species_name in TEAM_PREF:
            self.party_swap(0, ace.slot)

    def rotate_lead(self, foe_type: str | None = None) -> None:
        """Before each Elite Four fight: lead with the trained member that has
        the most attacking power left (level x power x PP). PP is what runs
        out over five fights; leading with the same Pokemon every time sent
        Swampert to Wallace with 0 PP. `foe_type` (the room's main attacking
        type) discounts whoever it hits hard: Rayquaza led into Glacia's Ice,
        fainted, and went on to Drake with its Outrage spent."""
        from .emerald import team_pref
        team = team_pref(self)

        def budget(m) -> float:
            if m.fainted or m.is_egg or m.species_name not in team:
                return 0.0
            power = m.level * sum(self.data.move(mv.id).power * min(mv.pp, 15)
                                  for mv in m.moves if mv.id)
            if foe_type:
                power /= max(0.5, self.data.effectiveness(
                    const(foe_type), self.data.species(m.species).types))
            return power
        party = self.game.party()
        best = max(party, key=budget)
        if best.slot != 0 and budget(best) > budget(party[0]):
            log.info("E4 lead: %s (attacking budget %.0f vs %.0f)", best.species_name,
                     budget(best), budget(party[0]))
            self.party_swap(0, best.slot)

    POTION_HEAL = [("ITEM_POTION", 20), ("ITEM_SUPER_POTION", 50), ("ITEM_HYPER_POTION", 200),
                   ("ITEM_MAX_POTION", 999), ("ITEM_FULL_RESTORE", 999)]

    def heal_with_items(self, min_frac: float = 0.8, slots=None) -> None:
        """Revive and top up the party (or just `slots`) from the bag (inside
        the Elite Four, and inside gyms, where walking out breaks the puzzle's
        progress)."""
        for mon in self.game.party():
            if mon.is_egg or (slots is not None and mon.slot not in slots):
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
            if status:
                cure = next((n for n in ("ITEM_FULL_HEAL", "ITEM_FULL_RESTORE")
                             if self.game.has_item(n)), None)
                if cure:
                    self.use_item(cure, slot)
                    mon = self.game.party()[slot]
            for _ in range(4):             # small potions may take a few
                if mon.hp_frac >= min_frac:
                    break
                missing = mon.max_hp - mon.hp
                have = [(n, amt) for n, amt in self.POTION_HEAL if self.game.has_item(n)]
                if not have:
                    break
                # The smallest potion that covers most of the gap, else the biggest.
                enough = [h for h in have if h[1] >= missing * 0.7]
                item = min(enough, key=lambda h: h[1])[0] if enough else \
                    max(have, key=lambda h: h[1])[0]
                self.use_item(item, slot)
                mon = self.game.party()[slot]
        log.info("ITEM HEAL done: %s", self.status_line())

    def heal(self) -> None:
        """Walk to the nearest reachable Pokemon Center and heal."""
        self.pump()
        start = self.ctl.state()
        goal_maps = set(self.CENTERS)

        def at_center(s):
            return s.map in goal_maps

        # Every Fly costs the same, so "nearest" alone once flew from the Sky
        # Pillar to Oldale and the way back failed. Aim for the Centers
        # closest by map first; any Center if none of those can be reached.
        hops = self.ctl.planner.map_distances({start.map})
        near = sorted((hops.get(c, 999), c) for c in goal_maps)
        close = {c for d, c in near if d <= near[0][0] + 2} if near else goal_maps
        self._healing = True
        try:
            if close != goal_maps:
                try:
                    goal_maps = close
                    self.ctl.goto(at_center, desc="nearest Pokemon Center")
                except Stuck as exc:
                    log.info("HEAL no way to a nearby Center (%s); any Center then", exc)
                    goal_maps = set(self.CENTERS)
            if not at_center(self.ctl.state()):
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

    def shop(self, wants: dict[str, int], max_steps: int | None = None) -> None:
        """Buy `wants` (capped by money) at the nearest Marts that stock them.
        With max_steps, only from a Mart that close (restocking on the way:
        unlimited, it once walked 720 steps for Hyper Potions)."""
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
            places = {mid for mid, _, items in stock
                      if len(set(wants) & items) == (best if max_steps is None else 1) or
                      (max_steps is not None and set(wants) & items)}

            def at_mart(st, places=places):
                return st.map in places
            at_mart.maps = places
            if max_steps is not None:
                self.ctl.planner.narrow = True
                from .nav import NavCaps
                walk = NavCaps(**{**self.ctl.nav_caps().__dict__, "fly": ()})
                plan = self.ctl.planner.plan(self.ctl.state(), at_mart, walk,
                                             live=MapGrid.from_ram(self.game),
                                             live_objects=self.game.objects())
                if plan is None or len(plan) > max_steps:
                    log.info("SHOP no Mart within %d steps for %s", max_steps, sorted(wants))
                    return
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
        wants: dict[str, int] = {}
        # Essentials first, then spare money buys more Full Restores: items
        # are cheaper than levels, and the training targets lean on them.
        for item, price, n in (("ITEM_FULL_RESTORE", 3000, 12), ("ITEM_REVIVE", 1500, 10),
                               ("ITEM_MAX_POTION", 2500, 8), ("ITEM_FULL_HEAL", 600, 5),
                               ("ITEM_FULL_RESTORE", 3000, 25)):
            k = max(0, n - self.game.has_item(item) - wants.get(item, 0))
            k = min(k, max(0, budget) // price)
            if k:
                wants[item] = wants.get(item, 0) + k
                budget -= k * price
        log.info("LEAGUE supplies: %s", wants)
        self.shop(wants)

    # Wild battles cost ~38 s of game time each and a run met ~200 of them
    # just walking: travel under a Repel (it keeps away anything below the
    # lead's level), except while grinding or catching.
    REPELS = [("ITEM_MAX_REPEL", 250), ("ITEM_SUPER_REPEL", 200), ("ITEM_REPEL", 100)]

    def wild_wanted(self) -> bool:
        # Random encounters give less experience per battle than a grinding
        # spot picked for it: travel under a Repel always, grind when a
        # target asks (135 random encounters were ~45 minutes of a run).
        return bool(self.grinding or self.training or self.battle.policy.catch_species
                    or self.__dict__.get("_want_wilds"))

    def maybe_repel(self) -> None:
        if self.wild_wanted() or self.game.var("VAR_REPEL_STEP_COUNT") > 0:
            return
        item = next((n for n, _ in self.REPELS if self.game.has_item(n)), None)
        if item is None or not self.ctl.free():
            return
        try:
            self.use_item(item)
        except Stuck as exc:
            log.info("REPEL failed: %s", exc)
            for _ in range(4):
                self.ctl.press("B", release=12)
            self.pump()
            return
        log.info("REPEL %s (%d steps)", item, self.game.var("VAR_REPEL_STEP_COUNT"))
        if self.game.var("VAR_REPEL_STEP_COUNT") == 0:
            log.info("REPELDBG mode=%s text=%r held=%d", self.game.mode(),
                     self.game.string_var4()[-60:], self.game.has_item(item))

    def repel_to_buy(self) -> str:
        """The best Repel the marts on our way sell by now."""
        badges = self.game.badges()
        return ("ITEM_MAX_REPEL" if badges >= 7 else
                "ITEM_SUPER_REPEL" if badges >= 5 else "ITEM_REPEL")

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
        # X items stand in for levels in boss fights (Battle.setup_choice).
        xs = [("ITEM_X_ATTACK", 2), ("ITEM_X_SPEED", 2)] if badges >= 1 else []
        if badges >= 3:
            xs.append(("ITEM_X_SPECIAL", 4))
        for item, n in xs:
            if self.game.has_item(item) == 0 and self.game.money() > 4000:
                wants[item] = n
        repel = self.repel_to_buy()
        held = sum(self.game.has_item(n) for n, _ in self.REPELS)
        price = {"ITEM_REPEL": 350, "ITEM_SUPER_REPEL": 500, "ITEM_MAX_REPEL": 700}[repel]
        if held < 3:
            n = min(10, max(0, (self.game.money() - 2500) // price))
            if n >= 3:
                wants[repel] = n
        if sum(wants.values()) and self.game.money() > 1500:
            self.shop(wants, max_steps=80)

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
                 teach_ok: bool = True, give: bool = False, register: bool = False) -> None:
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
        from .menus import _bag_ready
        pidx = self.BAG_POCKETS.index(pocket)
        for _ in range(8):
            _bag_ready(self.ctl)       # presses during a pocket's slide are dropped
            cur = self._bag_pos()["pocket"]
            if cur == pidx:
                break
            self.ctl.press("RIGHT" if cur < pidx else "LEFT", release=16)
        _bag_ready(self.ctl)
        order = self.game.bag_order(pocket)
        want = order.index(iid)
        # Check what the game actually opened: a Super Repel was "used" as
        # the X Speed next to it, wasting both.
        from .menus import open_item_context
        if not open_item_context(self.ctl, pidx, want, iid):
            raise Stuck(f"bag: could not select {item}")
        if give or register:
            # Context menus are grids: USE GIVE / TOSS CANCEL (items) or USE
            # REGISTER / - CANCEL (key items); the second entry is top right.
            # Its cursor variable is stale until moved (a second GIVE read "1"
            # and pressed A on USE), so walk from the known corner instead.
            self.ctl.idle(8)
            for key in ("UP", "LEFT", "RIGHT"):
                self.ctl.press(key, release=6)
            self.ctl.press("A", release=16)
        else:
            self.ctl._menu_select(0)                     # USE
        self._drive_item_flow(target_slot, forget_slot, teach_ok)

    def register_item(self, item: str) -> None:
        """Register a key item to SELECT (the Mach Bike)."""
        self.use_item(item, register=True)
        if self.game.registered_item() != const(item):
            raise Stuck(f"registering {item} did not take")
        log.info("REGISTERED %s to SELECT", item)

    def ride(self, on: bool = True) -> bool:
        """Get on or off the Mach Bike with SELECT. False where cycling is
        not allowed (the game says so and we stay on foot)."""
        if self.game.on_bike() == on:
            return True
        if on:
            from .symbols import maps
            if not maps()[self.game.map_id()].get("allow_cycling") or self.game.surfing():
                return False
            if not self.game.has_item("ITEM_MACH_BIKE"):
                return False
            if self.game.registered_item() != const("ITEM_MACH_BIKE"):
                self.register_item("ITEM_MACH_BIKE")
        self.ctl.press("SELECT", release=20)
        for _ in range(30):
            if self.game.on_bike() == on and self.ctl.free():
                break
            if self.game.text_printing() or self.game.text_waiting():
                self.ctl.press("B", release=10)
            else:
                self.ctl.idle(4)
        return self.game.on_bike() == on

    def give_item(self, item: str, slot: int) -> None:
        """Have party member `slot` hold `item` from the bag (swapping out
        whatever it held)."""
        self.use_item(item, slot, give=True)
        mon = self.game.party()[slot]
        if mon.item != const(item):
            raise Stuck(f"giving {item} to {mon.species_name} did not take")
        log.info("GAVE %s to %s", item, mon.species_name)

    def take_item(self, slot: int) -> None:
        """Take party member `slot`'s held item back into the bag:
        POKEMON -> the mon -> ITEM -> TAKE."""
        MENU_ITEM, MENU_TAKE_ITEM = 3, 5
        mon = self.game.party()[slot]
        self.open_start_menu("POKEMON")

        def wait(task: str, frames: int = 240) -> None:
            for _ in range(frames // 4):
                if any(task in t for t in self.game.active_tasks()) and not self.game.fading():
                    return
                self.ctl.idle(4)
            raise Stuck(f"party menu: never reached {task}")

        def choose(action: int) -> None:
            internal = self.emu.u32(S["sPartyMenuInternal"])
            actions = list(self.emu.read(internal + 15, self.emu.u8(internal + 23)))
            if action not in actions:
                raise Stuck(f"party menu: no action {action} in {actions}")
            self.ctl._menu_select(actions.index(action))

        wait("Task_HandleChooseMonInput")
        for _ in range(10):
            if struct.unpack("b", self.emu.read(S["gPartyMenu"] + 9, 1))[0] == slot:
                break
            self.ctl.press("DOWN", release=5)
        self.ctl.press("A", release=16)
        wait("Task_HandleSelectionMenuInput")
        choose(MENU_ITEM)
        self.ctl.idle(10)
        wait("Task_HandleSelectionMenuInput")
        choose(MENU_TAKE_ITEM)
        for _ in range(40):                  # "Received the X from Y." -> back out
            if self.game.mode().kind == "overworld" and self.ctl.free():
                break
            if self.game.text_printing() or self.game.text_waiting():
                self.ctl.press("A", release=12)
            else:
                self.ctl.press("B", release=12)
        log.info("TOOK %s's held item", mon.species_name)

    def hold(self, item: str, slot: int) -> bool:
        """Make party member `slot` hold `item`, fetching it from whoever holds it."""
        iid = const(item)
        party = self.game.party()
        if party[slot].item == iid:
            return True
        holder = next((m for m in party if m.item == iid), None)
        if holder is not None:
            self.take_item(holder.slot)
        if not self.game.has_item(item):
            return False
        self.give_item(item, slot)
        return self.game.party()[slot].item == iid

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
        # Done if the intended Pokemon already knows it (HMs can go to more
        # than one: Azumarill's Waterfall must not stop Rayquaza's).
        knowers = [p for p in party if p.knows(move_name)]
        if any(any(s in p.species_name for s in species_pref) for p in knowers) \
                if species_pref else knowers:
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
    LOSS_BOOST = 4             # levels added to a boss milestone's targets per loss
    LOSS_BOOST_MAX = 12

    # A loss anywhere in the League sends the run back to Sidney.
    LEAGUE_FIGHTS = frozenset({"sidney", "phoebe", "glacia", "drake", "champion"})

    def _same_fight(self, lost_in: str | None, name: str) -> bool:
        return lost_in == name or {lost_in, name} <= self.LEAGUE_FIGHTS

    def before_milestone(self, m) -> None:
        if not hasattr(self, "_level_boost"):
            self._level_boost: dict[str, int] = {}
            self._losses_seen = self.battle.trainer_losses
        self.battle.policy.important = m.important
        self.pump()
        if self.game.party() and m.heal_first and (
                self.needs_heal() or self.low_attack_pp(0.35)
                or (m.important and self.low_attack_pp())):
            self.heal()
        if self.game.badges() >= 1 and self.game.money() > 1500 \
                and not self.game.map_id().startswith(self.NO_CENTER_MAPS):
            try:
                self.restock()
            except Stuck as exc:
                log.info("RESTOCK skipped: %s", exc)
        # Targets are set low enough to win without overkill grinding; each
        # trainer battle lost since the last milestone start raises the
        # targets of the milestone we are (re)starting.
        # Only boss milestones are boosted: a rival lost to on the way (the
        # Route 110 Grovyle beats a Marshtomp that only has Tackle) must not
        # inflate whatever milestone happens to start next. Capped.
        # (And only on a retry of the milestone lost in: a loss that the same
        # milestone then made up for, like Tate & Liza won on the second try,
        # must not make the next boss grind.)
        losses = self.battle.trainer_losses
        if losses > self._losses_seen:
            if m.important and self._same_fight(getattr(self, "_started", None), m.name):
                league = m.name in self.LEAGUE_FIGHTS
                step, cap = (2, 4) if league else (self.LOSS_BOOST, self.LOSS_BOOST_MAX)
                self._level_boost[m.name] = min(
                    cap, self._level_boost.get(m.name, 0) + step * (losses - self._losses_seen))
                log.info("LOST a trainer battle: %s targets now +%d levels",
                         m.name, self._level_boost[m.name])
            self._losses_seen = losses
        self._started = m.name
        boost = self._level_boost.get(m.name, 0)
        self._grind_blocked = {}       # reachability changes with the story
        trained = False
        min_level, team_level = m.targets(self)
        if min_level:
            self._ace_to_front()       # the lead target is the ace's, whoever led last
            trained |= self.lead().level < min_level + boost
            self._grind_why = "lead target"
            try:
                self.grind_to(min_level + boost)
            finally:
                self._grind_why = ""
        if team_level:
            trained |= self.train(team_level + boost, members=m.team_size)
        # Grinding spends PP and HP: a boss fight starts fresh.
        if trained and m.important and not self.game.map_id().startswith(self.NO_CENTER_MAPS) \
                and (self.party_hp() < 0.95 or self.low_attack_pp(0.9)):
            self.heal()

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
                     "ROUTE130",           # its grass is Mirage Island: rarely there
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

    # Experience per battle grows with the foe's level, and walking there is
    # cheap (Fly), so grind among the strongest wild levels a trainee still
    # outlevels -- never in whatever grass happens to be nearest. The tier is
    # 5 levels wide and the nearest spot in it wins: a few levels of XP are
    # not worth doubling every heal trip (Victory Road B1F vs 1F: 87 heals).
    GRIND_TIER = 5

    def grind_maps(self, level: int) -> set[str]:
        """The best tier of maps for a L`level` trainee: the highest wild
        levels up to level+2 (and those within GRIND_TIER of them) among maps
        not yet found unreachable from here with today's abilities."""
        blocked = getattr(self, "_grind_blocked", {}).get(self._reach_key(), set())
        easy = {m: v for m, v in self._easy_maps(level).items() if m not in blocked}
        if not easy:
            return set()
        best = max(easy.values())
        return {m for m, v in easy.items() if v >= best - self.GRIND_TIER}

    def _easy_maps(self, level: int) -> dict[str, int]:
        """Every usable map whose strongest wild Pokemon the trainee outlevels
        (or nearly): map -> that top level."""
        usable = self._grind_usable_maps()
        tops = {m: max(hi for _, hi, _ in mons) for m, mons in self.data.wild_land().items()
                if m in usable}
        return {m: v for m, v in tops.items() if v <= level + 2}

    def _reach_key(self) -> tuple:
        """Field abilities: what "unreachable" depends on within a milestone.
        Story moves change it too (Granite Cave is out of reach from the
        mainland until Mr. Briney sails us to Dewford), so before_milestone
        forgets these findings."""
        caps = self.ctl.nav_caps()
        return (caps.surf, caps.waterfall, caps.dive, caps.strength)

    def _at_center(self):
        centers = set(self.CENTERS)

        def at_center(s):
            return s.map in centers
        at_center.maps = centers
        return at_center

    def _plannable(self, goal) -> bool:
        """One bounded search, story triggers relaxed the way goto relaxes
        them (the rotating-tile gyms are all triggers)."""
        from .nav import NavCaps
        caps = self.ctl.nav_caps(avoid_grass=0.0)
        live, objs = MapGrid.from_ram(self.game), self.game.objects()
        # Never the world-wide search: a grind spot beyond a 6-map detour is
        # too far to be worth it, and each failed check cost seconds.
        self.ctl.planner.narrow = True
        for c in (caps, NavCaps(**{**caps.__dict__, "active_triggers_block": False})):
            if self.ctl.planner.plan(self.ctl.state(), goal, c, live=live,
                                     live_objects=objs) is not None:
                return True
        return False

    def grind_to(self, level: int, max_battles: int = 400) -> None:
        """Fight wild Pokemon until the lead reaches `level` (see _grind_to).
        While it runs, self.grinding says who, to what level and why, for the
        TUI's goal pane."""
        lead = self.lead()
        if lead is None or lead.level >= level:
            return
        self.grinding = {"species": lead.species_name, "personality": lead.personality,
                         "start": lead.level, "target": level, "battles": 0, "map": "",
                         "why": getattr(self, "_grind_why", "")}
        try:
            self._grind_to(level, max_battles)
        finally:
            self.grinding = None

    _carrier: int | None = None        # personality of the switch-training carrier

    def _fighter(self):
        """Who does the fighting while grinding: the carrier when switch-
        training, else the lead."""
        if self._carrier is not None:
            mon = next((m for m in self.game.party()
                        if m.personality == self._carrier and not m.fainted), None)
            if mon is not None:
                return mon
        return self.lead()

    def _grind_to(self, level: int, max_battles: int = 400) -> None:
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
        log.info("GRIND %s L%d -> L%d%s", lead.species_name, lead.level, level,
                 f" carried by {self._fighter().species_name}" if self._carrier else "")
        self.battle.policy.fight_wild = True
        planner = self.ctl.planner
        battles = 0
        spot = None            # (map, x, y, direction, back)
        dry = 0                # consecutive spots without an encounter
        spots: set[str] = set()
        while self.lead().level < level and battles < max_battles:
            fighter = self._fighter()
            if self.needs_heal(0.55) or self.low_attack_pp(0.1, fighter) or \
                    (self._carrier and fighter.hp_frac < 0.45):
                self.heal()            # a trainee out of PP only switches and struggles
                spot = None
            want = self.grind_maps(self._fighter().level)
            if spot is not None and want and spot[0] not in want:
                spot = None    # outgrew this area
            if spot is not None and (self.game.map_id(), *self.game.pos()) != spot[:3]:
                try:
                    self.ctl.goto(at(*spot[:3]), desc="back to grinding spot")
                except Stuck:
                    spot = None
            if spot is None:
                last_resort = not want
                if last_resort:
                    # Nothing in the good tiers is reachable from here: grind
                    # in the nearest grass the trainee outlevels rather than
                    # fail the milestone.
                    want = set(self._easy_maps(self._fighter().level))
                    log.info("GRIND no good tier reachable from %s; nearest grass instead",
                             self.game.map_id())
                    if not want:
                        raise Stuck(f"grinding: no map suits L{self.lead().level}")
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
                in_grass.maps = spots
                # One bounded search first: a failing goto runs the whole
                # fallback chain, which is far too slow to learn "no".
                reachable = self._plannable(in_grass)
                if not reachable and not self._plannable(self._at_center()):
                    # Nothing plans from here (a puzzle room such as the
                    # Mossdeep Gym): it is where we stand, not the grass.
                    # Walk out properly, then choose again.
                    self.ctl.goto(self._at_center(), desc="out to a Pokemon Center")
                    continue
                try:
                    if not reachable:
                        raise Stuck("no grinding spot in range")
                    self.ctl.goto(in_grass, caps=self.ctl.nav_caps(avoid_grass=0.0),
                                  desc=f"grass (L{self.lead().level} spots)")
                except Stuck:
                    if last_resort:
                        raise Stuck(f"grinding: no grass reachable from {self.game.map_id()}")
                    # Unreachable from here with today's HMs: never search for
                    # them again from here, and try the next-best tier instead.
                    if not hasattr(self, "_grind_blocked"):
                        self._grind_blocked = {}
                    self._grind_blocked.setdefault(self._reach_key(), set()).update(spots)
                    log.info("GRIND spots unreachable for now: %s", sorted(spots))
                    dry += 1
                    if dry > 8:
                        raise Stuck("grinding: nowhere reachable produces encounters")
                    continue
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
                self.grinding["map"] = spot[0]
                log.info("GRIND spot %s (%d,%d) pacing %s/%s", spot[0], x, y, d, back)
            # pace back and forth until a battle starts
            fought, moved = False, 0
            for i in range(240):
                before = self.game.pos()
                self.ctl._hold_until_moved(spot[3] if i % 2 == 0 else spot[4])
                if self.game.mode().kind != "overworld":
                    battles += 1
                    self.grinding["battles"] = battles
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

    def train(self, level: int, members: int = 2) -> bool:
        """Bring `members` Pokemon (the route's TEAM_PREF first, then the
        strongest) up to `level`, one at a time.

        Double battles (Tate & Liza) and the Elite Four punish a one-Pokemon
        team. Each trainee is moved to the front so it earns the whole share
        of experience, then the original order is restored.
        """
        from .emerald import team_pref
        party = [m for m in self.game.party() if not m.is_egg]
        pref = {name: i for i, name in enumerate(team_pref(self))}
        # Only the route's team is ever trained (never Castform or the Fly
        # carrier); with Rayquaza that team is small.
        ranked = sorted((m for m in party if m.species_name in pref),
                        key=lambda m: (pref[m.species_name], -m.level))[:members]
        order = [m.personality for m in party]
        self.training = {"target": level, "members": [m.personality for m in ranked]}
        self._grind_why = "team target"
        try:
            return self._train(ranked, level, order)
        finally:
            self.training = None
            self._grind_why = ""

    def _train(self, ranked, level: int, order: list) -> bool:
        trained = False
        for mon in ranked:
            cur = next((m for m in self.game.party() if m.personality == mon.personality), None)
            if cur is None or cur.level >= level:
                continue
            trained = True
            slot = [m.personality for m in self.game.party()].index(mon.personality)
            self.party_swap(0, slot)
            trainee = self.game.party()[0]               # now in front
            carrier = self._carrier_for(trainee)
            if carrier is not None:
                log.info("CARRY %s L%d holds the Exp. Share; %s L%d fights",
                         trainee.species_name, trainee.level, carrier.species_name, carrier.level)
                self._carrier = carrier.personality
                self.battle.policy.carry = (trainee.personality, carrier.personality)
            try:
                self.grind_to(level)
            finally:
                self._carrier = None
                self.battle.policy.carry = None
                self.pump()
                now = [m.personality for m in self.game.party()]
                if now != order:
                    self.party_swap(0, slot)
        return trained

    CARRY_GAP = 8          # the carrier must outlevel the trainee by this much

    def _carrier_for(self, trainee):
        """The ace carries a trainee it clearly outlevels, once the trainee
        holds the Exp. Share (participant + holder = 3/4 of the experience,
        from wild Pokemon far stronger than the trainee could fight, with no
        damage taken and so no heal trips)."""
        from .emerald import TEAM_PREF
        party = self.game.party()
        team = [m for m in party if m.species_name in TEAM_PREF and not m.fainted
                and m.personality != trainee.personality]
        if not team or not (self.game.has_item("ITEM_EXP_SHARE") or any(
                m.item == const("ITEM_EXP_SHARE") for m in party)):
            return None
        ace = min(team, key=lambda m: (TEAM_PREF.index(m.species_name), -m.level))
        if ace.level < trainee.level + self.CARRY_GAP:
            return None
        # 3/4 of a strong foe's experience only beats all of a weak foe's when
        # the trainee's own grass is much weaker: carrying an Azumarill that
        # already handles Victory Road was no faster than letting it fight.
        own = max(self._easy_maps(trainee.level).values(), default=0)
        carried = max(self._easy_maps(ace.level).values(), default=0)
        if own >= 0.8 * carried:
            return None
        try:
            if not self.hold("ITEM_EXP_SHARE", trainee.slot):
                return None
        except Stuck as exc:
            log.info("CARRY: could not give the Exp. Share: %s", exc)
            self.pump()
            return None
        return next(m for m in self.game.party() if m.personality == ace.personality)

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
    def catch_static(self, species: str, map_id: str, pattern: str) -> None:
        """Walk up to a one-off encounter (a legendary standing on the map),
        talk to start the battle, and catch it."""
        from .route import object_id
        self.battle.policy.catch_species = {const(species)}
        try:
            self.talk(map_id, object_id(map_id, pattern))
            self.pump()
        finally:
            self.battle.policy.catch_species = set()
        log.info("CATCH %s: party %s", species, [m.species_name for m in self.game.party()])

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
