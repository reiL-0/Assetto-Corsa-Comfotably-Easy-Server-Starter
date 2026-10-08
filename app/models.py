from datetime import UTC, datetime

from sqlalchemy import JSON
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(UTC)


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    steam_id: str | None = Field(default=None, unique=True, index=True)
    discord_id: str | None = Field(default=None, unique=True, index=True)  # linked through app/discord.py (OAuth2)
    timezone: str = "America/Mexico_City"  # IANA name; where a time has to be shown as local (everything stored is UTC), the browser sends its own via PATCH /auth/me
    username: str
    password_hash: str | None = None
    role: str = "driver"
    created_at: datetime = Field(default_factory=_now)


class Token(SQLModel, table=True):
    """Cookie sessions (login, expiring) and Bearer API tokens (never expire). Only the hash is stored."""

    __tablename__ = "tokens"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    token_hash: str = Field(unique=True, index=True)
    name: str = "session"
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=_now)


class Server(SQLModel, table=True):
    __tablename__ = "servers"

    id: int | None = Field(default=None, primary_key=True)
    name: str
    base_port: int  # ports tcp/udp/http/plugin derived as base..base+2
    # server_cfg.ini as {SECTION: {KEY: value}}; entry_list.ini as [{CAR_0 fields}, ...]
    config: dict = Field(default_factory=dict, sa_type=JSON)
    entry_list: list = Field(default_factory=list, sa_type=JSON)
    welcome: str = ""  # text shown to a driver on joining (WELCOME_MESSAGE file); changes with each session
    weather_plan: dict | None = Field(default=None, sa_type=JSON)  # {steps: [[second, WeatherFX type]], transition_s, loop_s, ambient}: played to CSP clients (app/live/cspweather.py)
    csp_extra: str = ""  # Custom Shaders Patch extra options (INI) hidden at the end of the welcome message (app/csp.py); kept across sessions
    session: dict | None = Field(default=None, sa_type=JSON)  # the last servers.SessionIn applied (no admin password): what the panel edits and the calendar starts from
    integrity: str = "warn"  # off | warn | require: what to do when the content differs from its seal (app/integrity.py)
    integrity_extras: bool = False  # also verify the sealed extras (plugins, other files) before starting
    anchor_index: int | None = None  # the last session start seen (app/timeline.py): which session...
    anchor_at: float | None = None   # ...and when it started (unix s); the session clock runs from here even when the server is off
    cpu_limit: int | None = None  # CPU quota of this server in % of one core (100 = one core); None = unlimited (supervisor.limit_prefix)
    mem_limit_mb: int | None = None  # RAM cap in MB (the kernel kills the server above it); None = unlimited
    stewards: str = "off"  # off | shadow: the automatic stewards (app/stewards) only record incidents, they never punish (phase 0)
    wake: str = "window"  # when a player trying to join a stopped server starts it: off | window (inside an event's window) | always
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class ContentBlob(SQLModel, table=True):
    """A car or track folder known by the SHA-256 of its files (app/catalog.py): stored once, however many holders have it."""

    __tablename__ = "content_blobs"

    hash: str = Field(primary_key=True)
    kind: str  # car | track
    name: str  # the folder name AC looks for
    size: int = 0
    files: int = 0
    source_url: str = ""  # the modder's official page, shown to players as «Descargar» (we never serve the files)
    created_at: datetime = Field(default_factory=_now)


class ContentHolder(SQLModel, table=True):
    """Who has a blob enabled and on what basis. A rights claim against one holder revokes only that row; the file stays while another holder is active."""

    __tablename__ = "content_holders"

    id: int | None = Field(default=None, primary_key=True)
    hash: str = Field(index=True)
    holder: str = Field(index=True)  # tenant id; "league" for the content of our own league
    uploaded_by: str = ""
    attested_at: datetime | None = None  # when they declared they hold the licence (uploading counts)
    status: str = "active"  # active | revoked | disputed | superseded (the holder uploaded a newer version under the same name)
    note: str = ""
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class BlockedHash(SQLModel, table=True):
    """A file removed for everyone after a rights claim against the file itself; re-uploading it is refused."""

    __tablename__ = "blocked_hashes"

    hash: str = Field(primary_key=True)
    reason: str = ""
    blocked_at: datetime = Field(default_factory=_now)


class CatalogEvent(SQLModel, table=True):
    """Audit trail of the catalog: who did what to which file, when and why."""

    __tablename__ = "catalog_events"

    id: int | None = Field(default=None, primary_key=True)
    at: datetime = Field(default_factory=_now)
    actor: str = ""
    action: str  # upload | scan | source | revoke | dispute | restore | block | unblock | proof | purge
    hash: str = ""
    holder: str = ""
    detail: str = ""


class Event(SQLModel, table=True):
    """A saved session ("preset"): the new-session form's contents under a title, ready to run on any server."""

    __tablename__ = "events"

    id: int | None = Field(default=None, primary_key=True)
    title: str
    notes: str = ""
    data: dict = Field(default_factory=dict, sa_type=JSON)  # a servers.SessionIn, as JSON
    league_id: int | None = None  # the league whose roster is this event's closed entry list when it runs (app/league.py); None = the entries saved in `data`
    is_default: bool = False  # the preset a new calendar event starts from; at most one (events.set_default)
    derived: bool = False  # made by the website's calendar sync for one calendar event, not a preset an admin edits
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class ContentSeal(SQLModel, table=True):
    """The approved MD5s of what acServer verifies for a car / track / the system, or of an extra file or folder (app/integrity.py)."""

    __tablename__ = "content_seals"

    key: str = Field(primary_key=True)  # system | track:<track>:<layout> | car:<car> | extra:<path>
    files: dict = Field(default_factory=dict, sa_type=JSON)  # relative path -> md5
    sealed_at: datetime = Field(default_factory=_now)
    sealed_by: str = ""


class Schedule(SQLModel, table=True):
    """A saved event set to start on a server at a given time, with Discord reminders before it (app.schedule)."""

    __tablename__ = "schedules"

    id: int | None = Field(default=None, primary_key=True)
    event_id: int
    server_id: int
    start_at: float = Field(index=True)  # unix seconds, UTC
    reminders: list[int] = Field(default_factory=lambda: [60, 10], sa_type=JSON)  # minutes before start_at
    sent: list[int] = Field(default_factory=list, sa_type=JSON)  # the reminders already posted
    info: str = ""  # free text added to this schedule's Discord messages (server address, session times...)
    notes: str = ""  # the "Notas" section of the sign-up announcement only (download links, who it is for, rules)
    duration_min: int | None = None  # how long the event lasts; the server is stopped when it is over (None = idle stop only)
    loaded: bool = False  # the event's session is already on the server (a player woke it early, or it started)
    end_warned: bool = False  # the "ends in 5 minutes" chat message went out
    rsvp_message: str | None = None  # id of the bot's announcement in ACM_DISCORD_CHANNEL (reactions = sign-ups)
    rsvp_text: str = ""  # the text that message shows now; it is edited only when this changes
    state: str = "pending"  # pending | running (started, waiting for its end) | done | failed | missed
    result: str = ""  # why it failed / was missed
    created_at: datetime = Field(default_factory=_now)


class Setting(SQLModel, table=True):
    """Small admin-edited settings kept in the database (one row per key). Keys: `announcement` (app/announcement.py)."""

    __tablename__ = "settings"

    key: str = Field(primary_key=True)
    value: dict = Field(default_factory=dict, sa_type=JSON)


class Ban(SQLModel, table=True):
    """A Steam ID that may not stay on any server: kicked the moment it connects (app/bans.py)."""

    __tablename__ = "bans"

    guid: str = Field(primary_key=True)  # SteamID64
    name: str = ""  # who it was when banned
    reason: str = ""
    created_by: str = ""
    created_at: datetime = Field(default_factory=_now)


class Rsvp(SQLModel, table=True):
    """One Discord user's answer to a schedule's announcement, read from their reactions (schedule._rsvp)."""

    __tablename__ = "rsvps"

    schedule_id: int = Field(primary_key=True)  # no FK: schedule.delete removes the rows itself
    discord_id: str = Field(primary_key=True)
    user_id: int | None = Field(default=None, foreign_key="users.id", index=True)  # the linked account, None while the Discord user has not linked one
    status: str  # yes | maybe | no


class Penalty(SQLModel, table=True):
    """A steward's decision on one driver in one result file. The result file itself is never edited:
    penalties are applied when it is read (app.results.apply_penalties)."""

    __tablename__ = "penalties"

    id: int | None = Field(default=None, primary_key=True)
    server_id: int = Field(index=True)
    filename: str = Field(index=True)  # result JSON under data/instances/<server_id>/results/
    driver_guid: str
    kind: str  # time | position | dsq | grid | points
    value: int = 0  # time: ms added; position / grid: places lost; points: championship points taken; dsq: unused
    reason: str
    created_by: str = ""
    created_at: datetime = Field(default_factory=_now)


class Incident(SQLModel, table=True):
    """Something a driver did that the automatic stewards flagged (app/stewards). Append-only record; in shadow mode it is only evidence."""

    __tablename__ = "incidents"

    id: int | None = Field(default=None, primary_key=True)
    server_id: int = Field(index=True)
    ts: float = Field(index=True)  # epoch seconds
    session_type: int | None = None  # ACSP: 1 practice, 2 qualifying, 3 race
    session_name: str = ""
    session_ms: int = 0  # how long the session had run (LiveBoard.elapsed_ms)
    kind: str  # wall | contact | cuts
    car_id: int
    driver_guid: str | None = None
    driver_name: str = ""
    other_guid: str | None = None  # contact only
    other_name: str = ""
    speed: float = 0.0  # impact speed as acServer reports it (wall, contact)
    value: int = 0  # cuts: how many in the lap
    world_pos: list = Field(default_factory=list, sa_type=JSON)


class Activity(SQLModel, table=True):
    """What happened on the servers, for the metrics panel. Append-only; history cannot be rebuilt, so it starts
    accumulating from the day this exists. kinds: join, leave, lap, session, online (a sample of how many are on
    track, one a minute), server_start, server_stop, server_crash, import_ok, import_error, http_5xx."""

    __tablename__ = "activity"

    id: int | None = Field(default=None, primary_key=True)
    ts: float = Field(index=True)  # epoch seconds
    server_id: int = 0
    kind: str = Field(index=True)
    guid: str | None = None
    name: str | None = None  # driver / session / reason / path, depending on the kind
    car: str | None = None
    track: str | None = None
    value: float | None = None  # lap ms, players online, exit code, http status...
    cuts: int | None = None  # lap rows: track cuts in that lap (0 = valid); None for laps logged before this existed


DEFAULT_POINTS_SYSTEM = [25, 18, 15, 12, 10, 8, 6, 4, 2, 1]


class Championship(SQLModel, table=True):
    __tablename__ = "championships"

    id: int | None = Field(default=None, primary_key=True)
    name: str
    points_system: list[int] = Field(default_factory=lambda: list(DEFAULT_POINTS_SYSTEM), sa_type=JSON)
    practice_required: bool = False  # a roster driver needs `practice_laps` valid laps in the last `practice_days` days to be on the entry list (app/league.py)
    practice_laps: int = 5
    practice_days: int = 7
    penalties: list | None = Field(default=None, sa_type=JSON)  # the league's catalogue: [{name, seconds, dsq}]; the steward picks from it (app/penalties.py)
    created_at: datetime = Field(default_factory=_now)


class LeagueMember(SQLModel, table=True):
    """A driver on a league's roster, identified by Steam ID. The roster is the closed entry list of the league's races."""

    __tablename__ = "league_members"

    championship_id: int = Field(primary_key=True, foreign_key="championships.id")
    guid: str = Field(primary_key=True)  # SteamID64
    name: str = ""  # shown on the entry list
    team: str = ""
    car: str = ""  # preferred car model; the event's first car when it is not among the event's cars
    exempt: bool = False  # an admin lets this driver in whatever the practice requirement says
    non_racing: bool = False  # a car that is on the server but does not race (safety car, race director, caster): ignored by the grid penalty, the practice requirement and the standings


class LeagueSuspension(SQLModel, table=True):
    """A suspension or ban of one driver from a league's races (app/league.py). kinds: ban (until lifted), time (until `until`),
    races (out of the next `races_left` league races), qualy (no qualifying for `races_left` races: kicked while the server is in
    qualifying, so they start last), grid (`places_left` places lost on the grid, counted from where they qualified)."""

    __tablename__ = "league_suspensions"

    id: int | None = Field(default=None, primary_key=True)
    championship_id: int = Field(foreign_key="championships.id", index=True)
    guid: str = Field(index=True)
    name: str = ""
    kind: str
    until: float | None = None
    races_left: int = 0
    places_left: int = 0
    reason: str = ""
    created_by: str = ""
    created_at: datetime = Field(default_factory=_now)
    active: bool = True  # False once lifted or used up
    served: list = Field(default_factory=list, sa_type=JSON)  # qualifying result files already turned into a grid penalty


class ChampionshipEvent(SQLModel, table=True):
    __tablename__ = "championship_events"

    id: int | None = Field(default=None, primary_key=True)
    championship_id: int = Field(foreign_key="championships.id", index=True)
    server_id: int
    filename: str  # result JSON under data/instances/<server_id>/results/
    session_type: str | None = None  # "Race" (scores) or "Qualify" (feeds the grid penalties); None = added by hand, read as it is
    event_id: int | None = None  # the saved event whose run produced it (counted automatically, league.count_results); None = added by hand
    created_at: datetime = Field(default_factory=_now)
