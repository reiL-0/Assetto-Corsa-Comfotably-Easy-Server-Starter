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



def test_league_catalogue_penalties_are_race_time():
    sid, name = _sid_and_file("liga.json")
    url = f"{V}/servers/{sid}/results/{name}/penalties"
    assert api.get(url + "/catalogue").json() == []   # not a league's result
    cid = api.post(f"{V}/championships", json={"name": "Con catálogo", "penalties": [{"name": "Contacto", "seconds": 5}, {"name": "Grave", "dsq": True}]}).json()["id"]
    api.post(f"{V}/championships/{cid}/events", json={"server_id": sid, "filename": name})
    assert [i["name"] for i in api.get(url + "/catalogue").json()] == ["Contacto", "Grave"]
    r = api.post(url, json={"driver_guid": A, "item": "Contacto", "reason": "con B en la curva 3"}).json()
    assert (r["kind"], r["value"], r["reason"]) == ("time", 5, "Contacto: con B en la curva 3")
    assert api.post(url, json={"driver_guid": B, "item": "Grave"}).json()["kind"] == "dsq"
    assert api.post(url, json={"driver_guid": C, "item": "Inventada"}).status_code == 422
    assert api.post(url, json={"driver_guid": C, "kind": "points", "value": 3, "reason": "Conducta"}).status_code == 422   # no points in a league with a catalogue
    assert api.post(url, json={"driver_guid": C, "kind": "time", "value": 3, "reason": "Atajo"}).status_code == 201


def test_a_driver_with_no_finish_time_does_not_jump_to_the_front_when_a_time_penalty_resorts_the_race():
    """B has the same laps as A and C but never crossed the line (TotalTime 0). With a time penalty the race is re-sorted by laps then time; a zero time must
    not read as «the fastest»: B stays behind everybody who finished, and keeps the order acServer gave among those without a time."""
    sid = api.post(f"{V}/servers", json={"name": "dnf"}).json()["id"]
    rows = [("A", A, 600000), ("B", B, 0), ("C", C, 610000), ("D", D, 0)]
    laps = [{"DriverName": n, "DriverGuid": g, "CarId": i, "LapTime": 120000, "Cuts": 0} for i, (n, g, _) in enumerate(rows) for _ in range(5)]
    d = Path(settings.data_dir) / "instances" / str(sid) / "results"
    d.mkdir(parents=True, exist_ok=True)
    (d / "dnf.json").write_text(json.dumps({
        "Type": "Race", "TrackName": "spa", "Name": "Race",
        "Result": [{"DriverName": n, "DriverGuid": g, "CarModel": "bmw", "BestLap": 119000 + i, "TotalTime": t} for i, (n, g, t) in enumerate(rows)], "Laps": laps}))
    got = apply_penalties(parse_result_file(d / "dnf.json"), [P(A, "time", 20000)])     # A: 620 s, C: 610 s
    assert _order(got) == ["C", "A", "B", "D"], _order(got)


def test_public_races_show_the_penalised_classification_before_and_after_archiving():
    sid, name = _sid_and_file("pub.json")
    cid = api.post(f"{V}/championships", json={"name": "Publica"}).json()["id"]
    api.post(f"{V}/championships/{cid}/events", json={"server_id": sid, "filename": name})
    api.post(f"{V}/servers/{sid}/results/{name}/penalties", json={"driver_guid": A, "kind": "time", "value": 20, "reason": "Contacto"})
    races = lambda: api.get(f"{V}/championships/{cid}/races").json()

    def check(r):
        cls = r[0]["classification"]
        assert [e["driver_guid"] for e in cls[:3]] == [B, C, A] and cls[2]["original_position"] == 1 and cls[2]["adjusted_total_ms"] == 620000
        assert r[0]["penalties"] == [{"driver_guid": A, "kind": "time", "value": 20000, "reason": "Contacto"}]   # nobody's username leaks

    before = races()
    check(before)
    assert api.post(f"{V}/championships/{cid}/archive", json={"confirm": "Publica"}).status_code == 200
    assert races() == before   # the snapshot answers the same once the result file and the penalty rows are gone


def test_cut_notices_are_warnings_the_director_confirms_dismisses_or_closes():
    from app.championship import AutoRules
    from app.models import Championship, Notice
    from app.rulebook import CutRule
    from app.stewards.auto import cut_notices, cut_seconds

    rules = AutoRules(cuts_per_step=5, step_seconds=5, max_seconds=12).model_dump()   # the older column: still read, through the rulebook derived from it
    assert [cut_seconds(n, CutRule(**rules)) for n in (4, 5, 9, 10, 99)] == [0, 5, 5, 10, 12]
    assert [cut_seconds(n, CutRule(tolerance=5, cuts_per_step=1, step_seconds=5, max_seconds=5)) for n in (5, 6, 20)] == [0, 5, 5]   # VRL: more than 5 cuts = +5 s
    sid = api.post(f"{V}/servers", json={"name": "t"}).json()["id"]
    f = _result(sid, "cuts.json")
    data = json.loads(f.read_text())
    for lap in data["Laps"]:
        lap["Cuts"] = {"A": 3, "B": 1, "D": 1}.get(lap["DriverName"], 0)   # A: 5 laps x 3 = 15 cuts (capped 12 s), B: 5 cuts = 5 s, D: 4 laps x 1 = 4 (nothing), C: none
    data["Laps"].append({"DriverName": "C", "DriverGuid": C, "CarId": 2, "LapTime": 0, "Cuts": 50})   # an unfinished lap does not count
    f.write_text(json.dumps(data))
    parsed = parse_result_file(f)
    with Session(engine) as s:
        c = Championship(name="Auto", auto_rules=rules)
        s.add(c)
        s.commit()
        assert cut_notices(s, c, sid, "cuts.json", parsed) == 2
        s.commit()
        assert cut_notices(s, c, sid, "cuts.json", parsed) == 0   # counting the file again changes nothing
        assert cut_notices(s, Championship(name="Off"), sid, "cuts.json", parsed) == 0
    steward = _client("steward")
    url = f"{V}/servers/{sid}/results/cuts.json/penalties"
    assert steward.get(url).json() == []   # a warning is not a penalty: the result is untouched
    got = steward.get(f"{url}/notices").json()
    by = {n["driver_guid"]: n for n in got["notices"]}
    assert got["closed"] is False and by[A]["value"] == 12 and by[A]["penalty_kind"] == "time" and by[B]["value"] == 5 and by[A]["reason"] == "Cortes de pista: 15"
    # confirm A: it becomes a penalty under the steward's name and the warning is gone; a second click finds nothing
    r = steward.post(f"{url}/notices/{by[A]['id']}/confirm")
    assert r.status_code == 201 and r.json()["kind"] == "time" and r.json()["value"] == 12 and "confirmado" in r.json()["reason"]
    assert steward.post(f"{url}/notices/{by[A]['id']}/confirm").status_code == 404
    assert [p["driver_guid"] for p in steward.get(url).json()] == [A]
    # below steward cannot decide
    assert _client("reporter").post(f"{url}/notices/{by[B]['id']}/confirm").status_code == 403
    # close: the warning left (B) is deleted, the result takes no more penalties and its confirmed one stays
    assert steward.post(f"{url}/close").status_code == 204 and steward.post(f"{url}/close").status_code == 204
    assert steward.get(f"{url}/notices").json() == {"closed": True, "notices": []}
    assert steward.post(f"{url}/notices/{by[B]['id']}/confirm").status_code == 409
    assert steward.post(url, json={"driver_guid": B, "kind": "time", "value": 5, "reason": "tarde"}).status_code == 409
    p = steward.get(url).json()[0]
    assert steward.delete(f"{url}/{p['id']}").status_code == 409
    with Session(engine) as s:
        assert cut_notices(s, s.get(Championship, 1) or Championship(name="x", auto_rules=rules), sid, "cuts.json", parsed) == 0   # a closed result gets no new warnings
        assert s.query(Notice).filter_by(server_id=sid).count() == 0


def test_dismissing_a_notice_deletes_it_without_a_penalty():
    from app.models import Notice
    sid, name = _sid_and_file("dismiss.json")
    with Session(engine) as s:
        s.add(Notice(server_id=sid, filename=name, driver_guid=A, kind="cuts", penalty_kind="time", value=5000, reason="Cortes de pista: 5"))
        s.commit()
        nid = s.query(Notice).filter_by(server_id=sid).one().id
    steward = _client("steward")
    url = f"{V}/servers/{sid}/results/{name}/penalties"
    assert steward.delete(f"{url}/notices/{nid}").status_code == 204 and steward.delete(f"{url}/notices/{nid}").status_code == 404
    assert steward.get(f"{url}/notices").json()["notices"] == [] and steward.get(url).json() == []


def test_stop_and_go_is_charged_as_race_time_and_shown_in_seconds():
    sid, name = _sid_and_file("sg.json")
    url = f"{V}/servers/{sid}/results/{name}/penalties"
    steward = _client("steward")
    assert steward.post(url, json={"driver_guid": A, "kind": "stopgo", "value": 61, "reason": "Pits"}).status_code == 422   # more than a stop-and-go is
    r = steward.post(url, json={"driver_guid": A, "kind": "stopgo", "value": 30, "reason": "Velocidad en pits"})
    assert r.status_code == 201 and r.json()["kind"] == "stopgo" and r.json()["value"] == 30
    got = api.get(f"{V}/servers/{sid}/results/{name}/parsed").json()["classification"]
    assert [e["driver_name"] for e in got] == ["B", "C", "A", "D"] and got[2]["time_penalty_ms"] == 30000 and got[2]["adjusted_total_ms"] == 630000


def test_archiving_a_league_removes_its_notices_and_closing_marks():
    from app.models import Notice, ResultClose
    sid, name = _sid_and_file("arch.json")
    cid = api.post(f"{V}/championships", json={"name": "Limpia"}).json()["id"]
    api.post(f"{V}/championships/{cid}/events", json={"server_id": sid, "filename": name})
    with Session(engine) as s:
        s.add(Notice(server_id=sid, filename=name, driver_guid=A, kind="cuts", value=5000, reason="Cortes de pista: 5"))
        s.add(ResultClose(server_id=sid, filename=name, closed_by="x"))
        s.commit()
    assert api.post(f"{V}/championships/{cid}/archive", json={"confirm": "Limpia"}).status_code == 200
    with Session(engine) as s:
        assert s.query(Notice).filter_by(server_id=sid).count() == 0 and s.query(ResultClose).filter_by(server_id=sid).count() == 0
