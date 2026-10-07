import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from conftest import ADMIN
from fastapi.testclient import TestClient

from app import supervisor
from app.live import cspweather, tracktime
from app.main import app

api = TestClient(app, headers=ADMIN)
V = "/api/v1"
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def test_geotags_in_degrees_minutes_seconds_and_decimal():
    assert abs(tracktime.parse_geo("37°51′04″S") + 37.851111) < 1e-5 and abs(tracktime.parse_geo("144°58′12″E") - 144.97) < 1e-4
    assert tracktime.parse_geo("48.0061°N") == 48.0061 and tracktime.parse_geo("-6.5") == -6.5 and tracktime.parse_geo("0.1996°W") == -0.1996
    assert tracktime.parse_geo("north") is None


def test_the_offset_comes_from_the_plans_zone_or_from_the_tracks_geotags(tmp_path, monkeypatch):
    assert tracktime.offset_seconds(tmp_path, "t", "", "Australia/Melbourne", NOW) == 39600     # AEDT in October
    assert tracktime.offset_seconds(tmp_path, "t", "", "Europe/Rome", NOW) == 7200
    assert tracktime.offset_seconds(tmp_path, "t", "", "Not/AZone", NOW) == 0
    assert tracktime.offset_seconds(tmp_path, "nogeo", "", None, NOW) == 0                      # no geotags: UTC, as before
    (tmp_path / "mel" / "ui").mkdir(parents=True)
    (tmp_path / "mel" / "ui" / "ui_track.json").write_text(json.dumps({"geotags": ["37°51′04″S", "144°58′12″E"]}))
    calls = []

    class Reply:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, *a): return json.dumps({"utc_offset_seconds": 39600}).encode()
    monkeypatch.setattr(tracktime.urllib.request, "urlopen", lambda url, timeout=0: calls.append(url) or Reply())
    assert tracktime.offset_seconds(tmp_path, "mel", "", None, NOW) == 39600 and tracktime.offset_seconds(tmp_path, "mel", "", None, NOW) == 39600
    assert len(calls) == 1 and "latitude=-37.85" in calls[0]                                    # asked once, then cached

    def down(url, timeout=0): raise OSError("no network")
    monkeypatch.setattr(tracktime.urllib.request, "urlopen", down)
    (tmp_path / "other" / "ui").mkdir(parents=True)
    (tmp_path / "other" / "ui" / "ui_track.json").write_text(json.dumps({"geotags": ["10.0°N", "20.0°E"]}))
    assert tracktime.offset_seconds(tmp_path, "other", "", None, NOW) == 0                      # the service is down: UTC, nothing breaks


class _Inst:
    running, server_id = True, 1
    acsp = type("C", (), {"send": staticmethod(lambda m: None), "on_client_loaded": None,
                          "board": type("B", (), {"session": {"session_type": 3}, "elapsed_ms": staticmethod(lambda: 0)})()})()


def _first_timestamp(monkeypatch, offset: int) -> datetime:
    seen = []
    real = cspweather.command
    monkeypatch.setattr(cspweather, "command", lambda state, unix, period=30.0: seen.append(unix) or real(state, unix, period))
    monkeypatch.setattr(tracktime, "offset_seconds", lambda *a, **k: offset)

    async def go():
        d = cspweather.WeatherDirector(_Inst, {"mode": "entries", "update_s": 0.01, "sun_angle": 0, "entries": [{"type": 15, "sessions": ["race"]}]},
                                       {"SERVER": {"TRACK": "rj_melbourne_2019", "CONFIG_TRACK": "layout_f1_2025"}})
        await asyncio.sleep(0.3)
        _Inst.running = False
        await asyncio.sleep(0.05)
        d.stop()
        _Inst.running = True
    asyncio.run(go())
    return datetime.fromtimestamp(seen[0], UTC)


def test_the_director_sends_the_wanted_local_time_minus_the_tracks_offset(monkeypatch):
    # angle 0 = 13:00 local. Clients read the timestamp in the track's zone, so on a UTC+11 track it must be sent as 02:00 UTC
    assert _first_timestamp(monkeypatch, 39600).hour == 2
    assert _first_timestamp(monkeypatch, 0).hour == 13                                          # unknown zone: as it always was


def test_saving_a_weather_plan_on_a_running_server_does_not_crash_and_restarts_the_director(monkeypatch):
    """Regression: the sync endpoint ran in a worker thread, where creating the director's task raised 'no current event loop' (HTTP 500, director lost)."""
    sid = api.post(f"{V}/servers", json={"name": "Live plan"}).json()["id"]
    holder = {}

    class Running:
        def set_weather_plan(self, plan, cfg):
            if plan:   # (a cleared plan stops the director)
                holder["d"] = cspweather.WeatherDirector(_Inst, plan, cfg)   # what supervisor.Instance does

    monkeypatch.setattr(supervisor, "get", lambda i: Running())
    r = api.put(f"{V}/servers/{sid}/weather_plan", json={"entries": [{"type": 7}], "timezone": "Australia/Melbourne"})
    assert r.status_code == 200 and r.json()["weather_plan"]["timezone"] == "Australia/Melbourne" and "d" in holder
    assert api.delete(f"{V}/servers/{sid}/weather_plan").status_code == 204
    assert api.put(f"{V}/servers/{sid}/weather_plan", json={"entries": [{"type": 7}], "timezone": "Mars/Olympus"}).status_code == 422
