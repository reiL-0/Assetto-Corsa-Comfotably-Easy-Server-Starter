"""Pydantic schemas of the /servers API: the bodies the panel and the calendar send and the shapes the manager answers with.

Pure data (no I/O, no manager state). Moved out of `app/servers.py` unchanged; `servers` re-exports every name, so
`from app.servers import SessionIn` keeps working. `Scalar` is the value type of one INI key.
"""
from __future__ import annotations

from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, model_validator

Scalar = str | int | float | bool


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
    wake: str = "window"
    stewards: str = "off"
    limits: dict = {}  # {cpu_percent, mem_mb, enforced}: caps of this server, applied the next time it starts (supervisor.limit_prefix)
    integrity: str = "warn"
    integrity_extras: bool = False
    welcome: str = ""
    csp_extra: str = ""
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
    damage_multiplier: int | None = Field(default=None, ge=0, le=100)  # %
    fuel_rate: int | None = Field(default=None, ge=0, le=500)  # % of normal consumption
    tyre_wear_rate: int | None = Field(default=None, ge=0, le=500)
    allowed_tyres_out: int | None = Field(default=None, ge=-1, le=4)  # wheels outside the line before a cut; -1 = never
    legal_tyres: str | None = Field(default=None, pattern=r"^[\w;]{0,60}$")  # e.g. "SV;S;M;H"
    max_ballast_kg: int | None = Field(default=None, ge=0, le=500)
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
    ballast: int = Field(default=0, ge=0, le=300)  # kg
    restrictor: int = Field(default=0, ge=0, le=100)  # %
    spectator: bool = False


class SessionIn(BaseModel):
    """What the admin panel's "new session" form sends."""

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
    practice_min: int | None = Field(default=None, ge=0, le=720)
    qualify_min: int | None = Field(default=None, ge=0, le=720)
    race_laps: int | None = Field(default=None, ge=0, le=999)
    race_min: int | None = Field(default=None, ge=0, le=1440)  # a timed race (endurance) instead of laps; not both
    race_wait_s: int = Field(default=60, ge=0, le=600)
    reversed_grid: int = Field(default=0, ge=-1, le=50)  # race grid: 0 as qualified, N = invert the first N, -1 = all
    loop: bool = True  # start over after the last session (practice -> qualify -> race -> practice ...)
    restart: bool = True

    @model_validator(mode="after")
    def _has_cars(self) -> SessionIn:
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


class LimitsIn(BaseModel):
    cpu_percent: int | None = Field(default=None, ge=10, le=800)   # 100 = one core; None = unlimited
    mem_mb: int | None = Field(default=None, ge=256, le=65536)     # None = unlimited


class ChatIn(BaseModel):
    message: str
    car_id: int | None = None  # None -> broadcast to everyone


class SetSessionIn(BaseModel):
    index: int = Field(ge=0, le=7)
    name: str = Field(min_length=1, max_length=40)
    session_type: int = Field(ge=1, le=3)
    laps: int = Field(default=0, ge=0, le=999)
    time_min: int = Field(default=0, ge=0, le=1440)
    wait_s: int = Field(default=0, ge=0, le=600)


class AdminCommandIn(BaseModel):
    command: str  # e.g. "ballast 3 50", "restrict 3 10" -- console admin commands
