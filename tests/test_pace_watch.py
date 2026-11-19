"""The safety net of the pace car: stuck or silent while it leads a caution = sent to the pits by itself (RaceControl.pace_watch)."""
from app.live import racecontrol as live_rc


def _leading():
    rc = live_rc.RaceControl({"session_type": 3})
    rc.pace_car, rc.pace_out = 9, True
    return rc


def test_a_pace_car_still_leaving_the_pits_gets_longer():
    rc = _leading()
    assert rc.pace_watch({}, {9: 0.0}, 0.0) is None
    assert rc.pace_watch({}, {9: 0.0}, live_rc.PACE_STUCK_S + 5) is None, "it has not moved yet: it is leaving"
    assert rc.pace_watch({}, {9: 0.0}, live_rc.PACE_LEAVE_S) == "stuck", "but not forever"


def test_stuck_pace_car_goes_to_the_pits_after_the_grace():
    rc = _leading()
    assert rc.pace_watch({}, {9: 90.0}, 0.0) is None   # (it has been leading: now it stops)
    assert rc.pace_watch({}, {9: 2.0}, 1.0) is None
    assert rc.pace_watch({}, {9: 2.0}, live_rc.PACE_STUCK_S - 1) is None, "not before the grace"
    assert rc.pace_watch({}, {9: 80.0}, live_rc.PACE_STUCK_S) is None and rc._pace_bad is None, "moving again clears it"
    assert rc.pace_watch({}, {9: 2.0}, 100.0) is None
    assert rc.pace_watch({}, {9: 2.0}, 100.0 + live_rc.PACE_STUCK_S) == "stuck"
    assert rc.pace_park and rc.pace_car_state()["mode"] == "in" and not rc.pace_car_in, "it goes back, no restart armed"
    assert rc.pace_watch({}, {9: 2.0}, 500.0) is None, "once is enough"


def test_silent_pace_car_goes_to_the_pits_too():
    rc = _leading()
    assert rc.pace_watch({}, {}, 0.0) is None
    assert rc.pace_watch({}, {}, live_rc.PACE_SILENT_S) == "silent" and rc.pace_park


def test_it_only_watches_a_pace_car_that_leads():
    rc = live_rc.RaceControl({"session_type": 3})
    assert rc.pace_watch({}, {}, 0.0) is None and rc.pace_watch({}, {}, 999.0) is None, "no pace car"
    rc.pace_car = 9
    assert rc.pace_watch({}, {9: 0.0}, 0.0) is None and rc.pace_watch({}, {9: 0.0}, 999.0) is None, "not out (waiting in the pits)"
    rc.pace_out, rc.pace_car_in = True, True
    assert rc.pace_watch({}, {9: 0.0}, 0.0) is None and rc.pace_watch({}, {9: 0.0}, 999.0) is None, "already coming in"
    rc.pace_car_in = False
    rc.pace_car = 8
    assert rc.pace_watch({}, {8: 0.0}, 0.0) is None
    rc.pace_car = 7   # another pace car: the clock starts again
    assert rc.pace_watch({}, {7: 0.0}, live_rc.PACE_STUCK_S + 1) is None


def test_pace_source_decides_the_pace_car_a_session_names(monkeypatch):
    from app.config import settings
    from app.services import server_service as ss
    monkeypatch.setattr(settings, "pace_bot_steam_id", "76561198000000099")
    s = {"pace_steam_id": "76561198000000001"}
    assert ss.pace_guid_of(s) == "76561198000000001", "an old session without the setting is real"
    assert ss.pace_guid_of({**s, "pace_source": "real"}) == "76561198000000001"
    assert ss.pace_guid_of({**s, "pace_source": "bot"}) == "76561198000000099"
    assert ss.pace_guid_of({**s, "pace_source": "off"}) is None
    monkeypatch.setattr(settings, "pace_bot_steam_id", "")
    assert ss.pace_guid_of({**s, "pace_source": "bot"}) is None, "a bot nobody configured names nobody"
    assert ss.pace_guid_of(None) is None


def test_the_adapter_gives_the_watch_the_speed_of_a_non_racing_pace_car(monkeypatch):
    """The pace car is a non-racing guid: the speeds of the cars that race leave it out, and the watch must still see it (or it would be 'silent' and sent to the pits while it leads)."""
    import time
    from types import SimpleNamespace
    from app import racecontrol as monitor
    from app.live.acsp import ACSPClient
    guid = "76561190000000009"
    client = ACSPClient(843)
    client.session = {"session_type": 3}
    client.cars[2] = {"driver_guid": guid, "velocity": [30, 0, 0]}   # 108 km/h, fresh
    client.field.update(2, 0.2)
    rc = live_rc.RaceControl(client.session)
    seen = []
    monkeypatch.setattr(rc, "pace_watch", lambda samples, speeds, mono: seen.append(dict(speeds)))
    for name in ("autoflags_tick", "pass_tick", "pace_tick"):
        monkeypatch.setattr(rc, name, lambda *a: [])
    monkeypatch.setattr(monitor.supervisor, "live", lambda: [SimpleNamespace(server_id=843, acsp=client)])
    monkeypatch.setattr(monitor, "state_of", lambda *a, **k: rc)
    monkeypatch.setattr(monitor, "_non_racing_guids", lambda now: frozenset({guid}))
    monkeypatch.setattr(monitor, "flag_modes_peek", lambda sid: {"blue": "shadow", "danger": "shadow"})
    monitor.autoflags_tick(time.time())
    assert seen and 2 in seen[0] and seen[0][2] > 100, seen
