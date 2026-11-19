"""Pydantic schemas of the /servers API: the bodies the panel and the calendar send and the shapes the manager answers with.

Pure data (no I/O, no manager state). Moved out of `app/servers.py` unchanged; `servers` re-exports every name, so
`from app.servers import SessionIn` keeps working. `Scalar` is the value type of one INI key.
"""
from __future__ import annotations

import re
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

from app.live.nascar import MAX_SHARE, MAX_SHARES, MAX_TOTAL
from app.rulebook import CutRule, DrsRule, Rulebook

Scalar = str | int | float | bool

SETUP_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")   # a file name under the server's setups/ (without «.ini»): no slash, no spaces, so nothing can escape the folder or add INI keys
MAX_SLOTS = 50


def _folder_name(v: str) -> str:
    """A skin or car folder name as it goes into the entry list: one path segment, no control characters (a newline would add INI keys)."""
    if v and (len(v) > 80 or v in (".", "..") or "/" in v or "\\" in v or any(ord(ch) < 32 or ord(ch) == 127 for ch in v) or v != v.strip()):
        raise ValueError("a folder name is one path segment of 80 characters at most, without control characters")
    return v


def _setup_name(v: str) -> str:
    if v and (not SETUP_NAME.fullmatch(v) or ".." in v or v.startswith(".") or v.lower().endswith(".ini")):
        raise ValueError("a setup name has letters, digits, «_», «-» and «.» only (no «..», not starting with a dot, without the «.ini»), 80 at most")
    return v


class ServerIn(BaseModel):
    name: str = Field(min_length=1)
    config: dict[str, dict[str, Scalar]] = {}
    entry_list: list[dict[str, Scalar]] = []


class ServerOut(BaseModel):
    id: int
    name: str
    base_port: int
    ports: dict[str, int]
    config: dict[str, dict[str, Scalar]]
    entry_list: list[dict[str, Scalar]]
    wake: str = "always"
    stewards: str = "off"
    limits: dict = {}  # {cpu_percent, mem_mb, enforced}: caps of this server, applied the next time it starts (supervisor.limit_prefix)
    integrity: str = "warn"
    integrity_extras: bool = False
    welcome: str = ""
    csp_extra: str = ""
    csp_scripts: dict[str, bool] = {}  # {hub, trainer, nascar}: which OPR CSP scripts this server sends (app/csp.py)
    weather_plan: dict | None = None  # played to CSP clients as hidden chat commands (app/live/cspweather.py)
    session: dict | None = None  # the last SessionIn applied through /apply (None for a server never set up from the panel)


class WeatherIn(BaseModel):
    """One [WEATHER_n] block. acServer cycles through them; the name is only sent to the clients (they need it installed). With CSP the name
    ends in `_type=<WeatherFX id>` (3_clear_type=15, 7_heavy_clouds_type=7 for rain): CSP clients show that weather, others the stock folder."""

    graphics: str = Field(default="3_clear", pattern=r"^[\w.\-=]{1,80}$")   # CSP weather names carry _type=<id> (WeatherFX type)
    ambient: int = Field(default=18, ge=-10, le=50)  # °C
    road: int = Field(default=6, ge=-20, le=50)  # °C ABOVE the ambient (acServer's BASE_TEMPERATURE_ROAD is relative)
    ambient_var: int = Field(default=1, ge=0, le=20)
    road_var: int = Field(default=1, ge=0, le=20)
    wind_min: int = Field(default=0, ge=0, le=60)  # km/h
    wind_max: int = Field(default=0, ge=0, le=60)
    wind_direction: int = Field(default=0, ge=0, le=359)  # degrees
    wind_direction_var: int = Field(default=0, ge=0, le=359)


class DynamicTrackIn(BaseModel):
    session_start: int = Field(default=95, ge=0, le=100)  # grip % when the session starts
    randomness: int = Field(default=2, ge=0, le=100)
    session_transfer: int = Field(default=90, ge=0, le=100)  # % of the grip carried to the next session
    lap_gain: int = Field(default=130, ge=0, le=1000)  # laps for the track to gain one grip point


class OptionsIn(BaseModel):
    """server_cfg.ini [SERVER] options. A field left out (None) keeps whatever the server has now; names are the
    INI keys in lower case, so `field.upper()` is the key."""

    sun_angle: int | None = Field(default=None, ge=-80, le=80)  # time of day: 0 = 13:00, 16 degrees per hour
    time_of_day_mult: int | None = Field(default=None, ge=0, le=100)  # clock speed
    abs_allowed: int | None = Field(default=None, ge=0, le=2)  # 0 off, 1 factory, 2 forced on
    tc_allowed: int | None = Field(default=None, ge=0, le=2)
    stability_allowed: bool | None = None
    autoclutch_allowed: bool | None = None
    tyre_blankets_allowed: bool | None = None
    force_virtual_mirror: bool | None = None
    damage_multiplier: int | None = Field(default=None, ge=0, le=1000)  # % (acServer accepts >100 for severe damage)
    fuel_rate: int | None = Field(default=None, ge=0, le=1000)  # % of normal consumption
    tyre_wear_rate: int | None = Field(default=None, ge=0, le=1000)
    allowed_tyres_out: int | None = Field(default=None, ge=-1, le=4)  # wheels outside the line before a cut; -1 = never
    legal_tyres: str | None = Field(default=None, pattern=r"^[\w;]{0,60}$")  # e.g. "SV;S;M;H"
    max_ballast_kg: int | None = Field(default=None, ge=0, le=10000)
    start_rule: int | None = Field(default=None, ge=0, le=2)  # 0 locked until green, 1 teleport to pits, 2 drive-through
    race_gas_penalty_disabled: bool | None = None
    max_contacts_per_km: int | None = Field(default=None, ge=-1, le=50)  # -1 = off
    race_over_time: int | None = Field(default=None, ge=0, le=3600)  # s the race stays open after the winner
    result_screen_time: int | None = Field(default=None, ge=0, le=600)
    qualify_max_wait_perc: int | None = Field(default=None, ge=100, le=1000)
    race_pit_window_start: int | None = Field(default=None, ge=0, le=720)  # minutes; 0 and 0 = no window
    race_pit_window_end: int | None = Field(default=None, ge=0, le=720)
    kick_quorum: int | None = Field(default=None, ge=0, le=100)  # % of votes
    voting_quorum: int | None = Field(default=None, ge=0, le=100)
    vote_duration: int | None = Field(default=None, ge=1, le=300)
    blacklist_mode: int | None = Field(default=None, ge=0, le=2)  # 0 plain kick, 1 until restart, 2 permanent ban
    client_send_interval_hz: int | None = Field(default=None, ge=10, le=60)
    weather: list[WeatherIn] | None = Field(default=None, max_length=10)  # None = keep the current blocks
    dynamic_track: DynamicTrackIn | None = None


class EntryIn(BaseModel):
    """One slot of the entry list. With a `guid` only that driver can take it (needs the list locked or not)."""

    model: str
    skin: str = ""
    driver_name: str = Field(default="", max_length=60)
    team: str = Field(default="", max_length=60)
    guid: str = Field(default="", pattern=r"^(\d{17}(;\d{17})*)?$")  # SteamID64; several joined by ';' share the car
    ballast: int = Field(default=0, ge=0, le=10000)  # kg (acServer takes any value; the cfg's MAX_BALLAST_KG follows it, ini_generator.server_cfg)
    restrictor: int = Field(default=0, ge=0, le=100)  # % (acServer's own hard cap: >100 aborts the start)
    spectator: bool = False
    fixed_setup: str = ""   # name (without «.ini») of an uploaded setup (/content/setups) this car is locked to; the entry list gets FIXED_SETUP=<name>.ini

    _setup = field_validator("fixed_setup")(_setup_name)
    _skin = field_validator("skin")(_folder_name)


class NascarSessionIn(BaseModel):
    total: int = Field(ge=2, le=MAX_TOTAL)
    shares: list[Annotated[float, Field(gt=0, le=MAX_SHARE, allow_inf_nan=False)]] | None = Field(default=None, min_length=1, max_length=MAX_SHARES)


UNLIMITED_MIN = 30000   # «unlimited» session time (20 days): acServer has no such switch. Measured on a real acServer: 65535 min overflows its 32-bit millisecond clock (timeleft comes out NEGATIVE and the session would end at once); the largest safe value is 35791 min, 30000 leaves room


class SequenceStep(BaseModel):
    """One session of a session sequence (app/sessionpatch.py): what acServer will play at this position of the cycle."""

    kind: Literal["practice", "qualify", "race"]
    name: str = Field(min_length=1, max_length=40)
    time_min: int = Field(default=0, ge=0, le=1440)   # minutes; a race by laps has 0
    laps: int = Field(default=0, ge=0, le=999)        # a race by laps; practice and qualify have 0
    wait_s: int = Field(default=0, ge=0, le=600)      # the wait before the session starts (any kind: acServer counts it from the moment the session begins)
    over_s: int | None = Field(default=None, ge=0, le=3600)   # RACE ONLY: seconds the race stays open after the winner finishes, for the rest to get in; None = the server's own RACE_OVER_TIME
    result_s: int | None = Field(default=None, ge=0, le=600)  # seconds the results are shown when this session ends, before the next one is set up (every kind); None = the server's own RESULT_SCREEN_TIME
    cut: int | None = Field(default=None, ge=0, le=50)        # STAGE 1 or 2 only: how many cars are cut at the end of it; None = the automatic cut (F1's 6 and 6 with 22 cars, scaled to the grid)
    stage: int | None = Field(default=None, ge=1, le=3)       # QUALIFY ONLY: this step is stage 1, 2 or 3 of a knockout qualifying like Formula 1's (Q1, Q2, Q3: app/knockout.py): the slowest are cut and held in the pits for the next stage

    @model_validator(mode="after")
    def _has_a_length(self) -> SequenceStep:
        if self.kind != "race" and (self.laps or not self.time_min):
            raise ValueError("a practice or a qualify is timed: time_min > 0 and no laps")
        if self.kind == "race" and bool(self.laps) == bool(self.time_min):
            raise ValueError("a race is by laps or by time, one of them")
        if self.kind != "race" and self.over_s is not None:
            raise ValueError("over_s: only a race waits for the others to finish after the winner")
        if self.stage is not None and self.kind != "qualify":
            raise ValueError("stage: only a qualifying can be a stage of a knockout")
        if self.cut is not None and self.stage not in (1, 2):
            raise ValueError("cut: only stage 1 and stage 2 cut cars (the last stage ranks the ones left)")
        return self


class SessionIn(BaseModel):
    """What the admin panel's "new session" form sends."""

    cuts_rule: CutRule | None = None
    drs_rule: DrsRule | None = None   # a manual session's DRS rules (app/live/drs.py); an event of a league uses the rulebook's
    name: str = Field(min_length=1, max_length=80)
    password: str = ""  # join password; "" = open
    admin_password: str | None = None  # None = keep the current one
    track: str
    track_config: str = ""
    cars: list[str] = []  # open slots are spread over these; ignored when `entries` is given
    max_clients: int = Field(default=10, ge=1, le=50)  # ...as is this: the entry list sets the slot count
    entries: list[EntryIn] = Field(default_factory=list, max_length=50)
    options: OptionsIn = Field(default_factory=OptionsIn)
    locked: bool = False  # only the Steam IDs in `entries` may join (LOCKED_ENTRY_LIST)
    pickup: bool = True  # drivers without a reserved slot pick a free one on joining
    welcome: str = Field(default="", max_length=2000)  # shown to a driver when joining (the session's rules, the league's link...)
    practice_min: int | None = Field(default=None, ge=0, le=UNLIMITED_MIN)   # minutes; UNLIMITED_MIN is «unlimited»
    qualify_min: int | None = Field(default=None, ge=0, le=UNLIMITED_MIN)
    race_laps: int | None = Field(default=None, ge=0, le=999)
    race_min: int | None = Field(default=None, ge=0, le=UNLIMITED_MIN)  # a timed race (endurance) instead of laps; not both; UNLIMITED_MIN = no end
    race_est_min: int | None = Field(default=None, ge=0, le=1440)  # how long a race BY LAPS lasts for the session clock of a stopped server (app/timeline.py): 0 = the default of 20 minutes; left out = the server keeps the one it has. Not sent to acServer
    car_counts: dict[str, Annotated[int, Field(strict=True, ge=0, le=MAX_SLOTS)]] | None = None   # open slots per model («5 of this, 2 of that»); then `cars` and `max_clients` follow from it. Ignored with `entries`
    pace_steam_id: str | None = Field(default=None, pattern=r"^[0-9]{17}$")   # the pace car's driver (SteamID): named once when the server starts or the session is applied; the director can change it afterwards
    ballast: int = Field(default=0, ge=0, le=10000)   # kg for every slot (open ones too) whose own ballast is 0: a driver's own value wins
    restrictor: int = Field(default=0, ge=0, le=100)   # % for every slot whose own restrictor is 0, same rule
    random_skins: bool = False   # each open slot (and each entry with no skin) gets a random skin of its car instead of the pack's first
    car_setups: dict[str, str] = {}   # model -> uploaded setup name, locked on that model's open slots (the league's roster slots get it too)
    category: str = Field(default="", max_length=64)  # applied event category for neutralization
    rulebook_revision: str | None = None  # SHA-256 of the applied document
    rulebook_league_id: int | None = None
    rulebook_event_id: int | None = None  # binds the snapshot to the scheduled event's results
    rulebook_snapshot: Rulebook | None = None  # versioned copy saved with the applied session
    nascar: NascarSessionIn | None = None  # armed only when ACSP announces a new race
    cuts_off: list[Literal["practice", "qualify", "race"]] = Field(default_factory=list, max_length=3)   # the sessions in which the track-limit cuts are not counted or shown at all (an `inherit` is the empty list; a rule of its own is `cuts_rule`)
    drs_off: bool = False   # no DRS in this session, whatever the server or the league say (DRS only exists in a race)
    rolling: Literal["off", "nascar", "f1"] | None = None   # how the RACE starts: off = standing start, nascar = a formation lap behind the pace car and the green on the line, f1 = formation lap, back to the grid, standing start. None = the old behaviour (NASCAR: rolling; the rest: standing). A NASCAR race is always rolling (see _nascar_is_rolling)
    race_wait_s: int = Field(default=60, ge=0, le=600)
    reversed_grid: int = Field(default=0, ge=-1, le=50)  # race grid: 0 as qualified, N = invert the first N, -1 = all
    loop: bool = True  # start over after the last session (practice -> qualify -> race -> practice ...)
    restart: bool = True
    sequence: list[SequenceStep] | None = Field(default=None, min_length=4, max_length=12)   # the sessions of the cycle when they are not just practice, qualify, race (e.g. P, Q, R1, intermediate practice, R2): installed in the running acServer at every start (app/sessionpatch.py)
    grid_from_qualify: bool = False   # a practice AFTER the qualifying, right before a race, arranges the grid: the race starts as the qualifying is classified on the site (clean laps) with the stewards' grid penalties applied (app/gridtimes.py)
    grid_from_race: int | None = Field(default=None, ge=-1, le=50)   # the grid of the race that follows an intermediate session comes from the race before it: 0 = as it finished (stewards' grid penalties applied), N = invert the first N, -1 = all; None = the director sets it by hand (app/gridtimes.py)

    @field_validator("car_setups")
    @classmethod
    def _setups_named(cls, v: dict[str, str]) -> dict[str, str]:
        for name in v.values():
            if not name:
                raise ValueError("car_setups: a setup name is empty")
            _setup_name(name)
        return v

    @model_validator(mode="after")
    def _sequence_fits_the_sections(self) -> SessionIn:
        if self.sequence is None:
            return self
        have = {"practice": bool(self.practice_min), "qualify": bool(self.qualify_min), "race": bool(self.race_laps or self.race_min)}
        for st in self.sequence:
            if not have[st.kind]:
                raise ValueError(f"sequence: there is a {st.kind} step but the session has no {st.kind} section (practice_min / qualify_min / race_laps / race_min)")
        if not any(st.kind == "race" for st in self.sequence):
            raise ValueError("sequence: it needs at least one race")
        races = [st for st in self.sequence if st.kind == "race"]
        if any(r.laps for r in races) and any(not r.laps for r in races):
            raise ValueError("sequence: the races are all by laps or all by time (Content Manager has one flag for the whole list)")
        if self.reversed_grid:
            raise ValueError("sequence: acServer's own reversed grid does not work with a sequence (use grid_from_race)")
        stage_names = [st.name for st in self.sequence if st.stage is not None]
        others = [st.name for st in self.sequence if st.stage is None]
        if len(stage_names) != len(set(stage_names)) or set(stage_names) & set(others):
            raise ValueError("sequence: the steps of the knockout stages need names no other step has (each result file says which stage it is by that name)")
        stages = [st.stage for st in self.sequence if st.stage is not None]
        if stages and stages != list(range(1, len(stages) + 1)):
            raise ValueError("sequence: the knockout stages are 1, 2, 3 in that order, each once (the steps with a `stage`)")
        if stages and not any(st.kind == "race" for st in self.sequence[[st.stage for st in self.sequence].index(stages[-1]):]):
            raise ValueError("sequence: a race must come after the last knockout stage")
        return self

    @model_validator(mode="after")
    def _off_is_not_a_rule(self) -> SessionIn:
        self.cuts_off = list(dict.fromkeys(self.cuts_off))
        if self.cuts_rule is not None and "race" in self.cuts_off:
            raise ValueError("cuts_off: la carrera no puede tener cortes apagados y regla propia a la vez")
        if self.drs_rule is not None and self.drs_off:
            raise ValueError("drs_off: el DRS no puede estar apagado y tener regla propia a la vez")
        return self

    @model_validator(mode="after")
    def _nascar_is_rolling(self) -> SessionIn:
        if self.nascar and self.rolling not in (None, "nascar"):
            raise ValueError("rolling: una carrera NASCAR siempre es rolling start (nascar)")
        return self

    @model_validator(mode="after")
    def _has_cars(self) -> SessionIn:
        if self.entries:
            self.car_counts = None   # reserved slots say the cars one by one
        elif self.car_counts is not None:
            counts = {car: n for car, n in self.car_counts.items() if n}
            if not counts or sum(counts.values()) > MAX_SLOTS:
                raise ValueError(f"car_counts: at least one car, and {MAX_SLOTS} slots in total at most")
            self.car_counts, self.cars, self.max_clients = counts, list(counts), sum(counts.values())
        if not self.cars and not self.entries:
            raise ValueError("choose cars or fill the entry list")
        if self.race_laps and self.race_min:
            raise ValueError("a race is by laps or by time, not both")
        return self


class AppliedOut(ServerOut):
    restarted: bool


class WakeIn(BaseModel):
    mode: Literal["off", "window", "always"]


class WeatherEntryIn(BaseModel):
    """One weather of the plan, as in AC Server Manager's editor (app/live/weatherplan.py)."""

    type: int = Field(default=15, ge=0, le=32)          # WeatherFX type id: 15 clear, 7 rain, 8 heavy rain, 1 thunderstorm...
    duration_min: float = Field(default=0, ge=0, le=1440)   # real minutes before moving to the next one; 0 = until the session ends
    sessions: list[Literal["practice", "qualify", "race"]] = ["practice", "qualify", "race"]
    ambient: float = Field(default=20, ge=-10, le=50)
    road: float = Field(default=6, ge=-20, le=40)        # added to the ambient temperature
    ambient_var: float = Field(default=0, ge=0, le=20)
    road_var: float = Field(default=0, ge=0, le=20)
    wind_min: float = Field(default=0, ge=0, le=40)      # m/s
    wind_max: float = Field(default=0, ge=0, le=40)
    wind_dir: float = Field(default=0, ge=0, le=360)
    wind_dir_var: float = Field(default=0, ge=0, le=180)


class LiveWeatherIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    refresh_min: float = Field(default=10, ge=1, le=120)


class WeatherPlanIn(BaseModel):
    mode: Literal["entries", "live"] = "entries"        # entries: the list below per session; live: the real weather of `live` (lat, lon)
    entries: list[WeatherEntryIn] = Field(default=[], max_length=8)   # few changes: each one makes every client recompute clouds and rain (frame drops on weaker PCs)
    transition_s: float = Field(default=90, ge=20, le=900)          # each change is a smooth blend of this many seconds (long ones are gentler)
    update_s: float = Field(default=30, ge=5, le=120)               # seconds between commands to the clients
    live: LiveWeatherIn | None = None
    driving: Literal["real", "visual"] = "real"                    # visual: the weather is only seen (grip 100 %, no water on the track); real: it changes the grip
    timezone: str | None = Field(default=None, max_length=60)       # IANA zone of the track (e.g. Australia/Melbourne); None = from its geotags (app/live/tracktime.py)
    sun_angle: int | None = Field(default=None, ge=-80, le=80)      # sun position sent to CSP clients (0 = 13:00, 16 degrees per hour); None = the server's SUN_ANGLE

    @model_validator(mode="after")
    def _complete(self) -> WeatherPlanIn:
        if self.timezone:
            try:
                ZoneInfo(self.timezone)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError("timezone: unknown IANA zone") from None
        if self.mode == "entries" and not self.entries:
            raise ValueError("entries: at least one weather")
        if self.mode == "live" and not self.live:
            raise ValueError("live: latitude and longitude are required")
        return self


class CspWeatherIn(BaseModel):
    current: int = Field(default=15, ge=0, le=32)
    upcoming: int | None = Field(default=None, ge=0, le=32)
    transition: float = Field(default=0, ge=0, le=1)
    ambient: float = Field(default=20, ge=-10, le=50)
    road: float = Field(default=22, ge=-10, le=70)
    grip: float = Field(default=1.0, ge=0.6, le=1.0)
    rain: float = Field(default=0, ge=0, le=1)
    wetness: float = Field(default=0, ge=0, le=1)
    water: float = Field(default=0, ge=0, le=1)


class CspExtraIn(BaseModel):
    text: str = Field(default="", max_length=6000)  # INI; "" removes it


class CspScriptsIn(BaseModel):
    hub: bool
    trainer: bool
    nascar: bool | None = None   # the NASCAR caution banners of the hub (they load only when the session asks for NASCAR, see csp.nascar_requested); not sent = as it was


class LimitsIn(BaseModel):
    cpu_percent: int | None = Field(default=None, ge=10, le=800)   # 100 = one core; None = unlimited
    mem_mb: int | None = Field(default=None, ge=256, le=65536)     # None = unlimited


class ChatIn(BaseModel):
    message: str
    car_id: int | None = None  # None -> broadcast to everyone


class KickIn(BaseModel):
    driver_guid: str = Field(pattern=r"^\d{17}$")


class GridIn(BaseModel):
    """The starting order of the next race: Steam IDs, pole first (app/gridtimes.py). Drivers it does not name go last."""
    guids: list[Annotated[str, Field(pattern=r"^\d{17}$")]] = Field(min_length=1, max_length=100)


class SetSessionIn(BaseModel):
    index: int = Field(ge=0, le=11)   # the 12 slots of the grown acServer (app/sessionpatch.py MAX_SESSIONS); the server itself answers «out of bounds» past its real length
    name: str = Field(min_length=1, max_length=40)
    session_type: int = Field(ge=1, le=3)
    laps: int = Field(default=0, ge=0, le=999)
    time_min: int = Field(default=0, ge=0, le=1440)
    wait_s: int = Field(default=0, ge=0, le=600)


class AdminCommandIn(BaseModel):
    command: str  # e.g. "ballast 3 50", "restrict 3 10" -- console admin commands
