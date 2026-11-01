"""The cars feed the website's lap recorder reads (app/live/feed.py): only connected cars with a Steam ID, the server's own numbers, and only for the roles that may read it."""

import secrets
from datetime import UTC, datetime

from conftest import ADMIN
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import supervisor
from app.auth import _sha
from app.db import engine
from app.live import acsp
from app.main import app
from app.models import Token, User

V = "/api/v1"


class _Inst:
    running = True

    def __init__(self, sid, client):
        self.server_id, self.acsp = sid, client


def _client(role):
    raw = secrets.token_urlsafe(16)
    with Session(engine) as s:
        u = User(username=f"{role}-{raw[:5]}", role=role)
        s.add(u)
        s.commit()
        s.add(Token(user_id=u.id, token_hash=_sha(raw), name="t", created_at=datetime.now(UTC)))
        s.commit()
    return TestClient(app, headers={"Authorization": f"Bearer {raw}"})


def test_the_feed_lists_connected_cars_with_the_servers_own_numbers():
    c = acsp.ACSPClient(902)
    c.board.apply({"type": "new_session", "session_type": 1, "track": "spa", "track_config": "", "server_name": "P2", "session_index": 3})
    for car_id, guid in ((0, "76561190000000001"), (1, ""), (2, "76561190000000003")):
        c.board.apply({"type": "new_connection", "car_id": car_id, "driver_name": f"d{car_id}", "driver_guid": guid, "car_model": "gt3", "car_skin": "s"})
    c.board.apply({"type": "connection_closed", "car_id": 2})   # gone: not listed
    c.cars[0] = {"velocity": [30.0, 0.0, 40.0]}                 # 50 m/s = 180 km/h
    c.board.apply({"type": "car_update", "car_id": 0, "pos": [0, 0, 0], "velocity": [30.0, 0.0, 40.0], "spline_pos": 0.41})
    c.board.apply({"type": "lap_completed", "car_id": 0, "laptime_ms": 103420, "cuts": 0, "leaderboard": [{"car_id": 0, "laps": 1, "laptime_ms": 103420}]})
    supervisor._instances[902] = _Inst(902, c)
    try:
        r = TestClient(app, headers=ADMIN).get(f"{V}/telemetry-feed")
        assert r.status_code == 200
        srv = next(s for s in r.json()["servers"] if s["serverId"] == 902)
        assert srv["session"] == {"type": 1, "track": "spa", "trackConfig": "", "serverName": "P2", "index": 3}
        assert srv["cars"] == [{"carId": 0, "name": "d0", "guid": "76561190000000001", "connected": True, "splinePos": 0.41, "carModel": "gt3", "speedKmh": 180.0, "laps": 1, "lastLapMs": 103420}]
        c.cars.pop(0)   # no position packet yet: no speed, never a crash
        again = next(s for s in TestClient(app, headers=ADMIN).get(f"{V}/telemetry-feed").json()["servers"] if s["serverId"] == 902)
        assert again["cars"][0]["speedKmh"] is None
        for role in ("reporter", "driver", "steward"):
            assert _client(role).get(f"{V}/telemetry-feed").status_code == 200, role
        assert TestClient(app).get(f"{V}/telemetry-feed").status_code in (401, 403)
    finally:
        supervisor._instances.pop(902, None)
