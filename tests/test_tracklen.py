"""The length of the track (app/live/tracklen.py): read from ui_track.json when it is there, estimated from the cars otherwise, and never guessed."""

import json
import math

from app.live.field import Field
from app.live.tracklen import LapLength, TrackLength, from_ui_json, lookup, parse_length, track_dir


def test_the_ways_a_track_says_its_length():
    assert parse_length("5793") == 5793 and parse_length(5793) == 5793 and parse_length("4309 m") == 4309
    assert parse_length("5.8 km") == 5800 and parse_length("3,6 km") == 3600 and parse_length("4.3") == 4300, "a bare number under 100 is kilometres"
    for bad in ("", "long", None, True, "99999 km", "50", "12 m", {"a": 1}, "5793 miles"):
        assert parse_length(bad) is None, bad


def test_the_length_comes_from_the_layout_first_then_from_the_track(tmp_path):
    ui = tmp_path / "ui"
    (ui / "gp").mkdir(parents=True)
    (ui / "ui_track.json").write_text(json.dumps({"length": "7004"}))
    assert from_ui_json(tmp_path, None) == 7004 and from_ui_json(tmp_path, "nolayout") == 7004, "no layout file: the track's"
    (ui / "gp" / "ui_track.json").write_text(json.dumps({"length": "5.1 km"}))
    assert from_ui_json(tmp_path, "gp") == 5100, "the layout's own"
    (ui / "gp" / "ui_track.json").write_text("{not json")
    assert from_ui_json(tmp_path, "gp") == 7004, "a broken layout file falls back to the track's, it never raises"
    assert from_ui_json(tmp_path / "missing", None) is None


def lap(est, car, L, speeds, hz=5.0, laps=1, epoch=0, gen=0, start=0.0, t0=0.0):
    """Drives `laps` laps of a track of L metres: the speed in m/s is a function of the position (so a lap is not driven at a constant speed); the samples come at `hz`."""
    d, t = start, t0
    while d < start + laps * L + 100.0:   # (a little past the line: the lap is closed when the car is seen on the other side of it)
        v = speeds(d % L)
        est.observe(car, d / L, v, t, epoch, gen)
        d += v / hz
        t += 1.0 / hz
    return t


def speed(x):   # between 25 and 75 m/s, around the lap
    return 50 + 25 * math.sin(x / 700.0)


def test_the_length_is_estimated_from_clean_laps_within_one_percent():
    est = LapLength()
    lap(est, 1, 5000.0, speed, laps=5)   # (the first lap is not whole: four count)
    assert est.length is None, "one car's four laps are not enough: one car going through the pits every lap could say anything"
    est = LapLength()
    lap(est, 1, 5000.0, speed, laps=6)
    assert est.length is not None and abs(est.length - 5000.0) < 50, est.length


def test_cars_agreeing_give_the_length_sooner_and_a_teleport_spoils_only_its_lap():
    est = LapLength()
    for car in (1, 2, 3, 4):   # four cars, one lap each (the first of each is not whole)
        lap(est, car, 4000.0, speed, laps=2)
    assert est.length is not None and abs(est.length - 4000.0) < 40
    est2 = LapLength()
    t = 0.0
    for epoch in range(4):   # a car re-anchored (teleported) in every lap: no lap is whole, so no length
        t = lap(est2, 1, 4000.0, speed, laps=1, epoch=epoch, start=epoch * 4000.0, t0=t)
    assert est2.length is None


def test_a_hole_in_the_samples_spoils_the_lap_and_garbage_is_ignored():
    est = LapLength()
    L, t, d = 3000.0, 0.0, 0.0
    for lapno in range(9):
        while d < (lapno + 1) * L:
            est.observe(1, d / L, 40.0, t, 0, 0)
            d += 8.0
            t += 0.2
            if lapno == 2 and 1500 < d % L < 1600:
                t += 3.0   # the car was lost for 3 s in the middle of lap 2
    est.observe(2, float("nan"), 40.0, 0.0, 0, 0)
    est.observe(2, 1.0, -5.0, 0.0, 0, 0)
    assert est.length is not None and abs(est.length - 3000) < 30, "the laps without the hole agree"
    assert 2 not in est._cars, "NaN progress and a negative speed are not even looked at"


def test_a_car_going_backwards_over_the_line_does_not_make_a_clean_lap():
    est = LapLength()
    L, t, d = 3000.0, 0.0, 2900.0
    for _ in range(40):                       # up to the line, over it, and back over it
        est.observe(1, d / L, 30.0, t, 0, 0)
        d += 10.0 if t < 4.0 else -10.0
        t += 0.2
    for _ in range(int(2 * L / 6.0)):         # then one forward lap: from the crossing the car turned round at, and back over the line: that lap is whole
        est.observe(1, d / L, 30.0, t, 0, 0)
        d += 6.0
        t += 0.2
    assert len(est._laps) <= 1, "the backwards crossing made no lap: " + str(est._laps)


def test_laps_that_disagree_give_no_length():
    est = LapLength()
    for L in (3000.0, 4500.0, 6000.0, 7500.0):
        lap(est, 1, L, lambda x: 50.0, laps=2, start=0.0, t0=0.0)
    assert est.length is None, "four laps of four different lengths: nothing is agreed"


def test_a_configured_length_wins_and_the_source_is_said(tmp_path):
    t = TrackLength()
    assert t.metres() is None and t.source() == "unknown"
    lap(t.estimator, 1, 5000.0, speed, laps=7)
    assert abs(t.metres() - 5000) < 50 and t.source() == "estimated"
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui" / "ui_track.json").write_text(json.dumps({"length": "5012"}))
    t.reset()
    t.configured = from_ui_json(tmp_path, None)
    assert t.metres() == 5012 and t.source() == "ui_track.json" and t.estimator.length is None, "a new session starts the estimate again; the file is the length"
    t.reset()
    assert t.metres() is None, "another track with no file: unknown, not the last one's"


def test_with_the_field_the_line_does_not_make_a_lap_dirty():
    """Fed from a real Field (spline wraps 0.99 -> 0.01): crossing the line is not a re-anchoring, so the laps are whole."""
    L, f, est, t, d = 5000.0, Field(), LapLength(), 0.0, 0.0
    while d < 8 * L:
        spline = (d / L) % 1.0
        f.update(1, spline, int(d // L), now=t)
        s = f.samples()[1]
        est.observe(1, s.progress, 60.0, t, s.epoch, s.generation)
        d += 12.0
        t += 0.2
    assert f.samples()[1].epoch == 1, "one epoch: the first sample"
    assert est.length is not None and abs(est.length - L) < 30, est.length


def test_the_acsp_client_measures_the_length_from_the_car_updates(monkeypatch):
    """The wiring: car_update events (spline + velocity) feed the estimate, with the clock the client reads (time.monotonic)."""
    from app.live import acsp

    clock = [1000.0]
    monkeypatch.setattr(acsp.time, "monotonic", lambda: clock[0])
    c = acsp.ACSPClient(990)
    L, d = 4200.0, 0.0
    assert c.length.metres() is None
    for _ in range(int(7 * L / 12.0)):   # one car, seven laps at 60 m/s, a sample every 0.2 s
        c._apply({"type": "car_update", "car_id": 3, "spline_pos": (d / L) % 1.0, "velocity": (0.0, 12.0, 58.79), "pos": (0.0, 0.0, 0.0)})   # 60 m/s along a 20 % slope: the horizontal part alone would be 2 % short
        clock[0] += 0.2
        d += 12.0
    assert c.length.metres() is not None and abs(c.length.metres() - L) < 80 and c.length.source() == "estimated", c.length.metres()
    assert c._length_logged == c.length.metres(), "said once in the log"
    c2 = acsp.ACSPClient(992)
    c2.length.configured, c2._oval_known = 5000.0, True   # a configured length and a known layout need no estimate
    for _ in range(50):
        c2._apply({"type": "car_update", "car_id": 3, "spline_pos": 0.1, "velocity": (0.0, 0.0, 60.0), "pos": (0.0, 0.0, 0.0)})
    assert c2.length.estimator._cars == {}, "the estimator is not even fed when the length and the layout are known"


def test_a_new_session_reads_the_track_file(tmp_path, monkeypatch):
    from app import content
    from app.live import acsp

    (tmp_path / "imola" / "ui").mkdir(parents=True)
    (tmp_path / "imola" / "ui" / "ui_track.json").write_text(json.dumps({"length": "4909"}))
    monkeypatch.setattr(content, "_tracks_dir", lambda: tmp_path)
    c = acsp.ACSPClient(991)
    c.length.estimator._laps.extend([(1000.0, 1), (1001.0, 2), (999.0, 3)])
    assert c.length.metres() == 1000.0
    c._apply({"type": "new_session", "session_type": 3, "track": "imola", "track_config": "", "elapsed_ms": 0, "name": "Race", "laps": 3, "time_min": 0})
    assert c.length.metres() == 4909 and c.length.source() == "ui_track.json" and c.length.estimator.length is None, "the file, and the old estimate is gone"
    c._apply({"type": "new_session", "session_type": 3, "track": "../etc", "track_config": "", "elapsed_ms": 0, "name": "Race", "laps": 3, "time_min": 0})
    assert c.length.metres() is None, "a name that is not a plain folder name is never looked up"


def test_laps_with_a_stop_do_not_count_and_a_car_that_left_is_forgotten():
    est = LapLength()
    L, t, d = 3000.0, 0.0, 0.0
    for lapno in range(6):   # six laps, every one with a 10 s stop in the middle (a pit stop: the lap through the pits is not the track)
        stopped = False
        while d < (lapno + 1) * L + 50:
            if not stopped and d > lapno * L + 1500:
                for _ in range(50):
                    est.observe(1, d / L, 0.0, t, 0, 0)
                    t += 0.2
                stopped = True
            est.observe(1, d / L, 40.0, t, 0, 0)
            d += 8.0
            t += 0.2
    assert est.length is None, "no lap without a stop: nothing to say"
    est.forget(1)
    assert 1 not in est._cars


def test_the_speed_in_three_dimensions_measures_a_hill_properly():
    """Fed with the full speed vector the estimate follows the road, not its shadow."""
    est = LapLength()
    L, t, d = 4000.0, 0.0, 0.0
    slope = 0.08   # 8 % grade all the way: the road is 0.3 % longer than its horizontal projection (a little: this checks only that the 3-D speed is what is fed)
    while d < 8 * L:
        est.observe(1, d / L, 60.0 * math.sqrt(1 + slope ** 2), t, 0, 0)
        d += 12.0
        t += 0.2
    assert est.length is not None and abs(est.length - L * math.sqrt(1 + slope ** 2)) < 20


def test_track_dir_accepts_only_plain_folder_names(tmp_path, monkeypatch):
    from app import content

    (tmp_path / "imola").mkdir()
    monkeypatch.setattr(content, "_tracks_dir", lambda: tmp_path)
    assert track_dir("imola") == tmp_path / "imola" and track_dir("nowhere") is None
    for bad in (None, "", "..", ".", "../x", "a/b", "a\\b", "x" * 81, "imola\x00"):
        assert track_dir(bad) is None, bad
    (tmp_path / "imola" / "ui").mkdir()
    (tmp_path / "imola" / "ui" / "ui_track.json").write_text(json.dumps({"length": "4909"}))
    assert lookup("imola", None) == 4909 and lookup("../x", None) is None


def test_the_track_file_is_read_off_the_event_loop_and_a_stale_answer_is_ignored(tmp_path, monkeypatch):
    """With a running loop the lookup runs in a thread; its answer counts only if the session that asked is still the current one."""
    import asyncio
    import threading
    import time

    from app.live import acsp

    gates = {"imola": threading.Event(), "monza": threading.Event()}
    seen = {}

    def slow_lookup(track, config):
        seen["thread"] = threading.current_thread()
        gates[track].wait(5)
        return {"imola": 4909.0, "monza": 5793.0}[track]

    monkeypatch.setattr(acsp, "lookup", slow_lookup)

    async def scenario():
        c = acsp.ACSPClient(993)
        c._apply({"type": "new_session", "session_type": 3, "track": "imola", "track_config": "", "elapsed_ms": 0, "name": "Race", "laps": 3, "time_min": 0})
        assert c.length.metres() is None, "the loop was not held up: the answer is not there yet"
        c._apply({"type": "new_session", "session_type": 3, "track": "monza", "track_config": "", "elapsed_ms": 0, "name": "Race", "laps": 3, "time_min": 0})   # another session before the first answer
        gates["monza"].set()   # the NEW session's answer first...
        for _ in range(100):
            await asyncio.sleep(0.02)
            if c.length.configured is not None:
                break
        assert c.length.configured == 5793.0
        gates["imola"].set()   # ...and the OLD session's answer arrives late
        await asyncio.sleep(0.3)
        assert c.length.configured == 5793.0, "the first session's 4909 arrived late and was ignored: " + str(c.length.configured)

    asyncio.run(scenario())
    assert seen["thread"] is not threading.main_thread(), "the disk was touched off the event loop"


def test_a_car_that_goes_back_a_little_every_lap_makes_no_length():
    """Reversing and recovering adds distance the speed (never negative) cannot show: the progress going back spoils the lap."""
    est = LapLength()
    L, t, d = 3000.0, 0.0, 0.0
    for lapno in range(10):
        back_done = False
        while d < (lapno + 1) * L + 20:
            est.observe(1, d / L, 30.0, t, 0, 0)
            d += 6.0
            t += 0.2
            if not back_done and d > lapno * L + 1500:
                back_done = True
                for _ in range(50):   # 300 m back, at speed, then on again
                    d -= 6.0
                    est.observe(1, d / L, 30.0, t, 0, 0)
                    t += 0.2
    assert est.length is None, est.length


def test_two_cars_may_agree_sooner_but_one_car_needs_five_laps():
    est = LapLength()
    for car in (1, 2):
        lap(est, car, 4000.0, speed, laps=3)   # two laps each that count
    assert est.length is not None and abs(est.length - 4000) < 40, "four laps from two cars"
    solo = LapLength()
    lap(solo, 1, 4000.0, speed, laps=5)
    assert solo.length is None, "four laps of one car"
    lap(solo, 1, 4000.0, speed, laps=2, start=5 * 4000.0, t0=1000.0)
    assert solo.length is not None


def test_the_catalog_gives_the_oval_check_to_the_base_layout_too(tmp_path, monkeypatch):
    """A track with only its base layout (no ui/), and the base of one that has layouts, have no entry in `configs`: the «oval» check needs `base_oval`."""
    from app import content
    for track, layout in (("oval_only", None), ("daytona", "road")):
        (tmp_path / track / "data").mkdir(parents=True); (tmp_path / track / "data" / "surfaces.ini").write_text("")
        if layout:
            (tmp_path / track / "ui" / layout).mkdir(parents=True); (tmp_path / track / "ui" / layout / "ui_track.json").write_text('{"tags":["road"]}')
            (tmp_path / track / layout / "data").mkdir(parents=True); (tmp_path / track / layout / "data" / "surfaces.ini").write_text("")
    monkeypatch.setattr(content, "_tracks_dir", lambda: tmp_path)
    tracks = {t["track"]: t for t in content.list_tracks()}
    assert tracks["oval_only"]["configs"] == [] and tracks["oval_only"]["base_oval"] == {"oval_auto": None, "oval_detected": None, "oval": False}
    assert tracks["daytona"]["base_oval"]["oval"] is False and tracks["daytona"]["configs"][0]["oval_auto"] is False, "the road layout is tagged road"
