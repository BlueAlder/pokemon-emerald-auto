"""Run control shared between the worker thread (which owns the emulator) and a UI.

The emulator core is not thread-safe, so only the worker thread touches the
Game/Agent/emulator. It calls `RunControl.on_frames` after every emulator
run() call; that hook publishes a copied `Snapshot` (throttled), blocks while
paused, saves requested checkpoints and raises `StopRun` when asked to stop.
The UI only calls the thread-safe methods: snapshot(), drain_log(), pause(),
resume(), stop(), request_save().
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Callable

log = logging.getLogger("pokeauto")

ENDED = ("finished", "failed", "stopped")

# Log keywords that describe what the player is doing right now.
ACTIVITY = ("GOTO", "GRIND", "BATTLE", "FLEW", "FLY", "HEALED", "HEALTH", "CAUGHT",
            "TEACH", "LEARN", "SHOPPED", "BOULDERS", "PUZZLE", "STORY", "BLOCKER", "ITEM")


class StopRun(BaseException):
    """Raised inside the emulator hook to unwind the run.

    A BaseException, so RouteRunner's `except Exception` retry loop lets it through.
    """


@dataclass(frozen=True)
class MoveView:
    name: str
    pp: int


@dataclass(frozen=True)
class GrindView:
    """Who is grinding, to what level, and how far along."""
    species: str
    level: int
    start: int
    target: int
    why: str               # "lead target L74", "team target L66"
    battles: int
    map_id: str
    progress: float        # 0..1 of the experience from start to target level


@dataclass(frozen=True)
class MonView:
    species: str
    level: int
    hp: int
    max_hp: int
    status: str            # "", "SLP", "PSN", "BRN", "FRZ", "PAR", "TOX"
    moves: tuple[MoveView, ...]
    item: str = ""
    is_egg: bool = False

    @property
    def fainted(self) -> bool:
        return self.hp == 0 and not self.is_egg

    @property
    def hp_frac(self) -> float:
        return self.hp / self.max_hp if self.max_hp else 0.0


@dataclass(frozen=True)
class Snapshot:
    state: str = "starting"        # starting/running/paused/finished/failed/stopped
    backend: str = "headless"
    frame: int = 0
    t_start: float = 0.0
    t_end: float | None = None
    milestone: str = ""
    hint: str = ""
    index: int = -1                # 0-based index of the active milestone
    total: int = 0
    attempt: int = 0
    attempts: int = 0
    min_level: int = 0
    team_level: int = 0
    important: bool = False
    done: tuple[str, ...] = ()     # milestones finished this session
    activity: str = ""
    badges: int = 0
    money: int | None = None
    play_time: str = ""
    map_id: str = ""
    pos: tuple[int, int] | None = None
    stats: dict = field(default_factory=dict)
    party: tuple[MonView, ...] = ()
    message: str = ""
    goal_desc: str = ""            # where the current walk is headed
    grind: GrindView | None = None
    team: tuple[tuple[str, int], ...] = ()   # (species, level) being trained to team_target
    team_target: int = 0
    whiteouts: int = 0             # battles lost (the whole party fainted)
    faints: int = 0                # our Pokemon knocked out
    report: object = None          # runstats.Report once the run has ended


def _target(m, name: str, agent) -> int:
    """A milestone's level target (plain, or a callable of the agent)."""
    t = getattr(m, name, 0)
    try:
        return int(t(agent)) if callable(t) else int(t)
    except Exception:
        return 0


def grind_view(agent, party) -> GrindView | None:
    """agent.grinding (set while grind_to runs) plus experience progress."""
    g = getattr(agent, "grinding", None)
    if not g:
        return None
    mon = next((m for m in party if m.personality == g["personality"]), None)
    if mon is None:
        return None
    progress = 0.0
    try:
        growth = agent.data.species(mon.species).growth
        lo = agent.data.exp_for_level(growth, g["start"])
        hi = agent.data.exp_for_level(growth, g["target"])
        progress = min(1.0, max(0.0, (mon.exp - lo) / max(1, hi - lo)))
    except Exception:
        pass
    return GrindView(species=g["species"], level=mon.level, start=g["start"],
                     target=g["target"], why=g.get("why", ""), battles=g.get("battles", 0),
                     map_id=g.get("map", ""), progress=progress)


def status_name(status1: int) -> str:
    if status1 & 7:
        return "SLP"
    for bit, name in ((7, "TOX"), (3, "PSN"), (4, "BRN"), (5, "FRZ"), (6, "PAR")):
        if status1 & (1 << bit):
            return name
    return ""


def pretty_const(name: str, prefix: str) -> str:
    """'ITEM_KINGS_ROCK' -> 'Kings Rock'."""
    if name.startswith(prefix):
        name = name[len(prefix):]
    return " ".join(w.capitalize() if w.isalpha() else w for w in name.split("_"))


def map_name(map_id: str) -> str:
    """'MAP_ROUTE110_TRICK_HOUSE_ENTRANCE' -> 'Route 110 Trick House Entrance'."""
    name = map_id[4:] if map_id.startswith("MAP_") else map_id
    name = re.sub(r"(?<=[A-Z])(?=\d+(?:_|$))", "_", name)   # ROUTE110 -> ROUTE_110
    return pretty_const(name, "")


def mon_view(m) -> MonView:
    from .symbols import const_names
    item = const_names()["ITEM_"].get(m.item, "") if m.item else ""
    return MonView(species=m.species_name, level=m.level, hp=m.hp, max_hp=m.max_hp,
                   status=status_name(m.status),
                   moves=tuple(MoveView(pretty_const(mv.const, "MOVE_"), mv.pp) for mv in m.moves),
                   item=pretty_const(item, "ITEM_") if item else "", is_egg=m.is_egg)


class QueueLogHandler(logging.Handler):
    """Feeds formatted records into the RunControl's log queue."""

    def __init__(self, control: "RunControl"):
        super().__init__()
        self.control = control

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.control.push_log(record.levelno, self.format(record), record.getMessage())
        except Exception:
            self.handleError(record)


class RunControl:
    def __init__(self, backend: str = "headless", interval: float | None = None,
                 save_manual: Callable[[object], str] | None = None):
        self.backend = backend
        self.interval = interval if interval is not None else (0.25 if backend == "headless" else 1.0)
        self.save_manual = save_manual     # (emu) -> description, called in the worker
        self.log_queue: queue.Queue[tuple[int, str]] = queue.Queue()
        self.frame = 0                     # emulator frame at the latest hook call
        self.ok: bool | None = None
        self.summary = ""
        self.report = None                 # runstats.Report, set by finish()
        self.done_event = threading.Event()   # worker thread has finished
        self._lock = threading.Lock()
        self._running = threading.Event()
        self._running.set()
        self._stop = False
        self._save = False
        self._agent = None
        self._runner = None
        self._last_pub = 0.0
        self._state = "starting"
        self._activity = ""
        self._message = ""
        self._snap = Snapshot(backend=backend, t_start=time.time())

    # -- UI side (any thread) ---------------------------------------------------------
    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._snap

    def drain_log(self, limit: int = 5000) -> list[tuple[int, str]]:
        out = []
        try:
            while len(out) < limit:
                out.append(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        return out

    @property
    def paused_requested(self) -> bool:
        return not self._running.is_set()

    @property
    def stop_requested(self) -> bool:
        return self._stop

    def pause(self) -> None:
        if not self._stop:
            self._running.clear()

    def resume(self) -> None:
        self._running.set()

    def toggle_pause(self) -> bool:
        """Pause or resume; returns True if now paused (requested)."""
        if self._running.is_set():
            self.pause()
        else:
            self.resume()
        return self.paused_requested

    def stop(self) -> None:
        self._stop = True
        self._running.set()            # unblock a paused worker so it can unwind

    def request_save(self) -> None:
        self._save = True

    # -- worker side ------------------------------------------------------------------
    def push_log(self, levelno: int, line: str, message: str = "") -> None:
        self.log_queue.put((levelno, line))
        word = message.split(" ", 1)[0]
        if word in ACTIVITY and "PROMPT" not in message[:13]:
            with self._lock:
                self._activity = message

    def attach(self, agent=None, runner=None) -> None:
        self._agent, self._runner = agent, runner

    def set_state(self, state: str) -> None:
        self._state = state
        self.publish(force=True)

    def finish(self, state: str, ok: bool, summary: str = "", report=None) -> None:
        self.ok, self.summary, self.report = ok, summary, report
        self._state = state
        self.publish(force=True)
        self.done_event.set()

    def on_frames(self, emu) -> None:
        """Emulator hook: runs in the worker thread after every emulator run()."""
        self.frame = emu.frame
        if self._stop:
            raise StopRun()
        if self._save:
            self._save = False
            self._do_save(emu)
        if not self._running.is_set():
            self._state = "paused"
            self.publish(force=True)
            log.info("PAUSED at frame %d", emu.frame)
            while not self._running.wait(0.1):
                if self._save:             # saving works while paused too
                    self._save = False
                    self._do_save(emu)
                    self.publish(force=True)
            if self._stop:
                raise StopRun()
            log.info("RESUMED")
            self._state = "running"
            self.publish(force=True)
        elif time.time() - self._last_pub >= self.interval:
            if self._state == "starting":
                self._state = "running"
            self.publish()

    def _do_save(self, emu) -> None:
        if not self.save_manual:
            self._message = "manual checkpoints are not supported here"
            log.warning("CHECKPOINT %s", self._message)
            return
        try:
            self._message = f"saved {self.save_manual(emu)}"
            log.info("CHECKPOINT %s", self._message)
        except Exception as exc:          # a failed save must not end the run
            self._message = f"save failed: {exc}"
            log.error("CHECKPOINT %s", self._message)

    def publish(self, force: bool = False) -> None:
        """Build a snapshot from the game and publish it. Worker thread only."""
        now = time.time()
        if not force and now - self._last_pub < self.interval:
            return
        self._last_pub = now
        with self._lock:
            prev, activity = self._snap, self._activity
        kw: dict = {"state": self._state, "frame": self.frame, "activity": activity,
                    "message": self._message, "report": self.report,
                    "t_end": now if self._state in ENDED else None}
        r = self._runner
        if r is not None:
            m = getattr(r, "active", None)
            kw.update(total=len(r.milestones), index=getattr(r, "active_index", -1),
                      attempt=getattr(r, "attempt", 0),
                      done=tuple(name for name, _ in r.history))
            if m is not None:
                kw.update(milestone=m.name, hint=m.hint, attempts=m.attempts,
                          min_level=_target(m, "min_level", self._agent),
                          team_level=_target(m, "team_level", self._agent),
                          important=m.important)
        a = self._agent
        if a is not None:
            try:
                g = a.game
                party = g.party()
                kw.update(party=tuple(mon_view(m) for m in party), badges=g.badges(),
                          money=g.money(), play_time=g.play_time(), map_id=g.map_id(),
                          pos=tuple(g.pos()), stats=dict(a.ctl.stats),
                          goal_desc=getattr(a.ctl, "goal_desc", ""),
                          whiteouts=getattr(getattr(a, "battle", None), "whiteouts", 0),
                          faints=getattr(getattr(a, "battle", None), "faints", 0),
                          grind=grind_view(a, party))
                t = getattr(a, "training", None)
                if t:
                    by_id = {m.personality: m for m in party}
                    kw.update(team_target=t["target"], team=tuple(
                        (by_id[pid].species_name, by_id[pid].level)
                        for pid in t["members"] if pid in by_id))
                else:
                    kw.update(team_target=0, team=())
            except Exception:              # save blocks are garbage before a game is loaded
                pass
        snap = replace(prev, **kw)
        with self._lock:
            self._snap = snap
