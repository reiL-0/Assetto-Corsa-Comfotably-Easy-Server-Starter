import asyncio

from conftest import ADMIN
from fastapi.testclient import TestClient

from app import supervisor
from app.live import acsp, cspcmd, cspweather
from app.main import app

api = TestClient(app, headers=ADMIN)
V = "/api/v1"


def test_weather_command_round_trip_and_size():
    text = cspcmd.weather_set_v2(timestamp=1791250000, current=7, upcoming=15, transition=0.5, time_to_apply=5, ambient=14, road=12, grip=0.8, humidity=0.9,
                                 wind_deg=90, wind_kmh=12, pressure=1010, rain=0.6, wetness=0.4, water=0.25)
    assert text.startswith("\t\t\t\t$CSP0:") and "=" not in text and len(text) < 255        # fits a chat message, no base64 padding
    kind, payload = cspcmd.deserialize(text)
    assert kind == 1001 and len(payload) == 32
    d = cspcmd.parse_weather_set_v2(payload)
    assert (d["timestamp"], d["current"], d["upcoming"]) == (1791250000, 7, 15) and abs(d["transition"] - 0.5) < 1e-4
    assert (d["ambient"], d["road"], d["water"], d["pressure"]) == (14.0, 12.0, 0.25, 1010.0)
    assert abs(d["rain"] - 0.6) < 1e-3 and abs(d["wetness"] - 0.4) < 1e-3                       # Half precision
    assert abs(d["grip"] - 0.8) < 0.003 and abs(d["humidity"] - 0.9) < 0.003                 # grip over 0.6..1.0 in one byte
    assert cspcmd.deserialize(cspcmd.handshake_in(3898, True)) == (0, b"\x3a\x0f\x00\x00\x01")


def _board(session_type, elapsed_ms):
    class B:
        session = {"session_type": session_type}

        @staticmethod
        def elapsed_ms():
            return elapsed_ms
    return B


def test_director_plays_the_entries_of_the_running_session_and_greets_late_cars(monkeypatch):
    sent = []

    class Inst:
        running = True
        server_id = 1
        acsp = type("C", (), {"send": staticmethod(sent.append), "on_client_loaded": None, "board": _board(3, 0)})()
    monkeypatch.setattr(cspweather, "KEEPALIVE", 0.0)   # (the keepalive would otherwise hide that a plan with nothing new is not repeated: see below)
    plan = {"mode": "entries", "transition_s": 1, "update_s": 0.01, "entries": [
        {"type": 15, "duration_min": 0.05, "sessions": ["race"], "ambient": 24}, {"type": 7, "sessions": ["race"], "ambient": 16, "wind_max": 10},
        {"type": 8, "sessions": ["qualify"]}]}

    async def scenario():
        d = cspweather.WeatherDirector(Inst, plan, {"SERVER": {"SUN_ANGLE": 16, "TIME_OF_DAY_MULT": 10}})
        await asyncio.sleep(0.2)
        Inst.acsp.on_client_loaded(3)     # the director hooked itself into the ACSP client
        Inst.acsp.board = _board(1, 0)    # practice has no entry: nothing is sent, the server's own weather applies
        n = len(sent)
        await asyncio.sleep(0.1)
        assert len(sent) <= n + 1
        Inst.running = False
        await asyncio.sleep(0.05)
        d.stop()
    asyncio.run(scenario())
    chats = [m for m in sent if m[0] == acsp.BROADCAST_CHAT]
    greets = [m for m in sent if m[0] == acsp.SEND_CHAT]
    assert len(chats) >= 3 and len(greets) == 1 and greets[0][1] == 3 and greets[0][2:] in [m[1:] for m in chats]


def test_director_does_not_repeat_an_unchanged_weather(monkeypatch):
    sent = []

    class Inst:
        running = True
        server_id = 1
        acsp = type("C", (), {"send": staticmethod(sent.append), "on_client_loaded": None, "board": _board(3, 0)})()
    monkeypatch.setattr(cspweather, "KEEPALIVE", 3600.0)

    async def scenario():
        d = cspweather.WeatherDirector(Inst, {"mode": "entries", "update_s": 0.01, "entries": [{"type": 15, "sessions": ["race"]}]}, {})
        await asyncio.sleep(0.3)
        Inst.running = False
        await asyncio.sleep(0.05)
        d.stop()
    asyncio.run(scenario())
    assert len([m for m in sent if m[0] == acsp.BROADCAST_CHAT]) == 1       # many ticks, one broadcast: nothing changed


def test_timeline_blends_entries_and_live_types_map_from_wmo_codes():
    from random import Random
    from app.live.weatherplan import Entry, Timeline, Weather, live_type
    tl = Timeline([Entry(type=15, duration_min=10), Entry(type=7, duration_min=5), Entry(type=15)], 60, Random(1))
    assert tl.at(0) == (0, 0, 0.0) and tl.at(570) == (0, 1, 0.5) and tl.at(620) == (1, 1, 0.0) and tl.at(870) == (1, 2, 0.5) and tl.at(900) == (2, 2, 0.0) and tl.at(5000) == (2, 2, 0.0)
    assert (live_type(0, 5), live_type(2, 50), live_type(3, 95), live_type(61, 100), live_type(65, 100), live_type(95, 100), live_type(45, 100)) == (15, 17, 19, 6, 8, 1, 20)
    w = Weather({"mode": "live", "transition_s": 10, "live": {"lat": 1, "lon": 2}})
    assert w.step(1.0) is None                                                      # no data yet
    w.live = {"type": 15, "ambient": 20, "wind_kmh": 5, "wind_deg": 90, "humidity": 0.4, "pressure": 1010}
    assert w.step(1.0)["current"] == 15
    w.live = {**w.live, "type": 7}
    s = w.step(5.0)
    assert (s["current"], s["upcoming"]) == (15, 7) and 0.4 < s["transition"] < 0.6 and s["wind_kmh"] == 5 and s["pressure"] == 1010
    for _ in range(3):
        s = w.step(5.0)
    assert (s["current"], s["transition"]) == (7, 0.0) and s["rain"] > 0.5 and s["wetness"] > 0           # blended in, raining, the track wets


def test_weather_plan_endpoints_validate_and_store():
    sid = api.post(f"{V}/servers", json={"name": "Wx plan"}).json()["id"]
    bad = [{"mode": "entries"}, {"mode": "live"}, {"entries": [{"type": 99}]}, {"entries": [{"sessions": ["warmup"]}]}, {"mode": "live", "live": {"lat": 99, "lon": 0}}]
    assert all(api.put(f"{V}/servers/{sid}/weather_plan", json=b).status_code == 422 for b in bad)
    assert api.put(f"{V}/servers/{sid}/weather_plan", json={"entries": [{"type": 15}] * 9}).status_code == 422          # few changes
    assert api.put(f"{V}/servers/{sid}/weather_plan", json={"entries": [{"type": 15}], "transition_s": 5}).status_code == 422
    plan = {"mode": "entries", "transition_s": 40, "entries": [{"type": 15, "duration_min": 5, "sessions": ["race"], "ambient": 26, "road": 11},
                                                               {"type": 7, "sessions": ["race"], "wind_max": 12}]}
    stored = api.put(f"{V}/servers/{sid}/weather_plan", json=plan).json()["weather_plan"]
    assert stored["transition_s"] == 40 and stored["entries"][0]["road"] == 11 and stored["entries"][1]["duration_min"] == 0 and stored["live"] is None
    live = {"mode": "live", "live": {"lat": 47.22, "lon": 14.76, "refresh_min": 5}}
    assert api.put(f"{V}/servers/{sid}/weather_plan", json=live).json()["weather_plan"]["live"]["refresh_min"] == 5
    assert api.post(f"{V}/servers/{sid}/csp_weather", json={"current": 7, "rain": 0.6}).status_code == 409   # not running: nothing to send to
    assert api.delete(f"{V}/servers/{sid}/weather_plan").status_code == 204 and api.get(f"{V}/servers/{sid}").json()["weather_plan"] is None


def test_plan_sun_angle_overrides_the_server_one_and_is_validated():
    class Inst:
        running = False
    async def clock(plan):
        d = cspweather.WeatherDirector(Inst, plan, {"SERVER": {"SUN_ANGLE": 0}})
        d.stop()
        return d.t0
    base = {"mode": "entries", "entries": [{"type": 15}]}
    t0, t1 = asyncio.run(clock(base)), asyncio.run(clock({**base, "sun_angle": 16}))
    assert (t1 - t0).total_seconds() == 3600                      # 16 degrees = one hour later than the server's angle 0
    sid = api.post(f"{V}/servers", json={"name": "Wx sun"}).json()["id"]
    assert api.put(f"{V}/servers/{sid}/weather_plan", json={**base, "sun_angle": 99}).status_code == 422
    assert api.put(f"{V}/servers/{sid}/weather_plan", json={**base, "sun_angle": -32}).json()["weather_plan"]["sun_angle"] == -32


def test_visual_driving_keeps_the_rain_but_not_the_grip_loss_or_the_water():
    def run(driving):
        w = cspweather.Weather({"mode": "entries", "driving": driving, "entries": [{"type": 8, "sessions": ["race"]}]}) if hasattr(cspweather, "Weather") else None
        w.start_session(3)
        for _ in range(40):
            s = w.step(30, 0)
        return s
    real, vis = run("real"), run("visual")
    assert real["grip"] < 1 and real["water"] > 0
    assert vis["grip"] == 1.0 and vis["water"] == 0.0 and vis["rain"] > 0 and vis["wetness"] > 0     # still raining and wet to the eye
    sid = api.post(f"{V}/servers", json={"name": "Wx visual"}).json()["id"]
    assert api.put(f"{V}/servers/{sid}/weather_plan", json={"entries": [{"type": 7}], "driving": "visual"}).json()["weather_plan"]["driving"] == "visual"
    assert api.put(f"{V}/servers/{sid}/weather_plan", json={"entries": [{"type": 7}], "driving": "x"}).status_code == 422
