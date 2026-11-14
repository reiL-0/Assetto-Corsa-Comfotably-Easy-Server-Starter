"""DRS in practice and qualifying: only the rain closes it (no start laps, restart laps or flags); using it then is no penalty, the lap is invalid (the manager's own board) and the driver is told once per lap."""
import pytest

from app.live.board import LiveBoard
from app.live.drs import GRACE_S
from app.live.racecontrol import RaceControl
from app.rulebook import DrsRule
from test_racecontrol import V, _client, _notices, api, live  # noqa: F401  (fixture)

RULE = DrsRule(start_laps_off=2, restart_laps_off=2).model_dump()


@pytest.mark.parametrize('stype', [1, 2])
def test_only_the_rain_closes_the_drs_outside_a_race(stype):
    rc = RaceControl({'session_type': stype})
    rc.drs_tick(RULE, 100.0, 0, 0.0)
    assert rc.drs and rc.drs.on, 'the start laps of the race rule do not hold it in practice or qualifying'
    rc.set_flag('yellow')
    rc.drs_tick(RULE, 101.0, 0, 0.0)
    assert rc.drs.on, 'no flag closes it either'
    rc.drs_tick(RULE, 102.0, 0, 0.5)
    assert not rc.drs.on and rc.drs.why == 'rain'
    rc.drs_tick({**RULE, 'rain_disables': False}, 103.0, 0, 0.5)
    assert rc.drs is None, 'a rule that does not close it in the rain has nothing to do here'
    race = RaceControl({'session_type': 3})
    race.drs_tick(RULE, 100.0, 0, 0.0)
    assert not race.drs.on and race.drs.why == 'start', 'a race keeps all of it'


def test_a_closed_drs_is_known_after_the_grace_and_never_for_the_pace_car():
    rc = RaceControl({'session_type': 1})
    rc.drs_tick(RULE, 100.0, 0, 0.5)
    assert not rc.drs_closed(3, 100.0 + GRACE_S - 0.5), 'inside the grace: the hub learns it by polling'
    assert rc.drs_closed(3, 100.0 + GRACE_S + 0.5)
    rc.pace_car = 9
    assert not rc.drs_closed(9, 200.0)
    rc.drs_tick(RULE, 300.0, 0, 0.0)
    rc.drs_tick(RULE, 400.0, 0, 0.0)
    assert not rc.drs_closed(3, 500.0), 'dry again (after the hysteresis): open'


def test_the_board_rules_one_lap_invalid_and_the_next_is_valid_again():
    b = LiveBoard()
    b.apply({'type': 'new_session', 'session_type': 1, 'track': 'spa', 'track_config': ''})
    b.apply({'type': 'new_connection', 'car_id': 3, 'driver_name': 'Ana', 'driver_guid': '76561190000000001', 'car_model': 'gt3', 'car_skin': 's'})
    lap = lambda ms: b.apply({'type': 'lap_completed', 'car_id': 3, 'laptime_ms': ms, 'cuts': 0, 'leaderboard': [{'car_id': 3, 'laps': 1, 'laptime_ms': ms}]})
    assert b.invalidate_lap(3) and not b.invalidate_lap(3), 'once per lap'
    assert not b.invalidate_lap(7), 'nobody there'
    lap(99_000)   # the quickest, but ruled invalid
    lap(101_000)
    car = next(iter(next(e for e in b.leaderboard()['ConnectedDrivers'])['Cars'].values()))
    assert car['NumLaps'] == 2 and car['ValidLaps'] == 1 and car['BestValidLap'] == 101_000 * 1_000_000


def test_a_lap_ruled_invalid_does_not_follow_the_driver_to_a_new_slot_or_session():
    b = LiveBoard()
    b.apply({'type': 'new_session', 'session_type': 1, 'track': 'spa', 'track_config': ''})
    conn = lambda car: b.apply({'type': 'new_connection', 'car_id': car, 'driver_name': 'Ana', 'driver_guid': '76561190000000001', 'car_model': 'gt3', 'car_skin': 's'})
    lap = lambda car, ms: b.apply({'type': 'lap_completed', 'car_id': car, 'laptime_ms': ms, 'cuts': 0, 'leaderboard': [{'car_id': car, 'laps': 1, 'laptime_ms': ms}]})
    valid = lambda: next(iter(next(iter(b.leaderboard()['ConnectedDrivers']))['Cars'].values())).get('ValidLaps')
    conn(3)
    assert b.invalidate_lap(3)
    b.apply({'type': 'connection_closed', 'car_id': 3})
    conn(5)   # back, in another slot
    lap(5, 100_000)
    assert valid() == 1, 'the first lap of the new connection is valid'
    assert b.invalidate_lap(5)
    b.apply({'type': 'new_session', 'session_type': 2, 'track': 'spa', 'track_config': ''})
    lap(5, 99_000)
    assert valid() == 1, 'a new session starts clean'


@pytest.mark.parametrize('stype', [1, 2])
def test_using_the_closed_drs_in_practice_or_qualifying_invalidates_the_lap_with_no_penalty(live, monkeypatch, stype):
    import time
    sid, c, _sent = live
    c.session = {'session_index': 0, 'session_type': stype}
    c.board.apply({'type': 'new_session', 'session_type': stype, 'track': 'spa', 'track_config': ''})
    c.board.apply({'type': 'new_connection', 'car_id': 3, 'driver_name': 'Ana', 'driver_guid': '76561190000000001', 'car_model': 'gt3', 'car_skin': 's'})
    from app.live.racecontrol import state_of
    hub = _client('reporter')
    use = f'{V}/servers/{sid}/race-control/drs-use'
    rc = state_of(sid, True, c.session, create=True)
    t0 = time.time() - 60
    rc.drs_tick(RULE, t0, 0, 0.0)
    assert hub.post(use, json={'car_id': 3, 'name': 'Ana'}).status_code == 204 and not _notices(sid), 'open: nothing to say'
    rc.drs_tick(RULE, t0 + 1, 0, 0.5)   # it rains
    assert api.get(f'{V}/servers/{sid}/race-state').json()['drs'] == {'on': False, 'why': 'rain', 'until': None}
    assert hub.post(use, json={'car_id': 3, 'name': 'Ana'}).status_code == 204
    assert _notices(sid) == ['DRS CERRADO POR LLUVIA · VUELTA NO VÁLIDA'] and rc.penalties == [], 'a message, no drive-through'
    assert hub.post(use, json={'car_id': 3, 'name': 'Ana'}).status_code == 204 and len(_notices(sid)) == 1, 'once per lap'
    c.board.apply({'type': 'lap_completed', 'car_id': 3, 'laptime_ms': 99_000, 'cuts': 0, 'leaderboard': [{'car_id': 3, 'laps': 1, 'laptime_ms': 99_000}]})
    assert next(iter(next(iter(c.board.leaderboard()['ConnectedDrivers']))['Cars'].values()))['ValidLaps'] == 0, 'that lap is not valid'
    assert hub.post(use, json={'car_id': 3, 'name': 'Ana'}).status_code == 204 and len(_notices(sid)) == 2, 'the next lap, told again'
