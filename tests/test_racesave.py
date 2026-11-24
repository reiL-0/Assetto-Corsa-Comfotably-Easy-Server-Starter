"""A race that finished but acServer has not written (server 3, 2026-11-22): a RESTART_SESSION after the chequered flag lost the whole race, because acServer saves
results/*.json only when a session passes to the next one. The manager writes the result itself before a restart or a stop (app/racesave.py)."""
import asyncio
import json
import os
import shlex
import sys
import time

import pytest
from conftest import ADMIN
from fastapi.testclient import TestClient
from test_racecontrol import V, _Inst, live  # noqa: F401  (the `live` fixture)

from app import racesave, supervisor
from app.config import settings
from app.live import acsp
from app.live.board import Driver
from app.main import app
from app.results import parse_result_file

api = TestClient(app, headers=ADMIN)
T0 = 1_000_000.0   # session_t0 of these races (the green flag)


def _client(sid=1, laps=None, race=True):
    c = acsp.ACSPClient(sid)
    c.session = {"session_type": 3 if race else 1, "track": "jr_road_atlanta_2022", "track_config": "", "name": "Race", "time_min": 25, "laps": 0}
    c.session_t0 = T0
    names = {1: ("Paul Verboonen", "76561190000000001"), 2: ("Nando", "76561190000000002"), 3: ("Alan Montes", "76561190000000003"), 4: ("Jose TZ AL", "76561190000000004")}
    c.board.drivers = [Driver(car_id=i, name=n, guid=g, model="car") for i, (n, g) in names.items()]
    c.session_laps = laps if laps is not None else yesterday()
    return c


def _lap(car, n, ms, at, completed=False, cuts=0):
    names = {1: ("Paul Verboonen", "76561190000000001"), 2: ("Nando", "76561190000000002"), 3: ("Alan Montes", "76561190000000003"), 4: ("Jose TZ AL", "76561190000000004")}
    return {"car_id": car, "guid": names[car][1], "name": names[car][0], "model": "car", "ms": ms, "cuts": cuts, "at": T0 + at, "laps": n, "completed": completed}


def yesterday():
    """The race of 2026-11-22 in miniature: time ran out, the leader's NEXT crossing is the finish (has_completed), the others finish at theirs, a lapped car one lap down,
    and a car that kept lapping after it finished (it must not count)."""
    laps = []
    for n in range(1, 18):
        laps += [_lap(1, n, 85_900, 85 * n), _lap(2, n, 86_600, 86 * n + 10), _lap(3, n, 88_700, 88 * n + 20)]
    laps += [_lap(1, 18, 85_900, 1_540, True), _lap(2, 18, 87_100, 1_580, True),   # Paul finishes first, Nando after him
             _lap(3, 18, 88_000, 1_600, True),                                      # Alan: his 18th is also after the flag
             _lap(1, 19, 85_000, 1_630)]                                            # Paul keeps lapping after finishing: not part of the race
    laps += [_lap(4, n, 98_500, 300 * n) for n in range(1, 5)]                      # Jose: 4 laps, never finished
    return laps


def test_a_finished_timed_race_is_written_in_finishing_order_and_each_driver_ends_at_his_completed_lap(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    c = _client()
    path = racesave.save(1, c, now=T0 + 1_700)
    assert path and path.parent == tmp_path / "instances" / "1" / "results" and path.name.endswith("_RACE.json")
    assert not list(path.parent.glob("*.tmp")), "written atomically"
    parsed = parse_result_file(path)   # what the results list, the league and the web read
    assert parsed["type"] == "Race" and parsed["track"] == "jr_road_atlanta_2022"
    order = [e["driver_name"] for e in parsed["classification"]]
    assert order == ["Paul Verboonen", "Nando", "Alan Montes", "Jose TZ AL"], order   # 18 laps (least time first), 18 laps, 18 laps, 4 laps
    doc = json.loads(path.read_text())
    paul = [x for x in doc["Laps"] if x["DriverName"] == "Paul Verboonen"]
    assert len(paul) == 18, "the lap after his chequered flag is not the race"
    assert len([x for x in doc["Laps"] if x["DriverName"] == "Jose TZ AL"]) == 4, "a car that never finished keeps every lap it drove"
    assert doc["Source"] == "acmanager-racesave"


def test_it_writes_once_and_only_for_a_race_that_finished_and_was_not_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    d = tmp_path / "instances" / "1" / "results"
    assert racesave.save(1, _client(race=False)) is None, "a practice has nothing to do with it"
    assert racesave.save(1, _client(laps=[lap for lap in yesterday() if not lap["completed"]])) is None, "nobody has finished: the race is still on"
    first = racesave.save(1, _client(), now=T0 + 1_700)
    assert first and racesave.save(1, _client(), now=T0 + 1_701) is None, "the second call finds the file it wrote"
    for p in d.glob("*"):
        p.unlink()
    d.mkdir(parents=True, exist_ok=True)
    native = d / "2026_11_23_4_53_RACE.json"
    native.write_text("{}")
    os.utime(native, (T0 + 60, T0 + 60))   # a file from the middle of the race: the one of the race BEFORE, written while this one was on
    assert racesave.save(1, _client(), now=T0 + 1_700), "an older file is not this race's: it is not saved yet"
    for p in d.glob("*"):
        p.unlink()
    native.write_text("{}")
    os.utime(native, (T0 + 1_600, T0 + 1_600))   # acServer's own file, written after the finish
    assert racesave.save(1, _client(), now=T0 + 1_700) is None, "acServer already saved it: the manager writes nothing"


def test_a_file_with_the_same_name_is_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    d = tmp_path / "instances" / "1" / "results"
    d.mkdir(parents=True)
    now = T0 + 1_700
    lt = time.localtime(now)
    same = d / f"{lt.tm_year}_{lt.tm_mon}_{lt.tm_mday}_{lt.tm_hour}_{lt.tm_min}_RACE.json"
    same.write_text("older race")
    os.utime(same, (T0 - 3_600, T0 - 3_600))   # from before this session
    path = racesave.save(1, _client(), now=now)
    assert path and path != same and same.read_text() == "older race"


def test_restart_session_saves_the_finished_race_first_and_refuses_when_it_cannot(live, monkeypatch, tmp_path):   # noqa: F811
    sid, c, sent = live
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    c.session_laps, c.session_t0, c.session = yesterday(), T0, {**_client().session}
    c.board.drivers = _client().board.drivers
    restart = acsp.encode_restart_session()
    r = api.post(f"{V}/servers/{sid}/restart_session")
    assert r.status_code == 200 and r.json()["saved"], r.text
    assert (tmp_path / "instances" / str(sid) / "results" / r.json()["saved"]).exists()
    assert sent[-1] == restart, "and then it restarts"

    sent.clear()
    def broken(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(racesave, "save", broken)
    r = api.post(f"{V}/servers/{sid}/restart_session")
    assert r.status_code == 500 and not sent, "it could not save: nothing is restarted"
    r = api.post(f"{V}/servers/{sid}/restart_session?force=true")
    assert r.status_code == 200 and r.json()["saved"] is None and sent == [restart], "force: the dueño chooses to lose it"


def test_restart_session_with_no_race_to_save_just_restarts(live, tmp_path, monkeypatch):   # noqa: F811
    sid, c, sent = live
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    r = api.post(f"{V}/servers/{sid}/restart_session")
    assert r.status_code == 200 and r.json() == {"sent": True, "saved": None} and sent == [acsp.encode_restart_session()]


def test_a_stop_saves_the_finished_race_before_the_process_dies(tmp_path, monkeypatch):
    script = tmp_path / "fake_acserver.py"
    script.write_text("import time\ntime.sleep(60)\n")
    monkeypatch.setattr(settings, "acserver_cmd", f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}")
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))

    async def scenario():
        inst = await supervisor.start(96, tmp_path)
        try:
            inst.acsp = _client(96)
            await supervisor.stop(96)   # the idle stop, the end of the event and the apply with restart all come through here
        finally:
            await supervisor.stop(96)
    asyncio.run(scenario())
    files = list((tmp_path / "instances" / "96" / "results").glob("*_RACE.json"))
    assert len(files) == 1 and json.loads(files[0].read_text())["Source"] == "acmanager-racesave"


def _s(text):
    return bytes([len(text)]) + text.encode()


def test_the_client_keeps_every_lap_with_acservers_has_completed_and_starts_again_with_each_session():
    import struct
    c = acsp.ACSPClient(1)
    c.datagram_received(bytes([acsp.NEW_CONNECTION]) + acsp._write_string("Paul") + acsp._write_string("76561190000000001") + bytes([4]) + _s("car") + _s("skin"), ("127.0.0.1", 0))

    def lap(car, ms, laps, done):
        body = bytes([car]) + struct.pack("<I", ms) + bytes([0, 1]) + struct.pack("<BIHB", car, ms, laps, done) + struct.pack("<f", 1.0)
        c.datagram_received(bytes([acsp.LAP_COMPLETED]) + body, ("127.0.0.1", 0))
    lap(4, 85_900, 17, 0)
    lap(4, 85_800, 18, 1)   # the leader's crossing after the time ran out: acServer says it completed
    lap(9, 90_000, 3, 0)    # a car nobody is known to drive: there is no result to give it
    assert [(x["name"], x["ms"], x["laps"], x["completed"]) for x in c.session_laps] == [("Paul", 85_900, 17, False), ("Paul", 85_800, 18, True)]
    captured = (b"2\x04\x00\x00\x01\x08O\x00\x00\x00P\x00\x00\x00R\x00\x00\x00 \x00\x00\x00t\x00\x00\x00e\x00\x00\x00s\x00\x00\x00t\x00\x00\x00\x07magione\x00\x08Practice"
                b"\x01X\x02\x00\x00\x00\x00\x11\x16\x073_clear\x00\x00\x00\x00")
    c.datagram_received(captured, ("127.0.0.1", 0))   # a NEW_SESSION
    assert c.session_laps == [], "the laps of the session that ended are not the next one's"


def test_a_new_session_in_the_middle_of_the_save_cannot_mix_two_sessions(tmp_path, monkeypatch):
    """The UDP thread replaces session_laps when a NEW_SESSION arrives: the check and the write read ONE copy taken at the start."""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    c = _client()
    real = racesave.unsaved

    def then_a_new_session(snap, d):
        ok = real(snap, d)
        c.session_laps, c.session = [], {"session_type": 1}   # what _apply does at a NEW_SESSION, between the check and the write
        return ok
    monkeypatch.setattr(racesave, "unsaved", then_a_new_session)
    path = racesave.save(1, c, now=T0 + 1_700)
    assert path, "the race that was checked is the one written"
    doc = json.loads(path.read_text())
    assert doc["Type"] == "RACE" and doc["Result"][0]["DriverName"] == "Paul Verboonen" and len(doc["Laps"]) > 50


def test_a_finish_is_kept_even_past_the_cap_of_laps(monkeypatch):
    from app.live import acsp as acsp_mod
    import struct
    monkeypatch.setattr(acsp_mod, "SESSION_LAPS_MAX", 1)
    c = acsp.ACSPClient(1)
    c.datagram_received(bytes([acsp.NEW_CONNECTION]) + acsp._write_string("Paul") + acsp._write_string("76561190000000001") + bytes([4]) + _s("car") + _s("skin"), ("127.0.0.1", 0))

    def lap(done):
        body = bytes([4]) + struct.pack("<I", 85_900) + bytes([0, 1]) + struct.pack("<BIHB", 4, 85_900, 1, done) + struct.pack("<f", 1.0)
        c.datagram_received(bytes([acsp.LAP_COMPLETED]) + body, ("127.0.0.1", 0))
    lap(0)
    lap(0)   # past the cap: not kept
    lap(1)   # a finish: kept, or the race could never be saved
    lap(1)   # the same car laps on after finishing: not kept (the list stays bounded)
    assert [x["completed"] for x in c.session_laps] == [False, True]


def test_a_save_that_cannot_get_the_lock_fails_instead_of_hanging(tmp_path, monkeypatch):
    import threading
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(racesave, "LOCK_WAIT_S", 0.2)
    held = racesave._locks.setdefault(1, threading.Lock())
    held.acquire()   # a slow write of the same server that a stop gave up waiting for
    try:
        with pytest.raises(OSError):
            racesave.save(1, _client(), now=T0 + 1_700)
    finally:
        held.release()


def test_a_session_that_changes_while_the_snapshot_is_taken_is_copied_again():
    c = _client()
    old, new = c.session_laps, []
    calls = {"n": 0}

    class Swap(list):
        def __iter__(self):   # the copy of the OLD list happens here: a NEW_SESSION replaces the list at that instant
            if calls["n"] == 0:
                calls["n"] += 1
                c.session_laps, c.session = new, {"session_type": 1}
            return super().__iter__()
    c.session_laps = Swap(old)
    snap = racesave.snapshot(c)
    assert snap["laps"] == [] and snap["session"] == {"session_type": 1}, "the second copy is the new session's, whole"
