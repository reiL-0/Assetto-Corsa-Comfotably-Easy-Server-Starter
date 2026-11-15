"""The winner banner of a race with no NASCAR is the LEADER (most laps), not whoever crosses the line first after the time: a lapped car can get there first (grid race 2026-11-15:
Joseph, 1 lap down, crossed at 1812 s, the leader Alejandro Villa at 1851 s). The real races' laps (tests/fixtures/timed_races.json) are replayed as timed races."""
import json
from pathlib import Path
from types import SimpleNamespace

from app import racecontrol as rcmod
from app.racecontrol import race_leader

RACES = json.loads((Path(__file__).parent / "fixtures" / "timed_races.json").read_text())


def _events(race):
    """Every lap completion in time order: (t_s, car, laps done, leaderboard of that moment [laps per car])."""
    done = sorted((t / 1000, int(c), i + 1) for c, ts in race["cum_ms"].items() for i, t in enumerate(ts))
    laps, out = {}, []
    for t, c, n in done:
        laps[c] = n
        out.append((t, c, dict(laps)))
    return out


def _true_leader(laps, car_time):
    """The oracle: most laps; a tie goes to who completed that lap first."""
    return max(laps, key=lambda c: (laps[c], -car_time[(c, laps[c])]))


def _replay(race, limit_s, monkeypatch):
    """Feeds plain_winner as the manager would; returns (who it names, who crossed first after the limit)."""
    car_time = {(int(c), i + 1): t / 1000 for c, ts in race["cum_ms"].items() for i, t in enumerate(ts)}
    named = []
    monkeypatch.setattr(rcmod, "state_of", lambda *a, **k: SimpleNamespace(pace_car=None, notify=lambda text, *x, **y: named.append(text)))
    client = SimpleNamespace(session={"session_type": 3, "laps": 0, "time_min": limit_s / 60}, winner_done=False, session_t0=0.0, server_id=1,
                             cars={int(c): {"driver_name": n} for c, n in race["names"].items()})
    clock = [0.0]
    monkeypatch.setattr(rcmod.time, "time", lambda: clock[0])
    first = None
    for t, car, laps in _events(race):
        clock[0] = t
        if first is None and t >= limit_s:
            first = car
            ev = {"car_id": car, "leaderboard": [{"car_id": c, "laps": n} for c, n in sorted(laps.items(), key=lambda kv: (-kv[1], car_time[(kv[0], kv[1])]))]}   # (acServer's order: laps, then who got there first)
            rcmod.plain_winner(client, ev)
            return named, first, _true_leader(laps, car_time), client
    return named, first, None, client


def test_the_reported_race_names_alejandro_villa(monkeypatch):
    race = RACES[0]
    named, first, leader, client = _replay(race, 1800, monkeypatch)
    assert race["names"][str(first)] == "Joseph Marchant", "the lapped car was the first over the line, as it happened"
    assert named == ["GANADOR: Alejandro Villa"]


def test_every_real_race_names_the_leader_at_any_time_limit(monkeypatch):
    wrong_before, checked = 0, 0
    for race in RACES:
        end = max(ts[-1] for ts in race["cum_ms"].values()) / 1000
        for frac in (0.2, 0.35, 0.5, 0.65, 0.8, 0.95):
            named, first, leader, client = _replay(race, end * frac, monkeypatch)
            if leader is None:
                continue
            checked += 1
            assert named == [f"GANADOR: {race['names'][str(leader)]}"], (race["race"], frac, named)
            wrong_before += first != leader
    assert checked > 30
    print(f"{checked} replays, {wrong_before} where the first car over the line was not the leader (the old rule named the wrong winner)")


def test_race_leader_takes_the_most_laps_wherever_the_row_is_skips_the_pace_car_and_a_tie_goes_to_the_first_row():
    lb = [{"car_id": 7, "laps": 5}, {"car_id": 2, "laps": 9}, {"car_id": 9, "laps": 12}]
    assert race_leader({"car_id": 7, "leaderboard": lb}) == 9
    assert race_leader({"car_id": 7, "leaderboard": lb}, pace=9) == 2, "the safety car is not the leader"
    tie = [{"car_id": 1, "laps": 4}, {"car_id": 2, "laps": 4}]
    assert race_leader({"car_id": 2, "leaderboard": tie}) == 1, "the car that just crossed arrived last to the tie"


def test_the_safety_car_crossing_the_line_names_nobody(monkeypatch):
    named = []
    monkeypatch.setattr(rcmod, "state_of", lambda *a, **k: SimpleNamespace(pace_car=9, notify=lambda text, *x, **y: named.append(text)))
    client = SimpleNamespace(session={"session_type": 3, "laps": 2, "time_min": 0}, winner_done=False, server_id=1, cars={9: {"driver_name": "Pace"}})
    rcmod.plain_winner(client, {"car_id": 9, "leaderboard": [{"car_id": 9, "laps": 5}]})
    assert named == [] and not client.winner_done
