"""Emerald's critical path, as a checklist the agent can verify.

The single most important thing about Pokemon Emerald, for an agent trying to
finish it, is that the route through the game is FIXED. Every playthrough
visits the same towns in the same order and fights the same eight leaders. That
is knowledge, and knowledge belongs in code — asking a stateless model to
rediscover it every few seconds is what produced an agent that wandered around
a bedroom.

So the spine is scripted and the judgment is not:

* **Code owns** the order of objectives, the destination for each, and the test
  for whether one is finished. Every `done_when` reads real game state — badge
  flags, party contents, which map we are standing on — so progress is a fact,
  never a guess.
* **Jev owns** what to actually do once we arrive: which building is the Gym,
  which person is worth talking to, which move wins this battle. Those are
  judgments, and they are why the agent is not a fixed macro.

Objectives deliberately target OUTDOOR maps — towns, cities, routes — because
those are unambiguous in the map graph. Buildings inside a town all share the
town's name, so choosing among them is exactly the semantic call Jev is for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# Map keys from data/emerald_world.json (verified against the ROM's own graph).
LITTLEROOT = "0.9"
BIRCH_LAB = "1.4"          # the third building in Littleroot, from its warps
ROUTE_101 = "0.16"
OLDALE = "0.10"
ROUTE_103 = "0.18"
ROUTE_102 = "0.17"
PETALBURG = "0.0"
ROUTE_104 = "0.19"
RUSTBORO = "0.3"
DEWFORD = "0.11"
SLATEPORT = "0.1"
MAUVILLE = "0.2"
LAVARIDGE = "0.12"
FORTREE = "0.4"
LILYCOVE = "0.5"
MOSSDEEP = "0.6"
SOOTOPOLIS = "0.7"
EVER_GRANDE = "0.8"


@dataclass
class Objective:
    id: str
    goal: str                       # shown to Jev as the current goal
    target_map: str | None          # where code should route to
    done_when: Callable             # predicate over a QuestState
    hint: str = ""                  # extra guidance for the model

    def describe(self) -> str:
        return self.goal


@dataclass
class QuestState:
    """The facts an objective may be tested against."""
    badges: int
    party_size: int
    map_key: str
    visited: set
    player_named: bool
    party_healthy: bool = True


def _badges(n: int) -> Callable:
    return lambda st: st.badges >= n


def _visited(key: str) -> Callable:
    return lambda st: key in st.visited


# The critical path. Each entry is reachable by the router and testable from
# live memory; the model decides what to do on arrival.
QUESTLINE: list[Objective] = [
    Objective(
        "intro", "Get through the opening: choose a character, pick a name, and "
        "step out of the moving truck into Littleroot Town.",
        None, lambda st: st.player_named,
        "This is the fixed intro sequence; it is handled by script, not judgment.",
    ),
    Objective(
        "leave_home", "Leave the house and explore Littleroot Town.",
        LITTLEROOT, _visited(LITTLEROOT),
        "Talk to Mom downstairs and set the clock upstairs if prompted.",
    ),
    Objective(
        "visit_lab", "Go to Professor Birch's laboratory in Littleroot Town and "
        "introduce yourself.",
        BIRCH_LAB, _visited(BIRCH_LAB),
        "Mom asks whether you have introduced yourself to Professor Birch; the "
        "road north stays shut until you have. The lab is the building at the "
        "bottom of the town.",
    ),
    Objective(
        "get_starter", "Go north to Route 101, help Professor Birch, and choose "
        "a starter Pokemon from his bag.",
        ROUTE_101, lambda st: st.party_size >= 1,
        "Birch is being chased by a wild Pokemon. Walk up to him and his bag; "
        "a choice of three Pokemon follows.",
    ),
    Objective(
        "rival_route_103", "Travel through Oldale Town to Route 103 and battle "
        "your rival.",
        ROUTE_103, _visited(ROUTE_103),
        "Route 103 is north of Oldale Town.",
    ),
    Objective(
        "pokedex", "Return to Professor Birch's laboratory in Littleroot Town "
        "to receive the Pokedex.",
        LITTLEROOT, _visited(ROUTE_102),
        "The laboratory is one of the buildings in Littleroot Town.",
    ),
    Objective(
        "petalburg", "Travel west along Route 102 to Petalburg City and visit "
        "the Gym to meet your father, Norman.",
        PETALBURG, _visited(PETALBURG),
        "Norman will not battle you yet; this visit is to advance the story.",
    ),
    Objective(
        "rustboro", "Travel north through Route 104 and Petalburg Woods to "
        "Rustboro City.",
        RUSTBORO, _visited(RUSTBORO),
    ),
    Objective(
        "badge_1", "Find the Gym in Rustboro City and defeat Roxanne to earn "
        "the Stone Badge.",
        RUSTBORO, _badges(1),
        "Roxanne uses Rock-type Pokemon; Grass and Water moves are strong "
        "against them. The Gym is a building in the city.",
    ),
    Objective(
        "dewford", "Travel to Dewford Town, reached by Mr. Briney's boat from "
        "the beach on Route 104.",
        DEWFORD, _visited(DEWFORD),
        "Mr. Briney lives in the cottage on the southern part of Route 104.",
    ),
    Objective(
        "badge_2", "Defeat Brawly at the Dewford Town Gym for the Knuckle Badge.",
        DEWFORD, _badges(2),
        "Brawly uses Fighting-type Pokemon; Flying and Psychic moves are strong "
        "against them.",
    ),
    Objective(
        "slateport", "Sail onward to Slateport City.",
        SLATEPORT, _visited(SLATEPORT),
    ),
    Objective(
        "mauville", "Travel north through Route 110 to Mauville City.",
        MAUVILLE, _visited(MAUVILLE),
    ),
    Objective(
        "badge_3", "Defeat Wattson at the Mauville City Gym for the Dynamo Badge.",
        MAUVILLE, _badges(3),
        "Wattson uses Electric-type Pokemon; Ground moves are strong against "
        "them and are immune to Electric attacks.",
    ),
    Objective(
        "lavaridge", "Travel to Lavaridge Town, up through Route 111 and the "
        "Jagged Pass.",
        LAVARIDGE, _visited(LAVARIDGE),
    ),
    Objective(
        "badge_4", "Defeat Flannery at the Lavaridge Town Gym for the Heat Badge.",
        LAVARIDGE, _badges(4),
        "Flannery uses Fire-type Pokemon; Water, Rock and Ground moves are "
        "strong against them.",
    ),
    Objective(
        "badge_5", "Return to Petalburg City and defeat your father Norman at "
        "the Gym for the Balance Badge.",
        PETALBURG, _badges(5),
        "Norman uses Normal-type Pokemon; Fighting moves are strong against "
        "them and Ghost Pokemon are immune to Normal attacks.",
    ),
    Objective(
        "fortree", "Travel to Fortree City.",
        FORTREE, _visited(FORTREE),
    ),
    Objective(
        "badge_6", "Defeat Winona at the Fortree City Gym for the Feather Badge.",
        FORTREE, _badges(6),
        "Winona uses Flying-type Pokemon; Electric, Ice and Rock moves are "
        "strong against them.",
    ),
    Objective(
        "lilycove", "Travel east to Lilycove City.",
        LILYCOVE, _visited(LILYCOVE),
    ),
    Objective(
        "mossdeep", "Travel across the sea to Mossdeep City.",
        MOSSDEEP, _visited(MOSSDEEP),
    ),
    Objective(
        "badge_7", "Defeat Tate and Liza at the Mossdeep City Gym for the Mind "
        "Badge.",
        MOSSDEEP, _badges(7),
        "This is a double battle against Psychic-type Pokemon; Dark, Ghost and "
        "Bug moves are strong against them.",
    ),
    Objective(
        "sootopolis", "Travel to Sootopolis City and resolve the crisis at the "
        "Cave of Origin.",
        SOOTOPOLIS, _visited(SOOTOPOLIS),
    ),
    Objective(
        "badge_8", "Defeat Juan at the Sootopolis City Gym for the Rain Badge.",
        SOOTOPOLIS, _badges(8),
        "Juan uses Water-type Pokemon; Electric and Grass moves are strong "
        "against them.",
    ),
    Objective(
        "elite_four", "Travel to Ever Grande City, cross Victory Road, and "
        "defeat the Elite Four and the Champion.",
        EVER_GRANDE, lambda st: False,       # the run ends here
        "Heal and stock up on items first: the Elite Four must be fought in one "
        "unbroken sequence.",
    ),
]


def current_objective(state: QuestState) -> Objective:
    """The first objective not yet satisfied."""
    for objective in QUESTLINE:
        if not objective.done_when(state):
            return objective
    return QUESTLINE[-1]


def progress(state: QuestState) -> tuple[int, int]:
    done = sum(1 for o in QUESTLINE if o.done_when(state))
    return done, len(QUESTLINE)
