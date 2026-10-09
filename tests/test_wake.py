import asyncio
import json
import socket
import time

from conftest import ADMIN
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import schedule, wake
from app.services import server_service
from app.db import engine
from app.main import app
from app.models import Server

client = TestClient(app, headers=ADMIN)
AC = "Assetto Corsa Launcher"


def _free_pair() -> int:
    """A port P with P (udp + tcp) and P + 1 (tcp) all free on loopback."""
    while True:
        with socket.socket() as t:
            t.bind(("127.0.0.1", 0))
            p = t.getsockname()[1]
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u, socket.socket() as t2:
                u.bind(("127.0.0.1", p))
                t2.bind(("127.0.0.1", p + 1))
                return p
        except OSError:
            continue


def _set_port(sid: int) -> int:
    port = _free_pair()
    with Session(engine) as s:
        srv = s.get(Server, sid)
        srv.base_port = port
        s.add(srv)
        s.commit()
    return port


def _server_with_window(start_in: float):
    sid = client.post("/api/v1/servers", json={"name": "WakeServer"}).json()["id"]
    port = _set_port(sid)
    eid = client.post("/api/v1/events", json={"title": "Wake R1", "session": {"name": "x", "track": "spa", "cars": ["bmw"]}}).json()["id"]
    r = client.post("/api/v1/schedules", json={"event_id": eid, "server_id": sid, "start_at": time.time() + start_in, "reminders": [], "duration_min": 60})
    assert r.status_code == 201
    return sid, port


def _plain_server(mode: str, **cfg):
    sid = client.post("/api/v1/servers", json={"name": "Lobby", "config": cfg, "entry_list": [
        {"MODEL": "bmw_m3", "SKIN": "red", "DRIVERNAME": "reiL", "TEAM": "OPR"}, {"MODEL": "bmw_m3", "SKIN": ""}]}).json()["id"]
    port = _set_port(sid)
    assert client.put(f"/api/v1/servers/{sid}/wake", json={"mode": mode}).json()["wake"] == mode
    return sid, port


async def _http_get(port: int, path: str, agent: str = AC) -> tuple[int, str]:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(f"GET {path} HTTP/1.1\r\nHost: x\r\nUser-Agent: {agent}\r\n\r\n".encode())
    head = await r.readuntil(b"\r\n\r\n")        # read exactly what the headers announce, as a client of a keep-alive server does
    length = int(next(l.split(b":")[1] for l in head.split(b"\r\n") if l.lower().startswith(b"content-length")))
    body = await r.readexactly(length)
    w.close()
    return int(head.split()[1]), body.decode()


def _udp(port: int, payload: bytes, wait_reply: bool = False):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
        u.settimeout(0.5)
        u.sendto(payload, ("127.0.0.1", port))
        if wait_reply:
            try:
                return u.recvfrom(100)[0]
            except socket.timeout:
                return None


async def _tcp_connect(port: int) -> None:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await asyncio.sleep(0.05)
    w.close()


def _setup(monkeypatch):
    monkeypatch.setattr(wake, "HOST", "127.0.0.1")
    monkeypatch.setattr(wake.supervisor, "get", lambda _id: None)
    woken = []

    async def fake_wake(server_id, now=None):
        woken.append(server_id)
        return True
    monkeypatch.setattr(schedule, "wake", fake_wake)
    return woken


def test_only_assetto_corsa_wakes_the_server(monkeypatch):
    woken = _setup(monkeypatch)
    sid, port = _server_with_window(600)          # starts in 10 min: inside the hour-before window

    async def scenario():
        w = wake.Waker()
        await w.sync()
        assert sid in w.listening
        assert await asyncio.to_thread(_udp, port, b"\xc8", True) == b"\xc8" + (port + 1).to_bytes(2, "little"), "the game's ping is answered like acServer does"
        _udp(port, b"scan"), _udp(port, b"\x38\x00")
        await _tcp_connect(port)                    # a scanner: connects without ever asking the lobby
        await asyncio.sleep(0.3)
        assert woken == [] and sid in w.listening, "neither UDP nor a stranger's connection wakes anything"
        assert (await _http_get(port + 1, "/INFO", agent="curl/8"))[0] == 200
        await _tcp_connect(port)                    # asked the lobby, but not as the game does
        await asyncio.sleep(0.3)
        assert woken == []
        assert (await _http_get(port + 1, "/INFO"))[0] == 200     # the game / Content Manager looks at the lobby...
        await _tcp_connect(port)                    # ...and then connects
        await asyncio.sleep(0.3)
        assert woken == [sid] and sid not in w.listening, "listeners are released before the server starts"
        with socket.socket() as t:                  # the game port is free for acServer now
            t.bind(("127.0.0.1", port))
        w.woken[sid] = []                           # (the cooldown is tested below)
        await w.sync()
        await _tcp_connect(port)
        await asyncio.sleep(0.3)
        assert woken == [sid, sid]
        w.close()

    asyncio.run(scenario())


def test_a_stopped_server_looks_open_and_empty(monkeypatch):
    woken = _setup(monkeypatch)
    sid, port = _plain_server("always", SERVER={"NAME": "Prácticas", "TRACK": "monza", "CONFIG_TRACK": "", "CARS": "bmw_m3", "MAX_CLIENTS": 2,
                                                "PASSWORD": "x"}, PRACTICE={"TIME": 15}, RACE={"LAPS": 5})
    with Session(engine) as db:
        srv = db.get(Server, sid)
        srv.welcome = "Bienvenidos, sin contacto"
        db.add(srv)
        db.commit()

    async def scenario():
        w = wake.Waker()
        await w.sync()                                      # no event at all: "always" holds the ports anyway
        assert sid in w.listening
        code, body = await _http_get(port + 1, "/INFO")
        info = json.loads(body)
        assert code == 200 and info["name"] == "Prácticas" and info["clients"] == 0 and info["maxclients"] == 2 and info["pass"] is True
        assert info["track"] == "monza" and info["cars"] == ["bmw_m3"] and info["sessiontypes"] == [1, 3] and info["durations"] == [15, 5]
        assert info["timeleft"] == 15 * 60 and info["port"] == port and info["cport"] == port + 1
        cars = json.loads((await _http_get(port + 1, "/JSON|76561190000000001"))[1])["Cars"]
        assert [(c["Model"], c["Skin"], c["DriverName"], c["IsConnected"], c["IsEntryList"]) for c in cars] == [
            ("bmw_m3", "red", "reiL", False, True), ("bmw_m3", "", "", False, True)]   # as acServer: every slot is «entry list»
        code, body = await _http_get(port + 1, "/api/details")      # the Content Manager wrapper's page: the lobby info + the description
        det = json.loads(body)
        assert code == 200 and det["name"] == "Prácticas" and det["cport"] == port + 1 and det["description"] == "Bienvenidos, sin contacto"
        # the same shape as acServer's own answer: compact JSON in UTF-8, Date, keep-alive, many requests on one connection
        r, wr = await asyncio.open_connection("127.0.0.1", port + 1)
        for _ in range(2):
            wr.write(b"GET /INFO HTTP/1.1\r\nHost: x\r\nUser-Agent: Assetto Corsa Launcher\r\n\r\n")
            head = (await r.readuntil(b"\r\n\r\n")).decode()
            n = int(next(l.split(":")[1] for l in head.split("\r\n") if l.lower().startswith("content-length")))
            raw = await r.readexactly(n)
            assert "Date: " in head and "Connection" not in head and head.startswith("HTTP/1.1 200 OK")
            assert raw.decode().startswith('{"ip":"","port":') and "Prácticas" in raw.decode() and ", " not in raw.decode()
        wr.close()
        assert woken == [], "looking at the lobby wakes nothing"
        # the real answer of the running server, once saved, is what the lobby shows (with nobody on); the session lengths are the configured ones
        wake._instance_dir(sid).mkdir(parents=True, exist_ok=True)
        (wake._instance_dir(sid) / "info.json").write_text(json.dumps({**info, "name": "Real", "clients": 5, "session": 2, "durations": [10, 10, 30], "timeleft": 7}))
        got = json.loads((await _http_get(port + 1, "/INFO"))[1])
        assert got["name"] == "Real" and got["clients"] == 0 and got["session"] == 0 and got["durations"] == [15, 5] and got["timeleft"] == 15 * 60
        w.close()

    asyncio.run(scenario())


def test_cooldown_and_limits_and_wake_modes(monkeypatch):
    woken = _setup(monkeypatch)
    off, _ = _plain_server("off")
    win, _ = _plain_server("window")
    alw, aport = _plain_server("always")
    far, _ = _server_with_window(6 * 3600)         # its window opens in 5 h

    async def scenario():
        w = wake.Waker()
        await w.sync()
        assert alw in w.listening and not ({off, win, far} & set(w.listening)), "off / window without an event / window not open: no listening"
        await _http_get(aport + 1, "/INFO")
        await _tcp_connect(aport)
        await asyncio.sleep(0.3)
        assert woken == [alw]
        await w.sync()
        assert alw not in w.listening, "inside the cooldown it is not listened for again"
        w.woken[alw] = [time.time() - 100 * i for i in range(1, wake.MAX_PER_HOUR + 1)]
        await w.sync()
        assert alw not in w.listening, "at most MAX_PER_HOUR wakes an hour"

        class Up:
            running = True
        w.woken[alw] = []
        monkeypatch.setattr(wake.supervisor, "get", lambda _id: Up())
        await w.sync()
        assert alw not in w.listening, "a running server is not listened for"
        w.close()
    asyncio.run(scenario())

    started = []

    async def fake_start(sess, server_id):
        started.append(server_id)
    monkeypatch.setattr(server_service, "start", fake_start)
    monkeypatch.undo()
    monkeypatch.setattr(server_service, "start", fake_start)
    monkeypatch.setattr(schedule.supervisor, "get", lambda _id: None)
    assert asyncio.run(schedule.wake(alw)) is True and started == [alw]      # "always" with no event: started as it was left
    assert asyncio.run(schedule.wake(win)) is False and asyncio.run(schedule.wake(off)) is False


def test_starting_a_server_by_hand_frees_the_ports_first(monkeypatch):
    """Pressing «Iniciar» while the manager holds the ports must not leave acServer without its game port (the HTTP port stays ours:
    acServer's own is the internal one)."""
    _setup(monkeypatch)
    sid, port = _plain_server("always")

    async def scenario():
        wake.waker.close()
        w = wake.waker
        await w.sync()
        assert sid in w.listening
        for hook in wake.supervisor.before_start:      # what supervisor.start does right before spawning
            hook(sid)
        await asyncio.sleep(0.1)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u, socket.socket() as t1, socket.socket() as t2:
            u.bind(("127.0.0.1", port)), t1.bind(("127.0.0.1", port)), t2.bind(("127.0.0.1", wake._ports(port)["http_internal"]))   # free now
        await w.sync()
        assert sid not in w.listening, "and they are not taken back while it starts"
        w.holdoff[sid] = 0
        w.close()

    asyncio.run(scenario())



def test_a_running_server_is_relayed_and_still_gets_its_description(monkeypatch):
    """While acServer runs, the public HTTP port is the manager's: /INFO and /JSON come from acServer (its own port number swapped
    for ours), /api/details adds the description."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread
    _setup(monkeypatch)
    sid, port = _plain_server("off", SERVER={"NAME": "Real", "TRACK": "monza"})
    internal = wake._ports(port)["http_internal"]
    with Session(engine) as db:
        srv = db.get(Server, sid)
        srv.welcome = "Reglas: sin contacto"
        db.add(srv)
        db.commit()

    class Fake(BaseHTTPRequestHandler):
        def do_GET(self):
            body = {"/INFO": json.dumps({"name": "Real", "clients": 3, "cport": internal}), "/JSON|1": json.dumps({"Cars": ["real"]})}.get(self.path, "")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *a):
            pass
    up = HTTPServer(("127.0.0.1", internal), Fake)
    Thread(target=up.serve_forever, daemon=True).start()
    monkeypatch.setattr(wake.supervisor, "get", lambda i: type("I", (), {"running": True})())

    async def scenario():
        w = wake.Waker()
        await w.sync()                                        # running: the HTTP port is answered although waking is off
        assert sid in w.http and sid not in w.listening
        info = json.loads((await _http_get(port + 1, "/INFO"))[1])
        assert info["clients"] == 3 and info["cport"] == port + 1          # acServer's answer, with the public port
        assert json.loads((await _http_get(port + 1, "/JSON|1"))[1]) == {"Cars": ["real"]}
        det = json.loads((await _http_get(port + 1, "/api/details"))[1])
        assert det["clients"] == 3 and det["description"] == "Reglas: sin contacto" and det["cport"] == port + 1
        w.close()

    try:
        asyncio.run(scenario())
    finally:
        up.shutdown()


def test_acserver_http_answers_are_reused_for_a_few_seconds(monkeypatch):
    from app import wake
    calls = []

    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body
    monkeypatch.setattr(wake.urllib.request, "urlopen", lambda url, timeout: calls.append(url) or Resp(b'{"clients":1}'))
    wake._upstream_cache.clear()
    assert wake._upstream(9700, "/INFO") == wake._upstream(9700, "/INFO") == b'{"clients":1}' and len(calls) == 1
    wake._upstream(9700, "/JSON|1")   # another path is another entry
    assert len(calls) == 2
    t = wake.time.monotonic()
    monkeypatch.setattr(wake.time, "monotonic", lambda: t + wake.UPSTREAM_TTL + 1)
    wake._upstream(9700, "/INFO")
    assert len(calls) == 3                                   # expired: asked again
    monkeypatch.setattr(wake.urllib.request, "urlopen", lambda url, timeout: (_ for _ in ()).throw(OSError()))
    wake._upstream_cache.clear()
    assert wake._upstream(9700, "/INFO") == b"" and (9700, "/INFO") not in wake._upstream_cache   # failures are not kept
