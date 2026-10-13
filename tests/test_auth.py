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


def test_timezone_defaults_to_cdmx_and_is_validated():
    from conftest import ADMIN
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app, headers=ADMIN)
    assert c.get("/api/v1/auth/me").json()["timezone"] == "America/Mexico_City"
    assert c.patch("/api/v1/auth/me", json={"timezone": "Nope/Nowhere"}).status_code == 422
    assert c.patch("/api/v1/auth/me", json={"timezone": "Europe/Madrid"}).json()["timezone"] == "Europe/Madrid"


def test_a_reporter_can_only_file_track_limit_reports():
    """The website files the drivers' track-limit reports with ITS token. That token is a `reporter`, the lowest role: it reaches that one route and nothing else, so a
    leaked one cannot start servers, kick, ban, read configs (passwords), read leagues or manage users like the admin token it replaces."""
    admin = TestClient(app, headers=ADMIN)
    assert admin.post(f"{V}/users", json={"username": "rep", "password": "hunter2hunter2", "role": "reporter"}).status_code == 201
    login = _login("rep")
    token = login.post(f"{V}/auth/tokens", json={"name": "website"}).json()["token"]
    rep = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    assert rep.get(f"{V}/auth/me").json()["role"] == "reporter"           # a reporter can only mint reporter tokens: no escalation
    sid = admin.post(f"{V}/servers", json={"name": "rep-srv"}).json()["id"]
    body = {"car_id": 0, "name": "ana", "ms": 1800, "wheels": 4, "speed": 120.5, "lap": 2, "spline": 0.4, "pos": [1.0, 2.0, 3.0]}
    url = f"{V}/servers/{sid}/stewards/report"
    assert rep.post(url, json=body).status_code == 409                   # passed the role check: the server is simply not running
    assert TestClient(app).post(url, json=body).status_code == 401       # anonymous: no
    admin.post(f"{V}/users", json={"username": "drv-r", "password": "hunter2hunter2", "role": "driver"})
    assert _login("drv-r").post(url, json=body).status_code == 403       # a plain driver does not file reports
    admin.post(f"{V}/users", json={"username": "stw-r", "password": "hunter2hunter2", "role": "steward"})
    assert _login("stw-r").post(url, json=body).status_code == 409       # a steward does (passes the role check)
    for method, path, kw in (("get", "/servers", {}), ("get", f"/servers/{sid}", {}), ("post", f"/servers/{sid}/start", {}), ("post", f"/servers/{sid}/kick/0", {}),
                             ("get", f"/servers/{sid}/incidents", {}), ("put", f"/servers/{sid}/stewards", {"json": {"mode": "off"}}), ("get", "/users", {}),
                             ("post", "/users", {"json": {"username": "x", "password": "hunter2hunter2", "role": "admin"}}), ("get", "/bans", {}),
                             ("post", "/servers", {"json": {"name": "y"}}), ("get", "/championships", {}), ("get", "/content/cars", {})):
        r = getattr(rep, method)(f"{V}{path}", **kw)
        assert r.status_code == 403, (method, path, r.status_code)
