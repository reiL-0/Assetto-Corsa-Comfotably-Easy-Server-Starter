"""The client's own Discord bot (app/discord_config.py + the routing in app/discord.py): what is stored and what never is, the checks against Discord before anything is saved, the switches,
the reminders of events that have not started, the league's environment setup left exactly as it was, and no token in any answer, log or error. Discord is a fake: urllib's urlopen."""
import io
import json
import logging
import os
import stat
import time
import urllib.error
from pathlib import Path

import pytest
from conftest import ADMIN
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import discord, discord_config
from app.config import settings
from app.db import engine
from app.main import app
from app.models import Schedule, Setting

client = TestClient(app, headers=ADMIN)
TOKEN = "MTIzNDU2Nzg5MDEyMzQ1Njc4.GabcDE.fakeFAKEfake_Token-value-1234567890abcdefXYZ"
CH, CH2, ROLE = "123456789012345678", "223456789012345678", "323456789012345678"
GOOD = {"bot_token": TOKEN, "channel_id": CH, "role_id": ROLE}


class FakeDiscord:
    """urllib.request.urlopen stand-in: answers by (method, path), records every call, and can refuse with an HTTP code or be unreachable."""

    def __init__(self):
        self.calls, self.fail, self.down = [], {}, False

    def __call__(self, req, timeout=8):
        path = req.full_url.split("/api/v10", 1)[1]
        self.calls.append({"method": req.get_method(), "path": path, "auth": req.get_header("Authorization"), "body": json.loads(req.data) if req.data else None})
        if self.down:
            raise urllib.error.URLError("down")
        code = self.fail.get((req.get_method(), path)) or self.fail.get(path)
        if code:
            raise urllib.error.HTTPError(req.full_url, code, "refused", {}, io.BytesIO(b"{}"))
        body = {"/users/@me": {"id": "9", "username": "GridBot"}}.get(path) or ({"id": path.rsplit("/", 1)[-1], "name": "anuncios"} if path.startswith("/channels/") and "/messages" not in path else {"id": "55"})

        class R:
            def read(self_):
                return json.dumps(body).encode()

            def close(self_):
                pass

            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False
        return R()


@pytest.fixture
def fake(monkeypatch, tmp_path):
    f = FakeDiscord()
    monkeypatch.setattr(discord.urllib.request, "urlopen", f)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "discord_webhook", "")
    monkeypatch.setattr(settings, "discord_status_webhook", "")
    monkeypatch.setattr(settings, "discord_bot_token", "")
    monkeypatch.setattr(settings, "discord_channel", "")
    monkeypatch.setattr(discord_config, "_last_test", 0.0)
    monkeypatch.setattr(discord.time, "sleep", lambda s: None)
    with Session(engine) as s:   # the settings row lives in the shared test database: start and leave it clean
        row = s.get(Setting, "discord")
        if row:
            s.delete(row)
            s.commit()
    yield f
    with Session(engine) as s:   # (the events scheduled here must not be picked up by the tick of another test module)
        for sc in s.exec(select(Schedule)).all():
            s.delete(sc)
        row = s.get(Setting, "discord")
        if row:
            s.delete(row)
        s.commit()


def token_files():
    return sorted((Path(settings.data_dir) / "secrets").glob("discord_bot_token.*"))


def stored():
    with Session(engine) as s:
        row = s.get(Setting, "discord")
        return dict(row.value) if row else None


def drain():
    import threading
    for t in list(threading.enumerate()):
        if t is not threading.current_thread() and t.daemon and t.name.startswith("Thread"):
            t.join(timeout=2)


def test_nothing_configured_is_the_leagues_setup_untouched(fake, monkeypatch):
    d = client.get("/api/v1/discord").json()
    assert d["configured"] is False and d["reminders"] == [1440, 60, 10] and d["token_last4"] is None and discord.bot() is None
    sent = []
    monkeypatch.setattr(discord, "_send", lambda text, url=None: sent.append((text, url)))
    monkeypatch.setattr(settings, "discord_webhook", "https://hook.test/a")
    monkeypatch.setattr(settings, "discord_status_webhook", "https://hook.test/s")
    discord.announce("hola"); discord.alert("ojo"); drain()
    assert ("hola", "https://hook.test/a") in sent and ("ojo", None) in sent and not fake.calls, "the league's webhooks changed"
    assert discord.rsvp_enabled() is False and discord.role_id() == ""
    monkeypatch.setattr(settings, "discord_bot_token", "envtoken"); monkeypatch.setattr(settings, "discord_channel", "999")
    assert discord.rsvp_enabled() is True and discord._creds()[1] == "999"


def test_saving_checks_discord_first_and_never_gives_the_token_back(fake, caplog):
    caplog.set_level(logging.DEBUG)
    r = client.put("/api/v1/discord", json=GOOD)
    assert r.status_code == 200, r.text
    assert [(c["method"], c["path"]) for c in fake.calls] == [("GET", "/users/@me"), ("GET", f"/channels/{CH}")] and all(c["auth"] == f"Bot {TOKEN}" for c in fake.calls)
    d = r.json()
    assert d["configured"] and d["bot_name"] == "GridBot" and d["channel_name"] == "anuncios" and d["token_last4"] == TOKEN[-4:] and d["role_id"] == ROLE
    for text in (r.text, client.get("/api/v1/discord").text, json.dumps(stored()), caplog.text):
        assert TOKEN not in text and TOKEN[:20] not in text, "the token leaked"
    assert len(token_files()) == 1, token_files()
    p = token_files()[0]
    assert p.read_text() == TOKEN and stat.S_IMODE(p.stat().st_mode) == 0o600 and stat.S_IMODE(p.parent.stat().st_mode) == 0o700, "the token file is not private"


@pytest.mark.parametrize("body,sentence", [
    ({**GOOD, "channel_id": "abc"}, "solo números"), ({**GOOD, "channel_id": ""}, "Falta el id"), ({**GOOD, "role_id": "12"}, "solo números"), ({**GOOD, "status_channel_id": "1" * 30}, "solo números"),
    ({**GOOD, "bot_token": "short"}, "token de bot"), ({**GOOD, "bot_token": 12345}, "token de bot"), ({**GOOD, "reminders": [0]}, "recordatorios"), ({**GOOD, "reminders": [1441]}, "recordatorios"),
    ({**GOOD, "reminders": [1, 2, 3, 4, 5, 6]}, "recordatorios"), ({**GOOD, "reminders": "60"}, "recordatorios"), ({**GOOD, "reminders": [True]}, "recordatorios"),
    ({**GOOD, "rsvp_enabled": "yes"}, "verdadero o falso"), ([1, 2], "objeto JSON"),
])
def test_a_bad_request_is_refused_with_a_sentence_and_changes_nothing(fake, body, sentence):
    r = client.put("/api/v1/discord", json=body)
    assert r.status_code == 422 and sentence in r.json()["detail"], r.text
    assert TOKEN not in r.text and stored() is None and not token_files() and not fake.calls, "something was stored or sent"


def test_a_value_the_framework_would_echo_is_not_echoed(fake):
    r = client.put("/api/v1/discord", content=b"{not json", headers={**ADMIN, "Content-Type": "application/json"})
    assert r.status_code == 422 and "JSON" in r.json()["detail"] and stored() is None
    r = client.put("/api/v1/discord", json={**GOOD, "reminders": [TOKEN]})
    assert r.status_code == 422 and TOKEN not in r.text and TOKEN not in json.dumps(r.json()), "the validation error repeated the token"


@pytest.mark.parametrize("code,sentence", [(401, "no aceptó ese token"), (403, "no tiene permiso"), (404, "no existe, o el bot no lo ve"), (500, "error (500)")])
def test_discord_refusing_means_nothing_is_stored(fake, code, sentence):
    fake.fail[f"/channels/{CH}"] = code
    if code == 401:
        fake.fail["/users/@me"] = 401
    r = client.put("/api/v1/discord", json=GOOD)
    assert r.status_code == 422 and sentence in r.json()["detail"] and TOKEN not in r.text
    assert stored() is None and not token_files()


def test_discord_unreachable_means_nothing_is_stored(fake):
    fake.down = True
    r = client.put("/api/v1/discord", json=GOOD)
    assert r.status_code == 502 and "no se guardó nada" in r.json()["detail"] and stored() is None and not token_files()


def test_a_partial_update_keeps_the_token_and_does_not_ask_discord(fake):
    client.put("/api/v1/discord", json=GOOD)
    fake.calls.clear()
    r = client.put("/api/v1/discord", json={"reminders": [30, 5, 30], "announce_enabled": False})
    assert r.status_code == 200 and r.json()["reminders"] == [30, 5] and r.json()["announce_enabled"] is False and r.json()["configured"] and not fake.calls
    assert len(token_files()) == 1 and token_files()[0].read_text() == TOKEN and discord_config.reminders() == [30, 5]


def schedule(**kw):
    with Session(engine) as s:
        sc = Schedule(event_id=1, server_id=1, start_at=kw.pop("start_at", time.time() + 86400), **kw)
        s.add(sc); s.commit(); s.refresh(sc)
        return sc.id


def sched(i):
    with Session(engine) as s:
        return s.get(Schedule, i)


def test_changing_the_reminders_rewrites_events_that_have_not_started_and_keeps_what_was_sent(fake):
    client.put("/api/v1/discord", json=GOOD)
    soon = schedule(reminders=[1440, 60, 10], sent=[1440, 60], rsvp_message="m1")
    running = schedule(reminders=[1440, 60, 10], sent=[1440], state="running", rsvp_message="m2")
    past = schedule(reminders=[60], start_at=time.time() - 3600, rsvp_message="m3")
    client.put("/api/v1/discord", json={"reminders": [180, 60, 5]})
    assert sched(soon).reminders == [180, 60, 5] and sched(soon).sent == [60], "sent reminders were lost or resent"
    assert sched(running).reminders == [1440, 60, 10] and sched(past).reminders == [60], "an event that already started was rewritten"
    assert sched(soon).rsvp_message == "m1", "the sign-up message was dropped for a reminders-only change"


def test_a_new_bot_or_channel_clears_the_sign_up_messages_of_pending_events_only(fake):
    client.put("/api/v1/discord", json=GOOD)
    a, b = schedule(rsvp_message="m1", rsvp_text="x"), schedule(rsvp_message="m2", state="running")
    client.put("/api/v1/discord", json={"channel_id": CH2})
    assert sched(a).rsvp_message is None and sched(a).rsvp_text == "" and sched(b).rsvp_message == "m2"
    c = schedule(rsvp_message="m3")
    client.put("/api/v1/discord", json={"bot_token": TOKEN.replace("fake", "new1")})
    assert sched(c).rsvp_message is None


def test_messages_go_through_the_clients_bot_to_the_right_channels_and_the_switches_are_honoured(fake, monkeypatch):
    client.put("/api/v1/discord", json={**GOOD, "status_channel_id": CH2})
    fake.calls.clear()
    discord.announce("⏰ empieza"); discord.alert("⚠️ ojo"); drain()
    posts = {(c["path"], c["body"]["content"], c["auth"]) for c in fake.calls}
    assert posts == {(f"/channels/{CH}/messages", "⏰ empieza", f"Bot {TOKEN}"), (f"/channels/{CH2}/messages", "⚠️ ojo", f"Bot {TOKEN}")}, posts
    assert discord.role_id() == ROLE and discord.rsvp_enabled() is True and discord._creds()[1] == CH
    client.put("/api/v1/discord", json={"announce_enabled": False, "status_enabled": False, "rsvp_enabled": False})
    fake.calls.clear(); sent = []
    monkeypatch.setattr(discord, "_send", lambda text, url=None: sent.append(text))
    monkeypatch.setattr(settings, "discord_webhook", "https://hook.test/a")   # an environment webhook must NOT take over from a bot whose switches are off
    discord.announce("no"); discord.alert("no"); discord.on_event(1, "server_start", None, None); drain()
    assert not fake.calls and not sent and discord.rsvp_enabled() is False, "a switched-off bot (or the webhook behind it) still posted"


def test_the_server_status_posts_use_the_status_channel(fake):
    client.put("/api/v1/discord", json=GOOD)   # no status channel: the announcements channel
    fake.calls.clear()
    discord.on_event(1, "server_start", None, None); drain()
    assert [c["path"] for c in fake.calls] == [f"/channels/{CH}/messages"]


def test_a_failing_post_is_remembered_without_the_token_and_cleared_on_success(fake, caplog):
    caplog.set_level(logging.DEBUG)
    client.put("/api/v1/discord", json=GOOD)
    fake.fail[("POST", f"/channels/{CH}/messages")] = 403
    discord.announce("x"); drain()
    err = client.get("/api/v1/discord").json()["last_error"]
    assert err and "permiso" in err and TOKEN not in err and TOKEN not in caplog.text
    fake.fail.clear()
    discord.announce("y"); drain()
    assert client.get("/api/v1/discord").json()["last_error"] is None


def test_the_test_message_is_rate_limited_and_needs_a_saved_bot(fake):
    assert client.post("/api/v1/discord/test").status_code == 409
    client.put("/api/v1/discord", json={**GOOD, "status_channel_id": CH2})
    fake.calls.clear()
    r = client.post("/api/v1/discord/test")
    assert r.status_code == 200 and r.json()["channels"] == [CH, CH2] and [c["method"] for c in fake.calls] == ["POST", "POST"]
    assert client.post("/api/v1/discord/test").status_code == 429
    discord_config._last_test = 0.0
    fake.fail[("POST", f"/channels/{CH}/messages")] = 403
    r = client.post("/api/v1/discord/test")
    assert r.status_code == 422 and "enviar mensajes" in r.json()["detail"] and TOKEN not in r.text


def test_forgetting_the_bot_returns_to_the_environment(fake, monkeypatch):
    client.put("/api/v1/discord", json=GOOD)
    assert client.delete("/api/v1/discord").status_code == 204
    assert stored() is None and not token_files() and discord.bot() is None
    assert client.get("/api/v1/discord").json()["configured"] is False
    assert client.delete("/api/v1/discord").status_code == 204   # (twice: nothing to forget)


def test_only_an_admin(fake):
    anon = TestClient(app)
    for call in (lambda: anon.get("/api/v1/discord"), lambda: anon.put("/api/v1/discord", json=GOOD), lambda: anon.delete("/api/v1/discord"), lambda: anon.post("/api/v1/discord/test")):
        assert call().status_code in (401, 403)


def test_two_changes_at_once_never_overwrite_each_other(fake, monkeypatch):
    client.put("/api/v1/discord", json=GOOD)
    real, once = discord_config._discord, []

    def slow(token, path):   # while the first change is asking Discord, a second one is saved
        if not once:
            once.append(1)
            assert client.put("/api/v1/discord", json={"reminders": [7]}).status_code == 200
        return real(token, path)
    monkeypatch.setattr(discord_config, "_discord", slow)
    r = client.put("/api/v1/discord", json={"channel_id": CH2, "reminders": [999]})
    assert r.status_code == 409 and "Otro cambio" in r.json()["detail"], r.text
    v = stored()
    assert v["reminders"] == [7] and v["channel_id"] == CH, "the older change overwrote the newer one"


def test_a_failed_save_leaves_the_old_bot_working_and_a_rotation_leaves_one_token(fake, monkeypatch):
    client.put("/api/v1/discord", json=GOOD)
    new_token = TOKEN.replace("fake", "new1")
    with monkeypatch.context() as m:
        m.setattr(discord_config, "_save", lambda sess, value: (_ for _ in ()).throw(RuntimeError("disk full")))
        r = TestClient(app, headers=ADMIN, raise_server_exceptions=False).put("/api/v1/discord", json={"bot_token": new_token})
    assert r.status_code == 500 and new_token not in r.text
    assert len(token_files()) == 1 and token_files()[0].read_text() == TOKEN and discord_config.active()["token"] == TOKEN, "a failed save changed the bot"
    assert client.put("/api/v1/discord", json={"bot_token": new_token}).status_code == 200
    assert len(token_files()) == 1 and token_files()[0].read_text() == new_token and discord_config.active()["token"] == new_token and client.get("/api/v1/discord").json()["token_last4"] == new_token[-4:]


def test_the_note_never_puts_an_older_version_back(fake):
    client.put("/api/v1/discord", json=GOOD)
    client.put("/api/v1/discord", json={"reminders": [45], "status_enabled": False})
    discord_config.note("Al bot le falta permiso para publicar en el canal.")
    v = client.get("/api/v1/discord").json()
    assert v["last_error"] and v["reminders"] == [45] and v["status_enabled"] is False and v["channel_id"] == CH and v["configured"]


def test_events_created_without_reminders_use_the_clients_cadence(fake):
    from test_schedule import _setup
    client.put("/api/v1/discord", json={**GOOD, "reminders": [180, 20]})
    sid = client.post("/api/v1/servers", json={"name": "Cadence"}).json()["id"]
    eid = client.post("/api/v1/events", json={"title": "Ev", "session": {"name": "x", "track": "spa", "cars": ["bmw"]}}).json()["id"]
    r = client.post("/api/v1/schedules", json={"event_id": eid, "server_id": sid, "start_at": time.time() + 7200})
    assert r.status_code == 201 and r.json()["reminders"] == [180, 20], r.text
    sid2, explicit = _setup(7200, reminders=(30,))
    assert explicit["reminders"] == [30], "an explicit list was replaced by the configured one"


def test_a_sign_up_message_posted_while_the_bot_changes_is_not_kept(fake, monkeypatch):
    from test_schedule import _setup, _tick
    client.put("/api/v1/discord", json=GOOD)
    with Session(engine) as s:
        for old in s.exec(select(Schedule)).all():
            s.delete(old)
        s.commit()
    _, sc = _setup(3600)
    posts = []

    def post(payload):   # the channel is changed while the message is being posted
        posts.append(1)
        if len(posts) == 1:
            assert client.put("/api/v1/discord", json={"channel_id": CH2}).status_code == 200
        return f"m{len(posts)}"
    monkeypatch.setattr(discord, "rsvp_post", post)
    monkeypatch.setattr(discord, "rsvp_read", lambda mid: {"yes": [], "maybe": [], "no": []})
    _tick(time.time(), monkeypatch)
    assert sched(sc["id"]).rsvp_message is None, "a message posted for the old channel was kept as this event's announcement"
    _tick(time.time(), monkeypatch)
    assert sched(sc["id"]).rsvp_message == "m2" and len(posts) == 2


def test_simultaneous_test_messages_are_one_message(fake):
    import threading
    client.put("/api/v1/discord", json=GOOD)
    fake.calls.clear()
    codes = []
    ts = [threading.Thread(target=lambda: codes.append(client.post("/api/v1/discord/test").status_code)) for _ in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(codes) == [200] + [429] * 5 and len([c for c in fake.calls if c["method"] == "POST"]) == 1, codes


class SpyLock:
    """A lock that counts how many times it was taken (a race of two statements cannot be provoked reliably; that the lock is taken at all can)."""

    def __init__(self):
        import threading
        self.entered, self._l = 0, threading.Lock()

    def __enter__(self):
        self.entered += 1
        self._l.acquire()
        return self

    def __exit__(self, *a):
        self._l.release()
        return False


def test_the_note_and_the_test_message_take_their_locks(fake, monkeypatch):
    client.put("/api/v1/discord", json=GOOD)
    write, test = SpyLock(), SpyLock()
    monkeypatch.setattr(discord_config, "_write", write)
    monkeypatch.setattr(discord_config, "_test_lock", test)
    discord_config.note("algo falló")
    assert write.entered == 1, "note() rewrites the settings without the lock that serialises changes"
    assert client.post("/api/v1/discord/test").status_code == 200 and test.entered == 1, "the test message checks and marks its window without a lock"


def test_asking_for_the_bot_before_the_database_has_its_tables_is_no_bot_not_a_crash(monkeypatch, tmp_path):
    """The sample announcement (app/announcement.py, built at import) asks for the role; the manager imports before init_db has made `settings` on a fresh database. It used to raise «no such table»."""
    from sqlalchemy import create_engine
    monkeypatch.setattr(discord_config, "engine", create_engine(f"sqlite:///{tmp_path}/empty.db"))
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    assert discord_config.active() is None and discord_config.reminders() == [1440, 60, 10]
    assert discord.bot() is None and discord.role_id() == settings.discord_role and discord.rsvp_enabled() is False
    from app import announcement
    assert announcement.variables("T", "S", {"track": "spa", "cars": ["clio"], "practice_min": 15, "qualify_min": 15, "race_laps": 5}, 0, {"yes": 0, "maybe": 0, "no": 0})["title"] == "T"


def test_the_manager_imports_on_a_database_with_no_tables(tmp_path):
    """What the server's import check does before anything is touched: `import app.main` against an empty database file."""
    import os
    import subprocess
    import sys
    env = {**os.environ, "ACM_DATA_DIR": str(tmp_path), "ACM_DB_PATH": str(tmp_path / "empty.db"), "ACM_ACSERVER_CMD": ""}
    r = subprocess.run([sys.executable, "-c", "import app.main"], cwd=str(__import__("pathlib").Path(__file__).resolve().parent.parent), env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-800:]
