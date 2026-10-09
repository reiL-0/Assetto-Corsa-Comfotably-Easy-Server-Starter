"""T1.2: the composite operations on one server (apply = stop + start, start with its INI files, automatic stop) share one per-server lock."""
import asyncio

from conftest import ADMIN
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import content, servers, supervisor
from app.services import server_service
from app.config import settings
from app.db import engine
from app.main import app
from app.models import Server

api = TestClient(app, headers=ADMIN)
BODY = {"name": "Lock", "track": "lockspa", "cars": ["lockbmw"], "max_clients": 4, "race_laps": 3, "practice_min": 5, "restart": True}


def _server():
    t, c = content._tracks_dir(), content._cars_dir()
    (t / "lockspa" / "data").mkdir(parents=True, exist_ok=True)
    (t / "lockspa" / "data" / "surfaces.ini").write_text("x")
    (c / "lockbmw").mkdir(exist_ok=True)
    (c / "lockbmw" / "data.acd").write_text("x")
    return api.post("/api/v1/servers", json={"name": "t"}).json()["id"]


class _Inst:
    running, pid, stopped_for = True, 1, None

    def set_weather_plan(self, *_a):
        pass

    async def stop(self, reason="manual"):
        self.running, self.stopped_for = False, reason


def test_apply_restart_is_one_step_nothing_slips_between_its_stop_and_its_start(tmp_path, monkeypatch):
    """Without the composite lock a second flow (a wake, the API) could stop/start in the gap, so the config ended up on a server it did not expect."""
    (tmp_path / "acServer").write_text("")
    monkeypatch.setattr(settings, "acserver_cmd", str(tmp_path / "acServer"))
    sid, events = _server(), []

    async def fake_stop(server_id, reason="manual"):
        events.append((asyncio.current_task().get_name(), "stop"))
        await asyncio.sleep(0.02)

    async def fake_start(server_id, cwd, **kw):
        events.append((asyncio.current_task().get_name(), "start"))
        await asyncio.sleep(0.02)
        return _Inst()
    monkeypatch.setattr(supervisor, "stop", fake_stop)
    monkeypatch.setattr(supervisor, "start", fake_start)

    async def scenario():
        with Session(engine) as s1, Session(engine) as s2:
            body = servers.SessionIn(**BODY)
            a = asyncio.create_task(servers.apply_to_server(s1, s1.get(Server, sid), body), name="apply")
            b = asyncio.create_task(servers.start_server(sid, s2), name="other")
            await asyncio.gather(a, b)
    asyncio.run(scenario())
    i = events.index(("apply", "stop"))
    assert events[i + 1] == ("apply", "start"), events          # its start is the very next thing after its stop
    assert [e for e in events if e[1] == "start"].count(("other", "start")) == 1


def test_a_failed_start_inside_apply_releases_the_lock(tmp_path, monkeypatch):
    (tmp_path / "acServer").write_text("")
    monkeypatch.setattr(settings, "acserver_cmd", str(tmp_path / "acServer"))
    sid = _server()

    async def boom(server_id, cwd, **kw):
        raise RuntimeError("cannot spawn")

    async def noop_stop(server_id, reason="manual"):
        pass
    monkeypatch.setattr(supervisor, "stop", noop_stop)
    monkeypatch.setattr(supervisor, "start", boom)

    async def scenario():
        with Session(engine) as s:
            res = await server_service.apply(s, s.get(Server, sid), servers.SessionIn(**BODY))
            assert res.restarted is False and res.start_error.status == 409      # saved, start failed: a result, not a bare refusal
            assert s.get(Server, sid).session["track"] == "lockspa"              # the config really is on the server
            try:
                await servers.apply_to_server(s, s.get(Server, sid), servers.SessionIn(**BODY))
            except Exception as e:  # the route says so: same 409, and that the configuration was saved
                assert getattr(e, "status_code", None) == 409 and str(e.detail).startswith("configuración guardada, arranque fallido")
            else:
                raise AssertionError("the failed start must reach the caller")
            assert not servers.server_lock(sid).locked()       # released: a new attempt is possible
    asyncio.run(scenario())


def test_automatic_stop_waits_for_the_lock_and_skips_an_instance_that_was_replaced(monkeypatch):
    sid, inst = 4242, _Inst()
    monkeypatch.setitem(supervisor._instances, sid, inst)

    async def scenario():
        async with servers.server_lock(sid):                   # an apply is in the middle of its stop+start
            t = asyncio.create_task(servers.stop_instance(sid, inst, "event_end"))
            await asyncio.sleep(0.05)
            assert inst.running and not t.done()                # the end-of-event stop waits its turn
        await t
        assert not inst.running and inst.stopped_for == "event_end"
        other = _Inst()                                         # the registry now holds a different instance
        supervisor._instances[sid] = other
        await servers.stop_instance(sid, inst, "event_end")
        assert other.running                                    # the stale reference stops nothing
    asyncio.run(scenario())
