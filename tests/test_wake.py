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
    client.put(f"/api/v1/servers/{sid}/wake", json={"mode": "window"})   # new servers default to always
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


def test_a_stopped_server_books_the_slot_of_the_driver_who_asks(monkeypatch):
    """Locked list, no pickup: Content Manager disables Join («Wait for the next booking») unless /JSON|<steam id> comes back with that driver's slot booked."""
    _setup(monkeypatch)
    sid, port = _plain_server("always", SERVER={"NAME": "Liga", "TRACK": "monza", "CONFIG_TRACK": "", "CARS": "bmw_m3", "MAX_CLIENTS": 3})
    with Session(engine) as db:
        srv = db.get(Server, sid)
        srv.entry_list = [{"MODEL": "bmw_m3", "SKIN": "a", "GUID": "111;222"}, {"MODEL": "bmw_m3", "SKIN": "b", "GUID": "333"}, {"MODEL": "bmw_m3", "SKIN": "c"}]
        db.add(srv)
        db.commit()

    async def asked(path: str) -> list[bool]:
        return [c["IsRequestedGUID"] for c in json.loads((await _http_get(port + 1, path))[1])["Cars"]]

    async def scenario():
        w = wake.Waker()
        await w.sync()
        assert await asked("/JSON|222") == [True, False, False]       # one of the «;» GUIDs of the first slot
        assert await asked("/JSON%7C333") == [False, True, False]     # (the pipe may come percent-encoded)
        assert await asked("/JSON|444") == [False, False, False]      # not on the list: nothing booked for them
        assert await asked("/JSON|222?x=1") == [True, False, False]   # a query string is not part of the id
        assert await asked("/JSON|-1") == [False, False, False] and await asked("/JSON|") == [False, False, False]   # Content Manager without Steam asks with -1; an empty id books nothing
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
        assert code == 200 and info["name"] == f"Prácticas ℹ{port + 1}" and info["clients"] == 0 and info["maxclients"] == 2 and info["pass"] is True
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
        assert got["name"] == f"Real ℹ{port + 1}" and got["clients"] == 0 and got["session"] == 0 and got["durations"] == [15, 5] and got["timeleft"] == 15 * 60
        w.close()

    asyncio.run(scenario())


def test_pending_event_is_visible_in_all_cm_routes_without_loading_or_alerts(monkeypatch):
    _setup(monkeypatch)
    sid, port = _plain_server("always", SERVER={"NAME": "Old", "TRACK": "monza", "CARS": "old"}, PRACTICE={"TIME": 10})
    eid = client.post("/api/v1/events", json={"title": "Next", "session": {
        "name": "Next", "track": "spa", "cars": ["bmw_m3"], "practice_min": 12,
        "race_laps": 8, "welcome": "New rules", "entries": [{"model": "bmw_m3", "guid": "76561190000000001", "driver_name": "Driver"}],
        "locked": True, "pickup": False}}).json()["id"]
    r = client.post("/api/v1/schedules", json={"event_id": eid, "server_id": sid,
                    "start_at": time.time() + 600, "reminders": [], "duration_min": 60})
    assert r.status_code == 201, r.text

    async def scenario():
        w = wake.Waker()
        await w.sync()
        info = json.loads((await _http_get(port + 1, "/INFO"))[1])
        cars = json.loads((await _http_get(port + 1, "/JSON|76561190000000001"))[1])["Cars"]
        det = json.loads((await _http_get(port + 1, "/api/details"))[1])
        assert info["name"] == f"Next ℹ{port + 1}" and info["track"] == "spa"
        assert info["sessiontypes"] == [1, 3] and info["durations"] == [12, 8]
        assert info["maxclients"] == 1 and info["pickup"] is False
        assert cars[0]["IsRequestedGUID"] and cars[0]["Model"] == "bmw_m3"
        assert det["description"] == "New rules" and det["track"] == "spa"
        w.close()

    asyncio.run(scenario())
    from app.models import Schedule
    with Session(engine) as db:
        assert db.get(Schedule, r.json()["id"]).loaded is False


def test_loading_image_validates_bytes_and_exposes_public_url(monkeypatch):
    _setup(monkeypatch)
    sid, port = _plain_server("always", SERVER={"NAME": "Image", "TRACK": "spa", "CARS": "bmw"})
    path = f"/api/v1/servers/{sid}/loading-image"
    assert client.post(path, content=b"not an image").status_code == 400
    assert client.post(path, content=b"x" * (1024 * 1024 + 1)).status_code == 413
    assert TestClient(app).post(path, content=b"not an image").status_code == 401
    assert client.get(path).json() == {"uploaded": False}
    # Minimal valid PNG: complete IHDR, IDAT, IEND with checked CRCs.
    import struct
    import zlib
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"\0\0\0\0")) + chunk(b"IEND", b""))
    # Also verify RGBA (color type 6) works
    png_rgba = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(b"\0" * 36)) + chunk(b"IEND", b""))
    assert client.post(path, content=png_rgba).status_code == 200
    assert client.post(path, content=png).status_code == 200
    assert client.get(path).json() == {"uploaded": True}

    async def scenario():
        w = wake.Waker()
        await w.sync()
        host = f"127.0.0.1:{port + 1}"
        r, wr = await asyncio.open_connection("127.0.0.1", port + 1)
        wr.write(f"GET /api/details HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
        header = await r.readuntil(b"\r\n\r\n")
        body = await r.readexactly(int(next(x.split(b":")[1] for x in header.split(b"\r\n") if x.lower().startswith(b"content-length"))))
        wr.close()
        assert json.loads(body)["loadingImageUrl"] == f"http://{host}/loading-image"

        # Wrong port in host header must omit loadingImageUrl
        r, wr = await asyncio.open_connection("127.0.0.1", port + 1)
        wr.write(f"GET /api/details HTTP/1.1\r\nHost: 127.0.0.1:9999\r\nConnection: close\r\n\r\n".encode())
        header = await r.readuntil(b"\r\n\r\n")
        body = await r.readexactly(int(next(x.split(b":")[1] for x in header.split(b"\r\n") if x.lower().startswith(b"content-length"))))
        wr.close()
        assert "loadingImageUrl" not in json.loads(body)

        reader, writer = await asyncio.open_connection("127.0.0.1", port + 1)
        writer.write(b"GET /loading-image HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        image_headers = await reader.readuntil(b"\r\n\r\n")
        image_body = await reader.readexactly(int(next(x.split(b":")[1] for x in image_headers.split(b"\r\n") if x.lower().startswith(b"content-length"))))
        writer.close()
        assert b"image/png" in image_headers and image_body == png
        w.close()
    asyncio.run(scenario())


def test_content_manager_hibernation_and_loading_image_e2e(monkeypatch):
    """Verify Content Manager full query flow (INFO -> details -> JSON|guid -> loading image) against hibernation facade."""
    _setup(monkeypatch)
    sid, port = _plain_server("always", SERVER={"NAME": "OPR Champ", "TRACK": "monza", "CARS": "bmw"}, PRACTICE={"TIME": 10})
    guid = "76561198000000001"
    eid = client.post("/api/v1/events", json={
        "title": "Round 1",
        "session": {
            "name": "GT3 Round 1",
            "track": "spa",
            "track_config": "",
            "cars": ["bmw_m3_gt2"],
            "practice_min": 15,
            "qualify_min": 10,
            "race_laps": 12,
            "welcome": "[b]OPR GT3 Championship[/b]\n[color=#00d2be]Respetad los límites de pista.[/color]",
            "entries": [{"model": "bmw_m3_gt2", "guid": guid, "driver_name": "Juan Arias"}],
            "locked": True,
            "pickup": False,
        },
    }).json()["id"]

    client.post("/api/v1/schedules", json={
        "event_id": eid, "server_id": sid, "start_at": time.time() + 600, "reminders": [], "duration_min": 60,
    })

    import struct
    import zlib
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff)
    png_rgba = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 32, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(b"\0" * (32 * (1 + 32 * 4)))) + chunk(b"IEND", b""))
    assert client.post(f"/api/v1/servers/{sid}/loading-image", content=png_rgba).status_code == 200

    async def scenario():
        w = wake.Waker()
        await w.sync()
        http_port = port + 1
        host = f"127.0.0.1:{http_port}"

        # 1. Content Manager queries /INFO
        r, wr = await asyncio.open_connection("127.0.0.1", http_port)
        wr.write(f"GET /INFO HTTP/1.1\r\nHost: {host}\r\nUser-Agent: AssettoCorsaClient\r\nConnection: close\r\n\r\n".encode())
        headers = await r.readuntil(b"\r\n\r\n")
        body = json.loads(await r.read())
        wr.close()
        assert f"ℹ{http_port}" in body["name"] and body["track"] == "spa" and body["pickup"] is False
        assert body["sessiontypes"] == [1, 2, 3] and body["durations"] == [15, 10, 12]

        # 2. Content Manager queries /api/details
        r, wr = await asyncio.open_connection("127.0.0.1", http_port)
        wr.write(f"GET /api/details?guid={guid} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
        headers = await r.readuntil(b"\r\n\r\n")
        body = json.loads(await r.read())
        wr.close()
        assert "[b]OPR GT3 Championship[/b]" in body["description"]
        assert body["loadingImageUrl"] == f"http://{host}/loading-image"

        # 3. Content Manager queries /JSON|<guid> to check booked slot for locked league
        r, wr = await asyncio.open_connection("127.0.0.1", http_port)
        wr.write(f"GET /JSON|{guid} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
        headers = await r.readuntil(b"\r\n\r\n")
        body = json.loads(await r.read())
        wr.close()
        booked = [c for c in body["Cars"] if c["IsRequestedGUID"]]
        assert len(booked) == 1 and booked[0]["Model"] == "bmw_m3_gt2" and booked[0]["DriverName"] == "Juan Arias"

        # 4. Content Manager downloads loading image for join screen
        r, wr = await asyncio.open_connection("127.0.0.1", http_port)
        wr.write(f"GET /loading-image HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
        headers = await r.readuntil(b"\r\n\r\n")
        img_data = await r.read()
        wr.close()
        assert img_data == png_rgba and b"image/png" in headers
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


def test_a_running_server_only_relays_info_and_json(monkeypatch):
    """The public HTTP port has no login: while acServer runs it is relayed ONLY for /INFO and /JSON (what the lobby asks); any other path gets the
    same empty 200 as a stopped server, never reaches acServer's internal port, and a malformed one (no leading slash: urllib raises InvalidURL, which is
    not an OSError) leaves no unhandled exception in the connection task."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread
    _setup(monkeypatch)
    sid, port = _plain_server("off", SERVER={"NAME": "Real", "TRACK": "monza"})
    internal = wake._ports(port)["http_internal"]
    seen = []

    class Fake(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            body = {"/INFO": json.dumps({"name": "Real", "clients": 3, "cport": internal}), "/JSON|1": json.dumps({"Cars": ["real"]})}.get(self.path, "SECRET")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *a):
            pass
    up = HTTPServer(("127.0.0.1", internal), Fake)
    Thread(target=up.serve_forever, daemon=True).start()
    monkeypatch.setattr(wake.supervisor, "get", lambda i: type("I", (), {"running": True})())
    wake._upstream_cache.clear()
    assert wake._upstream(internal, ".evil.com/") == b"" and wake._upstream(internal, "/a b") == b""   # a bad path is an empty answer, not an exception

    async def scenario():
        errors = []
        asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: errors.append(ctx))
        w = wake.Waker()
        await w.sync()
        assert json.loads((await _http_get(port + 1, "/INFO"))[1])["clients"] == 3
        assert json.loads((await _http_get(port + 1, "/JSON|1"))[1]) == {"Cars": ["real"]}   # what the lobby asks still arrives intact
        for path in ("/secret", "/admin", ".evil.com/", "@127.0.0.1:1/x"):
            status, body = await _http_get(port + 1, path)
            assert status == 200 and body == "" or body == b"", (path, status, body)
        await asyncio.sleep(0.2)
        assert errors == [], errors   # no connection task died with an exception
        w.close()

    try:
        asyncio.run(scenario())
    finally:
        up.shutdown()
    assert seen == ["/INFO", "/JSON|1"], seen   # nothing but what the lobby asks ever reached acServer's internal port


def test_content_manager_finds_the_description_through_the_name_marker(monkeypatch):
    """Content Manager ignores the "extra" flag of /INFO: it reads the port of /api/details from the END OF THE SERVER NAME ("<name> ℹ<port>", ServerEntry.CleanUp in
    gro-ove/actools) and then asks that port. So /INFO must carry the marker, pointing at the port that really answers /api/details, whose answer repeats it as
    `wrappedPort` (ServerInformationExtended.PortExtended) and keeps the CLEAN name, because the league site shows it. Same while acServer runs."""
    _setup(monkeypatch)
    sid, port = _plain_server("always", SERVER={"NAME": "Liga OPR", "TRACK": "monza"})   # "always": a stopped server holds its ports
    with Session(engine) as db:
        srv = db.get(Server, sid)
        srv.welcome = "Reglas: sin contacto"
        db.add(srv)
        db.commit()

    async def scenario():
        w = wake.Waker()
        await w.sync()
        info = json.loads((await _http_get(port + 1, "/INFO"))[1])
        name, _, marked_port = info["name"].partition("ℹ")                         # how Content Manager splits it
        assert name.strip() == "Liga OPR" and int(marked_port) == port + 1, info["name"]
        det = json.loads((await _http_get(int(marked_port), "/api/details"))[1])      # and it asks THAT port
        assert det["description"].startswith("Reglas: sin contacto") and det["wrappedPort"] == int(marked_port) and det["name"] == "Liga OPR", det
        w.close()

    asyncio.run(scenario())


def test_the_name_marker_is_replaced_never_duplicated_and_real_names_survive():
    s = type("S", (), {"base_port": 9600})()
    http = wake._ports(9600)["http"]
    for given, wanted in (("Liga OPR", "Liga OPR"), ("Liga OPR ℹ9601", "Liga OPR"), ("Liga OPR ℹ9999", "Liga OPR"), ("Liga OPR ℹ1ℹ2", "Liga OPR ℹ1"),
                          ("Liga ℹ información", "Liga ℹ información"), ("", ""), (None, "")):
        got = wake.lobby_info(s, {"name": given})["name"]
        assert got == f"{wanted} ℹ{http}".lstrip() or got == f" ℹ{http}", (given, got)     # exactly one marker, always ours
        assert got.count("ℹ") == wanted.count("ℹ") + 1, (given, got)
        assert wake.clean_name(got) == wanted, (given, got)                                   # and /api/details gets the clean name back
    assert wake.lobby_info(s, {})["name"].endswith(f"ℹ{http}")                                 # a missing name does not crash
