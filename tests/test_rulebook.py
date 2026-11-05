"""A league's rulebook (app/rulebook.py): what is derived from the older columns, what a saved one changes, the VRL preset and the dry-run that calibrates it."""

import json
import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import ADMIN
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine, select

from app import db
from app.auth import _sha
from app.config import settings
from app.db import engine
from app.main import app
from app.models import Championship, Notice, Penalty, Token, User
from app.rulebook import PRESETS, Rulebook
from app.stewards.auto import cut_notices

V = "/api/v1"
api = TestClient(app, headers=ADMIN)
A, B, C = "76561190000000001", "76561190000000002", "76561190000000003"


def _result(sid: int, name: str, cuts: dict[str, int]) -> Path:
    """A 3-driver race of 5 laps each; `cuts` = cuts per lap by driver name."""
    rows = [("A", A, 600000), ("B", B, 605000), ("C", C, 610000)]
    laps = [{"DriverName": n, "DriverGuid": g, "CarId": i, "LapTime": 120000, "Cuts": cuts.get(n, 0)} for i, (n, g, _) in enumerate(rows) for _ in range(5)]
    d = Path(settings.data_dir) / "instances" / str(sid) / "results"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps({"Type": "Race", "TrackName": "spa", "Name": "Race", "Laps": laps,
                                      "Result": [{"DriverName": n, "DriverGuid": g, "CarModel": "bmw", "BestLap": 119000 + i, "TotalTime": t} for i, (n, g, t) in enumerate(rows)]}))
    return d / name


def _steward():
    raw = secrets.token_urlsafe(16)
    with Session(engine) as s:
        u = User(username=f"st-{raw[:5]}", role="steward")
        s.add(u)
        s.commit()
        s.add(Token(user_id=u.id, token_hash=_sha(raw), name="t", created_at=datetime.now(UTC)))
        s.commit()
    return TestClient(app, headers={"Authorization": f"Bearer {raw}"})


def _league(name, **extra):
    return api.post(f"{V}/championships", json={"name": name, **extra}).json()["id"]


def test_the_schema_refuses_what_makes_no_sense():
    ok = {"key": "a", "name": "A", "kind": "time", "value": 5}
    Rulebook.model_validate({"catalogue": [ok]})
    for bad in ({"catalogue": [ok, ok]},                                              # repeated key
                {"catalogue": [{**ok, "kind": "dsq", "value": 5}]},                    # a DSQ with seconds
                {"catalogue": [{**ok, "value": 0}]},                                   # a time penalty with none
                {"catalogue": [{**ok, "key": "s", "kind": "stopgo", "value": 61}]},    # longer than a stop-and-go can be
                {"catalogue": [{**ok, "key": "Bad Key"}]},
                {"detectors": {"cuts": {"cuts_per_step": 0, "step_seconds": 5, "max_seconds": 5}}},
                {"unknown_section": 1}, {"schema_version": 2}):
        with pytest.raises(ValueError):
            Rulebook.model_validate(bad)


def test_a_league_without_a_rulebook_has_one_derived_from_its_older_columns():
    cid = _league("Vieja", points_system=[10, 5], penalties=[{"name": "Contacto", "seconds": 5}, {"name": "Cortar", "dsq": True}], auto_rules={"cuts_per_step": 5, "step_seconds": 5, "max_seconds": 12})
    got = api.get(f"{V}/championships/{cid}/rulebook").json()
    assert got["source"] == "legacy" and got["rulebook"]["scoring"]["table"] == [10, 5]
    assert [(i["name"], i["kind"], i["value"]) for i in got["rulebook"]["catalogue"]] == [("Contacto", "time", 5), ("Cortar", "dsq", 0)]
    assert got["rulebook"]["detectors"]["cuts"] == {"tolerance": 0, "cuts_per_step": 5, "step_seconds": 5, "max_seconds": 12}


def test_a_saved_rulebook_becomes_the_source_and_the_older_columns_are_refused():
    cid = _league("Nueva", points_system=[10, 5])
    rb = {**PRESETS["vrl-base"].model_dump(), "scoring": {"table": [3, 2, 1]}}
    assert api.put(f"{V}/championships/{cid}/rulebook", json=rb).status_code == 200
    assert api.get(f"{V}/championships/{cid}/rulebook").json()["source"] == "rulebook"
    assert api.patch(f"{V}/championships/{cid}", json={"name": "Nueva", "points_system": [1]}).status_code == 409
    assert api.patch(f"{V}/championships/{cid}", json={"name": "Nueva 2"}).status_code == 200   # the name is not a rule
    # the table that scores is the rulebook's
    sid = api.post(f"{V}/servers", json={"name": "t"}).json()["id"]
    _result(sid, "r.json", {})
    api.post(f"{V}/championships/{cid}/events", json={"server_id": sid, "filename": "r.json"})
    assert [r["points"] for r in api.get(f"{V}/championships/{cid}/standings").json()] == [3, 2, 1]
    assert api.put(f"{V}/championships/{cid}/rulebook", json={"catalogue": [{"key": "x", "name": "x", "kind": "dsq", "value": 9}]}).status_code == 422


def test_the_rulebook_catalogue_feeds_the_penalty_panel_with_stop_and_go_drive_through_and_dsq():
    sid = api.post(f"{V}/servers", json={"name": "t"}).json()["id"]
    _result(sid, "cat.json", {})
    cid = _league("Catalogo")
    api.post(f"{V}/championships/{cid}/events", json={"server_id": sid, "filename": "cat.json"})
    api.put(f"{V}/championships/{cid}/rulebook", json=PRESETS["vrl-base"].model_dump())
    st = _steward()
    url = f"{V}/servers/{sid}/results/cat.json/penalties"
    cat = {i["key"]: i for i in st.get(f"{url}/catalogue").json()}
    assert cat["drive-through"]["seconds"] == 30 and cat["dsq"]["dsq"] is True and cat["stop-go-30"]["kind"] == "stopgo"   # the panel's old shape ({name, seconds, dsq}) plus kind
    made = {item: st.post(url, json={"driver_guid": A if item != "Descalificación" else C, "item": item}).json() for item in ("Stop & go 30 s", "Drive-through", "Descalificación")}
    assert (made["Stop & go 30 s"]["kind"], made["Stop & go 30 s"]["value"]) == ("stopgo", 30)
    assert (made["Drive-through"]["kind"], made["Drive-through"]["value"]) == ("time", 30)   # charged as time while acServer cannot force it
    assert made["Descalificación"]["kind"] == "dsq"
    assert st.post(url, json={"driver_guid": B, "kind": "points", "value": 3, "reason": "x y z"}).status_code == 422   # a league with a catalogue penalises with time


def test_presets_are_listed_and_the_route_is_not_taken_for_a_league_id():
    r = api.get(f"{V}/championships/rulebook/presets")
    assert r.status_code == 200 and r.json()["vrl-base"]["detectors"]["cuts"]["tolerance"] == 5


def test_the_dry_run_says_what_a_rulebook_would_warn_about_and_writes_nothing():
    sid = api.post(f"{V}/servers", json={"name": "t"}).json()["id"]
    _result(sid, "dry.json", {"A": 2, "B": 1})   # A: 10 cuts, B: 5, C: 0
    cid = _league("Calibrar")
    url = f"{V}/championships/{cid}/rulebook/dry-run"
    with Session(engine) as s:
        before = (len(s.exec(select(Notice)).all()), len(s.exec(select(Penalty)).all()))
    assert api.post(url, json={"server_id": sid, "filename": "dry.json"}).json()["notices"] == []   # the league has no cut rule
    r = api.post(url, json={"server_id": sid, "filename": "dry.json", "rulebook": PRESETS["vrl-base"].model_dump()}).json()
    assert [(n["driver_guid"], n["cuts"], n["seconds"]) for n in r["notices"]] == [(A, 10, 5)]   # VRL: more than 5 cuts = +5 s; B's 5 are tolerated
    assert "contactos y de quién es la culpa" in r["not_evaluated"]
    lax = {**PRESETS["vrl-base"].model_dump(), "detectors": {"cuts": {"tolerance": 0, "cuts_per_step": 5, "step_seconds": 5, "max_seconds": 30}}}
    assert {n["driver_guid"]: n["seconds"] for n in api.post(url, json={"server_id": sid, "filename": "dry.json", "rulebook": lax}).json()["notices"]} == {A: 10, B: 5}
    assert api.post(url, json={"server_id": sid, "filename": "nope.json"}).status_code == 404
    with Session(engine) as s:
        assert (len(s.exec(select(Notice)).all()), len(s.exec(select(Penalty)).all())) == before


def test_counting_a_race_uses_the_rulebook_cut_rule():
    sid = api.post(f"{V}/servers", json={"name": "t"}).json()["id"]
    f = _result(sid, "count.json", {"A": 2})
    from app.results import parse_result_file
    with Session(engine) as s:
        c = Championship(name="RB", rulebook=PRESETS["vrl-base"].model_dump())
        s.add(c)
        s.commit()
        assert cut_notices(s, c, sid, "count.json", parse_result_file(f)) == 1
        s.commit()
        assert [(n.driver_guid, n.value) for n in s.exec(select(Notice).where(Notice.server_id == sid))] == [(A, 5000)]


def test_migration_006_adds_the_column_and_leaves_every_league_on_its_older_rules(tmp_path, monkeypatch):
    path = tmp_path / "v5.db"
    monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{path}"))
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:5])
    db.init_db()
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE championships DROP COLUMN rulebook")
    con.execute("INSERT INTO championships (id, name, points_system, practice_required, practice_laps, practice_days, created_at, archived_by) VALUES (1, 'Vieja', '[25, 18]', 0, 5, 7, '2026-01-01', '')")
    con.commit()
    con.close()
    monkeypatch.undo()
    monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{path}"))
    db.init_db()
    con = sqlite3.connect(path)
    assert con.execute("SELECT id, name, rulebook FROM championships").fetchall() == [(1, "Vieja", None)]
    assert [r[0] for r in con.execute("SELECT version FROM schema_migrations ORDER BY version")] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
    db.init_db()   # rerun


def test_the_nascar_cup_preset_and_a_rulebook_saved_before_stage_points_existed():
    from app.rulebook import PRESETS, Rulebook
    nc = PRESETS["nascar-cup"].scoring
    assert nc.table[:4] == [40, 35, 34, 33] and nc.table[35] == 1 and len(nc.table) == 40 and set(nc.table[35:]) == {1}, "40 for the win, one less per place down to 1 at 36th and the rest"
    assert nc.stage_points == [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
    old = Rulebook.model_validate({"schema_version": 1, "scoring": {"table": [25, 18]}})   # a rulebook stored before `stage_points`
    assert old.scoring.stage_points == []


def test_old_rulebooks_receive_disabled_neutralization_defaults():
    rb = Rulebook.model_validate({"schema_version": 1})
    n = rb.neutralization
    assert rb.schema_version == 1 and n.mode == "off" and n.default_mode == "VSC"
    assert n.mode_by_category == {}
    assert (n.green_after_laps, n.green_clear_s, n.green_max_extra_laps) == (3, 15, 3)
    assert (n.slow_car.pace_ratio, n.slow_car.sustained_s, n.spin.stopped_s) == (0.6, 8, 5)
    assert (n.pileup.cars, n.pileup.window_s, n.pileup.escalate_to) == (3, 10, "FCY")
    assert (n.severe.cars, n.severe.escalate_to, n.stale_telemetry_s) == (5, "SC", 1.5)


@pytest.mark.parametrize("bad", [
    {"green_after_laps": 0}, {"green_after_laps": 11}, {"green_after_laps": 1.5},
    {"green_clear_s": 0}, {"green_clear_s": 301},
    {"green_max_extra_laps": -1}, {"green_max_extra_laps": 11},
    {"slow_car": {"pace_ratio": 0}}, {"slow_car": {"pace_ratio": 1}},
    {"slow_car": {"sustained_s": 0}}, {"slow_car": {"sustained_s": 121}},
    {"spin": {"stopped_s": 0}}, {"spin": {"stopped_s": 121}},
    {"pileup": {"cars": 1}}, {"pileup": {"cars": 51}},
    {"pileup": {"window_s": 0}}, {"pileup": {"window_s": 121}},
    {"pileup": {"escalate_to": "SC"}}, {"severe": {"cars": 1}},
    {"severe": {"cars": 51}}, {"severe": {"escalate_to": "FCY"}},
    {"stale_telemetry_s": 0}, {"stale_telemetry_s": 11},
    {"stale_telemetry_s": float("nan")},
    {"mode_by_category": {"GT3": "SC"}}, {"mode": "auto"},
])
def test_neutralization_rejects_invalid_values(bad):
    with pytest.raises(ValueError):
        Rulebook.model_validate({"neutralization": bad})


def test_neutralization_and_flags_survive_storage():
    cid = _league("Neutralization")
    rb = Rulebook.model_validate({
        "neutralization": {"mode": "shadow", "mode_by_category": {"GT3": "FCY"},
                           "green_after_laps": 4, "slow_car": {"pace_ratio": 0.7}},
        "detectors": {"flags": {"blue": "off", "danger": "live"}},
    }).model_dump()
    assert api.put(f"{V}/championships/{cid}/rulebook", json=rb).status_code == 200
    assert api.get(f"{V}/championships/{cid}/rulebook").json()["rulebook"] == rb
    with Session(engine) as s:
        assert Rulebook.model_validate(s.get(Championship, cid).rulebook).model_dump() == rb


def test_applied_session_keeps_a_versioned_copy_until_the_next_application():
    from app.league import _applied_rulebook, session_for
    from app.models import Event, LeagueMember, Server
    from app.schemas.servers import SessionIn
    cid = _league("Snapshot")
    sid = api.post(f"{V}/servers", json={"name": "Snapshot"}).json()["id"]
    with Session(engine) as s:
        s.add(LeagueMember(championship_id=cid, guid=A, name="A"))
        ev = Event(title="Snapshot", league_id=cid,
                   data={"name": "Snapshot", "track": "spa", "cars": ["bmw"]})
        s.add(ev)
        s.commit()
        applied = session_for(s, ev, 0)
        assert applied.rulebook_snapshot.schema_version == 1
        assert len(applied.rulebook_revision) == 64
        server = s.get(Server, sid)
        server.session = applied.model_dump()
        s.add(server)
        s.commit()
        c = s.get(Championship, cid)
        c.rulebook = Rulebook(neutralization={"green_after_laps": 7},
                              detectors={"flags": {"blue": "live"}}).model_dump()
        s.add(c)
        s.commit()
        s.expire_all()
        restored = SessionIn.model_validate(s.get(Server, sid).session)
        assert restored.rulebook_snapshot.neutralization.green_after_laps == 3
        assert _applied_rulebook(s, sid, c).detectors.flags.blue == "shadow"
        next_session = session_for(s, ev, 1)
        assert next_session.rulebook_revision != restored.rulebook_revision
        assert next_session.rulebook_snapshot.neutralization.green_after_laps == 7
        assert next_session.rulebook_snapshot.detectors.flags.blue == "live"
        assert _applied_rulebook(s, sid, None) is None   # no league event running: the old snapshot must not keep penalising
    import pytest
    with pytest.raises(ValueError):   # a category key is bounded, like the rest of the document
        Rulebook.model_validate({"neutralization": {"mode_by_category": {"x" * 65: "VSC"}}})
