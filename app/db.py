import logging
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings

log = logging.getLogger(__name__)

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
    """Creates missing tables from the models, then applies the pending migrations (MIGRATIONS) to existing data.
    Raises (and so stops the app in main.lifespan) if a migration fails: that one is rolled back and not recorded."""
    import app.models  # noqa: F401  (import registers the tables)
    from sqlalchemy import inspect

    if inspect(engine).get_table_names() and {v for v, _ in MIGRATIONS} - _applied():   # existing data about to change
        log.info("migrations pending: backup at %s", _backup())   # before create_all, so the copy is the database exactly as it was
    SQLModel.metadata.create_all(engine)   # only whole missing tables, empty and from the current models: harmless even if a migration then fails
    _migrate()


# --- migrations -------------------------------------------------------------
# SQL versioned by hand (one SQLite file, no Alembic). Each migration runs in its own BEGIN IMMEDIATE and its row in
# schema_migrations is written inside that same transaction, so a failure leaves neither half a change nor a "done" mark.
# New columns/backfills from now on go in as a new numbered migration, never through the generic one (001): it is what left
# Server.wake = '' on old rows. Code rollback does not undo these changes: to go back, restore the pre-migrate-*.db backup.

# Domain defaults of NOT NULL text columns: the generic migration 001 can only give '' to a column it adds.
_DOMAIN_DEFAULTS = {"wake": "window", "integrity": "warn", "stewards": "off"}   # servers.<column>: the model's default


def _m001_add_missing_columns(con: sqlite3.Connection) -> None:
    """Baseline: ALTER TABLE ... ADD COLUMN for every model column an older table lacks (create_all only creates whole tables).
    A NOT NULL column gets a constant by type (0 / '' / 'null' for JSON); the domain value is set by a later migration.
    Frozen: it reads the *current* models, which is only right for databases older than the migration system."""
    from sqlalchemy import JSON, Boolean, Float, Integer, Numeric

    for table in SQLModel.metadata.sorted_tables:
        have = {row[1] for row in con.execute(f'PRAGMA table_info("{table.name}")')}
        if not have:
            continue
        for col in table.columns:
            if col.name in have:
                continue
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(engine.dialect)}'
            if not col.nullable:
                numeric = isinstance(col.type, (Integer, Boolean, Float, Numeric))   # (python_type is not implemented for every type)
                ddl += " NOT NULL DEFAULT " + ("0" if numeric else "'null'" if isinstance(col.type, JSON) else "''")
            con.execute(ddl)


def _m002_domain_defaults_and_indexes(con: sqlite3.Connection) -> None:
    """Gives old servers rows the domain value where a column holds '' / NULL (valid values are kept), then creates the
    indexes the models declare and an old database lacks (incl. UNIQUE servers.base_port). Limit: SQLite cannot change a
    column's NOT NULL/DEFAULT in place; the model's default applies on insert, so that is not needed."""
    for column, value in _DOMAIN_DEFAULTS.items():
        con.execute(f'UPDATE servers SET "{column}" = ? WHERE "{column}" IS NULL OR "{column}" = \'\'', (value,))
    dup = con.execute("SELECT base_port FROM servers GROUP BY base_port HAVING COUNT(*) > 1").fetchall()
    if dup:   # not fixed automatically: renumbering ports would touch live servers
        raise RuntimeError(f"cannot add UNIQUE(servers.base_port): several servers share the ports {sorted(r[0] for r in dup)}; fix them by hand and restart")
    for table in SQLModel.metadata.sorted_tables:
        for idx in table.indexes:
            cols = [c.name for c in idx.columns]
            row = con.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?", (idx.name,)).fetchone()
            if row:   # same name must mean same definition, or the schemas silently differ
                found = [r[2] for r in con.execute(f'PRAGMA index_info("{idx.name}")')]
                unique = any(r[1] == idx.name and r[2] for r in con.execute(f'PRAGMA index_list("{table.name}")'))
                if found != cols or unique != bool(idx.unique):
                    raise RuntimeError(f"index {idx.name} exists with another definition (columns {found}, unique={unique}); expected {cols}, unique={bool(idx.unique)}")
                continue
            con.execute(f'CREATE {"UNIQUE " if idx.unique else ""}INDEX "{idx.name}" ON "{table.name}" ({", ".join(chr(34) + c + chr(34) for c in cols)})')


MIGRATIONS = [   # (version, function): append only, never edit or reorder one that has been released
    (1, _m001_add_missing_columns),
    (2, _m002_domain_defaults_and_indexes),
]


def _applied() -> set[int]:
    with engine.connect() as conn:
        if not conn.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name = 'schema_migrations'").first():
            return set()
        return {r[0] for r in conn.exec_driver_sql("SELECT version FROM schema_migrations")}


def _backup() -> Path | None:
    """Consistent copy (sqlite backup API: covers the WAL) in `backups/` next to the database."""
    src = engine.url.database
    if not src or src == ":memory:":
        return None
    dest = Path(src).parent / "backups" / f"pre-migrate-{datetime.now(UTC):%Y%m%d-%H%M%S-%f}.db"   # ponytail: never pruned; add retention if they pile up
    dest.parent.mkdir(parents=True, exist_ok=True)
    raw, out = engine.raw_connection(), sqlite3.connect(dest)
    try:
        raw.driver_connection.backup(out)
    finally:
        out.close()
        raw.close()
    return dest


def _migrate() -> None:
    raw = engine.raw_connection()
    con = raw.driver_connection
    old_mode, con.isolation_level = con.isolation_level, None   # autocommit: BEGIN/COMMIT are ours, DDL included
    try:
        con.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        done = {r[0] for r in con.execute("SELECT version FROM schema_migrations")}
        for version, fn in [(v, fn) for v, fn in MIGRATIONS if v not in done]:
            con.execute("BEGIN IMMEDIATE")
            try:
                if con.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (version,)).fetchone():   # another process won the race
                    con.execute("ROLLBACK")
                    continue
                fn(con)
                con.execute("INSERT INTO schema_migrations VALUES (?, ?)", (version, datetime.now(UTC).isoformat()))
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
            log.info("migration %s applied", version)
    finally:
        con.isolation_level = old_mode
        raw.close()


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]
