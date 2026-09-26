"""Run reports: records from a (fake) run, comparisons, markdown, log import.

No ROM needed. Run:  .venv/bin/python tests/test_runstats.py
"""
from __future__ import annotations

import json
import logging
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pokeauto import runstats as rs  # noqa: E402

log = logging.getLogger("pokeauto")


# -- fakes -------------------------------------------------------------------------------

class FakeGame:
    def __init__(self):
        self.clock = 0

    def play_seconds(self):
        return self.clock

    def play_time(self):
        return f"{self.clock // 3600}:{self.clock // 60 % 60:02d}"


@dataclass
class FakeBattle:
    whiteouts: int = 0
    faints: int = 0
    trainer_losses: int = 0


class FakeAgent:
    def __init__(self):
        self.game = FakeGame()
        self.battle = FakeBattle()

        class ctl:
            stats = {"battles": 10, "steps": 500}

            class planner:
                learned_blocks: set = set()
        self.ctl = ctl

    def status_line(self):
        return f"time={self.game.play_time()}"

    def before_milestone(self, m):
        pass

    def pump(self):
        pass

    def recover(self, m, exc):
        pass


@dataclass
class FakeRunner:
    records: list = field(default_factory=list)
    first: str | None = None
    active: object = None


@dataclass
class M:
    name: str


def fake_run(history: Path, times: list[tuple[str, int, float, int]], outcome="finished",
             new_game=True, whiteouts=0) -> rs.Report:
    """times: (milestone, game seconds, wall seconds, attempts)."""
    rec = rs.RunRecorder(history, backend="headless", start_point=rs.NEW_GAME if new_game else "x",
                         new_game=new_game, stop_after=None)
    agent = FakeAgent()
    rec.begin(agent)
    log.info("=== MILESTONE %s ===", times[0][0])
    runner = FakeRunner(records=[{"name": n, "game": g, "wall": w, "attempts": a}
                                 for n, g, w, a in times], first=times[0][0], active=M("x"))
    agent.game.clock = times[-1][1] + 5
    agent.battle.whiteouts = whiteouts
    rec.rec["started"] = f"2026-01-01T00:00:{len(list(history.glob('*.json'))):02d}"
    return rec.finish(outcome, agent, runner)


# -- tests -------------------------------------------------------------------------------

def test_record_and_comparison():
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    with tempfile.TemporaryDirectory() as tmp:
        h = Path(tmp)
        a = fake_run(h, [("leave_truck", 10, 1.0, 1), ("badge_stone", 2000, 50.0, 1),
                         ("badge_knuckle", 4000, 100.0, 2)], outcome="failed", whiteouts=2)
        ra = a.record
        assert (h / f"{ra['id']}.json").exists() and (h / f"{ra['id']}.md").exists()
        text = (h / ra["log"]).read_text()
        assert "RUN " in text and "=== MILESTONE leave_truck" in text and "REPORT" in text
        assert ra["outcome"] == "failed" and ra["failed_at"] == "x" and ra["game_start"] == 0
        assert ra["deaths"]["whiteouts"] == 2 and ra["battles"] == 10 and ra["game_end"] == 4005
        assert [m["major"] for m in ra["milestones"]] == [False, True, True]
        assert ra["milestones"][1]["label"] == "Stone Badge (Roxanne)"
        assert a.comparisons == []

        b = fake_run(h, [("leave_truck", 12, 1.0, 1), ("badge_stone", 1900, 45.0, 1),
                         ("badge_knuckle", 4100, 90.0, 1), ("badge_dynamo", 6000, 150.0, 1)])
        rb = b.record
        assert rb["compared_with"] == [ra["id"]]
        c = b.comparisons[0]
        assert c.mode == "absolute" and c.why.startswith("previous")
        rows = {x.name: x for x in c.rows}
        assert set(rows) == {"badge_stone", "badge_knuckle", "badge_dynamo"}
        assert rows["badge_stone"].d_game == -100 and rows["badge_knuckle"].d_game == 100
        assert rows["badge_dynamo"].d_game is None and rows["badge_stone"].d_wall == -5.0
        tot = {w: (d, s) for w, _, _, d, s in c.totals()}
        assert tot["Whiteouts"] == ("-2", 0) and not c.same_end      # ended elsewhere: no verdict
        worse = dict(rb, id="z", deaths={"whiteouts": 5, "faints": 0, "trainer_losses": 0})
        tot = {w: (d, s) for w, _, _, d, s in rs.compare(rb, worse).totals()}
        assert tot["Whiteouts"] == ("-5", -1) and tot["Wall"][1] == 0

        md = (h / f"{rb['id']}.md").read_text()
        assert f"[{ra['id']}]({ra['id']}.md)" in md and f"({rb['log']})" in md
        assert "**-0:01:40** faster" in md and "+0:01:40 slower" in md
        assert "Dynamo Badge (Wattson)" in md and "game clock" in md

        # A third run that reaches the Hall of Fame; then the fourth also compares with it.
        fake_run(h, [("leave_truck", 9, 1.0, 1), ("champion", 80000, 2000.0, 1)])
        d = fake_run(h, [("leave_truck", 9, 1.0, 1), ("badge_stone", 1800, 40.0, 1)])
        whys = [c.why for c in d.comparisons]
        assert len(whys) == 1 and "fastest" in whys[0]       # previous == best: listed once
        runs = rs.load_runs(h)
        assert [r["id"] for r, _ in rs.references(d.record, runs, "best")] == [runs[2]["id"]]
        assert rs.references(d.record, runs, "none") == []
        assert rs.references(d.record, runs, ra["id"])[0][0]["id"] == ra["id"]

        # Elapsed mode when the runs did not start a new game; not comparable with new games.
        e = fake_run(h, [("catch_marill", 11000, 3.0, 1), ("badge_balance", 13000, 60.0, 2)],
                     new_game=False)
        assert e.comparisons == []
        e.record["game_start"] = 10000
        rs.finalize(e.record)
        f = dict(e.record, id="other", game_start=10500)
        f["milestones"] = [dict(m) for m in e.record["milestones"]]
        rs.finalize(f)
        c = rs.compare(e.record, f)
        assert c.mode == "elapsed" and c.rows[0].d_game == 500
        assert "elapsed" in rs.render_md(e.record, [c])
        from rich.console import Console
        con = Console(width=120, record=True)
        con.print(b.renderable())
        con.print(rs.list_table(runs))
        out = con.export_text()
        assert "Knuckle Badge (Brawly)" in out and "-0:01:40" in out and rb["id"] in out
        print("ok   records, comparisons and reports:", [r["id"] for r in runs])


def test_record_without_game():
    """A run that crashed before the game was built still gets a record."""
    with tempfile.TemporaryDirectory() as tmp:
        rec = rs.RunRecorder(Path(tmp), backend="mgba", start_point="mGBA save", new_game=False)
        r = rec.finish("crashed", None, None, "mGBA: no connection").record
        saved = json.loads((Path(tmp) / f"{r['id']}.json").read_text())
        assert saved["outcome"] == "crashed" and saved["milestones"] == []
        assert saved["error"] == "mGBA: no connection" and saved["wall_seconds"] is not None
        assert "no connection" in (Path(tmp) / f"{r['id']}.md").read_text()


def test_route_runner_records():
    """RouteRunner keeps game time and attempts per milestone, and records the
    champion even though the credits end the route before its "done"."""
    from pokeauto.controller import Stuck
    from pokeauto.route import Milestone, RouteRunner
    agent = FakeAgent()
    state = {"a": False, "champ": False, "tries": 0}

    def do_a(ag):
        state["tries"] += 1
        ag.game.clock += 60
        if state["tries"] == 2:
            state["a"] = True

    def do_champ(ag):
        ag.game.clock += 600
        state["champ"] = True
        raise Stuck("credits rolled")
    route = [Milestone("a", lambda ag: state["a"], [do_a], heal_first=False),
             Milestone("champion", lambda ag: state["champ"], [do_champ], heal_first=False)]
    runner = RouteRunner(agent, route)
    assert runner.run()
    assert [(r["name"], r["game"], r["attempts"]) for r in runner.records] == \
        [("a", 120, 2), ("champion", 720, 1)]
    assert [n for n, _ in runner.history] == ["a", "champion"] and runner.first == "a"


LOG = """\
10:00:00 === MILESTONE badge_heat === MAP_X (1, 2) badges=3 money=1 time=3:00 party: A
10:00:05 milestone badge_heat stuck (attempt 1): no route
10:00:06 === MILESTONE badge_heat === MAP_X (1, 2) badges=3 money=1 time=3:01 party: A
10:00:30 BATTLE lost to a trainer (1 so far)
10:01:00 --- done badge_heat (60s elapsed, game 3:30)
10:01:00 === MILESTONE champion === MAP_Y (1, 2) badges=8 money=1 time=3:30 party: A
10:02:00 milestone champion stuck (attempt 1): no route to MAP_EVER_GRANDE_CITY_CHAMPIONS_ROOM
10:02:00 ROUTE champion now depends on None again; going back
10:02:00 ROUTE complete
10:02:00 finished=True in 120s wall, game time 3:45
10:05:00 RUN 20260101-100500-new from new game on headless (commit abc, dirty)
10:05:00 === MILESTONE leave_truck === MAP_PETALBURG_CITY (0, 0) badges=0 money=0 time=0:00 party:
10:05:01 --- done leave_truck (1s elapsed, game 0:00)
10:05:02 === MILESTONE set_clock === MAP_X (0, 0) badges=0 money=0 time=0:00 party:
"""


def test_import_log():
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "old.txt"
        f.write_text(LOG)
        h = Path(tmp) / "history"
        recs = rs.import_log(f, h)
        assert len(recs) == 2
        a, b = recs
        ms = {m["name"]: m for m in a["milestones"]}
        assert ms["badge_heat"]["attempts"] == 2 and ms["badge_heat"]["game"] == 3 * 3600 + 1800
        assert ms["champion"]["game"] == 3 * 3600 + 45 * 60 and ms["champion"]["attempts"] == 1
        assert a["outcome"] == "finished" and a["wall_seconds"] == 120
        assert a["deaths"]["trainer_losses"] == 1 and a["game_start"] == 3 * 3600
        assert a["milestones"][1]["game_elapsed"] == 45 * 60 and not a["new_game"]
        assert b["new_game"] and b["start_point"] == "new game" and b["backend"] == "headless"
        assert b["outcome"] == "unknown" and b["failed_at"] == "set_clock"
        assert (h / a["log"]).read_text().startswith("10:00:00 === MILESTONE badge_heat")
        assert "Imported from" in (h / f"{a['id']}.md").read_text()
    full = ROOT / "runs" / "validation" / "full_new_game.txt"
    if full.exists():
        with tempfile.TemporaryDirectory() as tmp:
            (r,) = rs.import_log(full, Path(tmp))
            ms = rs.done(r)
            assert r["new_game"] and rs.reached_hof(r) and r["outcome"] == "finished"
            assert all(n in ms for n in rs.MAJOR)
            times = [ms[n]["game"] for n in rs.MAJOR]
            assert times == sorted(times) and ms["champion"]["game"] == 23 * 3600 + 17 * 60
            print("ok   imported full_new_game.txt: Hall of Fame at", rs.hms(ms["champion"]["game"]))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
