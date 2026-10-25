"""What happens in the field under a caution, NASCAR style: the order is frozen when the yellow comes out, nobody passes, the free pass (lucky dog) and the wave-around, and the order of the restart.

Plain state, no I/O: `RaceControl` makes one `Caution` when the flag goes from green to any caution (with the progress of every car from app/live/field.py) and drops it when the green comes out. It only
DECIDES (ids, never names or messages); the adapter (app/racecontrol.py) turns that into notices and drive-throughs. The rules are the CARS Tour rulebook's (2026-1-3A, 4.10-4.15), which are the
Cup's in the parts that matter here:
  * the order is the progress of every car at the moment of the caution (4.10.3.4); the leader is the first. Cars one lap or more behind the leader are LAPPED.
  * nobody passes: two cars close to each other (within CLOSE of a lap) that swap places for PASS_HOLD seconds, by more than PASS_MARGIN, make the car that is now in front a violator -- once per caution.
    Not a violation: a pass of a car that is (nearly) stopped, any pass by or of a car the caller exempts (pit lane, directed to pass), and the free pass / wave-around cars once they are directed (`allow`).
  * the FREE PASS (4.13) goes to the first lapped car of the frozen order; if that one is not eligible (involved in the caution, was in the pits, under a penalty) there is NO free pass -- it does not go to the next one.
  * the WAVE-AROUND: the lapped cars (but the free pass one) that are physically between the leader and the pace car, which can pass the pace car and unlap themselves.
  * the PITS ARE CLOSED while it lasts (this league's rule, not the rulebook's): a car that enters the pit lane is a violator, unless it was already there when the yellow came out, it is involved in a contact
    (damaged), it has a drive-through to serve (the pit lane is how it is served) or the director let it in (`pit_allow`). One violation per car, on the tail of the restart like a passer.
  * the RESTART ORDER (4.14.5): lead lap cars that did not pit, lead lap cars that pitted, lapped cars, the free pass car, involved cars and wave-around cars, penalty cars.
"""

from __future__ import annotations

from itertools import combinations

from app.live.field import ring

CLOSE = 0.05          # of a lap: two cars further apart than this are not compared
PASS_MARGIN = 0.005   # of a lap: how far in front a car has to be to count as having passed (on a 4 km track: 20 m)
PASS_HOLD = 5.0       # s the swapped order has to last: a car that passes and gives the place back within this time is not a violator
PIT_GRACE = 8.0       # s after a pass is confirmed during which the car that was passed may still report the pit lane (it was heading to the pits, the pack went by): then nobody is judged
SLOW_KMH = 15.0       # the car that is passed is this slow or slower: it is stopped, passing it is not a violation
PACE_GAP = 0.03       # of a lap: where the pace car is in front of the leader when it is not known (the virtual one)


class Caution:
    def __init__(self, progress: dict[int, float], now: float, involved=(), penalised=(), pitted=()) -> None:
        self.t0 = now
        self.p0 = dict(progress)
        self.frozen = sorted(self.p0, key=lambda c: -self.p0[c])
        self.leader = self.frozen[0] if self.frozen else None
        self.involved, self.pitted, self.penalised = set(involved), set(pitted), set(penalised)
        self.allowed: set[int] = set()    # cars directed to pass (free pass, wave-around): not violators
        self.violators: set[int] = set()
        self.pit_ok: set[int] = set(pitted) | set(involved)   # cars that may be in the pit lane: there already when the yellow came out, involved (damaged), or let in by the director
        self.pit_entered: set[int] = set()                    # cars that reported the pit lane without being in pit_ok, not judged yet
        self.pit_broken: set[int] = set()                     # cars already judged for it (once per caution)
        self._pending: dict[tuple[int, int], float] = {}   # (passer, the car it passed) -> when the passer becomes a violator unless the passed car reached the pit lane first
        self.waved: list[int] = []        # the wave-around cars once the restart has been called ("to go")
        self.restart: list[int] = []      # the restart order once it has been called
        self._signs: dict[tuple[int, int], int] = {}
        self._since: dict[tuple[int, int], float] = {}
        self.gone: set[int] = set()       # cars that left the track list during the caution (disconnected): the slot may be somebody else's now, it is never judged again
        self.free_pass = self._pick_free_pass()
        if self.free_pass is not None:
            self.allowed.add(self.free_pass)   # it has to make its way up to the pace car through the line
        self._seed(progress)

    # ---- the order ---------------------------------------------------------------------------------------------------------------------------------------------
    def lapped(self) -> list[int]:
        """Cars a lap or more behind the leader at the moment of the caution, in frozen order."""
        if self.leader is None:
            return []
        top = self.p0[self.leader]
        return [c for c in self.frozen if top - self.p0[c] >= 1.0]

    def _pick_free_pass(self) -> int | None:
        lapped = self.lapped()
        if not lapped:
            return None
        first = lapped[0]
        return None if first in self.involved or first in self.pitted or first in self.penalised else first

    def wave_around(self, progress: dict[int, float], pace_progress: float | None = None) -> list[int]:
        """Lapped cars (not the free pass one, not involved, not pitted) now between the leader and the pace car, nearest the pace car first."""
        if self.leader is None or self.leader not in progress:
            return []
        lead = progress[self.leader]
        gap = ring(pace_progress - lead) if pace_progress is not None else PACE_GAP
        out = []
        for c in self.lapped():
            if c == self.free_pass or c in self.involved or c in self.pitted or c not in progress:
                continue
            ahead = ring(progress[c] - lead)
            if 0 < ahead <= max(gap, PACE_GAP):
                out.append((ahead, c))
        return [c for _, c in sorted(out, reverse=True)]

    def restart_order(self, waved=(), pitted_exit=()) -> list[int]:
        """The restart order (see the module docstring). `pitted_exit` = the cars that pitted, in the order they left the pits (the rest of the pitted go by frozen order)."""
        waved, bad = set(waved), self.penalised | self.violators
        lapped = set(self.lapped())
        exit_rank = {c: i for i, c in enumerate(pitted_exit)}
        lead = [c for c in self.frozen if c not in lapped and c not in self.involved and c not in bad]
        stayed = [c for c in lead if c not in self.pitted]
        pitted = sorted((c for c in lead if c in self.pitted), key=lambda c: exit_rank.get(c, len(exit_rank)))
        down = [c for c in self.frozen if c in lapped and c != self.free_pass and c not in self.involved and c not in waved and c not in bad]
        free = [self.free_pass] if self.free_pass is not None and self.free_pass not in bad else []
        mess = [c for c in self.frozen if c in self.involved and c not in bad] + [c for c in self.frozen if c in waved and c not in self.involved and c not in bad]
        tail = [c for c in self.frozen if c in bad]
        seen: set[int] = set()
        return [c for c in stayed + pitted + down + free + mess + tail if not (c in seen or seen.add(c))]

    def call_restart(self, progress: dict[int, float], pace_progress: float | None = None) -> tuple[list[int], list[int], set[int]]:
        """'To go': the wave-around cars may pass (they are not violators any more) and the restart order is fixed. -> (order, waved, cars that may choose their lane)."""
        self.waved = self.wave_around(progress, pace_progress)
        self.allowed.update(self.waved)
        self.restart = self.restart_order(self.waved)
        bad = self.penalised | self.violators
        choosers = {c for c in self.restart if c not in self.involved and c not in bad and c not in self.waved and c != self.free_pass}
        return self.restart, self.waved, choosers

    def snapshot(self) -> dict:
        return {"leader": self.leader, "free_pass": self.free_pass, "order": self.frozen, "lapped": self.lapped(), "involved": sorted(self.involved), "violators": sorted(self.violators),
                "waved": self.waved, "restart": self.restart, "pit_ok": sorted(self.pit_ok), "pit_broken": sorted(self.pit_broken)}

    # ---- nobody passes -----------------------------------------------------------------------------------------------------------------------------------------
    def _seed(self, progress: dict[int, float]) -> None:
        for a, b in combinations(sorted(self.p0), 2):
            s = ring(progress[a] - progress[b])
            if abs(s) <= CLOSE:
                self._signs[(a, b)] = 1 if s > 0 else -1

    def allow(self, *cars: int) -> None:
        self.allowed.update(cars)

    def remove(self, car: int) -> None:
        """A car that is not a competitor (it was named the pace car during the caution): out of the order, the lapped list and every set. Its free pass is not given to the next one."""
        self.p0.pop(car, None)
        self.frozen = [c for c in self.frozen if c != car]
        for group in (self.involved, self.pitted, self.penalised, self.allowed, self.violators):
            group.discard(car)
        self._pending = {k: t for k, t in self._pending.items() if car not in k}
        self.pit_ok.discard(car); self.pit_entered.discard(car); self.pit_broken.discard(car)
        self.waved, self.restart = [c for c in self.waved if c != car], [c for c in self.restart if c != car]
        if self.free_pass == car:
            self.free_pass = None
        if self.leader == car:
            self.leader = self.frozen[0] if self.frozen else None

    def pit_enter(self, car: int) -> None:
        """The car's hub says it is in the pit lane."""
        if car not in self.pit_ok and car not in self.pit_broken:
            self.pit_entered.add(car)

    def pit_allow(self, car: int, allowed: bool) -> None:
        """The director lets a car into the pit lane (or takes the permission back; a car already judged stays judged)."""
        if allowed:
            self.pit_ok.add(car)
            self.pit_entered.discard(car)
        else:
            self.pit_ok.discard(car)

    def pit_breakers(self, serving=()) -> list[int]:
        """The cars that entered the closed pit lane since the last look (once each). `serving` = cars with a drive-through listed: the pit lane is how they serve it."""
        out = [c for c in sorted(self.pit_entered) if c not in serving and c not in self.gone]
        self.pit_entered.clear()
        self.pit_broken.update(out)
        self.violators.update(out)
        return out

    def void_free_pass(self) -> int | None:
        """The free pass car went to the pits: no free pass (and no permission to pass). Returns that car."""
        car, self.free_pass = self.free_pass, None
        self.allowed.discard(car)
        return car

    def check(self, progress: dict[int, float], speeds: dict[int, float], now: float, exempt=()) -> list[int]:
        """The cars that passed another one now (once each). `speeds` in km/h; `exempt` = cars in the pit lane or otherwise not judged."""
        out: list[int] = []
        self.gone.update(c for c in self.p0 if c not in progress)
        known = sorted(c for c in progress if c in self.p0 and c not in self.gone)
        for key in [k for k in self._signs if k[0] in self.gone or k[1] in self.gone]:
            self._signs.pop(key)
            self._since.pop(key, None)
        for a, b in combinations(known, 2):
            s = ring(progress[a] - progress[b])
            key = (a, b)
            if abs(s) > CLOSE:
                self._signs.pop(key, None)
                self._since.pop(key, None)
                continue
            sign = 1 if s > 0 else -1
            old = self._signs.get(key)
            if old is None:
                self._signs[key] = sign
            elif sign == old or abs(s) < PASS_MARGIN:
                self._since.pop(key, None)
            elif now - self._since.setdefault(key, now) >= PASS_HOLD:
                self._since.pop(key, None)
                self._signs[key] = sign
                passer, passed = (a, b) if s > 0 else (b, a)
                if passer in self.violators or (passer, passed) in self._pending or passer in self.allowed or passer in exempt or passed in exempt:
                    continue
                if speeds.get(passed, 0.0) <= SLOW_KMH:   # (stopped, or its speed is not known: not judged)
                    continue
                self._pending[passer, passed] = now + PIT_GRACE
        for (passer, passed), due in list(self._pending.items()):
            if passer in self.violators or passer in self.gone or passed in self.gone or passer in self.allowed or passer in exempt or passed in exempt:
                del self._pending[passer, passed]   # (the passed car reached the pit lane, or one of them left, or the passer was directed to pass: nobody is judged)
            elif now >= due:
                del self._pending[passer, passed]
                self.violators.add(passer)
                out.append(passer)
        return out
