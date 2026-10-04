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


def _free_pair() -> int:
    """A port P with P (udp + tcp) and P + 1 (tcp) all free."""
    while True:
        p = _free_port()
        with socket.socket() as t:
            try:
                t.bind(("127.0.0.1", p + 1))
                return p
            except OSError:
                continue


def _plain_server(mode: str, **cfg):
    sid = client.post("/api/v1/servers", json={"name": "Lobby", "config": cfg, "entry_list": [
        {"MODEL": "bmw_m3", "SKIN": "red", "DRIVERNAME": "reiL", "TEAM": "OPR"}, {"MODEL": "bmw_m3", "SKIN": ""}]}).json()["id"]
    port = _free_pair()
    with Session(engine) as s:
        srv = s.get(Server, sid)
        srv.base_port = port
        s.add(srv)
        s.commit()
    assert client.put(f"/api/v1/servers/{sid}/wake", json={"mode": mode}).json()["wake"] == mode
    return sid, port


async def _http_get(port: int, path: str) -> tuple[int, str]:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    raw = await r.read(65536)
    w.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split()[1]), body.decode()


def test_a_stopped_server_looks_open_and_empty_and_only_a_connection_wakes_it(monkeypatch):
    import json as _json
    monkeypatch.setattr(wake, "HOST", "127.0.0.1")
    monkeypatch.setattr(wake.supervisor, "get", lambda _id: None)
    woken = []

    async def fake_wake(server_id, now=None):
        woken.append(server_id)
        return True
    monkeypatch.setattr(schedule, "wake", fake_wake)
    sid, port = _plain_server("always", SERVER={"NAME": "Prácticas", "TRACK": "monza", "CONFIG_TRACK": "", "CARS": "bmw_m3", "MAX_CLIENTS": 2,
                                                "PASSWORD": "x"}, PRACTICE={"TIME": 15}, RACE={"LAPS": 5})

    async def scenario():
        w = wake.Waker()
        await w.sync()                                      # no event at all: "always" holds the ports anyway
        assert sid in w.listening
        code, body = await _http_get(port + 1, "/INFO")
        info = _json.loads(body)
        assert code == 200 and info["name"] == "Prácticas" and info["clients"] == 0 and info["maxclients"] == 2 and info["pass"] is True
        assert info["track"] == "monza" and info["cars"] == ["bmw_m3"] and info["sessiontypes"] == [1, 3] and info["durations"] == [15, 5]
        assert info["timeleft"] == 15 * 60 and info["port"] == port and info["cport"] == port + 1
        cars = _json.loads((await _http_get(port + 1, "/JSON|76561190000000001"))[1])["Cars"]
        assert [(c["Model"], c["Skin"], c["DriverName"], c["IsConnected"], c["IsEntryList"]) for c in cars] == [
            ("bmw_m3", "red", "reiL", False, True), ("bmw_m3", "", "", False, False)]
        assert (await _http_get(port + 1, "/api/details")) == (200, "")
        assert woken == [], "looking at the lobby wakes nothing"
        # the real answer of the running server, once saved, is what the lobby shows (with nobody on)
        (wake._instance_dir(sid)).mkdir(parents=True, exist_ok=True)
        (wake._instance_dir(sid) / "info.json").write_text(_json.dumps({**info, "name": "Real", "clients": 5, "session": 2, "durations": [10, 10, 30], "timeleft": 7}))
        got = _json.loads((await _http_get(port + 1, "/INFO"))[1])
        assert got["name"] == "Real" and got["clients"] == 0 and got["session"] == 0 and got["timeleft"] == 600
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
            u.sendto(b"join", ("127.0.0.1", port))
        await asyncio.sleep(0.3)
        assert woken == [sid] and sid not in w.listening
        with socket.socket() as t:                          # all three ports are free for acServer
            t.bind(("127.0.0.1", port + 1))
        w.close()

    asyncio.run(scenario())


def test_wake_modes(monkeypatch):
    monkeypatch.setattr(wake, "HOST", "127.0.0.1")
    monkeypatch.setattr(wake.supervisor, "get", lambda _id: None)
    off, _ = _plain_server("off")
    win, _ = _plain_server("window")
    alw, _ = _plain_server("always")

    async def scenario():
        w = wake.Waker()
        await w.sync()
        assert alw in w.listening and off not in w.listening and win not in w.listening   # "window" needs an event window
        w.close()
    asyncio.run(scenario())

    started = []

    async def fake_start(server_id, sess):
        started.append(server_id)
    monkeypatch.setattr(schedule, "start_server", fake_start)
    assert asyncio.run(schedule.wake(alw)) is True and started == [alw]      # "always" with no event: started as it was left
    assert asyncio.run(schedule.wake(win)) is False and asyncio.run(schedule.wake(off)) is False

