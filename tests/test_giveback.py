"""Advantage gained off the track (app/live/giveback.py): pure state fed with numbers, 5 Hz like the manager's own positions."""
from app.live.giveback import BACK_HOLD, DEFAULT_WINDOW_S, SETTLE_S, GiveBack

DT = 0.2


def run(g, until, a, b, start=0.0, speeds=None, exempt=(), caution=False, extra=None, report=None):
    """Steps from `start` to `until` every DT s: `a(t)` / `b(t)` are the progress of car 2 (the one that goes off) and car 1 (in front of it); `extra` = {car: fn(t)}; `report` = (t, args) of an excursion."""
    events, t, sent = [], start, False
    while t <= until + 1e-9:
        prog = {1: b(t), 2: a(t), **{c: f(t) for c, f in (extra or {}).items()}}
        if report and not sent and t >= report[0] - 1e-9:   # (the report arrives once, at the first sample from its time on)
            g.report(2, t, *report[1])
            sent = True
        events += g.step(t, prog, speeds(t) if callable(speeds) else (speeds or {c: 120.0 for c in prog}), exempt, caution)
        t = round(t + DT, 6)
    return events


def b_line(t):
    return 5.20 + 0.004 * t


def a_goes_by(t):   # 0.02 behind car 1, off the track from t=5 to 7 in which it gains 0.03: ends 0.01 in front
    return b_line(t) - 0.02 + (0.03 * min(1.0, max(0.0, (t - 5.0) / 2.0)))


REPORT = (7.5, (2000, 5, a_goes_by(5.0) - 5.0))   # the game says: off the track 2 s, from lap 5 spline ... (what the script saw)


def test_going_off_the_track_and_coming_back_in_front_makes_an_obligation_that_expires_into_a_penalty():
    g = GiveBack()
    ev = run(g, 9.4, a_goes_by, b_line, report=REPORT)
    assert [e[0] for e in ev] == ["open"] and ev[0][1]["car"] == 2 and sorted(ev[0][1]["creditors"]) == [1], "read SETTLE_S after the excursion ended: car 2 owes car 1 the place"
    assert [o["creditors"] for o in g.open_obligations(9.4)] == [[1]] and 0 < g.open_obligations(9.4)[0]["left_s"] <= DEFAULT_WINDOW_S
    ev = run(g, 9.4 + DEFAULT_WINDOW_S - 1, a_goes_by, b_line, start=9.6)
    assert ev == [], "still in time"
    ev = run(g, 9.4 + DEFAULT_WINDOW_S + 2, a_goes_by, b_line, start=9.4 + DEFAULT_WINDOW_S - 0.8)
    assert [e[0] for e in ev] == ["due"] and ev[0][1]["car"] == 2, "time is up and it is still in front: the penalty, once"
    assert run(g, 80, a_goes_by, b_line, start=9.4 + DEFAULT_WINDOW_S + 2.2) == [] and g.open_obligations(80) == []


def test_giving_the_place_back_in_time_clears_it_and_a_short_dip_does_not():
    g = GiveBack()
    run(g, 9.4, a_goes_by, b_line, report=REPORT)

    def lifts(t):   # from t=12 car 2 drops 0.012 behind car 1
        return a_goes_by(t) - (0.022 if t >= 12 else 0.0)
    ev = run(g, 12 + BACK_HOLD - 0.4, lifts, b_line, start=9.6)
    assert ev == [], "behind it for less than BACK_HOLD"
    ev = run(g, 12 + BACK_HOLD + 0.6, lifts, b_line, start=12 + BACK_HOLD - 0.2)
    assert [(e[0], e[2]) for e in ev] == [("cleared", "returned")], "behind it for BACK_HOLD: given back"
    assert g.open_obligations(16) == []
    g = GiveBack()   # a dip that does not last restarts the count
    run(g, 9.4, a_goes_by, b_line, report=REPORT)

    def dips(t):
        return a_goes_by(t) - (0.022 if 12 <= t < 12.8 else 0.0)
    assert [e[0] for e in run(g, 20, dips, b_line, start=9.6)] == [] and len(g.open_obligations(20)) == 1


def test_it_is_no_case_when_nobody_was_owed_or_the_gain_is_small():
    g = GiveBack()
    ev = run(g, 12, lambda t: b_line(t) + 0.02, b_line, report=(7.5, (2000, 5, 0.2)))   # car 2 was in FRONT of car 1 before: nothing to give back
    assert ev == []
    g = GiveBack()

    def little(t):   # gains only 0.003: it did not really pass (and never gets ahead by more than the margin)
        return b_line(t) - 0.02 + (0.0225 * min(1.0, max(0.0, (t - 5.0) / 2.0)))
    assert run(g, 12, little, b_line, report=(7.5, (2000, 5, little(5.0) - 5.0))) == []
    g = GiveBack()
    far = lambda t: b_line(t) - 0.2 + (0.21 * min(1.0, max(0.0, (t - 5.0) / 2.0)))   # a car 0.2 of a lap away is not compared
    assert run(g, 12, far, b_line, report=(7.5, (2000, 5, far(5.0) - 5.0))) == []


def test_exemptions_stopped_pits_gone_caution_and_contact():
    def case(**kw):
        g = GiveBack()
        return g, run(g, 9.4, a_goes_by, b_line, report=REPORT, **kw)
    assert case(speeds={1: 10.0, 2: 120.0})[1] == [], "the car in front was (nearly) stopped: it was not defending a place"
    assert case(exempt={1})[1] == [] and case(exempt={2})[1] == [], "a car in the pit lane, either of them"
    assert case(caution=True)[1] == [] or all(e[0] != "open" for e in case(caution=True)[1]), "under a caution the no-passing judgement rules"
    g = GiveBack()
    g.note_contact(5.5, 1, 2)   # they touched while car 2 was going off: who forced whom is for the director
    ev = run(g, 9.4, a_goes_by, b_line, report=REPORT)
    assert ev == [] and g.reviews == [{"car": 2, "others": [1], "why": "contact"}]
    g = GiveBack()
    run(g, 9.4, a_goes_by, b_line, report=REPORT)
    ev = run(g, 12, a_goes_by, lambda t: b_line(t) if t < 10 else b_line(9.8), start=9.6, speeds=lambda t: {1: 120.0 if t < 10 else 5.0, 2: 120.0})
    assert [(e[0], e[2]) for e in ev] == [("cleared", "released")], "the creditor stopped meanwhile: nothing to give back to it"
    g = GiveBack()
    run(g, 9.4, a_goes_by, b_line, report=REPORT)
    ev = g.step(10.0, {2: a_goes_by(10.0)}, {2: 120.0})   # car 1 left the server
    assert [(e[0], e[2]) for e in ev] == [("cleared", "released")]
    g = GiveBack()
    run(g, 9.4, a_goes_by, b_line, report=REPORT)
    ev = g.step(9.6, {1: b_line(9.6), 2: a_goes_by(9.6)}, {1: 120.0, 2: 120.0}, caution=True)
    assert [(e[0], e[2]) for e in ev] == [("cleared", "suspended")] and g.open_obligations(9.6) == [], "a caution forgets what was owed"


def test_the_cars_it_passed_are_the_creditors_not_the_ones_that_passed_it_and_one_obligation_has_several():
    g = GiveBack()
    c_line = lambda t: b_line(t) + 0.01        # car 3 is 0.01 in front of car 1: car 2 passes both while it is off
    d_line = lambda t: b_line(t) - 0.04        # car 4 is behind car 2 the whole time: never owed anything
    ev = run(g, 9.4, a_goes_by, b_line, extra={3: c_line, 4: d_line}, report=REPORT, speeds=lambda t: {1: 120.0, 2: 120.0, 3: 120.0, 4: 120.0})
    assert len(ev) == 1 and ev[0][0] == "open" and sorted(ev[0][1]["creditors"]) == [1], "car 3 stays in front of car 2 (0.01 - 0.01 = 0): only car 1 was passed"
    g = GiveBack()
    e_line = lambda t: b_line(t) - 0.008      # between car 2 (before: 0.012 behind it) and car 1
    a2 = lambda t: b_line(t) - 0.03 + (0.045 * min(1.0, max(0.0, (t - 5.0) / 2.0)))
    ev = run(g, 9.4, a2, b_line, extra={3: e_line}, report=(7.5, (2000, 5, a2(5.0) - 5.0)), speeds=lambda t: {1: 120.0, 2: 120.0, 3: 120.0})
    assert len(ev) == 1 and sorted(ev[0][1]["creditors"]) == [1, 3], "it passed two cars while off the track: one obligation, two creditors"
    assert len(g.open_obligations(9.4)) == 1


def test_forgive_and_sanction_now_close_an_open_obligation_once():
    g = GiveBack()
    ev = run(g, 9.4, a_goes_by, b_line, report=REPORT)
    oid = ev[0][1]["id"]
    assert g.forgive(oid) is True and g.forgive(oid) is False and g.open_obligations(9.4) == []
    g = GiveBack()
    oid = run(g, 9.4, a_goes_by, b_line, report=REPORT)[0][1]["id"]
    assert g.due_now(oid)["car"] == 2 and g.due_now(oid) is None


def test_a_late_report_finds_the_start_of_the_excursion_in_the_history_of_the_car():
    g = GiveBack()
    g2 = GiveBack()   # the report comes 4 s after the end: the start is still found by the lap and the spline in the car's own history
    ev = run(g2, 13.0, a_goes_by, b_line, report=(11.0, (2000, 5, a_goes_by(5.0) - 5.0)))
    assert [e[0] for e in ev] == ["open"], "read as soon as it arrives when SETTLE_S had already passed"
    assert SETTLE_S == 2.0


def test_a_car_a_lap_down_is_no_place_to_gain_and_a_late_report_is_judged_on_the_order_right_after_the_excursion():
    g = GiveBack()
    lapped = lambda t: a_goes_by(t) + 1.0   # the leader (car 2) is a whole lap ahead of car 1: physically behind it and going by, but it gains no place
    assert run(g, 9.4, lapped, b_line, report=(7.5, (2000, 6, a_goes_by(5.0) - 5.0))) == []
    g = GiveBack()

    def passes_later(t):   # during the excursion it stays 0.02 behind; ten seconds later it goes by on the track, legally
        return 5.20 + 0.004 * (t - 1000.0) - 0.02 + (0.04 * min(1.0, max(0.0, (t - 1010.0) / 2.0)))
    ev = run(g, 1018.0, passes_later, lambda t: 5.20 + 0.004 * (t - 1000.0), start=1000.0, report=(1016.0, (2000, 5, passes_later(1005.0) - 5.0)))
    assert ev == [], "the report came late: at the end of the excursion it was still behind, the pass that came later is not its case"


def test_restoring_an_obligation_merges_with_one_opened_meanwhile_and_ids_are_never_reused():
    g = GiveBack()
    ob = run(g, 9.4, a_goes_by, b_line, report=REPORT)[0][1]
    taken = g.due_now(ob["id"])
    assert taken["car"] == 2 and g.open_obligations(9.4) == []
    other = run(g, 9.6, a_goes_by, b_line, start=9.6, report=(9.6, (2000, 5, a_goes_by(5.0) - 5.0)), extra=None)   # (a new excursion read later opens another for the same car)
    g._open[2] = {"id": 99, "car": 2, "creditors": {7: None}, "t0": 20.0, "due": 50.0, "met": False}
    g.restore({**taken, "due": 40.0})
    assert sorted(g._open[2]["creditors"]) == [1, 7] and g._open[2]["due"] == 40.0, "one obligation, all the creditors, the shorter time"
    assert GiveBack().step(0.0, {}, {}) == []
    a, b = GiveBack(), GiveBack()
    i1 = run(a, 9.4, a_goes_by, b_line, report=REPORT)[0][1]["id"]
    i2 = run(b, 9.4, a_goes_by, b_line, report=REPORT)[0][1]["id"]
    assert i1 != i2, "a detector made again never hands out an id that was used"
