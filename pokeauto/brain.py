"""TypeSafe decision layer.

Design follows the TypeSafe skill's division of labour:

* **Code owns** the arithmetic: type effectiveness, STAB, the Gen 3 damage
  formula, PP accounting, HP fractions. The model is never asked to compute.
* **Jev owns** the judgment: given damage already estimated, which move is
  actually the right play, is this the moment to switch, is the party hurt
  enough to turn back and heal.

Every decision point issues exactly ONE request carrying all independent
questions, including speculative ones the code may not consume. Per TypeSafe's
parallel-questions cookbook this is dramatically cheaper and faster than
sequential calls, and the questions cannot see each other's answers anyway.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from . import memory as mem
from .observer import Snapshot

log = logging.getLogger("pokeauto.brain")

DIRECTIONS = ["up", "down", "left", "right"]


# --------------------------------------------------------------------------
# Code-owned arithmetic
# --------------------------------------------------------------------------

def is_physical(type_id: int) -> bool:
    """Gen 3 splits physical/special by type, not by move."""
    return type_id <= 9


def estimate_damage(attacker: mem.Mon, defender: mem.Mon, move: mem.Move) -> float:
    """Estimated damage as a fraction of the defender's max HP.

    The Gen 3 formula with a neutral random roll. Approximate on purpose: it
    exists to rank moves, not to predict the exact number.
    """
    if move.power == 0 or defender.max_hp == 0:
        return 0.0
    if is_physical(move.type_id):
        atk, dfn = attacker.attack, defender.defense
    else:
        atk, dfn = attacker.sp_attack, defender.sp_defense
    if dfn == 0:
        return 0.0

    base = ((2 * attacker.level / 5 + 2) * move.power * atk / dfn) / 50 + 2
    if move.type_id in (attacker.type1, attacker.type2):
        base *= 1.5
    base *= mem.effectiveness(move.type_id, defender.type1, defender.type2)
    base *= 0.925  # average damage roll
    return min(base / defender.max_hp, 1.5)


@dataclass
class ScoredMove:
    move: mem.Move
    effectiveness: float
    damage_fraction: float
    kills: bool

    def describe(self) -> str:
        if self.move.power == 0:
            body = "status move, deals no direct damage"
        else:
            body = (
                f"{self.move.power} power, "
                f"{mem.effectiveness_label(self.effectiveness)} against this target, "
                f"estimated {self.damage_fraction * 100:.0f}% of its max HP"
                + (" — enough to knock it out" if self.kills else "")
            )
        return (
            f"{self.move.name} ({self.move.type_name} type, "
            f"{self.move.accuracy}% accuracy, {self.move.pp} PP left): {body}"
        )


def score_moves(attacker: mem.Mon, defender: mem.Mon) -> list[ScoredMove]:
    scored = []
    for move in attacker.moves:
        if not move.usable:
            continue
        eff = mem.effectiveness(move.type_id, defender.type1, defender.type2)
        dmg = estimate_damage(attacker, defender, move)
        scored.append(ScoredMove(move, eff, dmg, dmg * defender.max_hp >= defender.hp))
    return scored


# --------------------------------------------------------------------------
# Decisions
# --------------------------------------------------------------------------

@dataclass
class BattleDecision:
    action: str                      # "fight" | "switch" | "run"
    move_slot: int | None = None
    move_name: str = ""
    confidence: float = 0.0
    switch_probability: float = 0.0
    run_probability: float = 0.0
    rationale: str = ""
    source: str = "typesafe"


@dataclass
class OverworldDecision:
    action: str                      # "move" | "interact" | "heal"
    direction: str = "down"
    confidence: float = 0.0
    interact_probability: float = 0.0
    heal_probability: float = 0.0
    probabilities: dict = field(default_factory=dict)
    source: str = "typesafe"


# --------------------------------------------------------------------------
# Brain
# --------------------------------------------------------------------------

class Brain:
    """Wraps the TypeSafe client. Degrades to heuristics if it is unavailable."""

    def __init__(self, model: str = "jev-latest", offline: bool = False):
        self.model = model
        self.client = None
        self.offline = offline
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

        if offline:
            log.warning("brain running OFFLINE: heuristics only, no TypeSafe calls")
            return
        if not os.environ.get("TYPESAFE_API_KEY"):
            raise RuntimeError(
                "TYPESAFE_API_KEY is not set. Export your key from "
                "https://console.typesafe.ai/settings/keys, or pass --offline "
                "to run on built-in heuristics."
            )
        from typesafe_sdk import TypeSafeClient  # imported late so --offline works
        self.client = TypeSafeClient()

    # -- plumbing ----------------------------------------------------------

    def _ask(self, state, questions):
        response = self.client.system_one(state=state, questions=questions,
                                          model=self.model)
        self.calls += 1
        usage = getattr(response, "usage", None)
        if usage:
            self.input_tokens += getattr(usage, "input_tokens", 0) or 0
            self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        return response.answers

    # -- battle ------------------------------------------------------------

    def decide_battle(self, snap: Snapshot, is_wild: bool = True) -> BattleDecision:
        me, foe = snap.active, snap.opponent
        assert me and foe
        scored = score_moves(me, foe)
        if not scored:
            return BattleDecision("fight", move_slot=0, move_name="Struggle",
                                  rationale="no usable moves", source="fallback")
        if self.offline or self.client is None:
            return self._battle_heuristic(scored)

        bench = [m for m in snap.party if m is not None and not m.fainted
                 and m.species != me.species]

        state = {
            "situation": "A Pokemon battle. Choose this turn's action.",
            "my_active_pokemon": _mon_state(me),
            "opposing_pokemon": _mon_state(foe),
            # The moves themselves are NOT repeated here: they are the Choice
            # question's criteria, and duplicating them into the state would
            # pay for the same tokens twice on every single battle turn.
            "my_healthy_bench": [
                f"{m.nickname} (level {m.level}, {m.hp}/{m.max_hp} HP)" for m in bench
            ] or "no healthy Pokemon left on the bench",
            "battle_is_wild": is_wild,
            "latest_battle_message": snap.battle_text or "(none)",
            "note": (
                "Damage estimates and type effectiveness above are already "
                "computed by the game engine and are accurate. Judge strategy, "
                "not arithmetic."
            ),
        }

        move_criteria = {f"move_{s.move.slot + 1}": s.describe() for s in scored}
        questions = {
            "move": _choice(
                "Which of these moves is the best play this turn? Weigh knocking "
                "the opponent out, type effectiveness, accuracy, conserving PP on "
                "strong moves, and the risk of my Pokemon fainting first.",
                move_criteria,
            ),
            # Speculative: asked every turn, consumed only when relevant.
            "should_switch": _noul(
                "Switching to a different Pokemon right now would serve me better "
                "than using any available move.",
                {"true": "The active Pokemon is badly outmatched or about to faint "
                         "and a bench Pokemon would fare clearly better",
                 "false": "Attacking with the active Pokemon is the better play"},
            ),
            "should_run": _noul(
                "Fleeing this battle is the right call rather than fighting it out.",
                {"true": "This is a wild battle I am likely to lose, or not worth "
                         "the resources to win",
                 "false": "This battle is winnable or cannot be fled (trainer battle)"},
            ),
            "in_danger": _noul(
                "My active Pokemon is likely to be knocked out by the opponent's "
                "next attack.",
            ),
        }

        try:
            answers = self._ask(state, questions)
        except Exception as exc:
            log.warning("TypeSafe call failed (%s); falling back to heuristics", exc)
            return self._battle_heuristic(scored)

        move_answer = answers["move"]
        slot = int(move_answer.choice.rsplit("_", 1)[1]) - 1
        chosen = next((s for s in scored if s.move.slot == slot), scored[0])
        switch_p = float(answers["should_switch"].noul)
        run_p = float(answers["should_run"].noul)
        danger_p = float(answers["in_danger"].noul)

        decision = BattleDecision(
            action="fight",
            move_slot=chosen.move.slot,
            move_name=chosen.move.name,
            confidence=float(move_answer.confidence),
            switch_probability=switch_p,
            run_probability=run_p,
            rationale=f"danger={danger_p:.2f}",
        )

        # Code owns policy. The model supplies the signals; thresholds are ours
        # and are meant to be tuned on observed play.
        has_bench = bool(bench)
        if is_wild and run_p > 0.80 and not has_bench:
            decision.action = "run"
        elif switch_p > 0.75 and danger_p > 0.6 and has_bench:
            decision.action = "switch"
        return decision

    @staticmethod
    def _battle_heuristic(scored: list[ScoredMove]) -> BattleDecision:
        best = max(scored, key=lambda s: (s.kills, s.damage_fraction))
        return BattleDecision(
            "fight", move_slot=best.move.slot, move_name=best.move.name,
            confidence=1.0, rationale="highest estimated damage", source="heuristic",
        )

    # -- overworld ---------------------------------------------------------

    def decide_overworld(self, snap: Snapshot, objective: str,
                         recent: list[tuple], blocked: list[str],
                         steps_without_moving: int = 0,
                         times_visited: int = 1,
                         came_from: str = "nowhere yet") -> OverworldDecision:
        if self.offline or self.client is None:
            return self._overworld_heuristic(blocked)

        party_line = [
            f"{m.nickname} lvl {m.level}, {m.hp}/{m.max_hp} HP"
            + (f", {m.status}" if m.status else "")
            for m in snap.party
        ]
        state = {
            "situation": "Walking around the overworld in Pokemon Emerald.",
            "current_objective": objective,
            "position": {"map_group": snap.map_group, "map_number": snap.map_num,
                         "x": snap.x, "y": snap.y},
            "recent_positions": [f"({x},{y}) on map {g}.{n}" for g, n, x, y in recent[-8:]],
            "walls_confirmed_at_this_exact_tile": blocked or "none found yet",
            "times_i_have_stood_on_this_tile": times_visited,
            "direction_i_came_from": came_from,
            "my_last_move_attempt_failed": steps_without_moving > 0,
            "consecutive_steps_without_moving": steps_without_moving,
            # gStringVar4 is a formatting buffer, not a live text box. It can
            # hold a message from minutes ago, so it is labelled as unreliable
            # rather than presented as the current screen.
            "last_message_buffer_possibly_stale": snap.overworld_text or "(empty)",
            "my_party": party_line,
            "badges_earned": snap.badges or "none yet",
        }
        questions = {
            "direction": _choice(
                "Which direction should I walk to make progress toward the "
                "current objective? Walls already confirmed at this tile are "
                "wasted moves. If I have stood on this tile several times I am "
                "going in circles and should break the pattern rather than "
                "retrace my steps — walking back the way I came is usually the "
                "worst option.",
                {d: f"Walk one step {d}" for d in DIRECTIONS},
            ),
            "should_interact": _noul(
                "Pressing A is more likely to help here than walking — there is "
                "probably a person, sign, door, or object directly in front of me "
                "to interact with.",
                {"true": "Something is directly in front of me that responds to A, "
                         "or a text box is genuinely waiting to be advanced",
                 "false": "Nothing is there to interact with; walking is the way "
                          "to make progress. Note that repeated failed moves are "
                          "usually terrain blocking me, not a text box."},
            ),
            "should_heal": _noul(
                "My party is hurt enough that I should head to a Pokemon Center "
                "before continuing.",
                {"true": "Several Pokemon are fainted or badly hurt",
                 "false": "The party is healthy enough to keep going"},
            ),
        }

        try:
            answers = self._ask(state, questions)
        except Exception as exc:
            log.warning("TypeSafe call failed (%s); falling back to heuristics", exc)
            return self._overworld_heuristic(blocked)

        direction = answers["direction"]
        interact_p = float(answers["should_interact"].noul)
        heal_p = float(answers["should_heal"].noul)

        action = "move"
        if interact_p > 0.70:
            action = "interact"
        elif heal_p > 0.85:
            action = "heal"

        return OverworldDecision(
            action=action,
            direction=direction.choice,
            confidence=float(direction.confidence),
            interact_probability=interact_p,
            heal_probability=heal_p,
            probabilities=dict(direction.probabilities),
        )

    # -- world-scale navigation -------------------------------------------

    def decide_frontier(self, snap: Snapshot, objective: str, here: str,
                        ascii_map: str, world_summary: list[str],
                        frontiers: dict[str, str]) -> tuple[str, float, dict]:
        """Choose where in the whole known world to head next.

        This is the decision that actually matters. Everything the agent knows
        goes in: the goal it is chasing, where it is standing, the map it can
        see with its exits and people marked, every place it has been and what
        is still unexplored there, and its party. Code handles all the
        geometry -- routing across maps and walking tiles -- so the model is
        left with the judgment call: given all of that, where should I go?
        """
        if self.offline or self.client is None or not frontiers:
            return next(iter(frontiers), ""), 0.0, {}

        state = {
            "my_goal_right_now": objective,
            "where_i_am_standing": here,
            "my_exact_position": {"x": snap.x, "y": snap.y},
            "the_map_i_can_see": ascii_map,
            "map_legend": {
                "P": "me", ".": "walkable", "#": "wall or obstacle",
                "W": "an exit — door, stairs, cave mouth",
                "N": "a person or interactive object",
                "axes": "x increases rightward, y increases downward",
            },
            "everywhere_i_have_been": world_summary or "nowhere yet",
            "my_party": [
                f"{m.nickname} ({m.species}) lvl {m.level}, {m.hp}/{m.max_hp} HP"
                + (f", {m.status}" if m.status else "")
                for m in snap.party] or "no Pokemon yet — I still need a starter",
            "badges_earned": snap.badges or "none yet",
            "play_time": snap.play_time,
            "how_to_read_the_options": (
                "Each option is a place I know exists but have not fully "
                "explored, anywhere in the world above — not just on this map. "
                "'0 map(s) from here' means it is on this map; a higher number "
                "means I would walk through that many maps to reach it. Every "
                "option is already confirmed reachable and code will walk me "
                "there, so pick on usefulness, not on distance alone."
            ),
        }

        questions = {
            "where_to_go": _choice(
                "Where should I go next to make progress on my goal? Think "
                "about what the goal actually requires and which of these "
                "places plausibly leads there. Exits into unexplored areas "
                "advance the game; buildings I have already been through "
                "rarely do. Outdoor routes and town edges lead onward, while "
                "indoor rooms are usually dead ends unless the goal needs "
                "someone inside one. Prefer a nearer option only when it is "
                "genuinely as useful.",
                frontiers,
            ),
            "goal_needs_a_person": _noul(
                "My current goal requires talking to a specific person, "
                "rather than simply travelling somewhere.",
            ),
            "should_heal": _noul(
                "My party is hurt enough that I should find a Pokemon Center "
                "before continuing.",
                {"true": "Several Pokemon are fainted or badly hurt",
                 "false": "The party is healthy enough to keep going"},
            ),
        }

        try:
            answers = self._ask(state, questions)
        except Exception as exc:
            log.warning("TypeSafe call failed (%s); taking the nearest frontier", exc)
            return next(iter(frontiers)), 0.0, {}

        chosen = answers["where_to_go"]
        return (chosen.choice, float(chosen.confidence),
                dict(chosen.probabilities))

    # -- navigation with a real map ---------------------------------------

    def decide_destination(self, snap: Snapshot, objective: str, ascii_map: str,
                           candidates: dict[str, str],
                           visited_maps: list[str],
                           here: str = "") -> tuple[str, float, dict]:
        """Choose where to walk to. Returns (key, confidence, probabilities).

        This is the question the model should have been asked all along. With
        a collision grid in hand, code can path to anywhere reachable, so the
        model no longer guesses at geometry one tile at a time -- it picks a
        destination that serves the objective and lets code work out the route.
        """
        if self.offline or self.client is None or not candidates:
            first = next(iter(candidates), "")
            return first, 0.0, {}

        state = {
            "situation": "Navigating the overworld in Pokemon Emerald.",
            "current_objective": objective,
            "where_i_am": here or f"map {snap.map_group}.{snap.map_num}",
            "my_position": {"x": snap.x, "y": snap.y},
            "local_map": ascii_map,
            "map_legend": {
                "P": "me", ".": "walkable floor", "#": "wall or obstacle",
                "W": "an exit (door, stairs, cave mouth)",
                "N": "a person or interactive object",
                "note": "x increases to the right, y increases downward",
            },
            "places_i_have_already_been": visited_maps or "none yet",
            "note_on_names": "Several maps share one place name — every "
                             "building in a town is named after the town — so "
                             "the map id in brackets distinguishes them.",
            "my_party": [f"{m.nickname} lvl {m.level}, {m.hp}/{m.max_hp} HP"
                         for m in snap.party] or "no Pokemon yet",
            "badges_earned": snap.badges or "none yet",
            "note": "Every destination listed is already confirmed reachable, "
                    "and its step count is the true shortest walking distance. "
                    "Judge which one advances the objective.",
        }
        questions = {
            "destination": _choice(
                "Which destination should I walk to in order to advance the "
                "current objective? Prefer exits into places I have not been "
                "when the objective lies elsewhere, and prefer people worth "
                "talking to when the objective needs information or an item. "
                "Do not pick somewhere I have just come from unless it is the "
                "only route onward.",
                candidates,
            ),
            "should_heal": _noul(
                "My party is hurt enough that I should find a Pokemon Center "
                "before continuing.",
                {"true": "Several Pokemon are fainted or badly hurt",
                 "false": "The party is healthy enough to keep going"},
            ),
        }
        try:
            answers = self._ask(state, questions)
        except Exception as exc:
            log.warning("TypeSafe call failed (%s); taking the nearest option", exc)
            return next(iter(candidates)), 0.0, {}

        chosen = answers["destination"]
        return (chosen.choice, float(chosen.confidence),
                dict(chosen.probabilities))

    @staticmethod
    def _overworld_heuristic(blocked: list[str]) -> OverworldDecision:
        import random
        options = [d for d in DIRECTIONS if d not in blocked] or DIRECTIONS
        return OverworldDecision("move", direction=random.choice(options),
                                 source="heuristic")


# --------------------------------------------------------------------------
# Question helpers — keep SDK objects optional so --offline needs no install
# --------------------------------------------------------------------------

def _choice(instructions: str, criteria: dict):
    from typesafe_sdk import Choice
    return Choice(instructions=instructions, criteria=criteria)


def _noul(instructions: str, criteria: dict | None = None):
    from typesafe_sdk import Noul
    return Noul(instructions=instructions, criteria=criteria) if criteria \
        else Noul(instructions=instructions)


def _mon_state(m: mem.Mon) -> dict:
    types = mem.TYPE_NAMES[m.type1]
    if m.type2 != m.type1:
        types += f"/{mem.TYPE_NAMES[m.type2]}"
    return {
        "name": m.nickname, "species": m.species, "level": m.level,
        "type": types,
        "hp": f"{m.hp}/{m.max_hp} ({m.hp_fraction * 100:.0f}%)",
        "status_condition": m.status or "healthy",
    }
