import json
import secrets
from datetime import UTC, datetime
from pathlib import Path

from conftest import ADMIN
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.auth import _sha
from app.config import settings
from app.db import engine
from app.main import app
from app.models import Penalty, Token, User
from app.results import apply_penalties, parse_result_file

V = "/api/v1"
api = TestClient(app, headers=ADMIN)
A, B, C, D = "76561190000000001", "76561190000000002", "76561190000000003", "76561190000000004"


def _result(sid: int, name: str, kind: str = "Race") -> Path:
    """A 4-driver result: A, B, C finish 5 laps, D is a lap down. B is 5 s behind A, C 10 s."""
    rows = [("A", A, 600000), ("B", B, 605000), ("C", C, 610000), ("D", D, 590000)]
    laps = [{"DriverName": n, "DriverGuid": g, "CarId": i, "LapTime": 120000, "Cuts": 0}
            for i, (n, g, _) in enumerate(rows) for _ in range(4 if n == "D" else 5)]
    d = Path(settings.data_dir) / "instances" / str(sid) / "results"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps({
        "Type": kind, "TrackName": "spa", "Name": kind,
        "Result": [{"DriverName": n, "DriverGuid": g, "CarModel": "bmw", "BestLap": 119000 + i, "TotalTime": t}
                   for i, (n, g, t) in enumerate(rows)],
        "Laps": laps}))
    return d / name


def P(guid, kind, value=0):
    return Penalty(server_id=1, filename="x", driver_guid=guid, kind=kind, value=value, reason="test")


def _order(parsed):
    return [e["driver_name"] for e in parsed["classification"]]


def test_apply_penalties_reorders_and_keeps_the_original_visible(tmp_path):
    f = _result(api.post(f"{V}/servers", json={"name": "t"}).json()["id"], "pure.json")
    base = parse_result_file(f)
    plain = apply_penalties(base, [])
    assert _order(plain) == ["A", "B", "C", "D"] and plain["classification"][1]["gap_ms"] == 5000
    # +20 s on the winner: B and C pass him
    timed = apply_penalties(base, [P(A, "time", 20000)])
    assert _order(timed) == ["B", "C", "A", "D"] and [e["position"] for e in timed["classification"]] == [1, 2, 3, 4]
    a = timed["classification"][2]
    assert a["original_position"] == 1 and a["time_penalty_ms"] == 20000 and a["adjusted_total_ms"] == 620000
    assert timed["classification"][0]["gap_ms"] is None and timed["classification"][1]["gap_ms"] == 5000
    # laps come first: a huge time penalty on C does not drop him below D, who is a lap down
    assert _order(apply_penalties(base, [P(C, "time", 600000)])) == ["A", "B", "C", "D"]
    # places lost
    assert _order(apply_penalties(base, [P(B, "position", 2)])) == ["A", "C", "D", "B"]
    assert _order(apply_penalties(base, [P(D, "position", 5)])) == ["A", "B", "C", "D"]  # cannot fall off the end
    # disqualification
    dsq = apply_penalties(base, [P(A, "dsq")])
    assert _order(dsq) == ["B", "C", "D", "A"] and dsq["classification"][3]["position"] is None
    assert dsq["classification"][0]["position"] == 1 and dsq["classification"][3]["disqualified"] is True
    # grid / points do not touch the classification
    g = apply_penalties(base, [P(A, "grid", 2), P(B, "points", 10)])
    assert _order(g) == ["A", "B", "C", "D"] and g["grid"] == [B, C, A, D]
    assert g["classification"][1]["points_penalty"] == 10
    # a result with no penalties and the raw parser agree on order
    assert [e["driver_guid"] for e in plain["classification"]] == [e["driver_guid"] for e in base["classification"]]


def test_qualifying_is_not_resorted_by_race_time(tmp_path):
    f = _result(api.post(f"{V}/servers", json={"name": "t"}).json()["id"], "quali.json", "Qualify")
    out = apply_penalties(parse_result_file(f), [P(A, "time", 20000)])
    assert _order(out) == ["A", "B", "C", "D"]  # a time penalty is a race notion
    assert _order(apply_penalties(parse_result_file(f), [P(A, "position", 1)])) == ["B", "A", "C", "D"]


def _sid_and_file(name="api.json"):
    sid = api.post(f"{V}/servers", json={"name": "t"}).json()["id"]
    _result(sid, name)
    return sid, name


def _client(role):
    raw = secrets.token_urlsafe(16)
    with Session(engine) as s:
        u = User(username=f"{role}-{raw[:5]}", role=role)
        s.add(u)
        s.commit()
        s.add(Token(user_id=u.id, token_hash=_sha(raw), name="t", created_at=datetime.now(UTC)))
        s.commit()
    return TestClient(app, headers={"Authorization": f"Bearer {raw}"})


def test_penalty_api_roundtrip_and_parsed_result():
    sid, name = _sid_and_file()
    url = f"{V}/servers/{sid}/results/{name}/penalties"
    steward = _client("steward")
    r = steward.post(url, json={"driver_guid": A, "kind": "time", "value": 20, "reason": "Contacto con B en la curva 3"})
    assert r.status_code == 201 and r.json()["value"] == 20 and r.json()["created_by"].startswith("steward-")
    pid = r.json()["id"]
    assert [p["id"] for p in api.get(url).json()] == [pid]
    parsed = api.get(f"{V}/servers/{sid}/results/{name}/parsed").json()
    assert [e["driver_name"] for e in parsed["classification"]][:3] == ["B", "C", "A"]
    assert parsed["classification"][2]["penalties"][0]["reason"].startswith("Contacto")
    raw = api.get(f"{V}/servers/{sid}/results/{name}/parsed", params={"raw": True}).json()
    assert [e["driver_name"] for e in raw["classification"]] == ["A", "B", "C", "D"]  # exactly what acServer wrote
    assert steward.delete(f"{url}/{pid}").status_code == 204
    assert api.get(f"{V}/servers/{sid}/results/{name}/parsed").json()["classification"][0]["driver_name"] == "A"
    assert steward.delete(f"{url}/{pid}").status_code == 404


def test_penalty_api_validation_and_permissions():
    sid, name = _sid_and_file("val.json")
    url = f"{V}/servers/{sid}/results/{name}/penalties"
    ok = {"driver_guid": A, "kind": "position", "value": 2, "reason": "Salida en falso"}
    assert api.post(url, json={**ok, "driver_guid": "76561190000000099"}).status_code == 400  # not in this result
    for bad in ({"value": 0}, {"value": 31}, {"kind": "time", "value": 0}, {"kind": "time", "value": 3601},
                {"kind": "points", "value": 101}, {"kind": "dsq", "value": 1}):
        assert api.post(url, json={**ok, **bad}).status_code == 422, bad
    for bad in ({"reason": "no"}, {"reason": ""}, {"kind": "fine"}, {"driver_guid": "abc"}):
        assert api.post(url, json={**ok, **bad}).status_code == 422, bad
    assert api.post(url, json={"driver_guid": A, "kind": "dsq", "reason": "Pits abiertos"}).status_code == 201
    assert api.post(f"{V}/servers/{sid}/results/nope.json/penalties", json=ok).status_code == 404
    assert _client("driver").post(url, json=ok).status_code == 403
    assert _client("driver").get(url).status_code == 403
    assert TestClient(app).get(url).status_code == 401


def test_championship_uses_the_penalised_classification():
    sid, name = _sid_and_file("champ.json")
    url = f"{V}/servers/{sid}/results/{name}/penalties"
    cid = api.post(f"{V}/championships", json={"name": "Penalizada"}).json()["id"]
    api.post(f"{V}/championships/{cid}/events", json={"server_id": sid, "filename": name})
    pts = lambda: {r["driver_guid"]: r for r in api.get(f"{V}/championships/{cid}/standings").json()}
    assert pts()[A]["points"] == 25 and pts()[B]["points"] == 18
    api.post(url, json={"driver_guid": A, "kind": "time", "value": 20, "reason": "Contacto"})
    assert (pts()[B]["points"], pts()[C]["points"], pts()[A]["points"]) == (25, 18, 15)  # A drops to 3rd
    api.post(url, json={"driver_guid": B, "kind": "points", "value": 7, "reason": "Conducta antideportiva"})
    assert pts()[B]["points"] == 18 and pts()[B]["penalty_points"] == 7
    api.post(url, json={"driver_guid": C, "kind": "dsq", "reason": "Pits abiertos"})
    assert pts()[C]["points"] == 0  # disqualified drivers score nothing
    assert pts()[A]["points"] == 18  # A moved up to 2nd


def test_penalties_and_their_withdrawal_are_announced(monkeypatch):
    from app import discord
    said = []
    monkeypatch.setattr(discord, "announce", said.append)
    sid, name = _sid_and_file("announce.json")
    url = f"{V}/servers/{sid}/results/{name}/penalties"
    pid = api.post(url, json={"driver_guid": A, "kind": "time", "value": 20, "reason": "Contacto con B"}).json()["id"]
    assert len(said) == 1 and "**A**: +20 s" in said[0] and "Contacto con B" in said[0] and "Carrera" in said[0], said
    api.delete(f"{url}/{pid}")
    assert len(said) == 2 and said[1].startswith("↩️ Sanción retirada a **A** (+20 s)"), said
    api.post(url, json={"driver_guid": A, "kind": "dsq", "reason": "Pits abiertos"})
    api.post(url, json={"driver_guid": B, "kind": "position", "value": 1, "reason": "Salida en falso"})
    assert "descalificado" in said[2] and "pierde 1 posición\n" in said[3] + "\n"
    assert api.post(url, json={"driver_guid": A, "kind": "time", "value": 0, "reason": "mal"}).status_code == 422 and len(said) == 4  # a refused one says nothing

