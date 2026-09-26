"""Turning intentions into button presses, safely.

`Controller.pump()` is the heart of robustness: it runs the game forward and
deals with whatever is on screen -- dialogue, yes/no prompts, multichoice
menus, battles, evolutions, special screens -- until the player is standing in
the overworld with free control. Every higher-level action (walk somewhere,
talk to someone) calls it between moves, so an unexpected trainer, a phone
call or a story cutscene is handled in one place instead of breaking a plan.

What to answer at a prompt is decided by `Prompts` rules on the prompt text
(code).
"""
from __future__ import annotations

import logging
import re
import struct
import time
from dataclasses import dataclass, field

from .emu import Emu, keymask
from .game import Game
from .mapgrid import DELTA, MapGrid
from .nav import NavCaps, Planner, State, Step, adjacent, at, facing_dir
from .symbols import const, maps, symbols

log = logging.getLogger("pokeauto")
S = symbols()


class Stuck(RuntimeError):
    """An action could not make progress; the route layer decides what next."""


# ---------------------------------------------------------------------------
# Prompt policy
# ---------------------------------------------------------------------------

@dataclass
class Prompts:
    """Answers for yes/no prompts, chosen by regex on the prompt text.

    First match wins. Anything unmatched is answered YES: the story almost
    always wants yes.
    """
    rules: list[tuple[str, bool]] = field(default_factory=lambda: [
        (r"nickname", False),
        (r"give .* a nickname", False),
        (r"\bsave the game|want to save|Would you like to save", False),
        (r"(Surf|Cut|Strength|ROCK SMASH|Rock Smash|Waterfall|Dive|Flash)\?", True),
        (r"use (SURF|CUT|STRENGTH|ROCK SMASH|WATERFALL|DIVE|FLASH)", True),
        (r"stop learning|Stop trying to teach", False),
        (r"give up on learning", False),
        (r"Would you like to (rest|heal)", True),
        (r"switch POKéMON\?|change POKéMON\?", False),   # battle "will you switch?"
        (r"(throw|toss) away", False),
        (r"How about a little battle|want to battle\?", False),   # optional rival fights
    ])
    overrides: list[tuple[str, bool]] = field(default_factory=list)

    def answer(self, text: str) -> bool:
        for pattern, ans in self.overrides + self.rules:
            if re.search(pattern, text, re.I):
                return ans
        return True


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

def _on(map_id: str, goal):
    """Tag a goal with its map so the planner's A* can aim at it."""
    goal.maps = {map_id}
    return goal


class Controller:
    RUN_FLAG = "FLAG_SYS_B_DASH"

    def __init__(self, emu: Emu, game: Game, battle=None, prompts: Prompts | None = None,
                 on_unknown_screen=None):
        self.emu, self.game = emu, game
        self.planner = Planner(game)
        self.battle = battle                  # set by the runner
        self.prompts = prompts or Prompts()
        self.on_unknown_screen = on_unknown_screen
        self.multichoice_prefs: list[str] = []  # regexes, first matching label wins
        self.ui_handlers: dict[str, object] = {}
        self.health_check = None              # callable() -> True if it detoured to heal
        self.fly_hook = None                  # callable(dest_map) -> True once there
        self.fly_banned: set[str] = set()     # landing maps Fly failed to reach
        self._last_map, self._map_visit = "", 0
        self._in_health_check = False
        self.stats = {"frames": 0, "steps": 0, "battles": 0, "prompts": 0}
        self._last_prompt = None
        self.trace: list[str] = []

    def snapshot(self, tag: str) -> None:
        """Save a screenshot + screen state for post-mortem debugging."""
        from pathlib import Path
        out = Path(__file__).resolve().parent.parent / "runs" / "debug"
        out.mkdir(parents=True, exist_ok=True)
        n = len(list(out.glob("*.png")))
        path = out / f"{n:03d}_{tag}.png"
        try:
            self.emu.screenshot(str(path))
        except Exception:
            pass
        log.warning("SNAPSHOT %s: %s | text=%r", path.name, self.game.mode(),
                    self.game.string_var4()[:80])

    # -- primitive input ------------------------------------------------------
    def press(self, *buttons: str, hold: int = 3, release: int = 5) -> None:
        self.emu.press(*buttons, hold=hold, release=release)

    def idle(self, frames: int = 4) -> None:
        self.emu.idle(frames)

    # -- whole-screen state machine ----------------------------------------------
    def free(self) -> bool:
        """Overworld with free control and the avatar not mid-step."""
        m = self.game.mode()
        if m.kind != "overworld":
            return False
        av = self.game.avatar()
        return av["tile_transition"] == 0 and not self.game.fading()

    def pump(self, max_frames: int = 60 * 600, stop=None) -> None:
        """Advance the game, handling everything, until free overworld control.

        `stop` is an optional predicate checked each iteration; pump returns
        early when it holds (e.g. "the starter screen is open").
        """
        start = self.emu.frame
        idle_streak = 0
        last_kind = None
        while True:
            if stop and stop():
                return
            m = self.game.mode()
            if m.kind != last_kind:
                log.debug("mode %s", m)
                last_kind = m.kind
            if m.kind == "overworld":
                if self.free():
                    # Settle one extra frame batch: scripts can start right
                    # after a step lands (trainer sight, coord triggers).
                    self.idle(2)
                    if self.free():
                        return
                self.idle(2)
                continue
            if self.emu.frame - start > max_frames:
                self.snapshot("pump_timeout")
                raise Stuck(f"pump timed out in {m}")
            if m.kind == "battle":
                if self.battle is None:
                    self.press("A")
                else:
                    self.stats["battles"] += 1
                    self.battle.run()
                continue
            if m.kind == "script":
                idle_streak = self._script_step(m, idle_streak)
                continue
            if m.kind == "evolution":
                self.press("A", release=10)      # never B: let it evolve
                continue
            if m.kind == "title":
                # Title screen, main menu, Birch's speech: A advances all of it
                # (NEW GAME / CONTINUE is the first option either way).
                self.press("A", hold=3, release=20)
                continue
            if m.kind == "naming":
                # A nickname screen we did not avoid: accept the default.
                self.press("START", release=10)
                self.press("A", release=20)
                continue
            self._other_step(m)

    def _script_step(self, m, idle_streak: int) -> int:
        tasks = m.tasks
        if "Task_HandleYesNoInput" in tasks or any("YesNo" in t for t in tasks):
            self._answer_yes_no(self._prompt_text())
            return 0
        if "Task_HandleMultichoiceInput" in tasks:
            self._answer_multichoice()
            return 0
        if "ExecuteMatchCall" in tasks:
            self.press("A", release=6)            # PokeNav call: page through it
            return 0
        if self.game.text_waiting() or m.detail == "WaitForAorBPress":
            self.press("A")
            return 0
        if self.game.text_printing():
            self.press("B", hold=2, release=2)    # hurry the printer along
            return 0
        self.idle(4)
        return idle_streak + 1

    def _prompt_text(self) -> str:
        return self.game.string_var4().replace("\n", " ")

    def _answer_yes_no(self, text: str) -> None:
        ans = self.prompts.answer(text)
        self.stats["prompts"] += 1
        log.info("PROMPT %r -> %s", text[-80:], "YES" if ans else "NO")
        log.debug("PROMPT full %r tasks=%s", text, self.game.active_tasks())
        if ans:
            self._menu_select(0)
        else:
            self.press("B", release=8)
        for _ in range(20):          # wait for the prompt to close
            if not any("YesNo" in t for t in self.game.active_tasks()):
                break
            self.idle(2)

    def _menu(self) -> dict:
        raw = self.emu.read(S.all("sMenu")[2], 12)   # menu.c's sMenu
        return {"cursor": struct.unpack_from("b", raw, 2)[0],
                "min": struct.unpack_from("b", raw, 3)[0],
                "max": struct.unpack_from("b", raw, 4)[0],
                "columns": raw[9], "rows": raw[10]}

    def _menu_select(self, index: int) -> None:
        for _ in range(12):
            cur = self._menu()["cursor"]
            if cur == index:
                break
            self.press("DOWN" if cur < index else "UP", release=4)
        self.press("A", release=8)

    def multichoice_labels(self) -> list[str]:
        from .game import decode_text
        data = self.game.task_data("Task_HandleMultichoiceInput")
        if not data:
            return []
        mid = data[7]
        base = S["sMultichoiceLists"] + mid * 8
        list_ptr, count = struct.unpack("<IB", self.emu.read(base, 5))
        labels = []
        for i in range(count):
            text_ptr = self.emu.u32(list_ptr + i * 8)
            labels.append(decode_text(self.emu.read(text_ptr, 40)).replace("\n", " "))
        return labels

    def _answer_multichoice(self) -> None:
        menu = self._menu()
        text = self._prompt_text()
        labels = self.multichoice_labels()
        idx = menu["cursor"]                     # the game's default
        for pattern in self.multichoice_prefs:
            hit = next((i for i, l in enumerate(labels) if re.search(pattern, l, re.I)), None)
            if hit is not None:
                idx = hit
                break
        else:
            # A prompt that names one of its own options ("Please select the
            # POKéNAV.") wants that option.
            named = [i for i, l in enumerate(labels) if l.strip() and l.strip() in text]
            if named:
                idx = named[0]
        log.info("MULTICHOICE %r %s -> %s", text[-60:], labels,
                 labels[idx] if idx < len(labels) else idx)
        self._menu_select(idx)
        for _ in range(20):
            if "Task_HandleMultichoiceInput" not in self.game.active_tasks():
                break
            self.idle(2)

    def _choose_half_step(self, tasks) -> None:
        """ChooseHalfPartyForBattle (Steven's multi battle): ENTER the strongest
        three healthy Pokemon, then CONFIRM (party-menu slot 6)."""
        slot_addr = S["gPartyMenu"] + 9
        if any("Task_HandleSelectionMenuInput" in t for t in tasks):
            self._menu_select(0)                       # ENTER
            return
        chosen = {b - 1 for b in self.emu.read(S["gSelectedOrderFromParty"], 3) if b}
        party = [p for p in self.game.party() if not p.is_egg and not p.fainted]
        want = [p.slot for p in sorted(party, key=lambda p: -p.level)][:3]
        nxt = next((w for w in want if w not in chosen), None)
        target = 6 if nxt is None else nxt
        for _ in range(12):
            cur = struct.unpack("b", self.emu.read(slot_addr, 1))[0]
            if cur == target:
                break
            self.press("DOWN" if cur < target else "UP", release=5)
        self.press("A", release=16)

    def _other_step(self, m) -> None:
        tasks = m.tasks
        if "PartyMenu" in m.callback2 and self.emu.u8(S["gPartyMenu"] + 8) & 0xF == 4:
            self._choose_half_step(tasks)
            return
        if "HallOfFame" in m.callback2 or "Credits" in m.callback2 \
                or any(t.startswith(("Task_Hof", "Task_Credits")) for t in tasks):
            self.press("A", release=20)               # Hall of Fame / credits: press on
            return
        if "BagMenu" in m.callback2 and "Task_BagMenu_HandleInput" in tasks:
            self.press("B", release=12)               # a bag nobody is using: close it
            return
        for key, handler in self.ui_handlers.items():
            if key in m.callback2 or any(key in t for t in tasks):
                handler(self, m)
                return
        if "Pokenav" in m.callback2 or any("Pokenav" in t for t in tasks):
            self._pokenav_step()
            return
        if any("Task_SetClock_HandleInput" in t for t in tasks):
            self.press("A", release=10)
            return
        if any("SetClock" in t and "Confirm" in t for t in tasks):
            self._menu_select(0)
            return
        if self.game.text_waiting():
            self.press("A")
            return
        if self.on_unknown_screen:
            if self.on_unknown_screen(self, m):
                return
        self.idle(6)

    def _pokenav_step(self) -> None:
        """Leave the PokeNav. The Rustboro tutorial only lets you out after
        calling Mr. Stone (HandleMainMenuInputTutorial ignores B): MATCH CALL
        (3rd item) -> first contact -> CALL; then B, B.

        One small action per call, by state: while a call animates or prints
        (Task_RunLoopedTask / a text printer), read it with A; once a call
        has happened, only B. Blind A presses after the call used to land on
        the contact list and call Mr. Stone again, over and over."""
        st = self.__dict__.setdefault("_nav", {"called": False, "navigated": -1, "seen": -1})
        if self.emu.frame - st["seen"] > 600:          # a new visit to the PokeNav
            st.update(called=False, navigated=-1)
        st["seen"] = self.emu.frame
        tasks = self.game.active_tasks()
        printing = self.game.text_printing()
        if printing or any("RunLoopedTask" in t for t in tasks):
            if printing:
                st["called"] = True
                self.press("A", release=10)          # next line of the call
            else:
                self.idle(4)                         # the call screen animating
            return
        if st["called"]:
            self.press("B", release=12)              # list -> main menu -> out
            return
        main_menu = any("CurrentMenuOptionGlow" in t for t in tasks) and not self.game.fading()
        stale = st["navigated"] >= 0 and self.emu.frame - st["navigated"] > 600
        if main_menu and (st["navigated"] < 0 or stale):
            for _ in range(4):                       # 4 items in this menu: back to the top
                self.press("UP", release=8)
            for key in ("DOWN", "DOWN", "A"):         # MATCH CALL
                self.press(key, release=20)
            self.idle(60)
            for key in ("A", "A"):                   # first contact -> CALL
                self.press(key, release=30)
            st["navigated"] = self.emu.frame
        elif stale and not main_menu:
            self.press("B", release=20)              # the call never started: back out, retry
            st["navigated"] = self.emu.frame - 500
        else:
            self.idle(4)                             # fading in, or a screen loading

    # -- field abilities ------------------------------------------------------
    def nav_caps(self, avoid_grass: float = 0.5) -> NavCaps:
        party = self.game.party()
        knows = lambda mv: any(p.knows(mv) for p in party)
        f = self.game.flag
        return NavCaps(
            strength=knows("MOVE_STRENGTH") and f("FLAG_BADGE04_GET"),
            dive=knows("MOVE_DIVE") and f("FLAG_BADGE07_GET"),
            surf=knows("MOVE_SURF") and f("FLAG_BADGE05_GET"),
            cut=knows("MOVE_CUT") and f("FLAG_BADGE01_GET"),
            smash=knows("MOVE_ROCK_SMASH") and f("FLAG_BADGE03_GET"),
            waterfall=knows("MOVE_WATERFALL") and f("FLAG_BADGE08_GET"),
            avoid_grass=avoid_grass,
            fly=self.fly_destinations(),
        )

    def fly_destinations(self) -> tuple:
        """Landing States of every visited town, if someone can Fly now."""
        from .fly import destinations
        if self.fly_hook is None or not self.game.flag("FLAG_BADGE06_GET") or not any(
                p.knows("MOVE_FLY") and not p.fainted for p in self.game.party()):
            return ()
        out = []
        for d in destinations(self.emu):
            if self.game.flag(d["flag"]) and d["map"] not in self.fly_banned:
                g = self.planner.grid(d["map"])
                e = g.elevation(d["x"], d["y"]) if g.inside(d["x"], d["y"]) else 3
                out.append(State(d["map"], d["x"], d["y"], 3 if e in (0, 15) else e))
        return tuple(out)

    def state(self) -> State:
        x, y = self.game.pos()
        m = self.game.map_id()
        if m != self._last_map:
            # Entering a map reloads it: smashed rocks and pushed boulders reset.
            self._last_map = m
            self._map_visit += 1
        return State(m, x, y, self.game.player_elevation(), self.game.surfing())

    # -- walking ----------------------------------------------------------------
    def _hold_until_moved(self, direction: str, max_frames: int = 40) -> bool:
        """Hold a direction until the player's tile or map changes."""
        before = (self.game.location(), self.game.pos())
        run = self.game.flag(self.RUN_FLAG) and not self.game.surfing()
        mask = keymask(direction.upper(), *(["B"] if run else []))
        for _ in range(max_frames // 2):
            self.emu.run(mask, 2)
            if (self.game.location(), self.game.pos()) != before:
                # let the step finish so the next read is stable
                for _ in range(16):
                    if self.game.avatar()["tile_transition"] == 0:
                        break
                    self.emu.run(0, 1)
                return True
            if self.game.mode().kind != "overworld":
                return False
        self.emu.run(0, 2)
        return False

    def face(self, direction: str) -> None:
        if self.game.facing() == direction:
            return
        # A short tap turns in place; 3 frames survives the mGBA bridge's input
        # latency (1 frame was dropped there) and is still too short to walk.
        for hold in (3, 3, 5):
            self.emu.run(keymask(direction.upper()), hold)
            self.emu.run(0, 8)
            if self.game.facing() == direction:
                return

    def _execute(self, step: Step) -> bool:
        """Perform one plan step. True if we ended where the plan expected."""
        d = step.direction
        if step.action in ("cut", "smash", "surf"):
            self.face(d)
            self.press("A", release=10)
            self.pump()
            if step.action in ("cut", "smash"):
                return False          # object gone now; replan from here
            return self.state() == step.expect
        if step.action == "dive":
            # Dive down with A on dark water; surface with B while underwater.
            underwater = bool(self.game.avatar()["flags"] & 0x10)
            self.press("B" if underwater else "A", release=10)
            self.pump()
            return self.state().map == step.expect.map
        if step.action == "fly":
            ok = bool(self.fly_hook and self.fly_hook(step.expect.map))
            s = self.state()
            return ok and s.map == step.expect.map and (s.x, s.y) == (step.expect.x, step.expect.y)
        if step.action == "transport":
            self.face(d)
            self.press("A", release=10)
            self.pump()
            return self.state().map == step.expect.map
        if step.action == "bgwarp":
            self.face(d)
            self.press("A", release=10)
            self.pump()
            s = self.state()
            return s.map == step.expect.map and (s.x, s.y) == (step.expect.x, step.expect.y)
        if step.action == "waterfall":
            self.face("up")
            self.press("A", release=10)          # "Would you like to use WATERFALL?" -> yes
            self.pump()
            for _ in range(900):                 # the climb is a scripted ride
                if self.free() and self.game.avatar()["tile_transition"] == 0 \
                        and self.state() == step.expect:
                    break
                self.emu.run(0, 2)
                if not self.free():
                    self.pump()
            return self.state() == step.expect
        if step.action == "slide":
            # Onto a current / walk tile: it carries us; wait for the ride to end.
            self._hold_until_moved(d, max_frames=40)
            still, last = 0, None
            for _ in range(600):
                self.emu.run(0, 1)
                if not self.free():
                    break
                pos = self.game.pos()
                moving = self.game.avatar()["tile_transition"] != 0
                still = still + 1 if (pos == last and not moving) else 0
                last = pos
                if still >= 12:
                    break
            self.stats["steps"] += 1
            return self.state() == step.expect
        if step.action == "door":
            self.face("up")
            moved = self._hold_until_moved("up", max_frames=90)
            self.pump()
            return moved and self.game.map_id() == step.expect.map
        moved = self._hold_until_moved(d, max_frames=60 if step.action != "walk" else 40)
        self.stats["steps"] += 1
        if step.action in ("warp", "arrow", "edge"):
            # Map changes take a fade; judge by where we end up, not by timing.
            self.pump()
            s = self.state()
            return s.map == step.expect.map and (s.x, s.y) == (step.expect.x, step.expect.y)
        if not moved:
            return False
        s = self.state()
        return s.map == step.expect.map and (s.x, s.y) == (step.expect.x, step.expect.y)

    def _hold_segment(self, seg: list[Step]) -> bool:
        """Walk a straight run of steps holding the direction continuously.

        Releasing between tiles makes every step a stop-start; holding keeps
        running/biking speed (the Mach Bike needs momentum on cracked floors).
        Released the moment the last tile is entered, so it never overshoots.
        """
        d = seg[0].direction
        goal = seg[-1].expect
        run = self.game.flag(self.RUN_FLAG) and not self.game.surfing() \
            and not self.game.on_bike()
        mask = keymask(d.upper(), *(["B"] if run else []))
        expected = {(st.expect.x, st.expect.y) for st in seg}
        start = self.game.pos()
        last, still = start, 0
        for _ in range(len(seg) * 24 + 30):
            self.emu.run(mask, 1)
            pos = self.game.pos()
            if pos == (goal.x, goal.y) and self.game.map_id() == goal.map:
                break
            if self.game.mode().kind != "overworld":
                self.emu.run(0, 1)
                return False
            if pos != last:
                if pos not in expected:
                    self.emu.run(0, 1)
                    return False
                last, still = pos, 0
            else:
                still += 1
                if still > 40:            # blocked (NPC stepped in, etc.)
                    self.emu.run(0, 1)
                    return False
        self.emu.run(0, 1)
        for _ in range(24):              # finish the last step
            if self.game.avatar()["tile_transition"] == 0:
                break
            self.emu.run(0, 1)
        self.stats["steps"] += len(seg)
        return self.state() == goal

    def _run_steps(self, steps: list[Step], start: State) -> bool:
        """Execute steps; False at the first surprise (the caller decides)."""
        i = 0
        while i < len(steps):
            step = steps[i]
            # Straight runs of plain walking: one continuous hold.
            if step.action == "walk":
                j = i
                while (j + 1 < len(steps) and steps[j + 1].action == "walk"
                       and steps[j + 1].direction == step.direction
                       and steps[j + 1].expect.map == step.expect.map):
                    j += 1
                if j > i:
                    before = self.state()
                    fights = self.stats["battles"]
                    if not self._hold_segment(steps[i:j + 1]):
                        if not self.free():
                            self.pump()
                        # Stopped just short of a trigger tile on the way?
                        now = self.state()
                        prev = before
                        for st in steps[i:j + 1]:
                            if (prev.x, prev.y) == (now.x, now.y):
                                self._learn_pushback(prev, st, fights)
                                break
                            prev = st.expect
                        return False
                    if not self.free():
                        self.pump()
                        return False
                    i = j + 1
                    continue
            i += 1
            before = self.state()
            fights = self.stats["battles"]
            if not self._execute(step):
                log.debug("STEP %s %s expected %s got %s (mode %s)", step.action, step.direction,
                          step.expect, self.state(), self.game.mode().kind)
                self._learn_pushback(before, step, fights)
                return False
            if not self.free():
                # a script started (trainer, trigger...): let it finish,
                # then check it did not turn us back
                self.pump()
                self._learn_pushback(before, step, fights)
                return False
        return True

    def _learn_pushback(self, before: State, step: Step, fights: int | None = None) -> None:
        """A step onto a trigger tile that ran a script and did not leave us
        where planned means the trigger turned us back: never plan across that
        trigger (or its siblings with the same script) again this milestone.
        Not if a battle happened meanwhile: a trainer stopped us, not the tile."""
        if fights is not None and self.stats["battles"] != fights:
            return
        e = step.expect
        if e.map != before.map:
            return
        coords = [c for c in maps()[e.map]["coords"] if c.get("type") == "trigger"]
        hit = next((c for c in coords if (c["x"], c["y"]) == (e.x, e.y)), None)
        if hit is None:
            return
        now = self.state()
        if (now.x, now.y) == (e.x, e.y) or now.map != e.map:
            return
        if abs(now.x - before.x) + abs(now.y - before.y) > 2:
            return          # moved somewhere else entirely (warp, cutscene)
        tiles = {(e.map, c["x"], c["y"]) for c in coords if c["script"] == hit["script"]}
        log.info("LEARN trigger %s at (%d,%d) turned us back - avoiding %d tile(s)",
                 hit["script"], e.x, e.y, len(tiles))
        self.planner.learned_blocks |= tiles

    def goto(self, goal, caps: NavCaps | None = None, max_replans: int = 30,
             desc: str = "") -> None:
        """Walk until goal(State) holds, replanning around surprises."""
        outer, self.goal_desc = getattr(self, "goal_desc", ""), desc or goal.__name__
        try:
            self._goto(goal, caps, max_replans, desc)
        finally:
            self.goal_desc = outer

    def _goto(self, goal, caps: NavCaps | None, max_replans: int, desc: str) -> None:
        failures = 0
        last_state = None
        tried_triggers: set = set()
        plan: list[Step] | None = None
        started = time.process_time()
        while True:
            self.pump()
            s = self.state()
            if time.process_time() - started > 300:
                # Searches that keep failing across the whole world are what
                # take this long; give the route layer a chance instead.
                raise Stuck(f"gave up on {desc or goal.__name__} after 5 minutes at {s}")
            if goal(s):
                return
            # Resume the current plan after an interruption (a battle, a
            # message) if we are still standing on it: re-planning long
            # cross-region routes is the expensive part.
            if plan:
                where = next((i for i, st in enumerate(plan) if st.expect == s), None)
                if where is not None and where + 1 < len(plan):
                    rest = plan[where + 1:]
                    if self._run_steps(rest, s):
                        plan = None
                    else:
                        plan = rest
                        failures += 1
                        if failures > max_replans:
                            raise Stuck(f"could not reach {desc or goal.__name__}; "
                                        f"last at {self.state()}")
                    continue
            plan = None
            if self.health_check and not self._in_health_check:
                self._in_health_check = True
                try:
                    if self.health_check():
                        continue          # we detoured to heal; replan from here
                finally:
                    self._in_health_check = False
            live = MapGrid.from_ram(self.game)
            use = caps or self.nav_caps()
            # Fallbacks search only the map corridor around the shortest map
            # path (cheap to fail); the world-wide search runs once, last.
            self.planner.narrow = True
            plan = self.planner.plan(s, goal, use, live=live, live_objects=self.game.objects())
            if plan is None and use.active_triggers_block:
                # The only way (or the goal itself) is across a story trigger:
                # relax triggers on this map first, then everywhere.
                for relaxed in ({"trigger_exempt_map": s.map}, {"active_triggers_block": False}):
                    soft = NavCaps(**{**use.__dict__, **relaxed})
                    plan = self.planner.plan(s, goal, soft, live=live,
                                             live_objects=self.game.objects())
                    if plan is not None:
                        break
            if plan is None:
                before = (s, sorted((o.x, o.y) for o in self.game.objects()))
                if self._try_boulders(s, goal, use):
                    if (self.state(), sorted((o.x, o.y) for o in self.game.objects())) == before:
                        failures += 1          # the push sequence went nowhere
                        if failures > max_replans:
                            raise Stuck(f"boulders on {s.map} would not move")
                    continue
            if plan is None and use.strength:
                # Boulders on a later map: head there; the push puzzle is
                # solved on arrival (_try_boulders, current map only).
                for extra in ({}, {"active_triggers_block": False}):
                    shove = NavCaps(**{**use.__dict__, "ignore_boulders": True, **extra})
                    plan = self.planner.plan(s, goal, shove, live=live,
                                             live_objects=self.game.objects())
                    if plan is not None:
                        break
            if plan is None and self._try_story_triggers(s, tried_triggers):
                continue
            if plan is None:
                # Story NPCs (hide-flagged) may be what blocks us; walk up to
                # them -- the trigger that moves them is usually right there.
                bold = NavCaps(**{**use.__dict__, "ignore_story_objects": True,
                                  "active_triggers_block": False})
                plan = self.planner.plan(s, goal, bold, live=live,
                                         live_objects=self.game.objects())
                if plan and self._talk_to_blocker(s, plan, tried_triggers):
                    plan = None
                    continue
            self.planner.narrow = False
            if plan is None:
                objs = self.game.objects()
                for extra in ({}, {"active_triggers_block": False},
                              {"active_triggers_block": False, "ignore_boulders": use.strength},
                              {"active_triggers_block": False, "ignore_story_objects": True}):
                    plan = self.planner.plan(s, goal, NavCaps(**{**use.__dict__, **extra}),
                                             live=live, live_objects=objs)
                    if plan is not None:
                        break
            if plan is None:
                # Maybe an NPC is standing in the only corridor: wait, retry.
                failures += 1
                if failures > 6:
                    raise Stuck(f"no route to {desc or goal.__name__} from {s}")
                self.idle(30)
                continue
            if s != last_state:
                log.info("GOTO %s: %d steps from %s (%d,%d)", desc or goal.__name__,
                         len(plan), s.map, s.x, s.y)
                last_state = s
            ok = self._run_steps(plan, s)
            if ok:
                plan = None
            if not ok:
                failures += 1
                if failures > max_replans:
                    raise Stuck(f"could not reach {desc or goal.__name__}; last at {self.state()}")

    def _talk_to_blocker(self, s: State, plan: list[Step], tried: set) -> bool:
        """The only route walks through a trainer or story NPC standing in a
        doorway (the Space Center stair grunt): talk to them once -- beating
        or hearing them out is what moves them."""
        spots = {(o.x, o.y): o for o in self.game.objects() if not o.is_player}
        for st in plan:
            if st.expect.map != s.map:
                break
            o = spots.get((st.expect.x, st.expect.y))
            if o is None or ("blocker", s.map, o.local_id) in tried:
                continue
            t = next((t for t in maps()[s.map]["objects"] if t["local_id"] == o.local_id), None)
            if t is None or not t["script"] or t["script"] == "0x0":
                continue
            if t.get("trainer_type", "TRAINER_TYPE_NONE") in ("TRAINER_TYPE_NONE", "0") \
                    and t["flag"] in ("0", ""):
                continue
            tried.add(("blocker", s.map, o.local_id))
            log.info("BLOCKER %s at (%d,%d) is in the way; talking to it", t["script"], o.x, o.y)
            self.talk(s.map, o.local_id)
            return True
        return False

    def _try_story_triggers(self, s: State, tried: set) -> bool:
        """No route: step on the nearest reachable active trigger on this map
        that we have not tried yet (scripts often clear a blockade)."""
        obs = self.planner.obstacles(s.map, self.game.objects())
        live = MapGrid.from_ram(self.game)
        best = None
        for t in obs.triggers:
            if (s.map, t) in tried or t == (s.x, s.y):
                continue
            p = self.planner.plan(s, at(s.map, *t), NavCaps(**{**self.nav_caps().__dict__,
                                                               "active_triggers_block": True}),
                                  live=live, live_objects=self.game.objects())
            if p is not None and (best is None or len(p) < len(best[1])):
                best = (t, p)
        if best is None:
            return False
        tried.add((s.map, best[0]))
        script = next((c["script"] for c in maps()[s.map]["coords"]
                       if (c["x"], c["y"]) == best[0]), "?")
        log.info("STORY no route; stepping on trigger %s (%s) to see if it opens one",
                 best[0], script)
        for step in best[1]:
            if not self._execute(step) or not self.free():
                break
        self.pump()
        return True

    def _try_boulders(self, s: State, goal, caps: NavCaps) -> bool:
        """No route: if Strength boulders are in the way on this map, solve the
        push puzzle to wherever the boulder-free plan leaves this map."""
        from .puzzles import solve_boulders
        if not caps.strength:
            return False
        live = MapGrid.from_ram(self.game)
        objs = self.game.objects()
        obs = self.planner.obstacles(s.map, objs)
        if not obs.boulders:
            return False
        # Breakable rocks are part of these puzzles (Seafloor Cavern): the push
        # solver treats them as walls, so clear every rock we can reach first.
        if caps.smash and obs.rocks:
            done = self.__dict__.setdefault("_smashed", set())
            for rx, ry in sorted(obs.rocks, key=lambda r: abs(r[0] - s.x) + abs(r[1] - s.y)):
                key = (s.map, self._map_visit, rx, ry)   # rocks respawn on re-entry
                if key in done:
                    continue
                near = self.planner.plan(s, adjacent(s.map, rx, ry), caps, live=live,
                                         live_objects=objs)
                if near is None or any(st.expect.map != s.map for st in near):
                    # Out of reach from this side. Never detour through another
                    # floor to get there: coming back respawns every rock.
                    done.add(key)
                    continue
                tries = self.__dict__.setdefault("_smash_tries", {})
                tries[key] = tries.get(key, 0) + 1
                if tries[key] > 2:
                    done.add(key)              # smashing it keeps failing: route around
                    continue
                log.info("BOULDERS smashing rock at (%d,%d) on %s first", rx, ry, s.map)
                if near and not self._run_steps(near, s):
                    return True                # interrupted: try again next time
                here = self.state()
                if abs(here.x - rx) + abs(here.y - ry) == 1:
                    d = facing_dir(here.x, here.y, rx, ry)
                    self._execute(Step("smash", d, State(s.map, rx, ry, here.elev)))
                if not any((o.x, o.y) == (rx, ry) for o in self.game.objects()
                           if not o.is_player):
                    done.add(key)              # really gone
                return True                    # replan with the rock gone
        loose = NavCaps(**{**caps.__dict__, "ignore_boulders": True,
                           "active_triggers_block": False, "ignore_story_objects": True})
        plan = self.planner.plan(s, goal, loose, live=live, live_objects=objs)
        if not plan:
            return False
        # The last tile of this map the ideal route stands on.
        target = (s.x, s.y)
        for st in plan:
            if st.expect.map != s.map:
                break
            target = (st.expect.x, st.expect.y)
        walls = obs.walls | obs.trees | obs.rocks
        path = solve_boulders(live, (s.x, s.y), frozenset(obs.boulders),
                              lambda x, y, bs: (x, y) == target, walls, s.elev)
        rocks = set()
        if not path and caps.smash and obs.rocks:
            # Rocks we cannot reach yet: plan as if smashable on the way.
            rocks = set(obs.rocks)
            path = solve_boulders(live, (s.x, s.y), frozenset(obs.boulders),
                                  lambda x, y, bs: (x, y) == target,
                                  obs.walls | obs.trees, s.elev)
        if not path:
            log.info("BOULDERS no push sequence to %s on %s", target, s.map)
            return False
        log.info("BOULDERS %d moves to reach %s on %s", len(path), target, s.map)
        boulders = set(obs.boulders)
        for d in path:
            x, y = self.game.pos()
            dx, dy = DELTA[d]
            if (x + dx, y + dy) in boulders:
                self.face(d)
                if not self.game.flag("FLAG_SYS_USE_STRENGTH"):
                    self.press("A", release=10)      # "use STRENGTH?" -> yes
                    self.pump()
                for _ in range(40):
                    self.emu.run(keymask(d.upper()), 2)
                    now = {(o.x, o.y) for o in self.game.objects()
                           if (o.x, o.y) == (x + 2 * dx, y + 2 * dy)}
                    if now:
                        break
                self.emu.run(0, 20)
                boulders.discard((x + dx, y + dy))
                boulders.add((x + 2 * dx, y + 2 * dy))
            elif (x + dx, y + dy) in rocks:
                self._execute(Step("smash", d, State(s.map, x + dx, y + dy, s.elev)))
                rocks.discard((x + dx, y + dy))
                self.pump()
                if not self._hold_until_moved(d):
                    return True
            elif not self._hold_until_moved(d):
                self.pump()
                return True        # replan from wherever we are
        return True

    def goto_puzzle(self, goal, desc: str = "", max_depth: int = 10) -> None:
        """goto, but if the goal is walled off, search the map's switch tiles.

        Gym puzzles (Mauville's barriers, etc.) are coord-event switches that
        rewrite the live grid. A depth-first search over switch sequences --
        using savestates to back out of dead ends -- finds a working order
        without any hand-written solution. Paths never cross a switch except
        on purpose.
        """
        block = NavCaps(**{**self.nav_caps().__dict__, "triggers_block": True})

        def reachable() -> bool:
            s = self.state()
            return goal(s) or self.planner.plan(
                s, goal, block, live=MapGrid.from_ram(self.game),
                live_objects=self.game.objects()) is not None

        def switches() -> list[tuple[int, int]]:
            s = self.state()
            live = MapGrid.from_ram(self.game)
            out = []
            for c in maps()[s.map]["coords"]:
                if c.get("type") != "trigger":
                    continue
                t = (c["x"], c["y"])
                if t == (s.x, s.y):
                    out.append((2, t))     # step off and back on: press again
                    continue
                p = self.planner.plan(s, at(s.map, *t), block_to(t), live=live,
                                      live_objects=self.game.objects())
                if p is not None:
                    out.append((len(p), t))
            return [t for _, t in sorted(out)]

        def block_to(t):
            # walls on every switch except the one we are heading for
            return NavCaps(**{**block.__dict__, "triggers_block": False, "avoid_triggers": True})

        def key() -> tuple:
            # Puzzle state: where the objects stand, the live grid, and us.
            st = self.state()
            objs = tuple(sorted((o.local_id, o.x, o.y) for o in self.game.objects()
                                if not o.is_player))
            return (st.map, st.x, st.y, objs, tuple(sorted(self.game.object_templates().items())),
                    hash(tuple(MapGrid.from_ram(self.game).tiles)))

        # Breadth-first over switch presses, deduplicated by puzzle state, so
        # the shortest sequence wins and revisited layouts cost nothing.
        self.pump()
        if not reachable():
            frontier = [self.emu.save_state()]
            seen = {key()}
            solved = False
            for depth in range(1, max_depth + 1):
                nxt = []
                for snap in frontier:
                    self.emu.load_state(snap)
                    for t in switches():
                        self.emu.load_state(snap)
                        log.info("PUZZLE try switch %s (depth %d) for %s", t, depth, desc)
                        try:
                            here = self.state()
                            if (here.x, here.y) == t:
                                self.goto(adjacent(here.map, *t), caps=block_to(t), desc="step off")
                            self.goto(at(self.state().map, *t), caps=block_to(t), desc="switch")
                            self.pump()
                        except Stuck:
                            continue
                        k = key()
                        if k in seen:
                            continue
                        seen.add(k)
                        if reachable():
                            solved = True
                            break
                        nxt.append(self.emu.save_state())
                    if solved:
                        break
                if solved or not nxt:
                    break
                frontier = nxt
            if not solved:
                raise Stuck(f"puzzle: could not open a way to {desc}")
        self.goto(goal, caps=block, desc=desc)

    # -- interacting --------------------------------------------------------------
    def live_object(self, local_id: int):
        return next((o for o in self.game.objects() if o.local_id == local_id and not o.is_player),
                    None)

    def _talk_offset(self, map_id: str, sx: int, sy: int, ox: int, oy: int) -> bool:
        """Can someone at (sx,sy) talk to (ox,oy)? Adjacent, or across a counter."""
        d = abs(ox - sx) + abs(oy - sy)
        if d == 1:
            return True
        if d == 2 and (ox == sx or oy == sy):
            mx, my = (sx + ox) // 2, (sy + oy) // 2
            g = self.planner.grid(map_id)
            from .mapgrid import MB
            return g.inside(mx, my) and g.behavior(mx, my) == MB["MB_COUNTER"]
        return False

    def talk(self, map_id: str, local_id: int, max_steps: int = 120,
             pump_after: bool = True) -> None:
        """Walk up to an NPC and press A facing them.

        NPCs wander, so this chases: one planned step at a time toward where
        the NPC is *now*, pressing A the moment we are adjacent.
        """
        template = next((t for t in maps()[map_id]["objects"] if t["local_id"] == local_id), None)
        self.goto(at(map_id), desc=f"{map_id}")
        for _ in range(max_steps):
            self.pump()
            o = self.live_object(local_id)
            if o is None:
                if template is None:
                    raise Stuck(f"object {local_id} is not on {map_id}")
                # Objects only spawn near the camera: head for the map data position.
                self.goto(_on(map_id, lambda s: s.map == map_id and abs(s.x - template["x"]) <= 3
                          and abs(s.y - template["y"]) <= 3), desc=f"towards object {local_id}")
                if self.live_object(local_id) is None:
                    raise Stuck(f"object {local_id} is not on {map_id}")
                continue
            s = self.state()
            if s.map != map_id or abs(o.x - s.x) + abs(o.y - s.y) > 3:
                # far away: walk the whole way, then chase once close
                # far away: walk the whole way to a talking spot, then chase.
                # (Merely "close" can be a dead end on the wrong side of a wall.)
                ox, oy = o.x, o.y
                try:
                    self.goto(_on(map_id, lambda st: st.map == map_id
                              and self._talk_offset(map_id, st.x, st.y, ox, oy)),
                              desc=f"to talk to object {local_id}")
                except Stuck:
                    self.goto(_on(map_id, lambda st: st.map == map_id
                              and abs(st.x - ox) + abs(st.y - oy) <= 2),
                              desc=f"near object {local_id}")
                continue
            if s.map == map_id and self._talk_offset(map_id, s.x, s.y, o.x, o.y):
                self.face(facing_dir(s.x, s.y, o.x, o.y))
                o2 = self.live_object(local_id)
                if o2 and (o2.x, o2.y) == (o.x, o.y):
                    self.press("A", release=10)
                    if self.game.mode().kind != "overworld":
                        if pump_after:
                            self.pump()
                        return
                continue
            ox, oy = o.x, o.y
            goal = (lambda st: st.map == map_id and
                    self._talk_offset(map_id, st.x, st.y, ox, oy))
            plan = self.planner.plan(s, goal, self.nav_caps(),
                                     live=MapGrid.from_ram(self.game),
                                     live_objects=self.game.objects())
            if not plan:
                self.idle(20)          # blocked for now; let it wander
                continue
            self._execute(plan[0])
        raise Stuck(f"could not talk to object {local_id} on {map_id}")

    def interact(self, map_id: str, x: int, y: int, direction: str | None = None) -> None:
        """Face tile (x, y) from a neighbour and press A (signs, PCs, items)."""
        if direction:
            dx, dy = DELTA[direction]
            self.goto(at(map_id, x - dx, y - dy), desc=f"{map_id}({x - dx},{y - dy})")
        else:
            self.goto(adjacent(map_id, x, y), desc=f"next to {map_id}({x},{y})")
        s = self.state()
        self.face(direction or facing_dir(s.x, s.y, x, y))
        self.press("A", release=10)
        self.pump()
