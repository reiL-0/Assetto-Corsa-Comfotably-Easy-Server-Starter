"""`SessionIn.rolling`: the start style of the race, per session (a NASCAR race is always rolling; any other asks for it)."""
import asyncio

import pytest
from pydantic import ValidationError
from sqlmodel import Session

from app.db import engine
from app.live import racecontrol as live_rc
from app.models import Server
from app.schemas.servers import SessionIn
from app.services import server_service
from test_nascar_wiring import SESSION, session_events  # noqa: F401  (fixture)

PLAIN = {'name': 'Race', 'track': 'monza', 'cars': ['ks_gt3'], 'race_laps': 5, 'restart': False}


def _apply(sid, **extra):
    with Session(engine) as sess:
        asyncio.run(server_service.apply(sess, sess.get(Server, sid), SessionIn(**{**PLAIN, **extra})))


def test_a_nascar_race_is_always_rolling():
    assert SessionIn(**{**SESSION, 'nascar': {'total': 70}}).rolling is None
    assert SessionIn(**{**SESSION, 'nascar': {'total': 70}, 'rolling': 'nascar'}).rolling == 'nascar'
    for bad in ('off', 'f1'):
        with pytest.raises(ValidationError):
            SessionIn(**{**SESSION, 'nascar': {'total': 70}, 'rolling': bad})
    assert live_rc.rolling_mode({'nascar': {'total': 70}, 'rolling': None}) == 'nascar'
    assert live_rc.rolling_mode({}) == 'off' and live_rc.rolling_mode({'rolling': 'f1'}) == 'f1' and live_rc.rolling_mode(None) == 'off'


def test_the_applied_style_is_loaded_and_survives_a_reload(session_events):
    sid, _, _ = session_events
    from app import supervisor
    inst = supervisor.get(sid)
    _apply(sid, rolling='f1')
    assert inst.rolling == 'f1' and live_rc.FORMATION_MODE[sid] == 'f1'
    live_rc.FORMATION_MODE.clear(); inst.rolling = 'off'   # (a manager restart forgets the memory: adopt / start load it back from Server.session)
    with Session(engine) as sess:
        live_rc.load_rolling(sid, inst, sess.get(Server, sid).session)
    assert inst.rolling == 'f1' and live_rc.FORMATION_MODE[sid] == 'f1'
    live_rc.FORMATION_MODE[sid] = 'nascar-by-director'
    _apply(sid, rolling='f1')   # (the autosave again: the same value must not undo what the director did)
    assert live_rc.FORMATION_MODE[sid] == 'nascar-by-director'
    _apply(sid, rolling='off')
    assert inst.rolling == 'off' and sid not in live_rc.FORMATION_MODE
    live_rc.FORMATION_MODE.pop(sid, None)


@pytest.mark.parametrize('rolling, formation', [('nascar', True), ('f1', True), ('off', False), (None, False)])
def test_a_plain_race_starts_rolling_only_when_asked(session_events, rolling, formation):
    sid, client, _ = session_events
    client.cars[3] = {'driver_name': 'A'}
    client.cars[9] = {'driver_name': 'Safety Car', 'driver_guid': '76561198000000009'}
    _apply(sid, rolling=rolling, pace_steam_id='76561198000000009')
    live_rc.state_of(sid, True, client.session, create=True).set_pace_car(9, 120, '76561198000000009')   # (pace_car_check named him during the practice; the new session's state inherits him)
    client._apply({'type': 'new_session', 'session_index': 2, 'session_type': 3})
    rc = live_rc.state_of(sid, True, client.session)
    assert (rc is not None and rc.formation) == formation, 'rolling=%r' % rolling
    if formation:
        assert rc.nascar is None and rc.flag == 'yellow' and (rc._f1 is not None) == (rolling == 'f1')
    live_rc.FORMATION_MODE.pop(sid, None)


def test_practice_never_starts_a_rolling_start(session_events):
    sid, client, _ = session_events
    _apply(sid, rolling='nascar')
    client._apply({'type': 'new_session', 'session_index': 0, 'session_type': 1})
    assert live_rc.state_of(sid, True, client.session) is None


@pytest.mark.parametrize('adopt', [False, True])
def test_start_and_adopt_know_the_style_before_the_first_udp_event(session_events, tmp_path, monkeypatch, adopt):
    from types import SimpleNamespace
    from app import supervisor, timeline
    from app.config import settings
    sid, client, _ = session_events
    _apply(sid, rolling='f1', cuts_off=['practice'])
    live_rc.FORMATION_MODE.clear()
    supervisor._instances.pop(sid)
    monkeypatch.setattr(settings, 'data_dir', str(tmp_path))
    monkeypatch.setattr(settings, 'acserver_cmd', 'fake-acserver')
    monkeypatch.setattr(timeline, 'start_resume', lambda *args: None)
    cwd = tmp_path / 'instances' / str(sid)
    cwd.mkdir(parents=True)
    monkeypatch.setattr(server_service, 'write_instance', lambda s: cwd)
    proc = SimpleNamespace(pid=12345, returncode=None)
    seen = []

    async def spawn(*args, **kwargs):
        return proc

    async def connect(inst, *args, **kwargs):   # (acServer's UDP opens here: the first new_session can come right now)
        seen.append((inst.rolling, live_rc.FORMATION_MODE.get(sid), inst.cuts_off))

    monkeypatch.setattr(supervisor.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(supervisor.Instance, 'connect_acsp', connect)
    monkeypatch.setattr(supervisor, '_alive', lambda pid: proc.returncode is None)
    monkeypatch.setattr(supervisor, '_is_our_acserver', lambda *args: True)
    if adopt:
        (cwd / 'server.pid').write_text('{"pid": 12345}')

    async def scenario():
        try:
            with Session(engine) as sess:
                if adopt:
                    assert await server_service.adopt_running(sess) == 1
                else:
                    assert (await server_service.start(sess, sid))['running']
        finally:
            proc.returncode = 0
            await supervisor.stop(sid)

    asyncio.run(scenario())
    assert seen == [('f1', 'f1', ('practice',))], seen   # (rolling and cuts_off: what the first new_session needs)
    live_rc.FORMATION_MODE.pop(sid, None)
