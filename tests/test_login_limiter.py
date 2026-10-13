from concurrent.futures import ThreadPoolExecutor

from conftest import ADMIN
from fastapi.testclient import TestClient

from app import auth
from app.login_limiter import LoginLimiter
from app.main import app

V = "/api/v1"


def test_five_attempts_of_one_user_from_one_address_block_that_pair_for_the_window():
    lim = LoginLimiter()
    for i in range(5):
        assert lim.attempt("1.1.1.1", "Root" if i % 2 else "root ", now=100 + i) == 0   # the username is normalized: Root, "root " and root are one
    assert 290 <= lim.attempt("1.1.1.1", "ROOT", now=106) <= 300
    assert lim.attempt("2.2.2.2", "root", now=106) == 0                  # another address: the real admin is not locked out (no account DoS)
    assert lim.attempt("1.1.1.1", "other", now=106) == 0                 # another user from the same address
    assert lim.attempt("1.1.1.1", "root", now=399) > 0                   # attempts at 100..104: at 399 all five still count (a blocked try is not counted)
    assert lim.attempt("1.1.1.1", "root", now=400) == 0                  # at 400 the first one expired (window 300): four left, the pair is free again


def test_a_success_gives_the_attempts_back_and_an_address_has_a_ceiling_over_many_users():
    lim = LoginLimiter()
    for _ in range(4):
        assert lim.attempt("1.1.1.1", "root", now=10) == 0
    lim.succeeded("1.1.1.1", "root")
    for _ in range(4):
        assert lim.attempt("1.1.1.1", "root", now=11) == 0               # the earlier four no longer count
    for i in range(30):                                                    # one address trying 30 different names is a spray
        assert lim.attempt("9.9.9.9", f"user{i}", now=20) == 0
    assert lim.attempt("9.9.9.9", "brand-new-name", now=21) > 0            # blocked for every name


def test_the_tables_stay_bounded_and_shedding_keeps_the_blocks_that_are_in_force():
    lim = LoginLimiter(max_entries=50)
    for _ in range(5):
        lim.attempt("6.6.6.6", "victim", now=1000)
    assert lim.attempt("6.6.6.6", "victim", now=1001) > 0                  # blocked
    for i in range(500):                                                    # a flood of distinct keys must not wash the block away
        lim.attempt(f"10.0.{i // 250}.{i % 250}", f"u{i}", now=1002)
    assert len(lim._pair) <= 50 and len(lim._ip) <= 50
    assert lim.attempt("6.6.6.6", "victim", now=1003) > 0, "the flood evicted a block that was still in force"


def test_a_burst_of_parallel_wrong_guesses_cannot_all_slip_past_the_check():
    admin = TestClient(app, headers=ADMIN)
    assert admin.post(f"{V}/users", json={"username": "burst", "password": "hunter2hunter2", "role": "driver"}).status_code == 201
    auth._limiter.__init__()

    def guess(_):
        return TestClient(app).post(f"{V}/auth/login", json={"username": "burst", "password": "wrong-wrong"}).status_code

    with ThreadPoolExecutor(16) as pool:
        codes = list(pool.map(guess, range(16)))
    assert codes.count(401) == 5 and codes.count(429) == 11, codes         # exactly the allowed five reached the password check
    auth._limiter.__init__()


def test_login_answers_429_with_retry_after_and_the_right_password_does_not_bypass_it():
    admin = TestClient(app, headers=ADMIN)
    assert admin.post(f"{V}/users", json={"username": "locky", "password": "hunter2hunter2", "role": "driver"}).status_code == 201
    auth._limiter.__init__()
    c = TestClient(app)
    for _ in range(5):
        assert c.post(f"{V}/auth/login", json={"username": "locky", "password": "wrong-wrong"}).status_code == 401
    r = c.post(f"{V}/auth/login", json={"username": "locky", "password": "hunter2hunter2"})
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0, (r.status_code, r.headers)
    auth._limiter.__init__()
    assert c.post(f"{V}/auth/login", json={"username": "locky", "password": "hunter2hunter2"}).status_code == 200   # and a good login still works
    auth._limiter.__init__()


def test_a_forged_x_forwarded_for_does_not_give_a_caller_a_fresh_address():
    """uvicorn swaps the client for X-Forwarded-For when it arrives from loopback, and loopback is where every caller is: rotating the header must not reset the count."""
    auth._limiter.__init__()
    c = TestClient(app)
    codes = [c.post(f"{V}/auth/login", json={"username": "ghost", "password": "wrong-wrong"}, headers={"X-Forwarded-For": f"203.0.113.{i}"}).status_code
             for i in range(8)]
    assert codes[:5] == [401] * 5 and codes[5:] == [429] * 3, codes
    auth._limiter.__init__()


def test_an_unknown_user_costs_the_same_single_hash_as_a_known_one(monkeypatch):
    calls = []
    real = auth._check_pw
    monkeypatch.setattr(auth, "_check_pw", lambda pw, stored: calls.append(stored) or real(pw, stored))
    auth._limiter.__init__()
    TestClient(app).post(f"{V}/auth/login", json={"username": "nobody-at-all", "password": "whatever-1"})
    assert len(calls) == 1 and calls[0] == auth._DUMMY, "an unknown user must still pay for exactly one password hash (no extra work on first use)"
    auth._limiter.__init__()


def test_a_failure_of_ours_does_not_count_against_the_user(monkeypatch):
    """The attempt is counted before the password is judged; if the lookup or the hash itself blows up (a database error), that must be given back."""
    admin = TestClient(app, headers=ADMIN)
    assert admin.post(f"{V}/users", json={"username": "unlucky", "password": "hunter2hunter2", "role": "driver"}).status_code == 201
    auth._limiter.__init__()
    monkeypatch.setattr(auth, "_check_pw", lambda pw, stored: (_ for _ in ()).throw(RuntimeError("database is down")))
    c = TestClient(app, raise_server_exceptions=False)
    assert [c.post(f"{V}/auth/login", json={"username": "unlucky", "password": "hunter2hunter2"}).status_code for _ in range(8)] == [500] * 8
    monkeypatch.undo()
    assert c.post(f"{V}/auth/login", json={"username": "unlucky", "password": "hunter2hunter2"}).status_code == 200   # recovered: not locked out by our own 500s
    auth._limiter.__init__()
