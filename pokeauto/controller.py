"""Turning intentions into button presses, safely.

`Controller.pump()` is the heart of robustness: it runs the game forward and
deals with whatever is on screen -- dialogue, yes/no prompts, multichoice
menus, battles, evolutions, special screens -- until the player is standing in
the overworld with free control. Every higher-level action (walk somewhere,
talk to someone) calls it between moves, so an unexpected trainer, a phone
call or a story cutscene is handled in one place instead of breaking a plan.

What to answer at a prompt is decided by `Prompts` rules on the prompt text
(code), with an optional Jev fallback for text no rule recognises.
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

    First match wins. Anything unmatched is answered YES (the story almost
    always wants yes) unless an `unknown` callback (Jev) is installed.
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
    unknown: object = None       # callable(text) -> bool | None

    def answer(self, text: str) -> bool:
        for pattern, ans in self.overrides + self.rules:
            if re.search(pattern, text, re.I):
                return ans
        if self.unknown:
            verdict = self.unknown(text)
            if verdict is not None:
                return verdict
        return True


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class Controller:
    RUN_FLAG = "FLAG_SYS_B_DASH"

    def __init__(self, emu: Emu, game: Game, battle=None, prompts: Prompts | None = None,
                 on_unknown_screen=None):
        self.emu, self.game = emu, game
        self.planner = Planner(game)
        self.battle = battle                  # set by the runner
        self.prompts = prompts or Prompts()
        self.on_unknown_screen = on_unknown_screen
        self.multichoice_handler = None       # callable(text, labels) -> index (Jev)
        self.multichoice_prefs: list[str] = []  # regexes, first matching label wins
        self.ui_handlers: dict[str, object] = {}
        self.health_check = None              # callable() -> True if it detoured to heal
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
            elif self.multichoice_handler:
                chosen = self.multichoice_handler(text, labels)
                if chosen is not None:
                    idx = chosen
        log.info("MULTICHOICE %r %s -> %s", text[-60:], labels,
                 labels[idx] if idx < len(labels) else idx)
        self._menu_select(idx)
        for _ in range(20):
            if "Task_HandleMultichoiceInput" not in self.game.active_tasks():
                break
            self.idle(2)

    def _other_step(self, m) -> None:
        tasks = m.tasks
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
        """Leave the PokeNav. The Rustboro tutorial insists on calling Mr. Stone
        first: MATCH CALL (3rd item) -> first contact -> CALL -> dismiss."""
        self._pokenav_tries = getattr(self, "_pokenav_tries", 0) + 1
        for _ in range(4):
            self.press("B", release=12)
        if "Pokenav" not in S.name_at(self.game.callback2()):
            self._pokenav_tries = 0
            return
        if self._pokenav_tries % 2 == 0:
            for _ in range(4):
                self.press("UP", release=8)
            for key in ("DOWN", "DOWN", "A"):
                self.press(key, release=20)
            self.idle(40)
            for key in ("A", "A"):             # contact -> CALL
                self.press(key, release=30)
            for _ in range(8):                 # dismiss the conversation
                self.press("A", release=20)

    # -- field abilities ------------------------------------------------------
    def nav_caps(self, avoid_grass: float = 0.5) -> NavCaps:
        party = self.game.party()
        knows = lambda mv: any(p.knows(mv) for p in party)
        f = self.game.flag
        return NavCaps(
            surf=knows("MOVE_SURF") and f("FLAG_BADGE05_GET"),
            cut=knows("MOVE_CUT") and f("FLAG_BADGE01_GET"),
            smash=knows("MOVE_ROCK_SMASH") and f("FLAG_BADGE03_GET"),
            waterfall=knows("MOVE_WATERFALL") and f("FLAG_BADGE08_GET"),
            avoid_grass=avoid_grass,
        )

    def state(self) -> State:
        x, y = self.game.pos()
        return State(self.game.map_id(), x, y, self.game.player_elevation(),
                     self.game.surfing())

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
        for _ in range(3):
            self.emu.run(keymask(direction.upper()), 1)
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
        if step.action == "door":
            self.face("up")
            moved = self._hold_until_moved("up", max_frames=90)
            self.pump()
            return moved and self.game.map_id() == step.expect.map
        moved = self._hold_until_moved(d, max_frames=60 if step.action != "walk" else 40)
        if step.action in ("warp", "arrow", "edge"):
            self.pump()
        self.stats["steps"] += 1
        if not moved:
            return False
        s = self.state()
        return s.map == step.expect.map and (s.x, s.y) == (step.expect.x, step.expect.y)

    def goto(self, goal, caps: NavCaps | None = None, max_replans: int = 30,
             desc: str = "") -> None:
        """Walk until goal(State) holds, replanning around surprises."""
        failures = 0
        last_state = None
        while True:
            self.pump()
            s = self.state()
            if goal(s):
                return
            if self.health_check and not self._in_health_check:
                self._in_health_check = True
                try:
                    if self.health_check():
                        continue          # we detoured to heal; replan from here
                finally:
                    self._in_health_check = False
            live = MapGrid.from_ram(self.game)
            plan = self.planner.plan(s, goal, caps or self.nav_caps(), live=live,
                                     live_objects=self.game.objects())
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
            ok = True
            for step in plan:
                if not self._execute(step):
                    ok = False
                    break
                if not self.free():
                    ok = False
                    break
            if not ok:
                failures += 1
                if failures > max_replans:
                    raise Stuck(f"could not reach {desc or goal.__name__}; last at {self.state()}")

    def goto_puzzle(self, goal, desc: str = "", max_depth: int = 5) -> None:
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
                    continue
                p = self.planner.plan(s, at(s.map, *t), block_to(t), live=live,
                                      live_objects=self.game.objects())
                if p is not None:
                    out.append((len(p), t))
            return [t for _, t in sorted(out)]

        def block_to(t):
            # walls on every switch except the one we are heading for
            return NavCaps(**{**block.__dict__, "triggers_block": False, "avoid_triggers": True})

        def search(depth: int, used: tuple) -> bool:
            self.pump()
            if reachable():
                return True
            if depth == 0:
                return False
            for t in switches():
                if t in used[-1:]:
                    continue
                snap = self.emu.save_state()
                log.info("PUZZLE try switch %s (depth %d) for %s", t, max_depth - depth + 1, desc)
                try:
                    self.goto(at(self.state().map, *t), caps=block_to(t), desc="switch")
                    if search(depth - 1, used + (t,)):
                        return True
                except Stuck:
                    pass
                self.emu.load_state(snap)
            return False

        if not search(max_depth, ()):
            raise Stuck(f"puzzle: could not open a way to {desc}")
        self.goto(goal, caps=block, desc=desc)

    # -- interacting --------------------------------------------------------------
    def live_object(self, local_id: int):
        return next((o for o in self.game.objects() if o.local_id == local_id and not o.is_player),
                    None)

    def talk(self, map_id: str, local_id: int, max_steps: int = 120) -> None:
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
                self.goto(lambda s: s.map == map_id and abs(s.x - template["x"]) <= 3
                          and abs(s.y - template["y"]) <= 3, desc=f"towards object {local_id}")
                if self.live_object(local_id) is None:
                    raise Stuck(f"object {local_id} is not on {map_id}")
                continue
            s = self.state()
            if s.map == map_id and abs(o.x - s.x) + abs(o.y - s.y) == 1:
                self.face(facing_dir(s.x, s.y, o.x, o.y))
                o2 = self.live_object(local_id)
                if o2 and (o2.x, o2.y) == (o.x, o.y):
                    self.press("A", release=10)
                    if self.game.mode().kind != "overworld":
                        self.pump()
                        return
                continue
            plan = self.planner.plan(s, adjacent(map_id, o.x, o.y), self.nav_caps(),
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
