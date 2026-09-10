from datetime import UTC, datetime

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
    # Phase 1 replaces this blob with a structured server_cfg / entry_list model.
    config_json: str = "{}"
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
