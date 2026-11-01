"""Advantage gained OFF the track: a car that leaves the track (its own game says so) and comes back in front of a car that was in front of it must give the place back, in time, or it gets a time penalty.

Plain state, no I/O: `RaceControl` feeds it the progress of every car (app/live/field.py: laps + spline, unwrapped) at every tick, the excursions the drivers' own games report (`report`, from
`stewards/report`) and the contacts (`note_contact`); it DECIDES (ids, never names or messages) and the adapter (app/racecontrol.py) turns that into the banner, the state and the penalty.

How a move is judged (the debate of 2026-11-01, see the notes in the module `caution.py` for the same vocabulary):
  * an excursion is `report`ed once it is over (the script waits 0.3 s on the track before saying so, then the web hop: a second or two). Its start is found on the car's OWN history of progress
    (the lap and the spline where it began), so the late report does not matter; its end is that start plus its length.
  * SETTLE_S after it ends the order is read again. A car B is OWED the place when, before the excursion, it was ahead of the car A and close (within CLOSE of a lap), and now A is ahead of it by more than
    PASS_MARGIN having gained at least GAIN_MIN of a lap on it (A went off; the cars that passed A on the track, or A getting back a place it had, gain nothing for A and are no case).
    Only the cars A passed while it was out are owed: a line of cars that go by a car that went wide is no cascade, they are not A's creditors.
  * not a case: B stopped (STOPPED_KMH) or in the pit lane, A in the pit lane, a car that left the server, a caution (the no-passing judgement of caution.py is the one that rules then), and any
    contact between the two around the move (CONTACT_S before it starts to just after it ends): who forced whom is for the director (`reviews`).
  * the obligation lasts `window_s` seconds. It is met when A is behind the creditor by more than PASS_MARGIN for BACK_HOLD s; a creditor that stops, pits or leaves releases A of it. At the end of the
    time the creditors that remain make a `due` event: the penalty (once per obligation, whatever the number of creditors).
"""

from __future__ import annotations

import itertools
from collections import deque


CLOSE = 0.05          # of a lap: two cars further apart than this are not compared
PASS_MARGIN = 0.005   # of a lap: in front or behind by more than this, not side by side
GAIN_MIN = 0.004      # of a lap: what A has to have gained on B (about 16 m on a 4 km track) to be owing the place
SETTLE_S = 2.0        # s after the excursion ends before the order is read (the cars sort themselves out)
STOPPED_KMH = 25.0    # B this slow was not defending a place: nothing to give back
CONTACT_S = 3.0       # s: a contact between the two this close to the move makes it a matter for the director
BACK_HOLD = 2.0       # s behind the creditor that count as having given the place back
HISTORY_S = 60.0      # s of history kept per car
DEFAULT_WINDOW_S = 30.0


_IDS = itertools.count(1)   # (one counter for the whole process: an id is never reused when a detector is made again, so nobody can mistake a new obligation for an old one)


class GiveBack:
    def __init__(self, window_s: float = DEFAULT_WINDOW_S) -> None:
        self.window_s = window_s
        self._hist: dict[int, deque] = {}            # car -> deque of (t, progress, km/h)
        self._contacts: deque = deque(maxlen=200)    # (t, a, b)
        self._pending: list[dict] = []               # excursions reported and not read yet
        self._open: dict[int, dict] = {}             # car (A) -> its obligation {id, car, creditors: {B: back_since | None}, t0, due}
        self.reviews: list[dict] = []                # {car, others, why}: for the director, nobody is punished by the automatic judge

    # ---- what it is fed ----------------------------------------------------------------------------------------------------------------------------------------
    def observe(self, now: float, progress: dict[int, float], speeds: dict[int, float]) -> None:
        for car, p in progress.items():
            h = self._hist.setdefault(car, deque())
            h.append((now, p, speeds.get(car, 0.0)))
            while h and now - h[0][0] > HISTORY_S:
                h.popleft()

    def note_contact(self, now: float, a: int, b: int | None) -> None:
        if b is not None:
            self._contacts.append((now, a, b))

    def report(self, car: int, now: float, ms: int, lap: int, spline: float) -> None:
        """The game of `car` says it was off the track for `ms` milliseconds, starting at `spline` of lap `lap` (what its script saw). Read SETTLE_S after it ended."""
        t_out = self._time_at(car, lap + spline)
        if t_out is None:   # (the history does not reach back to it: count back from the report: the 0.3 s of grace and a second of web)
            t_out = now - ms / 1000.0 - 1.3
        t_in = t_out + ms / 1000.0
        self._pending.append({"car": car, "t_out": t_out, "t_in": t_in, "read_at": max(now, t_in + SETTLE_S)})

    # ---- the history ---------------------------------------------------------------------------------------------------------------------------------------------
    def _time_at(self, car: int, progress: float) -> float | None:
        """When `car` was at this progress (the first time, interpolated); None when its history does not cover it."""
        h = list(self._hist.get(car, ()))
        for (t0, p0, _), (t1, p1, _) in zip(h, h[1:]):
            if p0 <= progress <= p1 and p1 > p0:
                return t0 + (t1 - t0) * (progress - p0) / (p1 - p0)
        return None

    def _at(self, car: int, t: float) -> tuple[float, float] | None:
        """(progress, km/h) of `car` at time `t`, interpolated; None when its history does not cover that time."""
        h = self._hist.get(car)
        if not h or t < h[0][0] or t > h[-1][0]:
            return None
        prev = h[0]
        for cur in h:
            if cur[0] >= t:
                if cur[0] == prev[0]:
                    return cur[1], cur[2]
                k = (t - prev[0]) / (cur[0] - prev[0])
                return prev[1] + (cur[1] - prev[1]) * k, prev[2] + (cur[2] - prev[2]) * k
            prev = cur
        return None

    def _contact_between(self, a: int, b: int, t0: float, t1: float) -> bool:
        return any(((x == a and y == b) or (x == b and y == a)) and t0 - CONTACT_S <= t <= t1 + CONTACT_S for t, x, y in self._contacts)

    # ---- one look ------------------------------------------------------------------------------------------------------------------------------------------------
    def step(self, now: float, progress: dict[int, float], speeds: dict[int, float], exempt=(), caution: bool = False) -> list[tuple]:
        """-> events: ("open", obligation), ("cleared", obligation, why) with why 'returned' | 'released' | 'suspended', ("due", obligation). `exempt` = cars in the pit lane."""
        self.observe(now, progress, speeds)
        events: list[tuple] = []
        if caution:   # the no-passing judgement of the caution rules while it lasts: what was owed is forgotten, and what happened under it is not a case
            for ob in self._open.values():
                events.append(("cleared", ob, "suspended"))
            self._open.clear()
            self._pending.clear()
            return events
        for exc in [e for e in self._pending if e["read_at"] <= now]:
            self._pending.remove(exc)
            ob = self._read(exc, now, progress, speeds, exempt)
            if ob:
                events.append(("open", ob))
        for car, ob in list(self._open.items()):
            if car not in progress or car in exempt:   # (it left the server, or it went into the pits: no place to give back any more)
                events.append(("cleared", ob, "released"))
                del self._open[car]
                continue
            for b in list(ob["creditors"]):
                if b not in progress or b in exempt or speeds.get(b, 0.0) <= STOPPED_KMH:   # (gone, in the pits or stopped: nothing to give back to it)
                    del ob["creditors"][b]
                    continue
                s = progress[car] - progress[b]
                if s < -PASS_MARGIN:   # behind it again: for long enough?
                    ob["creditors"][b] = now if ob["creditors"][b] is None else ob["creditors"][b]
                    if now - ob["creditors"][b] >= BACK_HOLD:
                        del ob["creditors"][b]   # the place was given back
                        ob["met"] = True
                else:
                    ob["creditors"][b] = None
            if not ob["creditors"]:
                events.append(("cleared", ob, "returned" if ob["met"] else "released"))
                del self._open[car]
            elif now >= ob["due"]:
                events.append(("due", ob))
                del self._open[car]
        return events

    def _read(self, exc: dict, now: float, progress: dict[int, float], speeds: dict[int, float], exempt) -> dict | None:
        a = exc["car"]
        if a not in progress or a in exempt:
            return None
        before = self._at(a, exc["t_out"])
        if before is None:
            return None
        t_eval = min(now, exc["t_in"] + SETTLE_S)
        owed: list[int] = []
        for b in progress:
            if b == a or b in exempt:
                continue
            was = self._at(b, exc["t_out"])
            if was is None:
                continue
            s_out = before[0] - was[0]   # A relative to B when it left the track: it has to be behind and close, on the same lap (the progress does not wrap: a car a lap down is no place to gain)
            if not -CLOSE <= s_out < -PASS_MARGIN or was[1] <= STOPPED_KMH:
                continue
            at_a, at_b = self._at(a, t_eval), self._at(b, t_eval)   # the order right after the excursion (a late report is judged on what happened THEN, not on a pass that came later on the track)
            s_then = (at_a[0] - at_b[0]) if at_a and at_b else progress[a] - progress[b]
            s_now = progress[a] - progress[b]
            if s_then <= PASS_MARGIN or s_then - s_out < GAIN_MIN or s_now <= PASS_MARGIN:   # (not in front then, no real gain, or already behind again)
                continue
            if self._contact_between(a, b, exc["t_out"], exc["t_in"]):
                self.reviews.append({"car": a, "others": [b], "why": "contact"})
                self.reviews = self.reviews[-20:]
                continue
            owed.append(b)
        if not owed:
            return None
        if a in self._open:   # (a second excursion before the first was settled: its creditors are added, the time is not extended)
            for b in owed:
                self._open[a]["creditors"].setdefault(b, None)
            return None
        ob = {"id": next(_IDS), "car": a, "creditors": {b: None for b in owed}, "t0": now, "due": now + self.window_s, "met": False}
        self._open[a] = ob
        return ob

    # ---- what it says ---------------------------------------------------------------------------------------------------------------------------------------------
    def open_obligations(self, now: float) -> list[dict]:
        """The obligations that are open: {id, car, creditors, left_s}."""
        return [{"id": ob["id"], "car": ob["car"], "creditors": sorted(ob["creditors"]), "left_s": max(0, int(ob["due"] - now + 0.999))} for ob in self._open.values()]

    def forgive(self, ob_id: int) -> bool:
        """The director forgives an open obligation: True when it was open."""
        for car, ob in list(self._open.items()):
            if ob["id"] == ob_id:
                del self._open[car]
                return True
        return False

    def restore(self, ob: dict) -> None:
        """An obligation taken out by `due_now` whose penalty could not be written: back in, with the time it had."""
        now = self._open.get(ob["car"])
        if now is None:
            self._open[ob["car"]] = ob
        else:   # (another one was opened for that car meanwhile: they become one, with all the creditors and the shorter time)
            for b, since in ob["creditors"].items():
                now["creditors"].setdefault(b, since)
            now["due"] = min(now["due"], ob["due"])

    def due_now(self, ob_id: int) -> dict | None:
        """The director sanctions an open obligation now: it is closed and returned for the caller to penalise; None when it was not open."""
        for car, ob in list(self._open.items()):
            if ob["id"] == ob_id:
                del self._open[car]
                return ob
        return None

    def clear(self) -> None:
        self._hist.clear()
        self._contacts.clear()
        self._pending.clear()
        self._open.clear()
