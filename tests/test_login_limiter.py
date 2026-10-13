from conftest import ADMIN
from fastapi.testclient import TestClient

from app import auth
from app.login_limiter import LoginLimiter
from app.main import app

V = "/api/v1"


def test_five_failures_for_one_user_from_one_address_block_that_pair_for_five_minutes():
    lim = LoginLimiter()
    for i in range(5):
        assert lim.retry_after("1.1.1.1", "Root", now=100 + i) == 0
        lim.failed("1.1.1.1", "root ", now=100 + i)                          # the username is normalized
    assert 290 <= lim.retry_after("1.1.1.1", "ROOT", now=106) <= 300
    assert lim.retry_after("2.2.2.2", "root", now=106) == 0                  # another address: the real admin is not locked out (no account DoS)
    assert lim.retry_after("1.1.1.1", "other", now=106) == 0                 # another user from the same address
    assert lim.retry_after("1.1.1.1", "root", now=399) > 0                   # failures at 100..104: at 399 all five still count
    assert lim.retry_after("1.1.1.1", "root", now=400) == 0                  # at 400 the first one expired (window 300): four left, the pair is free


def test_a_success_clears_the_pair_and_an_address_has_a_ceiling_over_many_users():
    lim = LoginLimiter()
    for i in range(4):
        lim.failed("1.1.1.1", "root", now=10)
    lim.succeeded("1.1.1.1", "root")
    for i in range(4):
        lim.failed("1.1.1.1", "root", now=11)                                # the earlier four no longer count
    assert lim.retry_after("1.1.1.1", "root", now=12) == 0
    for i in range(30):                                                        # one address trying 30 different names is a spray: blocked for everyone
        lim.failed("9.9.9.9", f"user{i}", now=20)
    assert lim.retry_after("9.9.9.9", "brand-new-name", now=21) > 0


def test_the_table_cannot_grow_without_bound():
    lim = LoginLimiter(max_entries=50)
    for i in range(500):
        lim.failed(f"10.0.{i // 250}.{i % 250}", f"u{i}", now=1000)
    assert len(lim._pair) <= 50 and len(lim._ip) <= 50


def test_login_answers_429_with_retry_after_and_the_right_password_does_not_bypass_it():
    admin = TestClient(app, headers=ADMIN)
    assert admin.post(f"{V}/users", json={"username": "locky", "password": "hunter2hunter2", "role": "driver"}).status_code == 201
    c = TestClient(app)
    for _ in range(5):
        assert c.post(f"{V}/auth/login", json={"username": "locky", "password": "wrong-wrong"}).status_code == 401
    r = c.post(f"{V}/auth/login", json={"username": "locky", "password": "hunter2hunter2"})
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0, (r.status_code, r.headers)
    assert TestClient(app).post(f"{V}/auth/login", json={"username": "root", "password": "nope-nope"}).status_code in (401, 429)
    auth._limiter.__init__()   # the process-wide table is shared by the whole test session: leave it clean


def test_an_unknown_user_costs_the_same_hash_as_a_known_one(monkeypatch):
    calls = []
    real = auth._check_pw
    monkeypatch.setattr(auth, "_check_pw", lambda pw, stored: calls.append(stored) or real(pw, stored))
    c = TestClient(app)
    c.post(f"{V}/auth/login", json={"username": "nobody-at-all", "password": "whatever-1"})
    assert len(calls) == 1, "an unknown user must still pay for one password hash, or the response time tells which names exist"
    auth._limiter.__init__()
