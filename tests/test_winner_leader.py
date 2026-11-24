"""The winner banner of a race with no NASCAR comes when acServer says the LEADER completed the race (has_completed of his row), not when the time runs out and not when the first car crosses the
line after it. The real races' laps (tests/fixtures/timed_races.json) are replayed as timed races.

acServer's rule, MEASURED on the real race of 15 Nov (30 min): at the limit the leader of that moment finishes at his next crossing (Alejandro Villa, 1744 s -> 1851 s); a car that crosses after the limit
but BEFORE him goes on racing (Joseph Marchant crossed at 1811 s and finished at 1932 s, Ignacio Ramallo 1823 s -> 1959 s); every car finishes at its first crossing after the leader's.
(Before: the banner came at the first crossing after the limit, with whoever led at that instant: 2026-11-22, server 3, "GANADOR: Paul Verboonen" 50 s before he completed his last lap.)"""
import json
from pathlib import Path
from types import SimpleNamespace

from app import racecontrol as rcmod
from app.racecontrol import race_leader

RACES = json.loads((Path(__file__).parent / "fixtures" / "timed_races.json").read_text())


def _events(race):
    """Every lap completion in time order: (t_s, car, laps done, laps of every car at that moment)."""
    done = sorted((t / 1000, int(c), i + 1) for c, ts in race["cum_ms"].items() for i, t in enumerate(ts))
    laps, out = {}, []
    for t, c, n in done:
        laps[c] = n
        out.append((t, c, dict(laps)))
    return out


def _finish_times(race, limit_s, car_time):
    """acServer's rule: {car: time it completes}. The leader at the limit is the first car to complete one lap more than anybody had when the time ran out (the car in front on the road: laps, then
    position, not who reached the old lap count first); he completes at that crossing and each other car at its first crossing at or after it. None = the race never ends."""
    top = 0
    for t, _c, laps in _events(race):
        if t < limit_s:
            top = max(laps.values())
    if not top:
        return None, {}
    nxt = next(((t, c) for t, c, laps in _events(race) if t >= limit_s and laps[c] == top + 1), None)
    if nxt is None:
        return None, {}
    end, lead = nxt
    finish = {}
    for (c, _n), t in sorted(car_time.items(), key=lambda kv: kv[1]):
        if t >= end and c not in finish:
            finish[c] = t
    return lead, finish


def _replay(race, limit_s, monkeypatch):
    """Feeds plain_winner as the manager would, event by event, with acServer's has_completed. Returns (names announced, time of the event that announced, the leader at the limit, first car over the line after the limit, finish times)."""
    car_time = {(int(c), i + 1): t / 1000 for c, ts in race["cum_ms"].items() for i, t in enumerate(ts)}
    lead, finish = _finish_times(race, limit_s, car_time)
    named, shown = [], []
    monkeypatch.setattr(rcmod, "state_of", lambda *a, **k: SimpleNamespace(pace_car=None, flag="green", set_flag=lambda *a, **k: None, notify=lambda text, *x, **y: named.append((text, shown[-1] if shown else None))))
    client = SimpleNamespace(session={"session_type": 3, "laps": 0, "time_min": limit_s / 60}, winner_done=False, last_lap_shown=False, session_t0=0.0, server_id=1,
                             cars={int(c): {"driver_name": n} for c, n in race["names"].items()})
    first = None
    for t, car, laps in _events(race):
        shown.append(t)
        if first is None and t >= limit_s:
            first = car
        rows = [{"car_id": c, "laps": n, "has_completed": c in finish and finish[c] <= t} for c, n in sorted(laps.items(), key=lambda kv: (-kv[1], car_time[(kv[0], kv[1])]))]   # (acServer's order: laps, then who got there first)
        rcmod.plain_winner(client, {"car_id": car, "leaderboard": rows})
        if client.winner_done:
            return named, t, lead, first, finish
    return named, None, lead, first, finish


def test_the_reported_race_names_alejandro_villa_when_he_completes_his_last_lap_not_when_the_time_runs_out(monkeypatch):
    race = RACES[0]
    named, at, lead, first, finish = _replay(race, 1800, monkeypatch)
    assert race["names"][str(first)] == "Joseph Marchant", "a lapped car was the first over the line after the time, as it happened"
    assert [text for text, _ in named if text.startswith("GANADOR")] == ["GANADOR: Alejandro Villa"]
    assert round(at, 1) == 1851.0, f"the leader's own crossing, not Joseph's at 1811.6: {at}"
    assert any(text == "BANDERA DE CUADROS" for text, _ in named), "the chequered flag comes with the winner"


def test_every_real_race_announces_the_leader_exactly_when_he_completes_at_any_time_limit(monkeypatch):
    checked, early = 0, 0
    for race in RACES:
        end = max(ts[-1] for ts in race["cum_ms"].values()) / 1000
        for frac in (0.2, 0.35, 0.5, 0.65, 0.8, 0.95):
            named, at, lead, first, finish = _replay(race, end * frac, monkeypatch)
            if lead is None:
                continue   # (the race never ends in these data: nobody completes)
            assert at is not None, (race["race"], frac, "acServer completed the leader and no winner was announced")
            checked += 1
            assert [text for text, _ in named if text.startswith("GANADOR")] == [f"GANADOR: {race['names'][str(lead)]}"], (race["race"], frac, named)
            assert at == finish[lead], (race["race"], frac, at, finish[lead], "the banner is the leader's finish, not an earlier crossing")
            early += first != lead   # (the first car over the line after the limit was not the leader)
    assert checked > 30
    print(f"{checked} replays; in {early} of them a car crossed the line before the leader finished and the old rule would have announced too early")


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
    rcmod.plain_winner(client, {"car_id": 9, "leaderboard": [{"car_id": 9, "laps": 5, "has_completed": True}]})
    assert named == [] and not client.winner_done


def test_the_race_of_2026_11_22_in_server_3_the_banner_waits_for_pauls_last_lap(monkeypatch):
    """The sequence of crossings after the time ran out (CDMX seconds, from the activity log): the old rule named Paul at 21:51:09, 50 s before he completed lap 18 at 21:51:59."""
    named = []
    monkeypatch.setattr(rcmod, "state_of", lambda *a, **k: SimpleNamespace(pace_car=None, flag="green", set_flag=lambda *a, **k: None, notify=lambda text, *x, **y: named.append(text)))
    names = {1: "Paul Verboonen", 2: "Nando", 3: "Alejandro Villa", 4: "Alan Montes", 5: "Francisco"}
    client = SimpleNamespace(session={"session_type": 3, "laps": 0, "time_min": 25}, winner_done=False, last_lap_shown=False, server_id=1, cars={c: {"driver_name": n} for c, n in names.items()})
    # (car, laps after this crossing, completed?) in the order they crossed after the limit; rows: laps of everybody, Paul ahead (17 laps) until his 18th
    laps = {1: 17, 2: 17, 3: 17, 4: 16, 5: 3}
    done = set()

    def cross(car, n, complete=False):
        laps[car] = n
        if complete:
            done.add(car)
        rows = [{"car_id": c, "laps": laps[c], "has_completed": c in done} for c in sorted(laps, key=lambda c: (-laps[c], c))]   # (Paul first among the 17s: he got there first)
        rcmod.plain_winner(client, {"car_id": car, "leaderboard": rows})
    cross(2, 17)   # 21:50:55 Nando
    cross(3, 17)   # 21:51:09 Villa: where the old rule announced
    assert named == [] and not client.winner_done, "the leader has not crossed yet"
    cross(5, 4)    # 21:51:28 a lapped car
    cross(4, 17)   # 21:51:31
    assert named == []
    cross(1, 18, complete=True)   # 21:51:59 Paul completes his last lap
    assert named == ["BANDERA DE CUADROS", "GANADOR: Paul Verboonen"], named
    cross(2, 18, complete=True)   # 21:52:22 Nando after him: nothing more is announced
    assert named == ["BANDERA DE CUADROS", "GANADOR: Paul Verboonen"]


def _tick_world(monkeypatch, clock, rc, client):
    """A manager with one live race: plain_winner/plain_finish_tick see `client` and `rc`, time is `clock[0]`."""
    monkeypatch.setattr(rcmod.time, "time", lambda: clock[0])
    monkeypatch.setattr(rcmod, "state_of", lambda *a, **k: rc)
    monkeypatch.setattr(rcmod.supervisor, "live", lambda: [SimpleNamespace(acsp=client)])


def _client(cars, board=()):
    return SimpleNamespace(session={"session_type": 3, "laps": 0, "time_min": 25}, winner_done=False, last_lap_shown=False, first_done_at=0.0, finish_grace_s=0.0, done_ids=[],
                           finish_flag_pending=False, server_id=1, cars=cars, board=SimpleNamespace(drivers=list(board)))


def _rc(named, flags=None):
    rc = SimpleNamespace(pace_car=None, flag="green", nascar=None, notify=lambda text, *x, **y: named.append(text))
    rc.set_flag = lambda f, **k: (flags.append(f) if flags is not None else None, setattr(rc, "flag", f))
    return rc


def test_a_leader_who_retires_on_his_last_lap_does_not_leave_the_race_without_a_winner(monkeypatch):
    """If the leader never completes, the best placed car acServer marked completed wins when the wait is over (LEADER_GRACE_S, or three laps of the leader's): the TICK names him, because
    no lap event may come any more (the others have finished)."""
    named, clock = [], [1_000.0]
    rc = _rc(named)
    client = _client({1: {"driver_name": "Retired"}, 2: {"driver_name": "Nando"}, 3: {"driver_name": "Lapped"}}, [SimpleNamespace(car_id=1, best_ms=90_000)])
    _tick_world(monkeypatch, clock, rc, client)
    rows = [{"car_id": 1, "laps": 18, "has_completed": False}, {"car_id": 2, "laps": 17, "has_completed": True}, {"car_id": 3, "laps": 16, "has_completed": True}]   # the leader parked on his last lap
    rcmod.plain_winner(client, {"car_id": 2, "leaderboard": rows})
    assert named == [] and client.first_done_at == 1_000.0 and client.finish_grace_s == max(rcmod.LEADER_GRACE_S, 3 * 90), "the leader may still cross: the wait starts"
    clock[0] += client.finish_grace_s - 1
    rcmod.plain_finish_tick(clock[0])
    assert named == [], "not yet"
    clock[0] += 2
    rcmod.plain_finish_tick(clock[0])   # (no lap event came: the tick is what names him)
    assert named == ["BANDERA DE CUADROS", "GANADOR: Nando"] and client.winner_done, "the best placed of the completed, not the lapped one"
    rcmod.plain_finish_tick(clock[0] + 5)
    assert named == ["BANDERA DE CUADROS", "GANADOR: Nando"], "once"


def test_the_chequered_flag_goes_to_the_state_and_waits_for_the_green_when_a_caution_is_in_force(monkeypatch):
    flags, named, clock = [], [], [5_000.0]
    rc = _rc(named, flags)
    client = _client({1: {"driver_name": "Ana"}})
    client.session = {"session_type": 3, "laps": 3, "time_min": 0}
    _tick_world(monkeypatch, clock, rc, client)
    ev = lambda laps, done: {"car_id": 1, "leaderboard": [{"car_id": 1, "laps": laps, "has_completed": done}]}
    rcmod.plain_winner(client, ev(2, False))
    assert flags == ["white"] and rc.flag == "white", "the leader starts the last lap of a race by laps: the white flag, in the state"
    rcmod.plain_winner(client, ev(3, True))
    assert flags == ["white", "checkered"] and named == ["BANDERA BLANCA: última vuelta", "BANDERA DE CUADROS", "GANADOR: Ana"]
    rc.flag, client.winner_done, client.finish_flag_pending, flags[:], named[:] = "vsc", False, False, [], []   # a VSC in force when the leader completes
    rcmod.plain_winner(client, ev(3, True))
    assert flags == [] and named == ["GANADOR: Ana"] and client.finish_flag_pending, "the VSC is not taken off; the winner is named; the flag waits"
    rcmod.plain_finish_tick(clock[0])
    assert flags == [] and client.finish_flag_pending, "the VSC is still out"
    rc.flag = "green"   # the green comes
    rcmod.plain_finish_tick(clock[0] + 1)
    assert flags == ["checkered"] and not client.finish_flag_pending and named[-1] == "BANDERA DE CUADROS", "now the chequered flag, once"
    rcmod.plain_finish_tick(clock[0] + 2)
    assert flags == ["checkered"]


def test_the_automatic_flag_is_checked_and_set_under_the_same_lock(monkeypatch):
    """The director's VSC can arrive from another thread between a check of rc.flag and the change: both happen under the race control lock."""
    held = []
    rc = SimpleNamespace(pace_car=None, flag="green", nascar=None, notify=lambda *a, **k: None)
    rc.set_flag = lambda f, **k: (held.append(rcmod._lock._is_owned()), setattr(rc, "flag", f))
    assert rcmod._flag_out(rc, "white") is True and held == [True]
