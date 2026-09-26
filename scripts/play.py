#!/usr/bin/env python3
"""Play Pokemon Emerald from power-on (or a checkpoint) to the Hall of Fame.

    python scripts/play.py                         # headless, fastest
    python scripts/play.py --resume set_clock      # from a saved checkpoint
    python scripts/play.py --backend mgba          # drive the mGBA app (watchable)
    python scripts/play.py --no-tui                # plain log lines (for pipes/CI)

In a terminal the run is shown in an interactive TUI (log on top, goal, run
and party below; space pauses, q quits). See docs/CLI.md.

Checkpoints (savestates) are written to runs/checkpoints/<milestone>.state
after each milestone; the latest frame is mirrored to runs/live.png every few
seconds so a headless run can be watched.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


DEFAULT_ROM_CANDIDATES = [ROOT / "roms" / "emerald.gba",
                          Path.home() / "Downloads" / "Pokemon - Emerald Version (USA, Europe).gba"]


def find_rom(arg: str | None) -> Path:
    candidates = [Path(arg)] if arg else []
    if os.environ.get("POKEAUTO_ROM"):
        candidates.append(Path(os.environ["POKEAUTO_ROM"]))
    for c in candidates + DEFAULT_ROM_CANDIDATES:
        if c.exists():
            return c
    sys.exit("no ROM found: pass --rom or set POKEAUTO_ROM")


def run_game(args, rom: Path, runs: Path, control, hook: bool) -> None:
    """Build the emulator and play. Every emulator/game call happens in this thread."""
    from pokeauto.agent import Agent
    from pokeauto.emerald import ROUTE
    from pokeauto.emu import BridgeError, HeadlessEmu, MgbaEmu
    from pokeauto.route import RouteRunner
    from pokeauto.runstate import StopRun
    from pokeauto.runstats import NEW_GAME, RunRecorder

    t0 = time.time()
    ok, state, outcome, error = False, "failed", "failed", None
    emu = agent = runner = recorder = None
    start = args.resume or (NEW_GAME if args.new_game or args.backend == "headless" else "mGBA save")
    try:
        try:
            recorder = RunRecorder(Path(getattr(args, "history", None) or runs / "history"),
                                   backend=args.backend, start_point=start,
                                   new_game=start == NEW_GAME, stop_after=args.stop_after,
                                   compare=getattr(args, "compare", "auto"),
                                   command=getattr(args, "command", None), root=ROOT)
        except Exception:
            logging.error("no run report (could not start the record):\n%s", traceback.format_exc())
        if args.backend == "headless":
            emu = HeadlessEmu(rom)
            if args.resume:
                emu.load_state((runs / "checkpoints" / f"{args.resume}.state").read_bytes())
            last = [0.0]

            def live(e):
                if time.time() - last[0] > args.live:
                    last[0] = time.time()
                    e.screenshot(str(runs / "live.png"))
                if hook:
                    control.on_frames(e)
            emu.on_frames = live
        else:
            emu = MgbaEmu(port=args.port, rom_path=rom)
            if hook:
                emu.on_frames = control.on_frames

        agent = Agent(emu)

        def checkpoint(name: str) -> None:
            if args.backend == "headless":
                (runs / "checkpoints" / f"{name}.state").write_bytes(emu.save_state())

        if args.new_game:
            agent.new_game()
        if recorder:
            recorder.begin(agent)
        runner = RouteRunner(agent, ROUTE, checkpoint=checkpoint)
        control.attach(agent, runner)
        control.set_state("running")
        if not args.new_game and runner.current() is None:
            logging.warning("This game has already reached the Hall of Fame (game time %s): "
                            "the cartridge save mGBA loads is a finished game. "
                            "Start over with --new-game.", agent.game.play_time())
        ok = runner.run(stop_after=args.stop_after)
        state = outcome = "finished" if ok else "failed"
    except (StopRun, KeyboardInterrupt):
        state = outcome = "stopped"
        logging.info("STOPPED by the user")
    except BridgeError as exc:
        outcome, error = "crashed", f"mGBA: {exc}"
        logging.error("mGBA: %s", exc)
    except Exception as exc:
        outcome, error = "crashed", f"{type(exc).__name__}: {exc}"
        logging.error("run crashed:\n%s", traceback.format_exc())
    finally:
        summary = ""
        try:
            summary = "finished=%s in %.0fs wall, game time %s" % (
                ok, time.time() - t0, agent.game.play_time() if agent else "?")
            logging.info("%s", summary)
            if emu:
                emu.screenshot(str(runs / "final.png"))
        except Exception:
            logging.error("could not finish cleanly:\n%s", traceback.format_exc())
        report = None
        if recorder:
            try:
                report = recorder.finish(outcome, agent, runner, error)
            except Exception:
                logging.error("could not write the run report:\n%s", traceback.format_exc())
                recorder.close()
        try:
            if emu:
                # mGBA runs in lockstep while we are connected (the game only
                # advances when asked): let go at once, or the app stays
                # frozen for as long as the TUI shows the result.
                emu.close()
        except Exception:
            pass
        control.finish(state, ok, summary, report)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rom", help="ROM path (default: roms/emerald.gba, $POKEAUTO_ROM, ~/Downloads)")
    p.add_argument("--backend", choices=["headless", "mgba"], default="headless",
                   help="headless: in-process core, unthrottled; mgba: drive the mGBA app")
    p.add_argument("--port", type=int, default=8888, help="mGBA bridge port (default 8888)")
    p.add_argument("--resume", help="checkpoint name to load (headless only)")
    p.add_argument("--stop-after", help="stop after this milestone")
    p.add_argument("--log", default="INFO", help="log level (default INFO)")
    p.add_argument("--live", type=float, default=5.0, help="seconds between runs/live.png updates")
    p.add_argument("--no-tui", action="store_true",
                   help="plain streaming log instead of the TUI (automatic when stdout is not a TTY)")
    p.add_argument("--start-paused", action="store_true", help="TUI: start paused (space resumes)")
    p.add_argument("--new-game", action="store_true",
                   help="start a new game even if the cartridge has a save (mGBA loads the .sav "
                        "next to the ROM; beating the Elite Four saves). Not with --resume")
    p.add_argument("--compare", default="auto", metavar="RUN",
                   help="run report reference: auto (previous run from the same start and the "
                        "fastest to the Hall of Fame), last, best, none, or a run id "
                        "(see scripts/runstats.py)")
    args = p.parse_args()
    args.command = " ".join(["scripts/play.py", *sys.argv[1:]])
    use_tui = not args.no_tui and sys.stdout.isatty() and sys.stdin.isatty()
    if args.new_game and args.resume:
        sys.exit("--new-game and --resume contradict each other")

    runs = ROOT / "runs"
    (runs / "checkpoints").mkdir(parents=True, exist_ok=True)
    lock = runs / "play.lock"
    if lock.exists():
        try:
            os.kill(int(lock.read_text()), 0)
            sys.exit(f"another run (pid {lock.read_text()}) is using {runs}; stop it first")
        except (ProcessLookupError, ValueError):
            pass
    lock.write_text(str(os.getpid()))
    import atexit
    atexit.register(lambda: lock.unlink(missing_ok=True))
    rom = find_rom(args.rom)
    if args.resume and args.backend == "headless" \
            and not (runs / "checkpoints" / f"{args.resume}.state").exists():
        sys.exit(f"no checkpoint runs/checkpoints/{args.resume}.state")
    if args.compare not in ("auto", "last", "best", "none"):
        from pokeauto.runstats import find, load_runs
        try:
            ref = find(load_runs(runs / "history"), args.compare)
        except KeyError as exc:
            sys.exit(str(exc.args[0]))
        if ref is None:
            sys.exit(f"--compare: no run {args.compare!r} in runs/history (scripts/runstats.py lists them)")
        args.compare = ref["id"]

    from pokeauto.runstate import QueueLogHandler, RunControl

    def save_manual(e) -> str:
        path = runs / "checkpoints" / "manual.state"
        path.write_bytes(e.save_state())
        return str(path.relative_to(ROOT))

    control = RunControl(args.backend, save_manual=save_manual if args.backend == "headless" else None)
    logging.basicConfig(level=getattr(logging, args.log.upper()),
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S",
                        handlers=[QueueLogHandler(control) if use_tui else logging.StreamHandler(),
                                  logging.FileHandler(runs / "play.log")])

    if not use_tui:
        run_game(args, rom, runs, control, hook=False)
        print_report(control)
        return 0 if control.ok else 1

    from pokeauto.tui import PlayApp
    if args.start_paused:
        control.pause()
    worker = threading.Thread(target=run_game, args=(args, rom, runs, control, True),
                              name="game", daemon=True)
    worker.start()
    subtitle = f"{args.backend} · {rom.name}" + (f" · stop after {args.stop_after}" if args.stop_after else "")
    try:
        PlayApp(control, subtitle).run()
    finally:
        control.stop()                 # no-op if the run already ended
        worker.join(timeout=30)
    print(control.summary or "the run did not stop in time; see runs/play.log")
    print_report(control)
    return 0 if control.ok else 1


def print_report(control) -> None:
    if control.report is not None:
        from rich.console import Console
        Console().print(control.report.renderable())


if __name__ == "__main__":
    sys.exit(main())
