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


def _ensure_index(con: sqlite3.Connection, name: str, table: str, columns: tuple[str, ...], unique: bool = False) -> None:
    """Explicit DDL (migrations after 002 never read the models). An index with this name must already be exactly this one, or it is refused."""
    row = con.execute("SELECT tbl_name FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)).fetchone()
    if row:
        found = tuple(r[2] for r in con.execute(f'PRAGMA index_info("{name}")'))
        listed = [r for r in con.execute(f'PRAGMA index_list("{table}")') if r[1] == name]   # (seq, name, unique, origin, partial)
        is_unique, partial = bool(listed and listed[0][2]), bool(listed and listed[0][4])
        if row[0] != table or found != columns or is_unique != unique or partial:
            raise RuntimeError(f"index {name} exists with another definition (table {row[0]}, columns {found}, unique={is_unique}, partial={partial}); "
                               f"expected table {table}, columns {columns}, unique={unique}, not partial")
        return
    con.execute(f'CREATE {"UNIQUE " if unique else ""}INDEX "{name}" ON "{table}" ({", ".join(chr(34) + c + chr(34) for c in columns)})')


_LEGACY_SCAN_BEFORE = "2026-10-08"   # the scan bug (every pass logged `upload` for the same folder) was fixed on 2026-10-07; nothing later is touched


def _m003_catalog_dedupe_and_indexes(con: sqlite3.Connection) -> None:
    """Review A6/M5. (1) Stops if content_holders already has two rows for one (hash, holder), then makes that UNIQUE. (2) Removes the repeats
    the old scan loop left in catalog_events: only actor 'scan' + action 'upload' + before the fix date, keeping the FIRST row (lowest id, so
    its time) of each (hash, holder, detail); every other actor/action and date is left alone. (3) Indexes for the queries that do scan today
    (catalog events by hash; activity and incidents by server and time). It does not VACUUM: that cannot run inside the transaction."""
    dup = con.execute("SELECT hash, holder, COUNT(*), GROUP_CONCAT(id) FROM content_holders GROUP BY hash, holder HAVING COUNT(*) > 1").fetchall()
    if dup:   # merging holders would decide who owns what: never automatic
        shown = "; ".join(f"{h[:12]}…/{holder}: ids {ids}" for h, holder, _, ids in dup[:5])
        raise RuntimeError(f"cannot add UNIQUE(content_holders.hash, holder): {len(dup)} pair(s) are duplicated ({shown}); fix them by hand and restart")
    con.execute("DELETE FROM catalog_events WHERE actor = 'scan' AND action = 'upload' AND at < ? AND id NOT IN ("
                "SELECT MIN(id) FROM catalog_events WHERE actor = 'scan' AND action = 'upload' AND at < ? GROUP BY hash, holder, detail)",
                (_LEGACY_SCAN_BEFORE, _LEGACY_SCAN_BEFORE))
    _ensure_index(con, "ux_content_holders_hash_holder", "content_holders", ("hash", "holder"), unique=True)
    _ensure_index(con, "ix_catalog_events_hash_id", "catalog_events", ("hash", "id"))
    _ensure_index(con, "ix_activity_server_ts", "activity", ("server_id", "ts"))
    _ensure_index(con, "ix_incidents_server_ts", "incidents", ("server_id", "ts"))


def _m004_championship_archive(con: sqlite3.Connection) -> None:
    """A league can be archived (app/archive.py): two columns on `championships` (NULL / '' = a running league, which every existing row is). The snapshot table
    `championship_archive` is a whole new table: create_all made it just before the migrations ran."""
    have = {row[1] for row in con.execute('PRAGMA table_info("championships")')}
    if "archived_at" not in have:
        con.execute('ALTER TABLE "championships" ADD COLUMN "archived_at" DATETIME')
    if "archived_by" not in have:
        con.execute('ALTER TABLE "championships" ADD COLUMN "archived_by" VARCHAR NOT NULL DEFAULT \'\'')


def _m005_auto_penalties(con: sqlite3.Connection) -> None:
    """Automatic stewards: `penalties.auto_key` (NULL = a steward's own, every existing row) and `penalties.dismissed` (0), `championships.auto_rules` (NULL = off), and
    one automatic penalty per (file, driver, key). NULLs never collide in a UNIQUE index, so the manual penalties are free to repeat."""
    def add(table: str, column: str, ddl: str) -> None:
        if column not in {row[1] for row in con.execute(f'PRAGMA table_info("{table}")')}:
            con.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ddl}')
    add("penalties", "auto_key", "VARCHAR")
    add("penalties", "dismissed", "BOOLEAN NOT NULL DEFAULT 0")
    add("championships", "auto_rules", "JSON")
    _ensure_index(con, "ux_penalties_auto", "penalties", ("server_id", "filename", "driver_guid", "auto_key"), unique=True)


def _m006_rulebook(con: sqlite3.Connection) -> None:
    """`championships.rulebook` (NULL = the league has none: its rules are derived from points_system / penalties / auto_rules, so every existing league behaves as before)."""
    if "rulebook" not in {row[1] for row in con.execute('PRAGMA table_info("championships")')}:
        con.execute('ALTER TABLE "championships" ADD COLUMN "rulebook" JSON')


MIGRATIONS = [   # (version, function): append only, never edit or reorder one that has been released
    (1, _m001_add_missing_columns),
    (2, _m002_domain_defaults_and_indexes),
    (3, _m003_catalog_dedupe_and_indexes),
    (4, _m004_championship_archive),
    (5, _m005_auto_penalties),
    (6, _m006_rulebook),
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
