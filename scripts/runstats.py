#!/usr/bin/env python3
"""List, show and compare run reports (runs/history/, written by scripts/play.py).

    python scripts/runstats.py                      # recent runs
    python scripts/runstats.py 20260926-133012      # one run (id, unique prefix, or last)
    python scripts/runstats.py last --vs best       # compare with another run (id|best|last)
    python scripts/runstats.py last --all           # every milestone, not just badges/E4
    python scripts/runstats.py --import runs/validation/full_new_game.txt

In-game time is the metric (lower is better). See docs/CLI.md, "Run reports".
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402
from rich.text import Text  # noqa: E402

from pokeauto import runstats as rs  # noqa: E402


def comparison_table(c: rs.Comparison, majors_only: bool) -> Table:
    if not majors_only:
        c = rs.compare(c.run, c.ref, c.why, majors_only=False)
    t = Table(title=f"{c.run['id']}  vs  {c.ref['id']} ({c.why})",
              caption=c.mode_text + "; lower is better"
              + ("" if c.same_end else ".\nThe runs ended at different milestones: "
                 "the totals are not judged"), header_style="bold", box=None,
              padding=(0, 1))
    for col in ("Milestone", "In-game", "Ref", "Δ in-game", "Wall", "Ref", "Δ wall"):
        t.add_column(col, justify="left" if col == "Milestone" else "right", no_wrap=True)
    for x in c.rows:
        t.add_row(Text(x.label, style="bold" if x.name in rs.MAJOR else ""),
                  Text(rs.hms(x.game), style="#5fd7ff"), rs.hms(x.ref_game),
                  rs.delta_text(x.d_game), rs.hms(x.wall), rs.hms(x.ref_wall),
                  rs.delta_text(x.d_wall))
    t.add_section()
    fa, fb = rs.furthest(c.run), rs.furthest(c.ref)
    t.add_row("Furthest", rs.label(fa["name"]) if fa else "–", rs.label(fb["name"]) if fb else "–")
    for what, x, y, dtext, sign in c.totals():
        t.add_row(what, x, y, Text(dtext, style=rs.GOOD if sign < 0 else rs.BAD if sign > 0 else "dim"))
    return t


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("run", nargs="?", help="run id (or unique prefix, or 'last') to show")
    p.add_argument("--vs", help="compare with this run: an id, best or last (default: the "
                                "previous run from the same start and the fastest one)")
    p.add_argument("--all", action="store_true", help="every milestone, not just badges and the E4")
    p.add_argument("-n", type=int, default=20, help="runs to list (default 20)")
    p.add_argument("--import", dest="imports", nargs="+", metavar="LOG",
                   help="build records from old logs (runs/validation/*.txt, runs/play.log)")
    p.add_argument("--dir", default=str(ROOT / "runs" / "history"), help="history directory")
    args = p.parse_args()
    con = Console()
    history = Path(args.dir)

    if args.imports:
        for f in args.imports:
            recs = rs.import_log(Path(f), history)
            if not recs:
                con.print(f"[yellow]{f}: no run found (no '=== MILESTONE' lines)[/]")
            for r in recs:
                fm = rs.furthest(r)
                con.print(f"imported [bold]{r['id']}[/] from {f}: {r['outcome']}, "
                          f"{len(r['milestones'])} milestones, furthest "
                          f"{rs.label(fm['name']) if fm else '–'}, game {rs.hms(r['game_end'])}")
        return 0

    runs = rs.load_runs(history)
    if not runs:
        con.print(f"no runs in {history} yet: run scripts/play.py, or --import an old log")
        return 1
    if not args.run:
        con.print(rs.list_table(runs[-args.n:]))
        return 0
    try:
        rec = rs.find(runs, args.run)
        if rec is None:
            con.print(f"[red]no run {args.run!r} in {history}[/]")
            return 1
        refs = rs.references(rec, runs, args.vs or "auto", before=args.vs is None)
    except KeyError as exc:
        con.print(f"[red]{exc.args[0]}[/]")
        return 1
    comps = [rs.compare(rec, r, why) for r, why in refs]
    md = os.path.relpath(history / f"{rec['id']}.md")
    con.print(rs.rich_summary(rec, [], md, majors_only=not args.all))
    for c in comps:
        con.print()
        con.print(comparison_table(c, majors_only=not args.all))
    if not comps:
        con.print(Text("\nnothing to compare with" + (f" ({args.vs})" if args.vs else
                       ": no earlier run from the same start"), style="dim"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
