import sqlite3

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
