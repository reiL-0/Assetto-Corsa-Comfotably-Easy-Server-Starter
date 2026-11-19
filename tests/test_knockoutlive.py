"""app/knockoutlive.py: the knockout qualifying (Q1, Q2, Q3) while it is played: entrants and late drivers kept in the database, who is held in the pits, the grid."""

import asyncio
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import ADMIN
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import gridtimes, knockoutlive
from app.config import settings
from app.db import engine
from app.main import app
from app.models import Penalty, Server

STEPS = [
    {"kind": "practice", "name": "Práctica", "time_min": 10},
    {"kind": "qualify", "name": "Q1", "time_min": 18, "stage": 1},
    {"kind": "qualify", "name": "Q2", "time_min": 15, "stage": 2},
    {"kind": "qualify", "name": "Q3", "time_min": 13, "stage": 3},
    {"kind": "practice", "name": "Ajuste", "time_min": 4},
    {"kind": "race", "name": "Carrera", "laps": 10},
]
CARS = [f"7656119900000000{i}" for i in range(8)]   # 8 entrants: 8 -> 6 -> 4


def _server(extra=None):
    sid = TestClient(app, headers=ADMIN).post("/api/v1/servers", json={"name": "F1"}).json()["id"]
    with Session(engine) as db:
        srv = db.get(Server, sid)
        srv.session = {"sequence": STEPS, **(extra or {})}
        db.add(srv)
        db.commit()
    return sid


def _file(sid, stage, order, extra_laps=()):
    """The result of stage `stage`: the drivers of `order` best first, 100 ms apart (every lap clean). Returns the file name."""
    folder = Path(settings.data_dir) / "instances" / str(sid) / "results"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"2026_11_18_10_{stage}_QUALIFY.json"
    (folder / name).write_text(json.dumps({"Type": "QUALIFY", "TrackName": "spa", "TrackConfig": "", "Name": f"Q{stage}", "Result": [
        {"DriverName": g[-1], "DriverGuid": g, "CarModel": "c", "BestLap": 100_000 + 100 * i, "TotalTime": 1} for i, g in enumerate(order)],
        "Laps": [{"DriverGuid": g, "LapTime": 100_000 + 100 * i, "Cuts": 0} for i, g in enumerate(order)] + list(extra_laps)}))
    return name


def _cycle(sid, q1_order=None, q2_order=None, q3_order=None, late=()):
    data = knockoutlive.new_cycle(CARS, 1.0)
    data["late"] = list(late)
    for k, order in ((1, q1_order), (2, q2_order), (3, q3_order)):
        if order:
            data["stages"][str(k)] = _file(sid, k, order)
    knockoutlive.save(sid, data)
    return data


def test_a_driver_is_an_entrant_before_q1_ends_and_a_late_one_after_and_a_return_changes_nothing():
    d = knockoutlive.new_cycle(["a", "b", "", "a"], 5.0)
    assert d["entrants"] == ["a", "b"] and d["late"] == []
    assert knockoutlive.add_connection(d, "c") and d["entrants"] == ["a", "b", "c"]      # joined during Q1
    assert not knockoutlive.add_connection(d, "a")                                       # a reconnection
    d["stages"]["1"] = "q1.json"                                                         # Q1 ended
    assert knockoutlive.add_connection(d, "x") and knockoutlive.add_connection(d, "y")
    assert d["late"] == ["x", "y"] and not knockoutlive.add_connection(d, "x") and not knockoutlive.add_connection(d, "b")


def test_after_q1_the_slowest_and_the_late_are_held_and_after_q2_the_q2_casualties_too():
    sid = _server()
    order = CARS                                              # CARS[0] fastest ... CARS[7] slowest in every stage
    _cycle(sid, q1_order=order, late=["late-1"])
    held = knockoutlive.held_guids(sid, 2)
    assert held == [CARS[7], CARS[6], "late-1"] or set(held) == {CARS[6], CARS[7], "late-1"}   # 8 -> 6: two cut, plus the late one
    assert knockoutlive.held_guids(sid, 3) is None           # Q2 has no file yet: nothing can be said
    _cycle(sid, q1_order=order, q2_order=order[:6], late=["late-1"])
    assert set(knockoutlive.held_guids(sid, 3)) == {CARS[4], CARS[5], CARS[6], CARS[7], "late-1"}   # 6 -> 4: two more
    assert knockoutlive.held_guids(sid, 1) is None


def test_the_hub_is_told_which_cars_to_hold_and_a_driver_who_comes_back_in_another_slot_is_still_held():
    sid = _server()
    _cycle(sid, q1_order=CARS)
    q2 = {"session_index": 2, "current_session_index": 2}
    cars = {i: {"driver_guid": g} for i, g in enumerate(CARS)}
    assert knockoutlive.hold_view(sid, q2, cars) == {"stage": 2, "cars": [6, 7]}
    cars = {i: {"driver_guid": g} for i, g in enumerate(CARS[:6])} | {11: {"driver_guid": CARS[7]}, 12: {"driver_guid": CARS[6]}}   # the two cut left and came back in other slots
    assert knockoutlive.hold_view(sid, q2, cars) == {"stage": 2, "cars": [11, 12]}
    assert knockoutlive.hold_view(sid, {"current_session_index": 1}, cars) is None          # Q1: nobody is held yet
    assert knockoutlive.hold_view(sid, {"current_session_index": 4}, cars) is None          # the practice to arrange the grid: everybody is let go
    assert knockoutlive.hold_view(sid, {"current_session_index": 5}, cars) is None          # the race
    assert knockoutlive.hold_view(sid, None, cars) is None


def test_with_no_evidence_nobody_is_held():
    sid = _server()
    q2 = {"current_session_index": 2}
    assert knockoutlive.hold_view(sid, q2, {0: {"driver_guid": CARS[0]}}) is None            # no cycle at all
    knockoutlive.save(sid, {**knockoutlive.new_cycle(CARS, 1.0), "stages": {"1": "no-such-file.json"}})
    assert knockoutlive.hold_view(sid, q2, {0: {"driver_guid": CARS[0]}}) is None            # a file that cannot be read
    knockoutlive.clear(sid)


def test_the_grid_is_q3_then_q2_casualties_then_q1_casualties_then_the_late_ones_with_the_grid_penalties():
    sid = _server()
    q1 = CARS
    q2 = CARS[:6]
    q3 = [CARS[1], CARS[0], CARS[2], CARS[3]]                  # CARS[1] takes pole
    data = _cycle(sid, q1_order=q1, q2_order=q2, q3_order=q3, late=["late-1"])
    assert knockoutlive.grid_now(sid) == [CARS[1], CARS[0], CARS[2], CARS[3], CARS[4], CARS[5], CARS[6], CARS[7], "late-1"]
    with Session(engine) as db:   # the stewards: CARS[1] loses 2 places on the grid (the penalty sits on the file of Q3)
        db.add(Penalty(server_id=sid, filename=data["stages"]["3"], driver_guid=CARS[1], kind="grid", value=2, reason="t", created_by="root"))
        db.commit()
    assert knockoutlive.grid_now(sid)[:4] == [CARS[0], CARS[2], CARS[1], CARS[3]]
    gridtimes.set_order(sid, [CARS[7]], "root")
    assert gridtimes.order_now(sid, 0, "knockout") == [CARS[7]]    # the director's own order wins
    gridtimes.clear_order(sid)
    assert gridtimes.order_now(sid, 0, "knockout")[:4] == [CARS[0], CARS[2], CARS[1], CARS[3]]
    _cycle(sid, q1_order=q1)
    assert knockoutlive.grid_now(sid) == []                    # not until Q3 has a file


def _client(sid, cars):
    return SimpleNamespace(server_id=sid, cars={i: {"driver_guid": g} for i, g in enumerate(cars)}, session={"current_session_index": 1}, knockout_lock=asyncio.Lock())


def test_q1_begins_a_cycle_with_who_is_there_a_late_one_is_noted_and_the_race_ends_it():
    sid = _server()

    async def go():
        client = _client(sid, CARS[:3])
        await knockoutlive._session(client, 0, True)                  # the practice: nothing
        assert knockoutlive.load(sid) is None
        await knockoutlive._session(client, 1, True)                  # Q1
        assert knockoutlive.load(sid)["entrants"] == CARS[:3]
        await knockoutlive._connection(client, CARS[3])               # joins during Q1: an entrant
        await knockoutlive._connection(client, CARS[0])               # a reconnection: nothing
        f = _file(sid, 1, CARS[:4])
        client.session = {"current_session_index": 2}                 # (the next session has already been announced: the file's own name still says Q1)
        await knockoutlive._end(client, f"results/{f}")
        assert knockoutlive.load(sid)["stages"] == {"1": f} and knockoutlive.load(sid)["entrants"] == CARS[:4]
        await knockoutlive._connection(client, "late-1")              # after Q1: late
        await knockoutlive._connection(client, "late-2")
        await knockoutlive._connection(client, "late-1")              # leaves and comes back: the same place
        assert knockoutlive.load(sid)["late"] == ["late-1", "late-2"]
        await knockoutlive._session(client, 2, False)                 # Q2 described again after a manager restart: the cycle stays
        assert knockoutlive.load(sid)["late"] == ["late-1", "late-2"]
        await knockoutlive._session(client, 5, True)                  # the race
        assert knockoutlive.load(sid) is None

    asyncio.run(go())


def test_a_manager_restart_in_the_middle_of_q1_does_not_forget_the_cycle_but_starts_one_if_there_is_none():
    sid = _server()

    async def go():
        client = _client(sid, CARS[:2])
        await knockoutlive._session(client, 1, False)                 # described (not announced): no cycle yet -> one starts with who is connected
        assert knockoutlive.load(sid)["entrants"] == CARS[:2]
        client.cars[5] = {"driver_guid": CARS[5]}
        await knockoutlive._session(client, 1, False)                 # described again: it is kept as it is
        assert knockoutlive.load(sid)["entrants"] == CARS[:2]

    asyncio.run(go())


def test_the_grid_of_the_practice_after_q3_is_the_knockouts(monkeypatch):
    sid = _server()
    seen = []
    monkeypatch.setattr(gridtimes, "order_now", lambda s, since, src="race": (seen.append(src), ["A"])[1])
    monkeypatch.setattr(gridtimes, "_write", lambda *a: None)
    for name, v in (("LEAD_S", 0.0), ("LATE_S", 0.0), ("DRAIN_S", 0.0)):
        monkeypatch.setattr(gridtimes, name, v)
    import app.supervisor as supervisor
    monkeypatch.setattr(supervisor, "get", lambda s: SimpleNamespace(pid=4242, started_at=1.0))
    types = {i: {"practice": 1, "qualify": 2, "race": 3}[st["kind"]] for i, st in enumerate(STEPS)}
    client = SimpleNamespace(server_id=sid, grid_task=None, grid_key=None, grid_last=None, session_t0=0.0, session={}, cars={0: {"driver_guid": "A"}}, session_types={})
    client.send = lambda data: client.session_types.__setitem__(struct.unpack("<h", data[1:3])[0], types[struct.unpack("<h", data[1:3])[0]])
    asyncio.run(gridtimes._guard(client, 0.0, 4, len(STEPS)))      # the practice after Q3
    assert seen == ["knockout"]


def test_race_state_carries_pit_hold_and_the_sequence_validates_the_stages():
    sid = _server()
    state = TestClient(app, headers=ADMIN).get(f"/api/v1/servers/{sid}/race-state").json()
    assert "pit_hold" in state and state["pit_hold"] is None      # the server is not running: nothing is held
    from pydantic import ValidationError

    from app.schemas.servers import SessionIn
    base = {"name": "S", "track": "spa", "cars": ["gt3"], "practice_min": 10, "qualify_min": 18, "race_laps": 10}
    assert len(SessionIn(**base, sequence=STEPS).sequence) == 6
    swapped = [STEPS[0], {**STEPS[2]}, {**STEPS[1]}, STEPS[3], STEPS[4], STEPS[5]]
    for bad in (swapped, [{**s, "stage": 1} for s in STEPS[1:3]] + [STEPS[0], STEPS[5]], [*STEPS[:4]]):   # out of order / repeated / no race after the last stage
        with pytest.raises(ValidationError):
            SessionIn(**base, sequence=bad)
    with pytest.raises(ValidationError):
        SessionIn(**base, sequence=[{**STEPS[0], "stage": 1}, *STEPS[1:]])   # a practice cannot be a stage


# --- the review's findings ------------------------------------------------------------------------------------------------------------------------------------------

def test_a_result_file_that_cannot_be_read_yet_is_never_guessed_to_be_the_stage_being_played(monkeypatch):
    sid = _server()
    knockoutlive.save(sid, knockoutlive.new_cycle(CARS, 1.0))

    async def fast(*a, **k):
        return None

    async def go():
        client = _client(sid, CARS)
        client.session = {"current_session_index": 2}                     # Q2 has been announced already
        monkeypatch.setattr(knockoutlive.asyncio, "sleep", fast)
        await knockoutlive._end(client, "results/2026_11_18_10_1_QUALIFY.json")   # Q1's file: not there (yet)
        assert knockoutlive.load(sid)["stages"] == {}                      # nothing written, least of all as Q2

    asyncio.run(go())


def test_a_result_row_with_a_lost_steam_id_is_matched_to_its_driver_by_name():
    sid = _server()
    folder = Path(settings.data_dir) / "instances" / str(sid) / "results"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "ph.json").write_text(json.dumps({"Type": "QUALIFY", "TrackName": "spa", "TrackConfig": "", "Name": "Q1", "Result": [
        {"DriverName": "Dana", "DriverGuid": "$#@@!$kicked", "CarModel": "c", "BestLap": 99_000, "TotalTime": 1},
        {"DriverName": "Eli", "DriverGuid": CARS[1], "CarModel": "c", "BestLap": 100_000, "TotalTime": 1}],
        "Laps": [{"DriverGuid": "$#@@!$kicked", "DriverName": "Dana", "LapTime": 99_000, "Cuts": 0}, {"DriverGuid": CARS[1], "LapTime": 100_000, "Cuts": 0}]}))
    assert knockoutlive.stage_times(sid, "ph.json", {CARS[0]: "Dana", CARS[1]: "Eli"}) == {CARS[0]: 99_000, CARS[1]: 100_000}   # Dana is CARS[0]


def test_the_name_of_a_driver_is_kept_and_a_reconnection_updates_it():
    d = knockoutlive.new_cycle(["a"], 1.0, {"a": "Ana"})
    assert d["names"] == {"a": "Ana"}
    knockoutlive.add_connection(d, "b", "Beto")
    assert d["entrants"] == ["a", "b"] and d["names"]["b"] == "Beto"
    assert not knockoutlive.add_connection(d, "a", "Ana B.") and d["names"]["a"] == "Ana B."


def test_a_session_that_is_not_the_step_of_the_stage_holds_nobody():
    sid = _server()
    _cycle(sid, q1_order=CARS)
    cars = {i: {"driver_guid": g} for i, g in enumerate(CARS)}
    assert knockoutlive.hold_view(sid, {"current_session_index": 2, "name": "Q2"}, cars) == {"stage": 2, "cars": [6, 7]}
    assert knockoutlive.hold_view(sid, {"current_session_index": 2, "name": "Práctica libre"}, cars) is None   # the sequence changed under the cycle: index 2 is another session now


def test_the_stages_need_different_names():
    from pydantic import ValidationError

    from app.schemas.servers import SessionIn
    base = {"name": "S", "track": "spa", "cars": ["gt3"], "practice_min": 10, "qualify_min": 18, "race_laps": 10}
    twice = [STEPS[0], STEPS[1], {**STEPS[2], "name": "Q1"}, STEPS[3], STEPS[4], STEPS[5]]
    with pytest.raises(ValidationError):
        SessionIn(**base, sequence=twice)


def test_a_name_two_drivers_share_matches_nobody_and_one_driver_with_two_rows_keeps_his_best():
    sid = _server()
    folder = Path(settings.data_dir) / "instances" / str(sid) / "results"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "dup.json").write_text(json.dumps({"Type": "QUALIFY", "TrackName": "spa", "TrackConfig": "", "Name": "Q1", "Result": [
        {"DriverName": "Dana", "DriverGuid": "$#@@!$kicked", "CarModel": "c", "BestLap": 99_000, "TotalTime": 1}],
        "Laps": [{"DriverGuid": "$#@@!$kicked", "DriverName": "Dana", "LapTime": 99_000, "Cuts": 0}]}))
    two = knockoutlive.stage_times(sid, "dup.json", {CARS[0]: "Dana", CARS[1]: "Dana"})   # two Danas: whose is it?
    assert CARS[0] not in two and CARS[1] not in two


def test_a_practice_that_carries_the_name_of_a_stage_is_not_that_stage():
    sid = _server()
    folder = Path(settings.data_dir) / "instances" / str(sid) / "results"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pr.json").write_text(json.dumps({"Type": "PRACTICE", "TrackName": "spa", "TrackConfig": "", "Name": "Q1", "Result": [], "Laps": []}))
    assert knockoutlive._stage_of(sid, "pr.json") is None
    assert knockoutlive._stage_of(sid, _file(sid, 2, CARS)) == 2


def test_a_connection_while_a_file_is_still_being_awaited_is_not_made_late_by_the_wait(monkeypatch):
    sid = _server()
    knockoutlive.save(sid, knockoutlive.new_cycle(CARS[:2], 1.0))

    async def go():
        client = _client(sid, CARS[:2])
        real = knockoutlive.asyncio.sleep
        order = []

        async def slow(*a, **k):
            order.append("waiting")
            await knockoutlive._connection(client, CARS[5])      # arrives while _end waits for Q1's file: Q1 has NOT been written down yet
            monkeypatch.setattr(knockoutlive.asyncio, "sleep", real)
            _file(sid, 1, CARS[:2])                              # now the file appears

        monkeypatch.setattr(knockoutlive.asyncio, "sleep", slow)
        await knockoutlive._end(client, "results/2026_11_18_10_1_QUALIFY.json")
        assert order == ["waiting"]

    asyncio.run(go())
    data = knockoutlive.load(sid)
    assert CARS[5] in data["entrants"] and data["late"] == [] and "1" in data["stages"]


def test_applying_the_same_sequence_again_does_not_end_a_cycle_in_play_but_another_sequence_does(monkeypatch):
    import asyncio as _a

    from app.schemas.servers import SessionIn
    from app.services import server_service
    monkeypatch.setattr(server_service, "check_content", lambda body, cars: {c: ["skin"] for c in cars})   # (no content is installed in the tests: the checks of the cars and the track are not what is under test)
    sid = _server()
    base = {"name": "S", "track": "spa", "cars": ["gt3"], "practice_min": 10, "qualify_min": 18, "race_laps": 10, "restart": False}

    def apply(steps):
        with Session(engine) as db:
            _a.run(server_service.apply(db, db.get(Server, sid), SessionIn(**base, sequence=steps)))

    apply(STEPS)                                               # the sequence is now the server's
    knockoutlive.save(sid, knockoutlive.new_cycle(CARS, 1.0))
    apply(STEPS)                                               # the same one again, no restart: a cycle that is being played is left alone
    assert knockoutlive.load(sid) is not None
    apply([*STEPS[:5], {**STEPS[5], "laps": 12}])              # another sequence: no cycle carries over
    assert knockoutlive.load(sid) is None
