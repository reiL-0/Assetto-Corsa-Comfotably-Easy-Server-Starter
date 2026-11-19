"""The pace bot (ops/pace_bot.py): the .ai reader and the movement it makes for wait / lead / in, on a made-up circuit (no server, no website)."""
import importlib.util
import math
import struct
import time
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location("pace_bot", Path(__file__).resolve().parent.parent / "ops" / "pace_bot.py")
pb = importlib.util.module_from_spec(_spec)
sys.modules["pace_bot"] = pb
_spec.loader.exec_module(pb)

R = 100.0


def write_ai(path: Path, pts):
    out, run = b"", 0.0
    for i, p in enumerate(pts):
        run += math.dist(pts[i - 1], p) if i else 0.0
        out += struct.pack("<4fi", p[0], p[1], p[2], run, i)
    path.write_bytes(struct.pack("<4i", 7, len(pts), 0, 0) + out)


def arc(r, a0, a1, n):
    return [(r * math.cos(a0 + (a1 - a0) * i / (n - 1)), 0.0, r * math.sin(a0 + (a1 - a0) * i / (n - 1))) for i in range(n)]


def make(tmp_path):
    write_ai(tmp_path / "fast_lane.ai", arc(R, 0, 2 * math.pi * 399 / 400, 400))      # a closed circle, 628 m
    write_ai(tmp_path / "pit_lane.ai", arc(R - 4, -0.3, 0.3, 40))                      # the pit lane: an arc inside it across the line
    return pb.PaceBot(pb.Spline.load(tmp_path / "fast_lane.ai", True), pb.Spline.load(tmp_path / "pit_lane.ai", False))


def run(bot, seconds, mode, kmh, laps=None, dt=1 / 18):
    for _ in range(int(seconds / dt)):
        lap = bot.step(dt, mode, kmh)
        if lap and laps is not None:
            laps.append(lap)
        assert not bot.in_pit or bot.top <= pb.PIT_KMH + 1e-6, "above the pit lane speed in the pit lane"


def test_spline_reads_the_game_format(tmp_path):
    bot = make(tmp_path)
    assert abs(bot.fast.total - 2 * math.pi * R) < 2 and bot.fast.closed and not bot.pit.closed
    assert bot.in_pit and bot.parked and 0 < bot.entry < bot.fast.total and bot.exit < bot.entry   # in the box; it comes in before the line and out after it


def test_waits_leads_and_comes_in(tmp_path):
    bot = make(tmp_path)
    run(bot, 10, "wait", 0)
    assert bot.parked and bot.in_pit and abs(bot.s - bot.box) < 1, "it waits in its box"
    run(bot, 5, "lead", 100)
    assert 24 <= bot.top <= 26, "speeds up 5 km/h per second, no jump"
    laps = []
    run(bot, 120, "lead", 100, laps)
    assert not bot.in_pit and abs(bot.top - 100) < 1e-6
    assert len(laps) >= 3 and all(abs(ms - 2 * math.pi * R / (100 / 3.6) * 1000) < 700 for ms in laps), laps   # the lap time of 100 km/h, the out lap not counted
    run(bot, 120, "in", 100)
    assert bot.in_pit and bot.parked and bot.top == 0 and abs(bot.s - bot.box) < 1, "back in the pit lane, parked at its box"


def test_the_website_contract_the_hub_uses(tmp_path):
    """Link reads {"pacecar": {car, kmh, mode}} and is the pace car only when `car` is its own slot; it reports the pit lane the way the hub does."""
    import http.server
    import threading
    seen, state = [], {"pacecar": {"car": 3, "kmh": 90, "mode": "lead"}}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            body = json_dumps(state) if self.path.startswith("/api/csp/state") else b""
            self.send_response(200 if body else 204)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    import json
    json_dumps = lambda o: json.dumps(o).encode()
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    bot = make(tmp_path)
    d = type("D", (), {"session_id": 3, "name": "Pace Car"})()
    link = pb.Link(f"http://127.0.0.1:{srv.server_port}", 7, bot, d)
    link.poll_once()
    assert (link.mode, link.kmh) == ("lead", 90.0)
    assert "/api/csp/state?server=7" in seen and "/api/csp/pit?server=7&slot=3&name=Pace+Car&pit=1" in seen   # parked in the pit lane = pit=1
    d.session_id = 4   # another car is the pace car: this one waits
    link.poll_once()
    assert (link.mode, link.kmh) == ("wait", 0.0)
    srv.shutdown()


def test_find_ai_prefers_the_managers_copy_then_the_layout(tmp_path):
    for sub in ("ai", "gp/ai"):
        (tmp_path / "tracks/t" / sub).mkdir(parents=True)
        for n in ("fast_lane.ai", "pit_lane.ai"):
            (tmp_path / "tracks/t" / sub / n).write_bytes(b"")
    assert pb.find_ai(str(tmp_path), "t", "gp") == tmp_path / "tracks/t/gp/ai"
    assert pb.find_ai(str(tmp_path), "t", "") == tmp_path / "tracks/t/ai"
    kept = tmp_path / "lines/t/gp"   # what the manager keeps when the track is uploaded: first
    kept.mkdir(parents=True)
    (kept / "fast_lane.ai").write_bytes(b"")
    assert pb.find_ai(str(tmp_path), "t", "gp", str(tmp_path / "lines")) == tmp_path / "tracks/t/gp/ai", "one line alone is not a pair"
    (kept / "pit_lane.ai").write_bytes(b"")
    assert pb.find_ai(str(tmp_path), "t", "gp", str(tmp_path / "lines")) == kept
    try:
        pb.find_ai("", "t", "gp", "")
    except FileNotFoundError as e:
        assert "--lines" in str(e)
    else:
        raise AssertionError("with nowhere to look it says so")


def test_the_bot_joins_the_real_acserver_and_drives_its_line(tmp_path):
    """In the local simulator: it takes its slot, reads the track's ai lines from the content, leaves the pit lane and keeps the server's connection."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import test_fake_client as tf
    rig = tf.Rig(tmp_path)
    try:
        ai = rig.dir / "content/tracks/magione/ai"
        ai.mkdir(parents=True)
        make(ai)
        d = tf.fc.FakeDriver("127.0.0.1", 19600, guid=tf.GUIDS[0], name="Pace Car").connect()
        found = pb.find_ai(str(rig.dir / "content"), d.track, d.track_config)
        bot = pb.PaceBot(pb.Spline.load(found / "fast_lane.ai", True), pb.Spline.load(found / "pit_lane.ai", False))
        link = pb.Link("http://127.0.0.1:9", 1, bot, d)   # (no website: the state is set by hand)
        link.cmd = ("lead", 120.0)
        start = bot.pose()[:3]
        t = pb.threading.Thread(target=pb.drive, args=(d, bot, link), daemon=True)
        t.start()
        time.sleep(4)
        assert not any(k == "closed" for _, k, _ in d.events), "the server kept the connection"
        assert bot.top > 15 and math.dist(start, bot.pose()[:3]) > 5, "it moved"
        d.disconnect()
        t.join(3)
    finally:
        rig.close()


def test_the_pit_entry_is_not_missed_when_the_same_step_crosses_the_line(tmp_path):
    bot = make(tmp_path)
    assert bot.entry > bot.fast.total - 60   # the pit lane is entered before the line
    bot.path, bot.parked, bot.s, bot.top = "fast", False, bot.entry - 0.5, 100.0
    bot.step(1.0, "in", 100)   # 27 m in one step: over the entry, the line and after it
    assert bot.in_pit, "it turned in"
    bot.path, bot.s, bot.top = "fast", bot.fast.total - 0.5, 100.0   # the entry is already behind it... and the line is next
    bot.entry, bot.exit = 5.0, 1.0   # (an entry just after the line)
    bot.step(1.0, "in", 100)
    assert bot.in_pit, "entry after the line, crossed in the same step"


def test_asked_to_come_in_after_its_box_it_stops_where_it_is(tmp_path):
    bot = make(tmp_path)
    run(bot, 3, "lead", 100)
    assert bot.in_pit and bot.s > bot.box - 1
    bot.s = bot.box + 10
    before = bot.s
    run(bot, 20, "in", 100)
    assert bot.parked and bot.in_pit and bot.s - before < 5, "no jump back to the box"


def test_the_website_failing_keeps_the_order_and_ten_failures_send_it_in(tmp_path):
    bot = make(tmp_path)
    d = type("D", (), {"session_id": 3, "name": "P"})()
    link = pb.Link("http://127.0.0.1:9", 1, bot, d)   # nothing listens there
    link.cmd = ("lead", 100.0)
    for _ in range(9):
        link.poll_once()
    assert link.cmd == ("lead", 100.0), "it keeps leading while the website is out"
    link.poll_once()
    assert link.cmd == ("in", 0.0), "and after 10 failures it comes in"
    bot.path, bot.parked, bot.top = "fast", False, 0.0
    bot.step(1.0, "wait", 0.0)
    assert bot.top > 0, "on the track and not leading it goes in at the pit speed, it does not stop"


def test_no_pit_lane_or_a_short_file_is_not_a_line(tmp_path):
    (tmp_path / "x.ai").write_bytes(b"\x07")
    for f in ("x.ai", "missing.ai"):
        try:
            pb.Spline.load(tmp_path / f, True)
        except (ValueError, OSError):
            continue
        raise AssertionError("it should refuse " + f)


def test_a_garbled_answer_counts_as_a_failure(tmp_path):
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"pacecar": {"car": 3, "kmh": "fast", "mode": "lead"}}' if self.path.startswith("/api/csp/state") else b""
            self.send_response(200 if body else 204)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    bot, d = make(tmp_path), type("D", (), {"session_id": 3, "name": "P"})()
    link = pb.Link(f"http://127.0.0.1:{srv.server_port}", 1, bot, d)
    link.cmd = ("lead", 100.0)
    for _ in range(10):
        link.poll_once()
    assert link.cmd == ("in", 0.0), "ten unreadable answers in a row = the website is out"
    srv.shutdown()


def test_the_bot_is_on_the_server_only_when_the_session_names_it(tmp_path):
    import http.server
    import json
    import threading
    state = {"pace_source": "bot", "pacecar": {"car": 3, "kmh": 90, "mode": "lead"}}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(state).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    assert pb.wanted(base, 1)
    for other in ("real", "off"):
        state["pace_source"] = other
        assert not pb.wanted(base, 1), other
    del state["pace_source"]
    assert pb.wanted(base, 1), "an older manager that does not say: the bot was deployed on purpose"
    srv.shutdown()
    assert not pb.wanted(base, 1), "the website out: it does not join"
    bot, d = make(tmp_path), type("D", (), {"session_id": 3, "name": "P"})()
    link = pb.Link(base, 1, bot, d)
    assert link.want


def test_the_bot_reads_both_lines_from_one_folder(tmp_path):
    kept = tmp_path / "lines" / "t" / "gp"
    kept.mkdir(parents=True)
    make(kept)
    bot = pb.load_lines("", "t", "gp", str(tmp_path / "lines"))
    assert bot.fast.closed and not bot.pit.closed
    try:
        pb.load_lines("", "t", "other", str(tmp_path / "lines"))
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("a track without lines does not start")
