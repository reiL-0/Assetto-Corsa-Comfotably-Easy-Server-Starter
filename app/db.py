from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

from fastapi import Depends
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings

Path(settings.data_dir).mkdir(parents=True, exist_ok=True)

engine = create_engine(
    f"sqlite:///{settings.resolved_db_path()}",
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _connection_record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


def init_db() -> None:
    # Phase 1: create tables straight from the models.
    # Alembic goes in once there is data worth preserving across schema changes.
    import app.models  # noqa: F401  (import registers the tables)

    SQLModel.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """Forward-only migration: ALTER TABLE ... ADD COLUMN for model columns an existing table lacks (create_all only creates
    whole tables). A NOT NULL column needs a constant default: 0 / '' / '[]' by type."""
    from sqlalchemy import JSON, Boolean, Float, Integer, Numeric, inspect

    insp = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(engine.dialect)}'
                if not col.nullable:
                    numeric = isinstance(col.type, (Integer, Boolean, Float, Numeric))   # (python_type is not implemented for every type)
                    ddl += " NOT NULL DEFAULT " + ("0" if numeric else "'null'" if isinstance(col.type, JSON) else "''")
                conn.exec_driver_sql(ddl)


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]
