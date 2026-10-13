"""Brake on POST /auth/login: attempts are counted in memory, per (address, user) and per address, over a sliding window.

- An attempt is RECORDED when it is checked (one step under one lock), and a success takes it back. So parallel guesses cannot all slip through before the first
  failure is written: the 6th attempt of a burst is refused whatever the others are still doing.
- 5 attempts of one user from one address within `window` seconds block THAT PAIR until the oldest one expires (a real admin typing from another address is not
  locked out by somebody else's guesses: no account DoS). 30 attempts from one address (any users) block that address: one machine spraying many names.
- The username is normalized (case, spaces) so `Root` and `root ` are one.
The manager is ONE process (like the supervisor), so a process table is enough; a restart forgets it and a second process would need a shared store.
Memory is bounded: expired entries go on every access and the tables never pass `max_entries`; when they must shed, entries that are NOT blocked go first."""
import threading
import time


class LoginLimiter:
    def __init__(self, per_pair: int = 5, per_ip: int = 30, window: float = 300.0, max_entries: int = 10_000) -> None:
        self.per_pair, self.per_ip, self.window, self.max_entries = per_pair, per_ip, window, max_entries
        self._lock = threading.Lock()
        self._pair: dict[tuple[str, str], list[float]] = {}
        self._ip: dict[str, list[float]] = {}

    @staticmethod
    def _user(name: str) -> str:
        return (name or "").strip().lower()[:100]

    def _recent(self, table: dict, key, now: float) -> list[float]:
        times = [t for t in table.get(key, ()) if now - t < self.window]
        if times:
            table[key] = times
        else:
            table.pop(key, None)
        return times

    def attempt(self, ip: str, user: str, now: float | None = None) -> int:
        """0 = go ahead, and this attempt is now counted; otherwise the seconds to wait (the attempt is NOT counted)."""
        now = time.monotonic() if now is None else now
        pair_key = (ip, self._user(user))
        with self._lock:
            waits = []
            for table, key, limit in ((self._pair, pair_key, self.per_pair), (self._ip, ip, self.per_ip)):
                times = self._recent(table, key, now)
                if len(times) >= limit:
                    waits.append(times[len(times) - limit] + self.window - now)   # when enough of the oldest ones have expired to fall under the limit
            if waits:
                return int(max(waits)) + 1
            self._pair.setdefault(pair_key, []).append(now)
            self._ip.setdefault(ip, []).append(now)
            self._shed(now)
            return 0

    def refund(self, ip: str, user: str) -> None:
        """Take back the LAST counted attempt of this pair: the check never got to judge the password (a database error, say), so it must not weigh on the user."""
        with self._lock:
            times = self._pair.get((ip, self._user(user)))
            if not times:
                return
            t = times.pop()
            if not times:
                self._pair.pop((ip, self._user(user)), None)
            ip_times = self._ip.get(ip, [])
            if t in ip_times:
                ip_times.remove(t)
            if not ip_times:
                self._ip.pop(ip, None)

    def succeeded(self, ip: str, user: str) -> None:
        """A good login gives its attempts (and the pair's earlier ones) back, also to the address total."""
        with self._lock:
            for t in self._pair.pop((ip, self._user(user)), ()):
                times = self._ip.get(ip, [])
                if t in times:
                    times.remove(t)
                if not times:
                    self._ip.pop(ip, None)

    def _shed(self, now: float) -> None:
        for table, limit in ((self._pair, self.per_pair), (self._ip, self.per_ip)):
            if len(table) <= self.max_entries:
                continue
            for key in list(table):
                self._recent(table, key, now)
            if len(table) > self.max_entries:   # a flood of distinct keys: drop what is NOT blocked first (oldest first), blocked ones last
                order = sorted(table, key=lambda k: (len(table[k]) >= limit, table[k][-1]))
                for key in order[:len(table) - self.max_entries]:
                    table.pop(key, None)
