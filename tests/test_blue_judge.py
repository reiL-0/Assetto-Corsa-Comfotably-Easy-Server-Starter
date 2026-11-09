"""The blue flag as a rule (RaceControl.autoflags_tick / _blue_judge / auto_for): pace gate, the time to let him through, one offence per appearance, the ladder, NASCAR."""
from app.live import racecontrol as rcmod
from app.live.field import Sample
from app.live.racecontrol import RaceControl

L = 4000.0   # track length, m


def rc_live(laps_a=90000, laps_b=89000):
    rc = RaceControl(session_ref={"session_type": 3})
    rc.server_id = 7
    rc.flag_modes = {"blue": "live", "danger": "shadow"}
    rc.audit_guids = {1: "GA", 2: "GB"}
    for _ in range(3):
        rc.pace.lap(1, laps_a, True)   # car 1 is the lapped one, in front
        rc.pace.lap(2, laps_b, True)   # car 2 is the lap leader, behind it
    return rc


def tick(rc, mono, behind_m=40.0):
    """Car 2 is a lap ahead in progress and `behind_m` metres behind car 1 on the track."""
    a = 0.5
    b = a + 1.0 - behind_m / L
    s = {1: Sample(a, 0, 0, mono, mono - 10, None), 2: Sample(b, 0, 0, mono, mono - 10, None)}
    rc.autoflags_tick(s, {1: 150.0, 2: 160.0}, L, mono, 1000.0 + mono)


def test_no_pace_no_blue_and_a_quicker_lapped_car_is_not_flagged():
    rc = RaceControl(session_ref={"session_type": 3}); rc.server_id = 7
    rc.flag_modes = {"blue": "live", "danger": "shadow"}
    tick(rc, 10.0)
    assert not rc.autoflags.blue, "no pace known: nobody is flagged"
    rc = rc_live(laps_a=88000, laps_b=90000)   # the lapped car (1) is quicker than the leader behind it (2)
    tick(rc, 10.0)
    assert not rc.autoflags.blue, "a comeback: the car behind has less pace, no blue and nothing to yield"
    rc = rc_live()
    tick(rc, 10.0)
    assert (1, 2) in rc.autoflags.blue


def test_the_offence_comes_after_yield_s_once_per_appearance_and_follows_the_ladder():
    rcmod._flag_dues_pending.clear(); rcmod._flag_served.clear()
    rc = rc_live()
    tick(rc, 10.0)
    assert rc.auto_for(1, 0, 10.0)["blue"]["left_s"] == 15
    tick(rc, 20.0)
    assert rc.auto_for(1, 0, 20.0)["blue"]["left_s"] == 5 and not rc.penalties and not rc.take_blue_offences()
    tick(rc, 26.0)   # 16 s since it appeared: he did not let him through
    [o] = rc.take_blue_offences()
    assert (o["car"], o["seq"], o["step"]) == (1, 1, 0) and [p["car_id"] for p in rc.penalties] == [1], "step 0 of the ladder: a drive-through"
    [row] = rcmod.pending_flag_dues()
    assert (row["driver_guid"], row["kind"], row["seq"], row["seconds"]) == ("GA", "blue", 1, 10), "unserved costs unserved_s"
    assert rc.auto_for(1, 0, 26.0)["blue"]["ignored"] is True and rc.auto_for(1, 0, 26.0)["blue"]["n"] == 1
    tick(rc, 40.0)
    assert not rc.take_blue_offences(), "one offence per appearance of the flag"
    tick(rc, 41.0, behind_m=900.0)   # he got past or went away: the blue is gone
    assert not rc.autoflags.blue and not rc.blue_since
    tick(rc, 50.0); tick(rc, 66.0)   # it appears again and is ignored again
    [o2] = rc.take_blue_offences()
    assert (o2["seq"], o2["step"]) == (2, 5) and len(rc.penalties) == 1, "step 1 of the ladder: 5 s on the result, no second drive-through"
    assert sorted(r["seconds"] for r in rcmod.pending_flag_dues()) == [5, 10]
    rc.report_pit(1, True, now=rc.clock.time() + 5)   # a pit-lane pass serves the drive-through only
    assert rcmod.pending_flag_served() == {(7, rc.cuts_session_key, "GA", "blue", 1)}
    rcmod._flag_dues_pending.clear(); rcmod._flag_served.clear()


def test_shadow_nascar_and_a_rule_without_ladder_never_sanction():
    for mode, nascar, ladder in (("shadow", False, [0, 5]), ("live", True, [0, 5]), ("live", False, [])):
        rc = rc_live()
        rc.flag_modes = {"blue": mode, "danger": "shadow"}
        rc.nascar = object() if nascar else None
        rc.blue_rule = {**rcmod.BLUE_DEFAULT, "ladder": ladder}
        tick(rc, 10.0); tick(rc, 40.0)
        assert not rc.take_blue_offences() and not rc.penalties, (mode, nascar, ladder)
    rc = rc_live(); rc.nascar = object()
    tick(rc, 10.0)
    assert not rc.autoflags.blue, "NASCAR: a lapped car need not give way: no blue at all"


def test_an_unknown_driver_is_never_sanctioned_and_a_neutralisation_restarts_the_countdown():
    rcmod._flag_dues_pending.clear()
    rc = rc_live(); rc.audit_guids = {}
    tick(rc, 10.0); tick(rc, 40.0)
    assert not rc.take_blue_offences() and not rc.penalties and not rcmod.pending_flag_dues(), "a car that cannot be named is not sanctioned"
    rc = rc_live()
    tick(rc, 10.0); tick(rc, 20.0)
    rc.flag = "yellow"; tick(rc, 21.0); tick(rc, 40.0)   # a caution: the flag is gone and so is the countdown
    assert not rc.autoflags.blue and not rc.blue_since and not rc.blue_ignored
    rc.flag = "green"; tick(rc, 50.0); tick(rc, 51.0)
    assert not rc.take_blue_offences(), "back to green the seconds start again: no instant offence with the time accumulated before"
    tick(rc, 66.0)
    assert [o["seq"] for o in rc.take_blue_offences()] == [1]


def test_a_second_drive_through_is_not_added_but_the_one_pit_pass_serves_both():
    rcmod._flag_dues_pending.clear(); rcmod._flag_served.clear()
    rc = rc_live()
    rc.add_penalty(1, 3)   # the car already owes a drive-through
    tick(rc, 10.0); tick(rc, 26.0)
    assert len(rc.penalties) == 1 and [o["step"] for o in rc.take_blue_offences()] == [0]
    rc.report_pit(1, True, now=rc.clock.time() + 5)
    assert rcmod.pending_flag_served() == {(7, rc.cuts_session_key, "GA", "blue", 1)}, "its due is closed by the same pass"
    rcmod._flag_dues_pending.clear(); rcmod._flag_served.clear()


def test_a_driver_who_took_the_slot_does_not_inherit_the_pace():
    rc = RaceControl(session_ref={"session_type": 3}); rc.server_id = 7
    for n in (2, 3, 4):
        rc.lap_done(1, 90000, 0, n, 1000.0 + n, "GA")
    assert rc.pace.of(1) == 90000
    rc.lap_done(1, 120000, 0, 5, 1010.0, "GB")   # another driver in the same slot: his pace starts from nothing
    assert rc.pace.of(1) is None
    rc.lap_done(1, 90000, 0, 6, 1020.0, "")   # no driver known: the lap is not trusted
    assert rc.pace.of(1) is None


def test_clean_laps_only_feed_the_pace():
    rc = RaceControl(session_ref={"session_type": 3}); rc.server_id = 7
    rc.lap_done(1, 90000, 0, 1, 1000.0, "GA")   # the first lap
    rc.lap_done(1, 90000, 1, 2, 1000.0, "GA")   # a server cut
    rc._pit_lap.add(1); rc.lap_done(1, 90000, 0, 3, 1000.0, "GA"); rc._pit_lap.discard(1)   # a pit lap
    rc.lap_done(rc.pace_car if rc.pace_car is not None else 99, 90000, 0, 4, 1000.0, "GP")
    assert rc.pace.of(1) is None
    for n in (4, 5, 6):
        rc.lap_done(1, 90000, 0, n, 1000.0 + n, "GA")
    assert rc.pace.of(1) == 90000
    rc._neutral_end = 2000.0   # a caution ended: a lap that began before it is not clean
    rc.lap_done(2, 90000, 0, 5, 2010.0, "GB")
    assert rc.pace.of(2) is None
