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
    assert _q(path, "SELECT version FROM schema_migrations ORDER BY version") == [(1,), (2,), (3,), (4,), (5,), (6,), (7,)]


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
    assert _q(tmp_path / "new.db", "SELECT version FROM schema_migrations ORDER BY version") == [(1,), (2,), (3,), (4,), (5,), (6,), (7,)]


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


# --- review A6/M5: migration 003 (catalog dedupe, UNIQUE holders, indexes) ------------------------------------------------

def _db_at_version_2(tmp_path, monkeypatch):
    """A real database as it is deployed today: migrations 001 and 002 applied, 003 not yet."""
    path = tmp_path / "v2.db"
    monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{path}"))
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:2])
    db.init_db()
    monkeypatch.undo()   # the engine goes back; the caller sets it again
    return path


def _event(con, at, actor, action, hash_, detail, holder="league"):
    con.execute("INSERT INTO catalog_events (at, actor, action, hash, holder, detail) VALUES (?, ?, ?, ?, ?, ?)", (at, actor, action, hash_, holder, detail))


def _holder(con, hash_, holder="league"):
    con.execute("INSERT INTO content_holders (hash, holder, uploaded_by, status, note, created_at, updated_at) VALUES (?, ?, '', 'active', '', '2026-10-01', '2026-10-01')", (hash_, holder))


def test_migration_003_removes_only_the_old_scan_repeats_and_adds_the_indexes(tmp_path, monkeypatch):
    path = _db_at_version_2(tmp_path, monkeypatch)
    con = sqlite3.connect(path)
    for i in range(300):   # the old loop: the same folder, again and again
        _event(con, f"2026-10-07 05:{i % 60:02d}:00.{i:06d}", "scan", "upload", "h1", "car a (3 files, 9 bytes)")
        _event(con, f"2026-10-07 06:{i % 60:02d}:00.{i:06d}", "scan", "upload", "h2", "car b (1 files, 2 bytes)")
    _event(con, "2026-10-07 07:00:00.000000", "scan", "upload", "h1", "car a with another detail")      # different detail: kept
    _event(con, "2026-10-12 01:00:00.000000", "scan", "upload", "h1", "car a (3 files, 9 bytes)")        # after the fix date: kept
    _event(con, "2026-10-07 05:30:00.000000", "upload", "upload", "h1", "car a (3 files, 9 bytes)")      # another actor: kept
    _event(con, "2026-10-07 05:31:00.000000", "scan", "scan", "h1", "car a (3 files, 9 bytes)")          # another action: kept
    _event(con, "2026-10-07 05:32:00.000000", "admin", "source", "h1", "https://example.test")
    _holder(con, "h1")
    con.commit()
    first = {r[0]: r[1] for r in con.execute("SELECT hash, MIN(id) FROM catalog_events WHERE actor='scan' AND action='upload' GROUP BY hash")}
    con.close()
    monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{path}"))
    db.init_db()
    rows = _q(path, "SELECT id, hash, detail, actor, action, at FROM catalog_events ORDER BY id")
    scan_upload = [r for r in rows if r[3] == "scan" and r[4] == "upload"]
    assert len(rows) == 2 + 1 + 1 + 1 + 1 + 1 and len(scan_upload) == 4   # h1+h2 once each, the other detail, the post-fix repeat
    assert {r[1]: r[0] for r in scan_upload if r[2] in ("car a (3 files, 9 bytes)", "car b (1 files, 2 bytes)") and r[5] < "2026-10-08"} == first   # the FIRST of each survives
    assert {(r[3], r[4]) for r in rows} == {("scan", "upload"), ("upload", "upload"), ("scan", "scan"), ("admin", "source")}
    assert [r[0] for r in _q(path, "SELECT version FROM schema_migrations ORDER BY version")] == [1, 2, 3, 4, 5, 6, 7]
    names = {r[0] for r in _q(path, "SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"ux_content_holders_hash_holder", "ix_catalog_events_hash_id", "ix_activity_server_ts", "ix_incidents_server_ts"} <= names
    plan = " ".join(str(r) for r in _q(path, "EXPLAIN QUERY PLAN SELECT * FROM catalog_events WHERE hash = 'h1' ORDER BY id DESC LIMIT 5"))
    assert "ix_catalog_events_hash_id" in plan
    with pytest.raises(sqlite3.IntegrityError):   # the new rule is really enforced
        con = sqlite3.connect(path)
        _holder(con, "h1")
    before = _q(path, "SELECT * FROM catalog_events")
    db.init_db()   # rerun: nothing changes
    assert _q(path, "SELECT * FROM catalog_events") == before


def test_migration_003_stops_on_duplicate_holders_and_changes_nothing(tmp_path, monkeypatch):
    path = _db_at_version_2(tmp_path, monkeypatch)
    con = sqlite3.connect(path)
    for i in range(5):
        _event(con, f"2026-10-07 05:00:0{i}.000000", "scan", "upload", "h1", "car a (3 files, 9 bytes)")
    _holder(con, "h1")
    _holder(con, "h1")   # two rows for the same (hash, holder)
    con.commit()
    con.close()
    monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{path}"))
    with pytest.raises(RuntimeError, match=r"UNIQUE\(content_holders.hash, holder\).*1 pair"):
        db.init_db()
    assert len(_q(path, "SELECT id FROM catalog_events")) == 5   # the dedupe is not applied either
    assert 3 not in {r[0] for r in _q(path, "SELECT version FROM schema_migrations")}
    assert "ix_catalog_events_hash_id" not in {r[0] for r in _q(path, "SELECT name FROM sqlite_master WHERE type = 'index'")}


def test_migration_003_refuses_an_index_that_has_its_name_but_another_definition(tmp_path, monkeypatch):
    path = _db_at_version_2(tmp_path, monkeypatch)
    con = sqlite3.connect(path)
    con.execute("CREATE INDEX ix_activity_server_ts ON activity (kind)")
    con.commit()
    con.close()
    monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{path}"))
    with pytest.raises(RuntimeError, match="another definition"):
        db.init_db()
    assert 3 not in {r[0] for r in _q(path, "SELECT version FROM schema_migrations")}


def test_a_fresh_database_gets_the_same_indexes_as_an_upgraded_one(tmp_path, monkeypatch):
    old = _db_at_version_2(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{old}"))
    db.init_db()
    fresh = tmp_path / "fresh3.db"
    monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{fresh}"))
    db.init_db()
    names = lambda p: {(r[0], r[1]) for r in _q(p, "SELECT tbl_name, name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'")}
    assert names(old) == names(fresh)


@pytest.mark.parametrize("ddl", [
    "CREATE UNIQUE INDEX ux_content_holders_hash_holder ON content_holders (hash, holder) WHERE status = 'active'",   # unique, but only for some rows
    "CREATE INDEX ux_content_holders_hash_holder ON content_holders (hash, holder)",                                  # right columns, not unique
    "CREATE INDEX ix_catalog_events_hash_id ON content_holders (hash, id)",                                         # right name and columns, another table
])
def test_migration_003_refuses_look_alike_indexes(tmp_path, monkeypatch, ddl):
    path = _db_at_version_2(tmp_path, monkeypatch)
    con = sqlite3.connect(path)
    con.execute(ddl)
    con.commit()
    con.close()
    monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{path}"))
    with pytest.raises(RuntimeError, match="another definition"):
        db.init_db()
    assert 3 not in {r[0] for r in _q(path, "SELECT version FROM schema_migrations")}


# --- migration 004: archiving a league ------------------------------------------------------------------------------------

def test_migration_004_adds_the_archive_columns_and_keeps_every_running_league_running(tmp_path, monkeypatch):
    path = tmp_path / "v3.db"
    monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{path}"))
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:3])
    db.init_db()
    con = sqlite3.connect(path)   # a deployed database before 004: a league without the archive columns
    con.execute("ALTER TABLE championships DROP COLUMN archived_at")
    con.execute("ALTER TABLE championships DROP COLUMN archived_by")
    con.execute("DROP TABLE championship_archive")
    con.execute("INSERT INTO championships (id, name, points_system, practice_required, practice_laps, practice_days, created_at) VALUES (1, 'Vieja', '[25, 18]', 0, 5, 7, '2026-01-01')")
    con.commit()
    con.close()
    monkeypatch.undo()
    monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{path}"))
    db.init_db()
    assert _q(path, "SELECT id, name, archived_at, archived_by FROM championships") == [(1, "Vieja", None, "")], "an existing league must stay running"
    assert _q(path, "SELECT name FROM sqlite_master WHERE name = 'championship_archive'") == [("championship_archive",)]
    stamp = _q(path, "SELECT version, applied_at FROM schema_migrations ORDER BY version")
    assert [r[0] for r in stamp] == [1, 2, 3, 4, 5, 6, 7]
    db.init_db()   # rerun
    assert _q(path, "SELECT version, applied_at FROM schema_migrations ORDER BY version") == stamp


# --- migration 005: automatic penalties ------------------------------------------------------------------------------------

def test_migration_005_keeps_old_penalties_manual_and_allows_one_automatic_per_driver_and_file(tmp_path, monkeypatch):
    path = tmp_path / "v4.db"
    monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{path}"))
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:4])
    db.init_db()
    con = sqlite3.connect(path)   # a deployed database before 005: penalties and leagues without the new columns
    con.execute("DROP TABLE penalties")
    con.execute("CREATE TABLE penalties (id INTEGER PRIMARY KEY, server_id INTEGER NOT NULL, filename VARCHAR NOT NULL, driver_guid VARCHAR NOT NULL, kind VARCHAR NOT NULL,"
                " value INTEGER NOT NULL, reason VARCHAR NOT NULL, created_by VARCHAR NOT NULL, created_at DATETIME NOT NULL)")
    con.execute("INSERT INTO penalties VALUES (1, 1, 'r.json', '7656', 'time', 5000, 'old', 'root', '2026-01-01')")
    con.execute("ALTER TABLE championships DROP COLUMN auto_rules")
    con.commit()
    con.close()
    monkeypatch.undo()
    monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{path}"))
    db.init_db()
    assert _q(path, "SELECT id, auto_key, dismissed FROM penalties") == [(1, None, 0)], "an existing penalty is a steward's own and counts"
    con = sqlite3.connect(path)
    con.execute("INSERT INTO penalties VALUES (2, 1, 'r.json', '7656', 'time', 5000, 'again', 'root', '2026-01-01', NULL, 0)")   # manual ones repeat freely
    con.execute("INSERT INTO penalties VALUES (3, 1, 'r.json', '7656', 'time', 5000, 'auto', 'auto', '2026-01-01', 'cuts', 0)")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO penalties VALUES (4, 1, 'r.json', '7656', 'time', 5000, 'auto', 'auto', '2026-01-01', 'cuts', 0)")
    con.close()
    assert _q(path, "SELECT auto_rules FROM championships") == []
    db.init_db()   # rerun
    assert [r[0] for r in _q(path, "SELECT version FROM schema_migrations ORDER BY version")] == [1, 2, 3, 4, 5, 6, 7]
