"""Run reports: what one run achieved and how long it took, compared with another.

Every run of scripts/play.py leaves runs/history/<id>.json (the record),
<id>.md (the report) and <id>.log (the run's own log lines). A record holds,
per milestone finished in the run, the wall seconds since the run started, the
in-game play time (seconds) and the attempts it took; plus deaths, battles and
the git commit, so a change of tactics can be tied to the time it saves.

In-game time is the metric (lower is better). Two runs that both started a
new game are compared on the game clock at each milestone ("absolute");
otherwise on the game time elapsed within each run ("elapsed").
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("pokeauto")

# The milestones a report is about, in route order, with friendly labels.
MAJOR = {
    "badge_stone": "Stone Badge (Roxanne)",
    "badge_knuckle": "Knuckle Badge (Brawly)",
    "badge_dynamo": "Dynamo Badge (Wattson)",
    "badge_heat": "Heat Badge (Flannery)",
    "badge_balance": "Balance Badge (Norman)",
    "badge_feather": "Feather Badge (Winona)",
    "badge_mind": "Mind Badge (Tate & Liza)",
    "badge_rain": "Rain Badge (Juan)",
    "enter_league": "Pokémon League entered",
    "sidney": "Elite Four: Sidney",
    "phoebe": "Elite Four: Phoebe",
    "glacia": "Elite Four: Glacia",
    "drake": "Elite Four: Drake",
    "champion": "Champion / Hall of Fame",
}
HOF = "champion"
NEW_GAME = "new game"
OUTCOMES = ("finished", "failed", "stopped", "crashed", "unknown")   # unknown: imported, log cut short


# -- formatting ---------------------------------------------------------------------

def label(name: str) -> str:
    return MAJOR.get(name, name)


def hms(s: float | None) -> str:
    if s is None:
        return "–"
    s = int(round(s))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


def signed(d: float | None) -> str:
    """A time difference: '-0:03:12', '+0:00:40', '±0:00:00'."""
    if d is None:
        return ""
    sign = "-" if d < 0 else "+" if d > 0 else "±"
    return sign + hms(abs(d))


def signed_n(d: int | None) -> str:
    return "" if d is None else f"{d:+d}" if d else "±0"


# -- records ------------------------------------------------------------------------

def git_info(root: Path) -> dict:
    """{'commit': short hash, 'dirty': uncommitted changes to tracked files}."""
    def run(*cmd: str) -> str:
        return subprocess.run(["git", *cmd], cwd=root, capture_output=True, text=True,
                              timeout=5).stdout.strip()
    try:
        return {"commit": run("rev-parse", "--short", "HEAD") or None,
                "dirty": bool(run("status", "--porcelain", "--untracked-files=no"))}
    except Exception:
        return {"commit": None, "dirty": None}


def new_record(run_id: str, **kw) -> dict:
    rec = {"version": 1, "id": run_id, "started": None, "ended": None, "outcome": None,
           "failed_at": None, "error": None, "backend": None, "start_point": None,
           "new_game": False, "first_milestone": None, "stop_after": None, "command": None,
           "git": {"commit": None, "dirty": None}, "wall_seconds": None,
           "game_start": None, "game_end": None, "game_elapsed": None,
           "deaths": {"whiteouts": None, "faints": None, "trainer_losses": None},
           "battles": None, "steps": None, "milestones": [], "compared_with": [],
           "log": f"{run_id}.log", "imported_from": None}
    rec.update(kw)
    return rec


def finalize(rec: dict) -> dict:
    """Fill the derived fields (labels, per-milestone elapsed game time)."""
    g0 = rec.get("game_start")
    for m in rec["milestones"]:
        m["label"] = label(m["name"])
        m["major"] = m["name"] in MAJOR
        m["game_elapsed"] = m["game"] - g0 if m.get("game") is not None and g0 is not None else None
    if rec.get("game_end") is not None and g0 is not None:
        rec["game_elapsed"] = rec["game_end"] - g0
    return rec


def done(rec: dict) -> dict[str, dict]:
    """name -> milestone entry; a milestone finished twice (the Elite Four after
    a loss) counts at its last finish."""
    return {m["name"]: m for m in rec.get("milestones", [])}


def furthest(rec: dict) -> dict | None:
    ms = rec.get("milestones") or []
    return ms[-1] if ms else None


def reached_hof(rec: dict) -> bool:
    return HOF in done(rec)


def game_at(rec: dict, m: dict | None, mode: str) -> int | None:
    if m is None:
        return None
    return m.get("game") if mode == "absolute" else m.get("game_elapsed")


def start_desc(rec: dict) -> str:
    sp = rec.get("start_point") or "?"
    first = rec.get("first_milestone")
    return sp if sp == NEW_GAME or not first or first in sp else f"{sp} (at {first})"


# -- history ------------------------------------------------------------------------

def load(path: Path) -> dict:
    return json.loads(Path(path).read_text())


def load_runs(history: Path) -> list[dict]:
    """All records, oldest first."""
    out = []
    for p in Path(history).glob("*.json"):
        try:
            out.append(load(p))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda r: (r.get("started") or "", r["id"]))


def find(runs: list[dict], key: str) -> dict | None:
    """A run by id, unique id prefix/substring, or 'last' (the newest)."""
    if key == "last":
        return runs[-1] if runs else None
    for r in runs:
        if r["id"] == key:
            return r
    hits = [r for r in runs if r["id"].startswith(key)] or [r for r in runs if key in r["id"]]
    if len(hits) > 1:
        raise KeyError(f"{key!r} matches {len(hits)} runs: " + ", ".join(r["id"] for r in hits[:5]))
    return hits[0] if hits else None


def comparable(a: dict, b: dict) -> bool:
    """Same starting point: the same first milestone (and new game or not)."""
    return (a["id"] != b["id"] and bool(a.get("first_milestone"))
            and a.get("first_milestone") == b.get("first_milestone")
            and bool(a.get("new_game")) == bool(b.get("new_game")))


def previous(rec: dict, runs: list[dict]) -> dict | None:
    """The latest earlier run from the same start that got anywhere."""
    earlier = [r for r in runs if comparable(r, rec) and r.get("milestones")
               and (r.get("started") or "") < (rec.get("started") or "~")]
    return earlier[-1] if earlier else None


def hof_time(rec: dict) -> int | None:
    m = done(rec).get(HOF)
    if m is None:
        return None
    return m.get("game_elapsed") if m.get("game_elapsed") is not None else m.get("game")


def best(rec: dict, runs: list[dict], before: bool = True) -> dict | None:
    """The run from the same start with the least in-game time to the Hall of Fame."""
    cands = [r for r in runs if comparable(r, rec) and hof_time(r) is not None
             and (not before or (r.get("started") or "") < (rec.get("started") or "~"))]
    return min(cands, key=hof_time) if cands else None


def references(rec: dict, runs: list[dict], spec: str = "auto",
               before: bool = True) -> list[tuple[dict, str]]:
    """[(reference run, why)] for spec auto|last|best|none|<id>."""
    if spec == "none":
        return []
    if spec in ("auto", "last", "best"):
        out = []
        if spec in ("auto", "last"):
            p = previous(rec, runs)
            if p:
                out.append((p, "previous run from the same start"))
        if spec in ("auto", "best"):
            b = best(rec, runs, before)
            if b and all(b["id"] != r["id"] for r, _ in out):
                out.append((b, "fastest in-game to the Hall of Fame"))
            elif b and out:
                out[0] = (out[0][0], out[0][1] + ", also the fastest to the Hall of Fame")
        return out
    ref = find(runs, spec)
    if ref is None:
        raise KeyError(f"no run {spec!r} in the history")
    return [(ref, "chosen")] if ref["id"] != rec["id"] else []


# -- comparison ---------------------------------------------------------------------

@dataclass
class Row:
    name: str
    game: int | None           # this run (absolute or elapsed, per the comparison mode)
    ref_game: int | None
    wall: float | None
    ref_wall: float | None

    @property
    def label(self) -> str:
        return label(self.name)

    @property
    def d_game(self) -> int | None:
        return None if self.game is None or self.ref_game is None else self.game - self.ref_game

    @property
    def d_wall(self) -> float | None:
        return None if self.wall is None or self.ref_wall is None else self.wall - self.ref_wall


@dataclass
class Comparison:
    run: dict
    ref: dict
    why: str
    mode: str                  # "absolute": game clock; "elapsed": game time since the run began
    rows: list[Row] = field(default_factory=list)
    same_start: bool = True

    @property
    def mode_text(self) -> str:
        if self.mode == "absolute":
            return "in-game time is the game clock at each milestone (both runs started a new game)"
        return "in-game time is the game time elapsed since each run began"

    @property
    def same_end(self) -> bool:
        """Both runs got to the same milestone, so their totals can be judged."""
        fa, fb = furthest(self.run), furthest(self.ref)
        return bool(fa and fb and fa["name"] == fb["name"])

    def totals(self) -> list[tuple[str, str, str, str, int]]:
        """(what, this run, reference, delta, sign): sign < 0 is better, 0 is
        no verdict (also when the runs ended at different milestones)."""
        a, b = self.run, self.ref
        judge = self.same_end
        out = []

        def verdict(d):
            return 0 if d is None or not judge else (d > 0) - (d < 0)

        def add_time(what, x, y):
            d = None if x is None or y is None else x - y
            out.append((what, hms(x), hms(y), signed(d), verdict(d)))

        def add_n(what, x, y):
            d = None if x is None or y is None else x - y
            out.append((what, "–" if x is None else str(x), "–" if y is None else str(y),
                        signed_n(d), verdict(d)))
        add_time("In-game (this run)", a.get("game_elapsed"), b.get("game_elapsed"))
        add_time("Wall", a.get("wall_seconds"), b.get("wall_seconds"))
        for k, what in (("whiteouts", "Whiteouts"), ("faints", "Faints"),
                        ("trainer_losses", "Trainer losses")):
            add_n(what, a["deaths"].get(k), b["deaths"].get(k))
        add_n("Battles", a.get("battles"), b.get("battles"))
        return out


def compare(run: dict, ref: dict, why: str = "", majors_only: bool = True) -> Comparison:
    mode = "absolute" if run.get("new_game") and ref.get("new_game") else "elapsed"
    a, b = done(run), done(ref)
    order = list(MAJOR) if majors_only else _union_order(run, ref)
    rows = [Row(n, game_at(run, a.get(n), mode), game_at(ref, b.get(n), mode),
                a[n]["wall"] if n in a else None, b[n]["wall"] if n in b else None)
            for n in order if n in a or n in b]
    return Comparison(run, ref, why, mode, rows, same_start=comparable(run, ref))


def _union_order(a: dict, b: dict) -> list[str]:
    out: list[str] = []
    for rec in (a, b):
        for m in rec["milestones"]:
            if m["name"] not in out:
                out.append(m["name"])
    return out


# -- markdown -----------------------------------------------------------------------

def _md_delta(d: float | None, fmt=signed) -> str:
    if d is None:
        return ""
    if d < 0:
        return f"**{fmt(d)}** faster"
    return f"{fmt(d)} slower" if d > 0 else fmt(d)


def render_md(rec: dict, comparisons: list[Comparison]) -> str:
    rid = rec["id"]
    g = rec.get("git") or {}
    commit = (g.get("commit") or "?") + (" (dirty)" if g.get("dirty") else "")
    d = rec["deaths"]
    fm = furthest(rec)
    L = [f"# Run {rid}: {rec['outcome']}", ""]
    if rec.get("imported_from"):
        L += [f"Imported from `{rec['imported_from']}` (in-game times to the minute; "
              "whiteouts, faints and battles are not in the log).", ""]
    L += ["| | |", "|---|---|",
          f"| Outcome | **{rec['outcome']}**"
          + (f" at `{rec['failed_at']}`" if rec.get("failed_at") else "") + " |",
          f"| Started | {rec.get('started') or '?'} |",
          f"| Ended | {rec.get('ended') or '?'} |",
          f"| Start point | {start_desc(rec)} |",
          f"| Furthest | {label(fm['name']) if fm else '(no milestone finished)'} |",
          f"| Backend | {rec.get('backend') or '?'} |",
          f"| Commit | `{commit}` |",
          f"| Wall time | {hms(rec.get('wall_seconds'))} |",
          f"| In-game time | {hms(rec.get('game_start'))} → {hms(rec.get('game_end'))}"
          f" (+{hms(rec.get('game_elapsed'))} this run) |",
          f"| Deaths | {_n(d.get('whiteouts'))} whiteouts · {_n(d.get('faints'))} faints · "
          f"{_n(d.get('trainer_losses'))} trainer losses |",
          f"| Battles · steps | {_n(rec.get('battles'))} · {_n(rec.get('steps'))} |",
          f"| Log | [{rec['log']}]({rec['log']}) |"]
    if rec.get("command"):
        L.insert(-1, f"| Command | `{rec['command']}` |")
    if rec.get("stop_after"):
        L.insert(-1, f"| Stop after | `{rec['stop_after']}` |")
    if rec.get("error"):
        L.insert(-1, f"| Error | `{rec['error']}` |")
    L.append("")
    majors = [m for m in done(rec).values() if m["major"]]
    L += ["## Major milestones", ""]
    if majors:
        L += ["| Milestone | In-game | In-game (this run) | Wall | Attempts |",
              "|---|---:|---:|---:|---:|"]
        L += [f"| {m['label']} | {hms(m.get('game'))} | {hms(m.get('game_elapsed'))} | "
              f"{hms(m['wall'])} | {m.get('attempts', '?')} |" for m in majors]
    else:
        L.append("None reached in this run.")
    L.append("")
    for c in comparisons:
        r = c.ref
        L += [f"## Compared with [{r['id']}]({r['id']}.md) ({c.why})", "",
              f"Reference: {r['outcome']}, from {start_desc(r)}, commit "
              f"`{(r.get('git') or {}).get('commit') or '?'}`, log [{r['log']}]({r['log']}). "
              f"Here {c.mode_text}; lower is better."]
        if not c.same_start:
            L.append("**The runs started from different points: the times are not directly "
                     "comparable.**")
        L.append("")
        if c.rows:
            L += ["| Milestone | In-game | Ref | Δ in-game | Wall | Ref | Δ wall |",
                  "|---|---:|---:|---|---:|---:|---|"]
            L += [f"| {x.label} | {hms(x.game)} | {hms(x.ref_game)} | {_md_delta(x.d_game)} | "
                  f"{hms(x.wall)} | {hms(x.ref_wall)} | {_md_delta(x.d_wall)} |" for x in c.rows]
            L.append("")
        rf = furthest(r)
        L += ["| Total | This run | Ref | Δ |", "|---|---:|---:|---|",
              f"| Furthest | {label(fm['name']) if fm else '–'} | "
              f"{label(rf['name']) if rf else '–'} | |"]
        if not c.same_end:
            L[-3:-3] = ["The runs ended at different milestones: compare the milestone rows, "
                        "not the totals.", ""]
        for what, x, y, dtext, sign in c.totals():
            L.append(f"| {what} | {x} | {y} | "
                     + (f"**{dtext}** better" if sign < 0 else f"{dtext} worse" if sign > 0
                        else dtext) + " |")
        L.append("")
    if not comparisons:
        L += ["No earlier run from the same start to compare with.", ""]
    L += ["## All milestones", "",
          "| # | Milestone | In-game | In-game (this run) | Wall | Attempts |",
          "|---:|---|---:|---:|---:|---:|"]
    for i, m in enumerate(rec["milestones"], 1):
        name = f"**{m['label']}**" if m["major"] else m["name"]
        L.append(f"| {i} | {name} | {hms(m.get('game'))} | {hms(m.get('game_elapsed'))} | "
                 f"{hms(m['wall'])} | {m.get('attempts', '?')} |")
    return "\n".join(L) + "\n"


def _n(x) -> str:
    return "?" if x is None else f"{x:,}"


# -- rich (terminal and TUI) --------------------------------------------------------

GOOD, BAD = "bold #5fd75f", "bold #ff5f5f"


def delta_text(d, fmt=signed):
    from rich.text import Text
    if d is None:
        return Text("")
    return Text(fmt(d), style=GOOD if d < 0 else BAD if d > 0 else "dim")


def rich_summary(rec: dict, comparisons: list[Comparison], path: str | None = None,
                 majors_only: bool = True):
    """Header lines + the milestone table (with deltas vs the first reference)."""
    from rich.console import Group
    from rich.table import Table
    from rich.text import Text
    c = comparisons[0] if comparisons else None
    head = Text()
    style = {"finished": "bold #87ff5f", "failed": "bold #ff5f5f", "crashed": "bold #ff5f5f",
             "stopped": "bold #ff8700"}.get(rec["outcome"], "bold")
    head.append(f"Run {rec['id']} ", style="bold")
    head.append(rec["outcome"] or "?", style=style)
    if rec.get("failed_at"):
        head.append(f" at {rec['failed_at']}", style=style)
    head.append(f"  from {start_desc(rec)}", style="dim")
    g = rec.get("git") or {}
    head.append(f"  {g.get('commit') or '?'}{'+dirty' if g.get('dirty') else ''}\n", style="dim")
    d = rec["deaths"]
    head.append("In-game ", style="bold")
    head.append(f"{hms(rec.get('game_end'))} (+{hms(rec.get('game_elapsed'))})", style="#5fd7ff")
    head.append("   Wall ", style="bold")
    head.append(hms(rec.get("wall_seconds")), style="#5fd7ff")
    head.append("   Deaths ", style="bold")
    head.append(f"{_n(d.get('whiteouts'))} whiteouts · {_n(d.get('faints'))} faints · "
                f"{_n(d.get('trainer_losses'))} trainer losses",
                style="#ff5f5f" if d.get("whiteouts") or d.get("trainer_losses") else "#5fd75f")
    if c:
        head.append("\nvs ", style="dim")
        head.append(c.ref["id"], style="bold")
        head.append(f" ({c.why}; {c.mode} in-game time)", style="dim")
        tot = {w: (dtext, sign) for w, _, _, dtext, sign in c.totals()}
        for w, short in (("In-game (this run)", "in-game"), ("Wall", "wall"),
                         ("Whiteouts", "whiteouts")):
            dtext, sign = tot[w]
            if dtext:
                head.append(f"  {short} ", style="dim")
                head.append(dtext, style=GOOD if sign < 0 else BAD if sign > 0 else "dim")
    t = Table(box=None, padding=(0, 1), pad_edge=False, header_style="bold dim")
    t.add_column("Milestone", no_wrap=True)
    t.add_column("In-game", justify="right")
    if c:
        t.add_column("Δ", justify="right")
    t.add_column("Wall", justify="right")
    if c:
        t.add_column("Δ wall", justify="right")
    t.add_column("Tries", justify="right")
    mine = done(rec)
    mode = c.mode if c else ("absolute" if rec.get("new_game") else "elapsed")
    if c:
        rows = c.rows if majors_only else compare(rec, c.ref, majors_only=False).rows
        names = [x.name for x in rows]
    else:
        rows = []
        names = [m["name"] for m in rec["milestones"] if m["name"] in MAJOR or not majors_only]
        names = list(dict.fromkeys(names))
    by_name = {x.name: x for x in rows}
    for n in names:
        m, x = mine.get(n), by_name.get(n)
        style = "bold" if n in MAJOR else ""
        cells = [Text(label(n) if n in MAJOR else n, style=style if m else "dim"),
                 Text(hms(game_at(rec, m, mode)), style="#5fd7ff" if m else "dim")]
        if c:
            cells.append(delta_text(x.d_game if x else None))
        cells.append(Text(hms(m["wall"]) if m else "–", style="" if m else "dim"))
        if c:
            cells.append(delta_text(x.d_wall if x else None))
        tries = m.get("attempts") if m else None
        cells.append(Text("" if tries is None else str(tries),
                          style="#ff8700" if tries and tries > 1 else "dim"))
        t.add_row(*cells)
    parts = [head, t] if names else [head, Text("(no major milestone reached)", style="dim")]
    if path:
        parts.append(Text(f"Report: {path}", style="italic"))
    return Group(*parts)


def list_table(runs: list[dict]):
    from rich.table import Table
    from rich.text import Text
    t = Table(header_style="bold", box=None, padding=(0, 1),
              caption="in-game: game clock for new games, +elapsed otherwise; "
                      "deaths: whiteouts/faints/trainer losses", caption_justify="left")
    for col, j in (("Run", "left"), ("Outcome", "left"), ("From", "left"),
                   ("Furthest", "left"), ("In-game", "right"), ("Wall", "right"),
                   ("Deaths", "right"), ("Commit", "left")):
        t.add_column(col, justify=j, no_wrap=col in ("Run", "In-game", "Wall", "Commit"))
    for r in runs:
        fm = furthest(r)
        mode = "absolute" if r.get("new_game") else "elapsed"
        game = game_at(r, fm, mode)
        d = r["deaths"]
        ostyle = {"finished": "#87ff5f", "failed": "#ff5f5f", "crashed": "#ff5f5f",
                  "stopped": "#ff8700"}.get(r["outcome"], "")
        g = r.get("git") or {}
        t.add_row(r["id"], Text(r["outcome"] or "?", style=ostyle), r.get("start_point") or "?",
                  Text(label(fm["name"]) if fm else "–",
                       style="bold #ffd700" if reached_hof(r) else ""),
                  Text(("" if mode == "absolute" or game is None else "+") + hms(game),
                       style="#5fd7ff"),
                  hms(r.get("wall_seconds")),
                  "/".join(_n(d.get(k)) for k in ("whiteouts", "faints", "trainer_losses")),
                  Text((g.get("commit") or "?") + ("+" if g.get("dirty") else ""), style="dim"))
    return t


# -- writing ------------------------------------------------------------------------

def write(rec: dict, history: Path, comparisons: list[Comparison]) -> Path:
    history = Path(history)
    history.mkdir(parents=True, exist_ok=True)
    rec["compared_with"] = [c.ref["id"] for c in comparisons]
    (history / f"{rec['id']}.json").write_text(json.dumps(rec, indent=1) + "\n")
    md = history / f"{rec['id']}.md"
    md.write_text(render_md(rec, comparisons))
    return md


def unique_id(history: Path, base: str) -> str:
    rid, n = base, 2
    while (Path(history) / f"{rid}.json").exists() or (Path(history) / f"{rid}.log").exists():
        rid, n = f"{base}-{n}", n + 1
    return rid


@dataclass
class Report:
    """What the UI shows when a run ends (built once, read-only afterwards)."""
    record: dict
    comparisons: list[Comparison]
    path: str                  # the .md, relative to the project root when possible

    def renderable(self):
        return rich_summary(self.record, self.comparisons, self.path)


class RunRecorder:
    """Collects one run's facts and writes its report. Worker thread only.

    Creating it starts copying the log to runs/history/<id>.log; finish()
    writes the record and report, whatever happened to the run.
    """

    def __init__(self, history: Path, *, backend: str, start_point: str, new_game: bool,
                 stop_after: str | None = None, compare: str = "auto",
                 command: str | None = None, root: Path | None = None):
        self.history = Path(history)
        self.history.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.compare = compare
        self.t0 = time.time()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.t0))
        tag = "new" if new_game else re.sub(r"[^A-Za-z0-9_]+", "_", start_point)[:24]
        self.id = unique_id(self.history, f"{stamp}-{tag}")
        self.handler = logging.FileHandler(self.history / f"{self.id}.log")
        self.handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        logging.getLogger().addHandler(self.handler)
        self.rec = new_record(self.id, started=_iso(self.t0), backend=backend,
                              start_point=start_point, new_game=new_game,
                              stop_after=stop_after, command=command,
                              git=git_info(root) if root else {"commit": None, "dirty": None},
                              game_start=0 if new_game else None)
        g = self.rec["git"]
        log.info("RUN %s from %s on %s (commit %s%s)", self.id, start_point, backend,
                 g["commit"] or "?", ", dirty" if g["dirty"] else "")

    def begin(self, agent) -> None:
        """Note the game clock once the game is loaded."""
        if not self.rec["new_game"]:
            try:
                self.rec["game_start"] = agent.game.play_seconds()
            except Exception:
                pass

    def finish(self, outcome: str, agent=None, runner=None, error: str | None = None) -> Report:
        """Write <id>.json/.md with what is known; never raises for missing game state."""
        rec, t1 = self.rec, time.time()
        rec.update(outcome=outcome, ended=_iso(t1), wall_seconds=round(t1 - self.t0, 1),
                   error=error)
        if runner is not None:
            rec["milestones"] = [dict(m) for m in getattr(runner, "records", [])]
            rec["first_milestone"] = getattr(runner, "first", None)
            active = getattr(runner, "active", None)
            if outcome != "finished" and active is not None:
                rec["failed_at"] = active.name
        if agent is not None:
            try:
                b = getattr(agent, "battle", None)
                rec["deaths"] = {k: getattr(b, k, None) for k in
                                 ("whiteouts", "faints", "trainer_losses")}
                stats = dict(agent.ctl.stats)
                rec["battles"], rec["steps"] = stats.get("battles"), stats.get("steps")
            except Exception:
                pass
            try:
                rec["game_end"] = agent.game.play_seconds()
            except Exception:
                pass
        finalize(rec)
        comparisons: list[Comparison] = []
        try:
            runs = [r for r in load_runs(self.history) if r["id"] != rec["id"]]
            comparisons = [compare(rec, r, why) for r, why in references(rec, runs, self.compare)]
        except Exception as exc:
            log.warning("REPORT no comparison: %s", exc)
        md = write(rec, self.history, comparisons)
        shown = str(md)
        if self.root:
            try:
                shown = str(md.relative_to(self.root))
            except ValueError:
                pass
        log.info("REPORT %s", shown)
        self.close()
        return Report(rec, comparisons, shown)

    def close(self) -> None:
        logging.getLogger().removeHandler(self.handler)
        self.handler.close()


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t))


# -- importing old logs -------------------------------------------------------------

RE_LINE = re.compile(r"^(\d\d):(\d\d):(\d\d) (.*)$")
RE_START = re.compile(r"^=== MILESTONE (\S+) ===.*?time=(\d+):(\d\d)")
RE_DONE = re.compile(r"^--- done (\S+) \((\d+)s elapsed, game (\d+):(\d\d)\)")
RE_FAIL = re.compile(r"^milestone (\S+) (?:stuck|not done after|crashed)")
RE_RUN = re.compile(r"^RUN \S+ from (.+) on (\S+) \(commit")
RE_END = re.compile(r"^finished=(True|False) in (\d+)s wall, game time (\d+):(\d\d)")


def split_log(text: str) -> list[list[str]]:
    """A log (maybe many appended runs) -> one list of lines per run.

    A run ends at its "finished=" line; a new "RUN <id>" line or a first
    milestone after an ended segment starts the next.
    """
    segs: list[list[str]] = []
    cur: list[str] = []
    for line in text.splitlines():
        m = RE_LINE.match(line)
        msg = m.group(4) if m else ""
        if msg.startswith("RUN ") and cur:
            segs.append(cur)
            cur = []
        cur.append(line)
        if RE_END.match(msg):
            segs.append(cur)
            cur = []
    segs.append(cur)
    return [s for s in segs if any("=== MILESTONE" in x for x in s)]


def parse_log(lines: list[str], run_id: str, date: str, source: str = "") -> dict:
    """Build a record from one run's log lines (see split_log)."""
    rec = new_record(run_id, backend="?", imported_from=source,
                     deaths={"whiteouts": None, "faints": None, "trainer_losses": 0})
    tries: dict[str, int] = {}
    t_first = t_last = None
    active = None
    day = 0
    prev_secs = None
    last_game = None
    for line in lines:
        m = RE_LINE.match(line)
        if not m:
            continue
        secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        if prev_secs is not None and secs < prev_secs - 3600:
            day += 1                                   # past midnight
        prev_secs = secs
        secs += day * 86400
        t_first = secs if t_first is None else t_first
        t_last = secs
        msg = m.group(4)
        if s := RE_START.match(msg):
            active = s.group(1)
            last_game = int(s.group(2)) * 3600 + int(s.group(3)) * 60
            if rec["first_milestone"] is None:
                rec["first_milestone"] = active
                rec["game_start"] = int(s.group(2)) * 3600 + int(s.group(3)) * 60
            tries.setdefault(active, 1)                # a retry logs a failure first
        elif d := RE_DONE.match(msg):
            rec["milestones"].append({"name": d.group(1), "wall": float(d.group(2)),
                                      "game": int(d.group(3)) * 3600 + int(d.group(4)) * 60,
                                      "attempts": tries.pop(d.group(1), 1)})
            active, last_game = None, rec["milestones"][-1]["game"]
        elif f := RE_FAIL.match(msg):
            tries[f.group(1)] = tries.get(f.group(1), 1) + 1
        elif msg.startswith("BATTLE lost to a trainer"):
            rec["deaths"]["trainer_losses"] += 1
        elif msg.startswith("ROUTE failed at "):
            rec["failed_at"] = msg.split()[-1]
        elif msg.startswith("ROUTE complete") and active and active not in done(rec):
            # The route ended on the champion's credits: done, with no "done" line.
            rec["milestones"].append({"name": active, "wall": float(secs - t_first),
                                      "game": None, "attempts": max(1, tries.pop(active, 1) - 1)})
            active = None
        elif msg.startswith("STOPPED by the user"):
            rec["outcome"] = "stopped"
        elif msg.startswith("run crashed") or msg.startswith("mGBA:"):
            rec["outcome"] = "crashed"
        elif e := RE_END.match(msg):
            rec["wall_seconds"] = float(e.group(2))
            rec["game_end"] = int(e.group(3)) * 3600 + int(e.group(4)) * 60
            if rec["outcome"] is None:
                rec["outcome"] = "finished" if e.group(1) == "True" else "failed"
        elif r := RE_RUN.match(msg):
            rec["start_point"], rec["backend"] = r.group(1), r.group(2)
    for m in rec["milestones"]:
        if m["game"] is None:
            m["game"] = rec["game_end"]
    if rec["outcome"] is None:                         # no "finished=" line
        rec["outcome"], rec["game_end"] = "unknown", last_game
    if rec["outcome"] != "finished" and not rec["failed_at"] and active:
        rec["failed_at"] = active
    if rec["wall_seconds"] is None and t_first is not None:
        rec["wall_seconds"] = float(t_last - t_first)
    rec["new_game"] = rec["game_start"] == 0 and rec["first_milestone"] == "leave_truck"
    rec["start_point"] = rec["start_point"] or (NEW_GAME if rec["new_game"] else
                                                f"at {rec['first_milestone']}")
    rec["started"] = f"{date}T{lines[0][:8]}" if RE_LINE.match(lines[0]) else date
    last = next((x for x in reversed(lines) if RE_LINE.match(x)), None)
    rec["ended"] = f"{date}T{last[:8]}" if last else None     # the date may be a day off
    return finalize(rec)


def import_log(path: Path, history: Path) -> list[dict]:
    """Records (written to history, with a copy of the log) for each run in a log file."""
    path = Path(path)
    date = time.strftime("%Y-%m-%d", time.localtime(path.stat().st_mtime))
    segs = split_log(path.read_text(errors="replace"))
    out = []
    for i, seg in enumerate(segs, 1):
        stamp = date.replace("-", "") + "-" + (seg[0][:8].replace(":", "")
                                              if RE_LINE.match(seg[0]) else "000000")
        tag = re.sub(r"[^A-Za-z0-9_]+", "_", path.stem)[:24] + (f"_{i}" if len(segs) > 1 else "")
        rid = unique_id(history, f"{stamp}-{tag}")
        rec = parse_log(seg, rid, date, str(path))
        Path(history).mkdir(parents=True, exist_ok=True)
        (Path(history) / rec["log"]).write_text("\n".join(seg) + "\n")
        runs = [r for r in load_runs(history) if r["id"] != rid]
        write(rec, history, [compare(rec, r, why) for r, why in references(rec, runs)])
        out.append(rec)
    return out
