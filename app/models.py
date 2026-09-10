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
