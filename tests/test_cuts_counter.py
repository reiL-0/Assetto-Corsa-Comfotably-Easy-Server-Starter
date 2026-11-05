"""The cut counter of the hub ("CUTS 2/5"): the rule of the league event running on the server, the cuts of each car in a race (the acServer's lap count and the driver's own game, the larger one is shown) and the
state the hub reads (race-state `cuts` / `cuts_rule`)."""
import time

from conftest import ADMIN
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import league
from app.db import engine
from app import racecontrol as adapter, supervisor
from app.live import racecontrol as live_rc
from app.main import app
from app.models import Championship, Event, Schedule
from app.rulebook import CutRule, Detectors, Rulebook
from test_racecontrol import live, V  # noqa: F401  (the `live` fixture)

api = TestClient(app, headers=ADMIN)
RULE = {"cuts_per_step": 1, "step_seconds": 5, "max_seconds": 15}   # (the rule of the older `auto_rules` field: no free cuts)


def _rule_of(monkeypatch, rule):
    """The counter reads the rule through two names (the state and the route of the adapter): both see this one."""
    monkeypatch.setattr(live_rc, "cut_rule_peek", lambda server_id: rule)    # (what the snapshot shows)
    monkeypatch.setattr(live_rc, "cut_rule_of", lambda server_id: rule)
    monkeypatch.setattr(adapter, "cut_rule_of", lambda server_id: rule)


def _league_event(sid, rules):
    lid = api.post(f"{V}/championships", json={"name": f"L{time.time_ns()}", **({"auto_rules": rules} if rules else {})}).json()["id"]
    with Session(engine) as s:
        ev = Event(title="R", data={"name": "r", "track": "spa", "cars": ["a"]}, league_id=lid)
        s.add(ev)
        s.commit()
        s.add(Schedule(event_id=ev.id, server_id=sid, start_at=time.time() - 60, state="running"))
        s.commit()


def test_the_rule_is_the_one_of_the_league_event_running_on_the_server():
    sid = api.post(f"{V}/servers", json={"name": "cuts-rule"}).json()["id"]
    league._CUT_RULES.clear()
    assert league.cut_rule(sid) is None, "no event running"
    _league_event(sid, RULE)
    league._CUT_RULES.clear()
    assert league.cut_rule(sid) == {"max": 0, "per": 1, "step": 5, "cap": 10}, "the older field has no free cuts"
    with Session(engine) as s:   # a rulebook's own rule: 5 cuts are free
        for c in s.exec(__import__("sqlmodel").select(Championship)).all():
            if c.auto_rules == RULE:
                c.rulebook = Rulebook(detectors=Detectors(cuts=CutRule(tolerance=5, cuts_per_step=1, step_seconds=5, max_seconds=15))).model_dump()
                s.add(c)
        s.commit()
    league._CUT_RULES.clear()
    assert league.cut_rule(sid) == {"max": 5, "per": 1, "step": 5, "cap": 10}
    sid2 = api.post(f"{V}/servers", json={"name": "cuts-no-rule"}).json()["id"]
    _league_event(sid2, None)
    league._CUT_RULES.clear()
    assert league.cut_rule(sid2) is None, "a league without a cut rule: no counter"
    league._CUT_RULES[sid] = (time.time(), {"max": 9, "per": 2, "step": 3, "cap": 9})
    assert league.cut_rule(sid) == {"max": 9, "per": 2, "step": 3, "cap": 9}, "read at most every CUT_RULE_TTL s"
    league._CUT_RULES[sid] = (time.time() - league.CUT_RULE_TTL - 1, {"max": 9, "per": 2, "step": 3, "cap": 9})
    assert league.cut_rule(sid) == {"max": 5, "per": 1, "step": 5, "cap": 10}, "and read again after it"


def test_the_counter_counts_a_race_only_shows_the_larger_count_and_starts_again_with_each_session(live, monkeypatch):
    sid, client, _ = live
    rule = {"max": 5, "per": 1, "step": 5, "cap": 15}
    _rule_of(monkeypatch, rule)
    client.session = {"session_index": 1, "session_type": 2}   # qualifying
    live_rc.on_cuts(sid, client.session, 3, 2, False)
    assert api.get(f"{V}/servers/{sid}/race-state").json()["cuts_rule"] is None, "not a race: nothing counted, nothing shown"
    client.session = {"session_index": 2, "session_type": 3}   # the race
    assert api.get(f"{V}/servers/{sid}/race-state").json()["cuts_rule"] == rule, "the rule is there before the first cut: CUTS 0/5"
    assert api.get(f"{V}/servers/{sid}/race-state").json()["cuts"] == {}
    live_rc.on_cuts(sid, client.session, 3, 1, True)      # the driver's own game: an excursion, live
    assert api.get(f"{V}/servers/{sid}/race-state").json()["cuts"] == {"3": 1}
    live_rc.on_cuts(sid, client.session, 3, 2, False)     # the acServer closes the lap with 2 cuts: the larger count wins, nothing is added up twice
    assert api.get(f"{V}/servers/{sid}/race-state").json()["cuts"] == {"3": 2}
    live_rc.on_cuts(sid, client.session, 3, 1, True)
    live_rc.on_cuts(sid, client.session, 3, 1, True)
    assert api.get(f"{V}/servers/{sid}/race-state").json()["cuts"] == {"3": 3}, "the game reports faster than the lap ends"
    live_rc.on_cuts(sid, client.session, 3, 0, False)
    live_rc.on_cuts(sid, client.session, 4, 1, False)
    assert api.get(f"{V}/servers/{sid}/race-state").json()["cuts"] == {"3": 3, "4": 1}
    client.session = {"session_index": 3, "session_type": 3}   # the next session: the counts start again
    assert api.get(f"{V}/servers/{sid}/race-state").json()["cuts"] == {}
    _rule_of(monkeypatch, None)
    live_rc.on_cuts(sid, client.session, 3, 1, True)
    assert api.get(f"{V}/servers/{sid}/race-state").json()["cuts_rule"] is None and api.get(f"{V}/servers/{sid}/race-state").json()["cuts"] == {}, "no rule: no counter"


def test_the_lap_completed_event_and_the_drivers_report_feed_the_counter(live, monkeypatch):
    sid, client, _ = live
    _rule_of(monkeypatch, {"max": 5, "per": 1, "step": 5, "cap": 15})
    client.session = {"session_index": 2, "session_type": 3}
    supervisor._instances[sid] = type("I", (), {"running": True, "server_id": sid, "acsp": client})()   # (the manager's own report route reads the instance's server id)
    client._apply({"type": "lap_completed", "car_id": 3, "laptime_ms": 90000, "cuts": 2, "leaderboard": [{"car_id": 3, "laps": 1, "laptime_ms": 90000}]})
    assert api.get(f"{V}/servers/{sid}/race-state").json()["cuts"] == {"3": 2}, "the cuts of the lap the acServer closed"
    r = api.post(f"{V}/servers/{sid}/stewards/report", json={"car_id": 3, "name": "Ana", "ms": 900, "wheels": 4, "speed": 120.0, "lap": 2, "spline": 0.4, "pos": [1, 2, 3]})
    assert r.status_code == 204
    r = api.post(f"{V}/servers/{sid}/stewards/report", json={"car_id": 3, "name": "Otro", "ms": 900, "wheels": 4, "speed": 120.0, "lap": 2, "spline": 0.4, "pos": []})
    assert r.status_code == 409, "a report of somebody else's slot is refused and counts for nothing"
    assert api.get(f"{V}/servers/{sid}/race-state").json()["cuts"] == {"3": 2}, "one excursion reported: still below the acServer's 2"


def test_stale_rules_are_read_once_by_concurrent_requests_and_the_snapshot_never_reads_the_database(monkeypatch):
    import threading
    sid = api.post(f"{V}/servers", json={"name": "cuts-threads"}).json()["id"]
    _league_event(sid, RULE)
    league._CUT_RULES.clear()
    reads = []
    real = league.Session

    def counting(*a, **k):
        reads.append(1)
        return real(*a, **k)
    monkeypatch.setattr(league, "Session", counting)
    assert league.cut_rule_cached(sid) is None, "never read: nothing to show, and no database touched"
    assert not reads and live_rc.cut_rule_peek(sid) is None and not reads
    out = []
    ts = [threading.Thread(target=lambda: out.append(league.cut_rule(sid))) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(reads) == 1 and all(r == {"max": 0, "per": 1, "step": 5, "cap": 10} for r in out), "eight requests with a stale cache: one read, the same answer"
    assert live_rc.cut_rule_peek(sid) == {"max": 0, "per": 1, "step": 5, "cap": 10} and len(reads) == 1, "the snapshot sees it from the cache"
