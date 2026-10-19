"""NASCAR rules for a race the manager runs by itself: stages, the green finish and overtime. Plain state, no I/O (the wiring to ACSP, the hub and Control AC comes later).

The race is TIMED and long in acServer (its clock is ignored); the manager counts the LEADER's laps and decides when things end. The engine eats two kinds of events and answers with actions:

  on_flag(flag)                the director (or the engine itself) changed the flag
  on_leader_lap(laps)          the leader completed lap `laps`
(the flag only comes in through on_flag and the lap only through on_leader_lap: with two sources the order in which the events arrive would change the result)
  -> [("flag", "yellow"|"checkered"), ("say", text), ("finish",)]  for the caller to carry out

Rules (the owner's):
  * The distance is `total` laps, split into stages by percentages (30/45/25 by default, so 70 laps end stage 1 at lap 21 and stage 2 at lap 53) or by explicit laps. `set_total` changes the
    distance while racing; stages that are not over yet are recomputed.
  * A stage ends at its lap if the flag is green; if a CAUTION (yellow, vsc, fcy) comes out within the last `window` laps of the stage (10 laps, Cup 2026; fewer if the stage is shorter) the stage ends right there under caution. Then a stage break (a yellow if there was none) until the director says green.
  * The final stage ends when the leader completes `total`: green -> chequered (DRAINING: every car finishes its own last lap, then the caller calls `done`). A caution -> OVERTIME: wait for
    green, then `ot_laps` (2) laps; a caution before the end of those laps, or one at their end, is another attempt, with no limit (Cup 2026) unless `ot_max` is given; when they are spent the race ends.
  * A caution is counted when the flag goes from green to any caution (yellow -> fcy is the same caution).
  * The WHITE flag comes out by itself when the leader completes the lap before the last one (the race, or an overtime attempt) under green; if that lap was run under caution it comes out with the
    green flag that follows. The stage warnings ("5 laps to go") are plain `say` actions.
Everything lives in memory: a manager restart in the middle of a race loses it (recovery by hand in this first version).
"""

from __future__ import annotations

import math

CAUTION = ("yellow", "vsc", "fcy")
RACING, BREAK, OT_WAIT, OT_RUN, DRAINING, DONE = "racing", "break", "ot_wait", "ot_run", "draining", "done"
STAGES_DEFAULT = (30, 45, 25)
MAX_TOTAL = 1000
MAX_SHARES = 6
MAX_SHARE = 1000
WARN_LAPS = (5, 3, 1)   # "laps to go" notices before the end of a stage that is not the last
WINDOW_LAPS = 10   # Cup 2026: a caution in the last 10 laps of a stage (or fewer, if the stage is shorter) ends it right there


def stage_ends(total: int, shares: tuple[float, ...] = STAGES_DEFAULT) -> list[int]:
    """Cumulative end lap of every stage from the shares (the last one is always `total`)."""
    whole, acc, out = sum(shares), 0.0, []
    for s in shares[:-1]:
        acc += s
        out.append(min(total - 1, max(1, math.floor(total * acc / whole + 0.5))))
    return out + [total]


class Nascar:
    def __init__(self, total: int, shares: tuple[float, ...] = STAGES_DEFAULT, ends: list[int] | None = None, ot_laps: int = 2, ot_max: int | None = None) -> None:
        if type(total) is not int or not 2 <= total <= MAX_TOTAL:
            raise ValueError(f"total must be an integer between 2 and {MAX_TOTAL}")
        shares = tuple(shares)
        if not shares or len(shares) > MAX_SHARES or any(not 0 < s <= MAX_SHARE or not math.isfinite(s) for s in shares):
            raise ValueError(f"shares must contain 1 to {MAX_SHARES} finite weights greater than 0 and at most {MAX_SHARE}")
        if ends and (list(ends) != sorted(set(ends)) or ends[-1] != total or ends[0] < 1):
            raise ValueError("a race needs at least 2 laps and stage ends that rise up to the total")
        self.total, self.shares, self.fixed_ends = total, shares, list(ends) if ends else None
        self.ends = self.fixed_ends or stage_ends(total, self.shares)
        self.ot_laps, self.ot_max = ot_laps, ot_max
        self.phase, self.stage, self.laps, self.flag = RACING, 0, 0, "green"
        self.cautions_in_stage, self.attempts, self.ot_end = 0, 0, 0

    # ---- helpers ----------------------------------------------------------------------------------------------------------------------------------------------------
    def stage_start(self) -> int:
        return self.ends[self.stage - 1] if self.stage else 0

    def window(self) -> int:
        """Laps before the end of the current stage in which a caution ends it right there."""
        return min(WINDOW_LAPS, self.ends[self.stage] - self.stage_start())

    def set_total(self, total: int) -> list[tuple]:
        """Change the distance while racing (stages already over keep their end, also the one of a break that has just started). It cannot go under the laps already run."""
        if type(total) is not int or not 2 <= total <= MAX_TOTAL:
            raise ValueError(f"total must be an integer between 2 and {MAX_TOTAL}")
        laps = self.laps
        if total <= laps or self.phase in (DRAINING, DONE):
            raise ValueError("the distance must be over the laps already run and the race must still be on")
        if self.fixed_ends:
            raise ValueError("explicit stage ends: change them with the stages, not the distance")
        done = self.ends[:self.stage + (1 if self.phase == BREAK else 0)]
        fresh = stage_ends(total, self.shares)
        self.ends = done + [max(e, laps + 1) if e < total else total for e in fresh[len(done):]]
        self.ends[-1] = total
        self.total = total
        if self.flag == "white" and self.phase == RACING and self.laps < total - 1:   # the distance grew after the white flag: it is not the last lap any more
            self.flag = "green"
            return [("say", "Distancia ampliada: sigue la carrera"), ("flag", "green")]
        return self._white_if_due() if self.phase == RACING else []   # ... or it shrank to the lap before the last

    # ---- events -------------------------------------------------------------------------------------------------------------------------------------------------------
    def on_flag(self, flag: str) -> list[tuple]:
        before, self.flag = self.flag, flag
        out: list[tuple] = []
        was_caution, is_caution = before in CAUTION, flag in CAUTION
        if is_caution and not was_caution:
            if self.phase == RACING:
                self.cautions_in_stage += 1
                if self.stage < len(self.ends) - 1 and self.ends[self.stage] - self.laps <= self.window():
                    out += self._end_stage(f"AMARILLA: el stage {self.stage + 1} termina aquí")
            elif self.phase == OT_RUN:
                out += self._ot_failed("amarilla en el overtime")
        elif flag == "green" and was_caution:
            if self.phase == RACING:
                out += self._white_if_due()   # the lap before the last was run under caution: the white flag comes with the green
            elif self.phase == BREAK:
                self.stage += 1
                self.phase, self.cautions_in_stage = RACING, 0
                out.append(("say", f"STAGE {self.stage + 1}: bandera verde"))
                out += self._white_if_due()
            elif self.phase == OT_WAIT:
                self.attempts += 1
                self.phase, self.ot_end = OT_RUN, self.laps + self.ot_laps
                out.append(("say", f"OVERTIME: intento {self.attempts}, {self.ot_laps} vueltas" if self.ot_max is None else f"OVERTIME: intento {self.attempts} de {self.ot_max}, {self.ot_laps} vueltas"))
                out += self._white_if_due()   # (a one-lap overtime would start with the white flag)
        return out

    def _white_if_due(self) -> list[tuple]:
        """The white flag: the leader has completed the lap before the last one and the field is under green."""
        last_lap = self.ot_end if self.phase == OT_RUN else self.total if self.phase == RACING and self.stage == len(self.ends) - 1 else 0
        if not last_lap or self.laps != last_lap - 1 or self.flag in CAUTION or self.flag == "white":
            return []
        self.flag = "white"
        return [("say", "BANDERA BLANCA: última vuelta"), ("flag", "white")]

    def on_leader_lap(self, laps: int) -> list[tuple]:
        if laps <= self.laps:
            return []   # a repeated or late event
        self.laps = laps
        caution = self.flag in CAUTION
        if self.phase == RACING:
            last = self.stage == len(self.ends) - 1
            if laps >= self.ends[self.stage]:
                if not last:
                    return self._end_stage(f"FIN DEL STAGE {self.stage + 1}")
                if not caution:
                    return self._chequered("BANDERA A CUADROS")
                self.phase = OT_WAIT
                return [("say", f"OVERTIME: la vuelta {self.total} con amarilla; se alarga hasta acabar en verde")]
            left = self.ends[self.stage] - laps
            if not last and not caution and left in WARN_LAPS:
                return [("say", f"STAGE {self.stage + 1}: quedan {left} {'vuelta' if left == 1 else 'vueltas'}")]
            return self._white_if_due()
        if self.phase == OT_RUN:
            if laps >= self.ot_end:
                return self._chequered("BANDERA A CUADROS") if not caution else self._ot_failed("amarilla al final del overtime")
            return self._white_if_due()
        return []

    def done(self) -> None:
        """The caller says every car has finished (or the wait ran out)."""
        if self.phase == DRAINING:
            self.phase = DONE

    # ---- internals ----------------------------------------------------------------------------------------------------------------------------------------------------
    def _end_stage(self, text: str) -> list[tuple]:
        self.phase = BREAK
        out = [("say", text)]
        if self.flag not in CAUTION:
            out.append(("flag", "yellow"))   # the stage-break caution; the flag it brings back is ignored while the break lasts
            self.flag = "yellow"
        return out

    def _chequered(self, text: str) -> list[tuple]:
        self.phase = DRAINING
        self.flag = "checkered"
        return [("say", text), ("flag", "checkered"), ("finish",)]

    def _ot_failed(self, why: str) -> list[tuple]:
        if self.ot_max is not None and self.attempts >= self.ot_max:
            return [("say", f"OVERTIME: {why}; intentos agotados")] + self._chequered("FIN DE LA CARRERA")
        self.phase = OT_WAIT
        return [("say", f"OVERTIME: {why}; nuevo intento cuando haya verde")]

    def snapshot(self) -> dict:
        return {"phase": self.phase, "stage": self.stage + 1, "stages": len(self.ends), "stage_end": self.ends[self.stage], "total": self.total, "laps": self.laps,
                "cautions_in_stage": self.cautions_in_stage, "attempts": self.attempts, "ot_end": self.ot_end if self.phase == OT_RUN else None}
