
from fastapi.testclient import TestClient

from app import supervisor
from app.live import acsp
from app.main import app

V = "/api/v1"
SAMPLE = {
    "steamId": "7656",
    "pos": {"x": 1.0, "y": 0.0, "z": 2.0},
    "rotation": {"x": 0.5, "y": 0.0, "z": 0.0},
    "speedKmh": 180.0, "gear": 4, "rpm": 6000,
    "throttle": 0.8, "brake": 0.0, "clutch": 1.0, "steerAngle": 5.0,
}

class _Inst:  # stands in for a running supervisor.Instance
    running = True

    def __init__(self, client):
        self.acsp = client

def test_ingest_by_steam_id_no_auth():
    api = TestClient(app)  # no token, no cookie
    assert api.post(f"{V}/telemetry/ingest", json=SAMPLE).status_code == 409  # not connected

    client = acsp.ACSPClient(1)
    client.cars[3] = {"car_id": 3, "driver_guid": "7656", "pos": [0, 0, 0]}
    supervisor._instances[1] = _Inst(client)
    try:
        assert api.post(f"{V}/telemetry/ingest", json=SAMPLE).status_code == 204
        assert api.post(f"{V}/telemetry/ingest", json=SAMPLE).status_code == 429  # too fast
        other = {**SAMPLE, "steamId": "1111"}
        assert api.post(f"{V}/telemetry/ingest", json=other).status_code == 409  # someone else
        assert client.snapshot()[3]["telemetry"]["throttle"] == 0.8
        assert "steamId" not in client.snapshot()[3]["telemetry"]
        assert client.events[-1]["type"] == "telemetry"
        client.telemetry[3]["ts"] -= 10  # stale -> dropped from the live map
        assert "telemetry" not in client.snapshot()[3]
    finally:
        supervisor._instances.pop(1)

def test_feed_cursor_survives_full_deque():
    c = acsp.ACSPClient(2, max_events=3)
    for i in range(10):
        c._push({"type": "chat", "n": i})
    assert c.n_events == 10 and len(c.events) == 3
