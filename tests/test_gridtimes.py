"""app/gridtimes.py: the director's grid goes into acServer's times (1 ms, 2 ms ...) in the last seconds before a race, whoever is connected then; the qualifying of the site stays the real one."""

import asyncio
import json
import struct
from types import SimpleNamespace

import pytest
from conftest import ADMIN
from fastapi.testclient import TestClient

from app import gridtimes
from app.main import app
from app.results import parse_result_file

CUR, CARS, BEST, CAR_AT, SID_AT, IDX = 0x100, 0x120, 0x200, 0x400, 60, 0x80   # where the fake process keeps main.CurrentSessionResult, main.Cars, BestLaps and the Car structs (SessionID at byte 60 of each)


class FakeMem:
    """A tiny address space: BestLaps of `n` entries (999999999: no lap), Cars of `cn` pointers, Car i at CAR_AT + 80 i with its SessionID at byte 60."""

    def __init__(self, n=4, cn=4, sid_of=None, keep_writes=True):
        self.b = bytearray(0x2000)
        self.keep = keep_writes
        self.b[CUR:CUR + 12] = struct.pack("<III", BEST, n, n)
        self.b[CARS:CARS + 12] = struct.pack("<III", CARS + 16, cn, cn)
        for i in range(cn):
            self.b[CARS + 16 + 4 * i:CARS + 20 + 4 * i] = struct.pack("<I", CAR_AT + 80 * i)
            self.b[CAR_AT + 80 * i + SID_AT] = i if sid_of is None else sid_of(i)
        for i in range(n):
            self.b[BEST + 4 * i:BEST + 4 * i + 4] = struct.pack("<I", 999_999_999)

    def read(self, addr, n):
        return bytes(self.b[addr:addr + n])

    def write(self, addr, data):
        if self.keep:
            self.b[addr:addr + len(data)] = data

    def best(self):
        return list(struct.unpack_from("<4I", self.b, BEST))


@pytest.fixture(autouse=True)
def fake_addresses(monkeypatch):
    monkeypatch.setattr(gridtimes, "CURRENT_RESULT", CUR)
    monkeypatch.setattr(gridtimes, "CARS", CARS)
    monkeypatch.setattr(gridtimes, "CURRENT_INDEX", IDX)


def test_plan_is_by_steam_id_not_by_slot_and_the_unnamed_go_last():
    order = ["C", "A", "B"]
    assert gridtimes.plan(order, {0: "A", 1: "B", 2: "C"}) == {2: 1, 0: 2, 1: 3}
    assert gridtimes.plan(order, {0: "A", 2: "C", 5: "B"}) == {2: 1, 0: 2, 5: 3}   # B left and came back in another slot: same place
    assert gridtimes.plan(order, {0: "A", 1: "D", 2: "C", 5: "B"}) == {2: 1, 0: 2, 5: 3, 1: 4}   # D is new: last
    assert gridtimes.plan(order, {0: "A", 1: "", 2: "D"}) == {0: 1, 1: 2, 2: 3}   # nobody named: slot order, never 0 ms (0 is «no lap» to acServer)
    assert gridtimes.plan(order, {}) == {}


def test_apply_writes_1_2_3_ms_and_leaves_the_other_slots_alone():
    m = FakeMem()
    gridtimes.apply(m, {2: 1, 0: 2, 1: 3})
    assert m.best() == [2, 3, 1, 999_999_999]


def test_apply_refuses_when_main_cars_is_not_indexed_by_slot():
    m = FakeMem(sid_of=lambda i: (i + 1) % 4)
    with pytest.raises(gridtimes.GridError):
        gridtimes.apply(m, {0: 1, 1: 2})
    assert m.best() == [999_999_999] * 4   # nothing written


def test_apply_refuses_arrays_that_are_too_short_and_a_layout_that_is_not_ours():
    with pytest.raises(gridtimes.GridError):
        gridtimes.apply(FakeMem(n=2, cn=4), {3: 1})
    m = FakeMem()
    m.b[CUR:CUR + 12] = struct.pack("<III", BEST, 1_000_000, 1_000_000)   # garbage in the slice header
    with pytest.raises(gridtimes.GridError):
        gridtimes.apply(m, {0: 1})


def test_apply_notices_when_the_write_did_not_stick():
    with pytest.raises(gridtimes.GridError):
        gridtimes.apply(FakeMem(keep_writes=False), {0: 1})


def test_the_order_lives_in_the_database_without_repeats():
    gridtimes.set_order(9301, ["A", "B", "A", " ", "C"], "root")
    assert gridtimes.get_order(9301) == ["A", "B", "C"]
    gridtimes.set_order(9301, ["C", "A"], "root")
    assert gridtimes.get_order(9301) == ["C", "A"]
    assert gridtimes.clear_order(9301) is True and gridtimes.clear_order(9301) is False and gridtimes.get_order(9301) == []


def _client(sid, types=None, cars=None):
    c = SimpleNamespace(server_id=sid, grid_task=None, grid_key=None, grid_last=None, session_t0=0.0, session_types=dict(types or {}), cars=cars or {})
    c.sent = []
    c.send = lambda data: (c.sent.append(data), c.session_types.__setitem__(1, c.answer) if hasattr(c, "answer") else None)
    return c


def test_a_race_spends_the_order_and_a_timed_practice_arms_a_guard():
    async def go():
        client = _client(9302)
        gridtimes.set_order(9302, ["A"], "root")
        gridtimes.on_session(client, {"session_type": 1, "session_index": 0, "current_session_index": 0, "time_min": 5, "elapsed_ms": 0, "session_count": 3}, True)
        assert client.grid_task is not None and gridtimes.get_order(9302) == ["A"]   # armed; the order stays for the race
        task = client.grid_task
        gridtimes.on_session(client, {"session_type": 3, "session_index": 2, "current_session_index": 2, "time_min": 20, "elapsed_ms": 0, "session_count": 3}, True)
        await asyncio.sleep(0.05)
        assert task.cancelled() and client.grid_task is None and gridtimes.get_order(9302) == []   # the race began: spent
        gridtimes.on_session(client, {"session_type": 2, "session_index": 1, "current_session_index": 1, "time_min": 0, "session_count": 3}, True)
        assert client.grid_task is None   # nothing to wait for in an untimed session

    asyncio.run(go())


def test_after_a_manager_restart_the_description_of_the_current_session_arms_it_once_and_a_new_length_rearms():
    async def go():
        client = _client(9305)
        info = {"session_type": 1, "session_index": 0, "current_session_index": 0, "time_min": 5, "elapsed_ms": 60_000, "session_count": 3}
        gridtimes.on_session(client, info, False)
        first = client.grid_task
        assert first is not None
        gridtimes.on_session(client, dict(info), False)   # the same description again: nothing changes
        assert client.grid_task is first
        gridtimes.on_session(client, {**info, "time_min": 9}, False)   # the admin made it longer
        await asyncio.sleep(0)
        assert first.cancelled() and client.grid_task is not first
        other = client.grid_task
        gridtimes.on_session(client, {**info, "session_index": 2, "time_min": 1}, False)   # the answer about ANOTHER session: ignored
        assert client.grid_task is other
        client.grid_task.cancel()

    asyncio.run(go())


@pytest.mark.parametrize("next_type, writes", [(3, True), (2, False), (1, False)])
def test_the_guard_only_delivers_when_a_race_comes_next(monkeypatch, next_type, writes):
    written = []
    monkeypatch.setattr(gridtimes, "_write", lambda pid, started, ranks, sid, idx: written.append((ranks, idx)))
    monkeypatch.setattr(gridtimes, "LEAD_S", 0.0)
    monkeypatch.setattr(gridtimes, "LATE_S", 0.0)
    monkeypatch.setattr(gridtimes, "DRAIN_S", 0.0)
    import app.supervisor as supervisor
    monkeypatch.setattr(supervisor, "get", lambda sid: SimpleNamespace(pid=4242, started_at=1.0))
    gridtimes.set_order(9306, ["B", "A"], "root")
    client = _client(9306, cars={0: {"driver_guid": "A"}, 1: {"driver_guid": "B"}})
    client.send = lambda data: client.session_types.__setitem__(1, next_type)   # the answer to «what is session 1?»
    asyncio.run(gridtimes._guard(client, 0.0, 0, 2))
    assert written == ([({1: 1, 0: 2}, 0)] if writes else [])
    gridtimes.clear_order(9306)


def test_a_race_spends_only_the_orders_set_before_it_began():
    import time
    gridtimes.set_order(9307, ["A"], "root")
    assert gridtimes.clear_order(9307, before=time.time() - 3600) is False   # the race began an hour ago: this order is newer, it is for the next one
    assert gridtimes.get_order(9307) == ["A"]
    assert gridtimes.clear_order(9307, before=time.time() + 1) is True       # the race began after it was set
    assert gridtimes.get_order(9307) == []


def test_an_old_answer_in_flight_does_not_make_the_next_session_a_race(monkeypatch):
    monkeypatch.setattr(gridtimes, "DRAIN_S", 0.0)
    answers = iter([3, 2])   # the first answer is the stale one (a race), the second is the truth (a qualify)
    client = _client(9308)
    client.send = lambda data: client.session_types.__setitem__(1, next(answers))
    assert asyncio.run(gridtimes._race_at(client, 1)) is False


def test_apply_does_not_write_into_another_session():
    m = FakeMem()
    m.b[IDX:IDX + 4] = struct.pack("<i", 2)   # the server moved on to session 2
    with pytest.raises(gridtimes.GridError):
        gridtimes.apply(m, {0: 1}, session_index=1)
    assert m.best() == [999_999_999] * 4
    gridtimes.apply(m, {0: 1}, session_index=2)
    assert m.best()[0] == 1


def test_the_guard_follows_who_is_connected_at_every_tick(monkeypatch):
    written = []
    monkeypatch.setattr(gridtimes, "_write", lambda pid, started, ranks, sid, idx: written.append((pid, ranks)))
    import app.supervisor as supervisor
    monkeypatch.setattr(supervisor, "get", lambda sid: SimpleNamespace(pid=4242, started_at=1.0))
    gridtimes.set_order(9303, ["C", "A", "B"], "root")
    client = _client(9303, cars={0: {"driver_guid": "A"}, 1: {"driver_guid": "B"}, 2: {"driver_guid": "C"}})

    async def go():
        await gridtimes._tick(client, 0)
        client.cars.pop(1)                       # B leaves ...
        client.cars[5] = {"driver_guid": "B"}    # ... and comes back in another slot
        client.cars[3] = {"driver_guid": "D"}    # a new one
        await gridtimes._tick(client, 0)

    asyncio.run(go())
    assert written == [(4242, {2: 1, 0: 2, 1: 3}), (4242, {2: 1, 0: 2, 5: 3, 3: 4})]
    gridtimes.clear_order(9303)


def test_no_order_no_write(monkeypatch):
    written = []
    monkeypatch.setattr(gridtimes, "_write", lambda *a: written.append(a))
    import app.supervisor as supervisor
    monkeypatch.setattr(supervisor, "get", lambda sid: SimpleNamespace(pid=1, started_at=1.0))
    asyncio.run(gridtimes._tick(SimpleNamespace(server_id=9304, grid_last=None, cars={0: {"driver_guid": "A"}}), 0))
    assert written == []


def test_the_site_qualifying_stays_the_real_one_when_the_times_were_set_to_1_2_3_ms(tmp_path):
    data = {"Type": "QUALIFY", "TrackName": "spa", "TrackConfig": "", "Name": "Qualify", "Result": [
        {"DriverName": "Bob", "DriverGuid": "2", "CarModel": "c", "BestLap": 1, "TotalTime": 1},
        {"DriverName": "Alice", "DriverGuid": "1", "CarModel": "c", "BestLap": 2, "TotalTime": 1},
    ], "Laps": [
        {"DriverGuid": "1", "LapTime": 90_000, "Cuts": 0},
        {"DriverGuid": "2", "LapTime": 91_000, "Cuts": 0},
    ]}
    p = tmp_path / "q.json"
    p.write_text(json.dumps(data))
    cls = parse_result_file(p)["classification"]
    assert [(e["driver_guid"], e["best_lap_ms"]) for e in cls] == [("1", 90_000), ("2", 91_000)]


def test_the_grid_endpoints_keep_and_delete_the_order():
    api = TestClient(app, headers=ADMIN)
    sid = api.post("/api/v1/servers", json={"name": "GridServer"}).json()["id"]
    assert api.get(f"/api/v1/servers/{sid}/grid").json() == {"guids": []}
    assert api.put(f"/api/v1/servers/{sid}/grid", json={"guids": ["76561199000000002", "76561199000000001"]}).json() == {"guids": ["76561199000000002", "76561199000000001"]}
    assert api.put(f"/api/v1/servers/{sid}/grid", json={"guids": []}).status_code == 422
    assert api.put(f"/api/v1/servers/{sid}/grid", json={"guids": ["   "]}).status_code == 422   # not a Steam ID
    assert api.put(f"/api/v1/servers/{sid}/grid", json={"guids": ["abc"]}).status_code == 422
    assert api.get(f"/api/v1/servers/{sid}/grid").json()["guids"][0] == "76561199000000002"
    assert api.delete(f"/api/v1/servers/{sid}/grid").json() == {"deleted": True}
    assert api.get(f"/api/v1/servers/{sid}/grid").json() == {"guids": []}
    assert api.get("/api/v1/servers/999999/grid").status_code == 404


# --- the order from the race before (grid_from_race) ----------------------------------------------------------------------------------------------------------

def _race_server(policy, tmp_path):
    """A server whose results folder holds a race (Alice, Bob, Cat in that order) and whose session says `grid_from_race=policy`."""
    from sqlmodel import Session as DbSession

    from app.config import settings
    from app.db import engine
    from app.models import Server
    api = TestClient(app, headers=ADMIN)
    sid = api.post("/api/v1/servers", json={"name": f"Auto{policy}"}).json()["id"]
    with DbSession(engine) as db:
        srv = db.get(Server, sid)
        srv.session = {"grid_from_race": policy}
        db.add(srv)
        db.commit()
    folder = __import__("pathlib").Path(settings.data_dir) / "instances" / str(sid) / "results"
    folder.mkdir(parents=True, exist_ok=True)
    name = "2026_11_18_10_0_RACE.json"
    (folder / name).write_text(json.dumps({"Type": "RACE", "TrackName": "spa", "TrackConfig": "", "Name": "Race", "Result": [
        {"DriverName": n, "DriverGuid": g, "CarModel": "c", "BestLap": 90000, "TotalTime": 600000 + i} for i, (n, g) in enumerate([("Alice", "1"), ("Bob", "2"), ("Cat", "3")])],
        "Laps": [{"DriverGuid": g, "LapTime": 90000, "Cuts": 0} for g in "123"]}))
    return sid, name


@pytest.mark.parametrize("policy, expected", [(0, ["1", "2", "3"]), (2, ["2", "1", "3"]), (-1, ["3", "2", "1"])])
def test_the_grid_comes_from_the_race_before_by_the_policy(tmp_path, policy, expected):
    sid, _ = _race_server(policy, tmp_path)
    assert gridtimes.order_now(sid, since=0) == expected


def test_the_grid_penalties_of_the_stewards_move_the_driver_back_and_a_manual_order_wins(tmp_path):
    from sqlmodel import Session as DbSession

    from app.db import engine
    from app.models import Penalty
    sid, name = _race_server(0, tmp_path)
    with DbSession(engine) as db:
        db.add(Penalty(server_id=sid, filename=name, driver_guid="1", kind="grid", value=1, reason="test", created_by="root"))
        db.commit()
    assert gridtimes.order_now(sid, since=0) == ["2", "1", "3"]   # Alice won and loses a place on the grid
    gridtimes.set_order(sid, ["3", "1"], "root")
    assert gridtimes.order_now(sid, since=0) == ["3", "1"]         # the director's own order is not touched by the policy
    gridtimes.clear_order(sid)


def test_no_policy_or_a_result_from_before_this_session_gives_no_order(tmp_path):
    sid, _ = _race_server(None, tmp_path)
    assert gridtimes.order_now(sid, since=0) == []   # the server did not ask for it
    sid2, _ = _race_server(0, tmp_path)
    import time
    assert gridtimes.order_now(sid2, since=time.time() + 3600) == []   # the race file is older than the session that is ending


def test_shape_is_acservers_reversed_grid():
    g = ["a", "b", "c", "d"]
    assert gridtimes.shape(g, 0) == g and gridtimes.shape(g, 2) == ["b", "a", "c", "d"] and gridtimes.shape(g, -1) == ["d", "c", "b", "a"]
