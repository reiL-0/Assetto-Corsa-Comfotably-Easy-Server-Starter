from conftest import ADMIN
from fastapi.testclient import TestClient

from app.main import app

V = "/api/v1"


def _login(name: str, pw: str = "hunter2hunter2") -> TestClient:
    c = TestClient(app)
    assert c.post(f"{V}/auth/login", json={"username": name, "password": pw}).status_code == 200
    return c


def test_rbac_matrix():
    anon = TestClient(app)
    admin = TestClient(app, headers=ADMIN)
    for role in ("driver", "steward"):
        r = admin.post(f"{V}/users", json={"username": role, "password": "hunter2hunter2", "role": role})
        assert r.status_code == 201, r.text

    assert anon.get(f"{V}/championships").status_code == 401
    assert anon.get(f"{V}/version").status_code == 200  # public

    driver, steward = _login("driver"), _login("steward")
    assert driver.get(f"{V}/championships").status_code == 200
    assert driver.post(f"{V}/championships", json={"name": "x"}).status_code == 403
    assert driver.get(f"{V}/servers").status_code == 403  # config carries ADMIN_PASSWORD
    assert steward.get(f"{V}/servers").status_code == 200
    assert steward.post(f"{V}/servers", json={"name": "x"}).status_code == 403
    assert driver.post(f"{V}/servers/1/kick/0").status_code == 403
    assert steward.post(f"{V}/servers/999/kick/0").status_code == 404  # passed the role check
    assert driver.get(f"{V}/users").status_code == 403


def test_api_token_and_logout():
    admin = TestClient(app, headers=ADMIN)
    tok = admin.post(f"{V}/auth/tokens", json={"name": "ci"}).json()
    bearer = TestClient(app, headers={"Authorization": f"Bearer {tok['token']}"})
    assert bearer.get(f"{V}/auth/me").json()["role"] == "admin"
    assert admin.delete(f"{V}/auth/tokens/{tok['id']}").status_code == 204
    assert bearer.get(f"{V}/auth/me").status_code == 401

    c = _login("driver")
    assert c.get(f"{V}/auth/me").status_code == 200
    c.post(f"{V}/auth/logout")
    assert c.get(f"{V}/auth/me").status_code == 401


def test_bad_login_and_dup_user():
    c = TestClient(app)
    assert c.post(f"{V}/auth/login", json={"username": "driver", "password": "nope"}).status_code == 401
    admin = TestClient(app, headers=ADMIN)
    dup = {"username": "driver", "password": "hunter2hunter2"}
    assert admin.post(f"{V}/users", json=dup).status_code == 409


def test_websocket_requires_auth():
    import pytest
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect), TestClient(app).websocket_connect(f"{V}/servers/1/live"):
        pass
