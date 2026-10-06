from conftest import ADMIN
from fastapi.testclient import TestClient

from app import content
from app.main import app

V = "/api/v1"
api = TestClient(app, headers=ADMIN)
BASE = {"name": "Opts", "track": "optspa", "cars": ["optbmw"], "max_clients": 4, "race_laps": 3, "practice_min": 5}


def _setup():
    t, c = content._tracks_dir(), content._cars_dir()
    (t / "optspa" / "data").mkdir(parents=True, exist_ok=True)
    (t / "optspa" / "data" / "surfaces.ini").write_text("x")
    (c / "optbmw").mkdir(exist_ok=True)
    (c / "optbmw" / "data.acd").write_text("x")
    return api.post(f"{V}/servers", json={"name": "t"}).json()["id"]


def _apply(sid, **opts):
    return api.post(f"{V}/servers/{sid}/apply", json={**BASE, "restart": False, "options": opts})


def test_options_are_written_with_the_right_keys_and_values():
    sid = _setup()
    r = _apply(sid, sun_angle=-32, time_of_day_mult=5, abs_allowed=2, tc_allowed=0, stability_allowed=True,
               autoclutch_allowed=False, tyre_blankets_allowed=True, force_virtual_mirror=False, damage_multiplier=50,
               fuel_rate=150, tyre_wear_rate=200, allowed_tyres_out=-1, legal_tyres="SV;S;M", max_ballast_kg=120,
               start_rule=2, race_gas_penalty_disabled=True, max_contacts_per_km=3, race_over_time=240,
               result_screen_time=20, qualify_max_wait_perc=150, race_pit_window_start=10, race_pit_window_end=30,
               kick_quorum=70, voting_quorum=60, vote_duration=30, blacklist_mode=2, client_send_interval_hz=24)
    assert r.status_code == 200
    srv = r.json()["config"]["SERVER"]
    expected = {
        "SUN_ANGLE": -32, "TIME_OF_DAY_MULT": 5, "ABS_ALLOWED": 2, "TC_ALLOWED": 0, "STABILITY_ALLOWED": 1,
        "AUTOCLUTCH_ALLOWED": 0, "TYRE_BLANKETS_ALLOWED": 1, "FORCE_VIRTUAL_MIRROR": 0, "DAMAGE_MULTIPLIER": 50,
        "FUEL_RATE": 150, "TYRE_WEAR_RATE": 200, "ALLOWED_TYRES_OUT": -1, "LEGAL_TYRES": "SV;S;M", "MAX_BALLAST_KG": 120,
        "START_RULE": 2, "RACE_GAS_PENALTY_DISABLED": 1, "MAX_CONTACTS_PER_KM": 3, "RACE_OVER_TIME": 240,
        "RESULT_SCREEN_TIME": 20, "QUALIFY_MAX_WAIT_PERC": 150, "RACE_PIT_WINDOW_START": 10, "RACE_PIT_WINDOW_END": 30,
        "KICK_QUORUM": 70, "VOTING_QUORUM": 60, "VOTE_DURATION": 30, "BLACKLIST_MODE": 2, "CLIENT_SEND_INTERVAL_HZ": 24,
    }
    assert {k: srv[k] for k in expected} == expected
    ini = api.get(f"{V}/servers/{sid}/server_cfg.ini").text
    assert "SUN_ANGLE=-32" in ini and "LEGAL_TYRES=SV;S;M" in ini and "BLACKLIST_MODE=2" in ini


def test_options_left_out_keep_what_the_server_has():
    sid = _setup()
    _apply(sid, abs_allowed=2, fuel_rate=150)
    again = _apply(sid, tc_allowed=1).json()["config"]["SERVER"]  # a later apply that doesn't mention abs / fuel
    assert again["ABS_ALLOWED"] == 2 and again["FUEL_RATE"] == 150 and again["TC_ALLOWED"] == 1


def test_weather_blocks_are_replaced_and_default_is_sane():
    sid = _setup()
    first = _apply(sid).json()["config"]
    assert first["WEATHER_0"]["BASE_TEMPERATURE_ROAD"] == 6  # relative to the ambient, not 42 °C asphalt
    two = [{"graphics": "3_clear", "ambient": 22, "road": 8, "wind_min": 5, "wind_max": 15, "wind_direction": 90},
           {"graphics": "7_heavy_clouds", "ambient": 15, "road": 2, "wind_min": 9, "wind_max": 3}]  # max below min
    cfg = _apply(sid, weather=two).json()["config"]
    assert cfg["WEATHER_0"]["BASE_TEMPERATURE_AMBIENT"] == 22 and cfg["WEATHER_0"]["WIND_BASE_DIRECTION"] == 90
    assert cfg["WEATHER_1"]["GRAPHICS"] == "7_heavy_clouds" and cfg["WEATHER_1"]["WIND_BASE_SPEED_MAX"] == 9  # raised to min
    one = _apply(sid, weather=two[:1]).json()["config"]
    assert "WEATHER_0" in one and "WEATHER_1" not in one  # no leftover block
    kept = _apply(sid, abs_allowed=1).json()["config"]  # weather not mentioned: untouched
    assert kept["WEATHER_0"]["BASE_TEMPERATURE_AMBIENT"] == 22


def test_dynamic_track_section():
    sid = _setup()
    cfg = _apply(sid, dynamic_track={"session_start": 80, "randomness": 4, "session_transfer": 70, "lap_gain": 60}).json()["config"]
    assert cfg["DYNAMIC_TRACK"] == {"SESSION_START": 80, "RANDOMNESS": 4, "SESSION_TRANSFER": 70, "LAP_GAIN": 60}
    assert "[DYNAMIC_TRACK]" in api.get(f"{V}/servers/{sid}/server_cfg.ini").text


def test_options_are_range_checked():
    sid = _setup()
    for bad in ({"abs_allowed": 3}, {"sun_angle": 81}, {"sun_angle": -81}, {"damage_multiplier": 101}, {"allowed_tyres_out": 5},
                {"legal_tyres": "SV;../x"}, {"blacklist_mode": 3}, {"client_send_interval_hz": 5}, {"vote_duration": 0},
                {"weather": [{"graphics": "bad name!"}]}, {"weather": [{"ambient": 99}]}, {"weather": [{}] * 11},
                {"dynamic_track": {"session_start": 101}}):
        assert _apply(sid, **bad).status_code == 422, bad


def test_saved_event_keeps_the_options():
    sid = _setup()
    body = {"title": "Lluvia", "session": {**BASE, "options": {"abs_allowed": 2, "weather": [{"graphics": "7_heavy_clouds"}]}}}
    eid = api.post(f"{V}/events", json=body).json()["id"]
    got = api.get(f"{V}/events/{eid}").json()["session"]["options"]
    assert got["abs_allowed"] == 2 and got["weather"][0]["graphics"] == "7_heavy_clouds" and got["fuel_rate"] is None
    r = api.post(f"{V}/events/{eid}/run", json={"server_id": sid, "restart": False})
    assert r.status_code == 200 and r.json()["config"]["WEATHER_0"]["GRAPHICS"] == "7_heavy_clouds"


def test_csp_extra_options_hide_in_the_welcome_message():
    from conftest import ADMIN
    from fastapi.testclient import TestClient
    from pathlib import Path
    from app import csp
    from app.config import settings
    from app.main import app
    api = TestClient(app, headers=ADMIN)
    ini = "[SCRIPT_1]\nSCRIPT = https://example.test/probe.lua\nREQUIRED = 0\n"
    assert csp.decode(csp.welcome_with_extra("Hola", ini)) == ini and csp.welcome_with_extra("Hola", "  ") == "Hola"
    msg = csp.welcome_with_extra("Hola", ini)
    assert msg.startswith("Hola" + "\t" * 32 + "$CSP0:") and "=" not in msg.split("$CSP0:")[1]   # padding trimmed, as CSP expects
    sid = api.post("/api/v1/servers", json={"name": "Csp"}).json()["id"]
    assert api.put(f"/api/v1/servers/{sid}/csp_extra", json={"text": ini}).json()["csp_extra"] == ini.strip()
    assert "WELCOME_MESSAGE=cfg/welcome.txt" in api.get(f"/api/v1/servers/{sid}/server_cfg.ini").text   # no plain welcome, but extra options need the file
    from app.db import engine
    from app.models import Server
    from app.servers import _write_instance
    from sqlmodel import Session
    with Session(engine) as s:
        d = _write_instance(s.get(Server, sid))
    assert csp.decode((d / "cfg" / "welcome.txt").read_text()) == ini.strip() + "\n"
    assert api.put(f"/api/v1/servers/{sid}/csp_extra", json={"text": ""}).json()["csp_extra"] == ""
