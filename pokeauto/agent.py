"""The Agent: one object holding the emulator, state reader, controller,
battle system and the out-of-battle routines the route needs (menus, healing,
shopping, teaching moves, grinding).

Everything here is deterministic code. Jev is plugged in only through the
narrow hooks in `brain.py` (unknown prompts, stuck recovery, close battle calls).
"""
from __future__ import annotations

import logging
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

    def choose_move_to_forget(self, mon, new_move: int) -> int | None:
        """Slot to replace with new_move, or None to skip learning it."""
        new_v = self.move_value(mon, new_move)
        vals = []
        for i, mv in enumerate(mon.moves):
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

    # -- milestone hooks ----------------------------------------------------------------
    def before_milestone(self, m) -> None:
        self.battle.policy.important = m.important
        self.pump()
        if self.game.party() and m.heal_first and self.needs_heal():
            self.heal()
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

    # -- status --------------------------------------------------------------------------
    def status_line(self) -> str:
        party = " | ".join(str(m) for m in self.game.party())
        return (f"{self.game.map_id()} {self.game.pos()} badges={self.game.badges()} "
                f"money={self.game.money()} time={self.game.play_time()} party: {party}")
