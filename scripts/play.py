#!/usr/bin/env python3
"""Run the agent."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pokeauto import memory as mem
from pokeauto.env import load_dotenv
from pokeauto.agent import Agent
from pokeauto.brain import Brain
from pokeauto.bridge import Bridge, BridgeError
from pokeauto.observer import Observer

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    p = argparse.ArgumentParser(description="Play Pokemon Emerald with TypeSafe.")
    p.add_argument("--steps", type=int, default=None,
                   help="stop after N loop iterations (default: run until Ctrl-C)")
    p.add_argument("--max-calls", type=int, default=None,
                   help="hard cap on TypeSafe API calls; the agent keeps playing "
                        "on heuristics once the cap is hit")
    p.add_argument("--overworld-every", type=int, default=1,
                   help="reuse one overworld decision for N steps (default 1)")
    p.add_argument("--offline", action="store_true",
                   help="no API calls at all; play on built-in heuristics")
    p.add_argument("--model", default="jev-latest")
    p.add_argument("--port", type=int, default=8888)
    p.add_argument("--forget", action="store_true",
                   help="discard the accumulated world model and re-explore")
    p.add_argument("--log", default="INFO")
    args = p.parse_args()

    load_dotenv()   # real environment variables still win

    logging.basicConfig(
        level=getattr(logging, args.log.upper()),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    bridge = Bridge(port=args.port)
    try:
        bridge.connect(retries=3, delay=1.0)
    except BridgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    info = bridge.info()
    logging.info("connected to %s (%s)", info.title, info.game_code)

    world_file = ROOT / "runs" / "world.json"
    if args.forget and world_file.exists():
        world_file.unlink()
        logging.info("discarded the previous world model")

    observer = Observer(bridge, mem.Addresses.load(ROOT / "config.json"),
                        cache_path=ROOT / "runs" / "rom_tables.json")
    observer.calibrate()

    try:
        brain = Brain(model=args.model, offline=args.offline)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    agent = Agent(bridge, observer, brain,
                  overworld_every=args.overworld_every,
                  max_calls=args.max_calls,
                  world_path=ROOT / "runs" / "world.json")
    stats = agent.run(max_steps=args.steps)

    print("\n" + stats.summary(brain))
    bridge.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
