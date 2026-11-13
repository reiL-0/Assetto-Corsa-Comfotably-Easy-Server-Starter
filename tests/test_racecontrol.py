"""Race control (app/live/racecontrol.py, app/racecontrol.py) and the CSP hub every server now sends (app/csp.py `extra_for`)."""

import secrets
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import ADMIN
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import csp, supervisor
from app.auth import _sha
from app.config import settings
from app.db import engine
from app.live import acsp
from app.live.racecontrol import RaceControl, state_of
from app.main import app
from app.models import Token, User

V = "/api/v1"
api = TestClient(app, headers=ADMIN)


@pytest.fixture(autouse=True)
def _hub_on(monkeypatch):
    monkeypatch.setattr(settings, "csp_hub", True)   # the default in production; the rest of the suite runs with it off (tests/conftest.py)


class _Inst:
    running = True

    def __init__(self, client):
        self.acsp = client


def _client(role):
    raw = secrets.token_urlsafe(16)
    with Session(engine) as s:
        u = User(username=f"{role}-{raw[:5]}", role=role)
        s.add(u)
        s.commit()
        s.add(Token(user_id=u.id, token_hash=_sha(raw), name="t", created_at=datetime.now(UTC)))
        s.commit()
    return TestClient(app, headers={"Authorization": f"Bearer {raw}"})


def _notices(sid):
    """The on-screen messages the hub would read (they replaced the chat lines)."""
    return [n["text"] for n in api.get(f"{V}/servers/{sid}/race-state").json()["notices"]]


@pytest.fixture
def live(monkeypatch):
    """A server row with a fake running instance whose ACSP client records what it would send."""
    sid = api.post(f"{V}/servers", json={"name": "rc"}).json()["id"]
    c = acsp.ACSPClient(sid)
    c.session = {"session_index": 0, "session_type": 3}   # a race: NASCAR arms a live engine only in a race (the arming outside one is only remembered)
    c.cars[3] = {"driver_name": "Ana", "driver_guid": "76561190000000001"}
    sent = []
    c.send = sent.append
    supervisor._instances[sid] = _Inst(c)
    yield sid, c, sent
    supervisor._instances.pop(sid, None)


def test_the_state_changes_and_every_change_raises_rev():
    rc = RaceControl()
    assert rc.snapshot()["flag"] == "green" and rc.rev == 0
    rc.set_flag("yellow", 60)   # a yellow is only a flag: no pace
    assert (rc.flag, rc.pace_kmh, rc.rev) == ("yellow", None, 1)
    rc.set_flag("vsc", 60)
    assert (rc.flag, rc.pace_kmh, rc.rev) == ("vsc", 60, 2) and rc.vsc_id
    keep = (rc.flag, rc.pace_kmh, rc.vsc_id, rc.rev)
    for bad in ({"flag": "vsc"}, {"flag": "vsc", "pace_kmh": 60, "grace_s": 1}):
        with pytest.raises(ValueError):
            rc.set_flag(**bad)
    assert (rc.flag, rc.pace_kmh, rc.vsc_id, rc.rev) == keep, "a refused VSC changes nothing"
    rc.set_flag("white")
    assert (rc.flag, rc.pace_kmh, rc.vsc_id) == ("white", None, 0)
    p = rc.add_penalty(3, 2)
    assert p == {"id": p["id"], "car_id": 3, "type": "pits", "laps": 2} and rc.rev == 4
    assert rc.add_penalty(3, 1)["id"] > p["id"] and RaceControl().add_penalty(4, 1)["id"] > p["id"], "ids rise across states too (a new session must not reuse one)"
    rc.clear_penalty(p["id"] + 1)
    assert rc.clear_penalty(p["id"]) and not rc.clear_penalty(p["id"]) and rc.rev == 7   # clearing nothing is not a change
    for bad in (lambda: rc.set_flag("red"), lambda: rc.set_flag("vsc", 5), lambda: rc.set_flag("vsc"), lambda: rc.set_flag("vsc", 60, grace_s=1), lambda: rc.set_flag("vsc", 60, tolerance_kmh=99),
                lambda: rc.set_flag("vsc", 60, penalty={"kind": "pits", "laps": 9}), lambda: rc.set_flag("vsc", 60, penalty={"kind": "time"}), lambda: rc.add_penalty(3, 0), lambda: rc.add_penalty(3, 9)):
        with pytest.raises(ValueError):
            bad()


def test_the_state_is_forgotten_when_the_server_stops_or_the_session_changes():
    s1, s2 = {"n": 1}, {"n": 2}
    rc = state_of(901, True, s1, create=True)
    rc.set_flag("vsc", 60)
    assert state_of(901, True, s1) is rc
    fresh = state_of(901, True, s2)   # a new session: back to green, and rev moves on so the clients notice
    assert fresh is not rc and fresh.flag == "green" and fresh.rev == 1 and not fresh.penalties
    assert state_of(901, False, s2) is None and state_of(901, True, s2) is None   # stopped: nothing left, a later start begins clean


def test_the_hub_token_reads_the_state_and_only_a_steward_changes_it(live):
    sid, _, _ = live
    url = f"{V}/servers/{sid}/race-state"
    first = api.get(url).json()
    assert first["rev"] == 0 and first["flag"] == "green" and first["running"] is True and first["penalties"] == [] and first["violations"] == [] and first["grace_left"] == 0
    for role in ("reporter", "driver", "steward"):
        assert _client(role).get(url).status_code == 200, role
    assert TestClient(app).get(url).status_code in (401, 403)
    assert api.get(f"{V}/servers/99999/race-state").status_code == 404
    body = {"flag": "yellow"}
    for role in ("reporter", "driver"):
        assert _client(role).post(f"{V}/servers/{sid}/race-control", json=body).status_code == 403, role
    assert _client("steward").post(f"{V}/servers/{sid}/race-control", json=body).status_code == 200


def test_the_director_sets_the_flag_and_the_screen_says_it(live):
    sid, c, sent = live
    steward = _client("steward")
    r = steward.post(f"{V}/servers/{sid}/race-control", json={"flag": "yellow", "pace_kmh": 60})   # a pace on a yellow is ignored: it is only a flag
    assert r.status_code == 200 and r.json()["flag"] == "yellow" and r.json()["pace_kmh"] is None and _notices(sid) == ["BANDERA AMARILLA: sin adelantar"] and sent == [], "no chat line any more"
    assert steward.get(f"{V}/servers/{sid}/race-state").json()["flag"] == "yellow"
    assert steward.post(f"{V}/servers/{sid}/race-control", json={"flag": "red"}).status_code == 422
    r = steward.post(f"{V}/servers/{sid}/race-control", json={"flag": "vsc"})   # a VSC that says nothing is run with the server's rule (Reglas del servidor: 60 km/h)
    assert r.status_code == 200 and r.json()["flag"] == "vsc" and r.json()["pace_kmh"] == 60
    assert steward.post(f"{V}/servers/{sid}/race-control", json={"flag": "vsc", "pace_kmh": 5}).status_code == 422
    assert steward.post(f"{V}/servers/{sid}/race-control", json={"flag": "green"}).json()["pace_kmh"] is None
    c.session = {"session_index": 1}   # the next session: the state goes back to green on its own
    assert steward.get(f"{V}/servers/{sid}/race-state").json()["flag"] == "green"


def test_a_drive_through_is_listed_once_per_id_and_needs_a_car_on_the_server(live):
    sid, _, sent = live
    steward = _client("steward")
    assert steward.post(f"{V}/servers/{sid}/race-control/penalties", json={"car_id": 9, "laps": 3}).status_code == 409
    p = steward.post(f"{V}/servers/{sid}/race-control/penalties", json={"car_id": 3, "laps": 3})
    assert p.status_code == 201 and p.json()["car_id"] == 3 and sent == [] and "DRIVE-THROUGH para Ana" in _notices(sid)[0]
    assert steward.get(f"{V}/servers/{sid}/race-state").json()["penalties"] == [p.json()]
    assert steward.post(f"{V}/servers/{sid}/race-control/penalties", json={"car_id": 3, "laps": 9}).status_code == 422
    assert steward.delete(f"{V}/servers/{sid}/race-control/penalties/{p.json()['id']}").status_code == 204
    assert steward.delete(f"{V}/servers/{sid}/race-control/penalties/{p.json()['id']}").status_code == 404
    assert steward.get(f"{V}/servers/{sid}/race-state").json()["penalties"] == []


def test_race_control_needs_a_running_server():
    sid = api.post(f"{V}/servers", json={"name": "stopped"}).json()["id"]
    assert _client("steward").post(f"{V}/servers/{sid}/race-control", json={"flag": "yellow"}).status_code == 409
    assert api.get(f"{V}/servers/{sid}/race-state").json()["running"] is False


def test_every_server_sends_the_hub_and_keeps_its_own_options(monkeypatch):
    hub = csp.extra_for(7, "")
    assert hub.startswith("[SCRIPT_100]") and "opr_hub.lua?v=" in hub and "SERVER = 7" in hub
    mine = '[SCRIPT_100]\nSCRIPT = "mine.lua"\n[EXTRA_RULES]\nA = 1'
    both = csp.extra_for(7, mine)
    assert both.startswith(mine) and "[SCRIPT_101]" in both and both.count("opr_hub.lua") == 1   # the admin's script is not replaced
    assert "SERVER = 7" in csp.decode(csp.welcome_with_extra("hola", both))
    monkeypatch.setattr(settings, "csp_hub", False)   # ACM_CSP_HUB=false: the kill switch
    assert csp.extra_for(7, mine) == mine


def test_a_server_with_no_welcome_and_no_extra_still_gets_the_hub_in_cfg_welcome():
    from app.models import Server
    from app.services import server_service
    sid = api.post(f"{V}/servers", json={"name": "hubbed"}).json()["id"]
    with Session(engine) as s:
        d = server_service.write_instance(s.get(Server, sid))
    text = (Path(d) / "cfg" / "welcome.txt").read_text()
    assert "SERVER = %d" % sid in csp.decode(text) and "WELCOME_MESSAGE" in (Path(d) / "cfg" / "server_cfg.ini").read_text()


def test_the_vsc_through_the_api_and_the_pit_report(live):
    sid, c, sent = live
    c.cars[3] = {"driver_name": "Ana", "driver_guid": "76561190000000001", "velocity": [0, 0, 33.0]}   # ~119 km/h
    steward, hub = _client("steward"), _client("reporter")
    body = {"flag": "vsc", "pace_kmh": 60, "grace_s": 5, "tolerance_kmh": 5, "penalty": {"kind": "pits", "laps": 2}}
    r = steward.post(f"{V}/servers/{sid}/race-control", json=body)
    assert r.status_code == 200 and r.json()["flag"] == "vsc" and r.json()["pace_kmh"] == 60 and r.json()["grace_left"] in (4, 5) and r.json()["vsc_penalty"] == {"kind": "pits", "laps": 2, "warnings": 2, "severe_s": 10}
    assert _notices(sid) and sent == []
    pit = f"{V}/servers/{sid}/race-control/pit"
    assert hub.post(pit, json={"car_id": 3, "name": "ana", "pit": False}).status_code == 204
    assert hub.post(pit, json={"car_id": 3, "name": "Beto", "pit": True}).status_code == 409, "the name must be the driver's"
    assert hub.post(pit, json={"car_id": 9, "name": "Ana", "pit": True}).status_code == 409
    assert _client("driver").post(pit, json={"car_id": 3, "name": "Ana", "pit": True}).status_code == 204
    assert TestClient(app).post(pit, json={"car_id": 3, "name": "Ana", "pit": True}).status_code in (401, 403)
    assert steward.post(f"{V}/servers/{sid}/race-control", json={**body, "grace_s": 1}).status_code == 422
    assert steward.post(f"{V}/servers/{sid}/race-control", json={**body, "penalty": {"kind": "time"}}).status_code == 422
    assert steward.post(f"{V}/servers/{sid}/race-control", json={"flag": "green"}).json()["vsc_id"] == 0


def test_the_games_own_penalties_are_off_whenever_the_hub_is_on(monkeypatch):
    from app.services import ini_generator
    ports = {"tcp": 9600, "udp": 9600, "http_internal": 9601, "plugin": 9602, "plugin_local": 9603}
    mine = {"SERVER": {"START_RULE": 2, "RACE_GAS_PENALTY_DISABLED": 0, "ALLOWED_TYRES_OUT": 2, "NAME": "x"}}
    on = ini_generator.server_cfg(mine, ports, False, game_penalties_off=True)
    assert "START_RULE=0" in on and "RACE_GAS_PENALTY_DISABLED=1" in on and "ALLOWED_TYRES_OUT=-1" in on and "NAME=x" in on, "the admin's values do not win here"
    off = ini_generator.server_cfg(mine, ports, False)
    assert "START_RULE=2" in off and "ALLOWED_TYRES_OUT=2" in off
    sid = api.post(f"{V}/servers", json={"name": "nopen", "config": mine}).json()["id"]
    text = api.get(f"{V}/servers/{sid}/server_cfg.ini").text
    assert "ALLOWED_TYRES_OUT=-1" in text and "START_RULE=0" in text and "RACE_GAS_PENALTY_DISABLED=1" in text, "the file a server really starts with"
    monkeypatch.setattr(settings, "csp_hub", False)   # the kill switch also gives the game's own penalties back
    assert "ALLOWED_TYRES_OUT=2" in api.get(f"{V}/servers/{sid}/server_cfg.ini").text


def test_an_fcy_is_a_pace_flag_too_and_its_notice_comes_with_the_state(live):
    sid, c, sent = live
    steward = _client("steward")
    body = {"flag": "fcy", "pace_kmh": 60, "grace_s": 5, "tolerance_kmh": 5, "penalty": {"kind": "pits", "laps": 2}}
    r = steward.post(f"{V}/servers/{sid}/race-control", json=body)
    assert r.json()["flag"] == "fcy" and r.json()["pace_kmh"] == 60 and r.json()["vsc_id"] and r.json()["grace_left"] in (4, 5)
    assert steward.get(f"{V}/servers/{sid}/race-state").json()["flag"] == "fcy" and "FULL COURSE YELLOW" in _notices(sid)[0] and sent == []
    r = steward.post(f"{V}/servers/{sid}/race-control", json={"flag": "fcy"})
    assert r.status_code == 200 and r.json()["pace_kmh"] == 80, "an FCY that says nothing takes the server's FCY pace"
    race = c.session["session_type"]
    c.session["session_type"] = 1   # (the rules are frozen during a race: the server's settings are changed outside it)
    assert api.put(f"{V}/servers/{sid}/rules", json={"detectors": {"vsc": {"fcy_kmh": 70, "grace_s": 7, "penalty": "none"}}}).status_code == 200
    c.session["session_type"] = race
    r = steward.post(f"{V}/servers/{sid}/race-control", json={"flag": "fcy"})
    assert r.status_code == 200 and r.json()["pace_kmh"] == 70 and r.json()["grace_left"] in (6, 7), "the server's own settings: pace and grace"
    assert steward.post(f"{V}/servers/{sid}/race-control", json={"flag": "fcy", "pace_kmh": 90}).json()["pace_kmh"] == 90, "what the director says wins"


def test_a_flag_shown_by_hand_is_a_notice_for_one_driver_or_for_everybody(live):
    sid, _, _ = live
    steward = _client("steward")
    url = f"{V}/servers/{sid}/race-control/flag-notice"
    assert steward.post(url, json={"kind": "blue"}).status_code == 422, "a blue flag is for somebody"
    assert steward.post(url, json={"kind": "nope", "car_id": 3}).status_code == 422
    assert steward.post(url, json={"kind": "blue", "car_id": 3}).status_code == 200
    assert steward.post(url, json={"kind": "red"}).status_code == 200, "the red is for everybody"
    n = {x["text"]: x for x in steward.get(f"{V}/servers/{sid}/race-state").json()["notices"]}
    assert n["BANDERA AZUL: deja pasar al más rápido"]["to"] == 3 and n["BANDERA ROJA: vuelve a boxes"]["to"] == -1
    assert steward.get(f"{V}/servers/{sid}/race-state").json()["flag"] == "green" and steward.get(f"{V}/servers/{sid}/race-state").json()["penalties"] == [], "only a notice: no rule changes"


def test_the_winner_stays_on_the_screen_until_the_session_restarts():
    ref = object()
    rc = state_of(77, True, ref, create=True)
    rc.notify("GANADOR: Ana", "winner", hold="winner")
    rc.set_flag("yellow"); rc.set_flag("green")   # a flag leaving a caution drops the held notices... except the winner
    held = [n for n in rc.snapshot()["notices"] if n["text"] == "GANADOR: Ana"]
    assert held and held[0]["kind"] == "hold", "the winner is held through the flags"
    rc.notify("FREE PASS: Beto", "race", hold="freepass"); rc.set_flag("white"); rc.set_flag("green")
    assert not any(n["text"].startswith("FREE PASS") for n in rc.snapshot()["notices"]), "other held notices still go with the caution"
    fresh = state_of(77, True, object(), create=True)   # the session restarted
    assert not fresh.snapshot()["notices"], "a new session starts clean"
    state_of(77, False)


def test_notices_expire_are_capped_and_never_carry_quotes():
    rc = RaceControl()
    a = rc.notify('di "hola" \\ adios', "race", now=100.0)
    assert '"' not in a["text"] and "\\" not in a["text"]
    snap = rc.snapshot(now=103.0)
    assert snap["notices"] == [{"id": a["id"], "kind": "race", "left_s": 7, "text": a["text"], "to": -1}]
    assert rc.snapshot(now=111.0)["notices"] == [], "gone after its ttl"


def test_manual_checkered_flag_writes_finish_marker(live, tmp_path, monkeypatch):
    import json
    sid, c, sent = live
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    rc = state_of(sid, True, c.session, create=True)
    rc.leader_laps_seen = 78
    steward = _client("steward")
    r = steward.post(f"{V}/servers/{sid}/race-control", json={"flag": "checkered"})
    assert r.status_code == 200
    markers = list((tmp_path / "instances" / str(sid) / "results").glob("*.finish"))
    assert len(markers) == 1
    m = json.loads(markers[0].read_text())
    assert m["laps"] == 78
    ids = [rc.notify(f"n{i}", now=200.0)["id"] for i in range(100)]
    assert ids == sorted(set(ids)), "ids only rise"
    assert len(rc.snapshot(now=201.0)["notices"]) == 64, "the last 64 are kept (a full field gets one instruction each)"
    rev = rc.rev
    rc.notify("x", "info", 5, now=202.0)
    assert rc.rev == rev + 1 and rc.snapshot(now=202.5)["notices"][-1]["to"] == 5


def test_the_probe_goes_only_to_the_servers_named_in_the_setting(monkeypatch):
    monkeypatch.setattr(settings, "csp_trainer_servers", "")   # (the trainer is on everywhere by default: this one counts the probe's scripts alone)
    assert "opr_rc_probe" not in csp.extra_for(2, "")   # off by default
    monkeypatch.setattr(settings, "csp_probe_servers", "2, 3")
    on = csp.extra_for(2, "")
    assert on.count("opr_hub.lua") == 1 and "[SCRIPT_101]\nSCRIPT = \"https://www.oppenpaddockracing.site/static/csp/opr_rc_probe.lua\"" in on
    assert "opr_rc_probe" not in csp.extra_for(7, "")   # another server: only the hub
    assert csp.extra_for(3, '[SCRIPT_100]\nSCRIPT = "mine.lua"').count("[SCRIPT_") == 3   # the admin's script, the hub (101) and the probe (102) all stay


def test_the_trainer_goes_only_to_the_servers_named_when_the_setting_is_an_explicit_list(monkeypatch):
    monkeypatch.setattr(settings, "csp_trainer_servers", "")
    assert "opr_trainer" not in csp.extra_for(2, "")   # empty = nobody: the emergency stop
    monkeypatch.setattr(settings, "csp_trainer_servers", "2")
    on = csp.extra_for(2, "")
    assert on.count("opr_hub.lua") == 1 and '[SCRIPT_101]\nSCRIPT = "https://www.oppenpaddockracing.site/static/csp/opr_trainer.lua?v=' in on
    assert "opr_trainer" not in csp.extra_for(7, "")   # another server: only the hub
    monkeypatch.setattr(settings, "csp_probe_servers", "2")
    both = csp.extra_for(2, "")
    assert "[SCRIPT_101]\nSCRIPT = \"https://www.oppenpaddockracing.site/static/csp/opr_rc_probe.lua\"" in both and "[SCRIPT_102]" in both and both.index("opr_rc_probe") < both.index("opr_trainer")


def test_the_server_selector_means_every_server_unless_it_is_told_otherwise():
    on = csp.server_listed
    assert on(7, "*") and on(1, " * ") and on(4242, "*")
    assert not on(7, "") and not on(7, "  ")                        # empty = nobody
    assert on(2, "2") and not on(3, "2") and on(3, "2, 3")          # a list of ids
    assert not on(12, "1,2") and not on(1, "12")                    # ids, never substrings
    assert on(1, "*,!2") and not on(2, "*,!2") and on(3, "*, !2")   # every server but one: the brake for a single server
    assert not on(2, "!2") and not on(1, "!2")                      # an exclusion alone turns nothing on
    assert not on(2, "*,!2,2")                                      # the brake wins over a listing


def test_a_selector_that_is_not_one_is_refused_at_start_instead_of_switching_things_off_quietly():
    import pytest
    from app.config import Settings
    for ok in ("*", "", "  ", "2", "2, 3", "*,!3", "*, !3 ,", ",1"):   # what is allowed, with the blanks and the stray comma people leave
        assert Settings(csp_claim_servers=ok, csp_trainer_servers=ok, csp_probe_servers=ok)
    for bad in ("all", "true", "1;2", "*,!*", "*,!", "!", "2-3", "a", "**", "1 2", "*,!x"):
        for field in ("csp_claim_servers", "csp_trainer_servers", "csp_probe_servers"):
            with pytest.raises(ValueError):
                Settings(**{field: bad})


def test_every_server_gets_the_trainer_by_default_and_the_selector_can_stop_one(monkeypatch):
    for sid in (1, 2, 3, 7, 4242):   # the three of today and any server made later
        e = csp.extra_for(sid, "")
        assert e.count("opr_hub.lua") == 1 and "opr_trainer.lua?v=" in e, sid
        assert "opr_rc_probe" not in e, sid                          # the probe stays temporary and opt-in
    monkeypatch.setattr(settings, "csp_trainer_servers", "*,!3")
    assert "opr_trainer" not in csp.extra_for(3, "") and "opr_trainer" in csp.extra_for(4, "") and csp.extra_for(3, "").count("opr_hub.lua") == 1
    monkeypatch.setattr(settings, "csp_hub", False)                  # the hub's own kill switch also stops the trainer: it rides on the hub's section
    assert csp.extra_for(4, "") == ""


def test_rulebook_edits_are_refused_in_a_race_and_allowed_between_races(live):
    from app.models import Championship, Event, Schedule
    sid, client, _ = live
    cid = api.post(f"{V}/championships", json={"name": "Frozen rules"}).json()["id"]
    with Session(engine) as s:
        ev = Event(title="Frozen", league_id=cid)
        s.add(ev)
        s.commit()
        sc = Schedule(server_id=sid, event_id=ev.id, start_at=0, state="running")
        s.add(sc)
        s.commit()
        schedule_id = sc.id
    url = f"{V}/championships/{cid}/rulebook"
    assert api.put(url, json={"neutralization": {"green_after_laps": 4}}).status_code == 409
    assert api.patch(f"{V}/championships/{cid}", json={"name": "Frozen rules", "auto_rules": {"cuts_per_step": 1, "step_seconds": 5, "max_seconds": 5}}).status_code == 409
    with Session(engine) as s:
        assert s.get(Championship, cid).rulebook is None
    client.session = {"session_type": 1}
    assert api.put(url, json={"neutralization": {"green_after_laps": 4}}).status_code == 200
    with Session(engine) as s:
        sc = s.get(Schedule, schedule_id)
        sc.state = "done"
        s.add(sc)
        s.commit()


def test_applied_rulebook_cannot_be_replaced_during_a_race(live, monkeypatch):
    import asyncio
    from app.models import Server
    from app.rulebook import Rulebook
    from app.schemas.servers import SessionIn
    from app.services import server_service
    sid, client, _ = live
    monkeypatch.setattr(server_service, "check_content", lambda *args: {"bmw": [""]})
    with Session(engine) as s:
        server = s.get(Server, sid)
        server.session = {"rulebook_snapshot": Rulebook().model_dump(), "rulebook_league_id": 123}
        s.add(server)
        s.commit()
        body = SessionIn(name="Changed", track="spa", cars=["bmw"], race_laps=5,
                         rulebook_league_id=123,
                         rulebook_snapshot=Rulebook(neutralization={"green_after_laps": 6}))
        with pytest.raises(server_service.ServerError) as caught:
            asyncio.run(server_service.apply(s, server, body))
        assert caught.value.status == 409
        s.refresh(server)
        assert server.session["rulebook_snapshot"]["neutralization"]["green_after_laps"] == 3
        client.session = {"session_type": 1}
        body.restart = False
        from app import league
        monkeypatch.setitem(league._EFFECTIVE, sid, (0, {"flag_modes": {"blue": "live", "danger": "live"}}))
        asyncio.run(server_service.apply(s, server, body))
        assert sid not in league._EFFECTIVE
        assert server.session["rulebook_snapshot"]["neutralization"]["green_after_laps"] == 6


def test_drs_use_while_off_is_sanctioned_once_per_period(live):
    from app.rulebook import DrsRule
    sid, c, _sent = live
    hub = _client("reporter")
    use = f"{V}/servers/{sid}/race-control/drs-use"
    rc = state_of(sid, True, c.session, create=True)
    rule = DrsRule(start_laps_off=2).model_dump()
    rc.drs_tick(rule, 1000.0, 0, None)
    assert api.get(f"{V}/servers/{sid}/race-state").json()["drs"] == {"on": False, "why": "start", "until": 2}
    assert hub.post(use, json={"car_id": 3, "name": "Beto"}).status_code == 409, "the name must be the driver's"
    assert hub.post(use, json={"car_id": 3, "name": "Ana"}).status_code == 204
    assert [p["car_id"] for p in rc.penalties] == [3] and any("PENALIZACIÓN: DRS" in t for t in _notices(sid))
    n = len(_notices(sid))
    assert hub.post(use, json={"car_id": 3, "name": "Ana"}).status_code == 204 and len(_notices(sid)) == n and len(rc.penalties) == 1, "once per period"
    rc.drs_tick({**rule, "start_laps_off": 3}, 1050.0, 0, None)   # the rule changes mid-session: the sanction already given is not forgotten
    assert rc.drs_use(3, "76561190000000001", 1060.0) is None and len(rc.penalties) == 1
    rc.drs_tick(rule, 1100.0, 2, None)
    assert api.get(f"{V}/servers/{sid}/race-state").json()["drs"]["on"] is True
    rc.drs_tick(None, 1101.0, 2, None)
    assert api.get(f"{V}/servers/{sid}/race-state").json()["drs"] is None, "no rule, no DRS state"


def test_damage_report_goes_to_the_rc_state_and_is_checked_against_the_driver(live):
    sid, c, _sent = live
    hub = _client("reporter")
    url = f"{V}/servers/{sid}/race-control/damage"
    rc = state_of(sid, True, c.session, create=True)
    assert hub.post(url, json={"car_id": 3, "name": "Beto", "level": 80}).status_code == 409, "the name must be the driver's"
    assert hub.post(url, json={"car_id": 3, "name": "Ana", "level": 101}).status_code == 422
    assert hub.post(url, json={"car_id": 3, "name": "Ana", "level": 80}).status_code == 204
    assert rc.damage[3][0] == 80


def test_the_directors_white_flag_is_the_last_lap_in_a_laps_race_and_in_a_timed_one():
    from app.live import acsp
    from app.racecontrol import last_lap_bytes
    laps_race = {"session_type": 3, "session_index": 2, "name": "Race", "laps": 20, "time_min": 0, "wait_time": 60}
    pkt, orig = last_lap_bytes(laps_race, leader_laps=7, elapsed_ms=0)
    assert pkt == acsp.encode_set_session_info(2, "Race", 3, 8, 0, 60) and orig == {"laps": 20, "time_min": 0}   # the leader's lap + 1
    assert last_lap_bytes({**laps_race, "laps": 8}, 7, 0) is None   # (already the last lap)
    assert last_lap_bytes(laps_race, 7, 0, restore=orig)[0] == acsp.encode_set_session_info(2, "Race", 3, 20, 0, 60)   # yellow or green: the distance comes back
    timed = {**laps_race, "laps": 0, "time_min": 30}
    pkt, orig = last_lap_bytes(timed, 7, 12 * 60000 + 5000)
    assert pkt == acsp.encode_set_session_info(2, "Race", 3, 0, 12, 60) and orig == {"laps": 0, "time_min": 30}   # the time is up: the next crossing is the chequered
    assert last_lap_bytes({**laps_race, "session_type": 2}, 7, 0) is None


def test_pit_speed_is_sanctioned_once_per_pass_with_the_servers_penalty_and_the_managers_own_speed(live):
    from app import league
    sid, client, sent = live
    rc = state_of(sid, True, client.session, create=True)
    rc.audit_guids = {3: client.cars[3]["driver_guid"]}
    rc.flag_modes = {k: "live" for k in ("blue", "danger", "yellow", "meatball", "black", "red")}
    steward = _client("steward")
    client.session.update({"track": "imola", "track_config": ""})
    assert steward.get(f"{V}/servers/{sid}/race-control/pit-limit").json()["limit_kmh"] == 0
    assert steward.put(f"{V}/servers/{sid}/race-control/pit-limit", json={"limit_kmh": 80, "tolerance_kmh": 5, "penalty_s": 0}).status_code == 200
    assert league.effective_rules(sid)["pit"] == {"limit_kmh": 80, "tolerance_kmh": 5, "penalty_s": 0}, "the rule is the SERVER's: the same on any track"
    assert steward.get(f"{V}/servers/{sid}/rules").json()["detectors"]["pit"]["limit_kmh"] == 80
    api.get(f"{V}/servers/{sid}/race-state")   # (the state poll reads the track's rule)
    assert rc.pit_limit and rc.snapshot()["pit_limit"] == {"limit_kmh": 80, "tolerance_kmh": 5}
    assert rc.pit_speed(3, 84.0, 1000.0) is None, "inside the tolerance (80 + 5)"
    assert rc.pit_speed(3, 90.0, 1001.0) == {"kmh": 90, "limit": 80, "step": 0} and [p["car_id"] for p in rc.penalties] == [3], "a drive-through"
    assert rc.pit_speed(3, 95.0, 1005.0) is None, "the same pass: once"
    assert rc.pit_speed(3, 95.0, 1030.0) is None, "the same long pass: 20 s later he is still over, no second sanction without a new entry"
    rc.report_pit(3, True, now=1040.0)   # the hub's report while the car STAYS in the pit lane
    assert rc.pit_speed(3, 95.0, 1050.0) is None, "a pit report is no new pass"
    rc.lap_done(3, 95000, 0, 8, 1060.0, "")   # a lap completed since: another pass through the pit lane
    assert rc.pit_speed(3, 95.0, 1070.0) is not None, "another pass: a new offence"
    assert steward.put(f"{V}/servers/{sid}/race-control/pit-limit", json={"limit_kmh": 0}).status_code == 200 and league.effective_rules(sid)["pit"] is None
    rc2 = RaceControl(session_ref={"session_type": 3}); rc2.server_id = 9
    rc2.audit_guids = {1: "G1"}; rc2.pit_limit = {"limit_kmh": 60, "tolerance_kmh": 5, "penalty_s": 5}
    assert rc2.pit_speed(1, 70.0, 10.0)["step"] == 5 and not rc2.penalties, "seconds on the result, no drive-through"


def test_the_hub_report_of_pit_speed_is_judged_with_the_managers_own_speed(live):
    sid, client, sent = live
    rc = state_of(sid, True, client.session, create=True)
    rc.audit_guids = {3: client.cars[3]["driver_guid"]}
    rc.flag_modes = {k: "live" for k in ("blue", "danger", "yellow", "meatball", "black", "red")}
    rc.pit_limit = {"limit_kmh": 80, "tolerance_kmh": 5, "penalty_s": 0}
    steward = _client("steward")
    url, body = f"{V}/servers/{sid}/race-control/pit-speed", {"car_id": 3, "name": "Ana"}
    client.cars[3]["velocity"] = [0.0, 0.0, 20.0]   # 72 km/h: the hub claims more, the server says no
    assert steward.post(url, json=body).status_code == 204 and not rc.penalties
    client.cars[3]["velocity"] = [0.0, 0.0, 25.0]   # 90 km/h
    assert steward.post(url, json=body).status_code == 204 and [p["car_id"] for p in rc.penalties] == [3]
    assert steward.post(url, json={"car_id": 3, "name": "Beto"}).status_code == 409, "not that driver"


def test_the_directors_field_table_has_the_race_order_gaps_and_what_each_driver_has_on_him(live):
    from app.live.board import Driver
    sid, client, sent = live
    rc = state_of(sid, True, client.session, create=True)
    client.board.session = {"session_type": 3, "name": "Race"}
    for car, name, guid, laps, total, best in ((3, "Ana", "G3", 5, 500_000, 95_000), (4, "Beto", "G4", 5, 502_500, 96_000), (5, "Cleo", "G5", 4, 410_000, 97_000)):
        client.board.drivers.append(Driver(car_id=car, name=name, guid=guid, model="bmw", skin="red", connected=True, laps=laps, total_ms=total, best_ms=best))
    rc.audit_guids = {3: "G3", 4: "G4", 5: "G5"}
    rc.bw_shown.add("G4"); rc.disqualified.add("G5"); rc.add_penalty(4, 3); rc.conduct["G4"] = 3
    rc.report_pit(3, True)
    steward = _client("steward")
    r = steward.get(f"{V}/servers/{sid}/race-control/field")
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert [x["name"] for x in rows] == ["Ana", "Beto", "Cleo"] and rows[0]["gap"] is None and rows[0]["flags"] == ["boxes"]
    assert rows[1]["gap"] == {"laps": 0, "ms": 2500} and rows[1]["flags"] == ["bw", "dt"] and rows[1]["conduct"] == 3 and rows[1]["best_ms"] == 96000
    assert rows[2]["gap"] == {"laps": 1, "ms": None} and rows[2]["flags"] == ["dsq"]
    assert [x["driver_guid"] for x in rows] == ["G3", "G4", "G5"], "the table kick needs the driver GUID (kick checks who is in the slot)"
    assert _client("driver").get(f"{V}/servers/{sid}/race-control/field").status_code in (401, 403), "the director table is for stewards"
