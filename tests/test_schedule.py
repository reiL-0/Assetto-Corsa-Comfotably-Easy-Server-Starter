import asyncio
import time

from conftest import ADMIN
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import discord, schedule
from app.services import server_service
from app.services.server_service import ServerError
from app.db import engine
from app.main import app
from app.models import Schedule

client = TestClient(app, headers=ADMIN)


def _setup(start_in: float, reminders=(60, 10), duration=None):
    sid = client.post("/api/v1/servers", json={"name": "SchedServer"}).json()["id"]
    eid = client.post("/api/v1/events", json={"title": "Endurance R5", "session": {"name": "x", "track": "spa", "cars": ["bmw"]}}).json()["id"]
    r = client.post("/api/v1/schedules", json={"event_id": eid, "server_id": sid, "start_at": time.time() + start_in,
                                               "reminders": list(reminders), "duration_min": duration})
    assert r.status_code == 201, r.text
    return sid, r.json()


def _tick(now, monkeypatch, applied=None):
    said = []
    monkeypatch.setattr(discord, "announce", said.append)

    async def fake_apply(sess, srv, body, **kw):
        if isinstance(applied, Exception):
            raise applied
        (applied if applied is not None else []).append((srv.id, body.restart))
    monkeypatch.setattr(server_service, "apply", fake_apply)
    asyncio.run(schedule.tick(now))
    return said


def _row(sc_id):
    with Session(engine) as s:
        return s.get(Schedule, sc_id)


def test_validation():
    sid, sc = _setup(3600)
    eid = sc["event_id"]
    assert client.post("/api/v1/schedules", json={"event_id": eid, "server_id": sid, "start_at": time.time() - 5}).status_code == 422
    assert client.post("/api/v1/schedules", json={"event_id": eid, "server_id": 9999, "start_at": time.time() + 9}).status_code == 404
    assert client.post("/api/v1/schedules", json={"event_id": eid, "server_id": sid, "start_at": time.time() + 9, "reminders": [0]}).status_code == 422
    assert any(s["id"] == sc["id"] for s in client.get("/api/v1/schedules").json())
    assert client.delete(f"/api/v1/schedules/{sc['id']}").status_code == 204


def test_reminders_then_start(monkeypatch):
    sid, sc = _setup(7200)
    t0 = sc["start_at"]
    started = []
    assert _tick(t0 - 7000, monkeypatch, started) == []                      # too early: nothing
    said = _tick(t0 - 3500, monkeypatch, started)                             # inside the 60 min mark
    assert len(said) == 1 and "Endurance R5" in said[0] and f"<t:{int(t0)}:R>" in said[0]
    assert _tick(t0 - 3400, monkeypatch, started) == []                      # not repeated
    assert len(_tick(t0 - 500, monkeypatch, started)) == 1                    # the 10 min mark
    assert started == [] and _row(sc["id"]).sent == [60, 10]
    said = _tick(t0 + 1, monkeypatch, started)
    assert started == [(sid, True)] and _row(sc["id"]).state == "done" and "en marcha" in said[-1]
    assert _tick(t0 + 30, monkeypatch, started) == [] and len(started) == 1  # done ones are left alone


def test_late_manager_posts_only_the_nearest_reminder_and_old_starts_are_missed(monkeypatch):
    _, sc = _setup(7200)
    t0 = sc["start_at"]
    assert len(_tick(t0 - 300, monkeypatch, [])) == 1 and _row(sc["id"]).sent == [60, 10]   # both due at once -> one post
    started = []
    _tick(t0 + schedule.LATE + 5, monkeypatch, started)
    r = _row(sc["id"])
    assert started == [] and r.state == "missed" and r.result


def test_failed_start_is_reported(monkeypatch):
    _, sc = _setup(100, reminders=())
    said = _tick(sc["start_at"] + 1, monkeypatch, ServerError(409, "track not installed"))
    r = _row(sc["id"])
    assert r.state == "failed" and "track not installed" in r.result and "no pudo iniciarse" in said[-1]


class _FakeAcsp:
    def __init__(self):
        self.sent = []
        self.cars = {}

    def send(self, data):
        self.sent.append(data)


class _FakeInstance:
    def __init__(self):
        self.running, self.acsp, self.stopped_for = True, _FakeAcsp(), None

    async def stop(self, reason="manual"):
        self.running, self.stopped_for = False, reason


def test_a_scheduled_event_with_a_duration_runs_then_stops_the_server(monkeypatch):
    sid, sc = _setup(100, reminders=(), duration=60)
    t0, inst = sc["start_at"], _FakeInstance()
    monkeypatch.setitem(schedule.supervisor._instances, sid, inst)   # the real registry: the stop goes through supervisor.stop
    started = []
    said = _tick(t0 + 1, monkeypatch, started)
    assert started == [(sid, True)] and _row(sc["id"]).state == "running" and "en marcha" in said[-1]
    assert _tick(t0 + 1800, monkeypatch, []) == [] and inst.running                    # half way: nothing
    assert _tick(t0 + 3600 - 200, monkeypatch, []) == [] and _row(sc["id"]).end_warned  # 5 min before the end: chat, not Discord
    assert len(inst.acsp.sent) == 1
    said = _tick(t0 + 3601, monkeypatch, [])
    r = _row(sc["id"])
    assert not inst.running and inst.stopped_for == "event_end" and r.state == "done" and "terminó" in said[-1]
    assert _tick(t0 + 3700, monkeypatch, []) == []


def test_window_opens_an_hour_before_and_closes_at_the_end(monkeypatch):
    sid, sc = _setup(7200, reminders=(), duration=90)
    t0 = sc["start_at"]
    with Session(engine) as s:
        assert schedule.open_window(s, sid, t0 - 3601) is None
        assert schedule.open_window(s, sid, t0 - 3500).id == sc["id"]
        assert schedule.open_window(s, sid, t0 + 90 * 60 - 1).id == sc["id"]
        assert schedule.open_window(s, sid, t0 + 90 * 60) is None


def test_wake_loads_the_event_once_then_just_starts_the_server(monkeypatch):
    sid, sc = _setup(1800, reminders=(), duration=60)
    t0, applied, started = sc["start_at"], [], []
    monkeypatch.setattr(schedule.supervisor, "get", lambda _id: None)

    async def fake_apply(sess, srv, body, **kw):
        applied.append((srv.id, body.restart))

    async def fake_start(sess, server_id):
        started.append(server_id)
    monkeypatch.setattr(server_service, "apply", fake_apply)
    monkeypatch.setattr(server_service, "start", fake_start)
    assert asyncio.run(schedule.wake(sid, t0 - 7200)) is False and applied == []          # window not open yet
    assert asyncio.run(schedule.wake(sid, t0 - 600)) is True and applied == [(sid, True)] and _row(sc["id"]).loaded
    assert asyncio.run(schedule.wake(sid, t0 - 300)) is True and started == [sid] and len(applied) == 1   # loaded: no second apply
    # at the start time the event is already on the server: not applied again (it would kick the early arrivals)
    _tick(t0 + 1, monkeypatch, [])
    assert len(applied) == 1 and _row(sc["id"]).state == "running"
    monkeypatch.setattr(schedule.supervisor, "get", lambda _id: _FakeInstance())
    assert asyncio.run(schedule.wake(sid, t0 + 10)) is False                                # already running


def test_overlapping_events_on_one_server_are_refused_but_a_queue_is_fine():
    sid, first = _setup(7200, reminders=(), duration=60)
    eid = first["event_id"]
    t0 = first["start_at"]

    def post(start, duration=60, **extra):
        return client.post("/api/v1/schedules", json={"event_id": eid, "server_id": sid, "start_at": start, "duration_min": duration, "reminders": [], **extra})
    assert post(t0 + 1800).status_code == 409                      # starts while the first one is running
    assert post(t0 - 1800).status_code == 409                      # ends while the first one is running
    assert post(t0 + 3600).status_code == 201                      # right after it: queued
    assert post(t0 - 3600).status_code == 201                      # right before it


def test_silent_past_marks_the_reminders_already_due_as_sent_and_info_travels_with_the_messages(monkeypatch):
    sid, sc = _setup(20 * 3600, reminders=(), duration=None)
    eid = sc["event_id"]
    r = client.post("/api/v1/schedules", json={"event_id": eid, "server_id": 0 or sid, "start_at": time.time() + 7200 * 5 + 1,   # 10 h from now
                                               "reminders": [1440, 60, 10], "silent_past": True, "info": "📍 1.2.3.4:9600", "duration_min": 5})
    assert r.status_code == 201, r.text
    assert r.json()["sent"] == [1440] and r.json()["info"] == "📍 1.2.3.4:9600"      # the day-before notice is covered; 1 h and 10 min are still to come
    said = [m for m in _tick(r.json()["start_at"] - 3000, monkeypatch, []) if "📍" in m]   # (other tests' schedules share the database)
    assert len(said) == 1 and said[0].endswith("\n📍 1.2.3.4:9600") and _row(r.json()["id"]).sent == [1440, 60]


def test_the_end_of_the_event_waits_for_whoever_is_still_racing(monkeypatch):
    sid, sc = _setup(100, reminders=(), duration=30)
    t0, inst = sc["start_at"], _FakeInstance()
    inst.acsp.cars = {0: {"car_id": 0}}
    monkeypatch.setitem(schedule.supervisor._instances, sid, inst)
    _tick(t0 + 1, monkeypatch, [])
    _tick(t0 + 1800 + 5, monkeypatch, [])                         # the event is over but a car is still on track
    assert inst.running and _row(sc["id"]).state == "running"
    inst.acsp.cars = {}                                           # the last one leaves
    _tick(t0 + 1800 + 60, monkeypatch, [])
    assert not inst.running and inst.stopped_for == "event_end" and _row(sc["id"]).state == "done"
    sid, sc = _setup(100, reminders=(), duration=30)              # ...and it never waits forever
    inst2 = _FakeInstance()
    inst2.acsp.cars = {0: {"car_id": 0}}
    monkeypatch.setitem(schedule.supervisor._instances, sid, inst2)
    _tick(sc["start_at"] + 1, monkeypatch, [])
    _tick(sc["start_at"] + 1800 + schedule.END_GRACE + 1, monkeypatch, [])
    assert not inst2.running



def test_rsvp_reactions_become_rows_linked_to_users(monkeypatch):
    from app.config import settings
    from app.models import User
    monkeypatch.setattr(settings, "discord_bot_token", "x")
    monkeypatch.setattr(settings, "discord_channel", "1")
    with Session(engine) as s:
        s.add(User(username="piloto", discord_id="111"))
        for old in s.exec(select(Schedule)).all():   # other tests' pending schedules would be posted too
            s.delete(old)
        s.commit()
    posted, edited = [], []
    reactions = {"yes": ["111", "222"], "maybe": ["222"], "no": []}   # 222 reacted twice with no earlier answer: the first by priority
    monkeypatch.setattr(discord, "rsvp_post", lambda text: posted.append(text) or "m1")
    monkeypatch.setattr(discord, "rsvp_read", lambda mid: reactions)
    monkeypatch.setattr(discord, "rsvp_edit", lambda mid, text: edited.append(text))
    _, sc = _setup(3600)
    _tick(time.time(), monkeypatch)   # posts the announcement
    assert len(posted) == 1
    _tick(time.time(), monkeypatch)   # reads the reactions
    got = {r["discord_id"]: r for r in client.get(f"/api/v1/schedules/{sc['id']}/rsvps").json()}
    assert got["111"]["username"] == "piloto" and got["111"]["status"] == "yes"
    assert got["222"]["username"] is None
    assert "✅ 2" in edited[-1]["content"]
    reactions["yes"], reactions["maybe"] = ["111"], ["222"]   # 222 moves from yes to maybe
    _tick(time.time(), monkeypatch)
    assert client.get("/api/v1/schedules").json()[0]["rsvp"] == {"yes": 1, "maybe": 1, "no": 0}
    assert {r["discord_id"]: r["status"] for r in client.get(f"/api/v1/schedules/{sc['id']}/rsvps").json()}["222"] == "maybe"


def test_rsvp_read_asks_only_for_emojis_somebody_used(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "discord_channel", "1")
    monkeypatch.setattr(discord.time, "sleep", lambda s: None)
    calls = []

    def fake(method, path, *a, **k):
        calls.append(path)
        if "/reactions/" in path:
            return [{"id": "5"}, {"id": "9", "bot": True}]
        return {"reactions": [{"emoji": {"name": "✅"}, "count": 2, "me": True}, {"emoji": {"name": "❌"}, "count": 1, "me": True}]}
    monkeypatch.setattr(discord, "_api", fake)
    assert discord.rsvp_read("m") == {"yes": ["5"], "maybe": [], "no": []}
    assert len(calls) == 2   # the message + ✅ only; ❌ is just the bot


def test_announcement_layout(monkeypatch):
    from datetime import UTC, datetime
    from app import announcement
    from app.config import settings
    monkeypatch.setattr(settings, "discord_role", "42")
    start = datetime(2026, 10, 6, 3, 0, tzinfo=UTC).timestamp()
    v = announcement.variables("Fun Race", "Servidor 1", {"track": "spa", "cars": ["clio"], "practice_min": 15, "qualify_min": 15,
                                                         "race_laps": 15, "reversed_grid": -1}, start, {"yes": 3, "maybe": 1, "no": 0}, "Descarga: https://x.test")
    out = announcement.render(announcement.DEFAULT, v)
    text = out["content"]
    assert f"<t:{int(start)}:F>" in text and "<@&42>" in text and "Parrilla invertida (toda)" in text and "15 vueltas" in text
    assert f"Práctica: 15 min · <t:{int(start)}:t>" in text and f"Clasificación: 15 min · <t:{int(start) + 900}:t>" in text and f"15 vueltas · <t:{int(start) + 1800}:t>" in text
    assert "✅ 3 · ❔ 1 · ❌ 0" in text and text.endswith("Descarga: https://x.test")
    assert out["allowed_mentions"] == {"parse": ["roles"]} and "Clima" not in text          # no weather chosen: the line is left out
    wx = lambda g: {"track": "spa", "cars": ["clio"], "options": {"weather": [{"graphics": x} for x in g]}}
    assert announcement.variables("T", "S", wx(["7_heavy_clouds_type=8"]), start, {"yes": 0, "maybe": 0, "no": 0})["clima_line"] == "🌦️ Clima: Lluvia fuerte"
    assert announcement.variables("T", "S", wx(["3_clear", "5_light_clouds", "6_mid_clouds"]), start, {"yes": 0, "maybe": 0, "no": 0})["clima"] == "Realista"


def test_announcement_template_is_editable_and_validated():
    url = "/api/v1/announcement"
    assert client.put(url, json={"template": {"content": "x", "components": []}}).status_code == 422
    assert client.put(url, json={"template": {"content": "  "}}).status_code == 422
    r = client.post(url + "/preview", json={"template": {"content": "{title} va {yes}\n\n\n\n{formato}{nope}", "embeds": [{"title": "{server}"}]}}).json()
    assert r["content"].startswith("Fun Race | Spa PetitChamps va 3") and "{nope}" in r["content"] and r["embeds"][0]["title"] == "Servidor 1"
    assert client.put(url, json={"template": {"content": "hola {title}"}}).status_code == 200
    assert client.get(url).json()["template"] == {"content": "hola {title}"}
    assert client.delete(url).status_code == 204
    assert client.get(url).json()["template"]["content"].startswith("🏁")


def _race(monkeypatch, sid, apply_fail=None):
    """wake() and the tick both about to load the same event; apply yields (like the real stop/start awaits) so they can interleave."""
    applied, started = [], []
    monkeypatch.setattr(discord, "announce", lambda *_a, **_k: None)
    monkeypatch.setattr(schedule.supervisor, "get", lambda _id: None)

    async def fake_apply(sess, srv, body, **kw):
        applied.append(srv.id)
        await asyncio.sleep(0.05)
        if apply_fail:
            raise apply_fail

    async def fake_start(sess, server_id):
        started.append(server_id)
    monkeypatch.setattr(server_service, "apply", fake_apply)
    monkeypatch.setattr(server_service, "start", fake_start)
    return applied, started


def test_a_wake_and_the_tick_load_the_event_only_once(monkeypatch):
    """Without the atomic claim both saw loaded=False and applied twice: the second restart kicks the players who were already joining."""
    sid, sc = _setup(100, reminders=(), duration=60)
    t0 = sc["start_at"]
    applied, started = _race(monkeypatch, sid)

    async def both():
        return await asyncio.gather(schedule.wake(sid, t0 + 1), schedule.tick(t0 + 1))
    woke, _ = asyncio.run(both())
    assert applied == [sid]                                   # one apply, whoever got there first
    assert _row(sc["id"]).loaded and _row(sc["id"]).state == "running"
    assert woke is True


def test_a_failed_apply_gives_the_claim_back_so_the_next_try_loads_the_event(monkeypatch):
    sid, sc = _setup(100, reminders=(), duration=60)
    t0 = sc["start_at"]
    applied, _ = _race(monkeypatch, sid, apply_fail=ServerError(409, "track not installed"))
    assert asyncio.run(schedule.wake(sid, t0 + 1)) is False
    assert not _row(sc["id"]).loaded                          # not stuck as "loaded" with nothing on the server
    assert asyncio.run(schedule.wake(sid, t0 + 2)) is False and applied == [sid, sid]   # tried again, it was not skipped
