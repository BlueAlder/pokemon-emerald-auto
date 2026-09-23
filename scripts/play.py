#!/usr/bin/env python3
"""Play Pokemon Emerald from power-on (or a checkpoint) to the Hall of Fame.

    python scripts/play.py                         # headless, fastest
    python scripts/play.py --resume set_clock      # from a saved checkpoint
    python scripts/play.py --backend mgba          # drive the mGBA app (watchable)
    python scripts/play.py --no-jev                # never call TypeSafe

Checkpoints (savestates) are written to runs/checkpoints/<milestone>.state
after each milestone; the latest frame is mirrored to runs/live.png every few
seconds so a headless run can be watched.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pokeauto.env import load_dotenv  # noqa: E402

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


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rom")
    p.add_argument("--backend", choices=["headless", "mgba"], default="headless")
    p.add_argument("--port", type=int, default=8888)
    p.add_argument("--resume", help="checkpoint name to load (headless only)")
    p.add_argument("--stop-after", help="stop after this milestone")
    p.add_argument("--no-jev", action="store_true", help="never call TypeSafe")
    p.add_argument("--max-jev-calls", type=int, default=200)
    p.add_argument("--log", default="INFO")
    p.add_argument("--live", type=float, default=5.0, help="seconds between runs/live.png updates")
    args = p.parse_args()

    load_dotenv()
    runs = ROOT / "runs"
    (runs / "checkpoints").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=getattr(logging, args.log.upper()),
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(runs / "play.log")])

    from pokeauto.agent import Agent
    from pokeauto.emerald import ROUTE
    from pokeauto.emu import HeadlessEmu, MgbaEmu
    from pokeauto.route import RouteRunner

    rom = find_rom(args.rom)
    if args.backend == "headless":
        emu = HeadlessEmu(rom)
        if args.resume:
            emu.load_state((runs / "checkpoints" / f"{args.resume}.state").read_bytes())
        last = [0.0]

        def live(e):
            if time.time() - last[0] > args.live:
                last[0] = time.time()
                e.screenshot(str(runs / "live.png"))
        emu.on_frames = live
    else:
        emu = MgbaEmu(port=args.port, rom_path=rom)

    brain = None
    if not args.no_jev and os.environ.get("TYPESAFE_API_KEY"):
        from pokeauto.brain import Brain
        brain = Brain(max_calls=args.max_jev_calls)

    agent = Agent(emu, brain=brain)

    def checkpoint(name: str) -> None:
        if args.backend == "headless":
            (runs / "checkpoints" / f"{name}.state").write_bytes(emu.save_state())

    runner = RouteRunner(agent, ROUTE, checkpoint=checkpoint)
    t0 = time.time()
    ok = runner.run(stop_after=args.stop_after)
    logging.info("finished=%s in %.0fs wall, game time %s, jev calls %s", ok, time.time() - t0,
                 agent.game.play_time(), brain.calls if brain else 0)
    emu.screenshot(str(runs / "final.png"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
