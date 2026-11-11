"""A league's rulebook: the penalties and scoring rules of one championship in one versioned, validated document (`Championship.rulebook`).

Until a league has one, its rulebook is *derived* from the three older columns (`points_system`, `penalties`, `auto_rules`), so nothing changes for it: `effective(c)` is
the only place that decides, and everything that needs a rule reads it from there. Setting a rulebook (PUT /championships/{id}/rulebook) makes it the source; from then on
the older columns are refused by PATCH (they would be ignored).

What is here is what the manager can apply today: the points table, the penalty catalogue and the track-cut detector. Sections arrive with the step that uses them (conduct
points, race control, scoring details: todo/rulebook-por-liga-diseno.md in the site repo); a new optional section does not change `schema_version`. Only what a result file can
show is a detector: contacts, "advantage" and "avoidable" are the stewards' judgement. `PRESETS` are templates to copy into a league, not rules that follow it around.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from app.models import DEFAULT_POINTS_SYSTEM, Championship

SCHEMA_VERSION = 1


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Scoring(_Strict):
    table: list[int] = Field(default_factory=lambda: list(DEFAULT_POINTS_SYSTEM), max_length=100)   # points for 1st, 2nd...
    stage_points: list[int] = Field(default_factory=list, max_length=40)   # NASCAR: points for the 1st, 2nd... of the order at the end of every stage that is not the last ([] = none)
    min_laps_pct: int = Field(default=0, ge=0, le=100)   # a driver who completed fewer than this % of the winner's laps does not score (DNF); 0 = no minimum. Whoever drove no lap (DNS) never scores


class CatalogueItem(_Strict):
    """What a steward can pick. `value` is seconds: added to the race time (time), the stop (stopgo, max 60) or the time charged instead of a drive-through, which acServer cannot
    force (drivethrough). `conduct_points` is for the conduct ledger (a later step)."""

    key: str = Field(pattern=r"^[a-z0-9_-]{1,30}$")
    name: str = Field(min_length=1, max_length=40)
    kind: Literal["time", "stopgo", "drivethrough", "dsq"]
    value: int = Field(default=0, ge=0, le=3600)
    conduct_points: int = Field(default=0, ge=0, le=20)

    @model_validator(mode="after")
    def _value_fits_kind(self) -> CatalogueItem:
        if self.kind == "dsq" and self.value:
            raise ValueError("a disqualification takes no seconds")
        if self.kind != "dsq" and not self.value:
            raise ValueError("this penalty needs seconds")
        if self.kind == "stopgo" and self.value > 60:
            raise ValueError("a stop-and-go is at most 60 seconds")
        return self


class CutRule(_Strict):
    """Without ladder, retain legacy notices. With ladder (e.g. [0, 5, 5, 10]), every tolerance + 1 cuts completes a sanctioned round; the last step repeats.
    A step of 0 is a live drive-through (3 laps to serve it); one never served by the result is charged `dt_fixed_s` seconds instead.
    In ladder mode only tolerance and ladder set the rounds/seconds; cuts_per_step,
    step_seconds and max_seconds remain required for compatibility. Automatic seconds
    are capped by the same rulebook's neutralization.max_auto_time_penalty_s.
    """

    ladder: list[Annotated[int, Field(ge=0, le=600)]] | None = Field(default=None, min_length=1, max_length=10)
    dt_fixed_s: int | None = Field(default=None, ge=1, le=600)   # None = 10 s
    tolerance: int = Field(default=0, ge=0, le=1000)
    cuts_per_step: int = Field(ge=1, le=100)
    step_seconds: int = Field(ge=1, le=60)
    max_seconds: int = Field(ge=1, le=600)


class GiveBackRule(_Strict):
    """The advantage gained off the track (app/live/giveback.py): a car that leaves the track and comes back in front of a car it was behind has `window_s` seconds to give the place back, or the race time
    grows by `seconds`. ON by default (that is what a league that never touched it gets); `enabled: false` switches it off for the league."""

    enabled: bool = True
    seconds: int = Field(default=5, ge=1, le=60)
    window_s: int = Field(default=30, ge=10, le=120)


class DrsRule(_Strict):
    """DRS in a race (app/live/drs.py). Off by default (`detectors.drs` is None). DRS is not allowed for `start_laps_off` laps after the start, `restart_laps_off` after each restart, while it rains
    (the weather signal, max of rain and wetness, went over `wet_on`; allowed again after `dry_confirm_s` seconds under `dry_below`) and under any caution. Using it then is ALWAYS a drive-through (3 laps to serve it),
    once per period; one never served costs `unserved_s` seconds on the result."""

    start_laps_off: int = Field(default=2, ge=0, le=20)
    restart_laps_off: int = Field(default=2, ge=0, le=10)
    penalty: Literal["dt"] = "dt"   # (only a drive-through; kept so a rule saved with it still reads)
    unserved_s: int = Field(default=10, ge=1, le=60)   # seconds on the result for a drive-through nobody served
    rain_disables: bool = True
    wet_on: float = Field(default=0.15, gt=0, le=1, allow_inf_nan=False)
    dry_below: float = Field(default=0.05, ge=0, lt=1, allow_inf_nan=False)
    dry_confirm_s: int = Field(default=30, ge=5, le=600)

    @model_validator(mode="after")
    def _hysteresis(self) -> DrsRule:
        if self.dry_below >= self.wet_on:
            raise ValueError("dry_below must be under wet_on")
        return self


class BlueRule(_Strict):
    """The blue flag as a rule (app/live/flagpace.py, RaceControl.autoflags_tick). It shows only when the car behind has more PACE than the one it is about to lap (the median of its last `pace_laps` clean laps is at
    least `pace_margin_pct` % better; with no pace known there is no blue), and not in NASCAR. A driver who has not let it through after `yield_s` seconds commits one offence per appearance of the flag: the
    `ladder` gives the sanction of the n-th offence (0 = a drive-through, N = N seconds on the result; the last step repeats; empty = no sanction), and a drive-through nobody served costs `unserved_s` seconds.
    Only sanctions when the league set the blue flag to `live` (FlagsRule)."""

    yield_s: int = Field(default=15, ge=3, le=120)
    pace_margin_pct: float = Field(default=0.7, ge=0, le=10, allow_inf_nan=False)
    pace_laps: int = Field(default=3, ge=2, le=10)
    ladder: list[Annotated[int, Field(ge=0, le=600)]] = Field(default_factory=lambda: [0, 5], max_length=10)
    unserved_s: int = Field(default=10, ge=1, le=60)


_Ladder = list[Annotated[int, Field(ge=0, le=600)]]   # a sanction ladder: 0 = a drive-through, N = N seconds on the result; the last step repeats; empty = no sanction


class YellowRule(_Strict):
    """A yellow flag: no overtaking. Whoever passes owes the place for `window_s` seconds (app/live/nopass.py, the judge of the VSC/FCY); one who does not give it back commits an offence."""

    window_s: int = Field(default=15, ge=5, le=60)
    ladder: _Ladder = Field(default_factory=lambda: [0, 5], max_length=10)
    unserved_s: int = Field(default=10, ge=1, le=60)


class MeatballRule(_Strict):
    """ORANGE DISC: a car with mechanical damage of `damage_pct` % or more (read by the driver's own game and reported to the manager) must come into the pits within `pit_laps` laps, or it gets the black flag.
    `use_speed` adds the speed test (off by default): a car running under `slow_pct` % of the speed of the cars around it for `sustained_s` seconds counts too."""

    damage_pct: int = Field(default=60, ge=5, le=100)
    pit_laps: int = Field(default=2, ge=1, le=10)
    use_speed: bool = False
    slow_pct: int = Field(default=40, ge=10, le=80)
    sustained_s: int = Field(default=20, ge=5, le=120)


class BlackRule(_Strict):
    """BLACK AND WHITE then BLACK. Every sanction a driver earns in the race (from the flags, DRS, the cuts ladder) is a conduct offence: at `bw_at` of them the black-and-white flag warns him (no sanction), at `black_at` he
    gets the BLACK flag (also from an orange disc or a red flag that was not obeyed): he must come into the pits within `pit_laps` laps (the counter starts again) or he is DISQUALIFIED from the race."""

    bw_at: int = Field(default=3, ge=1, le=20)
    black_at: int = Field(default=6, ge=2, le=40)
    pit_laps: int = Field(default=3, ge=1, le=10)

    @model_validator(mode="after")
    def _order(self) -> BlackRule:
        if self.black_at <= self.bw_at:
            raise ValueError("black_at must be over bw_at")
        return self


class RedRule(_Strict):
    """RED flag: the director shows it and every car must come into the pit lane before it completes `laps` more lap(s) (by default: when it finishes the lap it is on); one that does not gets the BLACK flag."""

    laps: int = Field(default=1, ge=1, le=3)


class FlagsRule(_Strict):
    """The flags that need no director (app/live/autoflags.py): `live` decides and shows it to the driver (and sanctions); `off` does not even decide. There is no shadow mode: a rulebook saved with
    `shadow` (the old default) reads as `live`."""

    blue: Literal["off", "live"] = "live"
    danger: Literal["off", "live"] = "live"
    yellow: Literal["off", "live"] = "live"
    meatball: Literal["off", "live"] = "live"
    black: Literal["off", "live"] = "live"   # (black and white and black: one switch)
    red: Literal["off", "live"] = "live"

    @field_validator("*", mode="before")
    @classmethod
    def _no_shadow(cls, v):
        return "live" if v == "shadow" else v


class Detectors(_Strict):
    cuts: CutRule | None = None   # None = off
    drs: DrsRule | None = None   # None = off
    blue: BlueRule = Field(default_factory=BlueRule)
    yellow: YellowRule = Field(default_factory=YellowRule)
    meatball: MeatballRule = Field(default_factory=MeatballRule)
    black: BlackRule = Field(default_factory=BlackRule)
    red: RedRule = Field(default_factory=RedRule)
    flags: FlagsRule = Field(default_factory=FlagsRule)
    give_back: GiveBackRule = Field(default_factory=GiveBackRule)


class SlowCarRule(_Strict):
    pace_ratio: float = Field(default=0.6, gt=0, lt=1, allow_inf_nan=False)
    sustained_s: float = Field(default=8, ge=1, le=120)


class SpinRule(_Strict):
    resume_kmh: float = Field(default=30, ge=5, le=100)
    clear_s: float = Field(default=5, ge=1, le=60)
    stopped_s: float = Field(default=5, ge=1, le=120)


class PileupRule(_Strict):
    cars: int = Field(default=3, ge=2, le=50)
    window_s: float = Field(default=10, ge=1, le=120)
    escalate_to: Literal["FCY"] = "FCY"


class SevereRule(_Strict):
    cars: int = Field(default=5, ge=2, le=50)
    escalate_to: Literal["SC"] = "SC"  # stored only; no SC implementation


class Neutralization(_Strict):
    """Automatic incident escalation. Missing categories use default_mode; off does nothing."""

    mode: Literal["off", "shadow", "live"] = "off"
    default_mode: Literal["VSC", "FCY"] = "VSC"
    mode_by_category: dict[Annotated[str, StringConstraints(max_length=64)], Literal["VSC", "FCY"]] = Field(default_factory=dict, max_length=100)
    max_auto_time_penalty_s: int = Field(default=10, ge=1, le=60)
    green_after_laps: int = Field(default=3, ge=1, le=10)
    green_clear_s: float = Field(default=15, ge=1, le=300)
    green_max_wait_s: int = Field(default=600, ge=60, le=1800)
    green_max_extra_laps: int = Field(default=3, ge=0, le=10)
    slow_car: SlowCarRule = Field(default_factory=SlowCarRule)
    spin: SpinRule = Field(default_factory=SpinRule)
    pileup: PileupRule = Field(default_factory=PileupRule)
    severe: SevereRule = Field(default_factory=SevereRule)
    stale_telemetry_s: float = Field(default=1.5, gt=0, le=10)


class Rulebook(_Strict):
    schema_version: Literal[1] = SCHEMA_VERSION
    neutralization: Neutralization = Field(default_factory=Neutralization)
    preset: str | None = None   # the template it was copied from, for the record
    scoring: Scoring = Field(default_factory=Scoring)
    catalogue: list[CatalogueItem] = Field(default_factory=list, max_length=50)
    detectors: Detectors = Field(default_factory=Detectors)

    @model_validator(mode="after")
    def _unique_keys(self) -> Rulebook:
        keys = [i.key for i in self.catalogue]
        if len(keys) != len(set(keys)):
            raise ValueError("the catalogue has a repeated key")
        return self


def from_legacy(c: Championship) -> Rulebook:
    """The rulebook of a league that has none: its three older columns, read as they have always been read."""
    return Rulebook(
        scoring=Scoring(table=list(c.points_system or [])),
        catalogue=[CatalogueItem(key=f"l{n}", name=p["name"], kind="dsq" if p.get("dsq") else "time", value=0 if p.get("dsq") else p["seconds"]) for n, p in enumerate(c.penalties or [])],
        detectors=Detectors(cuts=CutRule(**c.auto_rules) if c.auto_rules else None),
    )


def effective(c: Championship) -> Rulebook:
    return Rulebook.model_validate(c.rulebook) if c.rulebook else from_legacy(c)


def legacy_catalogue(rb: Rulebook) -> list[dict]:
    """The catalogue as the Sancionar panel and `add_penalty` have always read it ({name, seconds, dsq}), plus `kind` and `key`."""
    return [{"key": i.key, "name": i.name, "kind": i.kind, "seconds": i.value, "dsq": i.kind == "dsq"} for i in rb.catalogue]


# NASCAR Cup: 40 for the win, 35 for second and one less per place down to 1 for 36th and the rest of the field; stage points 10 down to 1 for the first ten at the end of each stage but the last
NASCAR_TABLE = [40, *range(35, 0, -1), 1, 1, 1, 1]
PRESETS: dict[str, Rulebook] = {
    "nascar-cup": Rulebook(preset="nascar-cup", scoring=Scoring(table=NASCAR_TABLE, stage_points=list(range(10, 0, -1)))),
    # From the VRL championship rulebook (July 2026), the base to calibrate against, not gospel: points for the top 10, the +5/+10/+15 s, drive-through (30 s charged),
    # stop & go 30 s and DSQ with their conduct points, and "more than 5 cuts = +5 s".
    "vrl-base": Rulebook(
        preset="vrl-base",
        scoring=Scoring(table=[25, 18, 15, 12, 10, 8, 6, 4, 2, 1]),
        catalogue=[
            CatalogueItem(key="limites-5", name="Límites de pista / toque leve", kind="time", value=5, conduct_points=1),
            CatalogueItem(key="evitable-10", name="Incidente evitable +10 s", kind="time", value=10, conduct_points=2),
            CatalogueItem(key="evitable-15", name="Incidente evitable +15 s", kind="time", value=15, conduct_points=2),
            CatalogueItem(key="drive-through", name="Drive-through", kind="drivethrough", value=30, conduct_points=3),
            CatalogueItem(key="stop-go-30", name="Stop & go 30 s", kind="stopgo", value=30, conduct_points=4),
            CatalogueItem(key="dsq", name="Descalificación", kind="dsq", conduct_points=9),
        ],
        detectors=Detectors(cuts=CutRule(tolerance=2, cuts_per_step=1, step_seconds=5, max_seconds=600, ladder=[0, 5, 5, 10])),
    ),
}
