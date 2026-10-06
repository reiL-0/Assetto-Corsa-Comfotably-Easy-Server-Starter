from conftest import ADMIN
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import servers, tenancy
from app.db import engine
from app.main import app

root = TestClient(app, headers=ADMIN)
V = "/api/v1"


def _tenant(name: str, **plan) -> TestClient:
    """A customer with its own plan and an admin account, logged in."""
    p = root.post(f"{V}/plans", json={"name": f"plan-{name}", "max_servers": 2, "slots": 4, "cpu_percent": 150, "mem_mb": 1024, **plan}).json()
    t = root.post(f"{V}/tenants", json={"name": name, "plan_id": p["id"]}).json()
    root.post(f"{V}/tenants/{t['id']}/users", json={"username": f"u-{name}", "password": "password-123", "role": "admin"})
    c = TestClient(app)
    assert c.post(f"{V}/auth/login", json={"username": f"u-{name}", "password": "password-123"}).status_code == 200
    c.tenant_id, c.plan_id = t["id"], p["id"]
    return c


def test_a_customer_creates_servers_up_to_its_plan_and_gets_the_plan_limits():
    a = _tenant("acme")
    s1 = a.post(f"{V}/servers", json={"name": "A1"})
    assert s1.status_code == 201 and s1.json()["tenant_id"] == a.tenant_id
    assert s1.json()["limits"]["cpu_percent"] == 150 and s1.json()["limits"]["mem_mb"] == 1024
    assert a.post(f"{V}/servers", json={"name": "A2"}).status_code == 201
    r = a.post(f"{V}/servers", json={"name": "A3"})
    assert r.status_code == 403 and "plan allows 2" in r.text


def test_customers_cannot_see_or_touch_each_others_servers_or_our_admin_routes():
    a, b = _tenant("iso-a"), _tenant("iso-b")
    sa = a.post(f"{V}/servers", json={"name": "SA"}).json()["id"]
    sb = b.post(f"{V}/servers", json={"name": "SB"}).json()["id"]
    assert [x["id"] for x in a.get(f"{V}/servers").json()] == [sa]
    assert a.get(f"{V}/servers/{sb}").status_code == 404                       # not 403: ids do not leak
    assert a.put(f"{V}/servers/{sb}/wake", json={"mode": "off"}).status_code == 404
    assert a.post(f"{V}/servers/{sb}/start").status_code == 404
    assert a.delete(f"{V}/servers/{sb}").status_code == 404
    for path in ("/users", "/plans", "/tenants", "/championships", "/content/cars", "/events", "/catalog", "/schedules", "/metrics/now", "/announcement", "/requirements", "/integrity/check"):
        assert a.get(f"{V}{path}").status_code == 403, path                     # a route not whitelisted for customers
    assert a.put(f"{V}/servers/{sa}/limits", json={"cpu_percent": 800}).status_code == 403        # limits come from the plan
    assert a.patch(f"{V}/servers/{sa}", json={"name": "renamed"}).status_code == 200
    assert {x["id"] for x in root.get(f"{V}/servers").json()} >= {sa, sb}        # our staff see everything


def test_a_suspended_customer_is_locked_out_and_can_come_back():
    a = _tenant("susp")
    assert a.get(f"{V}/servers").status_code == 200
    assert root.patch(f"{V}/tenants/{a.tenant_id}", json={"status": "suspended"}).status_code == 200
    r = a.get(f"{V}/servers")
    assert r.status_code == 403 and "suspended" in r.text
    root.patch(f"{V}/tenants/{a.tenant_id}", json={"status": "active"})
    assert a.get(f"{V}/servers").status_code == 200


def test_a_per_server_token_works_on_that_server_only():
    a = _tenant("tok")
    s1 = a.post(f"{V}/servers", json={"name": "T1"}).json()["id"]
    s2 = a.post(f"{V}/servers", json={"name": "T2"}).json()["id"]
    tok = a.post(f"{V}/servers/{s1}/tokens", json={"name": "ci"}).json()
    c = TestClient(app, headers={"Authorization": f"Bearer {tok['token']}"})
    assert c.get(f"{V}/servers/{s1}").status_code == 200 and c.put(f"{V}/servers/{s1}/wake", json={"mode": "off"}).status_code == 200
    assert c.get(f"{V}/servers/{s2}").status_code == 404                        # the same customer's other server is out of reach
    assert [x["id"] for x in c.get(f"{V}/servers").json()] == [s1]
    assert c.post(f"{V}/servers", json={"name": "new"}).status_code == 403 and c.delete(f"{V}/servers/{s1}").status_code == 403
    assert c.post(f"{V}/servers/{s1}/tokens", json={"name": "more"}).status_code == 403   # it cannot mint tokens
    assert len(a.get(f"{V}/servers/{s1}/tokens").json()) == 1
    assert a.delete(f"{V}/servers/{s1}/tokens/{tok['id']}").status_code == 204
    assert c.get(f"{V}/servers/{s1}").status_code == 401                         # revoked
    other = _tenant("tok-b")
    assert other.post(f"{V}/servers/{s1}/tokens", json={"name": "x"}).status_code == 404   # not their server


def test_only_our_admins_manage_plans_and_customers():
    a = _tenant("nomgr")
    assert a.post(f"{V}/plans", json={"name": "mine"}).status_code == 403 and a.post(f"{V}/tenants", json={"name": "x", "plan_id": a.plan_id}).status_code == 403
    assert root.delete(f"{V}/plans/{a.plan_id}").status_code == 409                # customers are on it
    assert root.post(f"{V}/tenants", json={"name": "nomgr", "plan_id": a.plan_id}).status_code == 409
    assert root.delete(f"{V}/tenants/{a.tenant_id}").status_code == 409            # it still has a user


def test_a_customer_config_cannot_take_other_ports_more_slots_or_inject_keys():
    a = _tenant("cfg")
    sid = a.post(f"{V}/servers", json={"name": "C"}).json()["id"]
    evil = {"SERVER": {"TCP_PORT": 9600, "UDP_PORT": 9600, "MAX_CLIENTS": 40, "NAME": "x\nTCP_PORT=9600", "REGISTER_TO_LOBBY": 1, "TRACK": "imola", "CARS": "gp"}}
    entries = [{"MODEL": "gp", "SKIN": "a"} for _ in range(10)]
    assert a.patch(f"{V}/servers/{sid}", json={"name": "C", "config": evil, "entry_list": entries}).status_code == 200
    cfg = a.get(f"{V}/servers/{sid}/server_cfg.ini").text
    own = servers._ports(root.get(f"{V}/servers/{sid}").json()["base_port"])
    lines = cfg.splitlines()
    assert f"TCP_PORT={own['tcp']}" in lines and "TCP_PORT=9600" not in lines          # our ports, whatever they asked for
    assert "MAX_CLIENTS=4" in cfg and "REGISTER_TO_LOBBY=0" in cfg                   # plan slots, never published
    assert sum(1 for x in lines if x.startswith("TCP_PORT=")) == 1                   # the newline in NAME could not add a key (it became a space)
    assert a.get(f"{V}/servers/{sid}/entry_list.ini").text.count("[CAR_") == 4       # the plan's four slots
    bad = a.patch(f"{V}/servers/{sid}", json={"name": "C", "config": {"SERVER": {"TRACK": "../../etc"}}, "entry_list": []})
    assert bad.status_code == 200 and a.get(f"{V}/servers/{sid}/server_cfg.ini").status_code == 400    # a path as a content name is refused


def test_limits_follow_the_plan_when_it_changes():
    a = _tenant("plan-change")
    sid = a.post(f"{V}/servers", json={"name": "P"}).json()["id"]
    with Session(engine) as sess:
        s = sess.get(servers.Server, sid)
        assert tenancy.limits_for(sess, s) == (150, 1024)
    p = root.get(f"{V}/plans").json()
    plan = next(x for x in p if x["id"] == a.plan_id)
    root.put(f"{V}/plans/{a.plan_id}", json={"name": plan["name"], "max_servers": 2, "slots": 4, "cpu_percent": 300, "mem_mb": 2048})
    with Session(engine) as sess:
        assert tenancy.limits_for(sess, sess.get(servers.Server, sid)) == (300, 2048)   # applied at its next start
