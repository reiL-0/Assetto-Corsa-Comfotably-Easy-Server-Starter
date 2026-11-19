"""app/singlerunlive.py: the single-run qualifying while it is played: who is held in the pits by the clock and the laps, and the grid from the result file."""

import json
from pathlib import Path
from types import SimpleNamespace

from conftest import ADMIN
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import singlerunlive
from app.config import settings
from app.db import engine
from app.main import app
from app.models import Activity, Server

STEP = {"kind": "qualify", "name": "Indy", "time_min": 20, "run_interval_s": 20, "run_warmup": 1, "run_laps": 2}
G = [f"7656119900000000{i}" for i in range(4)]


def _server():
    sid = TestClient(app, headers=ADMIN).post("/api/v1/servers", json={"name": "Indy"}).json()["id"]
    with Session(engine) as db:
        srv = db.get(Server, sid)
        srv.session = {"sequence": [STEP, {"kind": "practice", "name": "P", "time_min": 5}, {"kind": "race", "name": "R", "laps": 5}]}
        db.add(srv)
        db.commit()
    return sid


def _client(t0):
    return SimpleNamespace(session={"current_session_index": 0, "name": "Indy"}, session_t0=t0, cars={10 + i: {"driver_guid": g} for i, g in enumerate(G)}, run_release={})


def _lap(sid, g, ts, ms=60_000, cuts=0):
    with Session(engine) as db:
        db.add(Activity(ts=ts, server_id=sid, kind="lap", guid=g, value=ms, cuts=cuts))
        db.commit()


def test_cars_are_released_one_every_interval_and_held_again_when_their_run_is_over():
    sid, t0 = _server(), 1000.0
    c = _client(t0)
    assert singlerunlive.hold_view(sid, c, now=t0 + 1) == {"stage": singlerunlive.RUN_STAGE, "cars": [11, 12, 13]}      # only the first is out
    assert singlerunlive.hold_view(sid, c, now=t0 + 65) == {"stage": singlerunlive.RUN_STAGE, "cars": []}               # all out
    _lap(sid, G[0], t0 + 90)                                                                      # out lap
    _lap(sid, G[0], t0 + 150)
    assert 10 not in singlerunlive.hold_view(sid, c, now=t0 + 151)["cars"]                        # 1 of 2 timed laps: still running
    _lap(sid, G[0], t0 + 210)
    assert singlerunlive.hold_view(sid, c, now=t0 + 211) == {"stage": singlerunlive.RUN_STAGE, "cars": [10]}            # run over: back to be held


def test_a_departure_moves_nobodys_turn_and_a_late_driver_goes_after_the_last_never_at_once():
    sid, t0 = _server(), 1000.0
    c = _client(t0)
    singlerunlive.hold_view(sid, c, now=t0 + 1)
    del c.cars[10]                                                                                # the first one leaves: the others keep their turns
    assert singlerunlive.hold_view(sid, c, now=t0 + 25) == {"stage": singlerunlive.RUN_STAGE, "cars": [12, 13]}         # 11 was due at +20
    c.cars[20] = {"driver_guid": "late1"}
    c.cars[21] = {"driver_guid": "late2"}
    assert singlerunlive.hold_view(sid, c, now=t0 + 200) == {"stage": singlerunlive.RUN_STAGE, "cars": [21]}            # late1 out now (turn passed: +80 -> now), late2 one interval after
    assert singlerunlive.hold_view(sid, c, now=t0 + 221) == {"stage": singlerunlive.RUN_STAGE, "cars": []}


def test_a_normal_session_holds_nobody():
    sid = _server()
    c = _client(1000.0)
    c.session = {"current_session_index": 1, "name": "P"}
    assert singlerunlive.hold_view(sid, c, now=1001.0) is None


def test_grid_ranks_by_total_time_voids_cuts_and_puts_who_never_ran_last():
    sid = _server()
    folder = Path(settings.data_dir) / "instances" / str(sid) / "results"
    folder.mkdir(parents=True, exist_ok=True)
    laps = lambda g, times, cuts=None: [{"DriverGuid": g, "LapTime": t, "Cuts": (cuts or [0] * 3)[i]} for i, t in enumerate(times)]
    doc = {"Type": "QUALIFY", "TrackName": "oval", "TrackConfig": "",
           "Result": [{"DriverName": g[-1], "DriverGuid": g, "CarModel": "c", "BestLap": 59_000, "TotalTime": 1} for g in G],
           "Laps": laps(G[0], [70_000, 60_000, 60_000]) + laps(G[1], [70_000, 59_000, 59_000]) + laps(G[2], [70_000, 50_000, 50_000], [0, 1, 0])}   # G[2]: fastest but a cut; G[3] no laps
    (folder / "2026_11_18_10_0_QUALIFY.json").write_text(json.dumps(doc))
    assert singlerunlive.grid_now(sid, 0) == [G[1], G[0], G[2], G[3]]                # the only single-run step of the sequence
    assert singlerunlive.grid_now(sid, 0, 0) == [G[1], G[0], G[2], G[3]]             # the step given by its index (the file says nothing of its session)
    assert singlerunlive.grid_now(sid, 0, 1) == []                                    # an index that is a practice, not a single-run step
