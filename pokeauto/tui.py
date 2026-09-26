"""Textual TUI for a run: the log on top, goal / run stats / party below.

The app never touches the game. It polls `RunControl.snapshot()` and drains
the log queue; pause, stop and checkpoint requests are flags that the worker
thread acts on inside its emulator hook (see runstate.py).
"""
from __future__ import annotations

import logging
import re
import time
from collections import deque

from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, RichLog, Static

from .runstate import ENDED, MonView, RunControl, Snapshot, map_name

ui_log = logging.getLogger("pokeauto.tui")

# Explicit RGB colours: Textual maps the 16 ANSI names through its theme, where
# magenta and red come out nearly the same.
ORANGE = "#ff8700"
RED = "#ff5f5f"
MAGENTA = "#d787ff"
GREEN = "#5fd75f"
YELLOW = "#ffff5f"
GOLD = "#ffd700"
CYAN = "#5fd7ff"
BRIGHT_CYAN = "#00ffff"
BLUE = "#5f87ff"
LIME = "#87ff5f"
PINK = "#ff87ff"
DARK_RED = "#d70000"
STATE_STYLE = {
    "starting": f"bold black on {CYAN}",
    "running": f"bold black on {GREEN}",
    "paused": f"bold black on {YELLOW}",
    "finished": f"bold black on {LIME}",
    "failed": f"bold white on {DARK_RED}",
    "stopped": f"bold black on {ORANGE}",
}
STATUS_STYLE = {"SLP": "bold white on grey42", "PSN": "bold white on purple",
                "TOX": "bold white on dark_magenta", "BRN": "bold white on red3",
                "FRZ": f"bold black on {CYAN}", "PAR": f"bold black on {YELLOW}"}
# Stone, Knuckle, Dynamo, Heat, Balance, Feather, Mind, Rain.
BADGE_COLORS = ["grey70", ORANGE, YELLOW, RED, "wheat1", "sky_blue1", "hot_pink", "dodger_blue1"]

ROUTINE_BATTLE = re.compile(r"^BATTLE T\d+ ")
# (first word(s) of the message, style), checked in order.
KEYWORD_STYLES = [
    (("=== MILESTONE",), f"bold {MAGENTA}"),
    (("--- done",), f"bold {GREEN}"),
    (("ROUTE",), f"bold {PINK}"),
    (("PAUSED", "RESUMED", "CHECKPOINT", "STOP"), "bold bright_white"),
    (("PROMPT", "MULTICHOICE", "ITEM PROMPT", "BATTLE PROMPT"), "dim"),
    (("BATTLE T",), f"dim {CYAN}"),
    (("BATTLE",), CYAN),
    (("GOTO",), "dodger_blue1"),
    (("GRIND",), YELLOW),
    (("HEALED", "HEALTH", "ITEM HEAL"), GREEN),
    (("FLEW", "FLY"), BRIGHT_CYAN),
    (("CAUGHT", "TEACH", "LEARN"), MAGENTA),
    (("SHOP", "LEAGUE", "RESTOCK"), "gold1"),
]


def line_style(levelno: int, message: str) -> str:
    if levelno >= logging.ERROR or "Traceback" in message or " failed" in message or "crashed" in message:
        return f"bold {RED}"
    if levelno >= logging.WARNING or "stuck" in message:
        return ORANGE
    for words, style in KEYWORD_STYLES:
        if message.startswith(words):
            return style
    return ""


def style_line(levelno: int, line: str) -> Text:
    """Colour one formatted log record ("HH:MM:SS message", maybe multi-line)."""
    m = re.match(r"^(\d\d:\d\d:\d\d) ", line)
    stamp, message = (m.group(1), line[m.end():]) if m else ("", line)
    text = Text()
    if stamp:
        text.append(stamp + " ", style="dim")
    text.append(message, style=line_style(levelno, message))
    return text


def is_routine(line: str) -> bool:
    return bool(ROUTINE_BATTLE.match(line[9:]))


def fmt_seconds(s: float) -> str:
    s = int(max(s, 0))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


def bar(frac: float, width: int, style: str, empty: str = "grey23") -> Text:
    n = round(max(0.0, min(1.0, frac)) * width)
    return Text.assemble(("█" * n, style), ("█" * (width - n), empty))


def hp_style(frac: float) -> str:
    return GREEN if frac > 0.5 else YELLOW if frac > 0.2 else RED


# -- panes ----------------------------------------------------------------------------

def render_goal(s: Snapshot) -> Text:
    t = Text()
    t.append(" GOAL ", style=f"bold black on {MAGENTA}")
    t.append(" ")
    t.append(s.milestone or "(waiting for the route)", style=f"bold {MAGENTA}")
    if s.important:
        t.append("  ★ boss", style="bold gold1")
    t.append("\n")
    if s.hint:
        t.append(s.hint + "\n", style="italic")
    if s.total:
        idx = max(s.index, 0)
        finished = bool(s.done) and s.done[-1] == s.milestone     # counts once done
        frac = (idx + finished) / s.total
        t.append(f"{idx + 1}/{s.total} ", style="bold")
        t.append_text(bar(frac, 20, MAGENTA))
        t.append(f" {frac:.0%}", style=MAGENTA)
        if s.attempt:
            t.append(f"  attempt {s.attempt}/{s.attempts}",
                     style=f"bold {RED}" if s.attempt > 1 else "dim")
        t.append("\n")
    targets = []
    if s.min_level:
        targets.append(f"lead L{s.min_level}")
    if s.team_level:
        targets.append(f"team L{s.team_level}")
    if targets:
        t.append("targets ", style="dim")
        t.append(" · ".join(targets) + "\n", style=YELLOW)
    render_now(t, s)
    if s.activity:
        t.append("▸ ", style="bold")
        t.append(s.activity, style=line_style(logging.INFO, s.activity) or "white")
    return t


def pretty_desc(desc: str) -> str:
    """'MAP_PETALBURG_CITY_GYM(7,15)' -> 'Petalburg City Gym (7,15)'."""
    return re.sub(r"MAP_[A-Z0-9_]+", lambda m: map_name(m.group(0)), desc).replace("(", " (")


def render_now(t: Text, s: Snapshot) -> None:
    """What the run is doing for this milestone right now."""
    g = s.grind
    if g is not None:
        t.append(" GRINDING ", style=f"bold black on {YELLOW}")
        t.append(f" {g.species.capitalize()} ", style="bold")
        t.append(f"L{g.level}", style=f"bold {CYAN}")
        t.append(" → ", style="dim")
        t.append(f"L{g.target}", style=f"bold {YELLOW}")
        if g.why:
            t.append(f"  ({g.why})", style="dim")
        t.append("\n")
        t.append_text(bar(g.progress, 20, YELLOW))
        t.append(f" {g.progress:.0%}", style=YELLOW)
        t.append(f"  {g.battles} battles\n", style="dim")
        if g.map_id:
            t.append("at ", style="dim")
            t.append(map_name(g.map_id), style="dodger_blue1")
            t.append(f"  (started L{g.start})\n", style="dim")
    if s.team and s.team_target:
        t.append("Team → ", style="bold")
        t.append(f"L{s.team_target}  ", style=f"bold {YELLOW}")
        for i, (species, level) in enumerate(s.team):
            ready = level >= s.team_target
            current = g is not None and g.species == species and not ready
            t.append(("▸" if current else "") + species.capitalize(),
                     style=f"bold {YELLOW}" if current else ("" if ready else "dim"))
            t.append(f" {level}" + (" ✓" if ready else ""),
                     style=GREEN if ready else CYAN)
            if i < len(s.team) - 1:
                t.append(" · ", style="dim")
        t.append("\n")
    if g is None and s.goal_desc:
        t.append("➜ ", style=f"bold {BLUE}")
        t.append(pretty_desc(s.goal_desc) + "\n", style=BLUE)


def render_run(s: Snapshot, pending: str = "") -> Text:
    t = Text()
    label = pending or s.state
    t.append(f" {label.upper()} ", style=STATE_STYLE.get(s.state, "bold reverse"))
    t.append(f"  {s.backend}", style="dim")
    t.append(f"  frame {s.frame:,}\n", style="dim")
    t.append("Badges ", style="bold")
    for i, color in enumerate(BADGE_COLORS):
        t.append("● " if i < s.badges else "○ ", style=color if i < s.badges else "grey35")
    t.append(f"{s.badges}/8\n", style="bold")
    t.append("Money ", style="bold")
    t.append(f"₽{s.money:,}" if s.money is not None else "-", style="gold1")
    t.append("   Game ", style="bold")
    t.append(s.play_time or "-", style=CYAN)
    t.append("   Wall ", style="bold")
    t.append(fmt_seconds((s.t_end or time.time()) - s.t_start), style=CYAN)
    t.append("\nMap ", style="bold")
    t.append(map_name(s.map_id) if s.map_id else "-", style="dodger_blue1")
    if s.pos:
        t.append(f" ({s.pos[0]},{s.pos[1]})", style="dim")
    t.append("\nBattles ", style="bold")
    t.append(f"{s.stats.get('battles', 0):,}", style=CYAN)
    t.append("   Steps ", style="bold")
    t.append(f"{s.stats.get('steps', 0):,}", style=BLUE)
    t.append("\nDeaths ", style="bold")
    t.append(f"{s.whiteouts} whiteout{'s' if s.whiteouts != 1 else ''}",
             style=f"bold {RED}" if s.whiteouts else GREEN)
    t.append(" · ", style="dim")
    t.append(f"{s.faints} faint{'s' if s.faints != 1 else ''}",
             style=RED if s.faints else GREEN)
    if s.message:
        t.append("\n" + s.message, style="italic bright_white")
    return t


def render_mon(m: MonView) -> list:
    if m.is_egg:
        return [Text("Egg", style="dim"), Text(""), Text(""), Text(""), Text("")]
    name = Text(f"{m.species.capitalize()}", style="bold strike dim" if m.fainted else "bold")
    name.append(f" L{m.level}", style="dim" if m.fainted else CYAN)
    if m.fainted:
        hp = Text(" FAINTED ", style=f"bold white on {DARK_RED}")
    else:
        hp = bar(m.hp_frac, 10, hp_style(m.hp_frac))
        hp.append(f" {m.hp}/{m.max_hp}", style=hp_style(m.hp_frac))
    status = Text(f" {m.status} ", style=STATUS_STYLE[m.status]) if m.status else Text("")
    moves = Text()
    for i, mv in enumerate(m.moves):
        if i:
            moves.append(" · ", style="grey35")
        moves.append(mv.name, style="dim" if mv.pp == 0 else "")
        moves.append(f" {mv.pp}", style=f"bold {RED}" if mv.pp == 0 else f"dim {CYAN}")
    item = Text(m.item, style="gold1") if m.item else Text("")
    return [name, hp, status, moves, item]


def render_party(s: Snapshot) -> Table:
    t = Table(box=None, expand=True, show_header=True, header_style="bold dim",
              padding=(0, 1), pad_edge=False)
    t.add_column("Pokémon", no_wrap=True, min_width=12)
    t.add_column("HP", no_wrap=True, min_width=10)
    t.add_column("", no_wrap=True, width=5)
    t.add_column("Moves (PP)", ratio=1, overflow="ellipsis", no_wrap=True)
    t.add_column("Item", no_wrap=True, overflow="ellipsis", max_width=14)
    for m in s.party:
        t.add_row(*render_mon(m))
    if not s.party:
        t.add_row(Text("(no party yet)", style="dim"), "", "", "", "")
    return t


def render_banner(s: Snapshot, stopping: bool) -> Text | None:
    report = f" Report: {s.report.path}." if s.report is not None else ""
    if stopping and s.state not in ENDED:
        return Text(" ■ Stopping the run… ", style=f"bold black on {ORANGE}")
    if s.state == "paused":
        return Text(" ⏸  PAUSED: the emulator is frozen. Press space to resume. ",
                    style=f"bold black on {YELLOW}")
    if s.state == "finished":
        return Text(f" ✔ RUN FINISHED.{report} Press q to exit. ", style=f"bold black on {LIME}")
    if s.state == "failed":
        return Text(f" ✖ RUN FAILED: see the log above (and runs/play.log).{report} "
                    "Press q to exit. ", style=f"bold white on {DARK_RED}")
    if s.state == "stopped":
        return Text(f" ■ RUN STOPPED.{report} Press q to exit. ", style=f"bold black on {ORANGE}")
    return None


# -- widgets ----------------------------------------------------------------------------

class LogView(RichLog):
    """A RichLog that follows the tail until the user scrolls up."""

    def add(self, levelno: int, line: str, scroll_end: bool | None = None) -> None:
        # Wrap at the pane's width (RichLog would otherwise measure against the console).
        width = self.scrollable_content_region.width
        self.write(style_line(levelno, line), width=width if width > 0 else None,
                   scroll_end=scroll_end)

    def on_resize(self) -> None:
        # The banner showing up shrinks the pane: keep the last line in view.
        if self.auto_scroll:
            self.call_after_refresh(self.scroll_end, animate=False)

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if new_value < old_value and not self.is_vertical_scroll_end:
            self.auto_scroll = False
        elif self.is_vertical_scroll_end:
            self.auto_scroll = True


class PlayApp(App):
    TITLE = "pokeauto"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { layout: vertical; background: $background; }
    #log { height: 1fr; min-height: 3; border: round $primary; border-title-color: $accent;
           border-subtitle-color: $text-muted; scrollbar-size-vertical: 1; }
    #banner { height: auto; width: 100%; display: none; }
    #banner.show { display: block; }
    #report { height: auto; max-height: 50%; display: none; overflow-y: auto; padding: 0 1;
              border: round #ffd700; border-title-color: #ffd700; }
    #report.show { display: block; }
    #overview { height: auto; max-height: 60%; overflow-y: auto; }
    #left { width: 1fr; min-width: 30; height: auto; }
    #goal { border: round #d787ff; border-title-color: #d787ff; padding: 0 1; height: auto; }
    #run { border: round #5fd75f; border-title-color: #5fd75f; padding: 0 1; height: auto; }
    #party { width: 2fr; border: round #5fd7ff; border-title-color: #5fd7ff; padding: 0 1; height: auto; }
    /* Narrow: party below goal/run; tiny: everything stacked. */
    .narrow #overview { layout: vertical; }
    .narrow #left { width: 100%; layout: horizontal; }
    .narrow #goal, .narrow #run { width: 1fr; }
    .narrow #party { width: 100%; }
    .tiny #left { layout: vertical; }
    .tiny #goal, .tiny #run { width: 100%; }
    """
    BINDINGS = [
        Binding("space,p", "toggle_pause", "Pause/Resume"),
        Binding("q", "quit_run", "Quit"),
        Binding("b", "toggle_battles", "Battle turns"),
        Binding("f,end", "follow", "Follow log"),
        Binding("s", "save", "Save state"),
        Binding("c", "clear_log", "Clear log"),
        Binding("ctrl+c", "force_quit", "Quit now", show=False, priority=True),
    ]

    def __init__(self, control: RunControl, subtitle: str = ""):
        super().__init__()
        self.control = control
        self.sub_title = subtitle
        self.lines: deque[tuple[int, str]] = deque(maxlen=5000)
        self.hide_battles = False
        self.stopping = False
        self._stop_at = 0.0
        self._quit_armed = 0.0
        self._rewrap_timer = None
        self._report = None

    def compose(self) -> ComposeResult:
        yield LogView(id="log", max_lines=5000, wrap=True, markup=False, highlight=False)
        yield Static(id="banner")
        yield Static(id="report")
        with Horizontal(id="overview"):
            with Vertical(id="left"):
                yield Static(id="goal")
                yield Static(id="run")
            yield Static(id="party")
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one(LogView)
        log.border_title = "Log"
        self.query_one("#goal").border_title = "Current goal"
        self.query_one("#run").border_title = "Run"
        self.query_one("#party").border_title = "Party"
        self.query_one("#report").border_title = "Run report"
        log.focus()
        self._update_narrow()
        self.set_interval(0.1, self.poll_log)
        self.set_interval(0.25, self.refresh_overview)
        self.refresh_overview()

    def on_resize(self) -> None:
        self._update_narrow()
        if self._rewrap_timer:
            self._rewrap_timer.stop()
        self._rewrap_timer = self.set_timer(0.3, self.rewrap)

    def _update_narrow(self) -> None:
        self.screen.set_class(self.size.width < 110, "narrow")
        self.screen.set_class(self.size.width < 80, "tiny")

    # -- polling ----------------------------------------------------------------------
    def poll_log(self) -> None:
        batch = self.control.drain_log()
        if not batch:
            return
        self.lines.extend(batch)
        log = self.query_one(LogView)
        for levelno, line in batch:
            if not (self.hide_battles and is_routine(line)):
                log.add(levelno, line)
        self._log_subtitle()

    def _log_subtitle(self) -> None:
        log = self.query_one(LogView)
        parts = ["following" if log.auto_scroll else "scrolled: f to follow"]
        if self.hide_battles:
            parts.append("battle turns hidden")
        log.border_subtitle = " · ".join(parts)

    def refresh_overview(self) -> None:
        s = self.control.snapshot()
        pending = ""
        if self.stopping and s.state not in ENDED:
            pending = "stopping"
        elif self.control.paused_requested and s.state == "running":
            pending = "pausing"
        self.query_one("#goal", Static).update(render_goal(s))
        self.query_one("#run", Static).update(render_run(s, pending))
        self.query_one("#party", Static).update(render_party(s))
        banner = render_banner(s, self.stopping)
        b = self.query_one("#banner", Static)
        b.set_class(banner is not None, "show")
        if banner is not None:
            b.update(banner)
        if s.report is not None and s.report is not self._report:
            self._report = s.report
            r = self.query_one("#report", Static)
            r.update(s.report.renderable())
            r.border_subtitle = s.report.path
            r.add_class("show")
        self._log_subtitle()
        if self.stopping and (self.control.done_event.is_set() or time.time() - self._stop_at > 15):
            self.poll_log()
            self.exit()

    # -- actions ----------------------------------------------------------------------
    def action_toggle_pause(self) -> None:
        if self.control.done_event.is_set() or self.stopping:
            return
        paused = self.control.toggle_pause()
        ui_log.info("TUI %s requested", "pause" if paused else "resume")
        self.refresh_overview()

    def action_quit_run(self) -> None:
        if self.control.done_event.is_set():
            self.exit()
            return
        now = time.time()
        if now - self._quit_armed > 3:
            self._quit_armed = now
            self.notify("Press q again to stop the run.", title="Quit?", severity="warning", timeout=3)
            return
        self._stop()

    def action_force_quit(self) -> None:
        if self.control.done_event.is_set():
            self.exit()
        else:
            self._stop()

    def _stop(self) -> None:
        if not self.stopping:
            ui_log.info("TUI stop requested")
            self.stopping, self._stop_at = True, time.time()
            self.control.stop()
        self.refresh_overview()

    def action_toggle_battles(self) -> None:
        self.hide_battles = not self.hide_battles
        self.rewrap()

    def rewrap(self) -> None:
        """Re-render the kept lines (after a filter change or a resize)."""
        log = self.query_one(LogView)
        follow = log.auto_scroll
        log.clear()
        for levelno, line in self.lines:
            if not (self.hide_battles and is_routine(line)):
                log.add(levelno, line, scroll_end=False)
        log.auto_scroll = follow
        if follow:
            log.scroll_end(animate=False)
        self._log_subtitle()

    def action_follow(self) -> None:
        log = self.query_one(LogView)
        log.auto_scroll = True
        log.scroll_end(animate=False)
        self._log_subtitle()

    def action_clear_log(self) -> None:
        self.lines.clear()
        log = self.query_one(LogView)
        log.clear()
        log.auto_scroll = True
        self._log_subtitle()

    def action_save(self) -> None:
        if self.control.done_event.is_set():
            self.notify("The run has ended.", severity="warning")
        elif self.control.save_manual is None:
            self.notify("Manual checkpoints are only supported on the headless backend.",
                        severity="warning")
        else:
            self.control.request_save()
            self.notify("Saving a checkpoint to runs/checkpoints/manual.state…")
