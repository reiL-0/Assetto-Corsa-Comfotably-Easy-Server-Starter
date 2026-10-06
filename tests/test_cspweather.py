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


def test_director_broadcasts_the_plan_and_greets_a_car_that_finished_loading(monkeypatch):
    sent = []

    class Inst:
        running = True
        acsp = type("C", (), {"send": staticmethod(sent.append), "on_client_loaded": None})()
    monkeypatch.setattr(cspweather, "PERIOD", 0.01)

    async def scenario():
        d = cspweather.WeatherDirector(Inst, {"steps": [[0, 15], [0.05, 7]], "transition_s": 1}, {"SERVER": {"SUN_ANGLE": 16, "TIME_OF_DAY_MULT": 10}})
        await asyncio.sleep(0.3)
        Inst.acsp.on_client_loaded(3)     # the director hooked itself into the ACSP client
        Inst.running = False
        await asyncio.sleep(0.05)
        d.stop()
    asyncio.run(scenario())
    chats = [m for m in sent if m[0] == acsp.BROADCAST_CHAT]
    greets = [m for m in sent if m[0] == acsp.SEND_CHAT]
    assert len(chats) >= 3 and len(greets) == 1 and greets[0][1] == 3                           # the late car gets a command addressed to it
    assert greets[0][2:] in [m[1:] for m in chats]                                              # ...the same text the plan last broadcast


def test_weather_plan_endpoints_validate_and_store():
    sid = api.post(f"{V}/servers", json={"name": "Wx plan"}).json()["id"]
    bad = [{"steps": []}, {"steps": [[10, 15], [5, 7]]}, {"steps": [[0, 99]]}]
    assert all(api.put(f"{V}/servers/{sid}/weather_plan", json=b).status_code == 422 for b in bad)
    plan = {"steps": [[0, 15], [120, 7], [420, 15]], "transition_s": 40, "loop_s": 600, "ambient": 20}
    assert api.put(f"{V}/servers/{sid}/weather_plan", json=plan).json()["weather_plan"] == plan
    assert api.post(f"{V}/servers/{sid}/csp_weather", json={"current": 7, "rain": 0.6}).status_code == 409   # not running: nothing to send to
    assert supervisor.get(sid) is None
    assert api.delete(f"{V}/servers/{sid}/weather_plan").status_code == 204 and api.get(f"{V}/servers/{sid}").json()["weather_plan"] is None
