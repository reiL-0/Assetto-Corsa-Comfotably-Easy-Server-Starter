import asyncio
import secrets
from datetime import UTC, datetime
from itertools import pairwise

from conftest import ADMIN
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import metrics, supervisor
from app.auth import _sha
from app.config import settings
from app.db import engine
from app.live import acsp
from app.main import app
from app.models import Activity, Token, User

V = "/api/v1"
# a fixed "now" long before the other tests' events, so their rows never fall inside the window
NOW = datetime(2026, 1, 15, 18, 0, tzinfo=UTC).timestamp()
DAY = 86400


def rows(sid: int, kind: str | None = None) -> list[Activity]:
    with Session(engine) as s:
        q = select(Activity).where(Activity.server_id == sid)
        return list(s.exec(q.where(Activity.kind == kind) if kind else q))


def test_summary_numbers_per_day_with_the_local_timezone():
    L = lambda kind, ago_h, **kw: metrics.log(900, kind, ts=NOW - ago_h * 3600, **kw)
    for i, n in enumerate((0, 2, 3, 1)):  # today 18:00, 17:00, ... one sample a minute would be many; four are enough
        L("online", i, value=n)
    L("online", 30, value=5)  # yesterday
    L("lap", 1, guid="G1", name="Ana", car="bmw", track="spa", value=91000)
    L("lap", 2, guid="G1", name="Ana", car="bmw", track="spa", value=90000)
    L("lap", 3, guid="G2", name="Beto", car="audi", track="spa", value=92000)
    L("join", 3, guid="G3", name="Cris")
    L("session", 4, name="Race", track="spa", value=3)
    L("session", 6, name="Qualify", track="spa", value=2)
    L("session", 28, name="Practice", track="magione", value=1)
    L("server_crash", 2, value=3, name="up 600s")
    L("server_start", 1.9)
    L("import_error", 5, name="car")
    L("http_5xx", 6, name="/api/x", value=500)
    s = metrics.summary(days=3, tz_offset_min=0, now=NOW)
    today, yesterday = s["daily"][-1], s["daily"][-2]
    assert (today["peak"], today["player_minutes"], today["laps"], today["drivers"]) == (3, 6, 3, 3)
    assert (today["sessions"], today["crashes"], today["starts"], today["import_errors"], today["http_5xx"]) == (2, 1, 1, 1, 1)
    assert (yesterday["peak"], yesterday["sessions"]) == (5, 1)
    assert s["top_tracks"][0] == {"track": "spa", "sessions": 2} and s["top_cars"][0] == {"car": "bmw", "laps": 2}
    assert s["top_drivers"][0] == {"guid": "G1", "name": "Ana", "laps": 2}
    assert [i["kind"] for i in s["incidents"]][:3] == ["server_start", "server_crash", "import_error"]  # newest first
    # 03:00 UTC is "today" in UTC but 21:00 of the day before in Mexico City (UTC-6): the day boundary follows the viewer
    metrics.log(900, "online", value=9, ts=NOW - 15 * 3600)
    assert metrics.summary(days=3, tz_offset_min=0, now=NOW)["daily"][-1]["peak"] == 9
    mx = metrics.summary(days=3, tz_offset_min=-360, now=NOW)["daily"]
    assert mx[-1]["peak"] == 3 and mx[-2]["peak"] == 9


def test_online_series_adds_servers_and_fills_gaps():
    metrics.log(901, "online", value=2, ts=NOW - 100)
    metrics.log(902, "online", value=3, ts=NOW - 100)
    metrics.log(901, "online", value=1, ts=NOW - 100 - 600)
    series = metrics.summary(days=1, now=NOW, hours=1)["online"]
    assert series[-1]["v"] >= 5 or series[-2]["v"] >= 5  # the two servers add up inside one 10-min bucket
    assert all(b["t"] - a["t"] == 600 for a, b in pairwise(series))  # no holes


def test_acsp_events_are_recorded():
    c = acsp.ACSPClient(903)
    c._apply({"type": "new_session", "name": "Practice", "session_type": 1, "track": "spa", "server_name": "S", "track_config": "",
              "time_min": 10, "laps": 0, "ambient_temp": 20, "road_temp": 25, "elapsed_ms": 0, "weather": "", "wait_time": 0,
              "version": 4, "session_index": 0, "current_session_index": 0, "session_count": 1})
    c._apply({"type": "new_connection", "car_id": 0, "driver_name": "Ana", "driver_guid": "G1", "car_model": "bmw", "car_skin": "red"})
    c._apply({"type": "lap_completed", "car_id": 0, "laptime_ms": 91000, "cuts": 0, "grip_level": 1.0, "leaderboard": []})
    c._apply({"type": "connection_closed", "car_id": 0, "driver_name": "Ana", "driver_guid": "G1", "car_model": "bmw", "car_skin": "red"})
    assert [r.kind for r in rows(903)] == ["session", "join", "lap", "leave"]
    lap = rows(903, "lap")[0]
    assert (lap.guid, lap.name, lap.car, lap.track, lap.value) == ("G1", "Ana", "bmw", "spa", 91000)


def _fake(tmp_path, body: str):
    f = tmp_path / "acServer"
    f.write_text("#!/bin/sh\n" + body + "\n")
    f.chmod(0o755)
    return f


def test_supervisor_records_start_stop_crash_and_online_samples(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "acserver_cmd", str(_fake(tmp_path, "exit 3")))
        await supervisor.start(904, tmp_path)
        await asyncio.sleep(0.5)
        crash = rows(904, "server_crash")
        assert len(crash) == 1 and crash[0].value == 3  # it died by itself: a crash

        monkeypatch.setattr(settings, "acserver_cmd", str(_fake(tmp_path, "exec sleep 30")))
        monkeypatch.setattr(supervisor, "SAMPLE_EVERY", 0.05)
        inst = await supervisor.start(905, tmp_path)
        inst.acsp = acsp.ACSPClient(905)
        for car, name in ((0, "Ana"), (1, "Beto")):
            inst.acsp.board.apply({"type": "new_connection", "car_id": car, "driver_name": name, "driver_guid": f"G{car}",
                                   "car_model": "bmw", "car_skin": ""})
        await asyncio.sleep(0.3)
        assert {r.value for r in rows(905, "online")} == {2}
        await supervisor.stop(905)
        await asyncio.sleep(0.2)
        assert [r.name for r in rows(905, "server_stop")] == ["manual"]
        assert not rows(905, "server_crash")  # a requested stop is not a crash
        assert len(rows(905, "server_start")) == 1

    asyncio.run(scenario())


def test_idle_stop_is_recorded_with_its_reason(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "acserver_cmd", str(_fake(tmp_path, "exec sleep 30")))
        monkeypatch.setattr(settings, "idle_stop_seconds", 1)
        monkeypatch.setattr(supervisor, "IDLE_POLL", 0.1)
        inst = await supervisor.start(906, tmp_path)
        inst.acsp = acsp.ACSPClient(906)
        await asyncio.sleep(1.6)
        assert not inst.running and [r.name for r in rows(906, "server_stop")] == ["idle"]

    asyncio.run(scenario())


def test_server_errors_are_counted():
    from fastapi import HTTPException

    def boom():
        raise RuntimeError("x")

    def unavailable():
        raise HTTPException(503, "down")

    for path, fn in (("/_boom", boom), ("/_unavailable", unavailable)):
        app.add_api_route(path, fn)
        app.router.routes.insert(0, app.router.routes.pop())  # before the UI's catch-all, which would answer first

    before = len(rows(0, "http_5xx"))
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/_boom").status_code == 500 and client.get("/_unavailable").status_code == 503
    client.get("/_nothing_here")  # a 404 is not a server error
    new = rows(0, "http_5xx")[before:]
    assert sorted((r.name, r.value) for r in new) == [("/_boom", 500), ("/_unavailable", 503)]


def test_activity_api_roles_shape_and_purge():
    api = TestClient(app, headers=ADMIN)
    r = api.get(f"{V}/metrics/activity", params={"days": 7, "tz": -360})
    assert r.status_code == 200
    body = r.json()
    assert len(body["daily"]) == 7 and {"servers_running", "online"} <= set(body["now"]) and "incidents" in body
    assert api.get(f"{V}/metrics/activity", params={"days": 100000, "tz": 99999}).status_code == 200  # clamped, not an error
    raw = secrets.token_urlsafe(16)
    with Session(engine) as s:
        u = User(username=f"drv-{raw[:5]}", role="driver")
        s.add(u)
        s.commit()
        s.add(Token(user_id=u.id, token_hash=_sha(raw), name="t", created_at=datetime.now(UTC)))
        s.commit()
    assert TestClient(app, headers={"Authorization": f"Bearer {raw}"}).get(f"{V}/metrics/activity").status_code == 403
    assert TestClient(app).get(f"{V}/metrics/activity").status_code == 401
    metrics.log(907, "lap", ts=NOW - 200 * DAY)
    assert metrics.purge(now=NOW) >= 1 and not rows(907)


def test_now_endpoint_reports_the_running_servers(tmp_path):
    api = TestClient(app, headers=ADMIN)
    assert api.get(f"{V}/metrics/now").json()["servers_running"] >= 0

    async def scenario():
        sid = api.post(f"{V}/servers", json={"name": "En vivo"}).json()["id"]
        f = _fake(tmp_path, "exec sleep 30")
        settings.acserver_cmd = str(f)
        try:
            inst = await supervisor.start(sid, tmp_path)
            inst.acsp = acsp.ACSPClient(sid)
            inst.acsp.board.apply({"type": "new_session", "name": "Race", "track": "spa", "session_type": 3, "server_name": "S",
                                   "track_config": "", "time_min": 10, "laps": 0, "ambient_temp": 20, "road_temp": 25,
                                   "elapsed_ms": 0, "weather": "", "wait_time": 0, "version": 4, "session_index": 0,
                                   "current_session_index": 0, "session_count": 1})
            inst.acsp.board.apply({"type": "new_connection", "car_id": 0, "driver_name": "Ana", "driver_guid": "G1",
                                   "car_model": "bmw", "car_skin": ""})
            mine = next(s for s in api.get(f"{V}/metrics/now").json()["servers"] if s["id"] == sid)
            assert (mine["name"], mine["online"], mine["track"], mine["session"]) == ("En vivo", 1, "spa", "Race")
        finally:
            await supervisor.stop(sid)
            settings.acserver_cmd = ""

    asyncio.run(scenario())
    assert TestClient(app).get(f"{V}/metrics/now").status_code == 401



def test_lifecycle_events_are_posted_to_discord_and_the_rest_are_not(monkeypatch):
    from app import discord
    from app.config import settings

    sent = []
    monkeypatch.setattr(discord, "_send", sent.append)
    monkeypatch.setattr(discord.threading, "Thread", lambda target, args, daemon: type("T", (), {"start": lambda self: target(*args)})())
    metrics.log(5, "server_start")
    assert sent == [], "no webhook configured -> nothing is posted"

    monkeypatch.setattr(settings, "discord_status_webhook", "https://example.invalid/hook")
    metrics.log(5, "server_start")
    metrics.log(5, "server_stop", name="idle", value=3725)
    metrics.log(5, "server_crash", name="up 90s", value=139)
    metrics.log(5, "lap", name="x")
    assert sent == ["🟢 **Servidor #5** iniciado", "🔴 **Servidor #5** detenido por inactividad (sin pilotos) · estuvo 1 h 2 min en marcha",
                    "💥 **Servidor #5** se cayó (código 139, up 90s)"]

