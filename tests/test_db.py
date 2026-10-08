import sqlite3

import pytest
from sqlmodel import create_engine

from app import db


def test_init_db_adds_the_columns_an_existing_table_lacks(tmp_path, monkeypatch):
    """The deployed database has tables from older versions: new model columns are added, whatever their type, and rows survive."""
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE schedules (id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL, server_id INTEGER NOT NULL, start_at FLOAT NOT NULL,"
                " reminders JSON NOT NULL, sent JSON NOT NULL, state VARCHAR NOT NULL, result VARCHAR NOT NULL, created_at DATETIME NOT NULL)")
    con.execute("INSERT INTO schedules VALUES (1, 2, 3, 99.0, '[60]', '[]', 'pending', '', '2026-01-01')")
    con.commit()
    con.close()
    monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{path}"))
    db.init_db()
    con = sqlite3.connect(path)
    cols = {r[1]: r for r in con.execute("PRAGMA table_info(schedules)")}
    assert {"info", "duration_min", "loaded", "end_warned"} <= set(cols)
    assert con.execute("SELECT info, duration_min, loaded, end_warned FROM schedules WHERE id=1").fetchone() == ("", None, 0, 0)
    db.init_db()   # idempotent


# --- T1.1: versioned migrations --------------------------------------------------------------------------------------

def _old_servers_db(tmp_path, monkeypatch, rows, with_wake=False):
    """An older database: `servers` from before wake/integrity/stewards and without UNIQUE(base_port)."""
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    extra = ", wake VARCHAR NOT NULL DEFAULT ''" if with_wake else ""
    con.execute(f"CREATE TABLE servers (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL, base_port INTEGER NOT NULL{extra})")
    con.executemany(f"INSERT INTO servers (id, name, base_port{', wake' if with_wake else ''}) VALUES ({'?, ?, ?, ?' if with_wake else '?, ?, ?'})", rows)
    con.commit()
    con.close()
    monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{path}"))
    return path


def _q(path, sql):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _indexes(path):
    return {(r[1], r[2]) for t in ("servers", "users", "schedules") for r in _q(path, f'PRAGMA index_list("{t}")') if not r[1].startswith("sqlite_")}


def test_old_servers_keep_rows_and_get_the_domain_values(tmp_path, monkeypatch):
    """Rows survive; wake/integrity/stewards that the generic step could only fill with '' get the domain default; valid values stay."""
    path = _old_servers_db(tmp_path, monkeypatch, [(1, "a", 9600, ""), (2, "b", 9610, "off"), (3, "c", 9620, "always")], with_wake=True)
    db.init_db()
    rows = _q(path, "SELECT id, name, base_port, wake, integrity, stewards FROM servers ORDER BY id")
    assert rows == [(1, "a", 9600, "window", "warn", "off"), (2, "b", 9610, "off", "warn", "off"), (3, "c", 9620, "always", "warn", "off")]
    assert _q(path, "SELECT version FROM schema_migrations ORDER BY version") == [(1,), (2,)]


def test_upgraded_and_fresh_databases_have_the_same_indexes(tmp_path, monkeypatch):
    old = _old_servers_db(tmp_path, monkeypatch, [(1, "a", 9600)])
    db.init_db()
    fresh = tmp_path / "fresh.db"
    monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{fresh}"))
    db.init_db()
    assert ("uq_servers_base_port", 1) in _indexes(old)
    assert _indexes(old) == _indexes(fresh)


def test_rerunning_changes_nothing_and_backs_up_only_once(tmp_path, monkeypatch):
    path = _old_servers_db(tmp_path, monkeypatch, [(1, "a", 9600)])
    db.init_db()
    before = (_q(path, "SELECT * FROM servers"), _q(path, "SELECT version, applied_at FROM schema_migrations"))
    backups = sorted((tmp_path / "backups").glob("pre-migrate-*.db"))
    assert len(backups) == 1 and _q(backups[0], "SELECT name FROM servers") == [("a",)]   # a usable copy of the data as it was
    assert _q(backups[0], "SELECT name FROM sqlite_master WHERE name = 'schema_migrations'") == []   # ...taken before anything was created or changed
    db.init_db()
    assert (_q(path, "SELECT * FROM servers"), _q(path, "SELECT version, applied_at FROM schema_migrations")) == before
    assert len(list((tmp_path / "backups").glob("pre-migrate-*.db"))) == 1


def test_fresh_database_is_not_backed_up(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{tmp_path / 'new.db'}"))
    db.init_db()
    assert not (tmp_path / "backups").exists()
    assert _q(tmp_path / "new.db", "SELECT version FROM schema_migrations ORDER BY version") == [(1,), (2,)]


def test_failed_migration_is_rolled_back_and_not_recorded(tmp_path, monkeypatch):
    """Two servers on the same ports: the UNIQUE index cannot be built. Nothing of migration 002 stays, and it is not marked done."""
    path = _old_servers_db(tmp_path, monkeypatch, [(1, "a", 9600, ""), (2, "b", 9600, "")], with_wake=True)
    with pytest.raises(RuntimeError, match="9600"):
        db.init_db()
    assert _q(path, "SELECT wake FROM servers") == [("",), ("",)]   # the backfill of the same migration was undone too
    assert [r[1] for r in _q(path, "PRAGMA index_list(servers)")] == []
    assert 2 not in {r[0] for r in _q(path, "SELECT version FROM schema_migrations")}


def test_an_index_with_the_same_name_but_another_definition_is_refused(tmp_path, monkeypatch):
    path = _old_servers_db(tmp_path, monkeypatch, [(1, "a", 9600)])
    con = sqlite3.connect(path)
    con.execute("CREATE INDEX uq_servers_base_port ON servers (name)")   # right name, wrong columns and not unique
    con.commit()
    con.close()
    with pytest.raises(RuntimeError, match="another definition"):
        db.init_db()
