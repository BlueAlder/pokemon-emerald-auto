# `scripts/play.py`: the command line

`scripts/play.py` plays Pokémon Emerald by itself, from power-on (or from a
checkpoint) to the Hall of Fame. It works through the milestones of the route
in `pokeauto/emerald.py` one by one, saves a checkpoint after each, and logs
what it does.

In a terminal it opens an interactive, colourful **TUI**: the log scrolls at
the top, and the current goal, the run's stats and the party are shown at the
bottom. You can pause the game at any time. When the output is not a
terminal (a pipe, a file, CI) or you pass `--no-tui`, it prints plain log
lines as before.

## Installation

Use Python 3.12 (stable-retro has no wheels for newer versions yet):

```bash
python3.12 -m venv .venv
./.venv/bin/pip install -r requirements.txt     # stable-retro, pillow, numpy, textual
```

Put the US Emerald ROM at `roms/emerald.gba`, or point `--rom` or
`$POKEAUTO_ROM` at it.

## Quick start

```bash
./.venv/bin/python scripts/play.py                          # new game, headless, TUI
./.venv/bin/python scripts/play.py --resume badge_mind      # continue from a checkpoint
./.venv/bin/python scripts/play.py --stop-after badge_rain  # stop after that milestone
./.venv/bin/python scripts/play.py --backend mgba           # drive the mGBA app (watchable)
./.venv/bin/python scripts/play.py --backend mgba --new-game  # ...starting over, whatever mGBA saved
./.venv/bin/python scripts/play.py --no-tui | tee out.log   # plain log lines
```

On macOS, run long jobs under `caffeinate -dimsu` (see
[Troubleshooting](#troubleshooting)):

```bash
caffeinate -dimsu ./.venv/bin/python scripts/play.py
```

## Options

| Flag | Default | What it does |
| --- | --- | --- |
| `--rom PATH` | `roms/emerald.gba`, then `$POKEAUTO_ROM`, then `~/Downloads/Pokemon - Emerald Version (USA, Europe).gba` | The ROM to play. The first candidate that exists wins. |
| `--backend headless\|mgba` | `headless` | `headless` runs mGBA's core in-process (stable-retro) with no frame limit, about 45x real time. `mgba` drives the mGBA desktop app through `lua/bridge.lua` at normal speed, so you can watch. |
| `--port N` | `8888` | TCP port of the mGBA bridge (`--backend mgba` only). |
| `--resume NAME` | none (new game) | Load `runs/checkpoints/NAME.state` before playing (headless only). `NAME` is a milestone name, or `manual` for the checkpoint saved with the `s` key. |
| `--stop-after NAME` | none (play to the end) | Stop successfully after milestone `NAME` is done. |
| `--log LEVEL` | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `--live SECONDS` | `5` | How often the headless backend writes the current frame to `runs/live.png`. |
| `--no-tui` | off | Print plain log lines instead of starting the TUI. This is automatic when stdout or stdin is not a terminal. |
| `--start-paused` | off | TUI only: start paused, so you can look around first. Press space to start. |
| `--new-game` | off | Start a new game even if the cartridge holds a save: soft reset (A+B+SELECT+START), choose NEW GAME at the title menu, then play from the truck. Without it, mGBA continues whatever game its `.sav` holds. Can't be combined with `--resume`. |

Milestone names are the first argument of each `Milestone(...)` in
`pokeauto/emerald.py`, for example `set_clock`, `starter`, `badge_stone`,
`reach_lilycove`, `victory_road` or `champion`. The runner skips milestones
that are already done, so resuming from any save works.

Examples:

```bash
# A short smoke test: the first few milestones of a new game, no TUI.
./.venv/bin/python scripts/play.py --no-tui --stop-after options

# Continue a run from the Victory Road checkpoint, starting paused.
./.venv/bin/python scripts/play.py --resume victory_road --start-paused

# Play up to the fifth gym, with more detail in the log.
./.venv/bin/python scripts/play.py --stop-after badge_balance --log DEBUG
```

### Exit status

| Code | Meaning |
| --- | --- |
| `0` | The route finished (the Hall of Fame, or the `--stop-after` milestone). |
| `1` | The run failed (a milestone could not be completed), crashed, or was stopped with `q` / Ctrl+C. |

The last log line is always `finished=<True|False> in <N>s wall, game time <H:MM>`.
The TUI prints it again after it closes.

## The TUI

```
╭─ Log ────────────────────────────────────────────────────────────────────────────────────────╮
│01:37:51 --- done rayquaza (1912s elapsed, game 7:49)                                         │
│01:37:51 === MILESTONE waterfall === MAP_SOOTOPOLIS_CITY (31, 34) badges=7 money=58693 ...    │
│01:37:51 GOTO MAP_SOOTOPOLIS_CITY: 1 steps from MAP_SKY_PILLAR_TOP (14,9)                     │
│01:37:55 FLEW to MAP_SOOTOPOLIS_CITY ok                                                       │
│01:37:57 TEACH ITEM_HM07 (MOVE_WATERFALL) to AZUMARILL, replacing slot 3                      │
│01:37:58 BATTLE T1194 SWAMPERT 201/201 vs TENTACOOL 37/37 -> move 3 (SURF ~208dmg KO)         │
│01:37:58 PAUSED at frame 1234567                                                              │
╰───────────────────────────────────────────────────────────────────────────────── following ──╯
 ⏸  PAUSED: the emulator is frozen. Press space to resume.
╭─ Current goal ─────────────────────╮╭─ Party ────────────────────────────────────────────────╮
│ GOAL  enter_league  ★ boss         ││Pokémon       HP                     Moves (PP)         │
│cross Victory Road and show the     ││Azumarill L52 ███░░░░░░░ 64/177      Facade 14 · Streng…│
│guards all eight badges             ││Swampert L74  ██████████ 266/266     Water Pulse 20 · E…│
│75/80 ████████████████░░ 92%        ││Hariyama L40  ██████████ 174/174     Rock Tomb 10 · Kno…│
│targets lead L74 · team L66         ││Golbat L40    ██████████ 110/110     Bite 25 · Wing Att…│
│ GRINDING  Azumarill L52 → L66      │╰────────────────────────────────────────────────────────╯
│  (team target)                     │
│██░░░░░░░░░░░░░░░░░░ 10%  15 battles│
│at Victory Road 1F  (started L51)   │
│Team → L66  Swampert 74 ✓ ·         │
│  ▸Azumarill 52 · Hariyama 40 · …   │
│▸ BATTLE T341 AZUMARILL 64/177 vs … │
╰────────────────────────────────────╯
╭─ Run ──────────────────────────────╮
│ PAUSED   headless  frame 444,606   │
│Badges ● ● ● ● ● ● ● ● 8/8          │
│Money ₽89,214  Game 11:37           │
│Wall 0:03:09                        │
│Map Victory Road 1F (14,39)         │
│Battles 267  Steps 470              │
│Deaths 0 whiteouts · 0 faints       │
╰────────────────────────────────────╯
 space Pause/Resume  q Quit  b Battle turns  f Follow log  s Save state  c Clear log
```

**Log (top).** The run's log, coloured by what each line is about:

| Line | Colour |
| --- | --- |
| `=== MILESTONE ...` | bold purple |
| `--- done ...` | bold green |
| `BATTLE T<n> ...` (one line per turn) | dim cyan |
| other `BATTLE ...` | cyan |
| `GOTO ...` | blue |
| `GRIND ...` | yellow |
| `HEALED`, `HEALTH`, `ITEM HEAL` | green |
| `FLEW`, `FLY` | bright cyan |
| `CAUGHT`, `TEACH`, `LEARN` | purple |
| `SHOP`, `LEAGUE`, `RESTOCK` | gold |
| `PROMPT`, `MULTICHOICE` | dim |
| `PAUSED`, `RESUMED`, `CHECKPOINT`, `STOPPED` | bold white |
| warnings and `stuck` | orange |
| errors, `failed`, crashes and tracebacks | bold red |

Timestamps are dim. The pane follows the newest line. Scroll up with the
mouse wheel, the arrow keys, PageUp/PageDown or Home, and it stays where you
put it (the bottom-right corner says `scrolled: f to follow`). Press `f` or
End, or scroll back to the bottom, to follow again. The pane keeps the last
5000 lines; `runs/play.log` has everything.

**Current goal.** The milestone being worked on and its plain-English goal,
its place in the route (`58/80` with a progress bar), the attempt number
(red after a failed attempt), a `★ boss` mark for important fights, and level
targets (`lead L74 · team L66`) when the milestone trains first. Below that,
what the run is doing for it right now:

* **Grinding:** who is training, current level → target level and whether
  it is the lead's or the team's target. A bar shows the experience gained
  towards the target level, followed by the battle count and the map, with
  the level it started from.
* **Training the team:** every member with its level against the target, `✓`
  once reached, and `▸` on the one training now.
* **Anything else:** where the current walk is headed (`➜ Petalburg City Gym
  (7,15)`, `➜ nearest Pokemon Center`, ...).

The last line is the latest activity (the last GOTO, BATTLE, GRIND, FLEW,
HEALED, ... log line).

**Run.** The run state (`STARTING` cyan, `RUNNING` green, `PAUSED`
yellow, `FINISHED` bright green, `FAILED` red, `STOPPED` orange; `PAUSING` and
`STOPPING` while a request is on its way), the backend and emulator frame,
the eight badges in their colours (earned ones filled), money, in-game play
time, wall-clock time, the map and position, and the battle and step
counters. **Deaths** counts whiteouts (battles lost: the whole party fainted,
and the game sends you back to a Pokémon Center) and faints (any of your
Pokémon knocked out). Both are red once non-zero, and both cover this session
only.

**Party.** One row per Pokémon: species and level, an HP bar (green above
50%, yellow above 20%, red below) with the numbers, a status badge (`SLP`,
`PSN`, `TOX`, `BRN`, `FRZ`, `PAR`), the moves with their PP (red when a move is
out of PP), and the held item. Fainted Pokémon are struck through and marked
`FAINTED`.

The overview refreshes about 4 times a second on the headless backend and
once a second on mGBA, where every RAM read is a round trip to the bridge.

When the run ends, the TUI stays open with a banner (`RUN FINISHED`, `RUN
FAILED` or `RUN STOPPED`) and the final state until you press `q`.

### Keys

| Key | Action |
| --- | --- |
| `space` or `p` | Pause or resume the game. |
| `q` | Quit. While the run is going, press `q` twice within 3 seconds: this stops the run and closes the TUI. Once the run has ended, one `q` closes it. |
| `Ctrl+C` | Stop the run and quit at once, without confirmation. |
| `b` | Hide or show the routine `BATTLE T<n>` turn lines (other battle lines stay). |
| `f` or `End` | Follow the end of the log again. |
| `s` | Save a checkpoint now to `runs/checkpoints/manual.state` (headless only). Works while paused. Resume it with `--resume manual`. |
| `c` | Clear the log pane. `runs/play.log` is not touched. |
| arrows, PageUp/PageDown, Home, mouse wheel | Scroll the log. |

### Pausing

Pausing takes effect at the next emulator step: usually at once, or when a
path search in progress finishes (the Run pane says `PAUSING` meanwhile). It
works at any moment, including in the middle of a battle or a menu. The game
thread then waits between two emulator calls, so nothing is lost and
resuming carries on exactly where it stopped.

* **headless:** the emulator core simply stops stepping.
* **mgba:** the bridge runs the game in lockstep with the player: emulated
  time only passes while the player asks for frames. So a paused run freezes
  the mGBA window too. Don't press keys in mGBA meanwhile; the player expects
  the game to be where it left it.

### Quitting and checkpoints

Quitting stops the run cleanly between two emulator calls: the log gets
`STOPPED by the user` and the `finished=False ...` line, the last frame is saved
to `runs/final.png`, and the exit status is 1. Quitting while paused works.

Checkpoints are written after every completed milestone to
`runs/checkpoints/<milestone>.state` (headless only), plus
`runs/checkpoints/manual.state` when you press `s`. To continue a stopped run,
resume from the latest checkpoint:

```bash
ls -t runs/checkpoints | head -3
./.venv/bin/python scripts/play.py --resume <milestone>
```

The runner skips everything that is already done, so a checkpoint of a
milestone continues with the next one.

## Files

| Path | What |
| --- | --- |
| `runs/play.log` | The full log, appended to by every run (`HH:MM:SS message`, the same lines as the TUI and `--no-tui` show). Delete it to start clean. |
| `runs/checkpoints/*.state` | Savestates after each milestone (headless), and `manual.state`. |
| `runs/live.png` | The current frame, refreshed every `--live` seconds (headless). |
| `runs/final.png` | The last frame of the run. |
| `runs/debug/` | Snapshots of anything unexpected. |
| `runs/play.lock` | Holds the pid of the running player; a second run in the same checkout refuses to start. |

## Plain output (`--no-tui`)

With `--no-tui`, or whenever stdout or stdin is not a terminal, the player
prints each log line to stderr (`HH:MM:SS message`) and writes the same lines to
`runs/play.log`. Use this for pipes, `nohup`, CI and scripts:

```bash
./.venv/bin/python scripts/play.py --no-tui --stop-after options 2>&1 | tail
nohup caffeinate -dimsu ./.venv/bin/python scripts/play.py --no-tui > run.out 2>&1 &
tail -f runs/play.log
```

Ctrl+C stops a plain run the same way `q` does in the TUI. There is no pause
in this mode.

## Troubleshooting

* **The terminal is small.** The TUI adapts: below 110 columns the party moves
  under the goal and run panes, below 80 columns everything is stacked, and
  the bottom panes scroll when they don't fit. The log needs at least a few
  rows; 120x40 or larger is comfortable.
* **Colours look wrong or flat.** Use a terminal with true colour (iTerm2,
  WezTerm, kitty, the VS Code terminal, recent Terminal.app). `$TERM` should be
  `xterm-256color` or similar; `COLORTERM=truecolor` helps. Inside tmux, enable
  true colour (`set -g default-terminal "tmux-256color"` and
  `set -as terminal-overrides ",*:Tc"`). `NO_COLOR=1` turns colours off.
* **The run is very slow on macOS.** macOS throttles background processes to a
  few percent of a CPU while the machine idles, which turns a one-hour
  playthrough into most of a day. Run under `caffeinate -dimsu`.
* **`another run (pid N) is using runs/`.** Another player is running in this
  checkout. Stop it first (or delete a stale `runs/play.lock` if that pid is
  gone; the check does this by itself when the pid no longer exists).
* **With `--backend mgba` it "loads the last run" and says the route is
  complete.** mGBA keeps an in-game save (`<rom name>.sav`) next to the ROM
  you opened and loads it every time. Beating the Elite Four saves the game,
  so after one finished run the title menu's first option, CONTINUE, is a
  finished game; the player continues it and has nothing left to do (it logs
  a warning saying so). Pass `--new-game` to start over. Your `.sav` is only
  overwritten when the new game saves too (at its own Hall of Fame). To keep
  the finished one, copy the `.sav` first.
* **`the mGBA bridge ... dropped the connection`** (was a crash:
  `OSError: [Errno 22] Invalid argument`). mGBA accepted the connection and
  closed it: no ROM running, the script not loaded after a restart, or mGBA
  shutting down. Open the ROM, load `lua/bridge.lua` again, and retry.
* **`could not reach the mGBA bridge`.** Open the ROM in mGBA and load
  `lua/bridge.lua` via *Tools ▸ Scripting… ▸ File ▸ Load script*. It listens on
  127.0.0.1:8888 (`--port` to change).
* **`lua/bridge.lua is outdated`, or odd mGBA behaviour after an update.**
  Restart mGBA and load `lua/bridge.lua` again. A second copy cannot bind the
  port while the old one is still loaded.
* **`s` says it is unsupported.** Manual checkpoints need the headless backend;
  with mGBA, use mGBA's own savestates (*File ▸ Save State*).
* **Selecting text with the mouse does not work.** The TUI uses the mouse
  for scrolling. Hold Option (iTerm2, Terminal.app) or Shift (most other
  terminals) while dragging, or copy from `runs/play.log`.
* **The TUI is not wanted at all.** Pass `--no-tui`, or pipe the output.
