"""DRS rules of a race (pure: no clock, no database, no locks: `RaceControl` calls it under its own lock).

DRS is OFF (and a car that uses it is sanctioned) while any of these holds, the first one that applies being the reason shown to the driver:
  * "flag"    the race is not under green or white (caution, VSC, FCY, red flag, pace car);
  * "rain"    the track is wet (`rain_disables`): the weather signal (max of rain and wetness, 0..1) went over `wet_on`; it is allowed again only after the signal stays under `dry_below`
              for `dry_confirm_s` (a shower that stops and starts again does not flicker the banner);
  * "start"   the leader has not completed `start_laps_off` laps yet;
  * "restart" the leader has not completed `restart_laps_off` laps since the race went back to green.
Each contiguous OFF period is a `window`: a driver is sanctioned at most once per window (the sanction is a drive-through or a warning, see `RaceControl.drs_use`).
The hub reads the state; the manager is the one that decides (the hub's report of a car using DRS is client evidence, like the cut counter).
"""
from __future__ import annotations

GRACE_S = 3.0   # s after DRS goes off during which a use is not sanctioned (the hub learns it by polling, about once a second)


class Drs:
    def __init__(self, rule: dict) -> None:
        self.rule = rule
        self.why: str | None = "start" if rule["start_laps_off"] > 0 else None
        self.on = self.why is None
        self.until: int | None = rule["start_laps_off"] or None   # the leader's completed laps from which DRS is allowed (None: no lap condition holds it)
        self.window = 0 if self.on else 1   # the number of OFF periods so far (the current one while `on` is False): a race with a DRS-free start begins inside the first
        self.off_since = -1e9      # when the current OFF period began (the start needs no grace)
        self._neutral = False      # the race was not green since the last restart was counted
        self._restart_until = 0
        self._wet = False
        self._dry_since: float | None = None
        self._punished: set[tuple[str, int]] = set()   # (driver, window): already sanctioned

    def step(self, now: float, laps: int, racing: bool, wet_signal: float | None) -> bool:
        """Advance with the leader's completed laps, whether the flag is green/white and the weather signal (None: unknown, treated as dry). True when `on` or `why` changed."""
        r = self.rule
        if not racing:
            self._neutral = True
        elif self._neutral:   # back to green: the restart's DRS-free laps count from here
            self._neutral, self._restart_until = False, laps + r["restart_laps_off"]
        if r["rain_disables"] and wet_signal is not None:
            if wet_signal >= r["wet_on"]:
                self._wet, self._dry_since = True, None
            elif self._wet and wet_signal < r["dry_below"]:
                self._dry_since = now if self._dry_since is None else self._dry_since
                if now - self._dry_since >= r["dry_confirm_s"]:
                    self._wet, self._dry_since = False, None
            else:
                self._dry_since = None   # (in between: still wet, the dry countdown starts over)
        if not racing:
            why, until = "flag", None
        elif self._wet and r["rain_disables"]:
            why, until = "rain", None
        elif laps < r["start_laps_off"]:
            why, until = "start", r["start_laps_off"]
        elif laps < self._restart_until:
            why, until = "restart", self._restart_until
        else:
            why, until = None, None
        was = (self.on, self.why)
        self.on, self.why, self.until = why is None, why, until
        if was[0] and not self.on:
            self.window, self.off_since = self.window + 1, now
        return was != (self.on, self.why)

    def used(self, driver: str, now: float) -> bool:
        """A car was seen using DRS: True when it is a new offence (DRS is off, the grace is over, and this driver was not sanctioned in this window yet)."""
        if self.on or now - self.off_since < GRACE_S or (driver, self.window) in self._punished:
            return False
        self._punished.add((driver, self.window))
        return True

    def view(self) -> dict:
        return {"on": self.on, "why": self.why, "until": self.until}
