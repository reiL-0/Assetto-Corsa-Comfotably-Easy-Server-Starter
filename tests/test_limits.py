from conftest import ADMIN
from fastapi.testclient import TestClient

from app import supervisor
from app.config import settings
from app.main import app

api = TestClient(app, headers=ADMIN)
V = "/api/v1"


def test_limit_prefix_wraps_acserver_in_a_limited_scope_only_when_enforced(monkeypatch):
    monkeypatch.setattr(settings, "limits_scope", "")
    assert supervisor.limit_prefix(3, 150, 1024) == []                                    # not enforced here (dev machine)
    monkeypatch.setattr(settings, "limits_scope", "user")
    assert supervisor.limit_prefix(3, None, None) == []                                   # nothing to limit
    p = supervisor.limit_prefix(3, 150, 1024)
    assert p[:2] == ["systemd-run", "--scope"] and "--user" in p and "--unit=acserver-3" in p
    assert "CPUQuota=150%" in p and "MemoryMax=1024M" in p
    monkeypatch.setattr(settings, "limits_scope", "system")
    assert "--user" not in supervisor.limit_prefix(3, 100, None) and "MemoryMax" not in " ".join(supervisor.limit_prefix(3, 100, None))


def test_limits_endpoint_validates_stores_and_clears():
    sid = api.post(f"{V}/servers", json={"name": "Limits"}).json()["id"]
    assert api.get(f"{V}/servers/{sid}").json()["limits"]["cpu_percent"] is None
    assert api.put(f"{V}/servers/{sid}/limits", json={"cpu_percent": 5}).status_code == 422
    assert api.put(f"{V}/servers/{sid}/limits", json={"mem_mb": 100}).status_code == 422
    lim = api.put(f"{V}/servers/{sid}/limits", json={"cpu_percent": 200, "mem_mb": 2048}).json()["limits"]
    assert (lim["cpu_percent"], lim["mem_mb"]) == (200, 2048)
    assert api.put(f"{V}/servers/{sid}/limits", json={}).json()["limits"]["mem_mb"] is None     # empty = unlimited again
