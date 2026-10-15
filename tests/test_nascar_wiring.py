"""NASCAR manager wiring, with isolated UDP, chat and clock."""
import asyncio
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
from tests.test_racecontrol import live, api, V, _client


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
