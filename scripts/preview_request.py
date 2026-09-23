#!/usr/bin/env python3
"""Print the exact TypeSafe request a battle turn would send, without sending it.

Useful for tuning instructions, criteria wording, and token cost before
spending anything. Needs no API key and no emulator.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pokeauto import memory as mem
from pokeauto.brain import Brain
from pokeauto.observer import Snapshot

T = mem._T


def sample_snapshot() -> Snapshot:
    blaze = mem.Mon(
        slot=0, species_id=256, species="COMBUSKEN", nickname="Blaze", level=20,
        hp=23, max_hp=60, status=None, type1=T["FIRE"], type2=T["FIGHTING"],
        attack=45, defense=35, speed=40, sp_attack=40, sp_defense=35,
        moves=[
            mem.Move(0, 33, "TACKLE", 35, 35, 35, T["NORMAL"], 95),
            mem.Move(1, 52, "EMBER", 25, 25, 40, T["FIRE"], 100),
            mem.Move(2, 43, "LEER", 30, 30, 0, T["NORMAL"], 100),
        ])
    lotad = mem.Mon(
        slot=0, species_id=270, species="LOTAD", nickname="Lotad", level=18,
        hp=48, max_hp=48, status=None, type1=T["WATER"], type2=T["GRASS"],
        attack=30, defense=30, speed=30, sp_attack=40, sp_defense=40, moves=[])
    bench = mem.Mon(
        slot=1, species_id=260, species="MARSHTOMP", nickname="Sludge", level=22,
        hp=70, max_hp=70, status=None, type1=T["WATER"], type2=T["GROUND"],
        attack=50, defense=45, speed=35, sp_attack=40, sp_defense=40, moves=[])
    return Snapshot(frame=0, in_battle=True, party=[blaze, bench],
                    active=blaze, opponent=lotad,
                    battle_text="Wild LOTAD used ASTONISH!")


def main() -> int:
    captured: dict = {}

    class Response:
        ns = types.SimpleNamespace
        answers = {
            "move": ns(choice="move_2", confidence=0.83,
                       probabilities={"move_1": 0.12, "move_2": 0.83, "move_3": 0.05}),
            "should_switch": ns(noul=0.22),
            "should_run": ns(noul=0.05),
            "in_danger": ns(noul=0.61),
        }
        usage = ns(input_tokens=0, output_tokens=0)

    brain = Brain.__new__(Brain)
    brain.model, brain.offline = "jev-latest", False
    brain.calls = brain.input_tokens = brain.output_tokens = 0
    brain.client = types.SimpleNamespace(
        system_one=lambda **kw: (captured.update(kw), Response())[1])

    decision = brain.decide_battle(sample_snapshot(), is_wild=True)

    print("=" * 70)
    print("STATE")
    print("=" * 70)
    print(json.dumps(captured["state"], indent=2))
    print("\n" + "=" * 70)
    print("QUESTIONS  (all sent in ONE request)")
    print("=" * 70)
    for key, q in captured["questions"].items():
        print(f"\n[{key}]  {type(q).__name__}")
        print(f"  instructions: {q.instructions}")
        for ck, cv in (getattr(q, "criteria", None) or {}).items():
            print(f"    {ck}: {cv}")
    print("\n" + "=" * 70)
    print(f"DECISION (from stubbed answers): {decision}")
    approx = len(json.dumps(captured["state"])) // 4
    print(f"state is roughly {approx} tokens before the questions are added")
    return 0


if __name__ == "__main__":
    sys.exit(main())
