import asyncio
import re
import struct
import threading
import time
from types import SimpleNamespace

import pytest
from conftest import ADMIN
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import identity, metrics, supervisor
from app.config import settings
from app.db import engine
from app.live import acsm, acsp
from app.main import app
from app.models import Server, Setting

GUID = '76561198000000001'
OTHER = '76561198000000002'
V = '/api/v1'
api = TestClient(app, headers=ADMIN)


@pytest.fixture
def state(monkeypatch):
    service = identity.IdentityService()
    monkeypatch.setattr(identity, 'service', service)
    monkeypatch.setattr(settings, 'site_url', 'https://opr.test/')
    with Session(engine) as sess:
        rows = list(sess.exec(select(Setting).where(Setting.key.startswith('identity.'))))
        saved = [(row.key, row.value) for row in rows]
        for row in rows:
            sess.delete(row)
        sess.commit()
    service.load()
    service.protected = {GUID}
    service.modes[991] = 'enforce'
    service.modes[992] = 'enforce'
    emitted = []
    monkeypatch.setattr(metrics, 'enqueue', lambda *a, **kw: emitted.append((a, kw)))
    yield service, emitted
    for client in list(service.clients):
        client.close()
    with Session(engine) as sess:
        for row in sess.exec(select(Setting).where(Setting.key.startswith('identity.'))):
            sess.delete(row)
        for key, value in saved:
            sess.add(Setting(key=key, value=value))
        sess.commit()


def client(sid=991):
    c = acsp.ACSPClient(sid)
    sent = []
    c.transport = SimpleNamespace(sendto=sent.append, close=lambda: None)
    return c, sent


def join(c, car_id=0, guid=GUID, loaded=True):
    e = {'type': 'new_connection', 'car_id': car_id, 'driver_guid': guid,
             'driver_name': 'Pilot', 'car_model': 'car', 'car_skin': 'skin'}
    c._apply(e)
    if loaded:
        c._apply({'type': 'client_loaded', 'car_id': car_id})
    return e


def code(sent):
    assert sent[-1][:2] == bytes([acsp.SEND_CHAT, 0])
    text, _ = acsp._read_string(sent[-1], 2)
    assert 'https://opr.test/verificar' in text
    return re.search(r'código: ([ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{6})', text).group(1)


def verify(value, guid=GUID):
    return api.post(V + '/identity/verify', json={'guid': guid, 'code': value})


def test_challenge_verified_once_cancels_timer_and_preserves_weather_callback(state):
    async def run():
        c, sent = client()
        seen = []
        c.on_client_loaded = seen.append
        join(c, loaded=False)
        assert sent == []
        c._apply({'type': 'client_loaded', 'car_id': 0})
        value = code(sent)
        record = state[0].clients[c][0]
        assert record.timer is not None
        assert (await asyncio.to_thread(verify, value.lower())).status_code == 204
        assert record.timer.cancelled()
        message = ''.join(chr(struct.unpack_from('<I', sent[-1], i)[0]) for i in range(3, len(sent[-1]), 4))
        assert message == 'OPR: identidad verificada ✓'
        assert state[0].status(991)['verified'] == [0]
        assert seen == [0]
        assert (await asyncio.to_thread(verify, value)).status_code == 404
        state[0].expire(c, 0, record.generation, GUID)
        assert not any(p[0] == acsp.KICK_USER for p in sent)
    asyncio.run(run())


def test_wrong_attempts_cancel_and_kick(state):
    c, sent = client()
    join(c)
    value = code(sent)
    wrong = 'AAAAAA' if value != 'AAAAAA' else 'BBBBBB'
    for left in (2, 1):
        response = verify(wrong)
        assert response.status_code == 403
        assert str(left) in response.json()['detail']
    assert verify(wrong).status_code == 429
    assert sent[-1] == acsp.encode_kick_user(0)
    assert state[0].status(991)['pending'] == []
    assert verify(value).status_code == 404
    assert state[1][-1][0][1] == 'identity_kick'


@pytest.mark.parametrize('mode,kick', [('warn', False), ('enforce', True)])
def test_expiry(state, mode, kick):
    state[0].modes[991] = mode
    c, sent = client()
    join(c)
    r = state[0].clients[c][0]
    state[0].expire(c, 0, r.generation, GUID)
    assert any(p[0] == acsp.KICK_USER for p in sent) == kick
    assert state[1][-1][0][1] == ('identity_kick' if kick else 'identity_unverified')
    assert state[0].status(991)['pending'] == []


def test_reused_slot_disconnect_and_close(state):
    c, sent = client()
    e = join(c)
    r = state[0].clients[c][0]
    join(c, guid=OTHER)
    state[0].expire(c, 0, r.generation, GUID)
    assert not any(p[0] == acsp.KICK_USER for p in sent)
    join(c)
    value = code(sent)
    c._apply({**e, 'type': 'connection_closed'})
    assert verify(value).status_code == 404
    join(c)
    assert verify(code(sent)).status_code == 204
    c.close()
    assert state[0].status(991)['verified'] == []


def test_duplicate_guid_independent_across_slots_and_servers(state):
    c, sent = client()
    d, other_sent = client(992)
    join(c)
    first = code(sent)
    join(c, 1)
    second, _ = acsp._read_string(sent[-1], 2)
    second = re.search(r'código: (\w{6})', second).group(1)
    join(d)
    third = code(other_sent)
    assert len({first, second, third}) == 3
    assert verify(second).status_code == 204
    assert state[0].status(991)['verified'] == [1]
    assert len(state[0].status(991)['pending']) == 1
    assert len(state[0].status(992)['pending']) == 1
    assert verify(second).status_code == 404
    assert verify(third).status_code == 204
    assert verify(first).status_code == 204


@pytest.mark.parametrize('mode,guid', [('off', GUID), ('enforce', OTHER)])
def test_off_and_unprotected_unchanged(state, mode, guid):
    state[0].modes[991] = mode
    c, sent = client()
    join(c, guid=guid)
    assert sent == []
    assert c.snapshot()[0]['driver_guid'] == guid
    assert c.add_telemetry(0, {'gas': .5})
    assert state[0].status(991) == {'mode': mode, 'pending': [], 'verified': []}


def test_pending_hidden_from_snapshot_leaderboard_and_telemetry(state, monkeypatch):
    c, sent = client()
    join(c)
    monkeypatch.setattr(supervisor, 'get', lambda _: SimpleNamespace(running=True, acsp=c))
    assert c.snapshot() == {}
    assert acsm.leaderboard(991)['ConnectedDrivers'] == []
    assert not c.add_telemetry(0, {'gas': .5})
    assert not c.telemetry
    assert verify(code(sent)).status_code == 204
    assert 0 in c.snapshot()
    assert acsm.leaderboard(991)['ConnectedDrivers'][0]['CarInfo']['CarID'] == 0
    assert c.add_telemetry(0, {'gas': .5})


def test_warn_visible_and_empty_url_no_challenge(state, monkeypatch, caplog):
    state[0].modes[991] = 'warn'
    c, _sent = client()
    join(c)
    assert 0 in c.snapshot()
    assert c.add_telemetry(0, {'gas': .5})
    monkeypatch.setattr(settings, 'site_url', '')
    d, packets = client(992)
    join(d)
    assert packets == []
    assert not state[0].status(992)['pending']
    assert 'ACM_SITE_URL' in caplog.text


def test_recovered_car_gets_full_challenge(state):
    c, sent = client()
    c._apply({'type': 'car_info', 'car_id': 0, 'is_connected': True, 'driver_guid': GUID,
                  'driver_name': 'Recovered', 'car_model': 'car', 'car_skin': 'skin'})
    assert code(sent)
    assert 179 <= state[0].status(991)['pending'][0]['expires_in'] <= 180
    c._apply({'type': 'car_info', 'car_id': 0, 'is_connected': True, 'driver_guid': GUID,
                  'driver_name': 'Recovered', 'car_model': 'car', 'car_skin': 'skin'})
    assert len(sent) == 1


def test_snapshot_version_persistence_modes_and_validation(state):
    assert api.put(V + '/identity/protected', json={'version': 8, 'guids': [GUID]}).json() == {'applied': True}
    assert api.put(V + '/identity/protected', json={'version': 7, 'guids': [OTHER]}).json() == {'applied': False}
    assert api.put(V + '/identity/protected', json={'version': 8, 'guids': [GUID]}).json() == {'applied': True}
    assert api.get(V + '/identity/protected').json() == {'version': 8, 'count': 1}
    sid = api.post(V + '/servers', json={'name': 'Identity'}).json()['id']
    assert api.get(f'{V}/servers/{sid}/identity').json()['mode'] == 'off'
    assert api.put(f'{V}/servers/{sid}/identity', json={'mode': 'warn'}).status_code == 200
    fresh = identity.IdentityService()
    fresh.load()
    assert fresh.protected == {GUID} and fresh.version == 8
    assert fresh.status(sid)['mode'] == 'warn'
    for body in ({'guid': 'bad', 'code': 'ABC234'}, {'guid': GUID, 'code': 'abc'}, {'guid': GUID, 'code': 'ABC!23'}):
        assert api.post(V + '/identity/verify', json=body).status_code == 422
    for body in ({'version': 1, 'guids': ['bad']}, {'version': 1, 'guids': [GUID] * 5001}, {'version': True, 'guids': []}):
        assert api.put(V + '/identity/protected', json=body).status_code == 422
    assert api.put(f'{V}/servers/{sid}/identity', json={'mode': 'require'}).status_code == 422
    assert api.get(V + '/servers/999999/identity').status_code == 404


def test_reporter_permissions(state):
    assert api.post(V + '/users', json={'username': 'identity-reporter', 'password': 'hunter2hunter2', 'role': 'reporter'}).status_code == 201
    rep = TestClient(app)
    assert rep.post(V + '/auth/login', json={'username': 'identity-reporter', 'password': 'hunter2hunter2'}).status_code == 200
    assert rep.put(V + '/identity/protected', json={'version': 1, 'guids': [GUID]}).status_code == 200
    c, sent = client()
    join(c)
    assert rep.post(V + '/identity/verify', json={'guid': GUID, 'code': code(sent)}).status_code == 204
    for path in ('/identity/protected', '/servers/991/identity', '/servers', '/bans', '/championships'):
        assert rep.get(V + path).status_code == 403
    assert rep.put(V + '/servers/991/identity', json={'mode': 'off'}).status_code == 403
    assert TestClient(app).post(V + '/identity/verify', json={'guid': GUID, 'code': 'ABC234'}).status_code == 401


def test_global_limits_guid_and_origin(state):
    for _ in range(5):
        assert verify('ABC234').status_code == 404
    assert verify('ABC234').status_code == 429
    state[0].attempts.clear()
    for i in range(30):
        assert verify('ABC234', f'7656119800000{i:04d}').status_code == 404
    assert verify('ABC234', OTHER).status_code == 429


def test_same_guid_slot_generation_and_guid_checked_before_kick(state):
    c, sent = client()
    join(c)
    old = state[0].clients[c][0]
    join(c)
    current = state[0].clients[c][0]
    assert current.generation != old.generation
    state[0].expire(c, 0, old.generation, GUID)
    assert not any(p[0] == acsp.KICK_USER for p in sent)
    c.cars[0]['driver_guid'] = OTHER
    state[0].expire(c, 0, current.generation, GUID)
    assert not any(p[0] == acsp.KICK_USER for p in sent)
    assert state[0].status(991)['pending'] == []


def test_expired_code_cannot_verify_even_before_timer_callback(state):
    c, sent = client()
    join(c)
    value = code(sent)
    state[0].clients[c][0].expires = 0
    assert verify(value).status_code == 404
    assert sent[-1] == acsp.encode_kick_user(0)
    assert not state[0].status(991)['verified']


def test_real_timer_callback_with_short_test_ttl(state, monkeypatch):
    monkeypatch.setattr(identity, 'TTL', .01)

    async def run():
        c, sent = client()
        join(c)
        await asyncio.sleep(.03)
        assert sent[-1] == acsp.encode_kick_user(0)
        assert not state[0].status(991)['pending']
    asyncio.run(run())


def test_disconnect_and_transport_loss_cancel_timer(state):
    async def run():
        c, sent = client()
        e = join(c)
        r = state[0].clients[c][0]
        c._apply({**e, 'type': 'connection_closed'})
        assert r.timer.cancelled()
        assert not state[0].status(991)['pending']
        join(c)
        r = state[0].clients[c][0]
        c.connection_lost(None)
        assert r.timer.cancelled()
        assert not state[0].status(991)['pending']
        assert not any(p[0] == acsp.KICK_USER for p in sent)
    asyncio.run(run())


def test_protection_removed_and_mode_off_cancel_state(state):
    c, sent = client()
    join(c)
    value = code(sent)
    assert api.put(V + '/identity/protected', json={'version': 1, 'guids': []}).status_code == 200
    assert verify(value).status_code == 404
    assert 0 in c.snapshot()
    assert api.put(V + '/identity/protected', json={'version': 2, 'guids': [GUID]}).status_code == 200
    join(c)
    value = code(sent)
    sid = api.post(V + '/servers', json={'name': 'Identity switch'}).json()['id']
    c.server_id = sid
    state[0].modes[sid] = 'enforce'
    assert api.put(f'{V}/servers/{sid}/identity', json={'mode': 'off'}).status_code == 200
    assert verify(value).status_code == 404
    assert 0 in c.snapshot()


def test_concurrent_verify_consumes_once(state):
    from concurrent.futures import ThreadPoolExecutor

    c, sent = client()
    join(c)
    value = code(sent)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: verify(value).status_code, range(2)))
    assert sorted(results) == [204, 404]
    assert state[0].status(991)['verified'] == [0]
    assert sum('identidad verificada' in acsp._read_string(p, 2)[0] for p in sent) == 1


def test_concurrent_versions_cannot_replace_newer_snapshot(state):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda version: api.put(V + '/identity/protected', json={'version': version, 'guids': [GUID] if version == 20 else [OTHER]}).status_code, (20, 19)))
    assert results == [200, 200]
    assert state[0].version == 20 and state[0].protected == {GUID}
    fresh = identity.IdentityService()
    fresh.load()
    assert fresh.version == 20 and fresh.protected == {GUID}


def test_unprotected_disconnected_driver_keeps_leaderboard_entry(state):
    c, _sent = client()
    e = join(c, guid=OTHER)
    c._apply({**e, 'type': 'connection_closed'})
    join(c)
    assert c.leaderboard()['ConnectedDrivers'] == []
    assert c.leaderboard()['DisconnectedDrivers'][0]['CarInfo']['DriverGUID'] == OTHER


def test_session_database_wait_does_not_block_identity_status(state, monkeypatch):
    import threading
    import time
    from app import timeline

    c, _sent = client()
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def blocked_session(*args):
        entered.set()
        release.wait(2)

    def apply_session():
        try:
            c._apply({'type': 'new_session', 'name': 'Practice', 'session_type': 1,
                      'session_index': 0, 'track': 'test'})
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(timeline, 'on_session_event', blocked_session)
    worker = threading.Thread(target=apply_session)
    worker.start()
    try:
        assert entered.wait(1)
        started = time.monotonic()
        assert state[0].status(1)['mode'] == 'off'
        assert time.monotonic() - started < 1
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert not errors


def test_recovered_loading_car_resends_same_pending_challenge(state):
    state[0].modes[991] = 'warn'

    async def run():
        c, sent = client()
        c._apply({'type': 'car_info', 'car_id': 0, 'is_connected': True,
                  'driver_guid': GUID, 'driver_name': 'Recovered',
                  'car_model': 'car', 'car_skin': 'skin'})
        value = code(sent)
        record = state[0].clients[c][0]
        original = (record.expires, record.attempts, record.timer, record.generation)
        assert len(sent) == 1
        c._apply({'type': 'client_loaded', 'car_id': 0})
        assert len(sent) == 2
        assert code(sent) == value
        assert sent[0] == sent[1]
        assert state[0].clients[c][0] is record
        assert (record.expires, record.attempts, record.timer, record.generation) == original
        assert len(state[0].status(991)['pending']) == 1
    asyncio.run(run())


def test_mode_change_restarts_failed_challenge_and_enforces_expiry(state):
    with Session(engine) as sess:
        server = Server(name='Identity retry', base_port=11999)
        sess.add(server)
        sess.commit()
        sess.refresh(server)
        sid = server.id
    state[0].modes[sid] = 'warn'
    c, sent = client(sid)
    join(c)
    old_code = code(sent)
    old = state[0].clients[c][0]
    state[0].expire(c, 0, old.generation, GUID)
    assert old.state == 'failed'
    assert not any(p[0] == acsp.KICK_USER for p in sent)
    with Session(engine) as sess:
        response = identity.server_put(sid, identity.ModeIn(mode='enforce'), sess)
    assert len(response['pending']) == 1
    new = state[0].clients[c][0]
    assert new.state == 'pending' and new.generation != old.generation
    assert code(sent) != old_code
    assert new.attempts == 3
    assert 179 <= response['pending'][0]['expires_in'] <= 180
    state[0].expire(c, 0, new.generation, GUID)
    assert sent[-1] == acsp.encode_kick_user(0)


def test_a_challenge_started_from_a_worker_thread_still_expires_on_the_acsp_loop(state, monkeypatch):
    """PUT /servers/{id}/identity is a plain `def` route, so it runs in a worker thread with NO running loop. A challenge started there used to get no expiry timer: in enforce
    the car stayed hidden for ever and was never kicked. The client remembers its loop and the timer is armed there."""
    service, _ = state
    monkeypatch.setattr(identity, 'TTL', 0.2)
    c, sent = client()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    c.loop = loop
    try:
        c.cars[0] = {'car_id': 0, 'driver_guid': GUID, 'driver_name': 'Pilot'}
        service.start(c, 0)                                                   # this thread has no running loop, like the HTTP worker
        assert sent and sent[-1][0] == acsp.SEND_CHAT, 'the code reaches the car'
        deadline = time.time() + 3
        while time.time() < deadline and not any(p[0] == acsp.KICK_USER for p in sent):
            time.sleep(0.02)
        assert any(p[0] == acsp.KICK_USER for p in sent), 'the challenge never expired: no timer was armed'
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(2)
