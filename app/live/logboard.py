"""A live leaderboard read from acServer's own log, for when the ACSP plugin socket is not connected.

acServer prints its leaderboard after every lap (`SendLapCompletedMessage`, then one `N) name BEST: m:ss:ms TOTAL: h:mm:ss:ms Laps:n
SesID:i HasFinished:b` line per car slot), who sits in which slot (`Dispatching TCP message to <model> (<slot>) [<name> []]`, with the
Steam ID on the `Looking for available slot ... GUID` line just before) and the session it is in (`SENDING session name/type/time/laps`,
`NextSession`). `LogBoard.feed` takes those lines (`supervisor.Instance._tail` calls it for every new line) and keeps a `LiveBoard`, the
same structure ACSP fills, so `app/live/acsm.py` serves the same JSON from either source. It knows less than ACSP: no positions on
track, no top speed and no last lap time.
"""

from __future__ import annotations

import re

from app.live.board import Driver, LiveBoard

_SLOT = re.compile(r"^Dispatching TCP message to (\S+) \((\d+)\) \[(.*?) ?\[\]\]")
_GUID = re.compile(r"^Looking for available slot by name for GUID (\d{17}) ")
_ROW = re.compile(r"^(\d+)\) (.*?) ?BEST: (\d+):(\d+):(\d+) TOTAL: (\d+):(\d+):(\d+) Laps:(\d+) SesID:(\d+) HasFinished:(\w+)$")
_SESSION = re.compile(r"^SENDING session (name|type|time|laps) : (.*)$")


def _ms(m: str, s: str, ms: str) -> int:
    return (int(m) * 60 + int(s)) * 1000 + int(ms)


class LogBoard:
    def __init__(self) -> None:
        self.board = LiveBoard()
        self._guid = ""   # the Steam ID seen on the last "looking for a slot" line: it belongs to the next car that shows up
        self._rows: list[tuple[int, str, int, int, int]] = []   # the leaderboard block being read: (slot, name, best, total, laps)

    def feed(self, line: str) -> None:
        if m := _ROW.match(line):
            if m[2]:   # empty slots have no name
                self._rows.append((int(m[10]), m[2], _ms(*m.group(3, 4, 5)), _ms(*m.group(6, 7, 8)), int(m[9])))
            return
        self._commit_rows()
        if line == "SendLapCompletedMessage":
            self._rows = []
        elif m := _GUID.match(line):
            self._guid = m[1]
        elif m := _SLOT.match(line):
            self._seat(int(m[2]), m[3], m[1])
        elif m := _SESSION.match(line):
            key = {"name": "name", "type": "session_type", "time": "time_min", "laps": "laps"}[m[1]]
            self.board.session[key] = m[2].strip() if key == "name" else int(m[2] or 0)
        elif line == "NextSession":   # a new session: everybody starts from zero
            self.board.apply({**self.board.session, "type": "new_session", "elapsed_ms": 0})

    def _seat(self, slot: int, name: str, model: str) -> None:
        d = self.board._by_car(slot)
        if d and d.name == name:
            return
        if d:
            d.connected = False   # somebody else took the slot
        self.board.drivers.append(Driver(car_id=slot, name=name, guid=self._guid, model=model))
        self._guid = ""

    def _commit_rows(self) -> None:
        """A whole leaderboard block was read: it is the truth for who is on the server and for their laps."""
        if not self._rows:
            return
        seen = set()
        for slot, name, best, total, laps in self._rows:
            d = self.board._by_car(slot)
            if not d or d.name != name:
                self._seat(slot, name, d.model if d else "")
                d = self.board._by_car(slot)
            seen.add(slot)
            d.best_ms = best if best < 900_000_000 else 0   # no lap yet prints 16666:39:999
            d.total_ms, d.laps = total, laps
        for d in self.board.drivers:
            if d.connected and d.car_id not in seen:
                d.connected = False
        self._rows = []
