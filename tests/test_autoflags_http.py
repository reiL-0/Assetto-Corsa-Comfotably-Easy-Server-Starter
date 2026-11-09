"""The automatic flags on a driver's screen over HTTP: his own slot only, 409 for somebody else's name, live only when the league says so, and the director's switch."""

import time

from app import racecontrol as adapter
from app.live import racecontrol as live_rc
from test_racecontrol import live, api, V, _client  # noqa: F401  (the fixture)


def _ready(live, mode="live"):
    sid, client, _ = live
    client.cars[4] = {"driver_name": "Beto", "driver_guid": "76561190000000002"}
    client.length.configured = 3000.0
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.flag_modes = {"blue": mode, "danger": "shadow"}
    now = time.monotonic()
    client.field.update(3, 0.30, 1, now=now - 5)   # Ana, 1000 m into lap 1
    client.field.update(4, 0.30, 2, now=now - 5)   # Beto a lap ahead and on the same spot: 0 m behind her
    for c in (3, 4):
        client.field.since[c] = now - 10
        client.field.stamp[c] = now
    return sid, client, rc, now


def test_only_the_asking_driver_gets_his_flag_and_a_wrong_name_is_refused(live, monkeypatch):
    monkeypatch.setattr(adapter, "flag_modes_of", lambda sid: {"blue": "live", "danger": "shadow"})   # (the league's rulebook, read from the database)
    sid, client, rc, now = _ready(live)
    from app.live.field import Sample
    rc.autoflags_tick({3: Sample(1.34, 1, 0, now, now - 10), 4: Sample(2.30, 1, 0, now, now - 10)}, {3: 150.0, 4: 150.0}, 3000.0, now, time.time())
    r = _client("reporter").get(f"{V}/servers/{sid}/race-state/auto", params={"slot": 3, "name": "ana"})
    assert r.status_code == 200 and r.json()["auto"]["blue"] == {"by": 4, "m": 120}, r.text
    assert _client("reporter").get(f"{V}/servers/{sid}/race-state/auto", params={"slot": 4, "name": "Beto"}).json()["auto"] is None, "the car in front has no flag"
    assert _client("reporter").get(f"{V}/servers/{sid}/race-state/auto", params={"slot": 3, "name": "Beto"}).status_code == 409, "somebody else's name"
    assert _client("reporter").get(f"{V}/servers/{sid}/race-state/auto", params={"slot": 9, "name": "Ana"}).status_code == 409, "an empty slot"


def test_shadow_gives_nothing_and_the_directors_switch_takes_a_live_flag_away(live, monkeypatch):
    league = {"blue": "shadow", "danger": "shadow"}
    monkeypatch.setattr(adapter, "flag_modes_of", lambda sid: league)
    sid, client, rc, now = _ready(live, mode="shadow")
    from app.live.field import Sample
    cars = {3: Sample(1.34, 1, 0, now, now - 10), 4: Sample(2.30, 1, 0, now, now - 10)}
    rc.autoflags_tick(cars, {3: 150.0, 4: 150.0}, 3000.0, now, time.time())
    ask = lambda: _client("reporter").get(f"{V}/servers/{sid}/race-state/auto", params={"slot": 3, "name": "Ana"}).json()["auto"]   # noqa: E731
    assert ask() is None, "shadow shows nothing"
    assert _client("reporter").get(f"{V}/servers/{sid}/race-state/auto", params={"slot": 3, "name": "Ana"}).json()["live"] == [], "nothing live: the hub asks rarely"
    rc.flag_modes = {"blue": "live", "danger": "shadow"}
    assert ask() is None, "the monitor says live but the league's reading just now says shadow: nothing"
    league["blue"] = "live"
    assert ask() and ask()["blue"]["by"] == 4
    assert _client("reporter").get(f"{V}/servers/{sid}/race-state/auto", params={"slot": 3, "name": "Ana"}).json()["live"] == ["blue"]
    r = _client("steward").post(f"{V}/servers/{sid}/race-control/autoflags", json={"kind": "blue", "off": True})
    assert r.status_code == 200 and r.json() == {"auto_off": ["blue"]}
    assert ask() is None, "off at once"
    assert _client("reporter").post(f"{V}/servers/{sid}/race-control/autoflags", json={"kind": "blue", "off": False}).status_code in (401, 403), "only a steward"


def test_the_director_changes_the_pace_rules_of_a_running_vsc_over_http(live):
    sid, client, _, now = _ready(live)
    url = f"{V}/servers/{sid}/race-control"
    assert _client("steward").post(f"{url}/vsc-config", json={"tolerance_kmh": 8}).status_code == 409, "no VSC running"
    r = _client("steward").post(url, json={"flag": "vsc", "pace_kmh": 60})
    assert r.status_code == 200 and r.json()["vsc_penalty"] == {"kind": "pits", "laps": 3}, "the default is the drive-through of 3 laps"
    r = _client("steward").post(f"{url}/vsc-config", json={"tolerance_kmh": 8, "penalty": {"kind": "pits", "laps": 5}})
    assert r.status_code == 200 and r.json() == {"tolerance_kmh": 8, "penalty": {"kind": "pits", "laps": 5}}
    assert _client("steward").post(f"{url}/vsc-config", json={"penalty": {"kind": "pits", "laps": 9}}).status_code == 422
    assert _client("reporter").post(f"{url}/vsc-config", json={"tolerance_kmh": 8}).status_code in (401, 403)
    assert _client("steward").post(f"{url}/autoflags", json={"kind": "pace", "off": True}).json() == {"auto_off": ["pace"]}, "the kill switch of the pace judge"
    assert _client("steward").get(f"{url}/autoflags").json()["auto_off"] == ["pace"], "the panel reads the switch back from the manager"


def test_a_vsc_can_ask_for_the_delta_mode_and_without_a_reference_lap_it_runs_the_fixed_pace(live):
    sid, client, rc, now = _ready(live)
    url = f"{V}/servers/{sid}/race-control"
    assert _client("steward").post(url, json={"flag": "vsc", "pace_kmh": 60, "mode": "f1"}).status_code == 422
    assert _client("steward").post(url, json={"flag": "vsc", "pace_kmh": 60, "mode": "delta"}).status_code == 200
    view = _client("steward").get(f"{url}/autoflags").json()
    assert view["vsc_mode"] == "delta" and view["vsc_fallback"] is True and view["ref_models"] == [], view
    texts = [n["text"] for n in _client("steward").get(f"{V}/servers/{sid}/race-state").json()["notices"]]
    assert any("VIRTUAL SAFETY CAR: reduce el ritmo" in t and "km/h" not in t for t in texts), texts   # a VSC is time per sector: the notice asks for no speed
    assert _client("steward").post(url, json={"flag": "green"}).status_code == 200
    assert _client("steward").post(url, json={"flag": "fcy", "pace_kmh": 60}).status_code == 200   # no mode: delta is the default, for an FCY too
    assert _client("steward").get(f"{url}/autoflags").json()["vsc_mode"] == "delta"
    texts = [n["text"] for n in _client("steward").get(f"{V}/servers/{sid}/race-state").json()["notices"]]
    assert any("FULL COURSE YELLOW: baja a 60 km/h" in t for t in texts), texts   # an FCY does say its pace
    assert _client("steward").post(url, json={"flag": "green"}).status_code == 200
