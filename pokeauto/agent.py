"""The play loop: observe -> decide -> press -> repeat.

Cost control is a first-class concern. A naive loop would call the API once
per frame (~60/second, millions per playthrough). Instead:

* Dialogue and cutscenes are advanced by code, with no model call at all.
* In battle we call once per TURN, gated on the battle signature changing.
* In the overworld we call at most once per step, and `--overworld-every`
  lets several steps share one decision.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .bridge import Bridge
from .brain import DIRECTIONS, Brain, BattleDecision, OverworldDecision
from .mapdata import MapNames, MapReader
from .questline import QuestState, current_objective, progress
from .world import World, map_key, parse_key
from .worldgraph import WorldGraph
from .observer import Observer, Snapshot

log = logging.getLogger("pokeauto.agent")

# Objectives are a scripted spine; the model steers within them. Emerald's
# critical path is far too long for a stateless model to infer unaided.
OBJECTIVES = [
    "Leave the moving truck and your house in Littleroot Town, then go next "
    "door and upstairs, then north to Route 101 to meet Professor Birch and "
    "pick a starter Pokemon.",
    "Reach Rustboro City and defeat Roxanne at the Rock-type Gym for the Stone Badge.",
    "Reach Dewford Town and defeat Brawly at the Fighting-type Gym for the Knuckle Badge.",
    "Reach Mauville City and defeat Wattson at the Electric-type Gym for the Dynamo Badge.",
    "Reach Lavaridge Town and defeat Flannery at the Fire-type Gym for the Heat Badge.",
    "Return to Petalburg City and defeat Norman at the Gym for the Balance Badge.",
    "Reach Fortree City and defeat Winona at the Flying-type Gym for the Feather Badge.",
    "Reach Mossdeep City and defeat Tate and Liza for the Mind Badge.",
    "Reach Sootopolis City and defeat Wallace for the Rain Badge.",
    "Travel to Ever Grande City and challenge the Elite Four.",
]

BATTLE_MENU_GAP = 6      # frames between menu inputs; the cursor needs a beat


@dataclass
class Destination:
    """A reachable place worth walking to, with its true step count."""
    tile: tuple[int, int]
    steps: int
    label: str
    leads_to: tuple[int, int] | None = None   # destination map, for warps


@dataclass
class Stats:
    steps: int = 0
    battles_seen: int = 0
    turns_decided: int = 0
    overworld_decisions: int = 0
    dialogue_presses: int = 0
    started: float = field(default_factory=time.monotonic)

    def summary(self, brain: Brain) -> str:
        mins = (time.monotonic() - self.started) / 60
        return (
            f"{self.steps} steps in {mins:.1f} min | "
            f"battle turns: {self.turns_decided} | "
            f"overworld decisions: {self.overworld_decisions} | "
            f"dialogue presses (free): {self.dialogue_presses} | "
            f"TypeSafe calls: {brain.calls} "
            f"({brain.input_tokens} in / {brain.output_tokens} out tokens)"
        )


class Agent:
    def __init__(self, bridge: Bridge, observer: Observer, brain: Brain,
                 overworld_every: int = 1, max_calls: int | None = None,
                 world_path=None):
        self.bridge, self.observer, self.brain = bridge, observer, brain
        self.overworld_every = max(1, overworld_every)
        self.max_calls = max_calls
        self.stats = Stats()

        self._recent: list[tuple] = []
        self._walls: dict[tuple, set[str]] = {}   # per-tile blocked directions
        self._visits: dict[tuple, int] = {}
        self._last_move: str | None = None
        self._no_interact_at: set[tuple] = set()
        self._pending_interact_tile: tuple | None = None
        self.map_reader = MapReader(bridge, observer.addr)
        self.map_names = MapNames(bridge, observer.addr)
        self.world = World(world_path)
        self.graph = WorldGraph()
        self._intro_phase = 0
        self._intro_presses = 0
        self._objective_id: str | None = None
        self._travel_leg: tuple | None = None
        self._travel_cooldown = 0
        self._view = None
        self._unblock_index = 0
        self._dialogue_streak = 0
        self._mission = None
        self._current_map_key: str | None = None
        self._goal_failures = 0
        self._path: list[str] = []
        self._goal: tuple[int, int] | None = None
        self._current_map: tuple[int, int] | None = None
        self._visited_maps: set[tuple[int, int]] = set()
        self._previous_map: tuple[int, int] | None = None
        self._settle_count = 0
        self._map_entries: dict[tuple[int, int], int] = {}
        self._talked_to: set[tuple[int, int, int, int]] = set()
        self._goal_kind: str | None = None
        self._last_battle_sig: tuple | None = None
        self._steps_since_overworld_call = 0
        self._cached_overworld: OverworldDecision | None = None
        self._last_location: tuple | None = None
        self._stuck_count = 0
        self._interact_streak = 0
        self._last_text: str | None = None

    # -- objectives --------------------------------------------------------

    @staticmethod
    def objective_for(snap: Snapshot) -> str:
        idx = min(len(snap.badges), len(OBJECTIVES) - 1)
        return OBJECTIVES[idx]

    # -- menu primitives ---------------------------------------------------

    def _tap(self, *buttons: str) -> None:
        self.bridge.press(*buttons, hold=6, gap=BATTLE_MENU_GAP)

    def _anchor_top_left(self) -> None:
        """Force a 2x2 menu cursor to the top-left cell from anywhere."""
        self._tap("UP")
        self._tap("LEFT")

    def _select_grid_cell(self, index: int) -> None:
        """Move to cell `index` of a 2x2 grid laid out 0 1 / 2 3, then confirm."""
        self._anchor_top_left()
        if index % 2 == 1:
            self._tap("RIGHT")
        if index >= 2:
            self._tap("DOWN")
        self._tap("A")

    def _use_move(self, slot: int) -> None:
        self._tap("B")             # back out of any open submenu
        self._select_grid_cell(0)  # FIGHT is the top-left action
        self._select_grid_cell(slot)

    def _flee(self) -> None:
        self._tap("B")
        self._select_grid_cell(3)  # RUN is the bottom-right action

    def _switch_to(self, party_slot: int) -> None:
        self._tap("B")
        self._select_grid_cell(2)  # POKEMON is the bottom-left action
        for _ in range(party_slot):
            self._tap("DOWN")
        self._tap("A")             # open the per-Pokemon menu
        self._tap("A")             # confirm SHIFT / SEND OUT (first entry)

    # -- battle ------------------------------------------------------------

    def _battle_step(self, snap: Snapshot) -> None:
        sig = snap.battle_signature()
        if sig == self._last_battle_sig:
            # Nothing has changed since our last decision: the game is still
            # animating or waiting on a prompt. Advance it for free.
            self._tap("A")
            self.stats.dialogue_presses += 1
            return

        if self._budget_exhausted():
            self._tap("A")
            return

        decision = self.brain.decide_battle(snap, is_wild=True)
        self.stats.turns_decided += 1
        self._last_battle_sig = sig

        me, foe = snap.active, snap.opponent
        log.info(
            "BATTLE %s %d/%d HP vs %s %d/%d -> %s%s [conf %.2f, %s]",
            me.nickname, me.hp, me.max_hp, foe.nickname, foe.hp, foe.max_hp,
            decision.action,
            f" {decision.move_name}" if decision.action == "fight" else "",
            decision.confidence, decision.source,
        )

        if decision.action == "run":
            self._flee()
        elif decision.action == "switch":
            target = next(
                (m.slot for m in snap.party
                 if not m.fainted and m.species != snap.active.species),
                None,
            )
            if target is None:
                self._use_move(decision.move_slot or 0)
            else:
                self._switch_to(target)
        else:
            self._use_move(decision.move_slot or 0)

    # -- overworld ---------------------------------------------------------
    #
    # Dialogue detection. gStringVar4 is a formatting buffer that is
    # essentially never empty -- a fresh save still holds Birch's title-screen
    # monologue -- so "text is present" means nothing. "Text is CHANGING"
    # means dialogue is advancing, and the answer is to keep pressing A.
    #
    # Blocked-direction memory is PER TILE and persistent. A global list that
    # cleared on every successful step could never learn "up is a wall here",
    # so the agent oscillated between two tiles forever: walk up, hit the
    # wall, probe picks down, walk back, repeat.

    STUCK_LIMIT = 24
    MAX_CONSECUTIVE_INTERACTS = 3
    MENU_SUSPECT_AFTER = 4      # frozen steps before we assume a menu is open
    MENU_ESCAPE_PRESSES = 3     # B presses to unwind nested menus
    CLEAR_ATTEMPTS = 14         # A/B presses to clear a transient block
    # Standing beside an NPC, every A press restarts their dialogue, which
    # makes the text change, which triggers another A press. The agent can
    # spend its whole run talking to someone's mother. Cap the streak so
    # movement always gets a turn.
    MAX_DIALOGUE_STREAK = 12
    MAX_SETTLE = 6              # steps to wait out a map transition
    REVERSE = {"up": "down", "down": "up", "left": "right", "right": "left"}
    DELTA = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}

    # -- the scripted opening ----------------------------------------------
    #
    # Character select, the naming keyboard and the truck are a fixed sequence
    # with no decisions worth making, so they are scripted outright. The one
    # non-obvious part is the keyboard: an empty name is rejected, and START
    # is the OK button.

    def _in_intro(self, snap: Snapshot) -> bool:
        """True before the save file exists, when all of SaveBlock1 reads zero."""
        return (snap.map_group, snap.map_num, snap.x, snap.y) == (0, 0, 0, 0)

    def _intro_step(self, snap: Snapshot) -> None:
        self._intro_presses += 1
        text = snap.overworld_text.lower()

        if self._intro_phase == 0:
            if "your name" in text or "what's your name" in text:
                log.info("INTRO naming keyboard reached")
                self._intro_phase = 1
                self._intro_presses = 0
            else:
                self._tap("A")
            return

        if self._intro_phase == 1:
            # Type a few letters; an empty name will not be accepted.
            if self._intro_presses <= 3:
                self._tap("A")
                return
            log.info("INTRO confirming the name with START")
            self.bridge.press("START", hold=6, gap=20)
            self._intro_phase = 2
            return

        self._tap("A")      # monologue, name confirmation, then the truck
        if self._intro_presses % 20 == 0:
            log.info("INTRO advancing... (%s)", snap.overworld_text[:60] or "…")

    # -- objectives ---------------------------------------------------------

    def _quest_state(self, snap: Snapshot) -> QuestState:
        return QuestState(
            badges=len(snap.badges),
            party_size=len(snap.party),
            map_key=map_key(snap.map_group, snap.map_num),
            visited={k for k, r in self.world.maps.items() if r.visits},
            player_named=not self._in_intro(snap),
            party_healthy=all(not m.fainted for m in snap.party) if snap.party else True,
        )

    def _objective(self, snap: Snapshot):
        objective = current_objective(self._quest_state(snap))
        if objective.id != self._objective_id:
            done, total = progress(self._quest_state(snap))
            log.info("QUEST [%d/%d] %s — %s", done, total, objective.id, objective.goal)
            self._objective_id = objective.id
            self._mission = None
            self._path = []
            self._travel_cooldown = 0
        return objective

    def _overworld_step(self, snap: Snapshot) -> None:
        if self._in_intro(snap):
            self._intro_step(snap)
            return

        location = snap.location_key
        first_look = self._last_location is None
        moved = not first_look and location != self._last_location

        text_changed = (self._last_text is not None
                        and snap.overworld_text != self._last_text)
        self._last_text = snap.overworld_text

        if moved or first_look:
            self._stuck_count = 0
            self._interact_streak = 0
            self._pending_interact_tile = None
        else:
            self._stuck_count += 1
            # We pressed A here last step and are still frozen: that press
            # opened something (a PC, a shop, the Bag) rather than doing
            # anything useful. Never press A on this tile again.
            if self._pending_interact_tile == location:
                log.info("OVERWORLD pressing A at (%d,%d) froze us — marking "
                         "that tile as do-not-interact", location[2], location[3])
                self._no_interact_at.add(location)
                self._pending_interact_tile = None
        self._last_location = location
        self._visits[location] = self._visits.get(location, 0) + 1

        if not self._recent or self._recent[-1] != location:
            self._recent.append(location)
            self._recent = self._recent[-32:]

        # 1. Text is advancing. Push it along; this costs nothing.
        #
        #    A is the normal button, but once we have been frozen for several
        #    steps the changing text is just as likely to be a MENU we opened
        #    by accident — the PC, the Bag, a shop. In a menu the D-pad moves
        #    a cursor rather than the player, so every direction reads as a
        #    wall, and pressing A navigates *deeper* in. B backs out of menus
        #    and advances dialogue, so it is the safe button once pinned.
        if text_changed and self._dialogue_streak < self.MAX_DIALOGUE_STREAK:
            self._dialogue_streak += 1
            self.stats.dialogue_presses += 1
            self._tap("B" if self._stuck_count >= self.MENU_SUSPECT_AFTER else "A")
            return
        if not text_changed:
            self._dialogue_streak = 0

        view = self._map_view()
        self._view = view

        # 2. We did not move last step. Something is holding us — a text box,
        #    a menu, terrain — and no amount of replanning fixes that, so
        #    probe (which presses B and tries directions) before doing
        #    anything clever. Replanning a route while pinned was an infinite
        #    loop: each replan reset the failure counter that was meant to
        #    give up.
        if self._stuck_count > 0:
            self._probe_while_stuck(location)
            return

        # 3. Navigate with the real map when we can read one. This is the
        #    good path: code pathfinds, the model only picks a destination.
        if view is not None and view.in_bounds(snap.x, snap.y):
            if view.passable(snap.x, snap.y):
                self._settle_count = 0
                if self._observe_world(snap, view):
                    objective = self._objective(snap)
                    if self._travel(snap, view, objective):
                        return
                    # Frontier exploration only gets a turn once travelling is
                    # not an option -- we have arrived, the objective names no
                    # destination, or the road is barred and we are looking for
                    # whatever opens it. Letting it run alongside travel meant
                    # a frontier on another map could drag us back through the
                    # door we had just come out of.
                    here = map_key(snap.map_group, snap.map_num)
                    free_to_explore = (
                        objective.target_map is None
                        or objective.target_map == here
                        or self._travel_cooldown > 0
                    )
                    if free_to_explore and self._pursue(snap, view):
                        return
            elif self._settle_count < self.MAX_SETTLE:
                # The grid still describes the previous map, which happens for
                # a few frames after a warp. Let it catch up rather than
                # walking blind while standing in a doorway.
                self._settle_count += 1
                self.bridge.idle(10)
                return
            else:
                self._settle_count = 0

        # 4. No usable map (a cutscene, a transition, a surf sequence): ask
        #    for a direction blind rather than stalling.
        decision = self._overworld_decision(snap)

        if decision.action == "interact":
            if location in self._no_interact_at:
                # Pressing A here already trapped us in something we had to
                # back out of. Do not walk into the same menu again.
                log.info("OVERWORLD refusing to press A at (%d,%d) again — it "
                         "opened something we had to escape", location[2], location[3])
                self._walk(self._choose_direction(location, decision))
                return
            if self._interact_streak >= self.MAX_CONSECUTIVE_INTERACTS:
                self._interact_streak = 0
                self._walk(self._choose_direction(location, decision))
                return
            self._interact_streak += 1
            self.stats.dialogue_presses += 1
            self._pending_interact_tile = location
            self._tap("A")
            return

        self._interact_streak = 0
        if decision.action == "heal":
            log.info("OVERWORLD party needs healing (p=%.2f); "
                     "walking on — routing to a Center is not implemented",
                     decision.heal_probability)
        self._walk(self._choose_direction(location, decision))

    def _blocked_at(self, location: tuple) -> set[str]:
        return self._walls.setdefault(location, set())

    def _target(self, location: tuple, direction: str) -> tuple:
        """The tile a step in `direction` would land on."""
        group, num, x, y = location
        dx, dy = self.DELTA[direction]
        return (group, num, x + dx, y + dy)

    def _rank_directions(self, location: tuple, probabilities: dict) -> list[str]:
        """Order the viable directions: unexplored first, model preference last.

        Per-tile wall memory alone cannot stop oscillation, because the step
        into a dead end genuinely succeeds — nothing marks the tile you came
        from. Knowing where each direction *leads* is what breaks the loop.
        """
        blocked = self._blocked_at(location)
        back = self.REVERSE.get(self._last_move or "")
        return sorted(
            (d for d in DIRECTIONS if d not in blocked),
            key=lambda d: (
                self._visits.get(self._target(location, d), 0),  # unexplored first
                d == back,                                       # avoid reversing
                -probabilities.get(d, 0.0),                      # then the model
            ),
        )

    def _choose_direction(self, location: tuple, decision: OverworldDecision) -> str:
        """Take the model's answer unless it is demonstrably retracing."""
        blocked = self._blocked_at(location)
        ranked = self._rank_directions(location, decision.probabilities)
        if not ranked:
            return decision.direction

        if decision.direction in blocked:
            log.info("OVERWORLD %s is a known wall at (%d,%d) — going %s instead",
                     decision.direction, location[2], location[3], ranked[0])
            return ranked[0]

        seen = self._visits.get(self._target(location, decision.direction), 0)
        if seen:
            fresh = [d for d in ranked
                     if not self._visits.get(self._target(location, d), 0)]
            if fresh:
                log.info("OVERWORLD %s leads back to a tile seen %dx — "
                         "taking %s into unexplored ground instead",
                         decision.direction, seen, fresh[0])
                return fresh[0]
        return decision.direction

    def _probe_while_stuck(self, location: tuple) -> None:
        """Escape a blocked tile deterministically. Makes no API calls.

        Alternates A with each direction not yet known to be a wall here. One
        A press is enough to bootstrap a waiting text box: it advances, the
        buffer changes, and the dialogue branch takes over.
        """
        if self._stuck_count >= self.STUCK_LIMIT:
            log.info("OVERWORLD stuck %d steps at (%d,%d) — forgetting walls "
                     "here and re-asking", self._stuck_count, location[2], location[3])
            self._stuck_count = 0
            self._walls.pop(location, None)
            self._last_location = None
            return

        # With a collision grid in hand we already know what is walkable, so
        # being stuck means something transient: a text box waiting on input,
        # a cutscene, a person in the way. Clear it rather than groping at
        # directions -- B cancels menus and advances text, A confirms prompts.
        if self._view is not None and self._stuck_count <= self.CLEAR_ATTEMPTS:
            self.stats.dialogue_presses += 1
            self._unblock_press()
            return

        if self._stuck_count % 2 == 1:
            # B, not A: if a menu is open, A digs deeper while B backs out.
            self.stats.dialogue_presses += 1
            self._tap("B")
            return

        blocked = self._blocked_at(location)
        untried = [d for d in DIRECTIONS if d not in blocked]
        if self._view is not None:
            warps = self._view.warp_tiles()
            # Probing onto a door teleports us and silently undoes the route
            # we were walking. Only consider it if there is nothing else left.
            safe = [d for d in untried
                    if (location[2] + self.DELTA[d][0],
                        location[3] + self.DELTA[d][1]) not in warps]
            untried = safe or untried
        if not untried:
            # Every direction reads as a wall. Far more often that means a
            # menu is open than that we are sealed in on all four sides, so
            # unwind with B rather than confirming with A.
            log.info("OVERWORLD every direction blocked at (%d,%d) — backing "
                     "out with B in case a menu is open", location[2], location[3])
            for _ in range(self.MENU_ESCAPE_PRESSES):
                self._tap("B")
            self.stats.dialogue_presses += self.MENU_ESCAPE_PRESSES
            self._no_interact_at.add(location)
            self._walls.pop(location, None)
            self._last_location = None
            self._stuck_count = 0
            return

        ranked = self._rank_directions(location, {})
        direction = (ranked or untried)[0]
        log.info("OVERWORLD probing %s at (%d,%d) (walls here: %s)",
                 direction, location[2], location[3],
                 ",".join(sorted(blocked)) or "none")
        self._walk(direction)

    WALK_FRAMES = 18

    # Rotation used when something invisible is holding us still. Each entry
    # handles a different kind of obstruction, because no single button clears
    # all of them:
    #
    #   A       advances a message or confirms a prompt
    #   UP+A    answers YES -- a two-option prompt puts YES on top, and the
    #           cursor often starts on NO, so bare A silently answers no. The
    #           bedroom clock deadlocked exactly here: A re-opened the "is this
    #           the correct time?" prompt and answered NO, forever.
    #   DOWN+A  nudges an adjustable widget (the clock will not accept a time
    #           until it has been moved) and then confirms
    #   B       backs out of a menu we opened by mistake
    UNBLOCK_SEQUENCE = (("A",), ("A",), ("UP", "A"), ("A",),
                        ("DOWN", "A"), ("A",), ("B",))

    def _unblock_press(self) -> None:
        step = self.UNBLOCK_SEQUENCE[self._unblock_index % len(self.UNBLOCK_SEQUENCE)]
        self._unblock_index += 1
        for button in step:
            self._tap(button)

    def _walk(self, direction: str) -> bool:
        """Take one step. Returns whether the position actually changed.

        Pressing a direction you are not already facing makes the character
        TURN rather than walk, so a single press legitimately moves nowhere.
        Retrying once distinguishes "I had to turn first" from "this is a
        wall" — without it the agent invents walls that are not there and
        replans constantly.
        """
        before = self._last_location
        for attempt in range(2):
            self.bridge.hold(direction.upper(), frames=self.WALK_FRAMES)
            self.bridge.idle(4)
            after = self.observer.snapshot().location_key
            if after != before:
                self._last_move = direction
                self._stuck_count = 0
                self._unblock_index = 0
                self._dialogue_streak = 0
                return True
            if attempt == 0:
                log.debug("step %s did not move us; retrying in case we only "
                          "turned to face that way", direction)

        # Only call it a wall if the collision grid agrees. When the grid says
        # the tile is walkable, something transient is in the way -- a waiting
        # text box, a person standing there, a cutscene -- and recording a wall
        # would poison the map with obstacles that are not real.
        target = self._target(before, direction)
        if self._view is None or not self._view.passable(target[2], target[3]):
            self._blocked_at(before).add(direction)
        else:
            log.debug("step %s failed but the grid says it is walkable — "
                      "treating it as a temporary obstruction", direction)
        return False

    def _overworld_decision(self, snap: Snapshot) -> OverworldDecision:
        self._steps_since_overworld_call += 1
        due = (self._cached_overworld is None
               or self._steps_since_overworld_call >= self.overworld_every)

        if not due or self._budget_exhausted():
            return self._cached_overworld or self.brain._overworld_heuristic(
                sorted(self._blocked_at(snap.location_key)))

        location = snap.location_key
        decision = self.brain.decide_overworld(
            snap, self.objective_for(snap), self._recent,
            sorted(self._blocked_at(location)),
            steps_without_moving=self._stuck_count,
            times_visited=self._visits.get(location, 1),
            came_from=self.REVERSE.get(self._last_move or "") or "nowhere yet",
        )
        self.stats.overworld_decisions += 1
        self._cached_overworld = decision
        self._steps_since_overworld_call = 0
        log.info("OVERWORLD map %d.%d (%d,%d) visits=%d -> %s %s [conf %.2f, %s]",
                 snap.map_group, snap.map_num, snap.x, snap.y,
                 self._visits.get(location, 1),
                 decision.action, decision.direction, decision.confidence,
                 decision.source)
        return decision

    def _map_view(self):
        """Current MapView, or None during cutscenes and transitions."""
        try:
            return self.map_reader.read()
        except Exception as exc:
            log.debug("map unavailable: %s", exc)
            return None

    def _observe_world(self, snap: Snapshot, view) -> bool:
        """Fold the current map into the world model. True if usable."""
        here = map_key(snap.map_group, snap.map_num)
        if here != self._current_map_key:
            self.world.observe(snap.map_group, snap.map_num, view, self.map_names)
            self.world.enter(snap.map_group, snap.map_num)
            self.world.save()
            record = self.world.maps[here]
            unexplored = sum(1 for d in record.exits().values()
                             if not self.world.visited(d))
            log.info("MAP entered %s — %dx%d, %d exit(s) (%d unexplored), "
                     "%d object(s)", record.described, view.width, view.height,
                     len(record.exits()), unexplored, len(record.objects))
            self._current_map_key = here
            self._path = []
        else:
            self.world.observe(snap.map_group, snap.map_num, view, self.map_names)
        return True

    def _travel(self, snap: Snapshot, view, objective) -> bool:
        """Head for the objective's target map. True if this step was handled.

        Routing uses the STATIC graph read from the ROM, so the destination
        need never have been visited. Emerald's road from Littleroot to
        Rustboro is seven maps long and entirely known in advance; there is no
        reason to rediscover it by wandering.
        """
        target = objective.target_map
        here = map_key(snap.map_group, snap.map_num)
        if not target or target == here or not self.graph:
            return False

        # Emerald gates the road with story blockers -- Route 101 is shut until
        # the opening is finished. When the way is barred, stand down for a
        # while so exploration can get a turn: talking to the right person is
        # usually what opens it, and that is what frontier-chasing does.
        if self._travel_cooldown > 0:
            self._travel_cooldown -= 1
            return False

        route = self.graph.route(here, target)
        if not route or len(route) < 2:
            return False

        next_key = route[1]
        if self._travel_leg != (here, next_key):
            self._travel_leg = (here, next_key)
            self._goal_failures = 0
            self._path = []

        if self._path:
            direction = self._path.pop(0)
            if not self._walk(direction):
                self._path = []
                self._goal_failures += 1
                log.debug("TRAVEL step %s failed (%d/%d)", direction,
                          self._goal_failures, self.GOAL_ATTEMPTS)
                if self._goal_failures >= self.GOAL_ATTEMPTS:
                    log.info("TRAVEL the way to %s is barred — exploring here "
                             "for %d steps to look for whatever opens it",
                             self.graph.describe(next_key), self.TRAVEL_COOLDOWN)
                    self._travel_leg = None
                    self._goal_failures = 0
                    self._travel_cooldown = self.TRAVEL_COOLDOWN
                    return False
            return True

        goal = self._leg_target(view, here, next_key, (snap.x, snap.y))
        if goal is None:
            return False
        path = view.path_to((snap.x, snap.y), goal,
                            avoid=view.warp_tiles() - {goal})
        if path is None:
            return False
        if not path:
            blocked = self._blocked_at((snap.map_group, snap.map_num, *goal))
            direction = view.door_direction(goal, self._last_move, exclude=blocked)
            if direction:
                self._walk(direction)
                return True
            return False

        log.info("TRAVEL %s -> %s (%d map(s) left to %s), %d step(s)",
                 self.graph.describe(here), self.graph.describe(next_key),
                 len(route) - 1, self.graph.describe(target), len(path))
        self._path = path
        self._goal_kind = "exit"
        return True

    def _leg_target(self, view, here: str, next_key: str, start):
        """Nearest tile on this map that leads to next_key."""
        options = []
        for where in self.graph.exits_toward(here, next_key):
            if where.startswith("edge:"):
                tile = self._edge_tile(view, where.split(":", 1)[1], start)
                if tile:
                    options.append(tile)
            else:
                x, y = (int(v) for v in where.split(","))
                if view.in_bounds(x, y) and (x, y) != start:
                    options.append((x, y))
        if not options:
            return None
        return min(options, key=lambda t: abs(t[0] - start[0]) + abs(t[1] - start[1]))

    # -- world-scale navigation --------------------------------------------
    #
    # Local destination-picking cannot get anywhere: the agent can only aim at
    # doors in the room it is standing in, so it paces between buildings. The
    # fix is a persistent world model. Every map entered is recorded with its
    # exits and where they lead; an exit into a map never entered is a
    # FRONTIER. The model chooses among frontiers across the entire known
    # world, and code routes there -- across maps if need be.

    MAX_FRONTIERS = 12
    FULL_RENDER_TILES = 2400        # bigger maps get a window instead

    def _render(self, snap: Snapshot, view, mission_tile=None) -> str:
        marks = {(w.x, w.y): "W" for w in view.warps}
        marks.update({(o.x, o.y): "N" for o in view.objects})
        if mission_tile:
            marks[tuple(mission_tile)] = "X"
        radius = (max(view.width, view.height)
                  if view.width * view.height <= self.FULL_RENDER_TILES else 12)
        return view.render((snap.x, snap.y), marks, radius=radius)

    def _mission_complete(self, snap: Snapshot) -> bool:
        m = self._mission
        if m is None:
            return True
        if m.kind in ("exit", "edge"):
            return bool(m.dest_key and self.world.visited(m.dest_key))
        record = self.world.maps.get(m.map_key)
        if record is None or m.tile is None:
            return True
        return record.objects.get(f"{m.tile[0]},{m.tile[1]}", False)

    def _choose_mission(self, snap: Snapshot, view, here: str):
        frontiers = self.world.frontiers(here)[:self.MAX_FRONTIERS]
        if not frontiers:
            log.info("WORLD no frontiers left in the known world")
            return None
        if self._budget_exhausted():
            return frontiers[0]

        labels = {f"goto_{i}": f"{f.label} — {f.hops} map(s) from here"
                  for i, f in enumerate(frontiers)}
        key, confidence, _ = self.brain.decide_frontier(
            snap,
            objective=self.objective_for(snap),
            here=self.world.maps[here].described if here in self.world.maps
                 else f"map {here}",
            ascii_map=self._render(snap, view),
            world_summary=self.world.summary(here),
            frontiers=labels,
        )
        self.stats.overworld_decisions += 1
        index = int(key.split("_")[1]) if key.startswith("goto_") else 0
        chosen = frontiers[min(index, len(frontiers) - 1)]
        log.info("GOAL %s [conf %.2f]", chosen.label, confidence)
        return chosen

    def _edge_tile(self, view, direction: str, start):
        return view.edge_exits(start).get(direction)

    def _pursue(self, snap: Snapshot, view) -> bool:
        """Walk one step toward the current mission. True if handled."""
        here = map_key(snap.map_group, snap.map_num)

        if self._mission_complete(snap):
            if self._mission is not None:
                log.info("GOAL reached: %s", self._mission.label)
            self._mission = self._choose_mission(snap, view, here)
            self._path = []
            self._goal_failures = 0
            if self._mission is None:
                return False

        mission = self._mission
        start = (snap.x, snap.y)

        # Continue an existing route.
        if self._path:
            direction = self._path.pop(0)
            if not self._walk(direction):
                self._path = []
                self._goal_failures += 1
                log.debug("TRAVEL step %s failed (%d/%d)", direction,
                          self._goal_failures, self.GOAL_ATTEMPTS)
                if self._goal_failures >= self.GOAL_ATTEMPTS:
                    self._abandon("kept getting blocked — it may be a story "
                                  "gate that is not open yet")
                else:
                    log.info("NAV step %s blocked — replanning (%d/%d)",
                             direction, self._goal_failures, self.GOAL_ATTEMPTS)
            elif not self._path and self._goal_kind == "object":
                self._arrive(self.observer.snapshot())
            return True

        # Work out the tile to aim at on THIS map.
        if mission.map_key == here:
            self._goal_kind = "object" if mission.kind == "object" else "exit"
            goal = (tuple(mission.tile) if mission.tile
                    else self._edge_tile(view, mission.direction, start))
            if goal is None:
                self._abandon(f"no {mission.direction} edge reachable here")
                return False
        else:
            route = self.world.route(here, mission.map_key)
            if not route or len(route) < 2:
                self._abandon(f"no known route to {mission.map_key}")
                return False
            goal = self._exit_toward(view, here, route[1], start)
            if goal is None:
                self._abandon(f"no usable exit toward {route[1]}")
                return False
            self._goal_kind = "exit"

        avoid = view.warp_tiles() - {goal}
        path = view.path_to(start, goal, avoid=avoid)
        if path is None:
            self._abandon(f"{goal} is unreachable on this map")
            return False

        if not path:
            blocked = self._blocked_at((snap.map_group, snap.map_num, *goal))
            direction = view.door_direction(goal, self._last_move, exclude=blocked)
            if direction and goal in view.warp_tiles():
                log.info("NAV standing on the exit at %s — stepping %s into it",
                         goal, direction)
                self._walk(direction)
                return True
            if self._goal_kind == "object":
                self._arrive(snap)
                return True
            self._abandon()
            return False

        log.info("NAV %s (%d,%d) -> %s, %d step(s)",
                 self.world.maps[here].described if here in self.world.maps else here,
                 snap.x, snap.y, goal, len(path))
        self._path = path
        self._goal = goal
        return True

    def _exit_toward(self, view, here: str, next_key: str, start):
        """The tile on this map that leads to next_key, nearest first."""
        record = self.world.maps.get(here)
        if record is None:
            return None
        options = []
        for tile, dest in record.warps.items():
            if dest != next_key:
                continue
            x, y = (int(v) for v in tile.split(","))
            if (x, y) == start:
                continue
            options.append((x, y))
        for direction, dest in record.connections.items():
            if dest != next_key:
                continue
            tile = self._edge_tile(view, direction, start)
            if tile:
                options.append(tile)
        if not options:
            return None
        return min(options, key=lambda t: abs(t[0] - start[0]) + abs(t[1] - start[1]))

    GOAL_ATTEMPTS = 4          # failed steps before a goal is written off
    TRAVEL_COOLDOWN = 60       # steps of local exploration after a barred road

    def _abandon(self, reason: str = "") -> None:
        """Write off the current goal so it is never chosen again.

        Applies to genuinely sealed exits as much as to unreachable ones: the
        game hides progress gates behind normal-looking doors, and the
        collision grid cannot see them.
        """
        m = self._mission
        if m is not None:
            if m.kind == "object" and m.tile:
                group, num = parse_key(m.map_key)
                self.world.mark_talked(group, num, *m.tile)
            else:
                self.world.mark_blocked(m.map_key, m.tile, m.direction)
            self.world.save()
            log.info("GOAL abandoned%s: %s", f" ({reason})" if reason else "",
                     m.label)
        self._mission = None
        self._path = []
        self._goal_failures = 0

    def _arrive(self, snap: Snapshot) -> None:
        """Reached an object we came to interact with."""
        if self._goal is None:
            return
        log.info("NAV arrived at the object at %s — talking to it", self._goal)
        self._tap("A")
        self._tap("A")
        self.stats.dialogue_presses += 2
        self.world.mark_talked(snap.map_group, snap.map_num, *self._goal)
        self.world.save()
        self._goal = None
        self._goal_kind = None

    # -- loop --------------------------------------------------------------

    def _budget_exhausted(self) -> bool:
        return self.max_calls is not None and self.brain.calls >= self.max_calls

    def run(self, max_steps: int | None = None) -> Stats:
        was_in_battle = False
        try:
            while max_steps is None or self.stats.steps < max_steps:
                snap = self.observer.snapshot()
                self.stats.steps += 1

                if snap.in_battle:
                    if not was_in_battle:
                        self.stats.battles_seen += 1
                        self._last_battle_sig = None
                        log.info("--- battle #%d begins: %s lvl %d ---",
                                 self.stats.battles_seen, snap.opponent.nickname,
                                 snap.opponent.level)
                    self._battle_step(snap)
                else:
                    if was_in_battle:
                        log.info("--- battle over ---")
                        self._last_battle_sig = None
                        self._cached_overworld = None
                    self._overworld_step(snap)

                was_in_battle = snap.in_battle

                if self.stats.steps % 25 == 0:
                    log.info("STATS %s", self.stats.summary(self.brain))
        except KeyboardInterrupt:
            log.info("interrupted by user")
        return self.stats
