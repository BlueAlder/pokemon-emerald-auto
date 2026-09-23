"""Battles: a code policy that reads exact state, and a UI driver.

Decisions are made in code from exact numbers -- both sides' stats, stat
stages, types, abilities and (in Gen 3 RAM) the opponent's full moveset -- so
the move that maximises expected progress is a calculation, not a guess.
An optional `advisor` (Jev) is consulted only for close calls in battles that
matter (gyms, rivals, Elite Four), where the top options score within a margin.

The UI driver keys off gBattlerControllerFuncs, i.e. exactly which input
handler the battle engine is waiting in (action menu, move menu, target
select, yes/no, party menu), so it never presses A into the wrong menu.
"""
from __future__ import annotations

import logging
import re
import struct
from dataclasses import dataclass, field

from .data import (GameData, combatant_from_battle, combatant_from_party, estimate_damage)
from .symbols import const, const_names, symbols

log = logging.getLogger("pokeauto.battle")
S = symbols()
C = const

ACT_FIGHT, ACT_BAG, ACT_POKEMON, ACT_RUN = 0, 1, 2, 3


@dataclass
class Choice:
    kind: str                  # move | switch | run | item
    slot: int = 0              # move slot or party slot
    target: int = 1            # battler id for targeted moves
    score: float = 0.0
    why: str = ""


@dataclass
class BattlePolicy:
    """Knobs the route layer sets per situation."""
    fight_wild: bool = True          # False -> run from wild battles
    catch_species: set = field(default_factory=set)   # species ids to catch
    min_hp_to_fight_wild: float = 0.35
    important: bool = False          # gym/rival/E4: allow advisor on close calls


class Battle:
    def __init__(self, ctl, data: GameData, advisor=None):
        self.ctl = ctl
        self.game = ctl.game
        self.emu = ctl.emu
        self.data = data
        self.policy = BattlePolicy()
        self.advisor = advisor               # callable(desc, options) -> index | None
        self.move_learner = None             # callable(mon, new_move_id) -> slot to forget | None
        self._pending: dict[int, Choice] = {}
        self.turns = 0
        self.log: list[str] = []

    # -- state helpers ----------------------------------------------------------------
    def is_trainer(self) -> bool:
        return bool(self.game.battle_type() & C("BATTLE_TYPE_TRAINER"))

    def is_double(self) -> bool:
        return bool(self.game.battle_type() & C("BATTLE_TYPE_DOUBLE"))

    def in_battle_screen(self) -> bool:
        return self.game.callback2() == S["BattleMainCB2"]

    def battle_over(self) -> bool:
        cb = S.name_at(self.game.callback2())
        return cb in ("CB2_Overworld",) or cb.startswith(("CB2_ReturnToField", "CB2_WhiteOut",
                                                          "CB2_EndWildBattle", "CB2_EndTrainerBattle"))

    # -- UI driver --------------------------------------------------------------------
    def run(self) -> None:
        """Drive the battle until control returns to the field."""
        self._pending.clear()
        idle = 0
        start = self.emu.frame
        while True:
            if self.battle_over():
                return
            if self.emu.frame - start > 60 * 60 * 10:
                from .controller import Stuck
                self.ctl.snapshot("battle_timeout")
                raise Stuck(f"battle ran 10 game-minutes: {self.game.mode()} "
                            f"cmd={self.script_command()} ctrl={self.game.battle_controller(0)}")
            cb = S.name_at(self.game.callback2())
            tasks = self.game.active_tasks()
            handled = False
            cmd = self.script_command() if cb == "BattleMainCB2" else ""
            if cmd in ("Cmd_yesnoboxlearnmove", "Cmd_yesnoboxstoplearningmove", "Cmd_yesnobox"):
                self._script_yes_no(cmd)
                handled = True
            elif cb == "BattleMainCB2":
                for battler in (0, 2) if self.is_double() else (0,):
                    fn = self.game.battle_controller(battler)
                    if fn.startswith("HandleInputChooseAction"):
                        self._choose_action(battler)
                        handled = True
                    elif fn.startswith("HandleInputChooseMove"):
                        self._choose_move(battler)
                        handled = True
                    elif fn.startswith("HandleInputChooseTarget"):
                        self._choose_target(battler)
                        handled = True
                    elif fn.startswith("PlayerHandleYesNoInput"):
                        self._yes_no()
                        handled = True
                    if handled:
                        break
            elif any("Task_HandleChooseMonInput" in t or "Task_HandleSelectionMenuInput" in t
                     for t in tasks):
                self._party_menu(tasks)
                handled = True
            elif any("ReplaceMove" in t for t in tasks) or "Summary" in cb:
                self._forget_move_screen(tasks)
                handled = True
            elif cb.startswith("CB2_EvolutionScene") or "Evolution" in cb:
                self.ctl.press("A", release=8)       # never B: that cancels evolution
                handled = True
            elif "NamingScreen" in cb:
                self.ctl.press("B", release=6)      # decline nicknames
                handled = True
            elif "Bag" in cb:
                self._bag_screen(tasks)
                handled = True
            if not handled:
                if cb == "BattleMainCB2" and (self.game.text_waiting() or idle > 30):
                    self.ctl.press("B", hold=2, release=4)
                    idle = 0
                elif cb != "BattleMainCB2" and self.game.mode().kind in ("overworld", "script"):
                    return
                elif cb != "BattleMainCB2" and self.game.text_waiting():
                    self.ctl.press("A", release=4)
                else:
                    self.ctl.idle(3)
                    idle += 1
            else:
                idle = 0

    def _cursor_grid(self, addr: int, target: int) -> None:
        """Move a 2x2 cursor (0 1 / 2 3) stored at addr to target, then press A."""
        for _ in range(6):
            cur = self.emu.u8(addr)
            if cur == target:
                break
            if (cur & 1) != (target & 1):
                self.ctl.press("RIGHT" if target & 1 else "LEFT", release=3)
            elif (cur & 2) != (target & 2):
                self.ctl.press("DOWN" if target & 2 else "UP", release=3)
        self.ctl.press("A", release=6)

    def _choose_action(self, battler: int) -> None:
        choice = self.decide(battler)
        self._pending[battler] = choice
        self.turns += 1
        me = self.game.battle_mons()[battler]
        foe = self._foes()[0][1] if self._foes() else None
        line = (f"T{self.turns} {me.species_name} {me.hp}/{me.max_hp} vs "
                f"{foe.species_name + ' ' + str(foe.hp) + '/' + str(foe.max_hp) if foe else '?'}"
                f" -> {choice.kind} {choice.slot} ({choice.why})")
        log.info("BATTLE %s", line)
        self.log.append(line)
        cursor = S["gActionSelectionCursor"] + battler
        if choice.kind == "move":
            self._cursor_grid(cursor, ACT_FIGHT)
        elif choice.kind == "switch":
            self._cursor_grid(cursor, ACT_POKEMON)
        elif choice.kind == "run":
            self._cursor_grid(cursor, ACT_RUN)
        elif choice.kind == "item":
            self._cursor_grid(cursor, ACT_BAG)

    def _choose_move(self, battler: int) -> None:
        choice = self._pending.get(battler)
        if choice is None or choice.kind != "move":
            # We landed in the move menu without meaning to; decide now.
            choice = self.decide(battler)
            if choice.kind != "move":
                self.ctl.press("B", release=6)
                return
            self._pending[battler] = choice
        self._cursor_grid(S["gMoveSelectionCursor"] + battler, choice.slot)

    def _choose_target(self, battler: int) -> None:
        choice = self._pending.get(battler)
        want = choice.target if choice else 1
        addr = S["gMultiUsePlayerCursor"]
        for _ in range(6):
            if self.emu.u8(addr) == want:
                break
            self.ctl.press("RIGHT", release=4)
        self.ctl.press("A", release=6)

    def script_command(self) -> str:
        """Name of the battle-script command currently executing."""
        ip = self.emu.u32(S["gBattlescriptCurrInstr"])
        if not 0x08000000 <= ip < 0x0A000000:
            return ""
        op = self.emu.u8(ip)
        return S.name_at(self.emu.u32(S["gBattleScriptingCommandsTable"] + op * 4))

    def _script_yes_no(self, cmd: str) -> None:
        """Yes/no boxes run by the battle script itself (learning moves etc.)."""
        text = self.game.battle_text().replace("\n", " ")
        if cmd == "Cmd_yesnoboxlearnmove":
            ans = self._want_new_move(text)
        elif cmd == "Cmd_yesnoboxstoplearningmove":
            ans = True          # yes, stop: we already decided not to learn it
        else:
            ans = not re.search(r"nickname|switch|change", text, re.I)
        log.info("BATTLE PROMPT %r -> %s", text[-70:], "YES" if ans else "NO")
        cursor = S["gBattleCommunication"] + 1
        for _ in range(3):
            cur = self.emu.u8(cursor)
            if cur == (0 if ans else 1):
                break
            self.ctl.press("UP" if ans else "DOWN", release=3)
        self.ctl.press("A", release=8)
        for _ in range(30):
            if self.script_command() != cmd:
                break
            self.ctl.idle(2)

    def _yes_no(self) -> None:
        text = self.game.battle_text().replace("\n", " ")
        ans = True
        if re.search(r"nickname", text, re.I):
            ans = False
        elif re.search(r"switch|change", text, re.I) and "Will" in text:
            ans = False
        elif re.search(r"Delete a move|make room|forget", text, re.I):
            ans = self._want_new_move(text)
        elif re.search(r"Stop learning|give up", text, re.I):
            ans = True   # we only get here after declining to forget a move
        log.info("BATTLE PROMPT %r -> %s", text[-70:], "YES" if ans else "NO")
        if ans:
            self.ctl.press("UP", release=3)
            self.ctl.press("A", release=6)
        else:
            self.ctl.press("B", release=6)

    # -- move learning ------------------------------------------------------------------
    def _learning(self, text: str) -> tuple[object, int] | None:
        """(party mon, new move id) for a 'wants to learn X' prompt."""
        mid = self.emu.u16(S["gMoveToLearn"])
        party = self.game.party()
        name = text.split(" is trying")[0].split(" wants")[0].strip().split(" ")[-1] if text else ""
        mon = next((m for m in party if m.nickname.upper() == name.upper()), None)
        if mon is None and party:
            mon = max(party, key=lambda m: m.level)
        return (mon, mid) if mon and mid else None

    def _want_new_move(self, text: str) -> bool:
        learn = self._learning(text)
        if not learn or not self.move_learner:
            return False
        mon, mid = learn
        self._forget_slot = self.move_learner(mon, mid)
        return self._forget_slot is not None

    def _forget_move_screen(self, tasks) -> None:
        slot = getattr(self, "_forget_slot", None)
        slot = 4 if slot is None else slot
        # Summary screen move list: cursor starts at the top move.
        for _ in range(slot):
            self.ctl.press("DOWN", release=6)
        self.ctl.press("A", release=20)
        self._forget_slot = None
        for _ in range(60):
            if not any("ReplaceMove" in t for t in self.game.active_tasks()):
                break
            self.ctl.idle(4)

    # -- party menu (switching in) ------------------------------------------------------
    def _party_menu(self, tasks) -> None:
        target = self._switch_target()
        addr = S["gPartyMenu"] + 9
        if any("Task_HandleSelectionMenuInput" in t for t in tasks):
            self.ctl.press("A", release=10)          # SHIFT / SEND OUT is first
            return
        for _ in range(8):
            if struct.unpack("b", self.emu.read(addr, 1))[0] == target:
                break
            self.ctl.press("DOWN", release=4)
        self.ctl.press("A", release=10)

    def _switch_target(self) -> int:
        choice = self._pending.get(0)
        if choice and choice.kind == "switch":
            return choice.slot
        return self.best_switch()

    def _bag_screen(self, tasks) -> None:
        # Bag use (potions, balls) is driven by items.py when installed.
        handler = getattr(self, "bag_handler", None)
        if handler and handler(self, tasks):
            return
        self.ctl.press("B", release=8)

    # -- decisions -------------------------------------------------------------------------
    def _foes(self):
        mons = self.game.battle_mons()
        ids = (1, 3) if self.is_double() else (1,)
        return [(i, mons[i]) for i in ids if mons[i].hp > 0 and mons[i].species]

    def decide(self, battler: int) -> Choice:
        mons = self.game.battle_mons()
        me_bm = mons[battler]
        me = combatant_from_battle(self.data, me_bm)
        foes = self._foes()
        if not foes:
            return Choice("move", 0, why="no foe visible")
        wild = not self.is_trainer()

        # Wild battles: run unless we want the XP (or to catch).
        if wild and foes:
            fid, fbm = foes[0]
            if fbm.species in self.policy.catch_species and getattr(self, "catcher", None):
                c = self.catcher(self, fbm)
                if c:
                    return c
            if not self.policy.fight_wild or me_bm.hp_frac < self.policy.min_hp_to_fight_wild:
                if not self._run_blocked():
                    return Choice("run", why="not worth fighting")

        options = self.move_options(me_bm, me, foes)
        if not options:
            return Choice("move", 0, why="struggle")
        options.sort(key=lambda c: -c.score)
        best = options[0]

        # Danger check: will the foe KO us before we KO it?
        fid, fbm = foes[0]
        foe = combatant_from_battle(self.data, fbm)
        threat = self.threat(foe, me)
        we_first = self._speed(me, me_bm) > self._speed(foe, fbm)
        we_ko = best.score >= 1.0
        if threat >= me_bm.hp and not (we_ko and we_first):
            sw = self.best_switch(exclude_active=True, against=foe)
            if sw is not None and self._switch_value(sw, foe) > 0.35 and not self._trapped():
                return Choice("switch", sw, why=f"threat {threat:.0f} >= hp {me_bm.hp}")
        # Close call in an important battle: ask the advisor.
        if (self.advisor and self.policy.important and len(options) > 1
                and options[1].score > 0.85 * best.score and best.score < 1.0):
            idx = self.advisor(self.describe(me_bm, fbm), options[:4])
            if idx is not None and 0 <= idx < len(options):
                best = options[idx]
                best.why += " (advisor)"
        return best

    def move_options(self, me_bm, me, foes) -> list[Choice]:
        out = []
        for slot, mv in enumerate(me_bm.moves):
            if mv.pp == 0:
                continue
            info = self.data.move(mv.id)
            eff = const_names()["EFFECT_"].get(info.effect, "")
            best_score, best_target, why = -1.0, foes[0][0], ""
            for fid, fbm in foes:
                foe = combatant_from_battle(self.data, fbm)
                dmg = estimate_damage(self.data, me, foe, info)
                acc = (info.accuracy or 100) / 100
                frac = min(1.0, dmg / max(1, fbm.hp))
                kill = estimate_damage(self.data, me, foe, info, roll=0.85) >= fbm.hp
                score = acc * frac + (0.5 * acc if kill else 0)
                if eff in ("EFFECT_EXPLOSION",):
                    score *= 0.1
                if eff in ("EFFECT_RECOIL", "EFFECT_DOUBLE_EDGE"):
                    score *= 0.9
                if eff in ("EFFECT_SOLAR_BEAM", "EFFECT_RAZOR_WIND", "EFFECT_SKY_ATTACK",
                           "EFFECT_SKULL_BASH", "EFFECT_FOCUS_PUNCH", "EFFECT_RECHARGE"):
                    score *= 0.55
                if eff in ("EFFECT_DREAM_EATER",) and not fbm.status1 & 7:
                    score = 0
                if eff in ("EFFECT_SNORE", "EFFECT_SLEEP_TALK") and not me_bm.status1 & 7:
                    score = 0
                if eff == "EFFECT_FALSE_SWIPE":
                    score *= 0.8
                if info.priority > 0 and kill:
                    score += 0.2
                if score > best_score:
                    best_score, best_target = score, fid
                    why = f"{info.name} ~{dmg:.0f}dmg{' KO' if kill else ''}"
            if info.power == 0 and best_score <= 0:
                best_score, why = 0.01, f"{info.name} (status)"
            out.append(Choice("move", slot, best_target, best_score, why))
        return out

    def threat(self, foe, me) -> float:
        """Highest expected damage the foe's known moves do to us."""
        best = 0.0
        for fid, fbm in self._foes():
            for mv in fbm.moves:
                info = self.data.move(mv.id)
                best = max(best, estimate_damage(self.data, foe, me, info) *
                           (info.accuracy or 100) / 100)
            break
        return best

    def _speed(self, c, bm) -> float:
        from .data import _stage
        spe = _stage(c.spe, c.stages[3])
        if bm.status1 & C("STATUS1_PARALYSIS"):
            spe //= 4
        return spe

    def _run_blocked(self) -> bool:
        return bool(self.game.battle_type() & (C("BATTLE_TYPE_TRAINER") |
                                                C("BATTLE_TYPE_FIRST_BATTLE")))

    def _trapped(self) -> bool:
        return False

    def best_switch(self, exclude_active: bool = False, against=None) -> int:
        party = self.game.party()
        active = self.game.battler_party_index(0)
        if against is None:
            foes = self._foes()
            against = combatant_from_battle(self.data, foes[0][1]) if foes else None
        best, best_v = None, -1e9
        for m in party:
            if m.fainted or m.is_egg or (m.slot == active):
                continue
            v = self._switch_value(m.slot, against) if against else m.hp_frac
            if v > best_v:
                best, best_v = m.slot, v
        return best if best is not None else (0 if not exclude_active else None)

    def _switch_value(self, slot: int, foe) -> float:
        mon = next(m for m in self.game.party() if m.slot == slot)
        me = combatant_from_party(self.data, mon)
        dealt = max((estimate_damage(self.data, me, foe, self.data.move(mv.id))
                     for mv in mon.moves if mv.pp), default=0.0)
        taken = 0.0
        foes = self._foes()
        if foes:
            for mv in foes[0][1].moves:
                taken = max(taken, estimate_damage(self.data, foe, me, self.data.move(mv.id)))
        return min(1.0, dealt / max(1, foe.hp)) - min(1.0, taken / max(1, mon.hp)) + mon.hp_frac * 0.3

    def describe(self, me_bm, foe_bm) -> dict:
        return {
            "my_pokemon": f"{me_bm.species_name} L{me_bm.level} {me_bm.hp}/{me_bm.max_hp} HP",
            "opponent": f"{foe_bm.species_name} L{foe_bm.level} {foe_bm.hp}/{foe_bm.max_hp} HP",
            "opponent_moves": [self.data.move(m.id).name for m in foe_bm.moves],
            "battle": "trainer" if self.is_trainer() else "wild",
        }
