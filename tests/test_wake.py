import asyncio
import socket
import time

from conftest import ADMIN
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import schedule, wake
from app.db import engine
from app.main import app
from app.models import Server

client = TestClient(app, headers=ADMIN)


def _free_port() -> int:
    while True:
        with socket.socket() as t:
            t.bind(("127.0.0.1", 0))
            port = t.getsockname()[1]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
            try:
                u.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue


def _server_with_window(start_in: float):
    sid = client.post("/api/v1/servers", json={"name": "WakeServer"}).json()["id"]
    port = _free_port()
    with Session(engine) as s:
        srv = s.get(Server, sid)
        srv.base_port = port
        s.add(srv)
        s.commit()
    eid = client.post("/api/v1/events", json={"title": "Wake R1", "session": {"name": "x", "track": "spa", "cars": ["bmw"]}}).json()["id"]
    r = client.post("/api/v1/schedules", json={"event_id": eid, "server_id": sid, "start_at": time.time() + start_in, "reminders": [], "duration_min": 60})
    assert r.status_code == 201
    return sid, port


def test_a_datagram_or_a_connection_in_the_window_wakes_the_server(monkeypatch):
    monkeypatch.setattr(wake, "HOST", "127.0.0.1")
    woken = []

    async def fake_wake(server_id, now=None):
        woken.append(server_id)
        return True
    monkeypatch.setattr(schedule, "wake", fake_wake)
    monkeypatch.setattr(wake.supervisor, "get", lambda _id: None)
    sid, port = _server_with_window(600)          # starts in 10 min: inside the hour-before window

    async def scenario():
        w = wake.Waker()
        await w.sync()
        assert sid in w.listening
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
            u.sendto(b"hello", ("127.0.0.1", port))
        await asyncio.sleep(0.3)
        assert woken == [sid] and sid not in w.listening, "listeners are released before the server starts"
        with socket.socket() as t:                # the port is free for acServer now
            t.bind(("127.0.0.1", port))
        # a second attempt right away is inside the cooldown: not listened for again
        await w.sync()
        assert sid not in w.listening
        # ...and a TCP connection also wakes it, after the cooldown
        w.woken[sid] = []
        await w.sync()
        r, wr = await asyncio.open_connection("127.0.0.1", port)
        wr.close()
        await asyncio.sleep(0.3)
        assert woken == [sid, sid]
        w.close()

    asyncio.run(scenario())


def test_no_listening_outside_a_window_or_while_running_or_after_too_many_wakes(monkeypatch):
    monkeypatch.setattr(wake, "HOST", "127.0.0.1")
    monkeypatch.setattr(wake.supervisor, "get", lambda _id: None)
    far, far_port = _server_with_window(6 * 3600)     # opens in 5 h: not now
    near, near_port = _server_with_window(600)

    async def scenario():
        w = wake.Waker()
        await w.sync()
        assert near in w.listening and far not in w.listening

        class Up:
            running = True
        monkeypatch.setattr(wake.supervisor, "get", lambda _id: Up())
        await w.sync()
        assert near not in w.listening, "a running server is not listened for"
        monkeypatch.setattr(wake.supervisor, "get", lambda _id: None)
        w.woken[near] = [time.time() - 100 * i for i in range(1, wake.MAX_PER_HOUR + 1)]
        await w.sync()
        assert near not in w.listening, "at most MAX_PER_HOUR wakes an hour"
        w.close()

    asyncio.run(scenario())
