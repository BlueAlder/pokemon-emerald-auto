"""TUI and run-control checks: rendering, colours, pause/resume, quit.

The fake-run tests need no ROM; the end-to-end test plays a fresh game
headless (skipped without roms/emerald.gba).
Run:  .venv/bin/python tests/test_tui.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from argparse import Namespace
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ROM = ROOT / "roms" / "emerald.gba"

from pokeauto.runstate import (QueueLogHandler, RunControl, StopRun, map_name,  # noqa: E402
                               pretty_const, status_name)
from pokeauto.tui import LogView, PlayApp, is_routine, style_line  # noqa: E402

log = logging.getLogger("pokeauto")


# -- fakes -------------------------------------------------------------------------------

@dataclass
class FakeMove:
    const: str
    pp: int


@dataclass
class FakeMon:
    species_name: str
    level: int
    hp: int
    max_hp: int
    status: int = 0
    moves: list = field(default_factory=list)
    item: int = 0
    is_egg: bool = False


class FakeGame:
    def party(self):
        return [FakeMon("MUDKIP", 12, 30, 35, 0, [FakeMove("MOVE_WATER_GUN", 20),
                                                    FakeMove("MOVE_TACKLE", 0)]),
                FakeMon("POOCHYENA", 5, 0, 20),
                FakeMon("ZIGZAGOON", 7, 4, 22, status=1 << 3)]

    def badges(self): return 3
    def money(self): return 12345
    def play_time(self): return "1:23"
    def map_id(self): return "MAP_ROUTE110_TRICK_HOUSE_ENTRANCE"
    def pos(self): return (4, 7)


class FakeAgent:
    game = FakeGame()

    class ctl:
        stats = {"battles": 12, "steps": 3456}


@dataclass
class FakeMilestone:
    name: str
    hint: str = ""
    min_level: int = 0
    team_level: int = 0
    important: bool = False
    attempts: int = 4


class FakeRunner:
    milestones = [FakeMilestone(f"m{i}") for i in range(9)] + [
        FakeMilestone("badge_stone", "Beat Roxanne in Rustboro.", min_level=15, important=True)]
    history = [("m0", 1.0)]
    active = milestones[-1]
    active_index = 9
    attempt = 2


class FakeEmu:
    frame = 0


def fake_worker(control: RunControl, lines: int = 40) -> None:
    """Plays the part of play.run_game: emulator calls hit the hook."""
    emu = FakeEmu()
    control.attach(FakeAgent(), FakeRunner())
    try:
        i = 0
        while True:
            emu.frame += 10
            if i < lines:
                log.info(["=== MILESTONE m%d ===", "BATTLE T%d MUDKIP vs X", "GOTO x %d"][i % 3], i)
            i += 1
            control.on_frames(emu)
            time.sleep(0.002)
    except StopRun:
        control.finish("stopped", False, "finished=False")


def install_handler(control: RunControl) -> QueueLogHandler:
    h = QueueLogHandler(control)
    h.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(h)
    return h


async def wait_for(pilot, cond, timeout: float = 10.0) -> bool:
    t = time.time()
    while time.time() - t < timeout:
        if cond():
            return True
        await pilot.pause(0.05)
    return cond()


# -- tests ---------------------------------------------------------------------------------

def test_helpers():
    assert map_name("MAP_ROUTE110_TRICK_HOUSE_ENTRANCE") == "Route 110 Trick House Entrance"
    assert map_name("MAP_PETALBURG_CITY_GYM") == "Petalburg City Gym"
    assert map_name("MAP_SEAFLOOR_CAVERN_ROOM1") == "Seafloor Cavern Room 1"
    assert map_name("MAP_VICTORY_ROAD_B1F") == "Victory Road B1F"
    assert pretty_const("ITEM_KINGS_ROCK", "ITEM_") == "Kings Rock"
    assert status_name(0) == "" and status_name(2) == "SLP" and status_name(1 << 3) == "PSN"
    assert status_name(1 << 7) == "TOX" and status_name(1 << 6) == "PAR"
    assert is_routine("01:02:03 BATTLE T12 MUDKIP 3/4 vs X") and not is_routine("01:02:03 BATTLE PROMPT x")


def test_log_colours():
    def style(level, msg):
        t = style_line(level, "01:02:03 " + msg)
        assert str(t.spans[0].style) == "dim"            # the timestamp
        return str(t.spans[1].style)
    assert style(logging.INFO, "=== MILESTONE badge_stone === x") == "bold #d787ff"
    assert style(logging.INFO, "--- done badge_stone") == "bold #5fd75f"
    assert style(logging.INFO, "BATTLE T3 A vs B") == "dim #5fd7ff"
    assert style(logging.INFO, "BATTLE item 3 had no effect") == "#5fd7ff"
    assert style(logging.INFO, "GOTO MAP_X: 3 steps") == "dodger_blue1"
    assert style(logging.INFO, "GRIND MUDKIP L5 -> L9") == "#ffff5f"
    assert style(logging.INFO, "HEALED at x") == "#5fd75f"
    assert style(logging.INFO, "FLEW to x ok") == "#00ffff"
    assert style(logging.INFO, "TEACH x") == "#d787ff"
    assert style(logging.INFO, "PROMPT 'x' -> YES") == "dim"
    assert style(logging.WARNING, "milestone x stuck (attempt 1)") == "#ff8700"
    assert style(logging.ERROR, "milestone x crashed:\nTraceback ...") == "bold #ff5f5f"
    assert style(logging.INFO, "ROUTE failed at x") == "bold #ff5f5f"


def test_tui_pause_resume_quit():
    control = RunControl("headless", interval=0.05, save_manual=lambda e: "manual.state")
    h = install_handler(control)
    worker = threading.Thread(target=fake_worker, args=(control,), daemon=True)

    async def go():
        app = PlayApp(control, "test")
        async with app.run_test(size=(140, 40)) as pilot:
            worker.start()
            assert await wait_for(pilot, lambda: len(app.lines) >= 40)
            logv = app.query_one(LogView)
            assert await wait_for(pilot, lambda: len(logv.lines) >= 40)
            s = control.snapshot()
            assert s.state == "running" and s.milestone == "badge_stone" and s.badges == 3
            assert s.party[1].fainted and s.party[2].status == "PSN" and s.index == 9
            print("ok   rendered", len(logv.lines), "log lines; snapshot", s.state, s.milestone)

            logv.scroll_home(animate=False)             # scrolling up stops following
            await pilot.pause(0.2)
            assert not logv.auto_scroll
            await pilot.press("f")
            await pilot.pause(0.2)
            assert logv.auto_scroll and logv.is_vertical_scroll_end

            n = len(logv.lines)                         # b hides routine battle turns
            await pilot.press("b")
            assert len(logv.lines) < n
            await pilot.press("b")
            assert len(logv.lines) == n

            await pilot.press("space")
            assert await wait_for(pilot, lambda: control.snapshot().state == "paused")
            f0 = control.frame
            await pilot.pause(0.4)
            assert control.frame == f0, "frames advanced while paused"
            assert "show" in app.query_one("#banner").classes
            await pilot.press("s")                      # saving works while paused
            assert await wait_for(pilot, lambda: "saved" in control.snapshot().message)
            await pilot.press("p")
            assert await wait_for(pilot, lambda: control.frame > f0)
            assert control.snapshot().state == "running"
            print("ok   pause froze the emulator at frame", f0, "and resume continued")

            await pilot.press("space")                  # quit while paused
            assert await wait_for(pilot, lambda: control.snapshot().state == "paused")
            await pilot.press("q")
            assert not control.stop_requested           # first q only asks to confirm
            await pilot.press("q")
            assert await wait_for(pilot, lambda: not app.is_running)
        worker.join(5)
        assert not worker.is_alive() and control.snapshot().state == "stopped"
    try:
        asyncio.run(go())
    finally:
        logging.getLogger().removeHandler(h)


def test_tui_narrow_and_finished():
    control = RunControl("mgba", interval=0.05)
    control.attach(FakeAgent(), FakeRunner())
    control.push_log(logging.INFO, "01:02:03 " + "GOTO " + "x" * 300, "GOTO")
    control.finish("finished", True, "finished=True")

    async def go():
        app = PlayApp(control, "narrow")
        async with app.run_test(size=(40, 12)) as pilot:
            await pilot.pause(0.5)
            assert {"narrow", "tiny"} <= app.screen.classes
            assert "show" in app.query_one("#banner").classes
            await pilot.press("s")                      # unsupported: just a message
            await pilot.press("space")                  # ignored once finished
            await pilot.press("q")                      # exits at once when finished
            assert await wait_for(pilot, lambda: not app.is_running)
    asyncio.run(go())


def test_end_to_end_headless():
    """A fresh game in a worker thread: pause freezes the core, q stops the run."""
    if not ROM.exists():
        print("skip end-to-end: no ROM")
        return
    sys.path.insert(0, str(ROOT / "scripts"))
    import play
    runs = ROOT / "runs"
    (runs / "checkpoints").mkdir(parents=True, exist_ok=True)
    control = RunControl("headless", save_manual=lambda e: "unused")
    h = install_handler(control)
    args = Namespace(backend="headless", resume=None, live=5.0, stop_after=None, port=8888)
    worker = threading.Thread(target=play.run_game, args=(args, ROM, runs, control, True),
                              name="game", daemon=True)

    async def go():
        app = PlayApp(control, "e2e")
        async with app.run_test(size=(160, 50)) as pilot:
            worker.start()
            assert await wait_for(pilot, lambda: control.snapshot().frame > 3000, 60)
            await pilot.press("space")
            assert await wait_for(pilot, lambda: control.snapshot().state == "paused", 30)
            f0 = control.frame
            await pilot.pause(1.0)
            assert control.frame == f0, "the emulator ran while paused"
            s = control.snapshot()
            print(f"ok   paused at frame {f0} during {s.milestone} ({s.map_id})")
            await pilot.press("space")
            assert await wait_for(pilot, lambda: control.frame > f0 + 100, 30)
            print("ok   resumed, frame", control.frame)
            # Pause again in the middle of the first battle.
            assert await wait_for(pilot, lambda: control.snapshot().activity.startswith("BATTLE T"), 180)
            await pilot.press("space")
            assert await wait_for(pilot, lambda: control.snapshot().state == "paused", 30)
            f1 = control.frame
            await pilot.pause(1.0)
            assert control.frame == f1
            s = control.snapshot()
            print(f"ok   paused mid-battle at frame {f1}: {s.activity}")
            await pilot.press("p")
            assert await wait_for(pilot, lambda: control.frame > f1 + 100, 30)
            await pilot.press("q", "q")
            assert await wait_for(pilot, lambda: not app.is_running, 30)
        worker.join(10)
        assert not worker.is_alive(), "worker did not exit"
        assert control.snapshot().state == "stopped" and control.ok is False
        assert control.summary.startswith("finished=False"), control.summary
        print("ok   quit stopped the worker:", control.summary)
    try:
        asyncio.run(go())
    finally:
        logging.getLogger().removeHandler(h)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
