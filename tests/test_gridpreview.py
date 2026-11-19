"""app/gridtimes.py preview: the order of the next race the panel shows (the director's own, or the one the guard will write) and why; results.py marks the practice whose times are the grid delivery's."""

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

from conftest import ADMIN
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import gridtimes
from app.config import settings
from app.db import engine
from app.main import app
from app.models import Activity, Penalty, Server
from app.results import parse_result_file

api = TestClient(app, headers=ADMIN)
G = [f"7656119800000030{i}" for i in range(4)]
SEQ = [{"kind": "practice", "name": "P", "time_min": 10}, {"kind": "qualify", "name": "Q", "time_min": 8}, {"kind": "practice", "name": "Ajuste", "time_min": 4}, {"kind": "race", "name": "R", "laps": 5}]


def _server(session):
    sid = api.post("/api/v1/servers", json={"name": "Prev"}).json()["id"]
    with Session(engine) as s:
        srv = s.get(Server, sid)
        srv.session = session
        s.add(srv)
        s.commit()
    return sid


def _file(sid, name, kind, laps_ms, age=10):
    d = Path(settings.data_dir) / "instances" / str(sid) / "results"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps({"Type": kind, "TrackName": "spa", "TrackConfig": "", "Name": "x", "Result": [
        {"DriverName": g[-1], "DriverGuid": g, "CarModel": "c", "BestLap": ms, "TotalTime": 0} for g, ms in zip(G, laps_ms)],
        "Laps": [{"DriverGuid": g, "LapTime": ms, "Cuts": 0} for g, ms in zip(G, laps_ms)]}))
    os.utime(d / name, (time.time() - age, time.time() - age))
    return d / name


def _client(idx, cars=()):
    return SimpleNamespace(session={"current_session_index": idx, "wait_time": 0}, session_t0=time.time() - 5, cars={i: {"driver_guid": g, "driver_name": f"P{g[-1]}"} for i, g in enumerate(cars)})


def test_a_practice_with_times_1_2_3_ms_is_the_grid_delivery_and_a_real_one_is_not():
    sid = _server({})
    assert parse_result_file(_file(sid, "p1.json", "PRACTICE", [1, 2, 3, 4]))["grid_delivery"] is True
    assert parse_result_file(_file(sid, "p2.json", "PRACTICE", [90000, 90500, 91000, 99000]))["grid_delivery"] is False
    assert parse_result_file(_file(sid, "q1.json", "QUALIFY", [1, 2, 3, 4]))["grid_delivery"] is False   # a qualifying is classified by its laps: never marked
    assert parse_result_file(_file(sid, "p3.json", "PRACTICE", [1, 999_999_999, 999_999_999, 999_999_999]))["grid_delivery"] is True   # a lone driver: his 1 ms is the delivery's too


def test_the_directors_own_order_shows_as_manual_and_who_came_late_goes_last():
    sid = _server({"sequence": SEQ})
    with Session(engine) as s:
        s.add(Activity(ts=time.time(), server_id=sid, kind="join", guid=G[0], name="Ana"))
        s.commit()
    gridtimes.set_order(sid, [G[2], G[0]], "admin")
    p = gridtimes.preview(sid, _client(2, cars=[G[0], G[2], G[3]]))
    assert p["mode"] == "manual" and [r["guid"] for r in p["order"]] == [G[2], G[0], G[3]]
    assert [r["tone"] for r in p["order"]] == ["hand", "hand", "new"] and p["order"][2]["reason"].startswith("Nuevo")
    assert p["order"][1]["online"] and p["order"][0]["name"] == "P2"
    cl = _client(2, cars=[G[3], G[1]])   # connected in the order G[3], G[1]: plan() puts them after the named ones by SLOT, so the one in the lower slot first
    cl.cars = {7: {"driver_guid": G[3], "driver_name": "x"}, 2: {"driver_guid": G[1], "driver_name": "y"}}
    assert [r["guid"] for r in gridtimes.preview(sid, cl)["order"]] == [G[2], G[0], G[1], G[3]]


def test_the_automatic_order_comes_from_the_qualifying_with_the_places_the_stewards_took():
    sid = _server({"sequence": SEQ, "grid_from_qualify": True})
    path = _file(sid, "2026_11_18_10_0_QUALIFY.json", "QUALIFY", [90000, 90100, 90200, 90300])
    with Session(engine) as s:
        s.add(Penalty(server_id=sid, filename="2026_11_18_10_0_QUALIFY.json", driver_guid=G[0], kind="grid", value=2, reason="Choque", created_by="x"))
        s.commit()
    p = gridtimes.preview(sid, _client(2, cars=G))   # the practice that arranges the grid is the session being played
    assert p["mode"] == "auto" and p["source"] == "qualify" and [r["guid"] for r in p["order"]] == [G[1], G[2], G[0], G[3]]
    assert p["order"][2]["reason"] == "P1 en la clasificación · −2 por sanción de parrilla" and p["order"][2]["tone"] == "pen"
    assert p["order"][0]["reason"] == "P2 en la clasificación · sube 1 puesto"


def test_without_a_practice_that_arranges_the_grid_there_is_nothing_to_show_and_it_says_why():
    sid = _server({})
    p = gridtimes.preview(sid, _client(1, cars=G))
    assert p["mode"] == "none" and p["order"] == [] and "acServer" in p["note"]
    sid = _server({"sequence": SEQ})   # in the qualifying: the practice that arranges it has not come
    assert gridtimes.preview(sid, _client(1, cars=G))["mode"] == "none"
    sid = _server({"sequence": SEQ})   # grid policy «I decide»: no grid_from_qualify
    p = gridtimes.preview(sid, _client(2, cars=G))
    assert p["mode"] == "none" and "decido" in p["note"]
    sid = _server({"sequence": [SEQ[0], SEQ[0], SEQ[2], SEQ[3]], "grid_from_qualify": True})   # two practices before the race: the guard takes a qualifying order only from a qualifying
    assert gridtimes.preview(sid, _client(2, cars=G))["mode"] == "none"


def test_the_endpoint_answers_for_a_stopped_server_with_the_manual_order():
    sid = _server({"sequence": SEQ})
    gridtimes.set_order(sid, [G[1], G[0]], "admin")
    r = api.get(f"/api/v1/servers/{sid}/grid/preview")
    assert r.status_code == 200 and r.json()["mode"] == "manual" and [x["guid"] for x in r.json()["order"]] == [G[1], G[0]]
