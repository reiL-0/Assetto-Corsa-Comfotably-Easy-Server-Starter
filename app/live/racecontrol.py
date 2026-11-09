"""Live race control of one server: the flag the director shows, a VIRTUAL SAFETY CAR (VSC) with its pace and the drive-throughs still to be served.

Plain state, no I/O: `RaceControl` is what the director (a steward in Control AC) sets and what the CSP hub script on every client reads (GET /servers/{id}/race-state, through the
website's /api/csp/state). The manager is the only authority: the client shows the flag, limits its pace and sets the game's own penalty; a modified client can ignore it, so nothing
here is a sanction by itself (the penalties of the result stay in app/penalties.py).

A yellow flag is only a flag. A VSC (power cut smoothly by the hub from the start, throttle only, no brake) and an FCY (full course yellow: controls untouched during the grace, braked gradually
after it) are the two things that limit speed, and are judged the same way: every driver first gets `grace_s` seconds (20 by default) to slow to `pace_kmh` by themselves; when the grace ends,
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
import uuid
from collections import deque
from copy import deepcopy
from typing import NamedTuple

from app.live import (
    autoflags as flag_rules,
    caution as caution_rules,
    fieldrec,
    giveback as giveback_rules,
    incidents as incident_rules,
    nopass as pass_rules,
    pacejudge as pace_rules,
    raceaudit,
    sectors as delta_rules,
)
from app.live.autoflags import AutoFlags
from app.live.caution import Caution
from app.live.drs import Drs
from app.live.flagpace import Pace
from app.live.giveback import GiveBack
from app.live.incidents import MOVING_KMH, STOP_KMH, NeutralizationWatch, Watch
from app.live.nopass import PassJudge
from app.live.pacejudge import PaceDeltaJudge, PaceJudge
from app.live.sectors import DeltaJudge, RefLaps, SectorClock
from app.live.nascar import BREAK, CAUTION, Nascar, STAGES_DEFAULT

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
GRACE_MIN, GRACE_MAX, GRACE_DEFAULT = 3, 60, 20
TOL_MAX, TOL_DEFAULT = 30, 5     # km/h over the pace that are still forgiven when the grace ends
VSC_FACTOR = 1.35                 # the minimum time of a sector in a delta VSC: this times the best clean lap of the car model (F1 asks for about this much more than a racing lap)
WARN_DEFAULT, WARN_MIN, WARN_MAX = 2, 1, 5   # warnings (sectors under the limit in a row) before the first breach of a VSC by sectors; the director sets it
SEVERE_DEFAULT_S, SEVERE_MAX_S = 10, 60      # the severe penalty (seconds added to the race result, as a proposal for the director): after the warnings are spent and as many more come; 0 = none
DEFAULT_PENALTY = {"kind": "pits", "laps": 3, "warnings": WARN_DEFAULT, "severe_s": SEVERE_DEFAULT_S}   # a VSC / FCY that nobody configured: the drive-through (3 laps to serve it), never a disqualification
LAPS_MAX = 5                     # laps a car gets to serve a drive-through (the game enforces it: pit-lane pass clears it, running out sends the car to the pits)
JUDGE_WINDOW = 1.5               # s before the grace ends whose speed samples decide (median)
JUDGE_MIN_SAMPLES = 3
PIT_EXEMPT_FOR = 4.0             # s a "I am in the pit lane" report counts for
NEXT_SESSION_DELAY = 600        # s; initial delay to keep the finished race and results visible
NOTICE_TTL, NOTICE_MAX = 10, 64   # s a notice stays on screen (the hub counts it from when it first sees it); how many are kept at once
HOLD_TTL = 3600                   # a `hold` notice (the free pass, the restart call...) stays on screen the whole caution: it is dropped when the flag leaves the cautions or when its tag is replaced (the TTL is only a ceiling)
CUT_DT_LAPS = 3                 # laps to serve the drive-through of a cuts ladder step 0 (served = the hub reports the car in the pit lane afterwards)
VIOLATION_LAPS = 3              # laps a car gets to serve the drive-through for passing under a caution
INVOLVED_FOR = 15.0              # s a contact makes a car 'involved' in a caution that follows
PACE_CAR_KMH = 120              # km/h the pace car leads at when nobody says otherwise
AUTO_MAX_AGE_S = 3.0              # the automatic flags of a screen are not served from a look older than this
PIT_RECENT = 30.0                # s after its last 'I am in the pit lane' report a car still counts as having been in the pits (caution procedures)
PITS_OPEN_LAP = 1                 # caution laps (as counted by caution_lap: 0 is the first, partial crossing) after which the pit lane opens by itself: the first full lap is the field lining up
CAUTION_MIN_LAPS, CAUTION_MAX_LAPS = 3, 6   # laps the pace car leads under a caution before the automatic 'to go' (earliest / latest: if the track is not clear by the last one the director takes over)
SC_INCIDENT_COOLDOWN_S = 120.0     # minimum interval after an automatic SC ends before another can deploy
FORMATION_LAPS = 0                # crossings after the first one at which the pace car gets the 'one to go' in a rolling start: 0 = at the first (the pit entry is before the line, so the formation lap that follows ends in the pits and the green comes at the next crossing)
GO_ARM_FROM, GO_ARM_TO = 0.25, 0.75   # of a lap: after "one to go" the car that takes the restart has to be seen in this stretch before its next crossing can bring the green
RACE_TYPE = 3                     # ACSP session_type of a race
PIT_CLEAR = 6.0                  # s after a pit-lane report during which a standing car is not a stopped car on the track (two reports of the hub)
STANDING = 5.0                   # km/h under which a car is standing (a spun or parked car is not a violator)


class TrackClear(NamedTuple):
    stopped: list[int]
    involved: set[int]
    missing: list[int]
    slow: tuple[int, ...] = ()

    @property
    def clear(self):
        return not self.stopped and not self.involved and not self.missing and not self.slow


class RaceControl:
    def __init__(self, session_ref: object = None, clock=None) -> None:
        self.clock = clock or time
        self._audit_state = None
        self._audit_last_status = -1e9
        self._audit_gate = None
        self.audit_guids = {}
        self.audit_revision = None
        self.flag, self.pace_kmh = "green", None
        self.penalties: list[dict] = []
        self.notices: list[dict] = []   # on-screen messages for the hub ({id, kind, text, to, until}); `to` = a car id, -1 = everybody
        self.rev, self.ts = 0, self.clock.time()
        self.cuts_session_key = uuid.uuid4().hex
        self.session_ref = session_ref   # the ACSP session object this state belongs to (a new session replaces it)
        self.server_id = 0               # (set by state_of)
        self.giveback: GiveBack | None = None   # the advantage gained off the track (app/live/giveback.py): only in a race of a league whose rule is on
        self.sectorclock, self.reflaps, self.deltajudge = SectorClock(), RefLaps(), DeltaJudge()   # the F1-style VSC (app/live/sectors.py): sector crossings, the best clean lap per car model, the delta judge
        self._seen: dict[int, float] = {}       # car -> stamp of the last packet fed to the sector clock (the monitor looks 4 times a second, the packets come 5)
        self.passjudge = PassJudge()            # no overtaking under a VSC / FCY (app/live/nopass.py): give the place back or the flag's penalty
        self.pacejudge = PaceJudge()            # the continuous judge of a VSC / FCY's pace (app/live/pacejudge.py), SHADOW only for now: it writes down who it WOULD penalise; `evaluate` below still decides
        self.pacedelta = PaceDeltaJudge()       # the delta by speed of the cars with no reference lap (app/live/pacejudge.py): an FCY, or a VSC before its first clean lap
        self.pace = Pace()                      # the pace of every car for the blue flag (app/live/flagpace.py), fed by `lap_done`
        self.rules: dict | None = None          # the rules of the flags (app/rulebook.py, as dicts): set by the monitor from its cache (None: the defaults)
        self.blue_since: dict[tuple[int, int], float] = {}   # (car in front, quicker car behind) -> when this blue appeared (the field's clock): the time to let it through counts from here
        self.blue_ignored: set[tuple[int, int]] = set()      # the pairs whose present blue already cost an offence: one per appearance
        self.flag_count: dict[tuple, int] = {}               # (driver, flag kind) -> offences of this session (the n-th one picks the ladder step)
        self.conduct: dict[str, int] = {}                    # driver -> sanctions earned in this race (every flag, DRS, the cuts ladder): the black-and-white and the black flag come from it
        self.bw_shown: set[str] = set()                      # drivers who have the black-and-white flag
        self.black: dict[str, dict] = {}                     # driver -> {car, laps}: the black flag, and the laps he has left to come into the pits
        self.meatball: dict[str, dict] = {}                  # driver -> {car, laps}: the orange disc, and the laps he has left to come into the pits
        self._slow_since: dict[int, float] = {}              # car -> since when it runs far below the speed of the cars around it
        self.red_since: float | None = None                  # wall time the director showed the red flag (cleared by the next flag)
        self.damage: dict[int, tuple[int, float]] = {}       # car -> (damage % its own game reported, when): the orange disc (client evidence)
        self.red_laps: dict[int, int] = {}                   # car -> laps it has left to come into the pits under the red flag
        self.yellowjudge = PassJudge(15.0)                   # no overtaking under a plain yellow (the same judge as the VSC/FCY one, its own instance)
        self._yellow_id = 0
        self._was_yellow = False
        self.flag_dt_open: dict[tuple, tuple[int, float]] = {}   # blue drive-throughs not served yet: due key -> (car, issued at)
        self.flag_new: list[dict] = []                       # what the adapter has to tell the drivers (`take_flag_messages`): offences, the yellow's give-back, the discs and black flags
        self._neutral_end = 0.0                              # wall time the race was last not under green or white: a lap that began before it is not a clean lap
        self._pace_owner: dict[int, str] = {}                # slot -> the driver whose laps are in its pace
        self._pace_gen: dict[int, int] = {}                  # slot -> generation seen: a slot that changes hands forgets its pace
        self.autoflags = AutoFlags()            # blue flag and danger warning, SHADOW only (app/live/autoflags.py): decided and written down, shown to nobody; a new session starts a new one
        self.neutralization_rule = {"mode": "off"}
        self.neutralization_watch = NeutralizationWatch()
        self._spins = {}  # session-owned local decisions; no local flag is emitted
        self._neutralization_flag = False
        self._neutralization_sc = False
        self._sc_cooldown_until = 0.0
        self.neutralization_excluded: set[int] = set()
        self.flag_modes = {"blue": "shadow", "danger": "shadow"}   # off | shadow | live per automatic flag: the league's rulebook, set by the monitor from its cache (never `live` without a fresh reading)
        self.auto_off: set[str] = set()         # the director's kill switch per type (blue, danger): forces it off in real time; the director's choice outlives the session
        self.drs: Drs | None = None             # the DRS rules of the race (app/live/drs.py): set up by `drs_tick` while the league's rule is on
        self.gb_rule: dict | None = None        # {seconds, window_s}: set by the monitor from the league's rule (the cache: no database here)
        self.gb_names: dict[int, str] = {}      # car -> the name the banner shows for it (set by the monitor from the cars on the server)
        self.gb_owner: dict[int, str] = {}      # obligation id -> the Steam ID of the driver who owed the place when it opened (a slot can change hands)
        self.cuts_emitted: dict[str, int] = {}
        self._manual_dt_keys: set[tuple] = set()
        self.drs_dt_open: dict[tuple, tuple[int, float]] = {}   # DRS drive-throughs not served yet: due key (server, session, driver, window) -> (car, issued at)
        self.dt_open: dict[tuple, tuple[int, float]] = {}   # cut-ladder drive-throughs not served yet: due key -> (car, issued at)
        self.cuts_at: dict[str, float] = {}
        self.cuts_totals: dict[str, tuple[int, int]] = {}
        self.cuts_owners: dict[int, str] = {}
        self.cuts_server: dict[int, int] = {}   # car -> cuts the acServer counted in the laps it finished in this race (the authority: what the rulebook's rule counts)
        self.cuts_client: dict[int, int] = {}   # car -> excursions its own game reported (live: it does not wait for the lap to end; evidence, the driver's game could lie about it)
        self.nascar: Nascar | None = None
        self.caution: Caution | None = None   # the procedures of the caution being run (only with NASCAR armed), see app/live/caution.py
        self.watch = Watch()   # contacts and stopped cars: the proposal of a caution, and who is involved in one (app/live/incidents.py)
        self.pace_guid = ""                # the SteamID of the pace car's DRIVER: who it is, whatever slot he connects in (the director names a driver, not a car id; see app/racecontrol.py)
        self.pace_car: int | None = None   # the slot where that driver sits now (None while he is not on the server) (a real client with the hub script on autopilot, see static/csp/opr_hub.lua): never judged, never in the order
        self.pace_car_kmh = PACE_CAR_KMH
        self.pace_out = False              # the pace car came out under a yellow: it keeps leading if that yellow turns into a VSC/FCY (a VSC/FCY from green does not bring it out)
        self.pace_car_in = False           # the director called "to go": the pace car goes into the pits
        self.pace_park = False             # green with the pace car out, without "to go": return only, no restart procedure
        self.one_more = False              # "two to go" was called: the pace car does one more lap, and at its next crossing it becomes a "one to go" (pace_car_in)
        self.pace_car_pitted = False       # ...and its hub has said it is in the pit lane (the green waits for it)
        self.go_early = 0                  # crossings of the restart leader that came before go_armed (a second one brings the green anyway: if the progress never showed it mid-lap, the green must not wait for ever)
        self.go_armed = False              # after "one to go" the car that takes the restart has been seen in the middle of its lap: only then does its next crossing bring the green (so "one to go" is always a whole lap)
        self.blocked_crossings = 0         # restart-leader crossings that found the pace car not in the pits (after two the director is told: the green is manual)
        self.auto_yellow = True    # the yellow comes out by itself on a pile-up or a stopped car (the director may turn it off: then it is only proposed)
        self.formation = False             # a rolling start is being run: the race begins under yellow behind the pace car (no Caution procedures, the NASCAR engine does not hear of it)
        self.lap_base = self.leader_laps_seen = 0   # leader laps run in the formation: the engine counts the race from the green
        self.caution_laps = -1             # laps the pace car (the leader when there is none) has done under this caution; -1 = the first crossing is still to come
        self.caution_notified = False      # the director was told that the track did not clear: the 'to go' is theirs
        self.stage_log: dict[int, list[str]] = {}   # NASCAR: stage number -> the steam ids in the order of the field when it ended (app/racecontrol.py `_record_stage`)
        self.finish_wait: set[int] | None = None
        self.finish_deadline = 0.0
        self.next_at: float | None = None
        self.next_delay_s = NEXT_SESSION_DELAY
        self._clear_vsc()

    @staticmethod
    def _clean_penalty(given: dict) -> dict:
        """The penalty of a VSC / FCY as the judges use it: {kind, laps, warnings, severe_s}. Raises ValueError when something is out of range. Fields a client does not send keep their defaults."""
        if given.get("kind") not in ("none", "pits") or (given["kind"] == "pits" and not 1 <= given.get("laps", 0) <= LAPS_MAX):
            raise ValueError(f"the penalty is none or a drive-through of 1 to {LAPS_MAX} laps")
        warnings, severe = given.get("warnings", WARN_DEFAULT), given.get("severe_s", SEVERE_DEFAULT_S)
        if not WARN_MIN <= warnings <= WARN_MAX:
            raise ValueError(f"the warnings are {WARN_MIN} to {WARN_MAX} sectors in a row")
        if not 0 <= severe <= SEVERE_MAX_S:
            raise ValueError(f"the severe penalty is 0 to {SEVERE_MAX_S} s")
        return {"kind": given["kind"], "laps": given.get("laps", 0) if given["kind"] == "pits" else 0, "warnings": warnings, "severe_s": severe}

    def _clear_vsc(self) -> None:
        self.vsc_id, self.grace_end, self.tolerance_kmh, self.vsc_penalty = 0, 0.0, TOL_DEFAULT, {"kind": "none", "laps": 0, "warnings": WARN_DEFAULT, "severe_s": SEVERE_DEFAULT_S}
        self.evaluated, self.violations, self.exempt = True, [], []
        self.vsc_mode, self.vsc_profiles, self.vsc_fallback = "fixed", {}, False   # `delta` only for a VSC/FCY that started with at least one reference lap or fallback; vsc_profiles: car model -> minimum ms of the 20 sectors, frozen at the start
        self._since: float | None = None   # the instant (clock of the samples) the grace of a delta VSC ended: latched ONCE, never recomputed from the wall clock
        self._pen_cars: set[tuple[int, int]] = set()   # (car, slot generation) already penalised in this flag, by whichever judge: one penalty per car and flag
        self._sev_cars: set[tuple[int, int]] = set()   # (car, slot generation) that already got the severe level in this flag
        self._green_clear_since = self._green_last_look = None
        self._green_anchor = None
        self._green_laps = -1  # first leader crossing is partial
        self._green_warned = self._green_logged = False
        self._green_crossing = None
        self._green_stage = None
        self._green_started = self.clock.monotonic()
        self._green_speeds, self._green_connected = {}, set()
        self.legacy = False   # the pace is judged by the old single judgement at the end of the grace (set when a VSC / FCY starts with the kill switch `pace` on): never both judges in one flag
        self._samples: dict[int, deque] = {}   # car id -> (time, km/h) of the last few seconds
        self._limits_counted: dict[int, float] = {}  # monotonic; session-owned, at most 256 slots, under _lock
        self._pit_lap: set[int] = set()  # pit observed during the current lap; protected by _lock
        self._pit_seen: dict[int, float] = {}  # car id -> when the hub last said it is in the pit lane

    def audit(self, kind, **fields):
        session = self.session_ref if isinstance(self.session_ref, dict) else {}
        raceaudit.record(self.server_id, kind, session={k: session.get(k) for k in ("session_type", "name")},
                         rulebook_revision=self.neutralization_rule.get("rulebook_revision") or self.audit_revision or session.get("rulebook_revision"),
                         mode=self.neutralization_rule.get("mode", "off"), guids=self.audit_guids, **fields)

    def _bump(self) -> None:
        state = {"flag": self.flag, "pacecar": self.pace_car_state(), "pace_park": self.pace_park,
                 "caution": self.caution.snapshot() if self.caution else None,
                 "caution_laps": self.caution_laps, "auto_off": sorted(self.auto_off),
                 "penalties": list(self.penalties), "formation": self.formation, "one_more": self.one_more,
                 "auto_yellow": self.auto_yellow, "flag_modes": dict(self.flag_modes),
                 "green_laps": self._green_laps, "green_clear_since": self._green_clear_since}
        if state != self._audit_state:
            self.audit("state_change", previous=self._audit_state, state=state, executed=True)
            self._audit_state = deepcopy(state)
        self.rev += 1
        self.ts = self.clock.time()

    @fieldrec.capture(_lock)
    def set_flag(self, flag: str, pace_kmh: int | None = None, grace_s: int = GRACE_DEFAULT, tolerance_kmh: int = TOL_DEFAULT, penalty: dict | None = None, now: float | None = None, source: str = "director", field=None, mode: str = "fixed") -> list[tuple]:
        """Return NASCAR actions for the caller to execute after releasing the lock."""
        if flag not in FLAGS:
            raise ValueError(f"flag must be one of {', '.join(FLAGS)}")
        now = self.clock.time() if now is None else now
        pen = {"kind": "none", "laps": 0}
        if flag in SLOWING:   # validate BEFORE touching the state: a refused VSC leaves everything as it was
            if pace_kmh is None or not PACE_MIN <= pace_kmh <= PACE_MAX:
                raise ValueError(f"a VSC needs a pace between {PACE_MIN} and {PACE_MAX} km/h")
            if not GRACE_MIN <= grace_s <= GRACE_MAX:
                raise ValueError(f"the grace is between {GRACE_MIN} and {GRACE_MAX} s")
            if not 0 <= tolerance_kmh <= TOL_MAX:
                raise ValueError(f"the tolerance is between 0 and {TOL_MAX} km/h")
            if mode not in ("fixed", "delta"):
                raise ValueError("the mode of a VSC is fixed or delta")
            pen = self._clean_penalty(penalty or DEFAULT_PENALTY)
        with _lock:
            if self.red_since is not None:   # (any new flag withdraws the red one)
                self.red_since = None; self.red_laps = {}
            if self.formation and source != "formation":   # (a director's flag, an incident: the formation is over and its yellow is no caution: what follows is a flag like any other)
                self._end_formation()
                self.flag = "green"
            if self._neutralization_sc and flag not in CAUTION:
                self._sc_cooldown_until = self.clock.monotonic() + SC_INCIDENT_COOLDOWN_S
                self._neutralization_sc = False
            self._neutralization_flag = source == "incident" and flag in SLOWING
            was_caution = self.flag in CAUTION
            pits = {car for car, t in self._pit_seen.items() if now - t < PIT_RECENT}   # (before _clear_vsc forgets them)
            if self.flag == "fcy" and flag == "green":
                self._fcy_green_dues(now)   # (before _clear_vsc: the judges still hold what each car gained over the flag)
            self._clear_vsc()
            self.flag, self.pace_kmh = flag, None
            if flag == "yellow":
                self.pace_park = False
            elif flag == "green" and self.pace_out and not self.pace_car_in:
                self.pace_park = True
            self.pace_out = flag == "yellow" or (self.pace_out and flag in SLOWING)
            if flag not in CAUTION:
                self.caution = None
                self.notices = [x for x in self.notices if not x.get("hold") or x.get("hold") == "winner"]   # (the caution is over: what was held on the screen goes with it, but the winner stays until the session restarts)
            if flag in SLOWING:
                self.pace_kmh, self.tolerance_kmh, self.vsc_penalty = pace_kmh, tolerance_kmh, pen
                self.vsc_id, self.grace_end, self.evaluated = next(_vsc_ids), now + grace_s, False
                self.legacy = "pace" in self.auto_off
                delta_wanted = mode == "delta" and flag in SLOWING and not self.legacy
                self.vsc_profiles = self.reflaps.profiles(VSC_FACTOR) if (delta_wanted and flag == "vsc") else {}   # a VSC: the minimum time of every sector from the car's own best clean lap; an FCY has no reference lap: the pace itself is the reference (the default profile below)
                self.vsc_mode = "delta" if delta_wanted else "fixed"
                self.vsc_fallback = delta_wanted and flag == "vsc" and not bool(self.vsc_profiles)   # a VSC asked for reference laps and there are none yet: its cars are judged by the speed delta until they have one
            actions = self.nascar.on_flag(flag) if self.nascar and source not in ("nascar", "formation") else []
            if field is not None and (self.nascar or self.pace_car is not None or self.caution):   # (without NASCAR only behind a pace car: a plain yellow with nobody to lead is just a flag)
                samples = field.samples() if hasattr(field, "samples") and callable(field.samples) else None
                if isinstance(samples, dict):
                    snap = {c: getattr(s, "progress", s) for c, s in samples.items()}
                    laps = {c: s.completed_laps for c, s in samples.items() if getattr(s, "completed_laps", None) is not None}
                else:
                    snap = field.snapshot() if hasattr(field, "snapshot") else dict(field)
                    raw_laps = getattr(field, "completed_laps", None)
                    laps = dict(raw_laps) if isinstance(raw_laps, dict) else None
                actions += self._caution_step(flag, was_caution, snap, now, pits, laps)
            self._bump()
            self.audit("flag", flag=flag, source=source, pace_kmh=pace_kmh, grace_s=grace_s, tolerance_kmh=tolerance_kmh, penalty=pen, actions=actions,
                       thresholds=raceaudit.thresholds(pace_rules, delta_rules, pass_rules, caution_rules), executed=True)
            log.info("flag %s source=%s was_caution=%s actions=%s", flag, source, was_caution, actions)
            return actions

    def without_pace_car(self, progress: dict) -> dict:
        """The pace car is not a competitor: out of the order, the free pass, the passing judgement and the restart."""
        return {c: v for c, v in progress.items() if c != self.pace_car} if self.pace_car is not None else progress

    def pace_car_state(self) -> dict | None:
        """What the pace car's own hub reads: wait in the pits / lead the field / come in."""
        if self.pace_car is None:
            return None
        mode = "in" if self.pace_car_in or self.pace_park else ("lead" if self.pace_out else "wait")   # only yellow deploys it; green returns it even without "to go", without arming the restart
        return {"car": self.pace_car, "kmh": self.pace_car_kmh, "mode": mode}

    def _end_formation(self) -> None:
        self.formation = False
        self.lap_base = self.leader_laps_seen   # (the laps of the formation are not race laps)

    @fieldrec.capture(_lock)
    def start_formation(self) -> bool:
        """The race begins: a rolling start behind the pace car. Only with NASCAR armed, a pace car and nothing run yet; otherwise the standing start goes on as always."""
        with _lock:
            if not (self.nascar and self.pace_car is not None and self.flag == "green" and self.nascar.laps == 0 and not self.formation):
                return False
            self.formation = True
            self.caution_laps, self.pace_car_in, self.pace_car_pitted, self.one_more = -1, False, False, False
            self.set_flag("yellow", source="formation")   # (no field: no Caution, no free pass, no order, nobody is judged for passing)
            return True

    @fieldrec.capture(_lock)
    def formation_lap(self, car: int) -> bool:
        """The pace car crossed the line in the formation: the first crossing (from the grid) brings the 'one to go' -> True: the pace car goes in at the end of the formation lap that follows, before the line, and the green comes at the next crossing."""
        with _lock:
            if not self.formation or self.pace_car_in or car != self.pace_car:
                return False
            if self.clock.time() - self._pit_seen.get(car, -1e9) < PIT_CLEAR:
                return False
            self.caution_laps += 1
            self.audit("formation_lap", car=car, laps=self.caution_laps, executed=True)
            if self.caution_laps < FORMATION_LAPS:
                return False
            self.pace_car_in, self.pace_car_pitted = True, False
            self._bump()
            return True

    @fieldrec.capture(_lock)
    def formation_green(self, car: int, alone: bool) -> bool:
        """After the 'one to go' of the formation: the green when the pace car is in the pit lane and a competitor crosses the line (alone on the track: when the pace car arrives)."""
        with _lock:
            if not (self.formation and self.pace_car_in):
                return False
            if (car == self.pace_car) != alone:
                return False
            if not alone and self.clock.time() - self._pit_seen.get(car, -1e9) < PIT_CLEAR:
                return False
            if not self.pace_car_pitted:
                if not alone:
                    self.blocked_crossings += 1   # (a competitor crossed and the pace car is not in the pits: after two the director is told)
                return False
            self._end_formation()
            self.set_flag("green", source="formation")
            return True

    @fieldrec.capture(_lock)
    def green_if_due(self, car: int, field=None) -> list[tuple] | None:
        """"To go" was called under our caution: the green comes out by itself (None = not due) once the pace car is in the pit lane (its hub said so) and the car that takes the restart crosses the line
        (the first of the restart order; with nobody to restart, the pace car reporting from the pits). Without a pace car, the restart leader crossing is enough. Checked and set under one lock:
        a director's checkered or a new pace car cannot slip in between."""
        with _lock:
            if self._neutralization_sc and ("neutralization" in self.auto_off or self.neutralization_rule.get("mode") != "live"):
                return None
            if self.flag in SLOWING and not self.pace_out:
                return self._neutralization_green(car, field)
            if not (self.pace_car_in and self.flag in CAUTION and self.caution):
                return None
            if car != (self.caution.restart[0] if self.caution.restart else self.pace_car):
                return None
            if car != self.pace_car and self.clock.time() - self._pit_seen.get(car, -1e9) < PIT_CLEAR:
                return None   # A lap through the pit lane cannot start a restart.
            if not self.go_armed:
                self.go_early += 1
                if self.go_early == 1:   # (a second crossing is a whole lap later: the progress never showed the car mid-lap, so the green goes ahead)
                    self.audit("green_held", car=car, reason="partial_lap", executed=False)
                    log.info("green held: car %s crossed before a whole lap since the \"to go\"", car)
                    return None
                self.audit("green_crossing_fallback", car=car, crossings=self.go_early, executed=False)
                log.info("green: car %s crossed twice since the \"to go\" without being seen mid-lap", car)
            if self.pace_car is not None and not self.pace_car_pitted:
                if self.caution.restart:
                    self.blocked_crossings += 1
                self.audit("green_held", car=car, pace_car=self.pace_car, reason="pace_not_pitted", blocked=self.blocked_crossings, executed=False)
                log.info("green held: restart leader %s crossed but the pace car %s has not reported the pit lane (blocked=%s)", car, self.pace_car, self.blocked_crossings)
                return None
            return self.set_flag("green", field=field)

    def track_clear(self, speeds, pit_seen, now, *, pace_car=None, fresh=None, strict=False, connected=None):
        """NASCAR's unchanged stopped/contact test; strict callers also require telemetry.

        fresh is the set whose speed AND position are recent. connected must come
        from the connection roster, not the subset that already has telemetry.
        """
        pits = {c for c, t in pit_seen.items() if now - t < (PIT_EXEMPT_FOR if strict else PIT_CLEAR)}
        stopped = [c for c, v in speeds.items() if c != pace_car and v < STANDING and c not in pits]
        involved = self.watch.recent_involved(now, INVOLVED_FOR)
        missing = []
        if strict:
            missing = sorted(c for c in (connected if connected is not None else speeds)
                             if c != pace_car and c not in pits
                             and (fresh is None or c not in fresh or c not in speeds
                                  or not math.isfinite(speeds[c])))
        return TrackClear(stopped, involved, missing)

    def _green_track(self, samples, speeds, connected, mono, now):
        cars = set(connected) - self.neutralization_excluded - {self.pace_car}
        fresh = {c for c, sample in samples.items()
                 if 0 <= mono - sample.stamp <= self.neutralization_rule["stale_telemetry_s"]}
        status = self.track_clear({c: v for c, v in speeds.items() if c in cars}, self._pit_seen, now,
                                  pace_car=self.pace_car, fresh=fresh, strict=True, connected=cars)
        track = cars - {c for c, t in self._pit_seen.items() if now - t < PIT_EXEMPT_FOR}
        stopped, slow = list(status.stopped), []
        for c in track & fresh & speeds.keys():
            v = speeds[c]
            others = [speeds[k] for k in track & fresh & speeds.keys() if k != c
                      and math.isfinite(speeds[k]) and speeds[k] >= MOVING_KMH]
            pace = statistics.median(others) if others else 0
            if not math.isfinite(v):
                continue
            if v <= STOP_KMH:
                if c not in stopped:
                    stopped.append(c)
            elif pace and v < pace * self.neutralization_rule["slow_car"]["pace_ratio"]:
                slow.append(c)
        return status._replace(stopped=stopped, slow=tuple(sorted(slow)))

    def _green_notice(self, verdict, now):
        mode = self.neutralization_rule.get("mode")
        texts = {
            "two": "DOS PARA VERDE / TWO TO GREEN",
            "one": "UNA PARA VERDE / ONE TO GREEN",
            "warn": "Máximo de vueltas o tiempo: verde manual / Lap or time limit: director controls green",
        }
        text = f"{self.flag.upper()}: {texts[verdict]}"
        self.audit("auto_green_notice", verdict=verdict, text=text, laps=self._green_laps, executed=mode == "live")
        if mode == "live":
            if verdict == "warn":
                self.drop_hold("togo")
            self.notify(text, "flag", hold="togo" if verdict != "warn" else None)
        else:
            self.autoflags.log.append({"op": verdict, "decision": verdict, "mode": mode,
                                      "timestamp": now, "text": text})

    def _green_handoff(self, now, reason):
        if not self._green_warned:
            self._green_warned = True
            self.audit("auto_green_extension", laps=self._green_laps, reason=reason,
                       director_notified=self.neutralization_rule.get("mode") == "live",
                       executed=self.neutralization_rule.get("mode") == "live")
            self._green_notice("warn", now)

    @fieldrec.capture(_lock)
    def neutralization_clear_tick(self, samples, speeds, mono, now, connected=None):
        """Monitor-owned freshness/clear interval; no I/O, shared with the lap callback under _lock.

        Any stopped/slow evidence interrupts the clear interval immediately (without
        waiting for incident escalation). Missing telemetry never means a clear track.
        """
        with _lock:
            rule = self.neutralization_rule
            active = (rule.get("mode", "off") != "off" and "neutralization" not in self.auto_off
                      and self.flag in SLOWING and not self.pace_out
                      and not self.formation and (self.session_ref or {}).get("session_type") == RACE_TYPE)
            if not active:
                self._green_clear_since = self._green_last_look = None
                return
            self._green_speeds = dict(speeds)
            self._green_connected = set(connected) if connected is not None else set(samples) | set(speeds)
            if self._green_logged:
                return
            if mono - self._green_started >= rule.get("green_max_wait_s", 600):
                self._green_handoff(now, "time_limit")
            age = rule["stale_telemetry_s"]
            cars = {c: s for c, s in samples.items() if c != self.pace_car and c not in self.neutralization_excluded}
            pits = {c for c, t in self._pit_seen.items() if now - t < PIT_EXEMPT_FOR}
            leader = max(cars, key=lambda c: cars[c].progress) if cars else None
            if (leader is None or leader in pits or cars[leader].progress < 1
                    or not 0 <= mono - cars[leader].stamp <= age):
                self._green_anchor = None
            track = set(cars) - pits
            status = self._green_track(samples, speeds, self._green_connected, mono, now)
            stopped, slow = status.stopped, list(status.slow)
            clear = bool(track) and status.clear
            previous_clear = self._green_clear_since
            if self._green_last_look is None or mono - self._green_last_look > age:
                self._green_clear_since = None
            self._green_last_look = mono
            if self.flag in SLOWING and mono - self._audit_last_status >= 5:
                self.audit("auto_green_status", flag=self.flag, pacecar=self.pace_car_state(), leader=leader, laps=self._green_laps, clear=clear,
                           clear_since=self._green_clear_since, speeds=speeds, stopped=stopped, slow=slow, rule=rule, executed=False)
                self._audit_last_status = mono
            self._green_clear_since = (mono if self._green_clear_since is None else self._green_clear_since) if clear else None
            if previous_clear != self._green_clear_since:
                self.audit("auto_green_timer", previous=previous_clear, clear_since=self._green_clear_since,
                           clear=clear, speeds=speeds, rule=rule, executed=False)

    def _neutralization_green(self, car, field):
        """VSC/FCY branch of green_if_due. Only actual, fresh leader crossings count.

        The first crossing completes the partial lap at activation. Losing the valid
        leader or seeing it in the pits discards that partial lap, never adds a lap.
        """
        rule = self.neutralization_rule
        mode = rule.get("mode", "off")
        if (self._green_logged or mode == "off" or "neutralization" in self.auto_off or field is None
                or self.formation or (self.session_ref or {}).get("session_type") != RACE_TYPE):
            return None
        mono, now = self.clock.monotonic(), self.clock.time()
        samples = {c: s for c, s in field.samples().items()
                   if c != self.pace_car and c not in self.neutralization_excluded}
        if mono - self._green_started >= rule.get("green_max_wait_s", 600):
            self._green_handoff(now, "time_limit")
        if self._green_warned:
            return None
        if not samples:
            self._green_anchor = None
            return None
        leader = max(samples, key=lambda c: samples[c].progress)
        s = samples[leader]
        valid = (s.progress >= 1 and 0 <= mono - s.stamp <= rule["stale_telemetry_s"]
                 and now - self._pit_seen.get(leader, -1e9) >= PIT_EXEMPT_FOR)
        if not valid:
            self._green_anchor = None
            return None
        if car != leader:
            return None
        identity = (leader, s.generation, s.epoch)
        completed = s.completed_laps
        if completed is None:
            return None
        crossing = (identity, completed)
        if (self._green_crossing is not None and self._green_crossing[0] == identity
                and completed <= self._green_crossing[1]):
            return None
        self._green_crossing = crossing
        if self._green_anchor != identity:
            self._green_anchor = identity
            self._green_laps = max(self._green_laps, 0)
            return None
        self._green_laps += 1
        self.audit("auto_green_lap", car=car, laps=self._green_laps, clear_since=self._green_clear_since, rule=rule, executed=False)
        laps = self._green_laps
        status = self._green_track(samples, self._green_speeds, self._green_connected | set(samples), mono, now)
        clear = (status.clear and self._green_clear_since is not None and self._green_last_look is not None
                 and 0 <= mono - self._green_last_look <= rule["stale_telemetry_s"]
                 and mono - self._green_clear_since >= rule["green_clear_s"])
        # Recheck the same criterion at EVERY crossing, especially the green itself.
        if not clear:
            self.audit("green_held", car=car, reason="track_not_clear", stopped=status.stopped,
                       involved=sorted(status.involved), missing=status.missing, slow=status.slow,
                       clear_since=self._green_clear_since, executed=False)
            if laps >= rule["green_after_laps"] + rule["green_max_extra_laps"]:
                self._green_handoff(now, "lap_limit")
            return None
        if self._green_stage is None and laps >= rule["green_after_laps"] - 1:
            self._green_stage = "two"
            self._green_notice("two", now)
        elif self._green_stage == "two":
            self._green_stage = "one"
            self._green_notice("one", now)
        elif self._green_stage == "one":
            if not self._green_logged:
                self.audit("auto_green_decision", laps=laps, clear=clear, executed=mode == "live")
            if mode == "shadow":
                if not self._green_logged:
                    self.autoflags.log.append({"op": "green", "decision": "green", "mode": mode,
                        "lap": math.floor(s.progress), "laps": laps, "state": self.flag,
                        "timestamp": now, "text": f"habría vuelto a verde en vuelta {math.floor(s.progress)}"})
                    self._green_logged = True
                return None
            return self.set_flag("green", source="neutralization", field=field)
        if laps >= rule["green_after_laps"] + rule["green_max_extra_laps"]:
            self._green_handoff(now, "lap_limit")
        return None

    @fieldrec.capture(_lock)
    def caution_lap(self, car: int, speeds: dict[int, float], now: float, connected: set[int] | None = None) -> str | None:
        """A car crossed the line under our caution. Only the pace car's laps count (the leader's when there is no pace car); the first crossing is a part of a lap. -> 'pits' (the first full lap is done: the pit lane opens), 'go' (at least CAUTION_MIN_LAPS
        done and the track is clear: the automatic 'to go', announced as 'two' a lap before: 'two' = "two to go"), 'warn' (CAUTION_MAX_LAPS done and still not clear: said once, from then on the 'to go' is the director's), or None."""
        with _lock:
            c = self.caution
            if self._neutralization_sc and ("neutralization" in self.auto_off or self.neutralization_rule.get("mode") != "live"):
                return None
            if not (c and self.flag in CAUTION and not self.pace_car_in and (self.one_more or not self.caution_notified)):   # (after the six-lap warning only a pending "two to go" still counts)
                return None
            if now - self._pit_seen.get(car, -1e9) < PIT_CLEAR:
                return None   # A pit-lane crossing is not a caution lap on track.
            if self.pace_car is not None:
                if car != self.pace_car:
                    return None
            elif car != c.leader and (connected is None or c.leader in connected):
                return None   # (the leader left the server: any car crossing counts, or the count would never move)
            if self.one_more:   # "two to go" was called: this crossing is the "one to go"
                self.one_more = False
                self.pace_car_in, self.pace_car_pitted, self.blocked_crossings, self.go_armed, self.go_early = True, False, 0, False, 0
                self._bump()
                log.info("two to go became one to go at the crossing of car %s", car)
                return "one"
            self.caution_laps += 1
            self.audit("caution_lap", car=car, laps=self.caution_laps, speeds=speeds, executed=True)
            if self.caution_laps == PITS_OPEN_LAP and not c.pits_open:
                c.open_pits()
                self._bump()
                log.info("caution lap %s counted (car %s): the pit lane opens", self.caution_laps, car)
                return "pits"
            if self.caution_laps < CAUTION_MIN_LAPS - 1:
                log.info("caution lap %s/%s counted (car %s): too early for the automatic to go", self.caution_laps, CAUTION_MIN_LAPS, car)
                return None
            status = self.track_clear(speeds, self._pit_seen, now, pace_car=self.pace_car)
            stopped, involved = status.stopped, status.involved
            self.audit("caution_clear", laps=self.caution_laps, stopped=stopped, involved=sorted(involved), min_laps=CAUTION_MIN_LAPS, max_laps=CAUTION_MAX_LAPS, executed=False)
            log.info("caution lap %s counted (car %s): stopped=%s recent_involved=%s", self.caution_laps, car, stopped, bool(involved))
            if not stopped and not involved:
                return "two" if self.caution_laps == CAUTION_MIN_LAPS - 1 else "go"   # ("two to go" a lap before: at the next crossing it becomes the "one to go"; with the track not clear then, it waits for the "go" of a later lap)
            if self.caution_laps < CAUTION_MIN_LAPS:
                return None
            if self.caution_laps >= CAUTION_MAX_LAPS:
                self.caution_notified = True
                return "warn"
            return None

    def caution_eta(self) -> dict | None:
        """The race laps (as the leader's counter will read at the crossing) on which the caution's next steps come, for the panel and the pit-lane notice. An estimate: the laps are counted at the
        crossings of the car that leads under the caution (caution_lap), and it assumes the track clears. `pits` = the pit lane opens (None once open), `two`/`one` = the automatic "two to go"/"one to go"
        (None once called), `green` = the earliest green, `green_max` = the lap after which the "to go" is the director's."""
        with _lock:
            if not (self.caution and self.nascar):
                return None
            at = self.nascar.laps - self.caution_laps    # (lap of the crossing that will make the count k: at + k)
            nxt = self.nascar.laps + 1                   # (lap of the next crossing)
            if self.pace_car_in:
                return {"pits": None, "two": None, "one": None, "green": nxt, "green_max": nxt}
            if self.caution_notified:   # (the track did not clear in time: the "to go" is the director's, there is no lap to tell)
                return {"pits": None, "two": None, "one": None, "green": None, "green_max": None}
            if self.one_more:
                return {"pits": None, "two": None, "one": nxt, "green": nxt + 1, "green_max": nxt + 1}
            pits = at + PITS_OPEN_LAP if not self.caution.pits_open else None
            two, one = at + CAUTION_MIN_LAPS - 1, max(at + CAUTION_MIN_LAPS, nxt)   # (the track not clear yet: the "to go" waits for the next clear crossing)
            return {"pits": pits, "two": two if two >= nxt else None, "one": one, "green": one + 1, "green_max": at + CAUTION_MAX_LAPS + 1}

    @fieldrec.capture(_lock)
    def stage_break(self, pending_crossing: bool) -> int | None:
        """A stage break began under our caution: the pit lane opens (the stops are what the break is for) and the lap on which the green will come is returned (None without a caution of ours; 0 behind a
        pace car: its laps are not the leader's, the green depends on when it is in the pits). The automatic "to go" comes at the crossing that makes CAUTION_MIN_LAPS laps counted, the green at the next one
        (caution_lap); `pending_crossing`: the crossing that ended the stage is still to be counted."""
        with _lock:
            if not (self.caution and self.nascar):
                return None
            self.caution.open_pits()
            self._bump()
            if self.pace_car is not None:
                return 0
            counted = self.caution_laps + (1 if pending_crossing else 0)
            return self.nascar.laps + max(CAUTION_MIN_LAPS - counted, 1) + 2

    def take_blocked_warning(self) -> bool:
        """True once, at the second restart-leader crossing that found the pace car not in the pits (its hub may not be reporting): the green has to be sent by hand."""
        with _lock:
            if self.blocked_crossings == 2:
                self.blocked_crossings = 3
                return True
            return False

    @fieldrec.capture(_lock)
    def set_pace_car(self, car: int | None, kmh: int = PACE_CAR_KMH, guid: str = "") -> None:
        if (car is not None or guid) and not PACE_MIN <= kmh <= PACE_MAX:
            raise ValueError(f"the pace of the pace car is between {PACE_MIN} and {PACE_MAX} km/h")
        with _lock:
            self.pace_car, self.pace_car_kmh, self.pace_guid = car, kmh, guid   # (a driver named but not connected: no slot yet, the guid stays)
            self.pace_car_in = self.pace_car_pitted = self.one_more = self.pace_out = False
            self.pace_park = False
            self.blocked_crossings = 0
            if car is None and self.formation:   # the pace car left in the formation: nobody leads the field any more, the green
                self._end_formation()
                self.set_flag("green", source="formation")
            if car is not None and self.caution:
                self.caution.remove(car)   # (named during a caution: it was in the frozen order)
            self._bump()
            self.audit("pace_car_config", car=car, guid=guid, kmh=kmh, source="set_pace_car", executed=True)

    @fieldrec.capture(_lock)
    def follow_pace_driver(self, car: int | None) -> None:
        """The pace car is a DRIVER (`pace_guid`, the SteamID); its slot changes when it connects or reconnects. `car` is where that driver sits now (None: not on the server): the driver stays named."""
        with _lock:
            if car == self.pace_car or not self.pace_guid:
                return
            self.set_pace_car(car, self.pace_car_kmh, self.pace_guid)

    @fieldrec.capture(_lock)
    def contact(self, now: float, car: int, other: int | None, speed: float) -> None:
        with _lock:
            if self.pace_car is not None and self.pace_car in (car, other):
                return   # the pace car is not a competitor: a touch with it makes nobody 'involved' and is no part of a pile-up
            self.watch.on_contact(now, car, other, speed)
            if self.giveback:
                self.giveback.note_contact(now, car, other)   # (who touched whom around a move off the track: for the director, not for the automatic judge)

    def _caution_step(self, flag: str, was_caution: bool, progress: dict[int, float], now: float, pits=(), completed_laps=None) -> list[tuple]:
        """The flag went to `flag`: a caution starts (freeze the order, pick the free pass) or ends. Returns actions for the adapter, which owns the names and the messages."""
        if flag in CAUTION and not was_caution:
            self.blocked_crossings, self.caution_laps, self.caution_notified = 0, -1, False
            self.pace_car_in = self.pace_car_pitted = self.one_more = self.go_armed = False   # a new caution: the pace car comes out again
            progress = self.without_pace_car(progress)
            recent = self.watch.recent_involved(now, INVOLVED_FOR)
            self.watch.clear()
            self.caution = Caution(progress, now, involved=recent, penalised={p["car_id"] for p in self.penalties}, pitted=pits, pace_only=self.nascar is None, completed_laps=completed_laps)
            if self.caution.pace_only:
                return []   # (no messages of the NASCAR rules: the yellow banner is already out)
            if self.nascar and self.nascar.phase == BREAK:
                self.caution.open_pits()   # (from the first instant: a report cannot be judged between the caution and the notice)
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
        now = self.clock.time() if now is None else now
        with _lock:
            if hold:
                kind, ttl = "hold", HOLD_TTL
            n = {"id": next(_notice_ids), "kind": kind, "text": text.replace('"', "'").replace("\\", "/")[:160], "to": to, "until": now + ttl, "hold": hold}
            alive = [x for x in self.notices if x["until"] > now and not (hold and x.get("hold") == hold)]
            held = [x for x in alive if x.get("hold")]   # (the held ones are not pushed out by the ephemeral ones)
            self.notices = held + [x for x in alive if not x.get("hold")][-(NOTICE_MAX - 1 - len(held)):] + [n]
            self._bump()
            self.audit("notice", notice=n, executed=True)
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
    @fieldrec.capture(_lock)
    def report_pit(self, car_id: int, in_pit: bool, now: float | None = None) -> None:
        """The hub of that car says whether it is in the pit lane. Only a yes is kept (a no never cancels one), and it expires: see the module docstring."""
        if in_pit:
            with _lock:
                self._pit_lap.add(car_id)
                self._pit_seen[car_id] = self.clock.time() if now is None else now
                self._pit_visit(car_id, self._pit_seen[car_id])
                for key, (car, issued) in list(self.dt_open.items()):
                    if car == car_id and self._pit_seen[car_id] > issued:   # a pit-lane pass after the penalty came out serves it
                        del self.dt_open[key]
                        if key in self._manual_dt_keys:
                            self._manual_dt_keys.discard(key)
                        else:
                            _cut_served.add(key)
                for key, (car, issued) in list(self.flag_dt_open.items()):   # (and a blue-flag one)
                    if car == car_id and self._pit_seen[car_id] > issued:
                        del self.flag_dt_open[key]
                        _flag_served.add(key)
                for key, (car, issued) in list(self.drs_dt_open.items()):   # (the same pit pass serves a DRS drive-through)
                    if car == car_id and self._pit_seen[car_id] > issued:
                        del self.drs_dt_open[key]
                        _drs_served.add(key)
                # Keep the return command through parking: pit-lane entry is not
                # the box, and switching to wait would release the hub's brake.
                if car_id == self.pace_car and self.pace_car_in:
                    if not self.pace_car_pitted:
                        log.info("pace car %s reported the pit lane after to go", car_id)
                    self.pace_car_pitted = True
                if self.caution:
                    self.caution.pitted.add(car_id)   # (in the pits during a caution: out of the free pass, to the pit group of the restart)
                    if car_id != self.pace_car:
                        self.caution.pit_enter(car_id)

    @fieldrec.capture(_lock)
    def incident_step(self, speeds: dict[int, float], field, now: float, connected=None) -> tuple[dict | None, list[tuple]]:
        """One look for an incident while racing under green. -> (the new proposal, actions): with `auto_yellow` the yellow is already out (actions are the NASCAR ones to carry out)."""
        with _lock:
            rule = self.neutralization_rule
            mono = self.clock.monotonic()
            samples = field.samples()
            self.neutralization_clear_tick(samples, speeds, mono, now, connected)
            if self.flag in CAUTION and mono - self._audit_last_status >= 5:
                self.audit("neutralization_status", flag=self.flag, pacecar=self.pace_car_state(),
                           leader=max((c for c in samples if c != self.pace_car), key=lambda c: samples[c].progress, default=None),
                           laps=self.caution_laps, green_laps=self._green_laps, speeds=speeds, slow=sorted(self.watch._limp),
                           stopped=[c for c, v in speeds.items() if c != self.pace_car and v <= STOP_KMH], executed=False)
                self._audit_last_status = mono
            if not self.nascar:
                mode = "off" if "neutralization" in self.auto_off else rule.get("mode", "off")
                mono = self.clock.monotonic()
                samples = {c: s for c, s in field.samples().items() if c != self.pace_car and c not in self.neutralization_excluded}
                in_pit = {c for c, t in self._pit_seen.items() if now - t < PIT_RECENT}
                racing = (self.session_ref or {}).get("session_type") == RACE_TYPE and (self.flag in ("green", "white") or (self.flag in SLOWING and self._neutralization_flag and not self.pace_out)) and not self.formation
                stale = sorted(c for c, sample in samples.items() if not 0 <= mono - sample.stamp <= rule.get("stale_telemetry_s", 3))
                gate = (mode, racing, tuple(sorted(self.auto_off)), tuple(stale))
                if gate != self._audit_gate:
                    self.audit("neutralization_gate", effective_mode=mode, stale_cars=stale,
                               kill_switch="neutralization" in self.auto_off, racing=racing, rule=rule,
                               telemetry_age_s={c: mono - samples[c].stamp for c in stale}, executed=False)
                    self._audit_gate = gate
                self._spin_clear_tick(samples, speeds, in_pit, connected, mono, now, mode)
                decision = self.neutralization_watch.step(mono, samples, speeds, in_pit, racing, {**rule, "mode": mode})
                if decision:
                    if decision["kind"] == "spin":
                        for c in decision["cars"]:
                            sample = samples[c]
                            self._spins[c] = {"generation": sample.generation, "since": None, "last": mono}
                    category = rule.get("category", "")
                    target = "local"
                    if decision["kind"] == "pileup":
                        target = rule["mode_by_category"].get(category, rule.get("default_mode", rule["pileup"]["escalate_to"]))
                    if len(decision["events"]) >= rule["severe"]["cars"]:
                        target = "SC"
                        decision["cars"] = sorted(decision["events"])
                    self.autoflags.log.append({**decision, "op": "open", "decision": target, "mode": mode,
                                              "state": self.flag, "timestamp": now, "category": category})
                    evidence = {"decision": decision, "target": target, "rule": rule,
                                "samples": {c: {"age_s": mono - sample.stamp, "generation": sample.generation, "epoch": sample.epoch}
                                            for c, sample in samples.items()}, "in_pit": sorted(in_pit), "thresholds": raceaudit.thresholds(incident_rules)}
                    self.audit("neutralization_decision", **evidence, executed=False)
                    if mode == "live" and target == "SC":
                        if mono < self._sc_cooldown_until:
                            self.audit("neutralization_discard", reason="cooldown", **evidence, executed=False)
                            return None, []
                        pace = field.samples().get(self.pace_car)
                        if pace is None or not 0 <= mono - pace.stamp <= rule["stale_telemetry_s"]:
                            self.audit("neutralization_discard", reason="pace_unavailable", **evidence, executed=False)
                            self.notify("SC recomendado, sácalo a mano: pace car no disponible en la sesión", "flag")
                            return None, []
                        if self.flag in SLOWING:
                            # End the virtual caution through the existing cleanup before starting a fresh SC.
                            self.set_flag("green", now=now, source="incident", field=field)
                        actions = self.set_flag("yellow", now=now, source="incident", field=field)
                        self._neutralization_sc = True
                        self.audit("neutralization_applied", **evidence, executed=True)
                        self.notify("SAFETY CAR AUTOMÁTICO: accidente muy grave", "flag")
                        return None, actions
                    if mode == "live" and target in ("VSC", "FCY") and self.flag in ("green", "white"):
                        actions = self.set_flag(target.lower(), self.pace_car_kmh, now=now, source="incident", field=field, mode="delta")
                        self.audit("neutralization_applied", **evidence, executed=True)
                        return None, actions
                return None, []
            racing = bool(self.nascar and self.nascar.phase in ("racing", "ot_run") and self.nascar.laps >= 1 and self.flag in ("green", "white"))
            in_pit = {car for car, t in self._pit_seen.items() if now - t < PIT_RECENT}
            prop = self.watch.step(now, {c: v for c, v in speeds.items() if c != self.pace_car}, in_pit, racing)
            if not prop:
                return None, []
            self.audit("nascar_incident", proposal=prop, speeds=speeds, executed=self.auto_yellow)
            actions: list[tuple] = []
            if self.auto_yellow:
                actions = self.set_flag("yellow", now=now, source="incident", field=field)
            else:
                self._bump()
            return prop, actions

    def _spin_clear_tick(self, samples, speeds, in_pit, connected, mono, now, mode):
        if mode == "off":
            for state in self._spins.values():
                state["since"] = None
            return
        rule = self.neutralization_rule
        spin = rule["spin"]
        roster = set(connected) if connected is not None else set(samples)
        involved = self.track_clear({}, {}, now).involved
        for car, state in list(self._spins.items()):
            sample = samples.get(car)
            retired = car not in roster or (sample is not None and sample.generation != state["generation"])
            fresh = sample is not None and 0 <= mono - sample.stamp <= rule["stale_telemetry_s"]
            recovered = retired or car in in_pit or (fresh and math.isfinite(speeds.get(car, float("nan")))
                                                     and speeds[car] > spin.get("resume_kmh", 30))
            if mono - state["last"] > rule["stale_telemetry_s"]:
                state["since"] = None
            state["last"] = mono
            if not recovered or involved:
                state["since"] = None
                continue
            if state["since"] is None:
                state["since"] = mono
            if mono - state["since"] >= spin.get("clear_s", 5):
                self.audit("spin_cleared", car=car, reason="retired" if retired else "pits" if car in in_pit else "resumed",
                           clear_s=spin.get("clear_s", 5), executed=False)
                self.autoflags.log.append({"op": "close", "decision": "spin_cleared", "cars": [car],
                                          "mode": mode, "timestamp": now})
                del self._spins[car]

    @fieldrec.capture(_lock)
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
                self.pace_car_in, self.pace_car_pitted, self.blocked_crossings, self.go_armed, self.go_early = True, False, 0, False, 0   # "to go": the pace car goes into the pits and the leader controls the restart
            self._bump()
            self.audit("restart_plan", order=plan[0], waved=plan[1], choosers=sorted(plan[2]), to_go=to_go, executed=True)
            log.info("restart plan: order=%s waved=%s choosers=%s pace_car=%s", plan[0], plan[1], sorted(plan[2]), self.pace_car)
            return plan

    @fieldrec.capture(_lock)
    def caution_tick(self, progress: dict[int, float], speeds: dict[int, float], now: float) -> list[tuple]:
        """One step of the caution being run: the free pass lost to the pits, the cars that passed. Returns actions for the adapter; a violator already has its drive-through listed."""
        with _lock:
            c = self.caution
            if not c:
                return []
            out: list[tuple] = []
            if self.pace_car_in and not self.go_armed:   # (the car that takes the restart seen in the middle of its lap: its next crossing is the one that brings the green)
                lead = c.restart[0] if c.restart else self.pace_car
                if lead in progress and GO_ARM_FROM <= progress[lead] % 1.0 <= GO_ARM_TO:
                    self.go_armed = True
                    self.audit("restart_armed", leader=lead, progress=progress[lead], executed=True)
            if c.pace_only:
                return out   # (practice behind a pace car: nobody is judged)
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
                self.audit("caution_judge", decisions=out, progress=progress, speeds=speeds, exempt=sorted(exempt), executed=True)
                self._bump()
            return out

    @fieldrec.capture(_lock)
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

    @fieldrec.capture(_lock)
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
            return max(0, math.ceil(self.grace_end - (self.clock.time() if now is None else now)))

    def due(self, now: float) -> bool:
        with _lock:
            return self.flag in SLOWING and not self.evaluated and now >= self.grace_end

    @fieldrec.capture(_lock)
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
            self.audit("legacy_pace_judge", violations=out, exempt=spared, limit=limit, window_s=JUDGE_WINDOW, executed=True)
            self.violations, self.exempt = out, spared
            for x in out:
                if self.vsc_penalty["kind"] == "pits":
                    self.add_penalty(x["car_id"], self.vsc_penalty["laps"])
            self._samples = {}
            self._bump()
            return out

    @fieldrec.capture(_lock)
    def autoflags_tick(self, samples: dict, speeds: dict[int, float], length: float | None, mono: float, now: float) -> None:
        """One look for the blue flag and the danger warning (shadow). `mono`: self.clock.monotonic(), the clock of the field's samples; `now`: wall clock, the one of the pit reports."""
        with _lock:
            racing = (self.session_ref or {}).get("session_type") == RACE_TYPE and self.flag in ("green", "white")
            in_pit = {car for car, t in self._pit_seen.items() if now - t < PIT_RECENT}
            pc = self.pace_car
            self.autoflags.modes = {k: ("off" if k in self.auto_off else v) for k, v in self.flag_modes.items()}
            if self.nascar:
                self.autoflags.modes["blue"] = "off"   # NASCAR: a lapped car need not give way
            rule = self._blue_rule()
            self.pace.configure(rule["pace_laps"], rule["pace_margin_pct"])
            self.autoflags.pace_ok = self.pace.quicker
            if self.flag not in ("green", "white"):
                self._neutral_end = now
            for c, g in self.audit_guids.items():   # (and so does a slot whose driver is not the one its laps came from)
                if g and self._pace_owner.get(c, g) != g:
                    self.pace.forget(c); self._pace_owner[c] = g
            for c, smp in samples.items():   # (a slot that changed hands starts from no pace)
                if self._pace_gen.get(c, smp.generation) != smp.generation:
                    self.pace.forget(c)
                self._pace_gen[c] = smp.generation
            previous = self.autoflags.sequence
            self.autoflags.step(mono, {c: s for c, s in samples.items() if c != pc}, {c: v for c, v in speeds.items() if c != pc}, in_pit, length, racing)

            generated = self.autoflags.sequence - previous
            retained = min(generated, len(self.autoflags.log))
            if generated > retained:
                self.audit("audit_gap", source="automatic_flag", lost=generated - retained, executed=False)
            for event in list(self.autoflags.log)[-retained:] if retained else ():
                flag_mode = self.autoflags.modes.get(event["kind"], "off")
                self.audit("automatic_flag", event=event, flag_mode=flag_mode, speeds=speeds,
                           length_m=length, thresholds=raceaudit.thresholds(flag_rules),
                           executed=flag_mode == "live")
            self._blue_judge(mono, now, racing and self.autoflags.modes.get("blue") == "live")
            self._flag_judges(samples, speeds, mono, now, racing, in_pit)

    @fieldrec.capture(_lock)
    def lap_done(self, car: int, ms: int, cuts: int, laps: int, now: float, guid: str = "") -> None:
        """A car completed a lap (ACSP): it feeds the pace of the blue flag when it is a CLEAN lap (not the first, no server cut, no pit-lane pass, begun under green or white, not the pace car). `guid` is the driver
        in the slot now: a slot that changed hands starts from no pace, whatever the field's samples have already noticed."""
        with _lock:
            if guid and self._pace_owner.get(car, guid) != guid:
                self.pace.forget(car)
            if guid:
                self._pace_owner[car] = guid
            clean = (bool(guid) and laps >= 2 and not cuts and car not in self._pit_lap and car != self.pace_car and self.flag in ("green", "white")
                     and now - ms / 1000.0 >= self._neutral_end and (self.session_ref or {}).get("session_type") == RACE_TYPE)
            self.pace.lap(car, ms, clean)
            self._flags_lap(car, car in self._pit_lap, now)

    def _rule(self, kind: str) -> dict:
        return (self.rules or FLAG_DEFAULTS)[kind]

    def _blue_rule(self) -> dict:
        return self._rule("blue")

    def _blue_judge(self, mono: float, now: float, live: bool) -> None:
        """The blue flag's countdown and offences: a pair that has had its blue for `yield_s` seconds without it going away commits ONE offence of the car in front (per appearance of the flag). Under the lock."""
        rule = self._blue_rule()
        pairs = set(self.autoflags.blue)
        for k in [k for k in self.blue_since if k not in pairs]:
            del self.blue_since[k]; self.blue_ignored.discard(k)
        for k in pairs:
            self.blue_since.setdefault(k, mono)
        if not (live and rule["ladder"]):
            return
        for (a, b), since in list(self.blue_since.items()):
            if mono - since >= rule["yield_s"] and (a, b) not in self.blue_ignored:
                self.blue_ignored.add((a, b))
                self._blue_offence(a, b, rule, now)

    def _blue_offence(self, car: int, by: int, rule: dict, now: float) -> None:
        self._flag_sanction("blue", car, rule, now, by=by)

    def _flag_sanction(self, kind: str, car: int, rule: dict, now: float, conduct: bool = True, **info) -> bool:
        """ONE offence of `car` against the flag `kind`: the n-th of this driver takes the ladder's n-th step (0 = a drive-through now, N = N s on the result; the last step repeats), written as a `FlagDue`
        (a drive-through nobody serves costs `unserved_s`). A driver who is not known is never sanctioned (a slot can change hands). Under the lock. True when it was sanctioned."""
        guid = str(self.audit_guids.get(car) or "")
        if not guid:
            self.audit("flag_offence_unattributed", flag=kind, car=car, executed=False, **info)
            return False
        n = self.flag_count.get((guid, kind), 0) + 1
        self.flag_count[(guid, kind)] = n
        ladder = rule["ladder"]
        if not ladder:   # (no sanction configured: written down only)
            self.audit("flag_offence", flag=kind, car=car, driver=guid, seq=n, step=None, executed=False, **info)
            return False
        step = ladder[min(n - 1, len(ladder) - 1)]
        if len(_flag_dues_pending) >= MAX_FLAG_DUES_PENDING:   # (nowhere to write it down: nobody is sanctioned for what could not be recorded)
            log.error("flags: pending ledger full; offence dropped: %s car %s", kind, car)
            self.flag_count[(guid, kind)] = n - 1
            return False
        if step == 0 and not any(c == car for c, _ in self.dt_open.values()) and not any(c == car for c, _ in self.drs_dt_open.values()) \
                and not any(c == car for c, _ in self.flag_dt_open.values()) and not any(p["car_id"] == car for p in self.penalties):
            self.add_penalty(car, CUT_DT_LAPS)
        key = (self.server_id, self.cuts_session_key, guid, kind, n)
        _flag_dues_pending[key] = dict(server_id=self.server_id, session_key=self.cuts_session_key, driver_guid=guid, kind=kind, seq=n, seconds=rule.get("unserved_s", 10) if step == 0 else step, ts=time.time())
        if step == 0:
            self.flag_dt_open[key] = (car, self.clock.time())
        self.audit("flag_offence", flag=kind, car=car, driver=guid, seq=n, step=step, executed=True, **info)
        self.flag_new.append({"msg": "offence", "kind": kind, "car": car, "seq": n, "step": step, "laps": CUT_DT_LAPS})
        if conduct:
            self.conduct_add(guid, car)
        self._bump()
        return True

    def conduct_add(self, guid: str, car: int | None = None) -> None:
        """One more sanction on this driver's conduct (any flag, DRS, a round of the cuts ladder): the black-and-white flag warns him at `bw_at`, the black one comes at `black_at` (rule `black`)."""
        rule = self._rule("black")
        n = self.conduct[guid] = self.conduct.get(guid, 0) + 1
        if car is None:
            car = next((c for c, g in self.audit_guids.items() if g == guid), None)
        if car is None or guid in self.black:
            return
        if n >= rule["black_at"]:
            self.black[guid] = {"car": car, "laps": rule["pit_laps"], "since": self.clock.time()}
            self.flag_new.append({"msg": "black", "car": car, "laps": rule["pit_laps"]})
            self.audit("black_flag", car=car, driver=guid, conduct=n, executed=self.flag_modes.get("black") == "live")
            self._bump()
        elif n >= rule["bw_at"] and guid not in self.bw_shown:
            self.bw_shown.add(guid)
            self.flag_new.append({"msg": "bw", "car": car, "n": n})
            self.audit("bw_flag", car=car, driver=guid, conduct=n, executed=self.flag_modes.get("black") == "live")
            self._bump()

    def _pit_visit(self, car: int, at: float) -> None:
        """A car came into the pit lane (reported at `at`): the orange disc and the black flag given BEFORE that are served (the conduct starts again). A car that was already in the pits when it got the flag has to come in again."""
        guid = str(self.audit_guids.get(car) or "")
        if not guid:
            return
        if guid in self.meatball and at > self.meatball[guid]["since"]:
            del self.meatball[guid]
            self.audit("meatball_served", car=car, driver=guid, executed=True)
        if guid in self.black and at > self.black[guid]["since"]:
            del self.black[guid]; self.conduct[guid] = 0; self.bw_shown.discard(guid)
            self.audit("black_served", car=car, driver=guid, executed=True)

    def _give_black(self, guid: str, car: int, why: str, live: bool) -> None:
        rule = self._rule("black")
        self.black[guid] = {"car": car, "laps": rule["pit_laps"], "since": self.clock.time()}
        self.flag_new.append({"msg": "black", "car": car, "laps": rule["pit_laps"]})
        self.audit("black_flag", car=car, driver=guid, conduct=self.conduct.get(guid, 0), why=why, executed=live)
        self._bump()

    def _flags_lap(self, car: int, in_pit_lap: bool, now: float) -> None:
        """A car completed a lap: one lap fewer to come into the pits for the orange disc (then the black flag), for the red flag (then the black flag) and for the black flag (then the disqualification)."""
        guid = str(self.audit_guids.get(car) or "")
        if not guid:
            return
        live = self.flag_modes.get("black") == "live" and "black" not in self.auto_off
        if self.red_since is not None and car in self.red_laps and car != self.pace_car and guid not in self.black:
            if self._pit_seen.get(car, 0.0) > self.red_since:
                del self.red_laps[car]   # (it came into the pit lane after the red flag: obeyed)
            else:
                self.red_laps[car] -= 1
                if self.red_laps[car] <= 0:
                    del self.red_laps[car]
                    self.meatball.pop(guid, None)
                    self._give_black(guid, car, "red", live and self.flag_modes.get("red") == "live")
        if in_pit_lap:
            return
        if guid in self.meatball:
            self.meatball[guid]["laps"] -= 1
            if self.meatball[guid]["laps"] <= 0:
                del self.meatball[guid]
                self._give_black(guid, car, "meatball", live)
        elif guid in self.black:
            self.black[guid]["laps"] -= 1
            if self.black[guid]["laps"] <= 0:
                del self.black[guid]
                self.conduct[guid] = 0; self.bw_shown.discard(guid)
                self._disqualify(car, guid, live)

    def _disqualify(self, car: int, guid: str, live: bool) -> None:
        """The black flag was not obeyed: the driver is disqualified from the race (a `FlagDue` of kind black; the result turns it into a `dsq` penalty). Only when the black flag is live."""
        if not live:
            self.audit("flag_offence", flag="black", car=car, driver=guid, step="dsq", executed=False)
            return
        n = self.flag_count.get((guid, "black"), 0) + 1
        key = (self.server_id, self.cuts_session_key, guid, "black", n)
        if len(_flag_dues_pending) >= MAX_FLAG_DUES_PENDING:
            log.error("flags: pending ledger full; disqualification dropped: car %s", car)
            return
        self.flag_count[(guid, "black")] = n
        _flag_dues_pending[key] = dict(server_id=self.server_id, session_key=self.cuts_session_key, driver_guid=guid, kind="black", seq=n, seconds=0, ts=time.time())   # (0 s = the disqualification)
        self.audit("flag_offence", flag="black", car=car, driver=guid, seq=n, step="dsq", executed=True)
        self.flag_new.append({"msg": "offence", "kind": "black", "car": car, "seq": n, "step": "dsq", "laps": 0})
        self._bump()

    def damage_report(self, car: int, level: int, now: float) -> None:
        """The driver's own game says how damaged its car is (0-100 %): client evidence for the orange disc. A repaired car (back under the level) loses the disc at once."""
        with _lock:
            if car == self.pace_car:
                return
            self.damage[car] = (max(0, min(100, level)), now)

    def take_flag_messages(self) -> list[dict]:
        with _lock:
            out, self.flag_new = self.flag_new, []
            return out

    def set_red(self, on: bool, now: float | None = None) -> None:
        """The director shows (or withdraws) the red flag: every car must come into the pit lane before it completes `laps` more lap(s), or it gets the black flag."""
        with _lock:
            self.red_since = (self.clock.time() if now is None else now) if on else None
            self.red_laps = {c: self._rule("red")["laps"] for c in self.audit_guids if c != self.pace_car} if on else {}
            self.audit("red_flag", on=on, executed=True)
            self._bump()

    def _flag_judges(self, samples: dict, speeds: dict[int, float], mono: float, now: float, racing: bool, in_pit: set) -> None:
        """The orange disc: a car whose own game reports `damage_pct` % damage or more (and, with `use_speed`, one crawling far below the cars around it) must come into the pits; see `_flags_lap` for what follows."""
        pc = self.pace_car
        rule = self._rule("meatball")
        mode = self.flag_modes.get("meatball", "shadow")
        if not (racing and mode != "off" and "meatball" not in self.auto_off):
            return
        wanted: dict[int, str] = {}   # car -> why
        for c, (level, at) in list(self.damage.items()):
            if now - at > DAMAGE_FRESH_S:
                del self.damage[c]   # (a stale report is no report: a hub that went quiet does not keep a disc on)
            elif level >= rule["damage_pct"] and c != pc and c not in in_pit:
                wanted[c] = "damage"
        if rule["use_speed"]:
            moving = sorted(v for c, v in speeds.items() if c != pc and c not in in_pit and v >= 30.0)
            ref = moving[len(moving) // 2] if len(moving) >= 3 else None
            for c, v in speeds.items():
                if c == pc or c in in_pit or ref is None or not (5.0 < v < ref * rule["slow_pct"] / 100.0):
                    self._slow_since.pop(c, None)
                    continue
                if mono - self._slow_since.setdefault(c, mono) >= rule["sustained_s"]:
                    wanted.setdefault(c, "slow")
            for c in [c for c in self._slow_since if c not in speeds]:
                del self._slow_since[c]
        for c, why in wanted.items():
            guid = str(self.audit_guids.get(c) or "")
            if guid and guid not in self.meatball and guid not in self.black:
                self.meatball[guid] = {"car": c, "laps": rule["pit_laps"], "since": self.clock.time()}
                self.flag_new.append({"msg": "meatball", "car": c, "laps": rule["pit_laps"]})
                self.audit("meatball_flag", car=c, driver=guid, why=why, damage=self.damage.get(c, (None,))[0], executed=mode == "live")
                self._bump()

    @fieldrec.capture(_lock)
    def yellow_tick(self, samples: dict, speeds: dict[int, float], mono: float, now: float) -> None:
        """No overtaking under a plain yellow (the judge of the VSC/FCY, its own instance): whoever passes owes the place for `window_s` s or commits an offence of the `yellow` rule. Not under a NASCAR caution (it has its own)."""
        with _lock:
            rule = self._rule("yellow")
            mode = self.flag_modes.get("yellow", "shadow")
            yellow = self.flag == "yellow"
            if yellow and not self._was_yellow:
                self._yellow_id += 1
            self._was_yellow = yellow
            active = yellow and mode != "off" and "yellow" not in self.auto_off and not self.caution and not self.nascar and (self.session_ref or {}).get("session_type") == RACE_TYPE
            self.yellowjudge.window_s = float(rule["window_s"])
            pc = self.pace_car
            in_pit = {car for car, t in self._pit_seen.items() if now - t < PIT_EXEMPT_FOR}
            for e in self.yellowjudge.step(mono, self._yellow_id, active, {c: s for c, s in samples.items() if c != pc}, {c: v for c, v in speeds.items() if c != pc}, in_pit):
                if e[0] == "open":
                    self.flag_new.append({"msg": "yellow_open", "car": e[1], "passed": list(e[2]), "seconds": e[3], "live": mode == "live"})
                elif e[0] == "cleared":
                    self.flag_new.append({"msg": "yellow_cleared", "car": e[1], "live": mode == "live"})
                else:
                    self.audit("no_pass", decision={"kind": "yellow_due", "car": e[1]}, executed=mode == "live")
                    if mode == "live":
                        self._flag_sanction("yellow", e[1], rule, now)

    def auto_for(self, slot: int, generation: int, mono: float, fresh_modes: dict | None = None, model: str | None = None) -> dict | None:
        """What this driver's screen may show now: the automatic flags (blue / danger: only the types set to `live` in the monitor's reading AND in `fresh_modes`, the one the request has just made, and not
        killed by the director, only under green or white, and only from a look of the last AUTO_MAX_AGE_S: a stalled monitor must not leave a flag on a screen) and, in a delta VSC, `vsc`: the frozen
        minimum times of his model for the screen to compute his delta. None when there is nothing."""
        with _lock:
            vsc = self.delta_block(model)
            live = {k for k, v in self.flag_modes.items() if v == "live" and k not in self.auto_off and (fresh_modes is None or fresh_modes.get(k) == "live")}
            res = None
            if live and self.flag in ("green", "white") and self.autoflags.last is not None and mono - self.autoflags.last <= AUTO_MAX_AGE_S:
                res = self.autoflags.for_slot(slot, generation, mono, live)
                if res and res.get("blue") and "blue" in live and self._blue_rule()["ladder"]:   # the counter next to the flag: seconds left to let him through, offences so far, the step of the next one
                    rule, k = self._blue_rule(), (slot, res["blue"]["by"])
                    guid = str(self.audit_guids.get(slot) or "")
                    n = self.flag_count.get((guid, "blue"), 0)
                    ladder = rule["ladder"]
                    res["blue"].update(left_s=max(0, math.ceil(rule["yield_s"] - (mono - self.blue_since.get(k, mono)))), n=n, step=ladder[min(n, len(ladder) - 1)], ignored=k in self.blue_ignored)
            extra = self._personal_flags(slot, fresh_modes)
            if extra:
                res = {**(res or {"blue": None, "danger": None, "age_ms": 0, "session": self.autoflags.uid}), **extra}
            if vsc is not None:
                res = {**(res or {"blue": None, "danger": None, "age_ms": 0, "session": self.autoflags.uid}), "vsc": vsc}
            return res

    def _personal_flags(self, slot: int, fresh_modes: dict | None) -> dict:
        """The flags this driver has that no detector of the field draws: the black-and-white and the black one (conduct), the orange disc (a crawling car), the red one (go to the pits). Each only while its rule is `live`."""
        def live(k: str) -> bool:
            return self.flag_modes.get(k) == "live" and k not in self.auto_off and (fresh_modes is None or fresh_modes.get(k) == "live")
        out: dict = {}
        guid = str(self.audit_guids.get(slot) or "")
        if guid and live("black"):
            if guid in self.black:
                out["black"] = {"laps": self.black[guid]["laps"]}
            elif guid in self.bw_shown:
                out["bw"] = {"n": self.conduct.get(guid, 0)}
        if guid and live("meatball") and guid in self.meatball:
            out["meatball"] = {"laps": self.meatball[guid]["laps"]}
        if self.red_since is not None and live("red") and slot != self.pace_car and guid not in self.black:
            out["red"] = {"laps": self.red_laps.get(slot, 0), "pit": self._pit_seen.get(slot, 0.0) > self.red_since}
        return out

    def auto_live(self, fresh_modes: dict | None = None) -> list[str]:
        """The types a screen may be shown now: the hub asks every second when this is not empty and rarely otherwise (`vsc`: a delta VSC/FCY is running)."""
        with _lock:
            out = sorted(k for k, v in self.flag_modes.items() if v == "live" and k not in self.auto_off and (fresh_modes is None or fresh_modes.get(k) == "live"))
            return out + ["vsc"] if self.flag in SLOWING and self.vsc_mode == "delta" else out

    def judging_continuously(self) -> bool:
        """Whether the continuous judge penalises now: not when this flag started with the kill switch `pace` on (then the old single judgement at the end of the grace decides, and only it), and not while
        the switch is on (the judge only logs). Switching it on or off in the middle of a flag never makes the two judges punish the same car."""
        return not self.legacy and "pace" not in self.auto_off

    def _is_race(self) -> bool:
        return (self.session_ref or {}).get("session_type") == RACE_TYPE

    def _queue_neutral_due(self, car: int, generation: int, kind: str, level: int, seconds: int, now: float, reason: str) -> None:
        """A time penalty of a VSC / FCY that waits for the result of the race (app/stewards/auto.py `neutral_penalties`): queued here, written to the database by the monitor (retained across session changes)."""
        key = (getattr(self, "server_id", 0), self.vsc_id, car, generation, level)
        with _lock:
            if key not in _neutral_dues_pending and len(_neutral_dues_pending) < MAX_NEUTRAL_DUES_PENDING:
                _neutral_dues_pending[key] = {"server_id": key[0], "flag_id": self.vsc_id, "car": car, "generation": generation, "level": level, "kind": kind, "seconds": seconds, "ts": now, "reason": reason}

    def _fcy_green_dues(self, now: float) -> None:
        """An FCY is over (green): every car whose delta over the whole flag (its total, without the credit limit) is still under -tol gets the severe penalty, the second level after the drive-through of the flag
        (the judged delta never lies above the total, so such a car was already penalised in the flag: the check is about whether the offence stands at the end; whoever recovered by slowing down is spared)."""
        severe_s = self.vsc_penalty.get("severe_s", 0)
        if severe_s <= 0 or not self._is_race() or not self.judging_continuously() or self.vsc_mode != "delta":
            return
        for car, (generation, real) in self.pacedelta.totals(self.vsc_id).items():
            if real < -self.pacedelta.tol_s:
                self._queue_neutral_due(car, generation, "fcy", 0, severe_s, now, f"FCY: delta total {real:+.2f} s al volver a verde")

    @fieldrec.capture(_lock)
    def pace_tick(self, samples: dict, speeds: dict[int, float], mono: float, now: float, models: dict[int, str] | None = None) -> list[dict]:
        """One look at the pace of a VSC / FCY. Two judges share it: the DELTA judge (app/live/sectors.py) for the cars of a delta VSC whose model has a reference lap, the FIXED-pace judge
        (app/live/pacejudge.py) for every other car. The sector clock and the reference laps are also fed here, in green. A breach is listed as a violation and gets the flag's penalty, once; with
        the kill switch `pace` on it is only logged. `mono`: the clock of the field's samples; `now`: the wall clock; `models`: car -> car model.
        -> the new breaches [{car, generation, kind: pace|delta, speed_kmh, ...}] (the caller announces them)."""
        with _lock:
            models = models or {}
            pc = self.pace_car
            in_pit = {car for car, t in self._pit_seen.items() if now - t < PIT_EXEMPT_FOR}
            stype = (self.session_ref or {}).get("session_type")
            green = stype in (1, 2, 3) and self.flag in ("green", "white")   # practice, qualifying or race (ACSP 1/2/3): an unknown session type never teaches a reference lap
            delta_on = self.flag in SLOWING and self.vsc_mode == "delta" and now >= self.grace_end
            if delta_on and self._since is None:
                self._since = mono - (now - self.grace_end)   # the instant the grace ended, on the clock of the samples: latched, a later change of the wall clock does not move it
            since = self._since if self._since is not None else float("inf")
            covered: set[int] = set()
            found: list[dict] = []
            for car in [c for c in self._seen if c not in samples]:   # gone: its slot may be somebody else's
                del self._seen[car]; self.sectorclock.forget(car); self.reflaps.forget(car)
            for car, s in samples.items():
                if car == pc or s.stamp <= self._seen.get(car, float("-inf")):
                    continue
                self._seen[car] = s.stamp
                events = self.sectorclock.observe(car, s.progress, s.stamp, s.epoch, s.generation)
                if green:
                    self.reflaps.feed(car, models.get(car), events, car in in_pit)
                else:
                    self.reflaps.forget(car)   # (a lap that was running when the green went away is not a reference: its partial times are dropped)
                prof = self.vsc_profiles.get(models.get(car)) if self.vsc_mode == "delta" else None   # (a model with a reference lap: its sectors; any other car: its speed, below)
                if prof is not None:
                    for d in self.deltajudge.feed(mono, self.vsc_id, delta_on, since, car, s.generation, prof, events, car in in_pit, self.vsc_penalty.get("warnings")):
                        found.append({**d, "kind": "delta", "speed_kmh": round(speeds.get(car) or 0)})
            active = self.flag in SLOWING and bool(self.pace_kmh) and now >= self.grace_end
            if self.vsc_mode == "delta":
                covered = {c for c in samples if c != pc}   # every car is judged by a delta: by sectors when its model has a reference lap, else by its own speed against a virtual car at the limit
                nolap = {c: s for c, s in samples.items() if c != pc and self.vsc_profiles.get(models.get(c)) is None}
                found += [{**d, "kind": "delta"} for d in self.pacedelta.step(mono, self.vsc_id, active, self.pace_kmh or 0, self.tolerance_kmh, nolap, {c: v for c, v in speeds.items() if c in nolap}, in_pit)]
            fixed = self.pacejudge.step(mono, self.vsc_id, active, (self.pace_kmh or 0) + self.tolerance_kmh, {c: s for c, s in samples.items() if c != pc and c not in covered},
                                        {c: v for c, v in speeds.items() if c != pc and c not in covered}, in_pit)
            found += [{**d, "kind": "pace"} for d in fixed]
            if not found:
                return found
            applied, pen, flag = self.judging_continuously(), dict(self.vsc_penalty), self.flag   # (taken here, under the lock: what the caller announces is what was done)
            severe = [d for d in found if d.get("level") == 2]   # (the second level of a VSC by sectors: not a drive-through, seconds on the result)
            found = [{**d, "applied": applied and (d["car"], d["generation"]) not in self._pen_cars, "pen": pen, "flag": flag} for d in found if d.get("level") != 2]
            if applied:
                for d in [d for d in found if d["applied"]]:
                    self._pen_cars.add((d["car"], d["generation"]))
                    self.violations.append({"car_id": d["car"], "speed_kmh": d["speed_kmh"]})
                    if pen["kind"] == "pits":
                        self.add_penalty(d["car"], pen["laps"])
                self._bump()
            for d in severe:
                key = (d["car"], d["generation"])
                sd = {**d, "pen": pen, "flag": flag, "applied": False}
                if applied and key not in self._sev_cars and pen.get("severe_s", 0) > 0 and self._is_race():
                    self._sev_cars.add(key)
                    self._queue_neutral_due(d["car"], d["generation"], "vsc", 2, pen["severe_s"], now, f"VSC: sectores en rojo tras {pen.get('warnings', WARN_DEFAULT)} avisos (delta {d['delta_s']:+.2f} s)")
                    sd["applied"] = True
                    self._bump()
                found.append(sd)
            for decision in found:
                self.audit("pace_judge", decision=decision, limit=(self.pace_kmh or 0) + self.tolerance_kmh, executed=decision["applied"])
            return found

    @fieldrec.capture(_lock)
    def pass_tick(self, samples: dict, speeds: dict[int, float], mono: float, now: float) -> list[dict]:
        """One look at the overtaking of a VSC / FCY whose grace is over (app/live/nopass.py). -> [{kind: pass_open | pass_cleared | pass_due, car, applied, flag, ...}] for the caller to announce; `applied` is False
        with the director's kill switch `pass` (the judge only logs). A `pass_due` lists the violation and gives the flag's penalty (the drive-through), once per car and flag."""
        with _lock:
            active = self.flag in SLOWING and bool(self.pace_kmh) and now >= self.grace_end
            pc = self.pace_car
            in_pit = {car for car, t in self._pit_seen.items() if now - t < PIT_EXEMPT_FOR}
            events = self.passjudge.step(mono, self.vsc_id, active, {c: s for c, s in samples.items() if c != pc}, {c: v for c, v in speeds.items() if c != pc}, in_pit)
            if not events:
                return []
            applied, pen, flag = "pass" not in self.auto_off, dict(self.vsc_penalty), self.flag
            out: list[dict] = []
            for e in events:
                base = {"car": e[1], "applied": applied, "flag": flag, "pen": pen}
                if e[0] == "open":
                    out.append({**base, "kind": "pass_open", "passed": e[2], "seconds": e[3]})
                elif e[0] == "cleared":
                    out.append({**base, "kind": "pass_cleared"})
                else:
                    fresh = applied and (e[1], e[2]) not in self._pen_cars   # (the pace judge may have penalised him already in this flag)
                    out.append({**base, "kind": "pass_due", "generation": e[2], "applied": fresh})
                    if fresh:
                        self._pen_cars.add((e[1], e[2]))
                        self.violations.append({"car_id": e[1], "speed_kmh": 0, "kind": "pass"})
                        if pen["kind"] == "pits":
                            self.add_penalty(e[1], pen["laps"])
            if applied:
                self._bump()
            for decision in out:
                self.audit("no_pass", decision=decision, executed=decision["applied"])
            return out

    def delta_block(self, model: str | None) -> dict | None:
        """What a screen of a car of this model needs to show the delta of the VSC/FCY being run: the frozen minimum ms per sector (a model with a reference lap) or the pace and its tolerance (any other car), and the judge numbers. None if this is not a delta flag."""
        with _lock:
            if self.flag not in SLOWING or self.vsc_mode != "delta":
                return None
            base = {"mode": "delta", "tol": self.deltajudge.tol_s, "credit": self.deltajudge.credit_s, "id": self.vsc_id, "need": self.vsc_penalty.get("warnings", WARN_DEFAULT)}
            prof = self.vsc_profiles.get(model)
            return {**base, "min_ms": list(prof)} if prof is not None else {**base, "pace_kmh": self.pace_kmh or 0, "band_kmh": self.tolerance_kmh}   # (no reference lap: a virtual car at the pace, by the car's own speed; the tolerance is a dead band)

    @fieldrec.capture(_lock)
    def set_pace_config(self, tolerance_kmh: int | None = None, penalty: dict | None = None) -> None:
        """The director changes the tolerance and / or the penalty of the VSC / FCY being run, from now on (whoever was judged already keeps what he got)."""
        with _lock:
            if self.flag not in SLOWING:
                raise ValueError("there is no VSC or FCY running")
            if tolerance_kmh is not None and not 0 <= tolerance_kmh <= TOL_MAX:
                raise ValueError(f"the tolerance is between 0 and {TOL_MAX} km/h")
            clean = self._clean_penalty(penalty) if penalty is not None else None
            if tolerance_kmh is not None:
                self.tolerance_kmh = tolerance_kmh
            if clean is not None:
                self.vsc_penalty = clean
            self._bump()

    def autoflags_view(self) -> dict:
        with _lock:
            return {**self.autoflags.view(), "pace": list(self.pacejudge.log)[-30:], "delta": sorted([*self.deltajudge.log, *self.pacedelta.log], key=lambda x: x["t"])[-30:], "auto_off": sorted(self.auto_off),
                    "vsc_mode": self.vsc_mode, "vsc_fallback": self.vsc_fallback, "ref_models": sorted(self.reflaps.best)}

    @fieldrec.capture(_lock)
    def give_back_tick(self, progress: dict[int, float], speeds: dict[int, float], now: float) -> list[tuple]:
        """One look at the advantage gained off the track. -> the events of app/live/giveback.py (open / cleared / due); [] and nothing kept when this is not a race with the rule on."""
        with _lock:
            if not (self.gb_rule and (self.session_ref or {}).get("session_type") == RACE_TYPE):
                if self.giveback:
                    self.giveback.clear()
                    self.giveback = None
                self.gb_owner.clear()
                return []
            if self.giveback is None:
                self.giveback = GiveBack(self.gb_rule["window_s"])
            self.giveback.window_s = self.gb_rule["window_s"]
            exempt = {car for car, t in self._pit_seen.items() if now - t < PIT_RECENT}
            progress = self.without_pace_car(progress)
            events = self.giveback.step(now, progress, {c: v for c, v in speeds.items() if c in progress}, exempt, caution=self.flag in CAUTION)
            if events:
                self.audit("give_back", events=events, rule=self.gb_rule, thresholds=raceaudit.thresholds(giveback_rules), executed=True)
                self._bump()
            return events

    @fieldrec.capture(_lock)
    def give_back_forgive(self, ob_id: int) -> bool:
        with _lock:
            ok = bool(self.giveback and self.giveback.forgive(ob_id))
            self.gb_owner.pop(ob_id, None)
            if ok:
                self.audit("give_back_forgiven", obligation_id=ob_id, executed=True)
                self._bump()
            return ok

    def give_back_peek(self, ob_id: int) -> dict | None:
        """A copy of an open obligation ({id, car, creditors}) without closing it."""
        with _lock:
            ob = next((o for o in (self.giveback._open.values() if self.giveback else ()) if o["id"] == ob_id), None)
            return {"id": ob["id"], "car": ob["car"], "creditors": sorted(ob["creditors"])} if ob else None

    @fieldrec.capture(_lock)
    def give_back_due_now(self, ob_id: int) -> dict | None:
        """The director sanctions an open obligation: closed and returned (None when it was not open); `give_back_restore` puts it back if its penalty cannot be written."""
        with _lock:
            ob = self.giveback.due_now(ob_id) if self.giveback else None
            if ob:
                self._bump()
                self.audit("give_back_due_manual", obligation=ob, executed=True)
            return ob

    @fieldrec.capture(_lock)
    def give_back_restore(self, ob: dict) -> None:
        with _lock:
            if self.giveback:
                self.giveback.restore(ob)
                self.audit("give_back_restored", obligation=ob, executed=True)
                self._bump()

    @fieldrec.capture(_lock)
    def give_back_report(self, car: int, now: float, ms: int, lap: int, spline: float) -> None:
        """An excursion off the track that the driver's own game reported (stewards/report)."""
        with _lock:
            if self.giveback and car != self.pace_car:
                self.giveback.report(car, now, ms, lap, spline)
                self.audit("give_back_excursion", car=car, ms=ms, lap=lap, spline=spline, executed=True)

    def add_cuts(self, car: int, n: int, from_client: bool, rule: dict | None = None, driver_guid: str = "") -> None:
        """CSP excursions count live; practice/qualifying return before race ladder reconciliation."""
        with _lock:
            if car == self.pace_car:
                return
            session = self.session_ref if isinstance(self.session_ref, dict) else {}
            if session.get("session_type") in (1, 2):
                if from_client and n > 0:
                    self.cuts_client[car] = self.cuts_client.get(car, 0) + n
                    self.audit("cuts", car=car, count=self.cuts_client[car], added=n, from_client=True, executed=True)
                    self._bump()
                return
            if session.get("session_type") != RACE_TYPE:
                return
            if driver_guid:
                client, server = self.cuts_totals.get(driver_guid, (0, 0))
                if rule and rule.get("ladder") and self.cuts_owners.get(car) in (None, driver_guid):
                    # Recover counts received before identity/rules were available.
                    client = max(client, self.cuts_client.get(car, 0))
                    server = max(server, self.cuts_server.get(car, 0))
                self.cuts_owners[car] = driver_guid
                self.cuts_totals[driver_guid] = (client + n, server) if from_client else (client, server + n)
                if n > 0:
                    self.cuts_at[driver_guid] = time.time()
            book = self.cuts_client if from_client else self.cuts_server
            if rule and rule.get("ladder") and driver_guid:
                # Slot changes must not transfer cuts or rounds to another driver.
                self.cuts_client[car], self.cuts_server[car] = self.cuts_totals[driver_guid]
            else:
                book[car] = book.get(car, 0) + n
            if rule and rule.get("ladder") and driver_guid:
                size, ladder = rule["max"] + 1, rule["ladder"]
                total = max(self.cuts_client.get(car, 0), self.cuts_server.get(car, 0))
                for round_no in range(self.cuts_emitted.get(driver_guid, 0) + 1, total // size + 1):
                    row = dict(server_id=self.server_id, session_key=self.cuts_session_key,
                               driver_guid=driver_guid, round=round_no,
                               seconds=ladder[min(round_no - 1, len(ladder) - 1)], ts=self.cuts_at.get(driver_guid, time.time()))
                    live_only = getattr(rule, "live_only", False)
                    if not live_only and len(_cut_dues_pending) >= MAX_CUT_DUES_PENDING:
                        log.error("cuts: pending ledger full; round could not be queued: %s", row)
                        continue
                    key = (self.server_id, self.cuts_session_key, driver_guid, round_no)
                    if not live_only:
                        _cut_dues_pending[key] = row
                    self.cuts_emitted[driver_guid] = round_no
                    self.conduct_add(driver_guid, car)
                    if row["seconds"] == 0:   # a drive-through step: one penalty per car at a time (a single pit pass serves them all)
                        if not any(c == car for c, _ in self.dt_open.values()):
                            self.add_penalty(car, CUT_DT_LAPS)
                        self.dt_open[key] = (car, self.clock.time())
                        if live_only:
                            self._manual_dt_keys.add(key)
                    # Manual sessions only execute zero-second (DT) steps live;
                    # positive seconds are counter-only and never reach results.
                    self.audit("cuts_due", due=row, executed=True, persistence="live_only" if live_only else "pending")
            if n > 0:
                self.audit("cuts", car=car, count=book[car], added=n, from_client=from_client, executed=True)
                self._bump()

    def drs_tick(self, rule: dict | None, now: float, laps: int, signal: float | None) -> None:
        """The DRS state of a race: `rule` is the cached rule (None = off, or not a race), `laps` the leader's completed laps, `signal` the weather's wetness 0..1 (None = unknown). Event loop, no database."""
        with _lock:
            if not rule or (self.session_ref or {}).get("session_type") != RACE_TYPE:
                if self.drs:
                    self.drs = None
                    self._bump()
                return
            if self.drs is None:
                self.drs = Drs(rule)
                self._bump()
            elif self.drs.rule != rule:
                self.drs.rule = rule   # (the rule changed mid-session: the periods, the restart count and who was already sanctioned stay; the next step applies the new numbers)
            if self.drs.step(now, laps, self.flag in ("green", "white"), signal):
                self._bump()
                self.audit("drs", state=self.drs.view(), window=self.drs.window, executed=True)

    def drs_use(self, car: int, guid: str, now: float) -> dict | None:
        """The hub of `car` saw it using DRS. None when nothing is owed (DRS is on, inside the grace, already sanctioned in this period, the pace car, no rule). Otherwise a drive-through is queued (one per car at a time:
        a single pit pass serves all) and the offence is written down as a `DrsDue` (`unserved_s` seconds on the result if nobody serves it; a manual session keeps it live only). The caller tells the driver."""
        with _lock:
            if not self.drs or car == self.pace_car or not self.drs.used(guid or f"car{car}", now):
                return None
            rule, why, window = self.drs.rule, self.drs.why, self.drs.window
            if guid and not getattr(rule, "live_only", False) and len(_drs_dues_pending) >= MAX_DRS_DUES_PENDING:   # (nowhere to write it down: no sanction for what could not be recorded)
                log.error("drs: pending ledger full; offence dropped: car %s", car)
                return None
            if not any(c == car for c, _ in self.dt_open.values()) and not any(c == car for c, _ in self.drs_dt_open.values()) and not any(p["car_id"] == car for p in self.penalties):
                self.add_penalty(car, CUT_DT_LAPS)
            if guid and not getattr(rule, "live_only", False):
                key = (self.server_id, self.cuts_session_key, guid, window)
                _drs_dues_pending[key] = dict(server_id=self.server_id, session_key=self.cuts_session_key, driver_guid=guid, period=window, seconds=rule["unserved_s"], ts=time.time())
                self.drs_dt_open[key] = (car, self.clock.time())
            self.audit("drs_use", car=car, driver=guid, window=window, why=why, penalty="dt", executed=True)
            if guid:
                self.conduct_add(guid, car)
            return {"why": why, "laps": CUT_DT_LAPS}

    def give_back_view(self, now: float) -> dict:
        """What the hub and the panel read: the obligations that are open ({id, car, creditors, left_s}) and the cases left for the director."""
        with _lock:
            gb = self.giveback
            label = lambda c: self.gb_names.get(c) or f"#{c}"
            clean = lambda s: str(s).replace('"', "'").replace("\\", "/")[:24]   # (the hub reads the state with patterns: no quote or backslash in a name)
            obs = [{**o, "names": ", ".join(clean(label(c)) for c in o["creditors"])} for o in (gb.open_obligations(now) if gb else [])]
            return {"obligations": obs, "give_back_reviews": list(gb.reviews) if gb else []}

    def cuts_view(self, rule: dict | None) -> dict:
        """The counter of the hub: {cuts: {car: n}, cuts_rule}. n includes CSP reports; native lap cuts are ignored at ingress.
        Practice/qualifying expose an informational counter; races retain the ladder view."""
        with _lock:
            session = self.session_ref if isinstance(self.session_ref, dict) else {}   # (a test may hold a bare object as the session reference: the default rule is always there now)
            if not (rule and session.get("session_type") in (1, 2, RACE_TYPE)):
                return {"cuts": {}, "cuts_rule": None}
            if session.get("session_type") in (1, 2):
                return {"cuts": {str(c): n for c, n in sorted(self.cuts_client.items())},
                        "cuts_rule": {k: rule[k] for k in ("cap", "max", "per", "step")}, "cuts_info": True}
            cars = set(self.cuts_server) | set(self.cuts_client)
            cuts = {str(c): max(self.cuts_server.get(c, 0), self.cuts_client.get(c, 0)) for c in sorted(cars)}
            extra = ({"cuts_round": {c: n % (rule["max"] + 1) for c, n in cuts.items()},
                      "cuts_rounds": {c: n // (rule["max"] + 1) for c, n in cuts.items()},
                      "cuts_ladder": list(rule["ladder"])}
                     if rule.get("ladder") else {})
            return {"cuts": cuts, "cuts_rule": {k: rule[k] for k in ("cap", "max", "per", "step")}, **extra}

    def snapshot(self, running: bool = True, now: float | None = None) -> dict:
        rule = cut_rule_peek(self.server_id) if self.server_id else None   # (the cache only: this is also taken from the event loop and under the caller's lock, so no database; race-state refreshes it first)
        with _lock:
            at = self.clock.time() if now is None else now
            notices = [{"id": n["id"], "kind": n["kind"], "left_s": math.ceil(n["until"] - at), "text": n["text"], "to": n["to"]} for n in self.notices if n["until"] > at]
            nascar = {**self.nascar.snapshot(), "next_delay_min": self.next_delay_s // 60} if self.nascar else None
            return {"rev": self.rev, "ts": int(self.ts), "running": running, "flag": self.flag, "pace_kmh": self.pace_kmh, "penalties": list(self.penalties), "notices": notices,
                    "vsc_id": self.vsc_id, "grace_left": self.grace_left(now), "tolerance_kmh": self.tolerance_kmh if self.flag in SLOWING else None,
                    "vsc_penalty": dict(self.vsc_penalty) if self.flag in SLOWING else None, "violations": list(self.violations), "exempt": list(self.exempt), "nascar": nascar, "caution": self.caution.snapshot() if self.caution else None, "caution_eta": self.caution_eta(),
                    "proposal": self.watch.proposal, "auto_yellow": self.auto_yellow, "pacecar": self.pace_car_state(), "pace_steam_id": self.pace_guid,
                    "nascar_next_delay_min": self.next_delay_s // 60, "drs": self.drs.view() if self.drs else None, **self.cuts_view(rule), **self.give_back_view(self.clock.time() if now is None else now)}


def cut_rule_peek(server_id: int) -> dict | None:
    """The cut rule as it was last read (no database, no waiting): what the state snapshot shows."""
    try:
        from app.league import cut_rule_cached   # (late: league imports servers, which imports this module)
        return cut_rule_cached(server_id)
    except Exception:   # noqa: BLE001 - the counter must never break the state every hub reads
        return None


def cut_rule_of(server_id: int) -> dict | None:
    """The cut rule of the league event running on this server (app/league.py `cut_rule`), read from the database when the cache is stale: only from a request thread, never the event loop or under the lock.
    None when the database cannot say: the counter is only a screen."""
    try:
        from app.league import cut_rule   # (late: league imports servers, which imports this module)
        return cut_rule(server_id)
    except Exception:   # noqa: BLE001 - never let the counter break the state every hub reads
        log.warning("cut rule of server %s could not be read", server_id, exc_info=True)
        return None


def flag_modes_peek(server_id: int) -> dict | None:
    """The league's modes of the automatic flags as last read (no database, no waiting): None when missing or old."""
    try:
        from app.league import flag_modes_cached
        return flag_modes_cached(server_id)
    except Exception:   # noqa: BLE001 - never let this break the monitor
        return None


def flag_modes_stale(server_id: int) -> bool:
    try:
        from app.league import flag_modes_stale as stale
        return stale(server_id)
    except Exception:   # noqa: BLE001
        return False


def flag_modes_of(server_id: int) -> dict | None:
    """The same modes, read from the database when the cache is stale: only from a request thread."""
    try:
        from app.league import flag_modes
        return flag_modes(server_id)
    except Exception:   # noqa: BLE001
        log.warning("automatic-flag modes of server %s could not be read", server_id, exc_info=True)
        return None


def flag_rules_peek(server_id: int) -> dict | None:
    try:
        from app.league import flag_rules_cached   # (late: league imports servers, which imports this module)
        return flag_rules_cached(server_id)
    except Exception:   # noqa: BLE001 - never let this break the monitor
        return None


def flag_rules_stale(server_id: int) -> bool:
    try:
        from app.league import flag_rules_stale as stale
        return stale(server_id)
    except Exception:   # noqa: BLE001
        return False


def flag_rules_of(server_id: int) -> dict | None:
    """Read from the database when the cache is stale: only from a request thread or a worker."""
    try:
        from app.league import flag_rules
        return flag_rules(server_id)
    except Exception:   # noqa: BLE001
        log.warning("flag rules of server %s could not be read", server_id, exc_info=True)
        return None


def drs_rule_peek(server_id: int) -> dict | None:
    """The DRS rule as it was last read (no database, no waiting): what the monitor uses on the event loop."""
    try:
        from app.league import drs_rule_cached   # (late: league imports servers, which imports this module)
        return drs_rule_cached(server_id)
    except Exception:   # noqa: BLE001 - never let this break the monitor
        return None


def drs_rule_stale(server_id: int) -> bool:
    try:
        from app.league import drs_rule_stale as stale
        return stale(server_id)
    except Exception:   # noqa: BLE001
        return False


def drs_rule_of(server_id: int) -> dict | None:
    """The same rule, read from the database when the cache is stale: only from a request thread or a worker."""
    try:
        from app.league import drs_rule
        return drs_rule(server_id)
    except Exception:   # noqa: BLE001
        log.warning("DRS rule of server %s could not be read", server_id, exc_info=True)
        return None


def give_back_rule_peek(server_id: int) -> dict | None:
    """The rule of the advantage gained off the track as it was last read (no database, no waiting): what the monitor uses on the event loop."""
    try:
        from app.league import give_back_rule_cached   # (late: league imports servers, which imports this module)
        return give_back_rule_cached(server_id)
    except Exception:   # noqa: BLE001 - never let this break the monitor
        return None


def give_back_rule_stale(server_id: int) -> bool:
    try:
        from app.league import give_back_rule_stale as stale
        return stale(server_id)
    except Exception:   # noqa: BLE001
        return False


def give_back_rule_of(server_id: int) -> dict | None:
    """The same rule, read from the database when the cache is stale: only from a request thread."""
    try:
        from app.league import give_back_rule
        return give_back_rule(server_id)
    except Exception:   # noqa: BLE001
        log.warning("give-back rule of server %s could not be read", server_id, exc_info=True)
        return None


def on_excursion(server_id: int, session: object, car_id: int, ms: int, lap: int, spline: float, now: float | None = None) -> None:
    """The game of a car reported an excursion off the track (stewards/report): it goes to the advantage judge when there is one (a race with the rule on)."""
    if (session or {}).get("session_type") != RACE_TYPE:
        return
    rc = _states.get(server_id)
    if rc and rc.session_ref is session:
        rc.give_back_report(car_id, time.time() if now is None else now, ms, lap, spline)


def on_cuts(server_id: int, session: object, car_id: int, n: int, from_client: bool = False) -> None:
    """CSP reports count in driving sessions; only races apply the penalty ladder."""
    if (from_client and n <= 0) or not isinstance(session, dict) or session.get("session_type") not in (1, 2, RACE_TYPE):
        return
    rc = state_of(server_id, True, session, create=True)
    if rc and not from_client:
        # This ingress closes a lap; add_cuts(0) also serves monitor reconciliation.
        with _lock:
            rc._pit_lap.discard(car_id)
        # Native cuts will only be recorded in a future shadow mode; they never count.
        return
    if rc and car_id != rc.pace_car:
        from app import supervisor   # supervisor owns the ACSP driver identities
        inst = supervisor.get(server_id)
        guid = ((inst.acsp.cars.get(car_id) or {}).get("driver_guid") or "") if inst else ""
        rc.add_cuts(car_id, n, from_client, cut_rule_peek(server_id), str(guid))


MAX_CUT_DUES_PENDING = 10000
MAX_NEUTRAL_DUES_PENDING = 500
_neutral_dues_pending: dict[tuple, dict] = {}   # the time penalties of VSC / FCY waiting to be written: protected by _lock, retained across session changes


def pending_neutral_dues() -> list[dict]:
    with _lock:
        return [dict(row) for row in _neutral_dues_pending.values()]


def acknowledge_neutral_dues(rows: list[dict]) -> None:
    with _lock:
        for r in rows:
            _neutral_dues_pending.pop((r["server_id"], r["flag_id"], r["car"], r["generation"], r["level"]), None)


_cut_dues_pending: dict[tuple, dict] = {}   # protected by _lock, retained across session changes


def pending_cut_dues() -> list[dict]:
    with _lock:
        return [dict(row) for row in _cut_dues_pending.values()]


from app.rulebook import Detectors   # noqa: E402  (after the module's own names: rulebook imports nothing of this module)

FLAG_DEFAULTS = {k: getattr(Detectors(), k).model_dump() for k in ("blue", "yellow", "meatball", "black", "red")}   # what a server with no applied rulebook uses
MAX_FLAG_DUES_PENDING = 2000
DAMAGE_FRESH_S = 30.0   # s a damage report counts (the hub repeats it while the car is damaged)
_flag_dues_pending: dict[tuple, dict] = {}   # sanctions for ignored flags waiting to be written (protected by _lock, retained across session changes)
_flag_served: set[tuple] = set()   # keys of flag drive-throughs served, waiting to be written


def pending_flag_dues() -> list[dict]:
    with _lock:
        return [dict(row) for row in _flag_dues_pending.values()]


def pending_flag_served() -> set[tuple]:
    with _lock:
        return set(_flag_served)


def acknowledge_flag(rows: list[dict], served: set[tuple]) -> None:
    with _lock:
        for row in rows:
            _flag_dues_pending.pop((row["server_id"], row["session_key"], row["driver_guid"], row["kind"], row["seq"]), None)
        _flag_served.difference_update(served)


MAX_DRS_DUES_PENDING = 2000
_drs_dues_pending: dict[tuple, dict] = {}   # DRS offences waiting to be written (protected by _lock, retained across session changes)
_drs_served: set[tuple] = set()   # keys of DRS drive-throughs served, waiting to be written


def pending_drs_dues() -> list[dict]:
    with _lock:
        return [dict(row) for row in _drs_dues_pending.values()]


def pending_drs_served() -> set[tuple]:
    with _lock:
        return set(_drs_served)


def acknowledge_drs(rows: list[dict], served: set[tuple]) -> None:
    with _lock:
        for row in rows:
            _drs_dues_pending.pop((row["server_id"], row["session_key"], row["driver_guid"], row["period"]), None)
        _drs_served.difference_update(served)


_cut_served: set[tuple] = set()   # keys of drive-through dues served, waiting to be written (protected by _lock; outlives the RaceControl that saw the pit pass)


def pending_cut_served() -> set[tuple]:
    with _lock:
        return set(_cut_served)


def acknowledge_cut_served(keys: set[tuple]) -> None:
    with _lock:
        _cut_served.difference_update(keys)


def acknowledge_cut_dues(rows: list[dict]) -> None:
    with _lock:
        for row in rows:
            _cut_dues_pending.pop((row["server_id"], row["session_key"], row["driver_guid"], row["round"]), None)


_states: dict[int, RaceControl] = {}


def state_of(server_id: int, running: bool, session_ref: object = None, create: bool = False) -> RaceControl | None:
    """The state of a server: forgotten when it is not running or the session changed (a new RaceControl starts green, with `rev` one ahead so clients notice the reset)."""
    with _lock:
        rc = _states.get(server_id)
        if rc and (not running or rc.session_ref is not session_ref):
            rc.audit("session_end", flag=rc.flag, running=running, executed=True)
            _old = _states.pop(server_id, None)
            rc = None
            if running and session_ref is not None:   # a session change: tell the hub clients (rev moves on) that the old flag is gone
                fresh = RaceControl(session_ref)
                fresh.server_id = server_id
                fresh.rev = 1
                old = _old
                fresh.auto_yellow, fresh.pace_car, fresh.pace_car_kmh, fresh.pace_guid = old.auto_yellow, old.pace_car, old.pace_car_kmh, old.pace_guid
                fresh.auto_off = set(old.auto_off)   # (the director's choices outlive the session)
                _states[server_id] = fresh
                fresh.audit("session_start", flag=fresh.flag, previous_flag=old.flag, executed=True)
                return fresh
        if rc is None and create and running:
            rc = _states[server_id] = RaceControl(session_ref)
            rc.server_id = server_id
            rc.audit("session_start", flag=rc.flag, executed=True)
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
        if not rc or rc.flag not in SLOWING or rc.evaluated or not rc.legacy:   # (the continuous judge, app/live/pacejudge.py, decides; this single judgement is only for a flag that started with the director's kill switch `pace` on)
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
