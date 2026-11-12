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


def test_a_damaged_car_gets_the_disc_then_the_black_flag_then_the_disqualification():
    rcmod._flag_dues_pending.clear()
    rc = rc_with(); rules(rc, meatball={"damage_pct": 60, "pit_laps": 2}, black={"pit_laps": 2})
    sp = {1: 200.0, 5: 120.0}
    rc.damage_report(5, 40, 1000.0)
    rc.autoflags_tick({c: sm(0.1 * c, 1.0) for c in sp}, sp, L, 1.0, 1001.0)
    assert not rc.meatball, "40 % is under the limit"
    rc.damage_report(5, 75, 1002.0)
    rc.autoflags_tick({c: sm(0.1 * c, 2.0) for c in sp}, sp, L, 2.0, 1003.0)
    assert {k: v for k, v in rc.meatball["G5"].items() if k != "since"} == {"car": 5, "laps": 2}
    assert rc.auto_for(5, 0, 2.0)["meatball"] == {"laps": 2}, "the driver's screen gets it"
    rc.lap_done(5, 100000, 0, 5, 1030.0, "G5")
    assert rc.meatball["G5"]["laps"] == 1
    rc.lap_done(5, 100000, 0, 6, 1130.0, "G5")
    assert "G5" not in rc.meatball and "G5" not in rc.black, "two laps without the pits: NO automatic black flag (it is the director's)"
    assert rc.show_black(5) and rc.black["G5"]["laps"] == 2, "the director shows it: two laps to come in"
    rc.lap_done(5, 100000, 0, 7, 1230.0, "G5")
    rc.lap_done(5, 100000, 0, 8, 1330.0, "G5")
    [o] = offences(rc)
    assert (o["kind"], o["step"]) == ("black", "dsq") and [(r["kind"], r["seconds"]) for r in rcmod.pending_flag_dues()] == [("black", 0)], "a black flag not obeyed: disqualified"
    assert not rc.penalties, "no drive-through for it"
    assert "G5" in rc.disqualified, "disqualified: out of the server, and he cannot come back to it"
    rcmod._flag_dues_pending.clear()
    rc = rc_with(); rules(rc)   # serving it: a pit-lane pass; and a stale report is no report
    rc.damage_report(5, 90, 1000.0)
    rc.autoflags_tick({c: sm(0.1 * c, 1.0) for c in sp}, sp, L, 1.0, 1001.0)
    assert "G5" in rc.meatball
    rc.report_pit(5, True, now=rc.clock.time() + 1)
    assert "G5" not in rc.meatball
    rc = rc_with(); rc.damage_report(5, 90, 1000.0)
    rc.autoflags_tick({c: sm(0.1 * c, 1.0) for c in sp}, sp, L, 1.0, 1100.0)
    assert not rc.meatball and 5 not in rc.damage, "a report older than DAMAGE_FRESH_S is dropped"


def test_the_speed_test_of_the_disc_is_off_unless_asked():
    sp = {1: 200.0, 2: 205.0, 3: 198.0, 4: 202.0, 5: 60.0}
    for use, expected in ((False, False), (True, True)):
        rc = rc_with(); rules(rc, meatball={"use_speed": use, "sustained_s": 20})
        for t in range(0, 22):
            rc.autoflags_tick({c: sm(0.1 * c, float(t)) for c in sp}, sp, L, float(t), 1000.0 + t)
        assert ("G5" in rc.meatball) is expected


def test_conduct_builds_to_black_and_white_then_black_and_a_pit_visit_starts_it_again():
    rc = rc_with(); rules(rc, black={"bw_at": 2, "black_at": 4, "pit_laps": 2})
    rc.conduct_add("G1", 1)
    assert "G1" not in rc.bw_shown and "bw" not in (rc.auto_for(1, 0, 1.0) or {})
    rc.conduct_add("G1", 1)
    assert rc.auto_for(1, 0, 1.0)["bw"] == {"n": 2}, "the warning: no sanction"
    rc.conduct_add("G1", 1)
    rc.conduct_add("G1", 1)
    assert "G1" not in rc.black and "black" not in (rc.auto_for(1, 0, 1.0) or {}), "conduct never gives the black flag by itself"
    assert rc.show_black(1)   # the director does
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


def test_red_flag_not_obeyed_gives_no_automatic_black_flag():
    rcmod._flag_dues_pending.clear()
    rc = rc_with(); rules(rc, red={"laps": 1})
    rc.set_red(True, now=1000.0)
    a = rc.auto_for(1, 0, 1.0)["red"]
    assert a == {"laps": 1, "pit": False}
    rc.report_pit(2, True, now=1010.0)   # car 2 came into the pit lane
    assert rc.auto_for(2, 0, 1.0)["red"] == {"laps": 1, "pit": True}
    rc.lap_done(1, 100000, 0, 5, 1050.0, "G1")   # car 1 finished its lap on the track: the black flag
    rc.lap_done(2, 100000, 0, 5, 1050.0, "G2")   # car 2 had come in: it obeyed
    assert "G1" not in rc.black and "G2" not in rc.black, "not obeyed: still no automatic black flag"
    rc.set_flag("checkered"); rc.set_flag("green")
    assert rc.red_since is not None and "red" in (rc.auto_for(3, 0, 1.0) or {}), "the red flag goes with any track flag"
    rc.set_red(False)
    assert rc.red_since is None and "red" not in (rc.auto_for(3, 0, 1.0) or {})
    rcmod._flag_dues_pending.clear()


def test_nothing_is_sanctioned_in_shadow():
    rcmod._flag_dues_pending.clear()
    rc = rc_with(red="shadow", meatball="shadow", black="shadow")
    rc.set_red(True, now=1000.0)
    rc.lap_done(1, 100000, 0, 5, 1050.0, "G1")
    assert not offences(rc) and not rc.penalties and not rcmod.pending_flag_dues()
    assert "red" not in (rc.auto_for(1, 0, 1.0) or {}) and "black" not in (rc.auto_for(1, 0, 1.0) or {}), "shadow shows nothing"
    rc.black["G1"] = {"car": 1, "laps": 1, "since": 0.0}
    rc.lap_done(1, 100000, 0, 6, 1150.0, "G1")
    assert not rcmod.pending_flag_dues(), "and does not disqualify anybody"


def test_a_flag_is_served_only_by_a_pit_visit_after_it_was_given_and_a_full_ledger_sanctions_nobody():
    rc = rc_with(); rules(rc, black={"bw_at": 1, "black_at": 2})
    rc.report_pit(1, True, now=rc.clock.time())   # already in the pit lane when the flag comes
    rc.conduct_add("G1", 1); rc.conduct_add("G1", 1)
    assert "G1" not in rc.black and rc.show_black(1) and "G1" in rc.black
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


def test_red_flag_order_is_the_last_line_the_whole_lead_lap_had_crossed():
    from app.live.racecontrol import red_reference
    times = {4: {1: 100.0, 2: 101.0, 3: 102.5}, 5: {1: 190.0}}   # the leader crossed lap 5; the others had not yet
    prog = {1: 5.02, 2: 4.97, 3: 4.90, 4: 2.60}   # car 4 is lapped
    ref = red_reference(prog, times)
    assert (ref["lap"], ref["order"]) == (4, [1, 2, 3, 4]), "the line before: order of lap 4, the lapped car after the lead lap, no lap given back"
    times[5].update({2: 191.0, 3: 192.0})
    full = red_reference({1: 5.10, 2: 5.05, 3: 5.01, 4: 2.60}, times)
    assert (full["lap"], full["order"]) == (5, [1, 2, 3, 4])   # everyone crossed 5: that line
    assert red_reference({1: 0.5, 2: 0.4}, {}) is None   # no full lap yet: a plain start
    swapped = {4: {1: 100.0, 2: 99.0}}   # the order is the one of the crossings, not where they were caught
    assert red_reference({1: 4.9, 2: 4.95}, swapped)["order"] == [2, 1]


def test_the_car_about_to_cross_the_line_when_the_red_comes_is_excused_that_crossing():
    class F:
        def snapshot(self):
            return {1: 3.97, 2: 3.40, 3: -0.02}   # (car 3 is on the grid, behind the line)
    rcmod._flag_dues_pending.clear()
    rc = rc_with(); rules(rc, red={"laps": 1})
    rc.session_ref = {"session_type": 3}
    rc.audit_guids = {1: "G1", 2: "G2"}
    rc.set_red(True, now=1000.0, field=F())
    assert rc.red_near == {1, 3}, "a car behind the line on the grid has its 0.98 too"
    assert rc.red_ref["lap"] == 3
    rc.lap_done(1, 100000, 0, 4, 1005.0, "G1")   # car 1 crossed the line under red, it could not have come in
    rc.lap_done(2, 100000, 0, 3, 1050.0, "G2")   # car 2 had a whole lap: nothing came in -> black
    assert "G1" not in rc.black and "G2" not in rc.black, "nothing gives the black flag by itself"
    rc.lap_done(1, 100000, 0, 5, 1100.0, "G1")   # its next crossing counts
    assert "G1" not in rc.black
    rcmod._flag_dues_pending.clear()


def test_in_practice_and_qualifying_a_flag_offence_is_a_live_drive_through_and_never_a_due():
    for session_type in (1, 2):
        rcmod._flag_dues_pending.clear()
        rc = rc_with(); rc.session_ref = {"session_type": session_type}
        rules(rc, yellow={"ladder": [5, 10]})   # seconds only: in a race that is a due on the result, here a drive-through
        assert rc._flag_sanction("yellow", 1, rc._rule("yellow"), 1000.0) is True
        assert rc.penalties and not rcmod._flag_dues_pending, "a due would become a time penalty on the race of the same event"
        rc._disqualify(2, "G2", True)
        assert not rcmod._flag_dues_pending, "a disqualification neither"
        assert [m for m in rc.take_flag_messages() if m.get("step") == "dsq"]
    race = rc_with()
    rules(race, yellow={"ladder": [5, 10]})
    race._flag_sanction("yellow", 1, race._rule("yellow"), 1000.0)
    assert len(rcmod._flag_dues_pending) == 1 and not race.penalties, "in a race it stays a due for the result"
    rcmod._flag_dues_pending.clear()


def test_the_blue_flag_is_a_race_matter_and_the_rest_judge_in_every_session():
    for session_type, blue in ((1, "off"), (2, "off"), (3, "live")):
        rc = rc_with(); rc.session_ref = {"session_type": session_type}
        rc.autoflags_tick({}, {}, L, 100.0, 1000.0)
        assert rc.autoflags.modes["blue"] == blue and rc.autoflags.modes["danger"] == "live"
        assert rc._sanctioned()
    other = RaceControl(session_ref={"session_type": 0})
    assert not other._sanctioned()


class _Field:
    def __init__(self, progress):
        self.p = progress

    def snapshot(self):
        return dict(self.p)

    def samples(self):
        return {c: Sample(p, 0, 0, 0.0, 0.0, int(p // 1)) for c, p in self.p.items()}


def test_the_restart_behind_the_pace_car_after_a_red_keeps_the_f1_order_and_the_green_ends_it():
    rc = rc_with()
    rc.pace_car = 9
    rc.lap_times = {4: {1: 100.0, 2: 101.0, 3: 102.0}}
    rc.set_red(True, now=1000.0, field=_Field({1: 4.95, 2: 4.90, 3: 4.80, 4: 2.5, 9: 4.99}))
    assert rc.red_ref["order"] == [1, 2, 3, 4] and 9 not in rc.red_ref["progress"]
    rc.set_red(False, now=1100.0)
    assert rc.red_ref is not None, "withdrawing the red keeps the reference: the restart needs it"
    scrambled = {3: 1.1, 1: 1.05, 4: 1.0, 2: 0.9, 9: 1.2}   # all in the pit lane, in any order
    rc.set_flag("yellow", now=1101.0, field=_Field(scrambled))
    assert rc.caution.frozen == [1, 2, 3, 4], "the order of the red, not where the cars are standing"
    assert 4 in rc.caution.lapped() and 1 not in rc.caution.lapped(), "the lapped car stays lapped: no lap given back"
    order, waved, _ = rc.caution.call_restart({c: p for c, p in scrambled.items() if c != 9})   # (the caller leaves the pace car out)
    assert order == [1, 2, 3, 4] and waved == [], "and the restart keeps it"
    assert rc.pass_tick({}, {}, 1.0, 1102.0) == [] and rc.passjudge._flag == rc._red_id, "the judge of the passes holds the grid"
    rc.set_flag("green", now=1200.0, field=_Field(scrambled))
    assert rc.red_ref is None


def test_a_caution_with_no_red_before_still_freezes_the_order_of_the_moment():
    rc = rc_with()
    rc.pace_car = 9
    rc.set_flag("yellow", now=1.0, field=_Field({1: 3.2, 2: 3.5, 9: 3.6}))
    assert rc.caution.frozen == [2, 1] and not rc.caution.keep_frozen


def test_a_plain_yellow_after_a_red_is_not_a_grid_hold_and_a_second_red_keeps_the_first_reference():
    rc = rc_with()
    rc.lap_times = {4: {1: 100.0, 2: 101.0}}
    rc.set_red(True, now=1000.0, field=_Field({1: 4.95, 2: 4.90}))
    first = rc.red_ref
    rc.set_red(True, now=1050.0, field=_Field({2: 1.0, 1: 0.9}))   # pressed twice, cars already in the pits
    assert rc.red_ref is first
    rc.set_red(False, now=1100.0)
    rc.set_flag("yellow", now=1101.0)   # no pace car, no caution: a plain yellow
    assert rc.caution is None and rc.pass_tick({}, {}, 1.0, 1102.0) == [] and rc.passjudge._flag != rc._red_id


def test_a_driver_who_reaches_the_black_flag_by_conduct_is_only_proposed_to_the_director():
    rc = rc_with(); rules(rc, black={"bw_at": 2, "black_at": 4})
    for _ in range(3):
        rc.conduct_add("G1", 1)
    assert rc.black_view() == [], "not there yet"
    rc.conduct_add("G1", 1)
    assert rc.black_view() == [{"car": 1, "conduct": 4}] and "G1" not in rc.black and rc.snapshot()["black_proposals"] == [{"car": 1, "conduct": 4}], "proposed, nothing done"
    rc.conduct_add("G1", 1)
    assert len(rc.black_view()) == 1, "still one proposal, not one per sanction"
    assert rc.show_black(1) and rc.black_view() == [], "the director shows it: no longer a proposal"
