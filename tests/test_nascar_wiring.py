"""NASCAR manager wiring, with isolated UDP, chat and clock."""
from types import SimpleNamespace

import pytest

from app import racecontrol as adapter
from app.live import acsp, racecontrol as live_rc
from app.live.board import Driver
from tests.test_racecontrol import live, api, V, _client


def test_leader_and_flag_do_not_reenter(live, monkeypatch):
    sid, client, _ = live
    def say(*args):
        assert not live_rc._lock._is_owned()
    monkeypatch.setattr(adapter, '_say_later', say)
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    set_flag = rc.set_flag
    def apply_flag(*args, **kwargs):
        if kwargs.get('source') == 'nascar':
            assert live_rc._lock._is_owned()
        return set_flag(*args, **kwargs)
    monkeypatch.setattr(rc, 'set_flag', apply_flag)
    calls = []
    original = rc.nascar.on_flag
    def on_flag(flag):
        assert live_rc._lock._is_owned()
        calls.append(flag)
        return original(flag)
    monkeypatch.setattr(rc.nascar, 'on_flag', on_flag)
    for lap in (20, 20, 18, 21, 21):
        state, actions = live_rc.on_lap(sid, lap, 3)
        adapter.nascar_actions(sid, state, actions, 3)
    assert rc.nascar.laps == 21 and rc.flag == 'yellow' and rc.pace_kmh is None
    assert calls == []
    adapter.nascar_actions(sid, rc, rc.set_flag('green'))
    assert calls == ['green'] and rc.nascar.stage == 1


def test_pending_stage_yellow_does_not_overwrite_director_green(live, monkeypatch):
    sid, client, _ = live
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    state, actions = live_rc.on_lap(sid, 21, 3)
    assert rc.nascar.phase == 'break' and ('flag', 'yellow') in actions

    def director_restarts(*args):
        assert not live_rc._lock._is_owned()
        rc.set_flag('green')

    monkeypatch.setattr(adapter, '_say_later', director_restarts)
    adapter.nascar_actions(sid, state, actions, 3)
    assert rc.snapshot()['flag'] == rc.nascar.flag == 'green'
    assert rc.nascar.phase == 'racing' and rc.nascar.stage == 1


def test_reused_slot_finishes_on_its_next_crossing(live, monkeypatch):
    sid, client, sent = live
    monkeypatch.setattr(client, '_record', lambda e: None)
    monkeypatch.setattr(adapter, '_say_later', lambda *args: None)
    monkeypatch.setattr(adapter.supervisor, 'live', lambda: [SimpleNamespace(server_id=sid, acsp=client)])
    monkeypatch.setattr(adapter.time, 'time', lambda: 100)
    from app.stewards import engine
    monkeypatch.setattr(engine, 'on_event', lambda *args: None)
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(100, stages=[100])
    client.board.drivers = [Driver(3), Driver(4)]
    client.cars[4] = {'driver_name': 'Anterior', 'driver_guid': '76561190000000002'}

    def cross(car_id, leader_laps, slot_laps):
        client._apply({'type': 'lap_completed', 'car_id': car_id, 'laptime_ms': 1000, 'cuts': 0,
                       'leaderboard': [{'car_id': 3, 'laps': leader_laps, 'laptime_ms': 1000},
                                       {'car_id': 4, 'laps': slot_laps, 'laptime_ms': 1000}]})

    cross(4, 50, 40)
    client.cars.pop(4)
    client.cars[4] = {'driver_name': 'Nuevo', 'driver_guid': '76561190000000003'}
    cross(4, 99, 1)
    cross(3, 100, 1)
    adapter.finish_nascar(101)
    assert not sent
    cross(4, 100, 2)
    assert not rc.finish_wait
    adapter.finish_nascar(102)
    assert rc.nascar.phase == 'done' and sent == [acsp.encode_next_session()]


def test_session_events_preserve_then_clear(live, monkeypatch):
    sid, client, _ = live
    monkeypatch.setattr(client, '_record', lambda e: None)
    from app import timeline
    from app.stewards import engine
    monkeypatch.setattr(timeline, 'on_session_event', lambda *args: None)
    monkeypatch.setattr(engine, 'on_event', lambda *args: None)
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    client._apply({'type': 'session_info', 'session_index': 0})
    assert live_rc.state_of(sid, True, client.session) is rc and rc.nascar
    client._apply({'type': 'new_session', 'session_index': 1})
    fresh = live_rc.state_of(sid, True, client.session)
    assert fresh is not rc and fresh.nascar is None


@pytest.mark.parametrize('timeout', [False, True])
def test_finish_once(live, monkeypatch, timeout):
    sid, client, sent = live
    monkeypatch.setattr(adapter, '_say_later', lambda *args: None)
    monkeypatch.setattr(adapter.supervisor, 'live', lambda: [SimpleNamespace(server_id=sid, acsp=client)])
    monkeypatch.setattr(adapter.time, 'time', lambda: 100)
    client.cars[4] = {'driver_name': 'Rezagado', 'driver_guid': '76561190000000002'}
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(2, stages=[2])
    count = []
    encoded = []
    encode_next = acsp.encode_next_session
    def next_session():
        assert not live_rc._lock._is_owned()
        encoded.append(1)
        return encode_next()
    monkeypatch.setattr(acsp, 'encode_next_session', next_session)
    original = rc.nascar.done
    monkeypatch.setattr(rc.nascar, 'done', lambda: (count.append(1), original())[1])
    state, actions = live_rc.on_lap(sid, 2, 3)
    adapter.nascar_actions(sid, state, actions, 3)
    assert rc.flag == rc.nascar.flag == 'checkered' and rc.finish_wait == {4}
    adapter.finish_nascar(189)
    assert not sent
    if not timeout:
        live_rc.on_lap(sid, 2, 4)
        assert rc.finish_wait == set()
    adapter.finish_nascar(190 if timeout else 189)
    adapter.finish_nascar(200)
    adapter.nascar_actions(sid, state, actions, 3)
    adapter.finish_nascar(201)
    assert count == [1] and encoded == [1] and sent == [encode_next()]
    assert rc.nascar.phase == 'done'


def test_http_snapshot_and_configuration(live):
    sid, _, _ = live
    url = f'{V}/servers/{sid}/race-control/nascar'
    before = api.get(f'{V}/servers/{sid}/race-state').json()
    assert before['nascar'] is None
    assert before.keys() == {'rev', 'ts', 'running', 'flag', 'pace_kmh', 'penalties', 'vsc_id', 'grace_left',
                            'tolerance_kmh', 'vsc_penalty', 'violations', 'exempt', 'nascar'}
    response = api.post(url, json={'total': 70})
    assert response.status_code == 200
    armed = response.json()
    assert armed.keys() == before.keys()
    assert {k: v for k, v in armed.items() if k not in ('rev', 'ts', 'nascar')} == {k: v for k, v in before.items() if k not in ('rev', 'ts', 'nascar')}
    assert armed['nascar']['total'] == 70 and armed['rev'] > before['rev']
    assert api.patch(url, json={'total': 100}).json()['nascar']['total'] == 100
    assert api.post(url, json={'total': 100, 'stages': [100]}).status_code == 422
    assert api.post(url, json={'total': 0}).json()['nascar'] is None
    assert api.post(url, json={'total': 10, 'shares': [0, 1]}).status_code == 422


def test_acsp_hook_uses_leaderboard_maximum(live, monkeypatch):
    sid, client, _ = live
    monkeypatch.setattr(client, '_record', lambda e: None)
    monkeypatch.setattr(adapter, '_say_later', lambda *args: None)
    from app.stewards import engine
    monkeypatch.setattr(engine, 'on_event', lambda *args: None)
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    client.board.drivers = [Driver(3), Driver(4)]
    for maximum in (10, 10, 8, 11):
        client._apply({'type': 'lap_completed', 'car_id': 4, 'laptime_ms': 1000, 'cuts': 0,
                       'leaderboard': [{'car_id': 3, 'laps': maximum, 'laptime_ms': 1000},
                                       {'car_id': 4, 'laps': 2, 'laptime_ms': 1000}]})
    assert rc.nascar.laps == 11


def test_nascar_is_steward_only(live):
    sid, _, _ = live
    url = f'{V}/servers/{sid}/race-control/nascar'
    assert _client('reporter').post(url, json={'total': 70}).status_code == 403
    assert _client('driver').patch(url, json={'total': 70}).status_code == 403
    assert _client('steward').post(url, json={'total': 70}).status_code == 200
