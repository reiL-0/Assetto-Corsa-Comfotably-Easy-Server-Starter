"""NASCAR manager wiring, with isolated UDP, chat and clock."""
import asyncio
import time
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlmodel import Session

from app import events, servers, racecontrol as adapter
from app.config import settings
from app.db import engine
from app.live import acsp, racecontrol as live_rc
from app.live.board import Driver
from app.models import Event, Server
from app.schemas.servers import ServerIn, SessionIn
from app.services import server_service
from test_racecontrol import live, api, V, _client


SESSION = {'name': 'Oval', 'track': 'oval', 'cars': ['stockcar'], 'race_min': 1440, 'restart': False}


@pytest.fixture
def session_events(monkeypatch):
    from app import timeline
    from app.stewards import engine as stewards_engine
    with Session(engine) as sess:
        sid = servers.create(ServerIn(name='NASCAR'), sess).id
    client = acsp.ACSPClient(sid)
    client.session = {'session_index': 0}
    sent = []
    monkeypatch.setattr(client, 'send', sent.append)
    monkeypatch.setitem(adapter.supervisor._instances, sid, SimpleNamespace(running=True, acsp=client, nascar_config=None))
    monkeypatch.setattr(client, '_record', lambda e: None)
    monkeypatch.setattr(timeline, 'on_session_event', lambda *args: None)
    monkeypatch.setattr(stewards_engine, 'on_event', lambda *args: None)
    monkeypatch.setattr(server_service, 'check_content', lambda body, cars: {car: [] for car in cars})
    yield sid, client, sent
    live_rc.state_of(sid, False)


def _apply_preset(sid, nascar):
    body = {**SESSION, 'nascar': nascar} if nascar is not None else SESSION
    with Session(engine) as sess:
        asyncio.run(server_service.apply(sess, sess.get(Server, sid), SessionIn(**body)))


def _race_state(sid):
    with Session(engine) as sess:
        return adapter.race_state(sid, sess)


@pytest.mark.parametrize('nascar', [{'total': 70}, {'total': 70, 'shares': None}, {'total': 70, 'shares': [20, 50, 30]}])
def test_preset_nascar_survives_dump_save_read_and_apply(session_events, nascar):
    sid, _, _ = session_events
    body = {**SESSION, 'nascar': nascar}
    expected = {'total': 70, 'shares': nascar.get('shares')}
    assert SessionIn(**body).model_dump()['nascar'] == expected
    with Session(engine) as sess:
        eid = events.create_event(events.EventIn(title='NASCAR', session=SessionIn(**body)), sess).id
    with Session(engine) as sess:
        saved = events.get_event(eid, sess).session
        assert saved.model_dump()['nascar'] == expected
        assert events.duplicate_event(eid, sess).session.model_dump()['nascar'] == expected
        applied = asyncio.run(servers.apply_to_server(sess, sess.get(Server, sid), saved))
        assert applied.session['nascar'] == expected
    with Session(engine) as sess:
        assert servers.get_server(sid, sess).session['nascar'] == expected
        assert sess.get(Event, eid).data['nascar'] == expected
        assert sess.get(Server, sid).session['nascar'] == expected
    assert adapter.supervisor.get(sid).nascar_config == expected
    # Editing the template does not change the session already applied to the server.
    with Session(engine) as sess:
        events.update_event(eid, events.EventIn(title='Changed', session=SessionIn(**{**body, 'nascar': {'total': 100}})), sess)
    assert adapter.supervisor.get(sid).nascar_config == expected


@pytest.mark.parametrize('nascar', [
    {'total': 1}, {'total': 0}, {'total': 2.5}, {'total': 70, 'shares': []},
    {'total': 1001}, {'total': 10**400}, {'total': 70, 'shares': [1] * 7},
    {'total': 70, 'shares': [1001, 1]}, {'total': 70, 'shares': [1e308, 1]},
    {'total': 70, 'shares': [0, 1]}, {'total': 70, 'shares': [-1, 1]},
    {'total': 70, 'shares': [float('inf')]}, {'total': 70, 'shares': [float('nan')]},
])
def test_preset_nascar_validation(nascar):
    with pytest.raises(ValidationError):
        SessionIn(**SESSION, nascar=nascar)


@pytest.mark.parametrize('shares', [[float('inf')], [float('-inf')], [float('nan')]])
def test_manual_nascar_rejects_nonfinite_shares_in_the_model(shares):
    with pytest.raises(ValidationError):
        adapter.NascarIn(total=70, shares=shares)


def test_nascar_models_accept_upper_limits():
    nascar = {'total': 1000, 'shares': [1000] * 6}
    assert SessionIn(**SESSION, nascar=nascar).nascar.model_dump() == nascar
    assert adapter.NascarIn(**nascar).shares == nascar['shares']


@pytest.mark.parametrize('previous', [False, True])
@pytest.mark.parametrize('legacy_overflow', [False, True])
def test_invalid_preset_does_not_interrupt_new_session(session_events, monkeypatch, caplog, previous, legacy_overflow):
    from app import timeline
    from app.stewards import engine as stewards_engine
    sid, client, sent = session_events
    old = live_rc.state_of(sid, True, client.session, create=previous)
    if old:
        old.configure_nascar(70)
    adapter.supervisor.get(sid).nascar_config = {'total': 70, 'shares': [1e308, 1]}
    if legacy_overflow:
        def fail(*args):
            raise OverflowError('legacy stage calculation overflow')
        monkeypatch.setattr(live_rc.RaceControl, 'configure_nascar', fail)
    timeline_events, steward_events = [], []
    monkeypatch.setattr(timeline, 'on_session_event', lambda sid, event: timeline_events.append((sid, event)))
    monkeypatch.setattr(stewards_engine, 'on_event', lambda client, event: steward_events.append((client, event)))
    client.restore_when_session_changes = (0, b'restore')
    event = {'type': 'new_session', 'session_index': 2, 'session_type': 3}
    with caplog.at_level('WARNING', logger='acmanager.acsp'):
        client._apply(event)
    rc = live_rc.state_of(sid, True, client.session)
    assert rc is not old and rc.nascar is None
    assert client.session is event
    assert timeline_events == [(sid, event)] and steward_events == [(client, event)]
    assert sent == [b'restore'] and client.restore_when_session_changes is None
    assert any(record.levelname == 'WARNING' and str(sid) in record.message and 'NASCAR' in record.message for record in caplog.records)


@pytest.mark.parametrize('nascar', [
    {'total': 1001}, {'total': 10**400}, {'total': 70, 'shares': [1e308, 1]},
    {'total': 70, 'shares': [1001, 1]}, {'total': 70, 'shares': [1] * 7},
    {'total': 70, 'shares': []}, {'total': 70, 'shares': [0, 1]}, {'total': 70, 'shares': [-1, 1]},
])
def test_http_rejects_invalid_nascar_before_applying_or_arming(live, nascar):
    sid, _, _ = live
    url = f'{V}/servers/{sid}/race-control/nascar'
    before = _race_state(sid)
    for method in (api.post, api.patch):
        assert method(url, json=nascar).status_code == 422
    assert api.post(f'{V}/servers/{sid}/apply', json={**SESSION, 'nascar': nascar}).status_code == 422
    assert _race_state(sid) == before
    assert getattr(adapter.supervisor.get(sid), 'nascar_config', None) is None


@pytest.mark.parametrize('adopt', [False, True])
def test_start_and_adopt_load_preset_before_first_udp_event(session_events, tmp_path, monkeypatch, adopt):
    sid, client, _ = session_events
    nascar = {'total': 70, 'shares': [20, 50, 30]}
    _apply_preset(sid, nascar)
    adapter.supervisor._instances.pop(sid)
    monkeypatch.setattr(settings, 'data_dir', str(tmp_path))
    monkeypatch.setattr(settings, 'acserver_cmd', 'fake-acserver')
    from app import timeline
    monkeypatch.setattr(timeline, 'start_resume', lambda *args: None)
    cwd = tmp_path / 'instances' / str(sid)
    cwd.mkdir(parents=True)
    monkeypatch.setattr(server_service, 'write_instance', lambda s: cwd)
    proc = SimpleNamespace(pid=12345, returncode=None)

    async def spawn(*args, **kwargs):
        return proc

    async def connect(inst, *args, **kwargs):
        assert inst.nascar_config == nascar
        inst.acsp = client
        client._apply({'type': 'new_session', 'session_index': 2, 'session_type': 3})

    monkeypatch.setattr(adapter.supervisor.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(adapter.supervisor.Instance, 'connect_acsp', connect)
    monkeypatch.setattr(adapter.supervisor, '_alive', lambda pid: proc.returncode is None)
    monkeypatch.setattr(adapter.supervisor, '_is_our_acserver', lambda *args: True)
    if adopt:
        (cwd / 'server.pid').write_text('{"pid": 12345}')

    async def scenario():
        try:
            with Session(engine) as sess:
                if adopt:
                    assert await server_service.adopt_running(sess) == 1
                else:
                    assert (await server_service.start(sess, sid))['running']
            assert live_rc.state_of(sid, True, client.session).nascar.ends == [14, 49, 70]
        finally:
            proc.returncode = 0
            await adapter.supervisor.stop(sid)

    asyncio.run(scenario())


@pytest.mark.parametrize('previous', [False, True])
@pytest.mark.parametrize('shares, ends', [(None, [21, 53, 70]), ([20, 50, 30], [14, 49, 70])])
def test_new_race_arms_fresh_engine_from_applied_preset(session_events, previous, shares, ends):
    sid, client, _ = session_events
    _apply_preset(sid, {'total': 70, 'shares': shares})
    old = live_rc.state_of(sid, True, client.session, create=previous)
    if old:
        old.configure_nascar(100)
        old.set_flag('yellow')
    client._apply({'type': 'new_session', 'session_index': 2, 'session_type': 3})
    rc = live_rc.state_of(sid, True, client.session)
    assert rc is not old and rc.flag == 'green'
    assert rc.nascar.total == 70 and rc.nascar.ends == ends and rc.nascar.laps == 0
    assert rc.finish_wait is None
    assert _race_state(sid)['nascar']['total'] == 70


@pytest.mark.parametrize('session_type', [1, 2])
def test_practice_and_qualifying_do_not_arm_preset(session_events, session_type):
    sid, client, _ = session_events
    _apply_preset(sid, {'total': 70})
    old = live_rc.state_of(sid, True, client.session, create=True)
    old.configure_nascar(70)
    client._apply({'type': 'new_session', 'session_index': 0, 'session_type': session_type})
    fresh = live_rc.state_of(sid, True, client.session)
    assert fresh is not old and fresh.nascar is None


def test_session_info_never_arms_or_resets_preset_engine(session_events):
    sid, client, _ = session_events
    _apply_preset(sid, {'total': 70, 'shares': [20, 50, 30]})
    client._apply({'type': 'session_info', 'session_index': 2, 'session_type': 3})
    assert _race_state(sid)['nascar'] is None
    client._apply({'type': 'new_session', 'session_index': 2, 'session_type': 3})
    rc = live_rc.state_of(sid, True, client.session)
    motor = rc.nascar
    live_rc.on_lap(sid, 5, 3)
    rc.configure_nascar(80)
    before = rc.snapshot()
    for _ in range(2):
        client._apply({'type': 'session_info', 'session_index': 2, 'session_type': 3})
        assert live_rc.state_of(sid, True, client.session) is rc
        assert rc.nascar is motor and rc.snapshot() == before
    rc.configure_nascar(0)
    client._apply({'type': 'session_info', 'session_index': 2, 'session_type': 3})
    assert rc.nascar is None


@pytest.mark.parametrize('previous', [False, True])
def test_session_without_nascar_keeps_legacy_behavior(session_events, previous):
    sid, client, _ = session_events
    _apply_preset(sid, {'total': 70})
    _apply_preset(sid, None)  # an old web client replaces the session without a NASCAR block
    assert adapter.supervisor.get(sid).nascar_config is None
    old = live_rc.state_of(sid, True, client.session, create=previous)
    if old:
        old.configure_nascar(70)
    client._apply({'type': 'new_session', 'session_index': 2, 'session_type': 3})
    fresh = live_rc.state_of(sid, True, client.session)
    if old is None:
        assert fresh is None
    else:
        assert fresh is not old and fresh.nascar is None
    assert _race_state(sid)['nascar'] is None


def test_leader_and_flag_do_not_reenter(live, monkeypatch):
    sid, client, _ = live
    def say(*args, **kw):
        assert not live_rc._lock._is_owned()
    monkeypatch.setattr(adapter, '_say', say)
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

    def director_restarts(*args, **kw):
        assert not live_rc._lock._is_owned()
        rc.set_flag('green')

    monkeypatch.setattr(adapter, '_say', director_restarts)
    adapter.nascar_actions(sid, state, actions, 3)
    assert rc.snapshot()['flag'] == rc.nascar.flag == 'green'
    assert rc.nascar.phase == 'racing' and rc.nascar.stage == 1


def test_reused_slot_finishes_on_its_next_crossing(live, monkeypatch):
    sid, client, sent = live
    monkeypatch.setattr(client, '_record', lambda e: None)
    monkeypatch.setattr(adapter, '_say', lambda *args: None)
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
    assert rc.nascar.phase == 'done' and not sent
    adapter.finish_nascar(702)
    assert sent == [acsp.encode_next_session()]


def test_session_events_preserve_then_clear(live, monkeypatch):
    sid, client, _ = live
    monkeypatch.setattr(client, '_record', lambda e: None)
    from app import timeline
    from app.stewards import engine
    monkeypatch.setattr(timeline, 'on_session_event', lambda *args: None)
    monkeypatch.setattr(engine, 'on_event', lambda *args: None)
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    rc.next_at = 700
    client._apply({'type': 'session_info', 'session_index': 0})
    assert live_rc.state_of(sid, True, client.session) is rc and rc.nascar
    assert rc.next_at == 700
    client._apply({'type': 'new_session', 'session_index': 1})
    fresh = live_rc.state_of(sid, True, client.session)
    assert fresh is not rc and fresh.nascar is None and fresh.next_at is None


@pytest.mark.parametrize('timeout', [False, True])
def test_finish_once(live, monkeypatch, timeout):
    sid, client, sent = live
    monkeypatch.setattr(adapter, '_say', lambda *args: None)
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
    finished_at = 190 if timeout else 189
    adapter.finish_nascar(finished_at)
    assert rc.nascar.phase == 'done' and count == [1]
    assert rc.next_at == finished_at + 600
    assert not sent and not encoded
    adapter.finish_nascar(200)
    adapter.nascar_actions(sid, state, actions, 3)
    adapter.finish_nascar(201)
    adapter.finish_nascar(finished_at + 599)
    assert not sent and not encoded
    rev = rc.rev
    adapter.finish_nascar(finished_at + 600)
    assert rc.next_at is None and rc.rev == rev + 1
    assert count == [1] and encoded == [1] and sent == [encode_next()]
    adapter.finish_nascar(finished_at + 600)
    adapter.finish_nascar(finished_at + 601)
    assert count == [1] and encoded == [1] and sent == [encode_next()]
    assert rc.nascar.phase == 'done'


def test_http_snapshot_and_configuration(live):
    sid, _, _ = live
    url = f'{V}/servers/{sid}/race-control/nascar'
    before = api.get(f'{V}/servers/{sid}/race-state').json()
    assert before['nascar'] is None
    assert before['nascar_next_delay_min'] == 10
    assert before.keys() == {'rev', 'ts', 'running', 'flag', 'red_ref', 'pace_kmh', 'penalties', 'vsc_id', 'grace_left',
                            'tolerance_kmh', 'vsc_penalty', 'notices', 'violations', 'exempt', 'nascar', 'nascar_armed', 'caution', 'caution_eta', 'cuts', 'cuts_rule', 'drs', 'obligations', 'give_back_reviews', 'proposal', 'auto_yellow', 'pacecar', 'pace_steam_id', 'nascar_next_delay_min'}
    response = api.post(url, json={'total': 70})
    assert response.status_code == 200
    armed = response.json()
    assert armed.keys() - {'cuts_round', 'cuts_rounds', 'cuts_ladder'} == before.keys()   # (the default cut rule has a ladder: a race state adds the per-round counters the empty template does not have)
    assert {k: v for k, v in armed.items() if k not in ('rev', 'ts', 'nascar', 'nascar_armed', 'cuts_round', 'cuts_rounds', 'cuts_ladder')} == {k: v for k, v in before.items() if k not in ('rev', 'ts', 'nascar', 'nascar_armed')}
    assert before['nascar_armed'] is None and armed['nascar_armed'] == {'total': 70, 'shares': [30, 45, 25]}   # (the arming of the server: what survives practice, qualifying and restarts)
    assert armed['nascar']['total'] == 70 and armed['rev'] > before['rev']
    assert armed['nascar']['next_delay_min'] == 10
    assert api.patch(url, json={'total': 100}).json()['nascar']['total'] == 100
    assert api.post(url, json={'total': 100, 'stages': [100]}).status_code == 422
    assert api.post(url, json={'total': 0}).json()['nascar'] is None
    assert api.post(url, json={'total': 10, 'shares': [0, 1]}).status_code == 422


def test_http_live_delay_configuration(live):
    sid, _, _ = live
    url = f'{V}/servers/{sid}/race-control/nascar'
    expected = 10
    for total, delay in [(70, 2), (100, 60), (100, None), (0, None), (0, 0), (70, None)]:
        response = api.patch(url, json={'total': total, 'next_delay_min': delay})
        assert response.status_code == 200
        state = response.json()
        expected = delay if delay is not None else expected
        assert state['nascar_next_delay_min'] == expected
        if total:
            assert state['nascar']['total'] == total
            assert state['nascar']['next_delay_min'] == expected
        else:
            assert state['nascar'] is None
        assert api.get(f'{V}/servers/{sid}/race-state').json() == state


@pytest.mark.parametrize('delay', [-1, 61])
@pytest.mark.parametrize('method', ['post', 'patch'])
@pytest.mark.parametrize('total', [0, 100])
def test_http_invalid_delay_preserves_total_and_delay(live, delay, method, total):
    sid, _, _ = live
    url = f'{V}/servers/{sid}/race-control/nascar'
    before = api.post(url, json={'total': 70, 'next_delay_min': 2}).json()
    assert getattr(api, method)(url, json={'total': total, 'next_delay_min': delay}).status_code == 422
    assert api.get(f'{V}/servers/{sid}/race-state').json() == before


@pytest.mark.parametrize('delay', [0, 2])
def test_finish_uses_live_delay_and_keeps_an_existing_deadline(live, monkeypatch, delay):
    sid, client, sent = live
    monkeypatch.setattr(adapter, '_say', lambda *args: None)
    monkeypatch.setattr(adapter.supervisor, 'live', lambda: [SimpleNamespace(server_id=sid, acsp=client)])
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(2, stages=[2])
    state, actions = live_rc.on_lap(sid, 2, 3)
    adapter.nascar_actions(sid, state, actions, 3)
    # The field is draining: only the delay changes, keeping the fixed distance.
    url = f'{V}/servers/{sid}/race-control/nascar'
    assert api.patch(url, json={'total': 2, 'next_delay_min': delay}).status_code == 200
    adapter.finish_nascar(100)
    assert rc.nascar.phase == 'done'
    if delay:
        assert rc.next_at == 220 and not sent
        assert api.patch(url, json={'total': 2, 'next_delay_min': 0}).status_code == 200
        assert rc.next_at == 220
        adapter.finish_nascar(219)
        assert not sent
        adapter.finish_nascar(220)
    assert rc.next_at is None and sent == [acsp.encode_next_session()]
    adapter.finish_nascar(1000)
    assert sent == [acsp.encode_next_session()]


def test_acsp_hook_uses_leaderboard_maximum(live, monkeypatch):
    sid, client, _ = live
    monkeypatch.setattr(client, '_record', lambda e: None)
    monkeypatch.setattr(adapter, '_say', lambda *args: None)
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


def test_panel_nascar_is_remembered_for_the_next_race(live, monkeypatch):
    from app import timeline
    from app.stewards import engine as stewards_engine
    sid, client, _ = live
    monkeypatch.setattr(client, '_record', lambda e: None)
    monkeypatch.setattr(timeline, 'on_session_event', lambda *args: None)
    monkeypatch.setattr(stewards_engine, 'on_event', lambda *args: None)
    url = f'{V}/servers/{sid}/race-control/nascar'

    def stored():
        with Session(engine) as sess:
            return (sess.get(Server, sid).session or {}).get('nascar')

    assert api.post(url, json={'total': 70, 'shares': [20, 50, 30]}).status_code == 200
    assert stored() == {'total': 70, 'shares': [20, 50, 30]}
    assert api.patch(url, json={'total': 100}).status_code == 200   # the distance changes, the frozen shares stay
    assert stored() == {'total': 100, 'shares': [20, 50, 30]}
    client._apply({'type': 'new_session', 'session_index': 1, 'session_type': 2})   # qualifying: nothing armed
    assert live_rc.state_of(sid, True, client.session).nascar is None
    client._apply({'type': 'new_session', 'session_index': 2, 'session_type': 3})   # the race arms itself, nobody touched the panel
    rc = live_rc.state_of(sid, True, client.session)
    assert rc.nascar.total == 100 and rc.nascar.ends == [20, 70, 100]
    assert api.post(url, json={'total': 0}).status_code == 200   # disarming forgets it
    assert stored() is None
    client._apply({'type': 'new_session', 'session_index': 2, 'session_type': 3})
    assert live_rc.state_of(sid, True, client.session).nascar is None


def test_panel_nascar_save_failure_is_reported_but_the_race_stays_armed(live, monkeypatch):
    from sqlalchemy.exc import OperationalError
    sid, client, _ = live

    def boom(self):
        raise OperationalError('commit', {}, Exception('disk full'))

    monkeypatch.setattr(Session, 'commit', boom)
    response = api.post(f'{V}/servers/{sid}/race-control/nascar', json={'total': 70})
    assert response.status_code == 503 and 'no se pudo guardar' in response.json()['detail']
    assert live_rc.state_of(sid, True, client.session).nascar.total == 70


def test_the_white_flag_of_the_engine_reaches_the_real_flag_and_the_screen(live):
    sid, client, sent = live
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    rc.nascar.stage, rc.nascar.laps = 2, 68
    state, actions = live_rc.on_lap(sid, 69, 3)
    adapter.nascar_actions(sid, state, actions, 3)
    snap = rc.snapshot()
    assert snap['flag'] == rc.nascar.flag == 'white' and sent == []
    assert [n['text'] for n in snap['notices']] == ['BANDERA BLANCA: última vuelta']


def test_a_green_that_brings_the_white_flag_says_green_first_and_white_after(live):
    sid, client, _ = live
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    rc.nascar.stage, rc.nascar.laps = 2, 68
    adapter.nascar_actions(sid, rc, rc.set_flag('yellow'))
    state, actions = live_rc.on_lap(sid, 69, 3)
    adapter.nascar_actions(sid, state, actions, 3)
    assert rc.flag == 'yellow'
    r = _client('steward').post(f'{V}/servers/{sid}/race-control', json={'flag': 'green'})
    assert r.status_code == 200 and r.json()['flag'] == 'white'
    assert [n['text'] for n in r.json()['notices']][-2:] == ['BANDERA VERDE: se reanuda la carrera', 'BANDERA BLANCA: última vuelta']


def _field(client, **cars):
    client.cars.update({i: {'driver_name': n} for i, n in ((3, 'Ana'), (4, 'Beto'), (5, 'Cleo'))})
    for car, spline_laps in cars.items():
        spline, laps = spline_laps
        client.field.update(int(car[1:]), spline, laps)
        client.field.completed_laps[int(car[1:])] = laps


def test_live_yellow_uses_server_lap_counts_when_spline_progress_disagrees(live):
    sid, client, _ = live
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    _field(client, c3=(0.20, 5), c4=(0.10, 4), c5=(0.11, 4))
    client.field.completed_laps.update({3: 5, 4: 5, 5: 4})
    rc.set_flag('yellow', field=client.field)
    assert rc.caution.lapped() == [5] and rc.caution.free_pass == 5


def test_a_yellow_freezes_the_order_names_the_free_pass_and_a_pass_under_it_costs_a_drive_through(live):
    sid, client, _ = live
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    _field(client, c3=(0.50, 9), c4=(0.48, 9), c5=(0.30, 7))   # 5 is two laps down: the free pass
    r = _client('steward').post(f'{V}/servers/{sid}/race-control', json={'flag': 'yellow'})
    assert r.status_code == 200 and rc.caution and rc.caution.free_pass == 5
    notices = [(n['text'], n['to']) for n in r.json()['notices']]
    assert ('FREE PASS: Cleo', -1) in notices and ('FREE PASS: pasa al pace car por fuera y ponte al final de la fila', 5) in notices
    # car 4 passes car 3 for longer than PASS_HOLD while both go 60 km/h
    client.cars[3]['velocity'] = client.cars[4]['velocity'] = [0, 0, 16.7]
    adapter.nascar_actions(sid, rc, rc.caution_tick(dict(client.field.progress), {3: 60, 4: 60, 5: 60}, 100.0))
    client.field.update(4, 0.52, 9)   # now in front of car 3 by 0.02
    for t in (101.0, 106.0, 130.0):   # swapped at 101, held PASS_HOLD at 106: car 4 is judged at its next crossing of the line (or GIVE_BACK_MAX later), not yet
        adapter.nascar_actions(sid, rc, rc.caution_tick(dict(client.field.progress), {3: 60, 4: 60, 5: 60}, t))
    assert not rc.penalties, 'the place can still be given back'
    adapter.nascar_actions(sid, rc, rc.caution_tick(dict(client.field.progress), {3: 60, 4: 60, 5: 60}, 106.0 + 125.0))
    assert [p['car_id'] for p in rc.penalties] == [4] and rc.penalties[0]['laps'] == 3
    assert 'Beto: adelantó bajo amarilla · DRIVE-THROUGH' in [n['text'] for n in rc.snapshot()['notices']]
    adapter.nascar_actions(sid, rc, rc.set_flag('green', field=client.field))
    assert rc.caution is None, 'the green ends the caution'


def test_a_free_pass_car_that_pits_loses_it_and_there_is_no_caution_without_nascar(live):
    sid, client, _ = live
    rc = live_rc.state_of(sid, True, client.session, create=True)
    _field(client, c3=(0.50, 9), c5=(0.30, 7))
    rc.set_flag('yellow', field=client.field)
    assert rc.caution is None, 'only with NASCAR armed'
    rc.set_flag('green')
    rc.configure_nascar(70)
    actions = rc.set_flag('yellow', field=client.field)
    assert ('caution', 5) in actions
    rc.report_pit(5, True, now=50.0)
    out = rc.caution_tick(dict(client.field.progress), {}, 51.0)
    assert out == [('no_free_pass', 5), ('pit_violation', 5)] and rc.caution.free_pass is None, 'the pits are closed: it loses the free pass and is judged for entering'


def test_to_go_tells_the_wave_around_the_restart_place_and_who_may_choose(live):
    sid, client, _ = live
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    client.cars.update({i: {'driver_name': n} for i, n in ((3, 'Ana'), (4, 'Beto'), (5, 'Cleo'), (6, 'Dora'))})
    for car, spline, laps in ((3, 0.50, 9), (4, 0.30, 9), (5, 0.52, 7), (6, 0.20, 8)):
        client.field.update(car, spline, laps)   # 5 is two laps down and 0.02 ahead of the leader (the wave-around), 6 one lap down (the free pass)
        client.field.completed_laps[car] = laps
    steward = _client('steward')
    assert steward.post(f'{V}/servers/{sid}/race-control/restart', json={}).status_code == 409, 'no caution yet'
    steward.post(f'{V}/servers/{sid}/race-control', json={'flag': 'yellow'})
    assert rc.caution.free_pass == 6
    client.field.update(3, 0.50, 9)
    r = steward.post(f'{V}/servers/{sid}/race-control/restart', json={'to_go': 1})
    assert r.status_code == 200
    notices = {(n['text'], n['to']) for n in r.json()['notices']}
    assert ('UNA PARA VERDE', -1) in notices
    assert ('WAVE-AROUND: pasa al pace car y al líder, y ponte al final de la fila', 5) in notices
    assert ('REINICIO: P1 · ELIGE CARRIL (interior o exterior)', 3) in notices
    assert ('REINICIO: P3 · sin elección: al final de tu vuelta', 5) in notices or ('REINICIO: P4 · sin elección: al final de tu vuelta', 5) in notices
    assert r.json()['caution']['restart'][:2] == [3, 4] and r.json()['caution']['free_pass'] == 6


def test_a_stopped_car_is_proposed_to_the_director_or_puts_the_yellow_out_by_itself(live):
    sid, client, _ = live
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    assert rc.auto_yellow is True, 'the yellow by incident is on by default'
    rc.auto_yellow = False   # (the director turned it off: then it is only a proposal)
    rc.nascar.laps = 5
    _field(client, c3=(0.50, 5), c4=(0.48, 5), c5=(0.46, 5))
    for a, b in ((3, 4), (4, 5), (5, None)):
        rc.watch.on_contact(90.0, a, b, 250.0)   # (contacts, however hard, are no incident)
    fast, stopped = {3: 200.0, 4: 200.0, 5: 200.0}, {3: 200.0, 4: 0.0, 5: 200.0}
    assert rc.incident_step(fast, client.field, 91.0) == (None, []) and rc.flag == 'green'
    rc.incident_step(stopped, client.field, 95.0)
    prop, actions = rc.incident_step(stopped, client.field, 101.5)
    assert prop and prop['kind'] in ('stopped', 'mass_stop') and actions == [] and rc.flag == 'green' and rc.snapshot()['proposal'] == prop, 'only a proposal'
    assert _client('steward').post(f'{V}/servers/{sid}/race-control/auto-yellow', json={'enabled': True}).json()['auto_yellow'] is True
    rc.watch.proposal = None
    for a, b in ((3, 4), (4, 5)):
        rc.watch.on_contact(200.0, a, b, 90.0)
    rc.incident_step(fast, client.field, 201.0)   # (car 4 is up to speed again, then stops once more)
    rc.incident_step(stopped, client.field, 202.0)
    prop, actions = rc.incident_step(stopped, client.field, 208.5)
    assert prop and rc.flag == 'yellow' and rc.nascar.cautions_in_stage == 1, 'the engine counts the caution'
    assert rc.caution and {3, 4, 5} <= rc.caution.involved | {5}, 'the cars of the contact are involved: no free pass for them'
    assert rc.snapshot()['proposal'] is None, 'the caution answers the proposal'


def test_the_cars_that_do_not_race_are_never_an_incident(live, monkeypatch):
    sid, client, _ = live
    monkeypatch.setattr(adapter.supervisor, 'live', lambda: [SimpleNamespace(server_id=sid, acsp=client)])
    monkeypatch.setattr(adapter, '_non_racing_guids', lambda now: frozenset({'76561190000000004'}))
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    rc.auto_yellow = False
    rc.nascar.laps = 5
    _field(client, c3=(0.50, 5), c4=(0.48, 5))
    client.cars[3] = {'driver_guid': '76561190000000003'}
    client.cars[4] = {'driver_guid': '76561190000000004'}   # the caster
    moving, still = [30.0, 0.0, 0.0], [0.0, 0.0, 0.0]   # (m/s)
    client.cars[3]['velocity'] = client.cars[4]['velocity'] = moving
    adapter.incident_tick(100.0)
    client.cars[3]['velocity'] = client.cars[4]['velocity'] = still
    adapter.incident_tick(101.0)
    adapter.incident_tick(108.0)
    assert [p['cars'] for p in [rc.watch.proposal] if p] == [[3]], 'the racing car stopped; the caster did not count'


def test_a_stage_break_yellow_of_the_engine_also_freezes_the_order_and_names_the_free_pass(live):
    sid, client, _ = live
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    _field(client, c3=(0.50, 20), c4=(0.48, 20), c5=(0.30, 18))
    state, actions = live_rc.on_lap(sid, 21, 3)
    assert ('flag', 'yellow') in actions
    adapter.nascar_actions(sid, state, actions, 3)
    assert rc.flag == 'yellow' and rc.caution and rc.caution.free_pass == 5
    assert 'FREE PASS: Cleo' in [n['text'] for n in rc.snapshot()['notices']], 'the notices of the caution came back through the queue'


def test_a_restart_call_with_a_full_field_keeps_every_cars_instruction(live):
    sid, client, _ = live
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    for car in range(30):
        client.cars[car] = {'driver_name': f'P{car}'}
        client.field.update(car, 0.9 - car * 0.01, 9)
    _client('steward').post(f'{V}/servers/{sid}/race-control', json={'flag': 'yellow'})
    r = _client('steward').post(f'{V}/servers/{sid}/race-control/restart', json={'to_go': 2})
    mine = [n for n in r.json()['notices'] if n['text'].startswith('REINICIO')]
    assert len(mine) == 30 and {n['to'] for n in mine} == set(range(30)), 'nobody loses their place to the cap of notices'


def test_the_pace_car_waits_leads_under_a_caution_goes_in_on_to_go_and_is_not_a_competitor(live):
    sid, client, _ = live
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    _field(client, c3=(0.50, 9), c4=(0.48, 9), c5=(0.30, 7))
    client.cars[9] = {'driver_name': 'Safety Car', 'driver_guid': '76561190000000009'}
    client.field.update(9, 0.60, 12)   # it runs ahead of everybody, a "leader" on paper
    steward = _client('steward')
    assert steward.post(f'{V}/servers/{sid}/race-control/pace-car', json={'car_id': 77}).status_code == 409, 'not on the server'
    assert steward.post(f'{V}/servers/{sid}/race-control/pace-car', json={'car_id': 9, 'kmh': 5}).status_code == 422
    r = steward.post(f'{V}/servers/{sid}/race-control/pace-car', json={'car_id': 9, 'kmh': 110})
    assert r.json()['pacecar'] == {'car': 9, 'kmh': 110, 'mode': 'wait'}
    r = steward.post(f'{V}/servers/{sid}/race-control', json={'flag': 'yellow'})
    assert r.json()['pacecar']['mode'] == 'lead'
    assert rc.caution.leader == 3 and 9 not in rc.caution.frozen and rc.caution.free_pass == 5, 'the pace car is not the leader nor in the order'
    steward.post(f'{V}/servers/{sid}/race-control', json={'flag': 'green'})
    assert rc.pace_car_state()['mode'] == 'in' and not rc.pace_car_in
    rc.report_pit(9, True)   # keep the return command while entering and parked in the pits
    for f in ('vsc', 'fcy'):   # a VSC/FCY from green sets its own pace: the pace car stays in the pits
        r = steward.post(f'{V}/servers/{sid}/race-control', json={'flag': f, 'pace_kmh': 60, 'grace_s': 10, 'tolerance_kmh': 5})
        assert r.json()['pacecar']['mode'] == 'in', f
    steward.post(f'{V}/servers/{sid}/race-control', json={'flag': 'yellow'})
    r = steward.post(f'{V}/servers/{sid}/race-control', json={'flag': 'vsc', 'pace_kmh': 60, 'grace_s': 10, 'tolerance_kmh': 5})
    assert r.json()['pacecar']['mode'] == 'lead', 'a yellow turned VSC: the pace car already out keeps leading, it is not left on the track'
    steward.post(f'{V}/servers/{sid}/race-control', json={'flag': 'yellow'})
    r = steward.post(f'{V}/servers/{sid}/race-control/restart', json={})
    assert r.json()['pacecar']['mode'] == 'in' and 9 not in r.json()['caution']['restart']
    r = steward.post(f'{V}/servers/{sid}/race-control', json={'flag': 'green'})
    assert r.json()['pacecar']['mode'] == 'in', 'after "to go" the green does not stop the pace car parking'
    assert steward.post(f'{V}/servers/{sid}/race-control/pace-car', json={'steam_id': None}).json()['pacecar'] is None


def test_the_pace_car_is_not_a_leader_not_a_contact_and_stops_being_one_when_the_slot_changes_hands(live, monkeypatch):
    sid, client, _ = live
    monkeypatch.setattr(client, '_record', lambda e: None)
    from app.stewards import engine
    monkeypatch.setattr(engine, 'on_event', lambda *args: None)
    monkeypatch.setattr(adapter.supervisor, 'live', lambda: [SimpleNamespace(server_id=sid, acsp=client)])
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    client.board.drivers = [Driver(3), Driver(9)]
    client.cars[9] = {'driver_name': 'Safety Car', 'driver_guid': '76561190000000009'}
    rc.set_pace_car(9, 100, '76561190000000009')
    client._apply({'type': 'lap_completed', 'car_id': 3, 'laptime_ms': 1000, 'cuts': 0,
                   'leaderboard': [{'car_id': 3, 'laps': 4, 'laptime_ms': 1000}, {'car_id': 9, 'laps': 40, 'laptime_ms': 1000}]})
    assert rc.nascar.laps == 4, 'the pace car has run more laps than the race: it is not the leader of anything'
    rc.contact(100.0, 3, 9, 90.0)
    rc.contact(100.0, 9, 3, 90.0)
    assert not rc.watch.involved, 'a touch with the pace car makes nobody involved'
    adapter.pace_car_check()
    assert rc.pace_car == 9, 'same driver in the slot'
    client.cars[9] = {'driver_name': 'Otro', 'driver_guid': '76561190000000010'}
    adapter.pace_car_check()
    assert rc.pace_car is None and rc.snapshot()['pacecar'] is None, 'somebody else took the slot: not the pace car any more'
    assert rc.pace_guid == '76561190000000009' and rc.snapshot()['pace_steam_id'] == '76561190000000009', 'but the driver stays named'
    client.cars[4] = {'driver_name': 'Safety Car', 'driver_guid': '76561190000000009'}   # he reconnects in another slot
    adapter.pace_car_check()
    assert rc.pace_car == 4 and rc.snapshot()['pacecar'] == {'car': 4, 'kmh': 100, 'mode': 'wait'}, 'the pace car follows the driver, not the slot'


def test_the_pace_car_is_named_by_steam_id_even_before_he_connects_and_the_old_car_id_still_works(live, monkeypatch):
    sid, client, _ = live
    monkeypatch.setattr(adapter.supervisor, 'live', lambda: [SimpleNamespace(server_id=sid, acsp=client)])
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    steward, url = _client('steward'), f'{V}/servers/{sid}/race-control/pace-car'
    for bad in ({}, {'steam_id': '1234'}, {'steam_id': '７６５６１１９００００００００９'}, {'steam_id': '76561190000000009', 'car_id': 9}, {'steam_id': 'x' * 17}):
        assert steward.post(url, json=bad).status_code == 422, bad
    r = steward.post(url, json={'steam_id': '76561190000000009', 'kmh': 110})
    assert r.status_code == 200 and r.json()['pace_steam_id'] == '76561190000000009' and r.json()['pacecar'] is None, 'named, not connected yet'
    client.cars[6] = {'driver_name': 'Safety Car', 'driver_guid': '76561190000000009'}
    adapter.pace_car_check()
    assert rc.pace_car == 6 and rc.pace_car_kmh == 110
    client.cars[7] = {'driver_name': 'Sin guid'}
    assert steward.post(url, json={'car_id': 7}).status_code == 409, 'a car without SteamID'
    client.cars[7] = {'driver_name': 'Otro', 'driver_guid': '76561190000000007'}
    r = steward.post(url, json={'car_id': 7})
    assert r.json()['pace_steam_id'] == '76561190000000007' and r.json()['pacecar']['car'] == 7, 'car_id (older panels) becomes its driver'
    assert steward.post(url, json={'car_id': None}).json()['pace_steam_id'] == ''


def test_the_directors_choices_outlive_the_session_and_a_pace_car_named_under_caution_leaves_the_order(live):
    sid, client, _ = live
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    _field(client, c3=(0.50, 9), c4=(0.48, 9), c5=(0.30, 7))
    client.cars[9] = {'driver_name': 'Safety Car'}
    client.field.update(9, 0.60, 12)
    rc.set_flag('yellow', field=client.field)
    assert 9 in rc.caution.frozen and rc.caution.leader == 9
    rc.set_pace_car(9, 100, 'g9')
    assert 9 not in rc.caution.frozen and rc.caution.leader == 3
    rc.auto_yellow = False
    client.session = {'session_index': 1}   # the next session
    fresh = live_rc.state_of(sid, True, client.session)
    assert fresh is not rc and fresh.auto_yellow is False and fresh.pace_car == 9 and fresh.pace_guid == 'g9' and fresh.flag == 'green'


def test_after_to_go_the_green_comes_out_when_the_restart_leader_crosses_the_line_and_the_pace_car_keeps_parking(live, monkeypatch):
    sid, client, _ = live
    monkeypatch.setattr(client, '_record', lambda e: None)
    from app.stewards import engine
    monkeypatch.setattr(engine, 'on_event', lambda *args: None)
    monkeypatch.setattr(adapter.supervisor, 'live', lambda: [SimpleNamespace(server_id=sid, acsp=client)])
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    _field(client, c3=(0.50, 9), c4=(0.48, 9))
    client.board.drivers = [Driver(3), Driver(4)]
    rc.set_pace_car(9, 120, 'g9')
    rc.set_flag('yellow', field=client.field)
    assert rc.pace_car_state()['mode'] == 'lead'
    lap = lambda car: client._apply({'type': 'lap_completed', 'car_id': car, 'laptime_ms': 1000, 'cuts': 0, 'leaderboard': [{'car_id': 3, 'laps': 4, 'laptime_ms': 1000}]})
    lap(3)
    assert rc.flag == 'yellow', 'no "to go" yet: crossing the line is only a lap'
    rc.restart_plan(client.field.snapshot())
    assert rc.pace_car_state()['mode'] == 'in'
    lap(4)
    assert rc.flag == 'yellow', 'only the car that takes the restart brings the green'
    lap(3)
    assert rc.flag == 'yellow' and not rc.go_armed and rc.blocked_crossings == 0, 'a crossing before a whole lap since the "to go" is not even counted'
    rc.caution_tick({3: 0.50, 4: 0.48, 9: 0.60}, {}, time.time())   # the restart leader is seen in the middle of its lap
    assert rc.go_armed
    lap(3)
    assert rc.flag == 'yellow', 'the pace car is not in the pit lane yet: the line crossing alone brings no green'
    rc.restart_plan(client.field.snapshot())
    assert not rc.pace_car_pitted, 'a second "to go" changes nothing'
    assert rc.blocked_crossings == 1
    lap(3)
    assert rc.flag == 'yellow' and rc.blocked_crossings == 3 and rc.take_blocked_warning() is False, 'the second blocked crossing told the director (once): the green is manual'
    rc.report_pit(9, True)
    rc.restart_plan(client.field.snapshot())
    assert rc.pace_car_pitted, 'a second "to go" does not forget that it pitted'
    lap(3)
    assert rc.flag == 'green', 'the pace car is in the pits and the restart leader crossed the line'
    assert rc.pace_car_state()['mode'] == 'in', 'the green does not stop the pace car parking'
    rc.set_flag('yellow', field=client.field)
    assert rc.pace_car_state()['mode'] == 'lead', 'a new caution brings it out again'


def test_alone_on_the_track_the_pace_car_arriving_in_the_pits_brings_the_green(live, monkeypatch):
    sid, client, _ = live
    monkeypatch.setattr(adapter.supervisor, 'live', lambda: [SimpleNamespace(server_id=sid, acsp=client)])
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    client.cars[9] = {'driver_name': 'Safety Car', 'driver_guid': 'g9'}
    _field(client, c9=(0.50, 9))
    rc.set_pace_car(9, 120, 'g9')
    rc.set_flag('yellow', field=client.field)
    rc.restart_plan(client.field.snapshot())
    rc.go_armed = True
    adapter.auto_green(sid, rc, 9)
    assert rc.flag == 'yellow', 'to go was called but the pace car is still on the track'
    rc.report_pit(9, True)
    adapter.auto_green(sid, rc, 9)
    assert rc.flag == 'green' and rc.pace_car_state()['mode'] == 'in'


def test_a_caution_ends_by_itself_after_the_pace_cars_third_lap_when_the_track_is_clear_and_asks_the_director_after_six(live, monkeypatch):
    sid, client, _ = live
    monkeypatch.setattr(client, '_record', lambda e: None)
    from app.stewards import engine
    monkeypatch.setattr(engine, 'on_event', lambda *args: None)
    monkeypatch.setattr(adapter.supervisor, 'live', lambda: [SimpleNamespace(server_id=sid, acsp=client)])
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    _field(client, c3=(0.50, 9), c4=(0.48, 9))
    client.board.drivers = [Driver(3), Driver(4)]
    client.cars[9] = {'driver_name': 'Safety Car', 'driver_guid': 'g9'}
    client.cars[4] = {'driver_name': 'Parado', 'velocity': [0.0, 0.0, 0.0]}
    rc.set_pace_car(9, 120, 'g9')
    rc.set_flag('yellow', field=client.field)
    lap = lambda car: client._apply({'type': 'lap_completed', 'car_id': car, 'laptime_ms': 1000, 'cuts': 0, 'leaderboard': [{'car_id': 3, 'laps': 4, 'laptime_ms': 1000}]})
    lap(3)
    assert rc.caution_laps == -1, 'only the pace car\'s laps count'
    lap(9); lap(9); lap(9)
    assert not rc.pace_car_in and rc.caution_laps == 2, 'the first crossing is a part of a lap: two laps done'
    lap(9)
    assert not rc.pace_car_in and rc.caution_laps == 3, 'three laps, but car 4 is standing on the track: not clear'
    lap(9); lap(9); lap(9)
    assert not rc.pace_car_in and rc.caution_notified and rc.caution_laps == 6, 'six laps and not clear: said once, the "to go" is the director\'s'
    msgs = [n['text'] for n in rc.snapshot()['notices']]
    assert any('es manual' in m for m in msgs)
    lap(9)
    assert rc.caution_laps == 6, 'after the warning the automatic "to go" is out'
    rc.set_flag('green', field=client.field); rc.set_flag('yellow', field=client.field)
    assert rc.caution_laps == -1 and not rc.caution_notified, 'a new caution starts the count again'
    client.cars[4]['velocity'] = [30.0, 0.0, 0.0]   # moving: the track is clear
    lap(9); lap(9); lap(9)
    assert not rc.pace_car_in and rc.one_more and rc.caution_laps == 2, 'two laps counted and clear: the "two to go" came out by itself'
    assert any(m.startswith('DOS PARA VERDE') for m in [n['text'] for n in rc.snapshot()['notices']])
    lap(9)
    assert rc.pace_car_in and not rc.one_more and rc.caution_laps == 2, 'a lap later it is the "one to go"'


def test_two_to_go_keeps_the_pace_car_leading_one_more_lap_then_it_is_a_one_to_go(live, monkeypatch):
    sid, client, _ = live
    monkeypatch.setattr(client, '_record', lambda e: None)
    from app.stewards import engine
    monkeypatch.setattr(engine, 'on_event', lambda *args: None)
    monkeypatch.setattr(adapter.supervisor, 'live', lambda: [SimpleNamespace(server_id=sid, acsp=client)])
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    _field(client, c3=(0.50, 9), c4=(0.48, 9))
    client.board.drivers = [Driver(3), Driver(4)]
    client.cars[9] = {'driver_name': 'Safety Car', 'driver_guid': 'g9'}
    rc.set_pace_car(9, 120, 'g9')
    rc.set_flag('yellow', field=client.field)
    lap = lambda car: client._apply({'type': 'lap_completed', 'car_id': car, 'laptime_ms': 1000, 'cuts': 0, 'leaderboard': [{'car_id': 3, 'laps': 4, 'laptime_ms': 1000}]})
    rc.restart_plan(client.field.snapshot(), 2)
    assert rc.pace_car_state()['mode'] == 'lead' and rc.one_more, 'two to go: the pace car keeps leading'
    lap(3)
    assert rc.pace_car_state()['mode'] == 'lead' and rc.flag == 'yellow', 'a competitor crossing changes nothing'
    lap(9)
    assert rc.pace_car_state()['mode'] == 'in' and not rc.one_more, 'the pace car crossing makes it the one to go'
    rc.report_pit(9, True)
    rc.go_armed = True
    lap(3)
    assert rc.flag == 'green', 'the restart leader crossing after the pace car pitted brings the green'


def test_the_automatic_to_go_ignores_the_pace_car_in_the_pit_lane_and_survives_the_leader_leaving(live, monkeypatch):
    sid, client, _ = live
    monkeypatch.setattr(client, '_record', lambda e: None)
    from app.stewards import engine
    monkeypatch.setattr(engine, 'on_event', lambda *args: None)
    monkeypatch.setattr(adapter.supervisor, 'live', lambda: [SimpleNamespace(server_id=sid, acsp=client)])
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    _field(client, c3=(0.50, 9), c4=(0.48, 9))
    client.board.drivers = [Driver(3), Driver(4)]
    client.cars[3] = {'driver_name': 'A', 'velocity': [30.0, 0.0, 0.0]}
    client.cars[4] = {'driver_name': 'B', 'velocity': [30.0, 0.0, 0.0]}
    client.cars[9] = {'driver_name': 'Safety Car', 'driver_guid': 'g9'}
    rc.set_pace_car(9, 120, 'g9')
    rc.set_flag('yellow', field=client.field)
    lap = lambda car: client._apply({'type': 'lap_completed', 'car_id': car, 'laptime_ms': 1000, 'cuts': 0, 'leaderboard': [{'car_id': 3, 'laps': 4, 'laptime_ms': 1000}]})
    rc.report_pit(9, True)
    lap(9)
    assert rc.caution_laps == -1, 'a crossing in the pit lane is not a lap on the track'
    rc.set_pace_car(None)
    rc.set_flag('green', field=client.field); rc.set_flag('yellow', field=client.field)
    leader = rc.caution.leader
    del client.cars[leader]
    other = 4 if leader == 3 else 3
    for _ in range(4):
        lap(other)
    assert rc.pace_car_in, 'the leader left: the other car\'s laps count and the "to go" came out'


def _rolling(live, monkeypatch, with_pace_car=True):
    sid, client, _ = live
    monkeypatch.setattr(client, '_record', lambda e: None)
    from app.stewards import engine
    monkeypatch.setattr(engine, 'on_event', lambda *args: None)
    monkeypatch.setattr(adapter.supervisor, 'live', lambda: [SimpleNamespace(server_id=sid, acsp=client)])
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    client.cars[3] = {'driver_name': 'A', 'velocity': [30.0, 0.0, 0.0]}
    client.cars[9] = {'driver_name': 'Safety Car', 'driver_guid': 'g9'}
    if with_pace_car:
        rc.set_pace_car(9, 120, 'g9')
    lap = lambda car, laps: client._apply({'type': 'lap_completed', 'car_id': car, 'laptime_ms': 1000, 'cuts': 0, 'leaderboard': [{'car_id': 3, 'laps': laps, 'laptime_ms': 1000}]})
    return sid, client, rc, lap


def test_a_rolling_start_runs_one_formation_lap_then_the_one_to_go_and_the_green_without_touching_the_engine(live, monkeypatch):
    sid, client, rc, lap = _rolling(live, monkeypatch)
    adapter.rolling_start(rc, client.cars)
    assert rc.formation and rc.flag == 'yellow' and rc.caution is None and rc.pace_car_state()['mode'] == 'lead'
    assert rc.nascar.cautions_in_stage == 0 and rc.nascar.flag == 'green', 'the engine does not hear of the formation yellow'
    lap(3, 1)
    assert rc.nascar.laps == 0, 'the formation lap is not a lap of the race'
    assert rc.formation and not rc.pace_car_in, 'no crossing of the pace car yet'
    lap(9, 1)
    assert rc.pace_car_in and rc.pace_car_state()['mode'] == 'in', 'the first crossing brings the one to go: it pits at the end of the formation lap'
    lap(3, 1)
    assert rc.flag == 'yellow', 'the pace car is not in the pits yet'
    lap(3, 1)
    assert rc.flag == 'yellow' and rc.blocked_crossings == 3, 'two blocked crossings: the director was told (once)'
    rc.report_pit(9, True)
    lap(9, 1)
    assert rc.flag == 'yellow', 'the pace car crossing is not the green when there are competitors'
    lap(3, 1)
    assert rc.flag == 'green' and not rc.formation and rc.nascar.cautions_in_stage == 0
    lap(3, 2)
    assert rc.nascar.laps == 1, 'the race counts from the green: the formation lap is out'


def test_a_rolling_start_needs_the_pace_car_and_alone_the_pace_car_arriving_in_the_pits_brings_the_green(live, monkeypatch):
    sid, client, rc, lap = _rolling(live, monkeypatch, with_pace_car=False)
    adapter.rolling_start(rc, client.cars)
    assert rc.flag == 'green' and not rc.formation, 'no pace car: the standing start'
    rc.set_pace_car(9, 120, 'g9')
    del client.cars[3]
    adapter.rolling_start(rc, client.cars)
    assert rc.formation
    lap(9, 0)
    assert rc.pace_car_in
    rc.report_pit(9, True)
    adapter.auto_green(sid, rc, 9)
    assert rc.flag == 'green' and not rc.formation


def test_the_formation_ends_with_the_green_when_the_pace_car_leaves_or_the_director_acts(live, monkeypatch):
    sid, client, rc, lap = _rolling(live, monkeypatch)
    adapter.rolling_start(rc, client.cars)
    rc.set_pace_car(None)
    assert rc.flag == 'green' and not rc.formation, 'nobody leads the field any more'
    rc.set_pace_car(9, 120, 'g9')
    adapter.rolling_start(rc, client.cars)
    assert rc.formation
    lap(3, 1)
    rc.set_flag('green', field=client.field)
    assert rc.flag == 'green' and not rc.formation and rc.lap_base == 1, 'the director\'s green ends the formation and the formation lap is not counted'
    lap(3, 2)
    assert rc.nascar.laps == 1, 'the race counts from the director\'s green'


def test_a_directors_yellow_in_the_formation_is_a_real_caution(live, monkeypatch):
    sid, client, rc, lap = _rolling(live, monkeypatch)
    adapter.rolling_start(rc, client.cars)
    _field(client, c3=(0.50, 1))
    rc.set_flag('yellow', field=client.field)
    assert not rc.formation and rc.caution is not None and rc.nascar.cautions_in_stage == 1, 'it starts a caution with its procedures'


def test_held_notices_stay_through_the_caution_are_replaced_by_their_tag_and_go_with_the_green(live):
    sid, client, _ = live
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    _field(client, c3=(0.50, 9), c4=(0.48, 9), c5=(0.30, 7))   # 5 is two laps down: the free pass
    adapter.nascar_actions(sid, rc, rc.set_flag('yellow', field=client.field))
    texts = lambda: [(n['text'], n['kind'], n['to']) for n in rc.snapshot()['notices']]
    assert ('FREE PASS: Cleo', 'hold', -1) in texts() and any(t.startswith('FREE PASS: pasa') and k == 'hold' and to == 5 for t, k, to in texts())
    left = {n['text']: n['left_s'] for n in rc.snapshot()['notices'] if n['kind'] == 'hold'}
    assert min(left.values()) > 600, 'a held notice does not run out in seconds'
    rc.notify('UNA PARA VERDE', 'flag', hold='togo'); rc.notify('DOS PARA VERDE', 'flag', hold='togo')
    assert [t for t, k, _ in texts() if k == 'hold' and 'PARA VERDE' in t] == ['DOS PARA VERDE'], 'the same tag replaces the old one'
    rc.set_flag('green', field=client.field)
    assert not [t for t in texts() if t[1] == 'hold'], 'the green takes the held notices away'


def test_a_car_entering_the_closed_pit_lane_gets_a_drive_through_and_the_directors_permission_prevents_it(live):
    sid, client, _ = live
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    _field(client, c3=(0.50, 9), c4=(0.48, 9), c5=(0.30, 7))
    adapter.nascar_actions(sid, rc, rc.set_flag('yellow', field=client.field))
    assert any(n['text'].startswith('BOXES CERRADOS') and n['kind'] == 'hold' for n in rc.snapshot()['notices'])
    assert rc.pit_exception(4, True)
    rc.report_pit(4, True)   # let in
    rc.report_pit(3, True)   # not
    adapter.nascar_actions(sid, rc, rc.caution_tick(dict(client.field.progress), {3: 60, 4: 60, 5: 60}, 100.0))
    assert [p['car_id'] for p in rc.penalties] == [3] and rc.penalties[0]['laps'] == 3
    assert 'Ana: entró a boxes cerrados · DRIVE-THROUGH' in [n['text'] for n in rc.snapshot()['notices']]
    rc.set_flag('green', field=client.field)
    assert not rc.pit_exception(4, True), 'no caution: nothing to let in'


def test_the_directors_permission_comes_with_its_notice_and_goes_with_the_caution(live):
    sid, client, _ = live
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    _field(client, c3=(0.50, 9), c4=(0.48, 9))
    rc.set_flag('yellow', field=client.field)
    assert rc.pit_exception(4, True)
    assert [n['to'] for n in rc.snapshot()['notices'] if n['text'].startswith('BOXES: tienes')] == [4]
    rc.set_flag('green', field=client.field)
    assert not [n for n in rc.snapshot()['notices'] if n['kind'] == 'hold']


def test_the_car_that_completes_the_distance_is_announced_as_the_winner(live):
    sid, client, _ = live
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(3, stages=[3])
    _field(client, c3=(0.50, 3), c4=(0.48, 3))
    state, actions = live_rc.on_lap(sid, 3, 3)
    adapter.nascar_actions(sid, state, actions, 3)
    assert rc.flag == 'checkered'
    won = [n for n in rc.snapshot()['notices'] if n['text'].startswith('GANADOR')]
    assert [n['text'] for n in won] == ['GANADOR: Ana'] and won[0]['kind'] == 'hold' and won[0]['left_s'] > 60, 'the winner is held (until the session restarts)'


def test_the_director_can_launch_a_fake_winner_notice_to_see_the_banner(live):
    sid, client, _ = live
    url = f'{V}/servers/{sid}/race-control/winner-test'
    r = _client('steward').post(url, json={'name': 'Sofia Rios'})
    won = [n for n in r.json()['notices'] if n['kind'] == 'winner']
    assert r.status_code == 200 and [n['text'] for n in won] == ['GANADOR: Sofia Rios'] and won[0]['left_s'] <= 15
    assert _client('steward').post(url, json={'name': 'a"b'}).status_code == 422   # (the hub reads notices with a regex)
    assert _client('steward').post(url, json={'name': ''}).status_code == 422


def test_a_race_without_nascar_announces_the_first_car_to_finish_the_laps_or_the_time(live, monkeypatch):
    sid, client, _ = live
    monkeypatch.setattr(client, '_record', lambda e: None)
    from app.stewards import engine
    monkeypatch.setattr(engine, 'on_event', lambda *args: None)
    _field(client, c3=(0.50, 2), c4=(0.48, 2))
    lap = lambda car, laps: client._apply({'type': 'lap_completed', 'car_id': car, 'laptime_ms': 1000, 'cuts': 0, 'leaderboard': [{'car_id': 3, 'laps': laps, 'laptime_ms': 1000}, {'car_id': 4, 'laps': laps - 1, 'laptime_ms': 1000}] if car == 3 else [{'car_id': 3, 'laps': laps, 'laptime_ms': 1000}, {'car_id': 4, 'laps': laps, 'laptime_ms': 1000}]})
    winners = lambda: [n['text'] for n in live_rc.state_of(sid, True, client.session, create=True).snapshot()['notices'] if n['text'].startswith('GANADOR')]
    client.session = {'session_type': 3, 'laps': 3, 'time_min': 0}; client.winner_done = False
    lap(3, 2)
    assert winners() == [], 'two laps of three'
    lap(3, 3); lap(4, 3)
    assert winners() == ['GANADOR: Ana'], 'the first to complete the distance, once'
    client.session = {'session_type': 3, 'laps': 0, 'time_min': 10}; client.winner_done = False; client.session_t0 = time.time() - 30
    live_rc._states.clear()
    lap(4, 5)
    assert winners() == [], 'ten minutes have not passed'
    client.session_t0 = time.time() - 601
    lap(4, 6)
    assert winners() == ['GANADOR: Beto'], 'a timed race: the first to cross after the time'
    client.session = {'session_type': 2, 'laps': 0, 'time_min': 10}; client.winner_done = False; live_rc._states.clear()
    lap(4, 7)
    assert winners() == [], 'only a race has a winner'


def _break_notices(rc):
    return [n['text'] for n in rc.snapshot()['notices']]


def test_a_stage_break_opens_the_pits_and_says_the_lap_where_the_green_comes(live):
    sid, client, _ = live
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    _field(client, c3=(0.50, 20), c4=(0.48, 20), c5=(0.30, 18))
    state, actions = live_rc.on_lap(sid, 21, 3)   # the leader ends the stage on the line
    adapter.nascar_actions(sid, state, actions, 3)
    texts = _break_notices(rc)
    assert rc.caution.pits_open and 'BOXES ABIERTOS: aprovecha para parar' in texts and 'BOXES CERRADOS: solo con permiso del director' not in texts
    assert 'REINICIO: bandera verde en la vuelta 26' in texts
    go_at = None
    for lap in range(21, 30):   # the automatic "to go" comes where the notice counted on: the green is the crossing after it
        if lap > 21:
            live_rc.on_lap(sid, lap, 3)
        if rc.caution_lap(3, {3: 200.0, 4: 200.0, 5: 200.0}, 1000.0 + lap, {3, 4, 5}) == 'go':
            go_at = lap
            break
    assert go_at == 26 - 2
    rc.caution.pit_enter(4)
    assert rc.caution.pit_breakers() == [], 'nobody is judged for entering the pits of a stage break'


def test_a_stage_break_that_comes_out_in_mid_lap_counts_the_lap_it_is_in(live):
    sid, client, _ = live
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    _field(client, c3=(0.50, 18), c4=(0.48, 18), c5=(0.30, 16))
    live_rc.on_lap(sid, 18, 3)
    assert _client('steward').post(f'{V}/servers/{sid}/race-control', json={'flag': 'yellow'}).status_code == 200   # within the last 10 laps of the stage
    texts = _break_notices(rc)
    assert rc.nascar.phase == 'break' and rc.caution.pits_open and 'REINICIO: bandera verde en la vuelta 24' in texts
    go_at = next(lap for lap in range(19, 30) if (live_rc.on_lap(sid, lap, 3), rc.caution_lap(3, {3: 200.0, 4: 200.0, 5: 200.0}, 1000.0 + lap, {3, 4, 5}))[1] == 'go')
    assert go_at == 24 - 2


def test_a_stage_break_behind_a_pace_car_opens_the_pits_but_does_not_promise_a_lap(live):
    sid, client, _ = live
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    rc.pace_car = 9
    _field(client, c3=(0.50, 20), c4=(0.48, 20), c5=(0.30, 18))
    state, actions = live_rc.on_lap(sid, 21, 3)
    adapter.nascar_actions(sid, state, actions, 3)
    texts = _break_notices(rc)
    assert rc.caution.pits_open and 'BOXES ABIERTOS: aprovecha para parar' in texts and not any(t.startswith('REINICIO: bandera verde') for t in texts)


def test_the_pits_of_an_incident_caution_open_by_themselves_after_the_first_full_lap(live):
    sid, client, _ = live
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    _field(client, c3=(0.50, 9), c4=(0.48, 9), c5=(0.46, 9))
    assert _client('steward').post(f'{V}/servers/{sid}/race-control', json={'flag': 'yellow'}).status_code == 200
    assert not rc.caution.pits_open and any(m.startswith('BOXES CERRADOS: solo con permiso del director') for m in _break_notices(rc))
    assert [m for m in _break_notices(rc) if m.startswith('BOXES CERRADOS')][0].endswith(f' · abren en la vuelta {rc.nascar.laps + 2}'), 'the notice says on which lap the pit lane opens (the second crossing)'
    assert rc.caution_eta()['pits'] == rc.nascar.laps + 2
    speeds, cars = {3: 200.0, 4: 200.0, 5: 200.0}, {3, 4, 5}
    assert rc.caution_lap(3, speeds, 1000.0, cars) is None, 'the first crossing is only part of a lap'
    rc.caution.pit_enter(4)
    assert rc.caution.pit_breakers() == [4], 'closed until then'
    assert rc.caution_lap(3, speeds, 1090.0, cars) == 'pits'
    rc.caution.pit_enter(5)
    assert rc.caution.pits_open and rc.caution.pit_breakers() == []
    assert rc.caution_eta()['pits'] is None, 'the pit lane is open: no lap to announce'
    assert rc.caution_lap(3, speeds, 1180.0, cars) == 'two' and rc.caution_laps == 2, 'then the lap count goes on: the automatic "two to go" comes a lap before the "one to go"'


def test_the_order_at_the_end_of_each_stage_is_written_down_and_becomes_the_races_own_file_when_the_session_ends(live, monkeypatch):
    import json
    from pathlib import Path
    from app.config import settings
    sid, client, _ = live
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    _field(client, c3=(0.50, 20), c4=(0.48, 20), c5=(0.30, 18))
    for car, guid in ((3, '76561190000000003'), (4, '76561190000000004'), (5, '76561190000000005')):
        client.cars[car]['driver_guid'] = guid
    monkeypatch.setattr(adapter, '_non_racing_guids', lambda now: frozenset({'76561190000000004'}))   # car 4 is the director
    state, actions = live_rc.on_lap(sid, 21, 3)   # the leader ends stage 1 on the line
    adapter.nascar_actions(sid, state, actions, 3)
    d = Path(settings.data_dir) / 'instances' / str(sid) / 'results'
    pending = json.loads((d / 'stages.pending').read_text())
    assert pending['stages'] == [{'stage': 1, 'order': ['76561190000000003', '76561190000000005']}], 'the order of the frozen field, without the director'
    adapter.claim_stages(sid, 'results/2026_10_30_2_00_QUALIFY.json')   # not a race: the journal stays
    assert (d / 'stages.pending').is_file()
    adapter.claim_stages(sid, 'results\\2026_10_30_2_00_RACE.json')
    assert not (d / 'stages.pending').exists() and json.loads((d / '2026_10_30_2_00_RACE.stages').read_text()) == pending


def test_a_stage_already_recorded_is_not_rewritten_and_a_car_that_left_takes_no_place(live, monkeypatch):
    import json
    from pathlib import Path
    from app.config import settings
    sid, client, _ = live
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    _field(client, c3=(0.50, 20), c4=(0.48, 20), c5=(0.30, 18))
    for car in (3, 4, 5):
        client.cars[car]['driver_guid'] = f'7656119000000000{car}'
    monkeypatch.setattr(adapter, '_non_racing_guids', lambda now: frozenset())
    state, actions = live_rc.on_lap(sid, 21, 3)
    adapter.nascar_actions(sid, state, actions, 3)
    inst = adapter._live(sid)
    rc.caution.gone.add(5)   # car 5 left before the next look
    client.cars[4]['driver_guid'] = '76561190000000099'   # (and car 4's slot is somebody else's now)
    adapter._record_stage(sid, inst, rc)   # the break action again: the stage is over, nothing is rewritten
    d = Path(settings.data_dir) / 'instances' / str(sid) / 'results'
    assert json.loads((d / 'stages.pending').read_text())['stages'][0]['order'] == ['76561190000000003', '76561190000000004', '76561190000000005']


def test_the_pace_car_an_event_names_is_put_once_and_the_director_changes_it_after(live, monkeypatch):
    sid, client, _ = live
    inst = SimpleNamespace(server_id=sid, acsp=client, pace_steam_id='76561190000000009')
    monkeypatch.setattr(adapter.supervisor, 'live', lambda: [inst])
    client.cars[6] = {'driver_name': 'Safety Car', 'driver_guid': '76561190000000009'}
    adapter.pace_car_check()
    rc = live_rc.state_of(sid, True, client.session)
    assert rc.pace_car == 6 and rc.pace_guid == '76561190000000009' and inst.pace_steam_id is None, 'named by the event, in his slot'
    rc.set_pace_car(None)
    adapter.pace_car_check()
    assert rc.pace_car is None and rc.pace_guid == '', 'once only: the director removing him stands'
    for bad in ('1234', 'x' * 17, '７６５６１１９００００００００９'):
        with pytest.raises(ValidationError):
            SessionIn(**{**SESSION, 'pace_steam_id': bad})
    assert SessionIn(**{**SESSION, 'pace_steam_id': '76561190000000009'}).pace_steam_id == '76561190000000009' and SessionIn(**SESSION).pace_steam_id is None


def test_applying_the_same_pace_car_again_does_not_undo_the_director(session_events):
    sid, _, _ = session_events
    inst = adapter.supervisor._instances[sid]

    def apply(pace):
        with Session(engine) as sess:
            asyncio.run(server_service.apply(sess, sess.get(Server, sid), SessionIn(**{**SESSION, 'pace_steam_id': pace})))
    apply('76561190000000009')
    assert inst.pace_steam_id == '76561190000000009', 'a new pace car is armed'
    inst.pace_steam_id = None   # pace_car_check named him, or the director chose
    apply('76561190000000009')
    assert inst.pace_steam_id is None, 'the same one again is not armed again'
    apply('76561190000000010')
    assert inst.pace_steam_id == '76561190000000010', 'a different one is'


def test_the_caution_eta_tells_the_lap_of_each_step(live):
    sid, client, _ = live
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    _field(client, c3=(0.50, 9), c4=(0.48, 9))
    assert rc.caution_eta() is None, 'no caution of ours'
    rc.set_flag('yellow', field=client.field)
    L = rc.nascar.laps
    assert rc.caution_eta() == {'pits': L + 2, 'two': L + 3, 'one': L + 4, 'green': L + 5, 'green_max': L + 8}
    rc.restart_plan(client.field.snapshot(), 2)
    assert rc.caution_eta() == {'pits': None, 'two': None, 'one': L + 1, 'green': L + 2, 'green_max': L + 2}, 'two to go called: the next crossing is the one to go'
    rc.restart_plan(client.field.snapshot(), 1)
    assert rc.caution_eta() == {'pits': None, 'two': None, 'one': None, 'green': L + 1, 'green_max': L + 1}
    rc.set_flag('green', field=client.field)
    assert rc.caution_eta() is None


def test_the_green_does_not_wait_for_ever_for_a_restart_leader_the_progress_never_shows_mid_lap(live):
    sid, client, _ = live
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    _field(client, c3=(0.50, 9), c4=(0.48, 9))
    rc.set_flag('yellow', field=client.field)
    rc.restart_plan(client.field.snapshot())
    assert rc.green_if_due(3) is None and not rc.go_armed, 'first crossing: too soon'
    assert rc.green_if_due(3) is not None and rc.flag == 'green', 'the second one is a whole lap later: the green, armed or not'


def test_the_caution_eta_stops_promising_laps_that_are_past_and_gives_up_after_the_warning(live):
    sid, client, _ = live
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    _field(client, c3=(0.50, 9), c4=(0.48, 9))
    rc.set_flag('yellow', field=client.field)
    rc.caution_laps = 5
    eta = rc.caution_eta()
    nxt = rc.nascar.laps + 1
    assert eta['one'] == nxt and eta['green'] == nxt + 1 and eta['two'] is None, 'the track is not clear: the next clear crossing, never a lap in the past'
    rc.caution_notified = True
    assert rc.caution_eta()['green'] is None, 'after the six-lap warning the "to go" is the director\'s'


def test_a_yellow_behind_a_pace_car_without_nascar_runs_the_to_go_and_the_green_but_judges_nobody(live):
    sid, client, _ = live
    client.session = {'session_index': 0, 'session_type': 1}   # practice
    rc = live_rc.state_of(sid, True, client.session, create=True)
    _field(client, c3=(0.50, 9), c4=(0.30, 9), c5=(0.10, 7))   # 5 is the pace car
    rc.set_flag('yellow', field=client.field)
    assert rc.caution is None, 'no pace car: a plain flag'
    rc.set_flag('green')
    rc.set_pace_car(5)
    assert rc.nascar is None
    assert rc.set_flag('yellow', field=client.field) == [] and rc.caution and rc.caution.pace_only and rc.caution.free_pass is None and rc.caution.pits_open
    assert rc.caution_tick(dict(client.field.progress), {3: 60, 4: 60}, 100.0) == []
    steward = _client('steward')
    r = steward.post(f'{V}/servers/{sid}/race-control/restart', json={'to_go': 1})
    assert r.status_code == 200 and r.json()['caution']['restart'] == [3, 4] and rc.pace_car_in
    assert ('UNA PARA VERDE', -1) in {(n['text'], n['to']) for n in r.json()['notices']}
    assert rc.snapshot()['pacecar']['mode'] == 'in'
    rc.report_pit(5, True, now=50.0)
    rc.go_armed = True
    adapter.auto_green(sid, rc, 3)
    assert rc.flag == 'green' and rc.caution is None, 'the restart leader crossing brings the green'


def test_manual_to_go_works_under_a_plain_yellow_without_nascar_or_pace_car(live):
    sid, client, _ = live
    rc = live_rc.state_of(sid, True, client.session, create=True)
    steward = _client('steward')
    url = f'{V}/servers/{sid}/race-control/restart'
    assert rc.nascar is None and rc.pace_car is None
    assert steward.post(url, json={'to_go': 2}).status_code == 409, 'green: nothing to call'
    steward.post(f'{V}/servers/{sid}/race-control', json={'flag': 'yellow'})
    assert rc.caution is None, 'a plain yellow with nobody to lead makes no NASCAR caution'
    r = steward.post(url, json={'to_go': 2})
    assert r.status_code == 200
    assert ('DOS PARA VERDE', -1) in {(n['text'], n['to']) for n in r.json()['notices']}
    r = steward.post(url, json={'to_go': 1})
    texts = {(n['text'], n['to']) for n in r.json()['notices']}
    assert ('UNA PARA VERDE', -1) in texts and ('DOS PARA VERDE', -1) not in texts, 'the second call replaces the first banner'
    assert rc.flag == 'yellow', 'the green stays the director\'s'
    steward.post(f'{V}/servers/{sid}/race-control', json={'flag': 'green'})
    assert steward.post(url, json={'to_go': 1}).status_code == 409


def test_restart_leader_crossing_in_pit_lane_cannot_release_green():
    from app.live.field import Field

    field = Field()
    field.update(3, 0.50, 9)
    field.update(4, 0.48, 9)
    rc = live_rc.RaceControl({'session_type': 3})
    rc.configure_nascar(70)
    rc.set_pace_car(9, 120, 'g9')
    rc.set_flag('yellow', field=field)
    rc.restart_plan(field.snapshot())
    rc.go_armed = True
    now = rc.clock.time()
    rc.report_pit(9, True, now)
    rc.report_pit(3, True, now)
    assert rc.green_if_due(3, field) is None
    assert rc.flag == 'yellow'
    assert rc.go_early == 0 and rc.blocked_crossings == 0
    rc._pit_seen[3] = now - 60
    assert rc.green_if_due(3, field) is not None
    assert rc.flag == 'green'


def test_formation_competitor_crossing_in_pits_cannot_release_green():
    rc = live_rc.RaceControl({'session_type': 3})
    rc.formation = rc.pace_car_in = rc.pace_car_pitted = True
    rc.pace_car = 9
    rc.report_pit(3, True)
    assert not rc.formation_green(3, False)
    rc._pit_seen[3] -= 60
    assert rc.formation_green(3, False)



def test_formation_pace_car_crossing_in_pits_does_not_advance_formation():
    rc = live_rc.RaceControl({'session_type': 3})
    rc.pace_car = 9
    rc.formation = True
    rc.caution_laps = 0
    rc.report_pit(9, True)
    assert not rc.formation_lap(9)
    assert rc.caution_laps == 0 and not rc.pace_car_in


def test_caution_leader_crossing_in_pits_does_not_advance_without_pace_car():
    from app.live.field import Field

    field = Field()
    field.update(3, 0.50, 9)
    rc = live_rc.RaceControl({'session_type': 3})
    rc.configure_nascar(70)
    rc.set_flag('yellow', field=field)
    before = rc.caution_laps
    now = rc.clock.time()
    rc.report_pit(3, True, now)
    assert rc.caution_lap(3, {3: 80}, now, {3}) is None
    assert rc.caution_laps == before


def test_the_frozen_order_notice_says_behind_whom_and_follows_a_car_that_goes_into_the_pits(live):
    sid, client, _ = live
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    client.cars.update({i: {'driver_name': n} for i, n in ((3, 'Ana'), (4, 'Beto'), (5, 'Cleo'))})
    for car, spline in ((3, 0.50), (4, 0.48), (5, 0.46)):
        client.field.update(car, spline, 9)
        client.field.completed_laps[car] = 9
    rc.set_flag('yellow', field=client.field)
    inst = SimpleNamespace(server_id=sid, acsp=client)
    adapter.line_up_notices(inst, rc, dict(client.field.progress))
    said = lambda: {(n['text'], n['to']) for n in rc.snapshot()['notices']}
    assert ('ORDEN CONGELADO: P1 · LÍDER', 3) in said() and ('ORDEN CONGELADO: P2 · detrás de Ana', 4) in said() and ('ORDEN CONGELADO: P3 · detrás de Beto', 5) in said()
    rc.report_pit(4, True, now=rc.clock.time())   # Beto goes into the pits: Cleo is now behind Ana
    adapter.line_up_notices(inst, rc, dict(client.field.progress))
    assert ('ORDEN CONGELADO: P2 · detrás de Ana', 5) in said() and ('ORDEN CONGELADO: P3 · detrás de Cleo', 4) in said()
    assert ('ORDEN CONGELADO: P3 · detrás de Beto', 5) not in said(), 'the old text is replaced, not left beside the new one'
