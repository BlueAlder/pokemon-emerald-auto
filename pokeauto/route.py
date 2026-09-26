"""The route: an ordered list of milestones, each with a completion test.

A Milestone's `done` predicate reads monotonic game state (flags, vars,
badges, items, party), so progress is a fact the game itself records, and a
run can resume from any save: the runner simply skips milestones already done.

`actions` are ordinary Python callables over the Agent. They should be
idempotent (goto / talk / interact), because a milestone may be retried after
a whiteout or a surprise.
"""
from __future__ import annotations

import logging
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable

from .controller import Stuck
from .symbols import const

log = logging.getLogger("pokeauto")


@dataclass
class Milestone:
    name: str
    done: Callable
    actions: list[Callable] = field(default_factory=list)
    min_level: int = 0            # grind the lead to this level first
    team_level: int = 0           # ...and the strongest team_size members (doubles, E4)
    team_size: int = 2
    heal_first: bool = True
    important: bool = False       # boss fight inside: spend items freely
    attempts: int = 4
    hint: str = ""                # plain-English goal, for logs and readers

    def run(self, agent) -> None:
        for act in self.actions:
            if self.done(agent):
                return
            act(agent)
            agent.pump()


# -- predicate helpers ---------------------------------------------------------------

def flag(name: str):
    return lambda a: a.game.flag(name)


def var_ge(name: str, value: int):
    return lambda a: a.game.var(name) >= value


def badges(n: int):
    return lambda a: a.game.badges() >= n


def has_item(name: str):
    return lambda a: a.game.has_item(name) > 0


def party_size(n: int):
    return lambda a: len(a.game.party()) >= n


def all_of(*preds):
    return lambda a: all(p(a) for p in preds)


def any_of(*preds):
    return lambda a: any(p(a) for p in preds)


def trainer_beaten(trainer: str):
    return lambda a: a.game.flag(const("TRAINER_FLAGS_START") + const(trainer))


# -- action helpers --------------------------------------------------------------------

def goto(map_id: str, x: int | None = None, y: int | None = None):
    def act(a):
        a.goto(map_id, x, y)
    act.__name__ = f"goto {map_id} {x},{y}"
    return act


def trigger(map_id: str, script: str):
    """Walk onto whichever tile of the coord-event `script` is reachable."""
    def act(a):
        from .nav import at_any
        from .symbols import maps
        events = [c for c in maps()[map_id]["coords"]
                  if (c.get("script") or "").endswith(script)]
        if not events:
            raise ValueError(f"no coord event {script!r} on {map_id}")
        tiles = [(c["x"], c["y"]) for c in events]
        on_tile = at_any(map_id, tiles)
        var, value = events[0].get("var"), events[0].get("var_value")

        def fired() -> bool:
            # The trigger's condition no longer holds: its script has run
            # (cutscenes often warp us away before we "arrive").
            try:
                return bool(var) and a.game.var(var) != int(str(value), 0)
            except (KeyError, ValueError):
                return False

        def goal(st):
            return on_tile(st) or fired()
        goal.maps = getattr(on_tile, "maps", {map_id})
        if not fired():
            a.ctl.goto(goal, desc=f"trigger {script}")
    act.__name__ = f"trigger {map_id} {script}"
    return act


def talk(map_id: str, local_id: int):
    def act(a):
        a.talk(map_id, local_id)
    act.__name__ = f"talk {map_id} #{local_id}"
    return act


def object_id(map_id: str, pattern: str) -> int:
    """Local id of the first object whose script/name/graphics contains pattern."""
    from .symbols import maps
    for o in maps()[map_id]["objects"]:
        if pattern in o["script"] or pattern == o["name"] or pattern in o["gfx"]:
            return o["local_id"]
    raise KeyError(f"no object matching {pattern!r} on {map_id}")


def talk_s(map_id: str, pattern: str):
    """Talk to the object on map_id whose script (or name/gfx) matches pattern."""
    lid = object_id(map_id, pattern)

    def act(a):
        a.talk(map_id, lid)
    act.__name__ = f"talk {map_id} {pattern}"
    return act


def interact(map_id: str, x: int, y: int, direction: str | None = None):
    def act(a):
        a.interact(map_id, x, y, direction)
    act.__name__ = f"interact {map_id} ({x},{y})"
    return act


def reachable(map_id: str):
    """Predicate: the planner can walk us to map_id from here right now."""
    from .nav import at as _at

    def pred(a):
        a.pump()
        return a.ctl.planner.plan(a.ctl.state(), _at(map_id), a.ctl.nav_caps()) is not None
    return pred


def unless(pred, *acts):
    """Run acts only if pred does not hold (e.g. take a boat unless we can walk)."""
    def act(a):
        if not pred(a):
            for x in acts:
                x(a)
                a.pump()
    act.__name__ = "unless(" + ",".join(getattr(x, "__name__", "?") for x in acts) + ")"
    return act


def answer(pattern: str, yes: bool):
    """Override the yes/no policy for prompts matching pattern (this run onward)."""
    def act(a):
        a.ctl.prompts.overrides.insert(0, (pattern, yes))
    act.__name__ = f"answer {pattern}={yes}"
    return act


def prefer(*patterns: str):
    """Set multichoice preferences (regexes on option labels) for what follows."""
    def act(a):
        a.ctl.multichoice_prefs = list(patterns)
    act.__name__ = f"prefer {patterns}"
    return act


def call(fn_name: str, *args, **kw):
    def act(a):
        getattr(a, fn_name)(*args, **kw)
    act.__name__ = f"{fn_name}{args}"
    return act


# -- runner ---------------------------------------------------------------------------------

class RouteRunner:
    def __init__(self, agent, milestones: list[Milestone], checkpoint=None):
        self.agent = agent
        self.milestones = milestones
        self.checkpoint = checkpoint      # callable(name) -> None (save a state)
        self.history: list[tuple[str, float]] = []
        # Per finished milestone, for run reports: name, wall (s since run()),
        # game (play time in s, None if unreadable), attempts (all tries, all visits).
        self.records: list[dict] = []
        self.first: str | None = None     # the first milestone this run started
        self._tries: dict[str, int] = {}
        self._open: Milestone | None = None   # started, not yet recorded as done
        # Read-only progress for status displays (never call current() from a UI).
        self.active: Milestone | None = None
        self.active_index = -1
        self.attempt = 0
        self.state = "idle"               # idle/running/finished/failed

    def current(self) -> Milestone | None:
        """The first unfinished milestone after the latest finished one.

        The route is ordered, so a later milestone being done implies the
        earlier ones are -- even if the game later clears one of their flags
        (the rival's Rayquaza call clears FLAG_DEFEATED_MAGMA_SPACE_CENTER).
        """
        last = -1
        for i in range(len(self.milestones) - 1, -1, -1):
            if self.milestones[i].done(self.agent):
                last = i
                break
        for m in self.milestones[last + 1:]:
            if not m.done(self.agent):
                return m
        return None

    def run(self, stop_after: str | None = None, max_seconds: float | None = None) -> bool:
        a = self.agent
        t0 = time.time()
        self.state = "running"
        while True:
            m = self.current()
            if self._open is not None and self._open is not m and self._open.done(a):
                # Done without its own "done" (the champion's credits end the route).
                self._record(self._open, t0)
            if m is None:
                self.state = "finished"
                log.info("ROUTE complete")
                if self.checkpoint:
                    self.checkpoint("hall_of_fame")
                return True
            self.active = m
            self.active_index = next(i for i, x in enumerate(self.milestones) if x is m)
            if max_seconds and time.time() - t0 > max_seconds:
                self.state = "failed"
                log.info("ROUTE time budget exhausted at %s", m.name)
                return False
            log.info("=== MILESTONE %s === %s", m.name, a.status_line())
            self.first = self.first or m.name
            self._open = m
            ok = False
            for attempt in range(m.attempts):
                self.attempt = attempt + 1
                self._tries[m.name] = self._tries.get(m.name, 0) + 1
                try:
                    a.before_milestone(m)
                    m.run(a)
                    a.pump()
                    if m.done(a):
                        ok = True
                        break
                    log.warning("milestone %s not done after attempt %d", m.name, attempt + 1)
                except Stuck as exc:
                    log.warning("milestone %s stuck (attempt %d): %s", m.name, attempt + 1, exc)
                    a.recover(m, exc)
                except Exception:
                    log.error("milestone %s crashed:\n%s", m.name, traceback.format_exc())
                    a.recover(m, None)
                # A loss can undo earlier milestones (whiting out resets the
                # Elite Four): go back to whatever is now first.
                if self.current() is not m:
                    log.info("ROUTE %s now depends on %s again; going back",
                             m.name, getattr(self.current(), "name", None))
                    break
            if not ok and self.current() is not m:
                continue
            if not ok:
                self.state = "failed"
                log.error("ROUTE failed at %s", m.name)
                return False
            self._record(m, t0)
            a.ctl.planner.learned_blocks.clear()      # story state changed
            if self.checkpoint:
                self.checkpoint(m.name)
            if stop_after and m.name == stop_after:
                self.state = "finished"
                return True

    def _record(self, m: Milestone, t0: float) -> None:
        a, wall = self.agent, time.time() - t0
        self._open = None
        self.history.append((m.name, wall))
        try:
            game = a.game.play_seconds()
        except Exception:
            game = None
        self.records.append({"name": m.name, "wall": round(wall, 1), "game": game,
                             "attempts": self._tries.pop(m.name, 1)})
        log.info("--- done %s (%.0fs elapsed, game %s)", m.name, wall, a.game.play_time())
