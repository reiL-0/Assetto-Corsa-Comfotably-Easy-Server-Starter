"""A pace car without the game: a fake driver (ops/fake_client.py) that drives the track's own AI line and does what the hub of a real pace car does
(static/csp/opr_hub.lua: modPaceCar / applyPaceCar), so any server can have its pace car by script, with no AC client and no GPU.

  * It asks the website what the race director wants:  GET {base}/api/csp/state?server=ID  ->  {"pacecar": {"car": slot, "kmh": N, "mode": "wait|lead|in"}}
    (it is the pace car only when `car` is its own slot; any other answer = it waits in the pits).
  * wait: parked in the pit lane. lead: leaves the pits, laps the fast line at `kmh` (speeds up 5 km/h per second, slows down 8, like the hub). in: keeps the pace
    to the pit entry, then 80 km/h along the pit lane and parks at its box (the middle of the pit lane).
  * It says it is in the pit lane the way the hub does:  GET {base}/api/csp/pit?server=ID&slot=S&name=N&pit=0|1  (once a second): that is what lets the manager give the green.
  * The track's two lines (fast_lane.ai + pit_lane.ai) come from the manager's copy kept when the track was uploaded (`--lines`, <data_dir>/pace_lines/<track>/<layout or ->/: the server's content does not keep `ai/`),
    else from `--content`'s tracks/<track>[/<config>]/ai. Read at every connection, so a track change = a restart = a new handshake. A track uploaded before this existed has to be uploaded again (or its two files copied there).
  * It joins only while the session says `pace_source: "bot"` (state of the manager; "off" and "real" keep it out, and it leaves the server when the setting changes). It reconnects by itself (backoff) when the server restarts or drops it. Needs the server to know its GUID (the pace car's SteamID of the manager) in the entry list.

    python3 ops/pace_bot.py --host 127.0.0.1 --port 9600 --guid 7656119... --name "Pace Car" --server 2 --base https://staging.example --lines /opt/acm-staging/data/pace_lines

ponytail: the box is the middle of the pit lane (not the car's own box); entering/leaving the pit spline is a jump of a few metres; no collision avoidance, other clients see a car they
cannot push. `YAW`: the heading is atan2(dx, dz) of the line (and the speed vector goes the same way); fake_client sends -yaw as the rotation because the game turns the car by -rotation.x (acs.exe createFromEuler). Not yet seen on a real client.
"""
from __future__ import annotations

import argparse
import bisect
import importlib.util
import json
import math
import os
import re
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

PC_UP, PC_DOWN, PIT_KMH = 5.0, 8.0, 80.0   # the hub's: km/h per second it may speed up / slow down, and the speed it comes into the pits at
HZ = 18
BOX_SLIDE = 25.0   # metres before and after the box over which the car moves from the lane line to the box

_spec = importlib.util.spec_from_file_location("fake_client", Path(__file__).resolve().parent / "fake_client.py")
fc = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("fake_client", fc)
_spec.loader.exec_module(fc)


class Spline:
    """An AC .ai line: header (version 7, count, 0, 0) then `count` points of (x, y, z, distance from the start, id) as little-endian floats/int. The rest of the file (speeds, widths...) is not read."""

    def __init__(self, pts, closed: bool):
        self.pts, self.closed = pts, closed
        self.cum = [0.0]
        for a, b in zip(pts, pts[1:] + ([pts[0]] if closed else [])):
            self.cum.append(self.cum[-1] + math.dist(a, b))
        self.total = self.cum[-1]

    @classmethod
    def load(cls, path, closed: bool):
        d = Path(path).read_bytes()
        ver, n = struct.unpack_from("<2i", d) if len(d) >= 16 else (0, 0)
        if ver != 7 or n < 2 or len(d) < 16 + 20 * n:
            raise ValueError(f"{path}: not a version 7 .ai line")
        return cls([struct.unpack_from("<3f", d, 16 + 20 * i) for i in range(n)], closed)

    def at(self, s: float):
        """(x, y, z, yaw) at the distance `s` (wraps on a closed line, stops at the end of an open one)."""
        s = s % self.total if self.closed else min(max(s, 0.0), self.total)
        i = min(bisect.bisect_right(self.cum, s) - 1, len(self.pts) - 1)
        a = self.pts[i]
        b = self.pts[(i + 1) % len(self.pts)] if (self.closed or i + 1 < len(self.pts)) else a
        seg = self.cum[i + 1] - self.cum[i] if i + 1 < len(self.cum) else 0.0
        f = (s - self.cum[i]) / seg if seg > 0 else 0.0
        dx, dz = b[0] - a[0], b[2] - a[2]
        yaw = math.atan2(dx, dz) if (dx or dz) else 0.0   # YAW: see the module note
        return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f, yaw

    def nearest(self, p) -> float:
        """Distance along the line of the point nearest to `p` (a vertex is enough: the points are ~1.5 m apart)."""
        i = min(range(len(self.pts)), key=lambda k: math.dist(self.pts[k], p))
        return self.cum[i]


_CSP_TRACK = re.compile(r"csp/\d+/\.\./(?:[A-Za-z0-9_]+/\.\./)?([^/]+)")   # the same as app/csp.py `clean_track` (this script runs alone, it cannot import the app)


def find_ai(content: str, track: str, config: str = "", lines: str = "") -> Path:
    """Where the two lines of the track are: the manager's own copy `<lines>/<track>/<config or ->/` (kept when the track is uploaded: the server's content does not keep `ai/`), else
    `<content>/tracks/<track>[/<config>]/ai` (a content folder that does have them)."""
    m = _CSP_TRACK.fullmatch(track or "")   # with CSP required the server's TRACK is `csp/<build>/../<track>` and the handshake gives it back like that: the folder is the last part
    track = m.group(1) if m else track
    if track in ("", ".", "..") or "/" in track or "\\" in track or "\0" in track:   # (the name is a folder name and nothing else: never a way out of the lines / content folders)
        raise FileNotFoundError(f"not a track folder name: {track!r}")
    found = []
    if lines:
        found.append(Path(lines) / track / (config or "-"))
    if content:
        base = Path(content) / "tracks" / track
        found += ([base / config / "ai"] if config else []) + [base / "ai"]
    for d in found:
        if (d / "fast_lane.ai").exists() and (d / "pit_lane.ai").exists():
            return d
    raise FileNotFoundError(f"no fast_lane.ai + pit_lane.ai for {track}/{config or '-'} (looked in {', '.join(map(str, found)) or 'nowhere: give --lines or --content'})")


class PaceBot:
    """The movement only: `step(dt, mode, kmh)` moves the car along its line; the I/O (server, website) is around it."""

    def __init__(self, fast: Spline, pit: Spline, box_offset: float = 0.0):
        self.box_offset = box_offset   # metres from the pit lane line to the box, + = to the LEFT of the direction of travel (measured on a real client: Imola rj_imola_2024 = 5.1)
        self.fast, self.pit = fast, pit   # (without a pit lane it could never say it is in the pits, which the green waits for: no pit_lane.ai = not started)
        self.entry = fast.nearest(pit.pts[0])   # where the pit lane leaves the fast line, and where it comes back
        self.exit = fast.nearest(pit.pts[-1])
        self.box = pit.total / 2
        self.reset()

    def reset(self):
        """A new session: back in the box, still (what the game does to every car)."""
        self.path, self.s, self.top, self.parked = "pit", self.box, 0.0, True
        self.lap_s = 0.0   # seconds since the last line crossing
        self.laps = 0

    @property
    def in_pit(self) -> bool:
        return self.path == "pit"

    def step(self, dt: float, mode: str, kmh: float):
        """Moves by `dt` seconds. Returns the lap time in ms when the line was crossed this step, else None."""
        lead = mode == "lead"
        if self.parked and not lead:
            self.top = 0.0
            return None
        self.parked = False
        if self.path == "pit":
            goal = min(kmh, PIT_KMH) if lead else PIT_KMH   # (the pit lane limit is respected both ways)
        else:
            goal = kmh if (lead or kmh > 0) else PIT_KMH   # (on the track and not leading: it keeps the pace it had, or goes in at the pit speed, as the hub does)
        self.top = min(goal, self.top + PC_UP * dt) if self.top < goal else max(goal, self.top - PC_DOWN * dt)
        move, prev = self.top / 3.6 * dt, self.s
        self.lap_s += dt
        self.s += move
        lap = None
        if self.path == "pit":
            if lead and self.s >= self.pit.total:   # out of the pit lane onto the fast line, at its exit
                self.path, self.s = "fast", self.exit + (self.s - self.pit.total)
                self.lap_s = 0.0   # (the out lap does not count)
            elif not lead and self.s >= self.box:
                self.top, self.parked = 0.0, True   # (where it is: a car that was asked to come in after its box stops there, it does not jump back)
        else:
            tot = self.fast.total
            turns_in = not lead and (prev < self.entry <= self.s or (self.s >= tot and prev < self.entry + tot <= self.s))   # (also when the same step crosses the line)
            if self.s >= tot:   # crossed the line
                self.s -= tot
                if self.laps:   # (the first crossing closes the out lap from the pits: not a lap)
                    lap = int(self.lap_s * 1000)
                self.laps += 1
                self.lap_s = 0.0
            if turns_in:   # the pit entry: it turns in
                self.path, self.s, self.top = "pit", 0.0, min(self.top, PIT_KMH)   # (it has braked before the entry: never above the pit lane speed inside)
        return lap

    def pose(self):
        line = self.pit if self.path == "pit" else self.fast
        x, y, z, yaw = line.at(self.s)
        if self.path == "pit" and self.box_offset:   # the lane line is the street: it slides sideways into the box over the last BOX_SLIDE m (ponytail: one offset per track, the box of this car is the middle of the pit line)
            k = self.box_offset * max(0.0, 1 - abs(self.s - self.box) / BOX_SLIDE)
            x, z = x - k * math.cos(yaw), z + k * math.sin(yaw)
        v = self.top / 3.6
        return x, y, z, yaw, (v * math.sin(yaw), 0.0, v * math.cos(yaw)), (self.s / line.total if line.closed else 0.0)


class Link:
    """The website side (what the hub does), polled once a second in a thread so the 18 Hz loop never waits for it."""

    def __init__(self, base: str, server: int, bot: PaceBot, driver):
        self.base, self.server, self.bot, self.d = base.rstrip("/"), server, bot, driver
        self.want, self.cmd, self.stop, self.fails, self._warned = True, ("wait", 0.0), threading.Event(), 0, {}   # want: the session still says the bot is this server's pace car; cmd = (mode, kmh): one tuple so the loop never reads half of a change

    @property
    def mode(self):
        return self.cmd[0]

    @property
    def kmh(self):
        return self.cmd[1]

    def _warn(self, kind: str, msg: str):   # one line per change per endpoint, not one per second
        if msg != self._warned.get(kind):
            self._warned[kind] = msg
            print(f"pace bot: {kind}: {msg}", flush=True)

    def _get(self, path: str):
        with urllib.request.urlopen(f"{self.base}{path}", timeout=3) as r:
            return r.read()

    def poll_once(self):
        try:
            state = json.loads(self._get(f"/api/csp/state?server={self.server}"))
            pc = state.get("pacecar")
            cmd = (pc.get("mode", "wait"), float(pc.get("kmh") or 0)) if pc and pc.get("car") == self.d.session_id else ("wait", 0.0)
            self.cmd, self.fails, self._warned["state"] = cmd, 0, None   # (only an answer that was read whole counts as the website being there)
            self.want = state.get("pace_source", "bot") == "bot"   # (a manager that does not know the setting: the bot was deployed on purpose)
        except Exception as e:   # the website or the manager is down: it keeps its last order, and after 10 s without them it comes into the pits (the hub's lost link)
            self.fails += 1
            self._warn("state", str(e))
            if self.fails >= 10 and self.cmd[0] == "lead":
                self.cmd = ("in", 0.0)
        try:
            q = urllib.parse.urlencode({"server": self.server, "slot": self.d.session_id, "name": self.d.name, "pit": int(self.bot.in_pit)})
            self._get(f"/api/csp/pit?{q}")
            self._warned["pit"] = None
        except Exception as e:
            self._warn("pit", str(e))

    def run(self):
        while not self.stop.is_set():
            self.poll_once()
            self.stop.wait(1.0)


def drive(d, bot: PaceBot, link: Link):
    """Until the server drops us."""
    t = time.monotonic()
    seen = d.new_sessions
    while link.want and not any(k == "closed" for _, k, _ in d.events):   # (not wanted any more = the session chose a real pace car or none: it leaves the server)
        now = time.monotonic()
        dt, t = now - t, now
        if d.new_sessions != seen:   # a new session: back to the box
            seen = d.new_sessions
            bot.reset()
        mode, kmh = link.cmd
        lap = bot.step(dt, mode, kmh)
        x, y, z, yaw, vel, norm = bot.pose()
        d.set_speed(bot.top)
        d.place(x, y, z, norm, yaw, vel)
        d.update()
        if lap:
            d.lap(lap)
        time.sleep(max(0.0, 1 / HZ - (time.monotonic() - now)))


def load_lines(content: str, track: str, config: str, lines: str, tries: int = 4, box_offset: float = 0.0) -> "PaceBot":
    """Both lines of the track from ONE folder: the manager swaps the whole folder when a track is uploaded again, so if it changed while the two files were read (another inode, or gone) they are read again."""
    for i in range(tries):
        ai = find_ai(content, track, config, lines)
        try:
            before = os.stat(ai).st_ino
            bot = PaceBot(Spline.load(ai / "fast_lane.ai", True), Spline.load(ai / "pit_lane.ai", False), box_offset)
            if os.stat(ai).st_ino == before:
                return bot
        except FileNotFoundError:
            pass   # (swapped under our hands)
        time.sleep(0.5)
    raise FileNotFoundError(f"the lines of {track}/{config or '-'} keep changing")


def wanted(base: str, server: int) -> bool:
    """Does the session want the bot on this server? Only a clear yes; the website or the manager being out is a no (it does not join a server it cannot be told about)."""
    try:
        with urllib.request.urlopen(f"{base.rstrip('/')}/api/csp/state?server={server}", timeout=3) as r:
            return json.loads(r.read()).get("pace_source", "bot") == "bot"
    except Exception:
        return False


def serve(args):
    delay = 2.0
    while True:
        if not wanted(args.base, args.server):   # off or a real pace car: stays out, and looks again in a while
            time.sleep(10)
            continue
        try:
            d = fc.FakeDriver(args.host, args.port, guid=args.guid, name=args.name, car=args.car, password=args.password, tyre=args.tyre).connect()
            link = None
            try:
                bot = load_lines(args.content, d.track, d.track_config, args.lines, box_offset=args.box_offset)
                link = Link(args.base, args.server, bot, d)
                threading.Thread(target=link.run, daemon=True).start()
                print(f"pace bot in slot {d.session_id} on {d.track}/{d.track_config}", flush=True)
                delay = 2.0
                drive(d, bot, link)
            finally:
                if link:
                    link.stop.set()
                d.disconnect()
        except (OSError, ConnectionError, fc.Refused, FileNotFoundError, ValueError) as e:
            print(f"pace bot: {e}; again in {delay:.0f} s", flush=True)
        time.sleep(delay)
        delay = min(delay * 2, 60.0)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True, help="the server's game TCP port")
    ap.add_argument("--guid", type=int, required=True, help="the pace car's SteamID (the manager's pace_steam_id; must be in the entry list)")
    ap.add_argument("--name", default="Pace Car")
    ap.add_argument("--car", default="bmw_m3_e30")
    ap.add_argument("--tyre", default="", help="SHORT_NAME of a tyre compound of the car (data/tyres.ini): without it the other drivers see the car with no wheels")
    ap.add_argument("--box-offset", type=float, default=0.0, help="metres from the pit lane line to the pit boxes, + = left of the direction of travel (0 = parks on the lane line)")
    ap.add_argument("--password", default="")
    ap.add_argument("--server", type=int, required=True, help="the server id on the website")
    ap.add_argument("--base", required=True, help="the website, e.g. https://www.oppenpaddockracing.site")
    ap.add_argument("--lines", default="", help="the manager's <data_dir>/pace_lines: the lines kept when a track is uploaded")
    ap.add_argument("--content", default="", help="the server's content folder (when its tracks keep an ai/ folder)")
    serve(ap.parse_args())
