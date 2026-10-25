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
import logging
import math
import statistics
import threading
import time
from collections import deque

from app.live.caution import Caution
from app.live.incidents import Watch
from app.live.nascar import CAUTION, Nascar, STAGES_DEFAULT

log = logging.getLogger("acmanager.nascar")

# Penalty and VSC ids only ever rise, across sessions and manager restarts (they start at the clock): the hub applies each penalty id once, and a client that stays connected through a
# session change must never see an id it has already used come back for something else.
_penalty_ids = itertools.count(int(time.time()))
_vsc_ids = itertools.count(int(time.time()))
_notice_ids = itertools.count(int(time.time()))   # like the penalty ids: the hub shows each notice id once, so an id must never come back for another text
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
NOTICE_TTL, NOTICE_MAX = 10, 64   # s a notice stays on screen (the hub counts it from when it first sees it); how many are kept at once
HOLD_TTL = 3600                   # a `hold` notice (the free pass, the restart call...) stays on screen the whole caution: it is dropped when the flag leaves the cautions or when its tag is replaced (the TTL is only a ceiling)
VIOLATION_LAPS = 3              # laps a car gets to serve the drive-through for passing under a caution
INVOLVED_FOR = 15.0              # s a contact makes a car 'involved' in a caution that follows
PACE_CAR_KMH = 120              # km/h the pace car leads at when nobody says otherwise
PIT_RECENT = 30.0                # s after its last 'I am in the pit lane' report a car still counts as having been in the pits (caution procedures)
CAUTION_MIN_LAPS, CAUTION_MAX_LAPS = 3, 6   # laps the pace car leads under a caution before the automatic 'to go' (earliest / latest: if the track is not clear by the last one the director takes over)
FORMATION_LAPS = 0                # crossings after the first one at which the pace car gets the 'one to go' in a rolling start: 0 = at the first (the pit entry is before the line, so the formation lap that follows ends in the pits and the green comes at the next crossing)
PIT_CLEAR = 6.0                  # s after a pit-lane report during which a standing car is not a stopped car on the track (two reports of the hub)
STANDING = 5.0                   # km/h under which a car is standing (a spun or parked car is not a violator)


class RaceControl:
    def __init__(self, session_ref: object = None) -> None:
        self.flag, self.pace_kmh = "green", None
        self.penalties: list[dict] = []
        self.notices: list[dict] = []   # on-screen messages for the hub ({id, kind, text, to, until}); `to` = a car id, -1 = everybody
        self.rev, self.ts = 0, time.time()
        self.session_ref = session_ref   # the ACSP session object this state belongs to (a new session replaces it)
        self.nascar: Nascar | None = None
        self.caution: Caution | None = None   # the procedures of the caution being run (only with NASCAR armed), see app/live/caution.py
        self.watch = Watch()   # contacts and stopped cars: the proposal of a caution, and who is involved in one (app/live/incidents.py)
        self.pace_guid = ""                # whose car it is: if the slot is somebody else's later, it is not the pace car any more (see app/racecontrol.py)
        self.pace_car: int | None = None   # the slot of the pace car (a real client with the hub script on autopilot, see static/csp/opr_hub.lua): never judged, never in the order
        self.pace_car_kmh = PACE_CAR_KMH
        self.pace_car_in = False           # the director called "to go": the pace car goes into the pits
        self.one_more = False              # "two to go" was called: the pace car does one more lap, and at its next crossing it becomes a "one to go" (pace_car_in)
        self.pace_car_pitted = False       # ...and its hub has said it is in the pit lane (the green waits for it)
        self.blocked_crossings = 0         # restart-leader crossings that found the pace car not in the pits (after two the director is told: the green is manual)
        self.auto_yellow = True    # the yellow comes out by itself on a pile-up or a stopped car (the director may turn it off: then it is only proposed)
        self.formation = False             # a rolling start is being run: the race begins under yellow behind the pace car (no Caution procedures, the NASCAR engine does not hear of it)
        self.lap_base = self.leader_laps_seen = 0   # leader laps run in the formation: the engine counts the race from the green
        self.caution_laps = -1             # laps the pace car (the leader when there is none) has done under this caution; -1 = the first crossing is still to come
        self.caution_notified = False      # the director was told that the track did not clear: the 'to go' is theirs
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

    def set_flag(self, flag: str, pace_kmh: int | None = None, grace_s: int = GRACE_DEFAULT, tolerance_kmh: int = TOL_DEFAULT, penalty: dict | None = None, now: float | None = None, source: str = "director", field=None) -> list[tuple]:
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
            if self.formation and source != "formation":   # (a director's flag, an incident: the formation is over and its yellow is no caution: what follows is a flag like any other)
                self._end_formation()
                self.flag = "green"
            was_caution = self.flag in CAUTION
            pits = {car for car, t in self._pit_seen.items() if now - t < PIT_RECENT}   # (before _clear_vsc forgets them)
            self._clear_vsc()
            self.flag, self.pace_kmh = flag, None
            if flag not in CAUTION:
                self.notices = [x for x in self.notices if not x.get("hold")]   # (the caution is over: what was held on the screen goes with it)
            if flag in SLOWING:
                self.pace_kmh, self.tolerance_kmh, self.vsc_penalty = pace_kmh, tolerance_kmh, pen
                self.vsc_id, self.grace_end, self.evaluated = next(_vsc_ids), now + grace_s, False
            actions = self.nascar.on_flag(flag) if self.nascar and source not in ("nascar", "formation") else []
            if self.nascar and field is not None:
                actions += self._caution_step(flag, was_caution, field.snapshot(), now, pits)
            self._bump()
            log.info("flag %s source=%s was_caution=%s actions=%s", flag, source, was_caution, actions)
            return actions

    def without_pace_car(self, progress: dict) -> dict:
        """The pace car is not a competitor: out of the order, the free pass, the passing judgement and the restart."""
        return {c: v for c, v in progress.items() if c != self.pace_car} if self.pace_car is not None else progress

    def pace_car_state(self) -> dict | None:
        """What the pace car's own hub reads: wait in the pits / lead the field / come in."""
        if self.pace_car is None:
            return None
        mode = "in" if self.pace_car_in else ("lead" if self.flag in CAUTION else "wait")   # after "to go" it stays «in» through the green (it still has to park); a new caution or a new pace car resets it
        return {"car": self.pace_car, "kmh": self.pace_car_kmh, "mode": mode}

    def _end_formation(self) -> None:
        self.formation = False
        self.lap_base = self.leader_laps_seen   # (the laps of the formation are not race laps)

    def start_formation(self) -> bool:
        """The race begins: a rolling start behind the pace car. Only with NASCAR armed, a pace car and nothing run yet; otherwise the standing start goes on as always."""
        with _lock:
            if not (self.nascar and self.pace_car is not None and self.flag == "green" and self.nascar.laps == 0 and not self.formation):
                return False
            self.formation = True
            self.caution_laps, self.pace_car_in, self.pace_car_pitted, self.one_more = -1, False, False, False
            self.set_flag("yellow", source="formation")   # (no field: no Caution, no free pass, no order, nobody is judged for passing)
            return True

    def formation_lap(self, car: int) -> bool:
        """The pace car crossed the line in the formation: the first crossing (from the grid) brings the 'one to go' -> True: the pace car goes in at the end of the formation lap that follows, before the line, and the green comes at the next crossing."""
        with _lock:
            if not self.formation or self.pace_car_in or car != self.pace_car:
                return False
            self.caution_laps += 1
            if self.caution_laps < FORMATION_LAPS:
                return False
            self.pace_car_in, self.pace_car_pitted = True, False
            self._bump()
            return True

    def formation_green(self, car: int, alone: bool) -> bool:
        """After the 'one to go' of the formation: the green when the pace car is in the pit lane and a competitor crosses the line (alone on the track: when the pace car arrives)."""
        with _lock:
            if not (self.formation and self.pace_car_in):
                return False
            if (car == self.pace_car) != alone:
                return False
            if not self.pace_car_pitted:
                if not alone:
                    self.blocked_crossings += 1   # (a competitor crossed and the pace car is not in the pits: after two the director is told)
                return False
            self._end_formation()
            self.set_flag("green", source="formation")
            return True

    def green_if_due(self, car: int, field=None) -> list[tuple] | None:
        """"To go" was called under our caution: the green comes out by itself (None = not due) once the pace car is in the pit lane (its hub said so) and the car that takes the restart crosses the line
        (the first of the restart order; with nobody to restart, the pace car reporting from the pits). Without a pace car, the restart leader crossing is enough. Checked and set under one lock:
        a director's checkered or a new pace car cannot slip in between."""
        with _lock:
            if not (self.pace_car_in and self.flag in CAUTION and self.caution):
                return None
            if car != (self.caution.restart[0] if self.caution.restart else self.pace_car):
                return None
            if self.pace_car is not None and not self.pace_car_pitted:
                if self.caution.restart:
                    self.blocked_crossings += 1
                log.info("green held: restart leader %s crossed but the pace car %s has not reported the pit lane (blocked=%s)", car, self.pace_car, self.blocked_crossings)
                return None
            return self.set_flag("green", field=field)

    def caution_lap(self, car: int, speeds: dict[int, float], now: float, connected: set[int] | None = None) -> str | None:
        """A car crossed the line under our caution. Only the pace car's laps count (the leader's when there is no pace car); the first crossing is a part of a lap. -> 'go' (at least CAUTION_MIN_LAPS
        done and the track is clear: the automatic 'to go'), 'warn' (CAUTION_MAX_LAPS done and still not clear: said once, from then on the 'to go' is the director's), or None."""
        with _lock:
            c = self.caution
            if not (c and self.flag in CAUTION and not self.pace_car_in and (self.one_more or not self.caution_notified)):   # (after the six-lap warning only a pending "two to go" still counts)
                return None
            if self.pace_car is not None:
                if car != self.pace_car or now - self._pit_seen.get(car, -1e9) < PIT_CLEAR:
                    return None   # (the pace car crossing the line in the pit lane is not a lap on the track)
            elif car != c.leader and (connected is None or c.leader in connected):
                return None   # (the leader left the server: any car crossing counts, or the count would never move)
            if self.one_more:   # "two to go" was called: this crossing is the "one to go"
                self.one_more = False
                self.pace_car_in, self.pace_car_pitted, self.blocked_crossings = True, False, 0
                self._bump()
                log.info("two to go became one to go at the crossing of car %s", car)
                return "one"
            self.caution_laps += 1
            if self.caution_laps < CAUTION_MIN_LAPS:
                log.info("caution lap %s/%s counted (car %s): too early for the automatic to go", self.caution_laps, CAUTION_MIN_LAPS, car)
                return None
            pits = {i for i, t in self._pit_seen.items() if now - t < PIT_CLEAR}
            stopped = [i for i, v in speeds.items() if i != self.pace_car and v < STANDING and i not in pits]
            involved = self.watch.recent_involved(now, INVOLVED_FOR)
            log.info("caution lap %s counted (car %s): stopped=%s recent_involved=%s", self.caution_laps, car, stopped, bool(involved))
            if not stopped and not involved:
                return "go"
            if self.caution_laps >= CAUTION_MAX_LAPS:
                self.caution_notified = True
                return "warn"
            return None

    def take_blocked_warning(self) -> bool:
        """True once, at the second restart-leader crossing that found the pace car not in the pits (its hub may not be reporting): the green has to be sent by hand."""
        with _lock:
            if self.blocked_crossings == 2:
                self.blocked_crossings = 3
                return True
            return False

    def set_pace_car(self, car: int | None, kmh: int = PACE_CAR_KMH, guid: str = "") -> None:
        if car is not None and not PACE_MIN <= kmh <= PACE_MAX:
            raise ValueError(f"the pace of the pace car is between {PACE_MIN} and {PACE_MAX} km/h")
        with _lock:
            self.pace_car, self.pace_car_kmh, self.pace_guid = car, kmh, guid if car is not None else ""
            self.pace_car_in = self.pace_car_pitted = self.one_more = False
            self.blocked_crossings = 0
            if car is None and self.formation:   # the pace car left in the formation: nobody leads the field any more, the green
                self._end_formation()
                self.set_flag("green", source="formation")
            if car is not None and self.caution:
                self.caution.remove(car)   # (named during a caution: it was in the frozen order)
            self._bump()

    def contact(self, now: float, car: int, other: int | None, speed: float) -> None:
        with _lock:
            if self.pace_car is not None and self.pace_car in (car, other):
                return   # the pace car is not a competitor: a touch with it makes nobody 'involved' and is no part of a pile-up
            self.watch.on_contact(now, car, other, speed)

    def _caution_step(self, flag: str, was_caution: bool, progress: dict[int, float], now: float, pits=()) -> list[tuple]:
        """The flag went to `flag`: a caution starts (freeze the order, pick the free pass) or ends. Returns actions for the adapter, which owns the names and the messages."""
        if flag in CAUTION and not was_caution:
            self.blocked_crossings, self.caution_laps, self.caution_notified = 0, -1, False
            self.pace_car_in = self.pace_car_pitted = self.one_more = False   # a new caution: the pace car comes out again
            progress = self.without_pace_car(progress)
            recent = self.watch.recent_involved(now, INVOLVED_FOR)
            self.watch.clear()
            self.caution = Caution(progress, now, involved=recent, penalised={p["car_id"] for p in self.penalties}, pitted=pits)
            return [("caution", self.caution.free_pass)]
        if flag not in CAUTION:
            self.caution = None
        return []

    def configure_nascar(self, total: int, stages: list[int] | None = None, shares: tuple | None = None, next_delay_min: int | None = None) -> list[tuple]:
        """Update distance and delay atomically; an existing `next_at` is never rescheduled by a delay edit. Returns the NASCAR actions of a distance change (the white flag moves with it) for the caller to carry out."""
        actions: list[tuple] = []
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
                    actions = self.nascar.set_total(total) or []
            else:
                motor = Nascar(total, shares=shares if shares is not None else STAGES_DEFAULT, ends=stages)
                motor.on_flag(self.flag)
                self.nascar = motor
            if next_delay_min is not None:
                self.next_delay_s = next_delay_min * 60
            self._bump()
            return actions

    def notify(self, text: str, kind: str = "info", to: int = -1, ttl: int = NOTICE_TTL, now: float | None = None, hold: str | None = None) -> dict:
        """A message for the hub's banner (it replaces the chat line). The text is kept free of quotes and backslashes: the hub reads the JSON with patterns.
        `hold` = a tag: the message stays (kind "hold", the hub draws it every frame from the state) until the caution ends or another notice takes the same tag."""
        now = time.time() if now is None else now
        with _lock:
            if hold:
                kind, ttl = "hold", HOLD_TTL
            n = {"id": next(_notice_ids), "kind": kind, "text": text.replace('"', "'").replace("\\", "/")[:160], "to": to, "until": now + ttl, "hold": hold}
            alive = [x for x in self.notices if x["until"] > now and not (hold and x.get("hold") == hold)]
            held = [x for x in alive if x.get("hold")]   # (the held ones are not pushed out by the ephemeral ones)
            self.notices = held + [x for x in alive if not x.get("hold")][-(NOTICE_MAX - 1 - len(held)):] + [n]
            self._bump()
            log.info("notice kind=%s to=%s: %s", kind, to, n["text"])
            return n

    def drop_hold(self, tag: str) -> None:
        with _lock:
            self.notices = [x for x in self.notices if x.get("hold") != tag]
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
                if car_id == self.pace_car and self.pace_car_in:
                    if not self.pace_car_pitted:
                        log.info("pace car %s reported the pit lane after to go", car_id)
                    self.pace_car_pitted = True
                if self.caution:
                    self.caution.pitted.add(car_id)   # (in the pits during a caution: out of the free pass, to the pit group of the restart)
                    if car_id != self.pace_car:
                        self.caution.pit_enter(car_id)

    def incident_step(self, speeds: dict[int, float], field, now: float) -> tuple[dict | None, list[tuple]]:
        """One look for an incident while racing under green. -> (the new proposal, actions): with `auto_yellow` the yellow is already out (actions are the NASCAR ones to carry out)."""
        with _lock:
            racing = bool(self.nascar and self.nascar.phase in ("racing", "ot_run") and self.nascar.laps >= 1 and self.flag in ("green", "white"))
            in_pit = {car for car, t in self._pit_seen.items() if now - t < PIT_RECENT}
            prop = self.watch.step(now, {c: v for c, v in speeds.items() if c != self.pace_car}, in_pit, racing)
            if not prop:
                return None, []
            actions: list[tuple] = []
            if self.auto_yellow:
                actions = self.set_flag("yellow", now=now, source="incident", field=field)
            else:
                self._bump()
            return prop, actions

    def restart_plan(self, progress: dict[int, float], to_go: int = 1) -> tuple[list[int], list[int], set[int]] | None:
        """The director calls 'to go': see Caution.call_restart. None when no caution of ours is being run."""
        with _lock:
            if not self.caution:
                return None
            plan = self.caution.call_restart(self.without_pace_car(progress))
            if to_go == 2 and not self.pace_car_in:   # one more lap behind the pace car: caution_lap makes it the "one to go"
                self.one_more = True
            elif not self.pace_car_in:   # (a second "to go" does not forget that the pace car already pitted)
                self.one_more = False
                self.pace_car_in, self.pace_car_pitted, self.blocked_crossings = True, False, 0   # "to go": the pace car goes into the pits and the leader controls the restart
            self._bump()
            log.info("restart plan: order=%s waved=%s choosers=%s pace_car=%s", plan[0], plan[1], sorted(plan[2]), self.pace_car)
            return plan

    def caution_tick(self, progress: dict[int, float], speeds: dict[int, float], now: float) -> list[tuple]:
        """One step of the caution being run: the free pass lost to the pits, the cars that passed. Returns actions for the adapter; a violator already has its drive-through listed."""
        with _lock:
            c = self.caution
            if not c:
                return []
            out: list[tuple] = []
            if c.free_pass is not None and c.free_pass in c.pitted:
                out.append(("no_free_pass", c.void_free_pass()))
            exempt = {car for car, t in self._pit_seen.items() if now - t < PIT_RECENT}
            for car in c.check(self.without_pace_car(progress), speeds, now, exempt):
                self.add_penalty(car, VIOLATION_LAPS)
                out.append(("violation", car))
            serving = {p["car_id"] for p in self.penalties}
            for car in c.pit_breakers(serving):
                self.add_penalty(car, VIOLATION_LAPS)
                out.append(("pit_violation", car))
            if out:
                self._bump()
            return out

    def pit_exception(self, car: int, allowed: bool) -> bool:
        """The director lets a car into the closed pit lane (or not any more). False when no caution of ours is being run."""
        with _lock:
            if not self.caution:
                return False
            self.caution.pit_allow(car, allowed)
            if allowed:   # (the notice in the same step: a green cannot come between the permission and its notice and leave the notice behind)
                self.notify("BOXES: tienes permiso para entrar", "race", car, hold=f"pitok-{car}")
            else:
                self.drop_hold(f"pitok-{car}")
            self._bump()
            return True

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
            at = time.time() if now is None else now
            notices = [{"id": n["id"], "kind": n["kind"], "left_s": math.ceil(n["until"] - at), "text": n["text"], "to": n["to"]} for n in self.notices if n["until"] > at]
            nascar = {**self.nascar.snapshot(), "next_delay_min": self.next_delay_s // 60} if self.nascar else None
            return {"rev": self.rev, "ts": int(self.ts), "running": running, "flag": self.flag, "pace_kmh": self.pace_kmh, "penalties": list(self.penalties), "notices": notices,
                    "vsc_id": self.vsc_id, "grace_left": self.grace_left(now), "tolerance_kmh": self.tolerance_kmh if self.flag in SLOWING else None,
                    "vsc_penalty": dict(self.vsc_penalty) if self.flag in SLOWING else None, "violations": list(self.violations), "exempt": list(self.exempt), "nascar": nascar, "caution": self.caution.snapshot() if self.caution else None,
                    "proposal": self.watch.proposal, "auto_yellow": self.auto_yellow, "pacecar": self.pace_car_state(),
                    "nascar_next_delay_min": self.next_delay_s // 60}


_states: dict[int, RaceControl] = {}


def state_of(server_id: int, running: bool, session_ref: object = None, create: bool = False) -> RaceControl | None:
    """The state of a server: forgotten when it is not running or the session changed (a new RaceControl starts green, with `rev` one ahead so clients notice the reset)."""
    with _lock:
        rc = _states.get(server_id)
        if rc and (not running or rc.session_ref is not session_ref):
            _old = _states.pop(server_id, None)
            rc = None
            if running and session_ref is not None:   # a session change: tell the hub clients (rev moves on) that the old flag is gone
                fresh = RaceControl(session_ref)
                fresh.rev = 1
                old = _old
                fresh.auto_yellow, fresh.pace_car, fresh.pace_car_kmh, fresh.pace_guid = old.auto_yellow, old.pace_car, old.pace_car_kmh, old.pace_guid   # (the director's choices outlive the session)
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
        rc.leader_laps_seen = max(rc.leader_laps_seen, leader_laps)
        if rc.formation:
            return rc, []   # (the formation lap is not a lap of the race)
        before = rc.nascar.snapshot()
        actions = rc.nascar.on_leader_lap(max(0, leader_laps - rc.lap_base))
        if before != rc.nascar.snapshot():
            rc._bump()
        return rc, actions
