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

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.models import DEFAULT_POINTS_SYSTEM, Championship

SCHEMA_VERSION = 1


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Scoring(_Strict):
    table: list[int] = Field(default_factory=lambda: list(DEFAULT_POINTS_SYSTEM), max_length=100)   # points for 1st, 2nd...
    stage_points: list[int] = Field(default_factory=list, max_length=40)   # NASCAR: points for the 1st, 2nd... of the order at the end of every stage that is not the last ([] = none)


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
    """Warn about track cuts in a Race: the first `tolerance` cuts are free, then every `cuts_per_step` cuts add `step_seconds`, up to `max_seconds`."""

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


class FlagsRule(_Strict):
    """The flags that need no director (app/live/autoflags.py). `shadow` (the default) decides and writes down, shows nothing; `live` shows it to the driver; `off` does not even decide."""

    blue: Literal["off", "shadow", "live"] = "shadow"
    danger: Literal["off", "shadow", "live"] = "shadow"


class Detectors(_Strict):
    cuts: CutRule | None = None   # None = off
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
        detectors=Detectors(cuts=CutRule(tolerance=5, cuts_per_step=1, step_seconds=5, max_seconds=5)),
    ),
}
