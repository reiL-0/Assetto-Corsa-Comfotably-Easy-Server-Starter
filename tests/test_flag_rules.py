"""The rest of the flags as rules (RaceControl._flag_judges / yellow_tick / conduct): danger, yellow, orange disc -> black, black and white -> black, red."""
from app.live import racecontrol as rcmod
from app.live.field import Sample
from app.live.racecontrol import RaceControl

L = 4000.0


def rc_with(**modes):
    rc = RaceControl(session_ref={"session_type": 3})
    rc.server_id = 9
    rc.flag_modes = {k: "live" for k in ("blue", "danger", "yellow", "meatball", "black", "red")}
    rc.flag_modes.update(modes)
    rc.audit_guids = {c: f"G{c}" for c in range(1, 8)}
    return rc


def rules(rc, **over):
    rc.rules = {**rcmod.FLAG_DEFAULTS, **{k: {**rcmod.FLAG_DEFAULTS[k], **v} for k, v in over.items()}}


def sm(progress, mono):
    return Sample(progress, 0, 0, mono, mono - 10, None)


def msgs(rc, kind=None):
    return [m for m in rc.take_flag_messages() if kind is None or m.get("kind") == kind or m["msg"] == kind]


def offences(rc):
    return [m for m in rc.take_flag_messages() if m["msg"] == "offence"]


def test_danger_is_one_offence_per_stopped_car_and_only_with_a_ladder():
    rcmod._flag_dues_pending.clear()
    rc = rc_with(); rules(rc, danger={"ladder": [0, 5], "zone_m": 80, "max_kmh": 120})
    for t in (0.0, 1.0):   # the stopped car (2) was moving, then stops for 3+ s
        rc.autoflags_tick({1: sm(0.40, t), 2: sm(0.5, t)}, {1: 150.0, 2: 100.0 if t == 0 else 0.0}, L, t, 1000.0 + t)
    for t in (2.0, 3.0, 4.0, 5.0):
        rc.autoflags_tick({1: sm(0.40, t), 2: sm(0.5, t)}, {1: 150.0, 2: 0.0}, L, t, 1000.0 + t)
    assert (1, 2) in rc.autoflags.danger, "the detector sees the stopped car"
    rc.autoflags_tick({1: sm(0.5 - 60 / L, 6.0), 2: sm(0.5, 6.0)}, {1: 100.0, 2: 0.0}, L, 6.0, 1006.0)
    assert not offences(rc), "slow enough inside the zone: nothing"
    rc.autoflags_tick({1: sm(0.5 - 60 / L, 7.0), 2: sm(0.5, 7.0)}, {1: 160.0, 2: 0.0}, L, 7.0, 1007.0)
    [o] = offences(rc)
    assert (o["kind"], o["car"], o["step"]) == ("danger", 1, 0) and [p["car_id"] for p in rc.penalties] == [1]
    rc.autoflags_tick({1: sm(0.5 - 40 / L, 8.0), 2: sm(0.5, 8.0)}, {1: 170.0, 2: 0.0}, L, 8.0, 1008.0)
    assert not offences(rc), "once per stopped car"
    rc2 = rc_with()   # the default has no ladder: the warning only
    for t in (0.0, 1.0, 2.0, 3.0, 4.0, 5.0):
        rc2.autoflags_tick({1: sm(0.40, t), 2: sm(0.5, t)}, {1: 150.0, 2: 100.0 if t == 0 else 0.0}, L, t, 1000.0 + t)
    rc2.autoflags_tick({1: sm(0.5 - 60 / L, 6.0), 2: sm(0.5, 6.0)}, {1: 200.0, 2: 0.0}, L, 6.0, 1006.0)
    assert not offences(rc2) and not rc2.penalties


def test_yellow_open_and_due_go_through_the_ladder_in_live_only():
    rcmod._flag_dues_pending.clear()
    rc = rc_with(); rc.flag = "yellow"
    calls = iter([[("open", 1, [2], 15)], [("cleared", 1, 0)], [("due", 1, 0)]])
    rc.yellowjudge.step = lambda *a, **k: next(calls)
    rc.yellow_tick({}, {}, 1.0, 1001.0)
    [o] = [m for m in rc.take_flag_messages() if m["msg"] == "yellow_open"]
    assert o["car"] == 1 and o["passed"] == [2] and o["seconds"] == 15
    rc.yellow_tick({}, {}, 2.0, 1002.0)
    assert [m for m in rc.take_flag_messages() if m["msg"] == "yellow_cleared"]
    rc.yellow_tick({}, {}, 3.0, 1003.0)
    [d] = offences(rc)
    assert (d["kind"], d["car"], d["step"]) == ("yellow", 1, 0)
    rc = rc_with(yellow="shadow"); rc.flag = "yellow"; rc.yellowjudge.step = lambda *a, **k: [("due", 1, 0)]
    rc.yellow_tick({}, {}, 1.0, 1001.0)
    assert not offences(rc) and not rc.penalties, "shadow: decided and written down, nobody sanctioned"
    rc = rc_with(); rc.flag = "yellow"; rc.nascar = object()
    seen = []
    rc.yellowjudge.step = lambda mono, fid, active, *a: seen.append(active) or []
    rc.yellow_tick({}, {}, 1.0, 1001.0)
    assert seen == [False], "a NASCAR caution has its own rules"


def test_a_crawling_car_gets_the_disc_then_the_black_flag_then_seconds():
    rcmod._flag_dues_pending.clear()
    rc = rc_with(); rules(rc, meatball={"sustained_s": 20, "pit_laps": 2}, black={"pit_laps": 2, "seconds": 30})
    speeds = {1: 200.0, 2: 205.0, 3: 198.0, 4: 202.0, 5: 60.0}   # car 5 runs at 30 % of the cars around it
    for t in range(0, 22):
        rc.autoflags_tick({c: sm(0.1 * c, float(t)) for c in speeds}, speeds, L, float(t), 1000.0 + t)
    assert {k: v for k, v in rc.meatball["G5"].items() if k != "since"} == {"car": 5, "laps": 2}
    assert rc.auto_for(5, 0, 21.0)["meatball"] == {"laps": 2}, "the driver's screen gets it"
    rc.lap_done(5, 100000, 0, 5, 1030.0, "G5")
    assert rc.meatball["G5"]["laps"] == 1
    rc.lap_done(5, 100000, 0, 6, 1130.0, "G5")
    assert "G5" not in rc.meatball and rc.black["G5"]["laps"] == 2, "two laps without the pits: the black flag"
    assert rc.auto_for(5, 0, 21.0)["black"] == {"laps": 2}
    rc.lap_done(5, 100000, 0, 7, 1230.0, "G5")
    rc.lap_done(5, 100000, 0, 8, 1330.0, "G5")
    [o] = offences(rc)
    assert (o["kind"], o["step"]) == ("black", 30) and [r["seconds"] for r in rcmod.pending_flag_dues()] == [30], "ignored: the seconds, no drive-through"
    rcmod._flag_dues_pending.clear()
    rc = rc_with(); rules(rc, meatball={"sustained_s": 5})   # serving it: a pit-lane pass
    for t in range(0, 8):
        rc.autoflags_tick({c: sm(0.1 * c, float(t)) for c in speeds}, speeds, L, float(t), 1000.0 + t)
    assert "G5" in rc.meatball
    rc.report_pit(5, True, now=rc.clock.time() + 1)
    assert "G5" not in rc.meatball


def test_conduct_builds_to_black_and_white_then_black_and_a_pit_visit_starts_it_again():
    rc = rc_with(); rules(rc, black={"bw_at": 2, "black_at": 4, "pit_laps": 2})
    rc.conduct_add("G1", 1)
    assert "G1" not in rc.bw_shown and "bw" not in (rc.auto_for(1, 0, 1.0) or {})
    rc.conduct_add("G1", 1)
    assert rc.auto_for(1, 0, 1.0)["bw"] == {"n": 2}, "the warning: no sanction"
    rc.conduct_add("G1", 1)
    rc.conduct_add("G1", 1)
    a = rc.auto_for(1, 0, 1.0)
    assert {k: v for k, v in rc.black["G1"].items() if k != "since"} == {"car": 1, "laps": 2} and a["black"] == {"laps": 2} and "bw" not in a
    rc.report_pit(1, True, now=rc.clock.time() + 1)
    assert "G1" not in rc.black and rc.conduct["G1"] == 0 and "G1" not in rc.bw_shown, "served: it starts again"


def test_a_sanction_of_any_rule_counts_as_conduct():
    rcmod._flag_dues_pending.clear()
    rc = rc_with(); rules(rc, black={"bw_at": 2, "black_at": 5}, yellow={"ladder": [5]})
    rc.flag = "yellow"
    for _ in range(2):
        rc._flag_sanction("yellow", 1, rc._rule("yellow"), 1000.0)
    assert rc.conduct["G1"] == 2 and "G1" in rc.bw_shown
    rc._flag_sanction("black", 1, {"ladder": [30]}, 1000.0, conduct=False)
    assert rc.conduct["G1"] == 2, "the black flag's own seconds do not feed the counter that made it"
    rcmod._flag_dues_pending.clear()


def test_red_flag_gives_pit_s_seconds_and_the_next_flag_withdraws_it():
    rcmod._flag_dues_pending.clear()
    rc = rc_with(); rules(rc, red={"pit_s": 30, "ladder": [0, 5]})
    rc.set_red(True, now=1000.0)
    assert rc.auto_for(1, 0, 1.0)["red"]["pit"] is False
    rc.report_pit(2, True, now=1010.0)   # car 2 made it to the pit lane
    sp = {1: 200.0, 2: 20.0, 3: 180.0}
    rc.autoflags_tick({c: sm(0.1 * c, 5.0) for c in sp}, sp, L, 5.0, 1025.0)
    assert not offences(rc), "inside the time"
    rc.autoflags_tick({c: sm(0.1 * c, 40.0) for c in sp}, sp, L, 40.0, 1040.0)
    assert sorted(m["car"] for m in offences(rc)) == [1, 3], "the cars not in the pits are sanctioned once; car 2 is not"
    rc.autoflags_tick({c: sm(0.1 * c, 41.0) for c in sp}, sp, L, 41.0, 1041.0)
    assert not offences(rc), "once per red flag"
    rc.set_flag("green")
    assert rc.red_since is None and "red" not in (rc.auto_for(1, 0, 1.0) or {})
    rcmod._flag_dues_pending.clear()


def test_nothing_is_sanctioned_in_shadow():
    rcmod._flag_dues_pending.clear()
    rc = rc_with(red="shadow", meatball="shadow", black="shadow"); rules(rc, red={"pit_s": 10, "ladder": [0]})
    rc.set_red(True, now=1000.0)
    rc.autoflags_tick({1: sm(0.1, 20.0)}, {1: 200.0}, L, 20.0, 1020.0)
    assert not offences(rc) and not rc.penalties and not rcmod.pending_flag_dues()
    assert "red" not in (rc.auto_for(1, 0, 1.0) or {}), "shadow shows nothing"


def test_a_flag_is_served_only_by_a_pit_visit_after_it_was_given_and_a_full_ledger_sanctions_nobody():
    rc = rc_with(); rules(rc, black={"bw_at": 1, "black_at": 2})
    rc.report_pit(1, True, now=rc.clock.time())   # already in the pit lane when the flag comes
    rc.conduct_add("G1", 1); rc.conduct_add("G1", 1)
    assert "G1" in rc.black
    rc.report_pit(1, True, now=rc.clock.time() - 1)   # (the same pass reported again, before the flag: not a new visit)
    assert "G1" in rc.black, "a car that was in the pits when it got the black flag has to come in again"
    rc.report_pit(1, True, now=rc.clock.time() + 5)
    assert "G1" not in rc.black
    rcmod._flag_dues_pending.clear()
    for i in range(rcmod.MAX_FLAG_DUES_PENDING):
        rcmod._flag_dues_pending[("x", i)] = {}
    rc = rc_with(); rules(rc, yellow={"ladder": [0]})
    assert rc._flag_sanction("yellow", 1, rc._rule("yellow"), 1000.0) is False
    assert not rc.penalties and not rc.conduct and rc.flag_count.get(("G1", "yellow"), 0) == 0, "nothing recorded, nobody sanctioned"
    rcmod._flag_dues_pending.clear()
