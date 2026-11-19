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
pytestmark = pytest.mark.skipif(not (SIM / "acServer").exists(), reason="needs the local acServer simulator (ACM_TEST_ACSERVER_SIM)")

_spec = importlib.util.spec_from_file_location("fake_client", Path(__file__).resolve().parent.parent / "ops" / "fake_client.py")
fc = importlib.util.module_from_spec(_spec)
sys.modules["fake_client"] = fc
_spec.loader.exec_module(fc)

GUIDS = [76561198000000001, 76561198000000002, 76561198000000003]
NAMES = ["Ana", "Beto", "Carla"]


class Rig:
    """A copy of the simulator (guids in the entry list: the server only lets in the ones it knows) + a plugin socket to command it."""

    def __init__(self, tmp: Path):
        self.dir = tmp / "sim"
        shutil.copytree(SIM, self.dir, ignore=shutil.ignore_patterns("logs", "results", "*.log"))
        (self.dir / "logs").mkdir(exist_ok=True)
        (self.dir / "results").mkdir(exist_ok=True)
        entry = (self.dir / "cfg" / "entry_list.ini")
        it = iter(GUIDS)
        entry.write_text(re.sub(r"GUID=.*\n", lambda m: f"GUID={next(it)}\n", entry.read_text()))
        self.log = open(tmp / "srv.log", "w")
        self.srv = subprocess.Popen(["./acServer"], cwd=self.dir, stdout=self.log, stderr=subprocess.STDOUT)   # our child: /proc/pid/mem and ptrace work
        self.plugin = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.plugin.bind(("127.0.0.1", 19602))
        self.drivers: list = []
        self._stop = threading.Event()
        end = time.time() + 15
        while time.time() < end:
            try:
                socket.create_connection(("127.0.0.1", 19600), 0.5).close()
                break
            except OSError:
                time.sleep(0.2)
        time.sleep(1)

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
        self._stop.set()
        for d in self.drivers:
            d.disconnect()
        self.srv.terminate()
        self.srv.wait(5)
        time.sleep(1)      # let the ports go before the next test starts its own server
        self.plugin.close()
        self.log.close()


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
