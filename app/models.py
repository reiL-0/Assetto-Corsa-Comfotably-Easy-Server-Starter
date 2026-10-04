from datetime import UTC, datetime

from sqlalchemy import JSON
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(UTC)


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    steam_id: str | None = Field(default=None, unique=True, index=True)
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
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class Event(SQLModel, table=True):
    """A saved session ("preset"): the new-session form's contents under a title, ready to run on any server."""

    __tablename__ = "events"

    id: int | None = Field(default=None, primary_key=True)
    title: str
    notes: str = ""
    data: dict = Field(default_factory=dict, sa_type=JSON)  # a servers.SessionIn, as JSON
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class Schedule(SQLModel, table=True):
    """A saved event set to start on a server at a given time, with Discord reminders before it (app.schedule)."""

    __tablename__ = "schedules"

    id: int | None = Field(default=None, primary_key=True)
    event_id: int
    server_id: int
    start_at: float = Field(index=True)  # unix seconds, UTC
    reminders: list[int] = Field(default_factory=lambda: [60, 10], sa_type=JSON)  # minutes before start_at
    sent: list[int] = Field(default_factory=list, sa_type=JSON)  # the reminders already posted
    state: str = "pending"  # pending | done | failed | missed
    result: str = ""  # why it failed / was missed
    created_at: datetime = Field(default_factory=_now)


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


DEFAULT_POINTS_SYSTEM = [25, 18, 15, 12, 10, 8, 6, 4, 2, 1]


class Championship(SQLModel, table=True):
    __tablename__ = "championships"

    id: int | None = Field(default=None, primary_key=True)
    name: str
    points_system: list[int] = Field(default_factory=lambda: list(DEFAULT_POINTS_SYSTEM), sa_type=JSON)
    created_at: datetime = Field(default_factory=_now)


class ChampionshipEvent(SQLModel, table=True):
    __tablename__ = "championship_events"

    id: int | None = Field(default=None, primary_key=True)
    championship_id: int = Field(foreign_key="championships.id", index=True)
    server_id: int
    filename: str  # result JSON under data/instances/<server_id>/results/
    created_at: datetime = Field(default_factory=_now)
