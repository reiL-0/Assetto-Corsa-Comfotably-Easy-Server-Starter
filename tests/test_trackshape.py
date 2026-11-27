"""Track shape (app/live/trackshape.py): the line of a layout and its corners T1..Tn, learnt from the quickest clean lap the cars drove."""

import math

from app.live import trackshape
from app.live.trackshape import Tracer, clean, find_corners, resample


def _rounded_rect(w=400.0, h=200.0, r=30.0):
    """A closed line, start in the middle of the bottom straight, driven anticlockwise: 4 corners of radius r."""
    pts = []

    def line(x0, z0, x1, z1, n):
        pts.extend((x0 + (x1 - x0) * i / n, z0 + (z1 - z0) * i / n) for i in range(n))

    def arc(cx, cz, a0, a1, n=30):
        pts.extend((cx + r * math.cos(a0 + (a1 - a0) * i / n), cz + r * math.sin(a0 + (a1 - a0) * i / n)) for i in range(n))

    line(0, 0, w / 2 - r, 0, 40)
    arc(w / 2 - r, r, -math.pi / 2, 0)
    line(w / 2, r, w / 2, h - r, 30)
    arc(w / 2 - r, h - r, 0, math.pi / 2)
    line(w / 2 - r, h, -w / 2 + r, h, 60)
    arc(-w / 2 + r, h - r, math.pi / 2, math.pi)
    line(-w / 2, h - r, -w / 2, r, 30)
    arc(-w / 2 + r, r, math.pi, 1.5 * math.pi)
    line(-w / 2 + r, 0, 0, 0, 40)
    return pts


def _samples(pts, t0=0.0, v=50.0):
    """The trace a car leaves on that line: (t, spline, x, z) by distance."""
    d = [0.0]
    for a, b in zip(pts, pts[1:] + pts[:1]):
        d.append(d[-1] + math.dist(a, b))
    total = d[-1]
    return [(t0 + d[i] / v, d[i] / total, x, z) for i, (x, z) in enumerate(pts)], total


def test_four_rounded_corners_are_t1_to_t4_in_the_order_of_the_lap():
    s, _ = _samples(_rounded_rect())
    corners = find_corners(resample(s + [(s[-1][0] + 1, 1.0, s[0][2], s[0][3])]))
    assert [c["name"] for c in corners] == ["T1", "T2", "T3", "T4"]
    assert [c["dir"] for c in corners] == [1, 1, 1, 1], "all the same hand: an anticlockwise loop"
    assert corners[0]["x"] > 150 and corners[0]["z"] < 50, "T1 is the first corner after the line (bottom right)"
    assert [c["spline"] for c in corners] == sorted(c["spline"] for c in corners)


def test_straights_and_gentle_bends_are_no_corners():
    pts = [(1000 * math.cos(a / 400 * 2 * math.pi), 1000 * math.sin(a / 400 * 2 * math.pi)) for a in range(400)]   # a circle of 1 km radius
    s, _ = _samples(pts)
    assert find_corners(resample(s + [(s[-1][0] + 1, 1.0, s[0][2], s[0][3])])) == []


def test_the_tracer_hands_over_a_whole_clean_lap_and_drops_one_with_a_hole():
    s, _ = _samples(_rounded_rect())
    t = Tracer()
    for k in range(5):   # the end of the lap before
        t.update(1, 0.95 + k * 0.01, 0, 0, k * 0.2)
    base = 1.0
    for (ts, sp, x, z) in s:
        t.update(1, sp, x, z, base + ts)
    assert t.update(1, 0.004, s[0][2], s[0][3], base + s[-1][0] + 0.2) is None   # crosses the line: waits for acServer's lap_completed
    lap, info = t.lap_done(1, {"ms": 60_000, "cuts": 0}, base + s[-1][0] + 0.5)
    assert clean(lap) and lap[0][1] < 0.01 and lap[-1][1] > 1.0 and info["ms"] == 60_000
    assert t.lap_done(1, {"ms": 61_000, "cuts": 0}, base + s[-1][0] + 0.6) is None, "handed over once: the next lap_completed waits for its own line"

    # the other order: lap_completed first, then the packet past the line
    t3 = Tracer()
    t3.update(3, 0.99, 0, 0, 0)
    for (ts, sp, x, z) in s:
        t3.update(3, sp, x, z, 1 + ts)
    assert t3.lap_done(3, {"ms": 59_500, "cuts": 0}, 1 + s[-1][0] + 0.1) is None
    lap3, info3 = t3.update(3, 0.004, 0, 0, 1 + s[-1][0] + 0.2)
    assert clean(lap3) and info3["ms"] == 59_500

    t2 = Tracer()
    t2.update(2, 0.99, 0, 0, 0)
    for (ts, sp, x, z) in s:
        if 0.4 < sp < 0.5:
            continue   # a hole in the packets
        t2.update(2, sp, x, z, 1 + ts)
    t2.update(2, 0.004, 0, 0, 1 + s[-1][0] + 0.2)
    got = t2.lap_done(2, {"ms": 60_000, "cuts": 0}, 1 + s[-1][0] + 0.3)
    assert got is None or not clean(got[0]), "a lap with a hole draws nothing"


def test_the_quickest_clean_lap_is_kept_and_a_slower_one_does_not_replace_it():
    s, _ = _samples(_rounded_rect())
    lap = s + [(s[-1][0] + 1, 1.0, s[0][2], s[0][3])]
    assert trackshape.submit("shape_test", "", 60_000, lap) is True
    shape = trackshape.get("shape_test", "")
    assert shape["lapMs"] == 60_000 and len(shape["points"]) == trackshape.N and [c["name"] for c in shape["corners"]] == ["T1", "T2", "T3", "T4"]
    assert trackshape.submit("shape_test", "", 61_000, lap) is False
    assert trackshape.submit("shape_test", "", 59_000, lap[: len(lap) // 2]) is False, "half a lap is no shape"
    assert trackshape.submit("shape_test", "", 59_000, lap) is True and trackshape.get("shape_test", "")["lapMs"] == 59_000


def test_the_shape_of_the_set_up_track_is_served_404_until_a_lap_was_traced():
    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from app.db import engine
    from app.main import app
    from app.models import Server
    with Session(engine) as s:
        s.add(Server(id=960, name="shape", base_port=18960, config={"SERVER": {"TRACK": "csp/3898/../shape_srv", "CONFIG_TRACK": ""}}))
        s.commit()
    api = TestClient(app)
    url = "/api/v1/servers/960/acsm/api/live-timings/track-shape.json"
    assert api.get(url).status_code == 404
    s_, _ = _samples(_rounded_rect())
    trackshape.submit("shape_srv", "", 70_000, s_ + [(s_[-1][0] + 1, 1.0, s_[0][2], s_[0][3])])
    trackshape._cache.clear()
    body = api.get(url).json()
    assert body["track"] == "shape_srv" and len(body["corners"]) == 4 and len(body["points"]) == trackshape.N


def _ai(path, pts):
    import struct
    path.parent.mkdir(parents=True, exist_ok=True)
    body = struct.pack("<4i", 7, len(pts), 0, 0) + b"".join(struct.pack("<4fi", x, 0.0, z, 0.0, i) for i, (x, z) in enumerate(pts))
    path.write_bytes(body)


def test_start_line_and_pit_lane_come_from_the_kept_ai_lines_even_before_a_lap_is_traced():
    from pathlib import Path

    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from app.config import settings
    from app.db import engine
    from app.main import app
    from app.models import Server
    d = Path(settings.data_dir) / "pace_lines" / "mark_trk" / "-"
    _ai(d / "fast_lane.ai", [(100.0, 50.0), (100.5, 50.0), (103.0, 50.0), (110.0, 50.0)])   # the line starts at the start/finish, heading +x
    _ai(d / "pit_lane.ai", [(float(x), 40.0) for x in range(0, 101)])                        # 100 m of pit lane, one point a metre
    with Session(engine) as s:
        s.add(Server(id=961, name="marks", base_port=18961, config={"SERVER": {"TRACK": "mark_trk", "CONFIG_TRACK": ""}}))
        s.add(Server(id=962, name="none", base_port=18962, config={"SERVER": {"TRACK": "no_lines_trk", "CONFIG_TRACK": ""}}))
        s.commit()
    api = TestClient(app)
    body = api.get("/api/v1/servers/961/acsm/api/live-timings/track-shape.json").json()
    assert body["start"] == [100.0, 50.0, 3.0, 0.0], "the first point of the racing line, and the way the cars go"
    assert body["pit"][0] == [0.0, 40.0] and body["pit"][-1] == [100.0, 40.0] and 20 <= len(body["pit"]) <= 30, "every ~4 m, both ends kept"
    assert body["points"] == [] and body["corners"] == []
    assert api.get("/api/v1/servers/962/acsm/api/live-timings/track-shape.json").status_code == 404


def test_a_broken_racing_line_does_not_hide_a_good_pit_lane():
    from pathlib import Path

    from app.config import settings
    d = Path(settings.data_dir) / "pace_lines" / "half_trk" / "-"
    _ai(d / "pit_lane.ai", [(float(x), 0.0) for x in range(0, 21)])
    (d / "fast_lane.ai").write_bytes(b"\x07\x00")   # truncated
    marks = trackshape.landmarks("half_trk", "")
    assert "start" not in marks and marks["pit"][0] == [0.0, 0.0]


def test_a_layout_nobody_lapped_is_drawn_from_its_ai_racing_line_with_its_corners():
    from pathlib import Path

    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from app.config import settings
    from app.db import engine
    from app.main import app
    from app.models import Server
    d = Path(settings.data_dir) / "pace_lines" / "ai_only_trk" / "-"
    _ai(d / "fast_lane.ai", _rounded_rect())
    with Session(engine) as s:
        s.add(Server(id=963, name="ai only", base_port=18963, config={"SERVER": {"TRACK": "ai_only_trk", "CONFIG_TRACK": ""}}))
        s.commit()
    api = TestClient(app)
    url = "/api/v1/servers/963/acsm/api/live-timings/track-shape.json"
    for _ in range(2):   # (the second answer comes from the cache: still whole)
        body = api.get(url).json()
        assert body["source"] == "ai" and len(body["points"]) == trackshape.N and [c["name"] for c in body["corners"]] == ["T1", "T2", "T3", "T4"]
        assert "line" not in body and body["start"][:2] == [0.0, 0.0]
