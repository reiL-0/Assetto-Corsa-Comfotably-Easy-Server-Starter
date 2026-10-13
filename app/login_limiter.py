"""Brake on POST /auth/login: failed attempts are counted in memory, per (address, user) and per address, over a sliding window.

- 5 failures of one user from one address within `window` seconds block THAT PAIR until the oldest failure expires (a real admin typing from another address
  is not locked out by somebody else's guesses: no account DoS).
- 30 failures from one address (any users) block that address: one machine spraying many names.
- A success clears the pair. The username is normalized (case, spaces) so `Root` and `root ` are one.
The manager is ONE process (like the supervisor), so a process table is enough; a restart forgets it and a second process would need a shared store.
Memory is bounded: expired entries are dropped on every access and the tables never grow past `max_entries`."""
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

    def retry_after(self, ip: str, user: str, now: float | None = None) -> int:
        """Seconds until this attempt may be made (0: go ahead)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            waits = []
            for table, key, limit in ((self._pair, (ip, self._user(user)), self.per_pair), (self._ip, ip, self.per_ip)):
                times = self._recent(table, key, now)
                if len(times) >= limit:
                    waits.append(times[len(times) - limit] + self.window - now)   # when enough of the oldest ones have expired to fall under the limit
            return int(max(waits)) + 1 if waits else 0

    def failed(self, ip: str, user: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._pair.setdefault((ip, self._user(user)), []).append(now)
            self._ip.setdefault(ip, []).append(now)
            for table in (self._pair, self._ip):
                if len(table) > self.max_entries:
                    for key in list(table):
                        self._recent(table, key, now)
                    if len(table) > self.max_entries:   # all of them recent: a flood from many addresses; forgetting is better than growing
                        table.clear()
                        table.setdefault(ip if table is self._ip else (ip, self._user(user)), []).append(now)

    def succeeded(self, ip: str, user: str) -> None:
        with self._lock:
            self._pair.pop((ip, self._user(user)), None)
