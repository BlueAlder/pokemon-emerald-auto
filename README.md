# poke_auto

Plays Pokémon Emerald in mGBA by feeding decoded game state to
[TypeSafe](https://typesafe.ai)'s System One model (Jev) and piping its typed
judgments back as button presses.

```
mGBA ──lua/bridge.lua──▶ TCP ──▶ observer.py ──▶ brain.py ──▶ TypeSafe (Jev)
  ▲                                                               │
  └────────────────── button presses ◀── agent.py ◀──── typed answer
```

## What this actually does, and what it does not

**It does not finish the game.** Be clear-eyed about this before you start.
Jev is a *System One* model: it returns a typed answer and a calibrated
probability, with no memory between calls, no reasoning trace, and no image
input. Emerald needs twenty-plus hours of long-horizon planning, maze
navigation, and puzzle solving. No stateless per-step judgment can carry that.

What it *does* do is the split the TypeSafe skill actually prescribes:

| Owned by code | Owned by Jev |
| --- | --- |
| Reading and decrypting RAM | Which move is the right play this turn |
| Type effectiveness, STAB, damage estimates | Whether to switch Pokémon |
| PP accounting, HP fractions | Whether a wild battle is worth fleeing |
| Menu cursor arithmetic, button timing | Which direction advances the objective |
| Objective sequencing, thresholds | Whether the party needs healing |

The result plays battles genuinely well — it reads types, respects PP, and
switches out of bad matchups. Overworld navigation is a weak explorer: it will
wander, and it will not solve Granite Cave or route a path to a gym. Treat it
as a working harness and a strong battle AI, not an unattended speedrunner.

## You need to supply two things

1. **A Pokémon Emerald ROM you legally own.** Dump it from your own cartridge.
   None is included and none will be downloaded for you.
2. **A TypeSafe API key** from https://console.typesafe.ai/settings/keys.

## Setup

mGBA is already installed (`/Applications/mGBA.app`). Then:

```bash
cd ~/Documents/poke_auto
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
cp .env.example .env                      # then put your key in it
```

`.env` is loaded automatically by both scripts, with or without an `export`
prefix, and quotes are stripped. A real environment variable still wins, so
`TYPESAFE_API_KEY=... ./.venv/bin/python scripts/play.py` overrides the file.
The doctor prints which variables it loaded and from where.

1. Open mGBA, load your Emerald ROM, and start or load a save.
2. **Tools ▸ Scripting… ▸ File ▸ Load script…** and pick `lua/bridge.lua`.
   The scripting console prints `poke_auto: bridge listening on 127.0.0.1:8888`.
3. Verify everything before letting it play:

```bash
./.venv/bin/python scripts/doctor.py
```

The doctor is not decoration. It checks the bridge, confirms the game code,
re-derives the ROM data tables, and validates **every hardcoded RAM address
against live memory** — it prints your actual party, and fails loudly if a
Pokémon decodes with an impossible level or HP. Run it first, and again any
time the agent starts behaving strangely.

## Running

```bash
./.venv/bin/python scripts/play.py                    # play until Ctrl-C
./.venv/bin/python scripts/play.py --steps 200        # bounded run
./.venv/bin/python scripts/play.py --max-calls 100    # cap API spend
./.venv/bin/python scripts/play.py --offline          # no API calls at all
```

`--offline` runs the same loop on built-in heuristics (highest estimated
damage, random unblocked direction). Use it to shake out emulator and memory
problems without spending a cent, then drop the flag.

To see exactly what gets sent to Jev — state, questions, criteria, rough token
count — without spending anything:

```bash
./.venv/bin/python scripts/preview_request.py
```

Useful flags: `--overworld-every N` reuses one navigation decision for N steps,
`--log DEBUG` for detail, `--port` if you changed the port in the Lua script.

## Controlling cost

A naive "state → button" loop would call the API ~60 times a second: millions
of calls per playthrough, and latency that makes the game unplayable. This one
gates hard:

- **Dialogue and cutscenes cost nothing.** Advancing text is pure code.
- **Battles cost one call per turn**, not per frame. The loop hashes a battle
  signature (active/opposing species, HP, status, PP) and only asks Jev when it
  changes — which is exactly once per turn.
- **Every decision point sends one request carrying all its questions.**
  Battle turns ask for the move *and* switch/flee/danger in a single call.
  TypeSafe's parallel-questions cookbook measures this at ~12× cheaper and
  ~10× faster than sequential calls, and the questions are independent anyway.
- The speculative answers (`should_run`, `in_danger`) are often discarded.
  That is the fan-out pattern working as intended: one round trip, code picks
  what is relevant.

Every run prints totals: calls made, input and output tokens.

## How it is put together

| File | Role |
| --- | --- |
| `lua/bridge.lua` | mGBA-side TCP server. Deliberately dumb: reads bytes, presses buttons, reports frames. Knows nothing about Pokémon. |
| `pokeauto/bridge.py` | Client for that protocol. Batches memory reads into one round trip. |
| `pokeauto/memory.py` | Emerald RAM map, Gen 3 text codec, party substructure decryption, Gen 3 type chart. |
| `pokeauto/observer.py` | Raw memory → a small named `Snapshot`. Owns ROM-table calibration. |
| `pokeauto/mapdata.py` | Collision grid, warps, connections, place names, and breadth-first pathfinding. |
| `pokeauto/world.py` | Persistent world model: every map seen, the graph between them, frontiers, and cross-map routing. |
| `pokeauto/brain.py` | Damage math in code; Choice/Noul questions to Jev; confidence thresholds. |
| `pokeauto/agent.py` | The loop, menu navigation, and call gating. |
| `scripts/doctor.py` | Validates every assumption against live memory. |
| `pokeauto/env.py` | Dependency-free `.env` loader. |
| `scripts/play.py` | CLI entry point. |
| `scripts/preview_request.py` | Prints the exact request a battle turn sends, without sending it. |

### Two kinds of address, handled differently

**ROM data tables are not hardcoded.** Species names, move names, and move
stats are located at runtime by searching the ROM for known content and
verifying a *different* entry — the species table is found via `BULBASAUR` and
confirmed by checking that index 4 reads `CHARMANDER`; the move stat table is
matched on Pound/Karate Chop/Double-Slap's power, type, accuracy, and PP, so it
does not depend on text at all. Results are cached in `runs/rom_tables.json`.
A coincidental byte match cannot survive the second check.

**RAM symbol addresses are hardcoded** (from the pokeemerald decompilation)
because nothing in memory identifies them. They are asserted rather than
trusted: the doctor sanity-checks each one, and `config.json` overrides any of
them without touching code.

### Species ids are not Dex numbers

A genuine Gen 3 trap. `gSpeciesNames` is indexed by *internal* species id:
ids 1–251 happen to match the National Dex, then **252–276 are 25 unused `?`
placeholder slots**, and Hoenn begins at 277 — Treecko is 277, Torchic 280,
Mudkip 283, Rayquaza 406. RAM stores internal ids, so ordinary lookups are
correct, but never hardcode a Dex number. `rom.find_species("MUDKIP")` resolves
a name to whatever id this ROM actually uses.

### The save blocks move

`gSaveBlock1Ptr` / `gSaveBlock2Ptr` are pointers, and Emerald **relocates what
they point at during play** — observed shifting several times within a single
minute of walking around. Reading the pointer once and caching it means every
later read comes from dead memory. The giveaway is a position of `map 0.0
(0,0)`: plausible-looking zeroes rather than an obvious crash.

`observer.py` re-reads both pointers inside the same batched request on every
snapshot and re-fetches the dependent ranges if they moved, logging
`save blocks relocated:` when it happens. Steady state costs nothing extra.

### There is no "text box is open" flag

`gStringVar4` is a string *formatting* buffer, not a live text box. It retains
whatever was last formatted, so a freshly loaded save still reports Birch's
title-screen monologue indefinitely — meaning "text is present" carries no
information at all.

What does carry information is **text that is changing**. A buffer differing
between snapshots means dialogue is actively advancing, and the response is to
keep pressing A until it settles. That costs no API calls.

When movement fails and the text is static, terrain and a waiting text box are
indistinguishable — and need not be told apart. Probing alternates A presses
with untried directions; if a box was waiting, the A press makes the buffer
change and the dialogue branch takes over.

### B is the escape button, not A

A menu is invisible to this agent: there is no flag saying "the PC is open".
What it sees is that the player has stopped moving and every direction reads as
a wall — because in a menu the D-pad moves a *cursor*, not the player.

The instinct to press A there is exactly wrong. **A confirms, so it navigates
deeper into the menu; B cancels, and also advances dialogue.** An early build
pressed A on the bedroom PC and then kept pressing A, logging 262 presses over
450 steps while visiting one tile 299 times.

So B is the escape button. Every stuck-probe press is B, "changing text" after
several frozen steps switches from A to B (that text is as likely to be a menu
as dialogue), and all-directions-blocked sends three B presses to unwind
nested menus. A is pressed only when the model deliberately chooses to
interact, at a moment when the agent is not stuck.

The agent also remembers its mistakes: if pressing A on a tile leaves it frozen
the next step, that tile goes on a do-not-interact list and it will not open
the same thing again.

### It remembers the world, and aims at frontiers

Reading the current map is not enough on its own. An agent that can only pick
from the doors in the room it is standing in has no way to aim at anywhere it
cannot currently see, so it paces between buildings looking busy.

`world.py` keeps a **persistent model of everywhere it has been** — each map's
name, size, exits and where each one leads, and its objects — saved to
`runs/world.json` so knowledge accumulates across runs rather than resetting.

From that comes the idea that makes navigation work: a **frontier** is an exit
whose destination has never been visited, or a person never spoken to.
Frontiers are computed over the *whole known world*, each tagged with how many
maps away it is:

```
[0 hop] unexplored exit at (14,8) in LITTLEROOT TOWN (town, map 0.9) leading to somewhere new
[0 hop] the unexplored north edge of LITTLEROOT TOWN (town, map 0.9) leading to an unvisited area
[1 hop] unexplored exit at (8,2) in PLACE 1.0 (indoors, map 1.0) leading to somewhere new
```

Jev picks one, and it can be anywhere — several maps from where the agent is
standing. Code then does all the geometry: breadth-first search over the map
graph for the route, then breadth-first search over the collision grid for each
leg of it. The model never touches coordinates; it answers "given my goal and
everything I know, where should I be going?"

That is the whole state it gets: the current goal, where it is standing, the
map rendered as ASCII with exits and people marked, every place visited and
what remains unexplored there, its party, its badges, and the frontier list.

**Sealed exits are remembered too.** Emerald gates progress with invisible
script blockers — Route 101 is shut until the intro is finished — and nothing
in the collision grid says so. A goal that keeps failing is written off and
persisted to `runs/world.json`, so the agent tries something else instead of
battering the same door. `--forget` wipes the world model and re-explores.

### It reads the actual map

The biggest upgrade over blind wandering. Three structures, all located
empirically and verified against live play:

| What | Where | Gives |
| --- | --- | --- |
| `gBackupMapLayout` | `0x03005DC0` | the collision grid for the loaded map |
| `gMapHeader` | `0x02037318` | warps (doors, stairs), objects, connections |
| `gMapGroups` / `gRegionMapEntries` | `0x08486578` / `0x085A147C` | real place names for any map |

Gen 3 packs collision straight into each map-grid entry (bits 10-11), so the
agent knows what is walkable without bumping into it. That turns navigation
into an ordinary graph problem:

**Code does the pathfinding. The model chooses the destination.** Every turn
the agent builds the list of places it could reach — each exit and who it
leads to, each person or object, each map border that has a real connection —
runs breadth-first search to get exact step counts, and hands the model a
named menu plus an ASCII view of the surroundings:

```
NAV map 0.9 (10,3) -> exit at (14,8) into LITTLEROOT TOWN (indoors, map 1.2), 11 step(s)
MAP entered LITTLEROOT TOWN (indoors, map 1.3) — 10x8, 1 exit(s), 4 object(s)
NAV arrived at the object at (3, 4) — talking to it
```

This is the division of labour the TypeSafe skill argues for. Asking a model
which of four directions to press is asking it to do geometry badly; asking it
which of six named destinations serves the objective is a judgment it is
actually good at. Confidence rose from ~0.3-0.5 on direction guesses to
0.6-1.0 on destination choices, and the same exploration costs roughly a third
as many calls.

Names matter more than they sound. `map 0.9` is meaningless next to an
objective that says "Littleroot Town"; `LITTLEROOT TOWN (town, map 0.9)` is
something a model can reason about. Several maps share a name — every building
in a town is named after the town — so the raw id stays alongside it.

#### Things the map layer must get right

Each of these was a live failure first:

- **Routes must not clip a warp.** Walking over a door teleports you, so every
  non-goal warp is an obstacle during search.
- **Edges only count where `connections` says so.** Indoor maps have none, and
  their walkable border tiles are dead ends however inviting they look.
- **A doorway is often two tiles wide**, both separate warps to the same place.
  Offered separately the agent paces between them, so warps are deduplicated by
  destination.
- **An "object" on a warp tile is the door sprite**, not someone to talk to.
- **Arriving somewhere must do something.** Walking to an object and then
  re-deciding is why the agent shuttled between two pieces of bedroom
  furniture; it now interacts on arrival and remembers it did.
- **Pressing a direction you are not facing only turns you.** Every step
  retries once before concluding it hit a wall, or the agent invents walls
  everywhere.

### Exploration when there is no map: per-tile walls, frontier first

The blind logic below still runs whenever the grid is unreadable — cutscenes,
transitions, anything that leaves the layout stale.


Blocked directions are remembered **per tile** and never globally cleared. A
global list wiped on each successful step can never learn "up is a wall here".

Per-tile memory alone is still not enough, because the step *into* a dead end
succeeds — nothing marks the tile you came from, so the agent oscillates
between two squares forever. The fix is knowing where each direction **leads**:
the agent tracks visit counts per tile and, when the model's choice would walk
back into ground it has already covered while unexplored neighbours exist,
takes the unexplored one instead and says so in the log:

```
OVERWORLD up leads back to a tile seen 3x — taking right into unexplored ground instead
OVERWORLD up is a known wall at (7,2) — going down instead
```

The model still picks among equally-unexplored options; code stops it
retracing. `tests/test_integration.py` pins this with a dead-end corridor and a
brain that votes "up" forever — it must still cover the whole corridor.

### Cost: never pay twice for the same answer

The agent asks Jev only when the situation is genuinely new — that is, when the
last step actually moved. While pinned in place it probes in code, because
re-asking with near-identical state buys the same answer. An early build called
once per step and burned 166 calls and 132k input tokens in under two minutes
of being stuck; the regression test now holds a permanently stuck agent to a
handful of calls across 40 steps.

### Menu navigation is address-free

Reading the battle cursor position would mean two more fragile addresses.
Instead the agent *anchors*: pressing Up then Left puts a 2×2 menu cursor in
the top-left cell from anywhere, so any cell is reachable by a fixed sequence.
No cursor address required, and it self-corrects if a press is dropped.

## Testing

```bash
./.venv/bin/python tests/test_offline.py   # no emulator, ROM, or key needed
lua tests/test_bridge_lua.lua              # drives bridge.lua under stubs
```

The Python suite round-trips the Gen 3 substructure encryption across multiple
personality values (each selects a different substructure ordering), decodes a
synthetic battle Pokémon, checks the damage model, and runs `bridge.py` against
a fake server speaking the same protocol. The Lua suite drives the command
handler and asserts the exact per-frame button mask sequence.

## Tuning

The thresholds in `brain.py` are starting points, not tuned constants — the
TypeSafe docs are explicit that thresholds should be set on your own data and
consequences. Watch a few dozen battles, then adjust:

```python
if is_wild and run_p > 0.80 and not has_bench:      decision.action = "run"
elif switch_p > 0.75 and danger_p > 0.6 and has_bench: decision.action = "switch"
```

Raw probabilities are logged, so you can see what the model actually said
before deciding where to put the line.

## Known limits

- The agent explores toward frontiers; it does not plan a campaign. It will
  systematically open up a region, but it has no notion of "Rustboro is
  north-west of here" beyond what it has already walked.
- Story gates are discovered by bumping into them, not understood. The agent
  learns that a door will not open, not why, and cannot work out that talking
  to a particular person is what unlocks it.
- `should_heal` is reported but not acted on; routing to a Pokémon Center is
  not implemented.
- The in-battle switch path (Pokémon menu → pick → confirm) is the least
  tested sequence. Watch it the first few times.
- Item use, HMs, the Bag, and PC box management are not implemented.
- Double battles decode only battler slots 0 and 1.
- Tested against Emerald US (BPEE; mGBA reports it as `AGB-BPEE`). Other
  revisions need `config.json` edits.
