"""Drivers without the game (ops/fake_client.py) in the REAL acServer of the local simulator: what a player's client would receive
and decide when the sessions change and the grid of the race is built (qualifying order, custom grid by times, F1 style Q1/Q2/Q3 with the same type repeated).

What the real client does with it was read in the decompiled acs.exe (ACClient::onNewSession / getCurrentSessionIndex / convertRemoteSession):
the fake driver mirrors those rules, so these tests assert what a player would see. They do NOT cover the CSP hub (Lua, needs the game).
Uses the ports of the simulator cfg (19600 game, 19601/19602 plugin): do not run two of these at once."""
import importlib.util
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app import gridtimes, sessionpatch
from app.live import acsp

SIM = Path(os.environ.get("ACM_TEST_ACSERVER_SIM", Path.home() / "Documents/acserver-re/sim"))

_spec = importlib.util.spec_from_file_location("fake_client", Path(__file__).resolve().parent.parent / "ops" / "fake_client.py")
fc = importlib.util.module_from_spec(_spec)
sys.modules["fake_client"] = fc
_spec.loader.exec_module(fc)

GUIDS = [76561198000000001, 76561198000000002, 76561198000000003]
NAMES = ["Ana", "Beto", "Carla"]


class Rig:
    """A copy of the simulator (guids in the entry list: the server only lets in the ones it knows) + a plugin socket to command it."""

    def __init__(self, tmp: Path, plugin: bool = True):
        if not (SIM / "acServer").exists():
            pytest.skip("needs the local acServer simulator (ACM_TEST_ACSERVER_SIM)")
        with socket.socket() as probe:                 # never talk to a server that is not ours (the ports are the simulator's fixed ones)
            if probe.connect_ex(("127.0.0.1", 19600)) == 0:
                pytest.fail("port 19600 is already taken by another server: stop it first")
        self.drivers: list = []
        self.srv = None
        self.plugin = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.log = open(tmp / "srv.log", "w")
        self._stop = threading.Event()
        try:
            if plugin:   # the manager's own ACSPClient takes that port when it is in the test
                self.plugin.bind(("127.0.0.1", 19602))
            self.dir = tmp / "sim"
            shutil.copytree(SIM, self.dir, ignore=shutil.ignore_patterns("logs", "results", "*.log"))
            (self.dir / "logs").mkdir(exist_ok=True)
            (self.dir / "results").mkdir(exist_ok=True)
            entry = (self.dir / "cfg" / "entry_list.ini")
            it = iter(GUIDS)
            entry.write_text(re.sub(r"GUID=.*\n", lambda m: f"GUID={next(it)}\n", entry.read_text()))
            cfg = self.dir / "cfg" / "server_cfg.ini"      # short sessions and waits: a timed session ends by itself in a minute
            cfg.write_text(re.sub(r"(?m)^(TIME=)\d+", r"\g<1>1", re.sub(r"(?m)^WAIT_TIME=\d+", "WAIT_TIME=5", re.sub(r"(?m)^RESULT_SCREEN_TIME=\d+", "RESULT_SCREEN_TIME=3", cfg.read_text()))))
            self.srv = subprocess.Popen(["./acServer"], cwd=self.dir, stdout=self.log, stderr=subprocess.STDOUT)   # our child: /proc/pid/mem and ptrace work
            end = time.time() + 15
            while True:
                assert self.srv.poll() is None, "the simulator's acServer died at start (see srv.log)"
                try:
                    socket.create_connection(("127.0.0.1", 19600), 0.5).close()
                    break
                except OSError:
                    assert time.time() < end, "the simulator's acServer never opened its port"
                    time.sleep(0.2)
            time.sleep(1)
        except BaseException:
            self.close()
            raise

    def send(self, data: bytes):
        self.plugin.sendto(data, ("127.0.0.1", 19601))

    def join(self, n: int = 3):
        for i in range(n):
            d = fc.FakeDriver("127.0.0.1", 19600, guid=GUIDS[i], name=NAMES[i]).connect()
            self.drivers.append(d)
            threading.Thread(target=self._drive, args=(d,), daemon=True).start()
        return self.drivers

    def _drive(self, d):
        while not self._stop.is_set():
            try:
                d.update()
            except OSError:
                return
            time.sleep(1 / 18)

    def next_session(self, wait_for: int | None = None):
        """NEXT_SESSION and wait until EVERY driver has read the NEW_SESSION it causes. The first one right after the patch + restart can be
        swallowed by acServer, so it is sent again (the manager's timeline.resume does the same: it repeats until the session is the one it wants)."""
        have = [d.new_sessions for d in self.drivers]
        for _ in range(3):
            self.send(acsp.encode_next_session())
            end = time.time() + 3
            while time.time() < end and any(d.new_sessions == h for d, h in zip(self.drivers, have)):
                time.sleep(0.05)
            if all(d.new_sessions > h for d, h in zip(self.drivers, have)):
                break
        assert all(d.new_sessions > h for d, h in zip(self.drivers, have)), "a driver never got the new session"
        assert all(d.new_sessions == have[0] + 1 for d in self.drivers), "NEXT_SESSION moved more than one session"
        time.sleep(0.3)

    def laps(self, ms_by_slot: dict[int, list[int]]):
        for slot, laps in ms_by_slot.items():
            for ms in laps:
                self.drivers[slot].lap(ms)
        time.sleep(0.5)

    def close(self):
        if getattr(self, "_closed", False):
            return
        self._closed = True
        self._stop.set()
        for d in self.drivers:
            d.disconnect()
        if self.srv:
            self.srv.terminate()
            self.srv.wait(5)
            time.sleep(1)      # let the ports go before the next test starts its own server
        self.plugin.close()
        self.log.close()


def delivered(rig, session_type="PRACTICE"):
    """{guid: best lap ms} of the newest result file of this type in the simulator: after the adjusting practice it holds what the manager wrote (1 ms, 2 ms, 3 ms ...). Waits for the file to be there and whole."""
    import json
    for _ in range(20):
        files = sorted((rig.dir / "results").glob(f"*_{session_type}.json"), key=lambda p: p.stat().st_mtime)
        try:
            doc = json.loads(files[-1].read_text(encoding="utf-8-sig"))
            return {r["DriverGuid"]: r["BestLap"] for r in doc["Result"]}
        except (IndexError, ValueError, KeyError):
            time.sleep(0.5)
    raise AssertionError("the adjusting practice has no (whole) result file")


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    try:
        yield r
    finally:
        r.close()


def test_the_handshake_gives_each_driver_a_slot_and_the_session_list(rig):
    ds = rig.join()
    assert [d.session_id for d in ds] == [0, 1, 2]
    assert [s.type for s in ds[0].sessions] == [fc.PRACTICE, fc.QUALIFY, fc.RACE]
    assert (ds[0].session_type, ds[0].spawn) == (fc.PRACTICE, "PIT")
    assert not any(d.refused for d in ds)


def test_the_race_grid_follows_the_qualifying_laps(rig):
    ds = rig.join()
    rig.laps({0: [91000, 90500], 1: [88000, 89000], 2: [92000, 87000]})      # practice
    rig.next_session()
    assert all(d.session_type == fc.QUALIFY and d.spawn == "PIT" for d in ds)
    assert ds[0].grid_list == [2, 1, 0]                                     # the order of the practice, the best lap of each
    rig.laps({0: [90500], 1: [88000], 2: [87000]})                          # qualify: Carla, Beto, Ana
    rig.next_session()
    assert all(d.session_type == fc.RACE and d.spawn == "START" for d in ds)
    assert ds[0].grid_list == [2, 1, 0]
    assert [d.grid_slot for d in ds] == [2, 1, 0]                           # each one puts himself in the slot of his place in the list
    assert "DISCARDED" not in Path(rig.log.name).read_text()                # all the laps counted


def test_a_custom_grid_by_times_reaches_every_client_as_its_start_slot(rig):
    """The director's order (gridtimes: 1 ms, 2 ms ... into acServer's BestLaps) beats the laps the drivers really did."""
    ds = rig.join()
    rig.next_session()                                                       # practice -> qualify
    rig.laps({0: [87000], 1: [88000], 2: [89000]})                           # the real qualifying says Ana, Beto, Carla
    want = [1, 2, 0]                                                         # ... the custom grid says Beto, Carla, Ana
    mem = gridtimes.Mem(rig.srv.pid)
    try:
        gridtimes.apply(mem, {slot: i + 1 for i, slot in enumerate(want)}, session_index=1)
    finally:
        mem.close()
    rig.next_session()
    assert ds[0].grid_list == want
    assert [d.grid_slot for d in ds] == [2, 0, 1]                            # Ana last, Beto first, Carla second


def test_a_grid_list_always_names_every_connected_car_once(rig):
    """The real client dereferences the car of each id of the list: a missing or repeated id is a crash risk there (Codex's audit of onNewSession)."""
    ds = rig.join()
    for _ in range(2):
        rig.next_session()
        for d in ds:
            assert sorted(d.grid_list) == [0, 1, 2]


F1 = [
    {"kind": "practice", "name": "Practice", "time_min": 1, "laps": 0, "wait_s": 0},
    {"kind": "qualify", "name": "Q1", "time_min": 1, "laps": 0, "wait_s": 0},
    {"kind": "qualify", "name": "Q2", "time_min": 1, "laps": 0, "wait_s": 0},
    {"kind": "qualify", "name": "Q3", "time_min": 1, "laps": 0, "wait_s": 0},
    {"kind": "race", "name": "Race", "time_min": 0, "laps": 2, "wait_s": 0},
]


def test_f1_style_sequence_three_qualifyings_then_the_race_with_connected_drivers(rig):
    """Q1 Q2 Q3 are three sessions of the SAME type: the client must treat each as a qualifying (spawn PIT, fresh laps) and the race as START."""
    # like the manager: the patch goes in at start, BEFORE anybody joins (a client already inside keeps the list it got when it joined)
    threading.Timer(1.0, lambda: rig.send(acsp.encode_restart_session())).start()
    assert sessionpatch.grow(rig.srv.pid, sessionpatch.layout_for(F1, [1, 2, 3]), 20).startswith("grown to 5 sessions")
    for i, st in enumerate(F1):
        rig.send(acsp.encode_set_session_info(i, st["name"], sessionpatch.TYPES[st["kind"]], st["laps"], st["time_min"], st["wait_s"]))
        time.sleep(0.1)
    time.sleep(2)
    ds = rig.join()
    assert [s.type for s in ds[0].sessions] == [1, 2, 2, 2, 3]
    seen = []
    for step in range(4):
        rig.next_session()
        rig.laps({0: [91000 - 500 * step], 1: [90000 - 500 * step], 2: [89000 - 500 * step]})
        seen.append((ds[0].current.name, ds[0].session_type, ds[0].spawn, ds[0].session_index))
    assert [s[:3] for s in seen] == [("Q1", 2, "PIT"), ("Q2", 2, "PIT"), ("Q3", 2, "PIT"), ("Race", 3, "START")], seen
    # known cosmetic limit of the real client: it finds the index of the session by TYPE, so Q1, Q2 and Q3 all say the index of Q1 (Codex, getCurrentSessionIndex)
    assert [s[3] for s in seen[:3]] == [1, 1, 1]
    assert sorted(ds[0].grid_list) == [0, 1, 2]
    assert all(d.grid_slot is not None for d in ds)


def test_the_manager_writes_the_saved_grid_by_itself_in_the_last_seconds_of_the_qualifying(tmp_path, monkeypatch):
    """The whole manager side: ACSPClient (the real one, on acServer's plugin port) + gridtimes.on_session with an order saved in the database.
    Nobody runs a command: the order is written into acServer's times in the last 5 s of a timed qualifying whose next session is the race (about 65 s of real time)."""
    import asyncio
    from app.live import acsp as acsp_mod
    SID = 7701
    gridtimes.set_order(SID, [str(GUIDS[2]), str(GUIDS[0]), str(GUIDS[1])], "manual")     # Carla, Ana, Beto
    rig = Rig(tmp_path, plugin=False)
    from types import SimpleNamespace
    from app import supervisor
    monkeypatch.setattr(supervisor, "get", lambda sid: SimpleNamespace(pid=rig.srv.pid, started_at=None))   # the process the supervisor would own

    async def run():
        client = await acsp_mod.connect(SID, remote_port=19601, local_port=19602, car_slots=3)
        try:
            ds = await asyncio.to_thread(rig.join)
            await asyncio.sleep(1)
            client.send(acsp.encode_next_session())                                          # practice -> qualify (1 min)
            end = time.time() + 15
            while time.time() < end and not all(d.session_type == fc.QUALIFY for d in ds):
                await asyncio.sleep(0.2)
            assert all(d.session_type == fc.QUALIFY for d in ds)
            for slot, ms in {0: 87000, 1: 88000, 2: 89000}.items():                          # the REAL qualifying says Ana, Beto, Carla
                ds[slot].lap(ms)
            end = time.time() + 90
            while time.time() < end and not all(d.session_type == fc.RACE for d in ds):
                await asyncio.sleep(0.5)
            return ds
        finally:
            if client.transport:
                client.transport.close()
            for t in (client.grid_task, getattr(client, "_hello", None), getattr(client, "_sweep", None)):
                if t:
                    t.cancel()
    try:
        ds = asyncio.run(run())
        assert all(d.session_type == fc.RACE for d in ds), "the race never started"
        assert ds[0].grid_list == [2, 0, 1], ds[0].grid_list                                 # the saved order, not the laps
        assert [d.grid_slot for d in ds] == [1, 2, 0]
    finally:
        rig.close()
        gridtimes.clear_order(SID)


# --- the wire format and the framing of the fake driver itself: no simulator needed -------------------------------------------------------------------------------------

def _s8(t):
    b = t.encode()
    return bytes([len(b)]) + b


def test_handshake_and_new_session_are_read_the_way_the_wire_format_says():
    import struct
    d = fc.FakeDriver("x", 1, guid=1, name="n")
    hs = (fc._s32("Srv") + struct.pack("<HB", 9600, 18) + _s8("spa") + _s8("") + _s8("car") + _s8("skin") + struct.pack("<f", 0.5)
          + struct.pack("<hBBBBBB", 2, 1, 1, 1, 1, 0, 0) + struct.pack("<fff", 1, 1, 1) + bytes([1, 255])
          + struct.pack("<ii", 5000, 3000) + bytes([0, 0]) + struct.pack("<hhh", 0, 0, 0)
          + bytes([4, 3]) + struct.pack("<BHH", 1, 0, 10) + struct.pack("<BHH", 2, 0, 5) + struct.pack("<BHH", 3, 8, 0)
          + _s8("Practice") + struct.pack("<BBHH", 0, 1, 10, 0) + struct.pack("<f", 1.0) + bytes([4]) + struct.pack("<q", 1234) + bytes([2]) + _s8("a/b") + _s8("c"))
    d._parse_handshake(fc._Reader(hs))
    assert (d.session_id, d.udp_port, d.track, d.car_model) == (4, 9600, "spa", "car")
    assert [(s.type, s.laps, s.time) for s in d.sessions] == [(1, 0, 10), (2, 0, 5), (3, 8, 0)]
    assert (d.current.name, d.current.type, d.race_over_ms, d.session_time_ms, d.checksum_paths) == ("Practice", 1, 5000, 1234, ["a/b", "c"])
    ns = bytes([fc.CURRENT_SESSION]) + _s8("Race") + struct.pack("<BBHH", 2, 3, 0, 8) + struct.pack("<f", 1.0) + bytes([2, 4, 1]) + struct.pack("<q", 99)
    d._on_tcp(ns)
    assert (d.current.type, d.spawn, d.grid_list, d.new_sessions) == (3, "START", [2, 4, 1], 1)
    assert d.grid_slot == 1                                     # my slot (4, from the handshake) is the second of the list
    d._on_tcp(bytes([fc.CURRENT_SESSION]) + _s8("Q") + struct.pack("<BBHH", 1, 2, 5, 0) + struct.pack("<f", 1.0) + bytes([4, 2, 1]) + struct.pack("<q", 100))
    assert (d.spawn, d.session_index) == ("PIT", 1)             # a qualifying: the pits; the index is found by TYPE in the handshake list


def test_a_tcp_frame_that_arrives_in_pieces_is_not_torn_by_a_timeout():
    import struct
    a, b = socket.socketpair()
    d = fc.FakeDriver("x", 1, guid=1, name="n")
    d.tcp = a
    a.settimeout(0.1)
    frame = struct.pack("<H", 5) + b"\x4a123\x07"
    b.sendall(frame[:3])                                        # the header and one byte of the body
    with pytest.raises(socket.timeout):
        d._read_tcp()
    b.sendall(frame[3:] + struct.pack("<H", 1) + b"\x4d")      # the rest, and the next frame right behind
    assert d._read_tcp() == b"\x4a123\x07"
    assert d._read_tcp() == b"\x4d"
    a.close(); b.close()


# --- the F1 style qualifying (Q1, Q2, Q3) played by the real manager and three fake drivers --------------------------------------------------------------------------

F1_LIVE = [
    {"kind": "practice", "name": "Practice", "time_min": 1, "laps": 0, "wait_s": 0},
    {"kind": "qualify", "name": "Q1", "time_min": 1, "laps": 0, "wait_s": 0, "stage": 1, "cut": 1},
    {"kind": "qualify", "name": "Q2", "time_min": 1, "laps": 0, "wait_s": 0, "stage": 2, "cut": 1},
    {"kind": "qualify", "name": "Q3", "time_min": 1, "laps": 0, "wait_s": 0, "stage": 3},
    {"kind": "practice", "name": "Ajuste", "time_min": 1, "laps": 0, "wait_s": 0},
    {"kind": "race", "name": "Race", "time_min": 0, "laps": 2, "wait_s": 0},
]


def test_f1_qualifying_the_manager_holds_the_cut_cars_and_the_race_grid_is_the_knockouts(tmp_path, monkeypatch, request):
    """Q1 cuts the slowest (Ana), Q2 the slower of the two left (Beto), Q3 is Carla's: the manager must say who is held in each stage (pit_hold, what the hub obeys)
    and the grid of the race must be Carla, Beto, Ana even though the slots go 0, 1, 2. About 3 minutes of real time (the adjusting practice is timed and the stages must end in different minutes, like the real ones)."""
    import asyncio
    from types import SimpleNamespace

    from conftest import ADMIN
    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from app import knockoutlive, supervisor
    from app.config import settings
    from app.db import engine
    from app.live import acsp as acsp_mod
    from app.main import app
    from app.models import Server

    sid = TestClient(app, headers=ADMIN).post("/api/v1/servers", json={"name": "F1 live"}).json()["id"]
    with Session(engine) as db:
        srv = db.get(Server, sid)
        srv.session = {"sequence": F1_LIVE}
        db.add(srv)
        db.commit()
    rig = Rig(tmp_path, plugin=False)
    request.addfinalizer(rig.close)   # released even when the preparation below fails
    results_dir = Path(settings.data_dir) / "instances" / str(sid) / "results"       # the folder the manager reads is the one acServer writes
    results_dir.parent.mkdir(parents=True, exist_ok=True)
    if results_dir.exists() or results_dir.is_symlink():
        shutil.rmtree(results_dir, ignore_errors=True) if not results_dir.is_symlink() else results_dir.unlink()
    results_dir.symlink_to(rig.dir / "results")
    monkeypatch.setattr(supervisor, "get", lambda s: SimpleNamespace(pid=rig.srv.pid, started_at=None))
    sender = {}

    async def run():
        # like the manager at start: the patch goes in before anybody joins, then every step is defined
        loop = asyncio.get_running_loop()

        def patch():
            threading.Timer(1.0, lambda: rig.send(acsp.encode_restart_session())).start()
            assert sessionpatch.grow(rig.srv.pid, sessionpatch.layout_for(F1_LIVE, [1, 2, 3]), 20).startswith("grown to 6 sessions")
        plug = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)       # a plain socket for the patch commands: the manager's client owns 19602 afterwards
        rig.plugin = plug
        plug.bind(("127.0.0.1", 19602))
        await loop.run_in_executor(None, patch)
        for i, st in enumerate(F1_LIVE):
            rig.send(acsp.encode_set_session_info(i, st["name"], sessionpatch.TYPES[st["kind"]], st["laps"], st["time_min"], st["wait_s"]))
            await asyncio.sleep(0.1)
        await asyncio.sleep(2)
        plug.close()
        client = await acsp_mod.connect(sid, remote_port=19601, local_port=19602, car_slots=3)
        out = {}
        try:
            ds = await asyncio.to_thread(rig.join)
            await asyncio.sleep(2)

            last_end = {"minute": None}

            async def advance(name, ends_a_stage=False):
                if ends_a_stage:   # acServer names a result file by the MINUTE (2026_11_19_12_21_QUALIFY.json): two stages ending in one minute would be one file (real stages last 13+ minutes)
                    while last_end["minute"] == time.strftime("%H%M"):
                        await asyncio.sleep(1)
                before = [d.new_sessions for d in ds]
                for _ in range(3):
                    client.send(acsp.encode_next_session())
                    end = time.time() + 3
                    while time.time() < end and any(d.new_sessions == b for d, b in zip(ds, before)):
                        await asyncio.sleep(0.1)
                    if all(d.new_sessions > b for d, b in zip(ds, before)):
                        break
                assert ds[0].current.name == name, (ds[0].current, name)
                if ends_a_stage:
                    last_end["minute"] = time.strftime("%H%M")           # taken after the new session: the file of the stage that ended was written before it
                await asyncio.sleep(1.5)                              # the manager reads the new session and saves the cycle

            def hold():
                return knockoutlive.hold_view(sid, client.session, client.cars)

            await advance("Q1")
            assert hold() is None                                       # nobody is held in Q1
            for slot, ms in {0: 90000, 1: 88000, 2: 89000}.items():
                ds[slot].lap(ms)
            await asyncio.sleep(1)
            await advance("Q2", True)                                   # Q1's file is written now (and the event reaches the manager)
            await asyncio.sleep(2)
            out["q2"] = await asyncio.to_thread(hold)
            for slot, ms in {1: 87000, 2: 86000}.items():              # Ana is held and does not run
                ds[slot].lap(ms)
            await asyncio.sleep(1)
            await advance("Q3", True)
            await asyncio.sleep(2)
            out["q3"] = await asyncio.to_thread(hold)
            ds[2].lap(85000)
            await asyncio.sleep(1)
            await advance("Ajuste", True)
            for d in ds:
                d.lap(95000)                                        # a real lap (slower than the 1 ms, 2 ms ... the manager writes): the practice has a result file, as a real one does
            await asyncio.sleep(2)
            out["grid_now"] = await asyncio.to_thread(knockoutlive.grid_now, sid)
            end = time.time() + 90                                      # the adjusting practice ends by itself: gridtimes writes the knockout's grid in its last seconds
            while time.time() < end and not all(d.session_type == fc.RACE for d in ds):
                await asyncio.sleep(0.5)
            out["race"] = [d.session_type for d in ds]
            out["grid_list"] = ds[0].grid_list
            out["slots"] = [d.grid_slot for d in ds]
            out["delivered"] = delivered(rig)
            return out
        finally:
            if client.transport:
                client.transport.close()
            for t in (client.grid_task, getattr(client, "_hello", None), getattr(client, "_sweep", None)):
                if t:
                    t.cancel()
    try:
        out = asyncio.run(run())
    finally:
        rig.close()
        knockoutlive.clear(sid)
    guid = lambda i: str(GUIDS[i])
    assert out["q2"] == {"stage": 2, "cars": [0]}, out                    # Ana, the slowest of Q1, is held in Q2
    assert out["q3"] == {"stage": 3, "cars": [0, 1]}, out                 # Ana (Q1) and Beto (Q2) are held in Q3
    assert out["grid_now"] == [guid(2), guid(1), guid(0)], out            # Carla, Beto, Ana
    assert out["race"] == [fc.RACE] * 3, out
    assert out["grid_list"] == [2, 1, 0] and out["slots"] == [2, 1, 0], out
    assert out["delivered"] == {guid(2): 1, guid(1): 2, guid(0): 3}, out   # the manager wrote the knockout grid as 1 ms, 2 ms, 3 ms in the adjusting practice (the grid acServer carries from Q3 can be the same by chance)


# --- the IndyCar style qualifying (single runs) played by the real manager and three fake drivers --------------------------------------------------------------------

INDY_LIVE = [
    {"kind": "practice", "name": "Practice", "time_min": 1, "laps": 0, "wait_s": 0},
    {"kind": "qualify", "name": "Indy", "time_min": 1, "laps": 0, "wait_s": 0, "run_interval_s": 10, "run_warmup": 1, "run_laps": 2},
    {"kind": "practice", "name": "Ajuste", "time_min": 1, "laps": 0, "wait_s": 0},
    {"kind": "race", "name": "Race", "time_min": 0, "laps": 2, "wait_s": 0},
]


def test_indy_qualifying_cars_leave_one_by_one_are_held_after_their_run_and_the_grid_is_by_total_time(tmp_path, monkeypatch, request):
    """Single runs every 10 s: the cars that have not left (and the ones that already ran) are in pit_hold, the ranking is the total of the 2 timed laps (the first lap of a run is the warm-up) and the race grid is that.
    About 2 minutes of real time (the qualifying and the adjusting practice are timed)."""
    import asyncio
    from types import SimpleNamespace

    from conftest import ADMIN
    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from app import singlerunlive, supervisor
    from app.config import settings
    from app.db import engine
    from app.live import acsp as acsp_mod
    from app.main import app
    from app.models import Server

    sid = TestClient(app, headers=ADMIN).post("/api/v1/servers", json={"name": "Indy live"}).json()["id"]
    with Session(engine) as db:
        srv = db.get(Server, sid)
        srv.session = {"sequence": INDY_LIVE}
        db.add(srv)
        db.commit()
    rig = Rig(tmp_path, plugin=False)
    request.addfinalizer(rig.close)   # released even when the preparation below fails
    results_dir = Path(settings.data_dir) / "instances" / str(sid) / "results"
    results_dir.parent.mkdir(parents=True, exist_ok=True)
    if results_dir.is_symlink():
        results_dir.unlink()
    elif results_dir.exists():
        shutil.rmtree(results_dir, ignore_errors=True)
    results_dir.symlink_to(rig.dir / "results")
    monkeypatch.setattr(supervisor, "get", lambda s: SimpleNamespace(pid=rig.srv.pid, started_at=None))

    async def run():
        loop = asyncio.get_running_loop()
        plug = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rig.plugin = plug
        plug.bind(("127.0.0.1", 19602))

        def patch():
            threading.Timer(1.0, lambda: rig.send(acsp.encode_restart_session())).start()
            assert sessionpatch.grow(rig.srv.pid, sessionpatch.layout_for(INDY_LIVE, [1, 2, 3]), 20).startswith("grown to 4 sessions")
        await loop.run_in_executor(None, patch)
        for i, st in enumerate(INDY_LIVE):
            rig.send(acsp.encode_set_session_info(i, st["name"], sessionpatch.TYPES[st["kind"]], st["laps"], st["time_min"], st["wait_s"]))
            await asyncio.sleep(0.1)
        await asyncio.sleep(2)
        plug.close()
        client = await acsp_mod.connect(sid, remote_port=19601, local_port=19602, car_slots=3)
        out = {}
        try:
            ds = await asyncio.to_thread(rig.join)
            await asyncio.sleep(2)
            before = [d.new_sessions for d in ds]
            for _ in range(3):
                client.send(acsp.encode_next_session())
                end = time.time() + 3
                while time.time() < end and any(d.new_sessions == b for d, b in zip(ds, before)):
                    await asyncio.sleep(0.1)
                if all(d.new_sessions > b for d, b in zip(ds, before)):
                    break
            assert ds[0].current.name == "Indy"
            await asyncio.sleep(1.5)
            hold = lambda: asyncio.to_thread(singlerunlive.hold_view, sid, client)
            out["t0"] = await hold()                                    # Ana is out, Beto and Carla wait for their turn
            runs = {0: [70000, 60000, 60000], 1: [70000, 59000, 62000], 2: [70000, 61000, 61000]}   # warm-up + the 2 timed laps. Total: Ana 120.0, Beto 121.0, Carla 122.0; best lap (acServer's own order): Beto 59.0, Ana 60.0, Carla 61.0
            t0 = client.session_t0
            for slot in (0, 1, 2):
                await asyncio.sleep(max(0.0, t0 + slot * 10 + 2 - time.time()))   # each one runs after his own release
                if slot:
                    out[f"turn_{slot}"] = await hold()                  # the turn of this one has come: he is out, the ones that ran are held, the ones after him wait
                for ms in runs[slot]:
                    ds[slot].lap(ms)
                await asyncio.sleep(1)
            await asyncio.sleep(1)
            out["all_done"] = await hold()
            end = time.time() + 90
            while time.time() < end and not all(d.current.name == "Ajuste" for d in ds):
                await asyncio.sleep(0.5)
            for d in ds:
                d.lap(95000)                                            # a real lap (slower than the 1 ms, 2 ms ... the manager writes): the practice has a result file, as a real one does
            end = time.time() + 150                                     # the qualifying ends by itself, then the adjusting practice: gridtimes writes the grid in its last seconds
            while time.time() < end and not all(d.session_type == fc.RACE for d in ds):
                await asyncio.sleep(0.5)
            out["race"] = [d.session_type for d in ds]
            out["grid_list"] = ds[0].grid_list
            out["slots"] = [d.grid_slot for d in ds]
            out["delivered"] = delivered(rig)
            return out
        finally:
            if client.transport:
                client.transport.close()
            for t in (client.grid_task, getattr(client, "_hello", None), getattr(client, "_sweep", None)):
                if t:
                    t.cancel()
    try:
        out = asyncio.run(run())
    finally:
        rig.close()
    stage = singlerunlive.RUN_STAGE
    assert out["t0"] == {"stage": stage, "cars": [1, 2]}, out
    assert out["turn_1"] == {"stage": stage, "cars": [0, 2]}, out       # Ana finished and is held; Beto's turn has come; Carla's has not
    assert out["turn_2"] == {"stage": stage, "cars": [0, 1]}, out       # Ana and Beto finished; Carla is out
    assert out["all_done"] == {"stage": stage, "cars": [0, 1, 2]}, out  # everybody ran: all held in the pits
    assert out["race"] == [fc.RACE] * 3, out
    assert out["grid_list"] == [0, 1, 2] and out["slots"] == [0, 1, 2], out   # by the TOTAL of the timed laps (Ana first), not by acServer's best-lap order (Beto first)
    assert out["delivered"] == {str(GUIDS[0]): 1, str(GUIDS[1]): 2, str(GUIDS[2]): 3}, out   # the manager wrote 1 ms, 2 ms, 3 ms into the adjusting practice


def test_what_the_other_clients_need_from_a_car_that_is_not_a_game(monkeypatch):
    """Seen on a real client (2026-11-20): the pace bot had no wheels, sat facing the wrong way and flew off the map once it moved. The game (acs.exe) wants: a pong to every
    server ping [F9] (else the server's ping of the car goes stale), the tyre compound as TCP 0x50, and the heading as rotation.x = -atan2(dx, dz)."""
    import math
    import struct

    class Sock:
        def __init__(self, incoming=()):
            self.sent, self.incoming = [], list(incoming)

        def send(self, b):
            self.sent.append(b)

        def sendall(self, b):
            self.sent.append(b)

        def recv(self, n):
            if self.incoming:
                return self.incoming.pop(0)
            raise OSError("closed")

    d = fc.FakeDriver("x", 1, guid=1, name="n", tyre="S")
    d.udp = Sock([b"\xf9" + struct.pack("<IH", 123456, 20)])
    d._udp_loop()
    pong = d.udp.sent[0]
    assert pong[0] == 0xF8 and pong[1:5] == struct.pack("<I", 123456) and len(pong) == 9, "the pong echoes the server time and adds the game's own clock"
    d.tcp = Sock()
    d._send_tcp(bytes([fc.TYRE_COMPOUND]) + _s8(d.tyre))
    assert d.tcp.sent[0] == struct.pack("<H", 3) + b"\x50\x01S"
    d.place(1.0, 2.0, 3.0, 0.5, math.atan2(1.0, 0.0), (10.0, 0.0, 0.0))   # driving along +X
    d.update()
    assert struct.unpack_from("<4B", d.udp.sent[1], 1 + 1 + 4 + 36) == (100,) * 4, "a stopped car: the wheel-speed bytes read 0 rad/s at 100 (a 0 is 100 000 rad/s backwards: no tyres)"
    d.set_speed(120)
    d.update()
    off = 1 + 1 + 4 + 36 + 4 + 2 + 2 + 1
    assert struct.unpack_from("<I", d.udp.sent[1], off)[0] == 0x10 and d.udp.sent[1][off + 6] == 0, "at rest: brake bit, no gas (what the game sends parked)"
    assert struct.unpack_from("<I", d.udp.sent[2], off)[0] == 0 and d.udp.sent[2][off + 6] == 200, "speeding up: gas, no brake"
    w = struct.unpack_from("<4B", d.udp.sent[2], 1 + 1 + 4 + 36)
    assert w[0] == w[3] > 100 and abs((10 ** ((w[0] - 100) * 0.05) - 1) - 120 / 3.6 / 0.33) < 12, "and a moving one spins about speed / radius"
    d.set_speed(-30)
    assert d._wheel_bytes()[0] < 100, "reversing: the wheels spin the other way"
    seq, ts, x, y, z, rot = struct.unpack_from("<BI3ff", d.udp.sent[1], 1)
    assert rot == pytest.approx(-math.pi / 2), "rotation.x is minus the heading: the game turns the car about Y by -rotation.x"
