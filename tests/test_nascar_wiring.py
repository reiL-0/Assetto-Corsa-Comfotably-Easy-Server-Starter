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
    def say(*args):
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

    def director_restarts(*args):
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
    assert before.keys() == {'rev', 'ts', 'running', 'flag', 'pace_kmh', 'penalties', 'vsc_id', 'grace_left',
                            'tolerance_kmh', 'vsc_penalty', 'notices', 'violations', 'exempt', 'nascar', 'caution', 'proposal', 'auto_yellow', 'nascar_next_delay_min'}
    response = api.post(url, json={'total': 70})
    assert response.status_code == 200
    armed = response.json()
    assert armed.keys() == before.keys()
    assert {k: v for k, v in armed.items() if k not in ('rev', 'ts', 'nascar')} == {k: v for k, v in before.items() if k not in ('rev', 'ts', 'nascar')}
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
    for t in (101.0, 104.0):
        adapter.nascar_actions(sid, rc, rc.caution_tick(dict(client.field.progress), {3: 60, 4: 60, 5: 60}, t))
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
    assert out == [('no_free_pass', 5)] and rc.caution.free_pass is None


def test_to_go_tells_the_wave_around_the_restart_place_and_who_may_choose(live):
    sid, client, _ = live
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    client.cars.update({i: {'driver_name': n} for i, n in ((3, 'Ana'), (4, 'Beto'), (5, 'Cleo'), (6, 'Dora'))})
    for car, spline, laps in ((3, 0.50, 9), (4, 0.30, 9), (5, 0.52, 7), (6, 0.20, 8)):
        client.field.update(car, spline, laps)   # 5 is two laps down and 0.02 ahead of the leader (the wave-around), 6 one lap down (the free pass)
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


def test_a_pileup_is_proposed_to_the_director_or_puts_the_yellow_out_by_itself(live):
    sid, client, _ = live
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.configure_nascar(70)
    rc.nascar.laps = 5
    _field(client, c3=(0.50, 5), c4=(0.48, 5), c5=(0.46, 5))
    speeds = {3: 40.0, 4: 40.0, 5: 40.0}
    for a, b in ((3, 4), (4, 5), (5, None)):
        rc.watch.on_contact(100.0, a, b, 90.0)
    prop, actions = rc.incident_step(speeds, client.field, 101.0)
    assert prop and prop['kind'] == 'pileup' and actions == [] and rc.flag == 'green' and rc.snapshot()['proposal'] == prop, 'only a proposal'
    assert _client('steward').post(f'{V}/servers/{sid}/race-control/auto-yellow', json={'enabled': True}).json()['auto_yellow'] is True
    rc.watch.proposal = None
    for a, b in ((3, 4), (4, 5), (5, None)):
        rc.watch.on_contact(200.0, a, b, 90.0)
    prop, actions = rc.incident_step(speeds, client.field, 201.0)
    assert prop and rc.flag == 'yellow' and rc.nascar.cautions_in_stage == 1, 'the engine counts the caution'
    assert rc.caution and rc.caution.involved == {3, 4, 5}, 'the cars of the pile-up are involved: no free pass for them'
    assert rc.snapshot()['proposal'] is None, 'the caution answers the proposal'


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
