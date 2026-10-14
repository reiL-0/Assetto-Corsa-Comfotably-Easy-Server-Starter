"""NASCAR rules for a race the manager runs by itself: stages, the green finish and overtime. Plain state, no I/O (the wiring to ACSP, the hub and Control AC comes later).

The race is TIMED and long in acServer (its clock is ignored); the manager counts the LEADER's laps and decides when things end. The engine eats two kinds of events and answers with actions:

  on_flag(flag)                the director (or the engine itself) changed the flag
  on_leader_lap(laps)          the leader completed lap `laps`
(the flag only comes in through on_flag and the lap only through on_leader_lap: with two sources the order in which the events arrive would change the result)
  -> [("flag", "yellow"|"checkered"), ("say", text), ("finish",)]  for the caller to carry out

Rules (the owner's):
  * The distance is `total` laps, split into stages by percentages (30/45/25 by default, so 70 laps end stage 1 at lap 21 and stage 2 at lap 53) or by explicit laps. `set_total` changes the
    distance while racing; stages that are not over yet are recomputed.
  * A stage ends at its lap if the flag is green; if a CAUTION (yellow, vsc, fcy) comes out within the last `window` laps of the stage (2 % of its laps while no caution has been deployed in that
    stage, 1 % once one has; at least one lap) the stage ends right there under caution. Then a stage break (a yellow if there was none) until the director says green.
  * The final stage ends when the leader completes `total`: green -> chequered (DRAINING: every car finishes its own last lap, then the caller calls `done`). A caution -> OVERTIME: wait for
    green, then `ot_laps` (2) laps; a caution before the end of those laps, or one at their end, is another attempt, up to `ot_max` (3); when they are spent the race ends.
  * A caution is counted when the flag goes from green to any caution (yellow -> fcy is the same caution).
Everything lives in memory: a manager restart in the middle of a race loses it (recovery by hand in this first version).
"""

from __future__ import annotations

import math

CAUTION = ("yellow", "vsc", "fcy")
RACING, BREAK, OT_WAIT, OT_RUN, DRAINING, DONE = "racing", "break", "ot_wait", "ot_run", "draining", "done"
STAGES_DEFAULT = (30, 45, 25)
WINDOW_PCT = (2, 1)   # % of the stage's laps: with no caution deployed in it yet / with one or more


def stage_ends(total: int, shares: tuple[float, ...] = STAGES_DEFAULT) -> list[int]:
    """Cumulative end lap of every stage from the shares (the last one is always `total`)."""
    whole, acc, out = sum(shares), 0.0, []
    for s in shares[:-1]:
        acc += s
        out.append(min(total - 1, max(1, math.floor(total * acc / whole + 0.5))))
    return out + [total]


class Nascar:
    def __init__(self, total: int, shares: tuple[float, ...] = STAGES_DEFAULT, ends: list[int] | None = None, ot_laps: int = 2, ot_max: int = 3) -> None:
        if total < 2 or (ends and (list(ends) != sorted(set(ends)) or ends[-1] != total or ends[0] < 1)):
            raise ValueError("a race needs at least 2 laps and stage ends that rise up to the total")
        self.total, self.shares, self.fixed_ends = total, tuple(shares), list(ends) if ends else None
        self.ends = self.fixed_ends or stage_ends(total, self.shares)
        self.ot_laps, self.ot_max = ot_laps, ot_max
        self.phase, self.stage, self.laps, self.flag = RACING, 0, 0, "green"
        self.cautions_in_stage, self.attempts, self.ot_end = 0, 0, 0

    # ---- helpers ----------------------------------------------------------------------------------------------------------------------------------------------------
    def stage_start(self) -> int:
        return self.ends[self.stage - 1] if self.stage else 0

    def window(self) -> int:
        """Laps before the end of the current stage in which a caution ends it right there."""
        length = self.ends[self.stage] - self.stage_start()
        return max(1, math.ceil(length * WINDOW_PCT[1 if self.cautions_in_stage else 0] / 100))

    def set_total(self, total: int) -> None:
        """Change the distance while racing (stages already over keep their end, also the one of a break that has just started). It cannot go under the laps already run."""
        laps = self.laps
        if total <= laps or self.phase in (DRAINING, DONE) or total < 2:
            raise ValueError("the distance must be over the laps already run and the race must still be on")
        if self.fixed_ends:
            raise ValueError("explicit stage ends: change them with the stages, not the distance")
        done = self.ends[:self.stage + (1 if self.phase == BREAK else 0)]
        fresh = stage_ends(total, self.shares)
        self.ends = done + [max(e, laps + 1) if e < total else total for e in fresh[len(done):]]
        self.ends[-1] = total
        self.total = total

    # ---- events -------------------------------------------------------------------------------------------------------------------------------------------------------
    def on_flag(self, flag: str) -> list[tuple]:
        before, self.flag = self.flag, flag
        out: list[tuple] = []
        was_caution, is_caution = before in CAUTION, flag in CAUTION
        if is_caution and not was_caution:
            if self.phase == RACING:
                self.cautions_in_stage += 1
                if self.stage < len(self.ends) - 1 and self.ends[self.stage] - self.laps <= self.window_after_count():
                    out += self._end_stage(f"AMARILLA: el stage {self.stage + 1} termina aquí")
            elif self.phase == OT_RUN:
                out += self._ot_failed("amarilla en el overtime")
        elif flag == "green" and was_caution:
            if self.phase == BREAK:
                self.stage += 1
                self.phase, self.cautions_in_stage = RACING, 0
                out.append(("say", f"STAGE {self.stage + 1}: bandera verde"))
            elif self.phase == OT_WAIT:
                self.attempts += 1
                self.phase, self.ot_end = OT_RUN, self.laps + self.ot_laps
                out.append(("say", f"OVERTIME: intento {self.attempts} de {self.ot_max}, {self.ot_laps} vueltas"))
        return out

    def window_after_count(self) -> int:
        """The window once this caution is counted: the first caution of a stage still uses the 'none before' percentage."""
        length = self.ends[self.stage] - self.stage_start()
        return max(1, math.ceil(length * WINDOW_PCT[1 if self.cautions_in_stage > 1 else 0] / 100))

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
        elif self.phase == OT_RUN and laps >= self.ot_end:
            return self._chequered("BANDERA A CUADROS") if not caution else self._ot_failed("amarilla al final del overtime")
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
        return [("say", text), ("flag", "checkered"), ("finish",)]

    def _ot_failed(self, why: str) -> list[tuple]:
        if self.attempts >= self.ot_max:
            return [("say", f"OVERTIME: {why}; intentos agotados")] + self._chequered("FIN DE LA CARRERA")
        self.phase = OT_WAIT
        return [("say", f"OVERTIME: {why}; nuevo intento cuando haya verde")]

    def snapshot(self) -> dict:
        return {"phase": self.phase, "stage": self.stage + 1, "stages": len(self.ends), "stage_end": self.ends[self.stage], "total": self.total, "laps": self.laps,
                "cautions_in_stage": self.cautions_in_stage, "attempts": self.attempts, "ot_end": self.ot_end if self.phase == OT_RUN else None}
