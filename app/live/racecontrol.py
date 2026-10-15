"""Live race control of one server: the flag the director shows, a VIRTUAL SAFETY CAR (VSC) with its pace and the drive-throughs still to be served.

Plain state, no I/O: `RaceControl` is what the director (a steward in Control AC) sets and what the CSP hub script on every client reads (GET /servers/{id}/race-state, through the
website's /api/csp/state). The manager is the only authority: the client shows the flag, limits its pace and sets the game's own penalty; a modified client can ignore it, so nothing
here is a sanction by itself (the penalties of the result stay in app/penalties.py).

A yellow flag is only a flag. A VSC (power cut smoothly by the hub from the start, throttle only, no brake) and an FCY (full course yellow: controls untouched during the grace, braked gradually
after it) are the two things that limit speed, and are judged the same way: every driver first gets `grace_s` seconds (10 by default) to slow to `pace_kmh` by themselves; when the grace ends,
anybody still over `pace_kmh + tolerance_kmh` -- judged by the MEDIAN of the manager's own speed samples of the last 1.5 s of the grace (ACSP, ~5 Hz, so one lost packet does not decide it)
-- gets the penalty set for this VSC (none or a drive-through of N laps), once per VSC; the hub then brings every car down to the pace gradually and keeps it there. A car the hub says
is in the pit lane is exempt: only a "yes" counts and it lasts a few seconds, so nobody can cancel another driver's exemption (the hub is untrusted: a liar only escapes the penalty, the
speeds stay on record). `rev` rises on every change. Lives in memory: gone when the server stops; `state_of` resets it when the session changes.

NASCAR keeps a live-editable NEXT_SESSION delay (10 min initially, 0..60 min), also while disarmed. Configuration updates it atomically with the distance.
The finish monitor captures it in `next_at` when the field finishes; later delay edits do not move an existing deadline.
"""

from __future__ import annotations

import itertools
import math
import statistics
import threading
import time
from collections import deque

from app.live.nascar import Nascar, STAGES_DEFAULT

# Penalty and VSC ids only ever rise, across sessions and manager restarts (they start at the clock): the hub applies each penalty id once, and a client that stays connected through a
# session change must never see an id it has already used come back for something else.
_penalty_ids = itertools.count(int(time.time()))
_vsc_ids = itertools.count(int(time.time()))
_lock = threading.RLock()   # the API runs in worker threads and the monitor in the event loop: every change and read of a state goes through it

FLAGS = ("green", "yellow", "vsc", "fcy", "white", "checkered")
SLOWING = ("vsc", "fcy")   # the flags with a pace, a grace and a judgement: a VSC cuts the power smoothly from the start, an FCY (full course yellow) brakes the car once the grace is over
PACE_MIN, PACE_MAX = 30, 250     # km/h a VSC may ask for
GRACE_MIN, GRACE_MAX, GRACE_DEFAULT = 3, 60, 10
TOL_MAX, TOL_DEFAULT = 30, 5     # km/h over the pace that are still forgiven when the grace ends
LAPS_MAX = 5                     # laps a car gets to serve a drive-through (the game enforces it: pit-lane pass clears it, running out sends the car to the pits)
JUDGE_WINDOW = 1.5               # s before the grace ends whose speed samples decide (median)
JUDGE_MIN_SAMPLES = 3
PIT_EXEMPT_FOR = 4.0             # s a "I am in the pit lane" report counts for
NEXT_SESSION_DELAY = 600        # s; initial delay to keep the finished race and results visible
STANDING = 5.0                   # km/h under which a car is standing (a spun or parked car is not a violator)


class RaceControl:
    def __init__(self, session_ref: object = None) -> None:
        self.flag, self.pace_kmh = "green", None
        self.penalties: list[dict] = []
        self.rev, self.ts = 0, time.time()
        self.session_ref = session_ref   # the ACSP session object this state belongs to (a new session replaces it)
        self.nascar: Nascar | None = None
        self.finish_wait: set[int] | None = None
        self.finish_deadline = 0.0
        self.next_at: float | None = None
        self.next_delay_s = NEXT_SESSION_DELAY
        self._clear_vsc()

    def _clear_vsc(self) -> None:
        self.vsc_id, self.grace_end, self.tolerance_kmh, self.vsc_penalty = 0, 0.0, TOL_DEFAULT, {"kind": "none", "laps": 0}
        self.evaluated, self.violations, self.exempt = True, [], []
        self._samples: dict[int, deque] = {}   # car id -> (time, km/h) of the last few seconds
        self._pit_seen: dict[int, float] = {}  # car id -> when the hub last said it is in the pit lane

    def _bump(self) -> None:
        self.rev += 1
        self.ts = time.time()

    def set_flag(self, flag: str, pace_kmh: int | None = None, grace_s: int = GRACE_DEFAULT, tolerance_kmh: int = TOL_DEFAULT, penalty: dict | None = None, now: float | None = None, source: str = "director") -> list[tuple]:
        """Return NASCAR actions for the caller to execute after releasing the lock."""
        if flag not in FLAGS:
            raise ValueError(f"flag must be one of {', '.join(FLAGS)}")
        now = time.time() if now is None else now
        pen = {"kind": "none", "laps": 0}
        if flag in SLOWING:   # validate BEFORE touching the state: a refused VSC leaves everything as it was
            if pace_kmh is None or not PACE_MIN <= pace_kmh <= PACE_MAX:
                raise ValueError(f"a VSC needs a pace between {PACE_MIN} and {PACE_MAX} km/h")
            if not GRACE_MIN <= grace_s <= GRACE_MAX:
                raise ValueError(f"the grace is between {GRACE_MIN} and {GRACE_MAX} s")
            if not 0 <= tolerance_kmh <= TOL_MAX:
                raise ValueError(f"the tolerance is between 0 and {TOL_MAX} km/h")
            given = penalty or pen
            if given.get("kind") not in ("none", "pits") or (given["kind"] == "pits" and not 1 <= given.get("laps", 0) <= LAPS_MAX):
                raise ValueError(f"the penalty is none or a drive-through of 1 to {LAPS_MAX} laps")
            pen = {"kind": given["kind"], "laps": given.get("laps", 0) if given["kind"] == "pits" else 0}
        with _lock:
            self._clear_vsc()
            self.flag, self.pace_kmh = flag, None
            if flag in SLOWING:
                self.pace_kmh, self.tolerance_kmh, self.vsc_penalty = pace_kmh, tolerance_kmh, pen
                self.vsc_id, self.grace_end, self.evaluated = next(_vsc_ids), now + grace_s, False
            actions = self.nascar.on_flag(flag) if self.nascar and source != "nascar" else []
            self._bump()
            return actions

    def configure_nascar(self, total: int, stages: list[int] | None = None, shares: tuple | None = None, next_delay_min: int | None = None) -> None:
        """Update distance and delay atomically; an existing `next_at` is never rescheduled by a delay edit."""
        with _lock:
            if next_delay_min is not None and (type(next_delay_min) is not int or not 0 <= next_delay_min <= 60):
                raise ValueError("next_delay_min must be an integer between 0 and 60")
            if total == 0:
                self.nascar = None
                self.finish_wait = None
                self.finish_deadline = 0.0
                self.next_at = None
            elif self.nascar:
                if stages is not None or shares is not None:
                    raise ValueError("stages and shares are frozen while armed")
                if type(total) is not int or total != self.nascar.total:
                    self.nascar.set_total(total)
            else:
                motor = Nascar(total, shares=shares if shares is not None else STAGES_DEFAULT, ends=stages)
                motor.on_flag(self.flag)
                self.nascar = motor
            if next_delay_min is not None:
                self.next_delay_s = next_delay_min * 60
            self._bump()

    def add_penalty(self, car_id: int, laps: int) -> dict:
        if not 1 <= laps <= LAPS_MAX:
            raise ValueError(f"laps is between 1 and {LAPS_MAX}")
        with _lock:
            p = {"id": next(_penalty_ids), "car_id": car_id, "type": "pits", "laps": laps}
            self.penalties.append(p)
            self._bump()
            return p

    def clear_penalty(self, penalty_id: int) -> bool:
        with _lock:
            n = len(self.penalties)
            self.penalties = [p for p in self.penalties if p["id"] != penalty_id]
            if len(self.penalties) != n:
                self._bump()
            return len(self.penalties) != n

    # ---- the VSC monitor ------------------------------------------------------------------------------------------------------------------------------------------------
    def report_pit(self, car_id: int, in_pit: bool, now: float | None = None) -> None:
        """The hub of that car says whether it is in the pit lane. Only a yes is kept (a no never cancels one), and it expires: see the module docstring."""
        if in_pit:
            with _lock:
                self._pit_seen[car_id] = time.time() if now is None else now

    def sample(self, speeds: dict[int, float], now: float) -> None:
        """Speeds (km/h) of the cars now. Only the last seconds of the grace are kept."""
        with _lock:
            if self.flag not in SLOWING or self.evaluated:
                return
            for car_id, v in speeds.items():
                d = self._samples.setdefault(car_id, deque(maxlen=64))
                d.append((now, v))

    def grace_left(self, now: float | None = None) -> int:
        with _lock:
            if self.flag not in SLOWING:
                return 0
            return max(0, math.ceil(self.grace_end - (time.time() if now is None else now)))

    def due(self, now: float) -> bool:
        with _lock:
            return self.flag in SLOWING and not self.evaluated and now >= self.grace_end

    def evaluate(self, now: float) -> list[dict]:
        """When the grace is over: who was still over the pace (see the module docstring). Marks the VSC judged (once); returns [{car_id, speed_kmh}], the penalty is already listed."""
        with _lock:
            if not self.due(now):
                return []
            self.evaluated = True
            limit, out, spared = (self.pace_kmh or 0) + self.tolerance_kmh, [], []
            for car_id, d in self._samples.items():
                window = [v for t, v in d if self.grace_end - JUDGE_WINDOW <= t <= self.grace_end + 0.3]
                if len(window) < JUDGE_MIN_SAMPLES:
                    continue   # not enough to judge fairly (a car that just connected, a lagging one): the ramp still slows it
                v = statistics.median(window)
                if v < STANDING or v <= limit:
                    continue
                if self._pit_seen.get(car_id, -1e9) >= self.grace_end - (PIT_EXEMPT_FOR + JUDGE_WINDOW):   # judged at the END OF THE GRACE, not whenever the monitor happens to run
                    spared.append({"car_id": car_id, "speed_kmh": round(v)})   # the director sees who was over but said "pit lane": a liar only escapes the penalty
                    continue
                out.append({"car_id": car_id, "speed_kmh": round(v)})
            self.violations, self.exempt = out, spared
            for x in out:
                if self.vsc_penalty["kind"] == "pits":
                    self.add_penalty(x["car_id"], self.vsc_penalty["laps"])
            self._samples = {}
            self._bump()
            return out

    def snapshot(self, running: bool = True, now: float | None = None) -> dict:
        with _lock:
            nascar = {**self.nascar.snapshot(), "next_delay_min": self.next_delay_s // 60} if self.nascar else None
            return {"rev": self.rev, "ts": int(self.ts), "running": running, "flag": self.flag, "pace_kmh": self.pace_kmh, "penalties": list(self.penalties),
                    "vsc_id": self.vsc_id, "grace_left": self.grace_left(now), "tolerance_kmh": self.tolerance_kmh if self.flag in SLOWING else None,
                    "vsc_penalty": dict(self.vsc_penalty) if self.flag in SLOWING else None, "violations": list(self.violations), "exempt": list(self.exempt), "nascar": nascar,
                    "nascar_next_delay_min": self.next_delay_s // 60}


_states: dict[int, RaceControl] = {}


def state_of(server_id: int, running: bool, session_ref: object = None, create: bool = False) -> RaceControl | None:
    """The state of a server: forgotten when it is not running or the session changed (a new RaceControl starts green, with `rev` one ahead so clients notice the reset)."""
    with _lock:
        rc = _states.get(server_id)
        if rc and (not running or rc.session_ref is not session_ref):
            _states.pop(server_id, None)
            rc = None
            if running and session_ref is not None:   # a session change: tell the hub clients (rev moves on) that the old flag is gone
                fresh = RaceControl(session_ref)
                fresh.rev = 1
                _states[server_id] = fresh
                return fresh
        if rc is None and create and running:
            rc = _states[server_id] = RaceControl(session_ref)
        return rc


def speed_kmh(car: dict) -> float | None:
    v = car.get("velocity")
    return math.hypot(v[0], v[2]) * 3.6 if v and len(v) >= 3 else None


def tick(now: float, live) -> list[tuple[int, list[dict]]]:
    """One step of the monitor for every server with an active VSC: sample the speeds, judge when the grace ends. `live` = [(server_id, cars dict)] of the running servers.
    Returns [(server_id, violations)] for the judgements made now."""
    done = []
    for server_id, cars in live:
        with _lock:
            rc = _states.get(server_id)
        if not rc or rc.flag not in SLOWING or rc.evaluated:
            continue
        rc.sample({i: s for i, c in cars.items() if (s := speed_kmh(c)) is not None}, now)
        if rc.due(now):
            done.append((server_id, rc.evaluate(now)))
    return done


def on_lap(server_id: int, leader_laps: int, car_id: int | None = None) -> tuple[RaceControl | None, list[tuple]]:
    """Feed only the leaderboard maximum; a crossing also drains a waiting finish."""
    with _lock:
        rc = _states.get(server_id)
        if not rc or not rc.nascar:
            return rc, []
        if rc.finish_wait is not None and car_id in rc.finish_wait:
            rc.finish_wait.remove(car_id)
            rc._bump()
        before = rc.nascar.snapshot()
        actions = rc.nascar.on_leader_lap(leader_laps)
        if before != rc.nascar.snapshot():
            rc._bump()
        return rc, actions
