import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from conftest import ADMIN
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text
from sqlmodel import Session

from app.db import engine
from app.live import racecontrol as rc
from app.main import app
from app.models import Server, Setting
from app.stewards import limits_config as store
from app.stewards.api import LimitsIn, get_track_limits, put_track_limits, report_limits
from app.stewards.limits_filter import TrackLimitsConfig as Config, count_cut

BASE = dict(wheels=3, speed=120, ms=1000, vo=100, vi=100, vp=100)


@pytest.mark.parametrize('cfg,change,reason', [
    (dict(wheels_out=4), dict(wheels=3), 'wheels_out'),
    (dict(min_speed_kmh=40), dict(vo=39.9), 'min_speed'),
    (dict(max_cut_s=3), dict(ms=3001), 'max_cut_s'),
    (dict(min_slow_ratio=0.9), dict(vi=89.9), 'min_slow_ratio'),
    (dict(post_cut_slowdown=True), dict(vp=89.9), 'post_cut_slowdown'),
    (dict(post_cut_slowdown=True, min_slow_ratio=0.8), dict(vp=79.9), 'post_cut_slowdown'),
])
def test_each_filter(cfg, change, reason):
    config = Config(**cfg)
    assert count_cut(config, {**BASE, **change}, 10, 'green') == (False, reason)
    good = {**BASE, 'wheels': 4}
    assert count_cut(config, good, 10, 'green')[0]


@pytest.mark.parametrize('cfg,change', [
    (dict(wheels_out=4), dict(wheels=4)),
    (dict(min_speed_kmh=40), dict(vo=40)),
    (dict(max_cut_s=3), dict(ms=3000)),
    (dict(min_slow_ratio=0.9), dict(vi=90)),
    (dict(post_cut_slowdown=True), dict(vp=90)),
    (dict(post_cut_slowdown=True, min_speed_kmh=0), dict(vo=0, vp=0)),
])
def test_inclusive_boundaries(cfg, change):
    assert count_cut(Config(**cfg), {**BASE, **change}, 10, 'green')[0]


def test_old_hub_and_default_regression():
    old = dict(wheels=3, speed=100, ms=1000)
    assert count_cut(Config(min_slow_ratio=1.5, post_cut_slowdown=True), old, 10, 'green')[0]
    assert count_cut(Config(min_speed_kmh=101), old, 10, 'green') == (False, 'min_speed')
    for flag in ['green', *rc.CAUTION]:
        assert count_cut(Config(), old, 10, flag)[0]
    assert not count_cut(Config(), {**BASE, 'ms': 600000, 'vi': 0, 'vp': 0}, 1, 'yellow')[0]


def test_caution_order_and_debounce():
    cfg = Config(wheels_out=4, between_s=3, during_caution=False)
    assert count_cut(cfg, BASE, 11, 'yellow', 10) == (False, 'wheels_out')
    for flag in rc.CAUTION:
        assert count_cut(cfg, {**BASE, 'wheels': 4}, 11, flag, 10) == (False, 'during_caution')
    assert count_cut(cfg, {**BASE, 'wheels': 4}, 12.999, 'green', 10) == (False, 'between_s')
    assert count_cut(cfg, {**BASE, 'wheels': 4}, 13, 'green', 10) == (True, 'counted')


def test_server_lap_source_consumes_pit_mark(monkeypatch):
    from app.live.acsp import ACSPClient
    client = ACSPClient(901); client.session = {'session_type': 3}
    state = rc.RaceControl(); state.session_ref = client.session
    monkeypatch.setattr(rc, '_states', {901: state})
    monkeypatch.setattr('app.supervisor.get', lambda sid: None)
    state._pit_lap.add(1)
    client._apply(dict(type='lap_completed', car_id=1, cuts=5, laptime_ms=90000, leaderboard=[], grip_level=None))
    assert 1 not in state._pit_lap and not state.cuts_server
    client._apply(dict(type='lap_completed', car_id=1, cuts=5, laptime_ms=90000, leaderboard=[], grip_level=None))
    assert state.cuts_server.get(1, 0) == 0
    rc.on_cuts(901, state.session_ref, 1, 1, True)
    assert state.cuts_client[1] == 1


@pytest.mark.parametrize("session_type", [1, 2, 3])
def test_filtered_report_keeps_evidence_and_judge_and_only_counts_accepted(monkeypatch, session_type):
    import app.stewards.api as api
    session = {'session_type': session_type}
    client = SimpleNamespace(cars={1: {'driver_name': 'Ana'}}, session=session)
    monkeypatch.setattr(api, '_get', lambda *a: None)
    monkeypatch.setattr(api.supervisor, 'live', lambda: [SimpleNamespace(server_id=902, acsp=client)])
    monkeypatch.setattr(store, 'load', lambda sid: Config(min_speed_kmh=40, between_s=3))
    state = rc.RaceControl(session); monkeypatch.setattr(rc, '_states', {902: state})
    record, cuts, judge, audit = Mock(return_value=True), Mock(), Mock(), Mock()
    monkeypatch.setattr(api.engine, 'record_limits', record)
    monkeypatch.setattr(rc, 'on_cuts', cuts); monkeypatch.setattr(rc, 'on_excursion', judge)
    monkeypatch.setattr(state, 'audit', audit)
    async def immediate(fn, *args): return fn(*args)
    monkeypatch.setattr(api.asyncio, 'to_thread', immediate)
    for now, vo in [(10, 30), (11, 100), (12, 100), (14, 100)]:
        monkeypatch.setattr(api.time, 'monotonic', lambda now=now: now)
        asyncio.run(report_limits(902, LimitsIn(car_id=1, name='Ana', **{**BASE, 'vo': vo}), None))
    assert record.call_count == judge.call_count == 4
    assert cuts.call_count == 2 and state._limits_counted == {1: 14}
    assert [c.kwargs['reason'] for c in audit.call_args_list] == ['min_speed', 'between_s']


@pytest.mark.parametrize('key,value', [('wheels_out', 2), ('source','server'), ('min_speed_kmh',301), ('max_cut_s',61), ('min_slow_ratio',2.1), ('between_s',61), ('between_s',-1), ('during_caution','false'), ('unknown',1), ('max_cut_s',float('nan'))])
def test_config_validation(key, value):
    with pytest.raises(ValidationError): Config(**{key: value})


@pytest.mark.parametrize('key', ['vo','vi','vp'])
def test_facts_range(key):
    for value in [-1,1000.1,float('inf'),float('nan')]:
        with pytest.raises(ValidationError): LimitsIn(car_id=1,name='Ana',ms=1000,wheels=3,speed=100,**{key:value})
    for value in [None,0,1000]:
        assert getattr(LimitsIn(car_id=1,name='Ana',ms=1000,wheels=3,speed=100,**{key:value}), key) == value


def test_persistence_and_invalidation(monkeypatch):
    monkeypatch.setattr(store, '_cache', {})
    with Session(engine) as sess:
        server = Server(name='filter persistence', base_port=26500); sess.add(server); sess.commit()
        sid = server.id
        assert get_track_limits(sid, sess) == Config()
        cfg = Config(between_s=4)
        put_track_limits(sid, cfg, sess)
        assert store.peek(sid) == cfg
        assert sess.get(Setting, f'track_limits:{sid}').value == cfg.model_dump()
        store._cache.clear()
        assert get_track_limits(sid, sess) == cfg
        with pytest.raises(HTTPException) as e: get_track_limits(999999, sess)
        assert e.value.status_code == 404
        with pytest.raises(HTTPException) as e: put_track_limits(999999, cfg, sess)
        assert e.value.status_code == 404


def test_old_concurrent_read_cannot_publish_after_put(monkeypatch):
    monkeypatch.setattr(store, '_cache', {})
    entered, release = threading.Event(), threading.Event()
    class OldSession:
        def __init__(self,*a): pass
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def get(self,*a):
            entered.set(); assert release.wait(3)
            return SimpleNamespace(value=Config().model_dump())
    monkeypatch.setattr(store, 'Session', OldSession)
    result = []
    worker = threading.Thread(target=lambda: result.append(store.load(903)))
    worker.start(); assert entered.wait(3)
    cfg = Config(between_s=4); store.invalidate(903, cfg); release.set(); worker.join(3)
    assert not worker.is_alive() and result == [cfg] and store.peek(903) == cfg


def test_endpoints_admin_validation_404_and_persistence():
    api = TestClient(app, headers=ADMIN)
    sid = api.post('/api/v1/servers',json={'name':'filter API'}).json()['id']
    url = f'/api/v1/servers/{sid}/track-limits'
    assert api.get(url).json() == Config().model_dump()
    assert api.put(url,json={'source':'hub'}).status_code == 422
    assert 'source' not in api.get(url).json()
    assert api.put(url,json={'min_speed_kmh':301}).status_code == 422
    assert api.get('/api/v1/servers/999999/track-limits').status_code == 404
    assert api.put('/api/v1/servers/999999/track-limits',json={}).status_code == 404
    for role in ['driver','reporter','steward']:
        from app.models import User
        from app.auth import current_user
        app.dependency_overrides[current_user] = lambda role=role: User(username='filters', role=role)
        try:
            assert api.get(url).status_code == 403
            assert api.put(url,json={}).status_code == 403
        finally: app.dependency_overrides.clear()


def test_evidence_preserves_optional_facts(monkeypatch):
    from app.stewards import engine as intake
    client = SimpleNamespace(server_id=904, session={}, cars={1: {'driver_name':'Ana'}},
                             board=SimpleNamespace(elapsed_ms=lambda now: 0))
    rows = []
    monkeypatch.setattr(intake, '_admit', lambda key, now, interval, build: rows.append(build()) or True)
    monkeypatch.setattr(intake, 'state_of', lambda *args: None)
    report = {**BASE, 'pos': [], 'lap':1, 'spline':0.5}
    assert intake.record_limits(client, 1, 'Ana', report)
    assert {k: rows[0].evidence[k] for k in ('vo','vi','vp')} == dict(vo=100,vi=100,vp=100)


@pytest.mark.parametrize('missing', [('vo',), ('vi',), ('vp',), ('vo','vi','vp')])
def test_partial_speed_facts_skip_only_dependent_filters(missing):
    report = {k:v for k,v in BASE.items() if k not in missing}
    if 'vo' in missing or 'vi' in missing:
        assert count_cut(Config(min_slow_ratio=2), report, 1, 'green')[0]
    assert count_cut(Config(post_cut_slowdown=True), report, 1, 'green')[0]
    assert count_cut(Config(max_cut_s=0.5), report, 1, 'green') == (False, 'max_cut_s')


def test_initial_speed_zero_is_not_replaced_by_peak():
    assert count_cut(Config(min_speed_kmh=40), {**BASE,'vo':0}, 1, 'green') == (False, 'min_speed')


def test_config_snapshot_is_immutable():
    cfg = Config()
    with pytest.raises(ValidationError): cfg.between_s = 5


def test_full_filter_order():
    cfg = Config(wheels_out=4, min_speed_kmh=40, max_cut_s=3, min_slow_ratio=0.9,
                 post_cut_slowdown=True, during_caution=False, between_s=3)
    report = dict(wheels=3, speed=100, vo=30, vi=10, vp=0, ms=4000)
    fixes = [('wheels_out', 'wheels', 4), ('min_speed', 'vo', 100),
             ('max_cut_s', 'ms', 1000), ('min_slow_ratio', 'vi', 100),
             ('post_cut_slowdown', 'vp', 100)]
    for reason, key, value in fixes:
        assert count_cut(cfg, report, 11, 'yellow', 10) == (False, reason)
        report[key] = value
    assert count_cut(cfg, report, 11, 'yellow', 10) == (False, 'during_caution')
    assert count_cut(cfg, report, 11, 'green', 10) == (False, 'between_s')
    assert count_cut(cfg, report, 13, 'green', 10) == (True, 'counted')


@pytest.mark.parametrize('session_type', [1, 2])
@pytest.mark.parametrize('speed', [30, 100])
@pytest.mark.parametrize('pace', [False, True])
def test_non_race_report_filters_and_creates_state(monkeypatch, session_type, speed, pace):
    import app.stewards.api as api
    session = {'session_type': session_type}
    client = SimpleNamespace(cars={1: {'driver_name': 'Ana'}}, session=session)
    monkeypatch.setattr(api, '_get', lambda *a: None)
    monkeypatch.setattr(api.supervisor, 'live', lambda: [SimpleNamespace(server_id=905, acsp=client)])
    monkeypatch.setattr(store, 'load', lambda sid: Config(min_speed_kmh=40))
    monkeypatch.setattr(rc, '_states', {})
    if pace:
        state = rc.state_of(905, True, session, create=True)
        state.pace_car = 1
    record, cuts, judge, audit = Mock(return_value=True), Mock(), Mock(), Mock()
    monkeypatch.setattr(api.engine, 'record_limits', record)
    monkeypatch.setattr(rc, 'on_cuts', cuts)
    monkeypatch.setattr(rc, 'on_excursion', judge)
    monkeypatch.setattr(rc.RaceControl, 'audit', audit)
    async def immediate(fn, *args): return fn(*args)
    monkeypatch.setattr(api.asyncio, 'to_thread', immediate)
    asyncio.run(report_limits(905, LimitsIn(car_id=1, name='Ana', **{**BASE, 'vo': speed}), None))
    state = rc.state_of(905, True, session)
    assert state is not None
    assert cuts.call_count == int(speed >= 40 and not pace)
    assert state._limits_counted.keys() == ({1} if speed >= 40 and not pace else set())
    if speed < 40:
        assert any(c.args == ("cut_filtered",) for c in audit.call_args_list)
    record.assert_called_once()
    judge.assert_called_once_with(905, session, 1, BASE['ms'], 0, 0.0)


@pytest.mark.parametrize('value', ['{broken json', {'between_s': -1}])
def test_invalid_stored_config_defaults_caches_reports_and_can_be_replaced(monkeypatch, caplog, value):
    import app.stewards.api as api
    monkeypatch.setattr(store, '_cache', {})
    monkeypatch.setattr(store, '_generation', {})
    monkeypatch.setattr(rc, '_states', {})
    with Session(engine) as sess:
        server = Server(name='invalid filter config', base_port=26510 + int(isinstance(value, dict)))
        sess.add(server); sess.commit()
        sid = server.id
        sess.add(Setting(key=f'track_limits:{sid}', value=value)); sess.commit()
        if isinstance(value, str):
            sess.execute(text('UPDATE settings SET value = :value WHERE key = :key'),
                         {'value': value, 'key': f'track_limits:{sid}'})
            sess.commit()
        with caplog.at_level('WARNING', logger=store.__name__):
            assert store.load(sid) == store.DEFAULT
        assert len(caplog.records) == 1
        assert caplog.records[0].exc_info
        assert str(value) not in caplog.text
        with monkeypatch.context() as cached:
            cached.setattr(store, 'Session', Mock(side_effect=AssertionError('cache miss')))
            assert store.load(sid) == store.DEFAULT
            assert get_track_limits(sid, sess) == store.DEFAULT
        session = {'session_type': rc.RACE_TYPE}
        client = SimpleNamespace(cars={1: {'driver_name': 'Ana'}}, session=session)
        monkeypatch.setattr(api.supervisor, 'live', lambda: [SimpleNamespace(server_id=sid, acsp=client)])
        record, cuts, judge = Mock(return_value=True), Mock(), Mock()
        monkeypatch.setattr(api.engine, 'record_limits', record)
        monkeypatch.setattr(rc, 'on_cuts', cuts)
        monkeypatch.setattr(rc, 'on_excursion', judge)
        monkeypatch.setattr(rc.RaceControl, 'audit', Mock())
        async def immediate(fn, *args): return fn(*args)
        monkeypatch.setattr(api.asyncio, 'to_thread', immediate)
        # Exercise the cold load on the report path as well.
        store._cache.clear()
        asyncio.run(report_limits(sid, LimitsIn(car_id=1, name='Ana', **BASE), sess))
        record.assert_called_once()
        cuts.assert_called_once()
        judge.assert_called_once()
        cfg = Config(between_s=4)
        assert put_track_limits(sid, cfg, sess) == cfg
        assert sess.get(Setting, f'track_limits:{sid}').value == cfg.model_dump()
        store._cache.clear()
        assert get_track_limits(sid, sess) == cfg


def test_recommended_defaults_and_retired_stored_keys(monkeypatch):
    assert Config().model_dump() == dict(wheels_out=3, min_speed_kmh=40, max_cut_s=3,
        min_slow_ratio=0.9, post_cut_slowdown=True, between_s=3, during_caution=True)
    monkeypatch.setattr(store, '_cache', {})
    with Session(engine) as sess:
        sess.add(Setting(key='track_limits:999991', value=dict(source='both', retired=True, between_s=7, min_speed_kmh=80)))
        sess.commit()
    assert store.load(999991) == Config(between_s=7, min_speed_kmh=80)
