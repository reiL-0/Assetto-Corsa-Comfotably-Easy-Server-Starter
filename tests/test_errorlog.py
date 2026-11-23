"""Uncaught errors: 500 {detail, error_id} with no traceback in the response, the traceback in the log file and (admin only) behind /api/v1/errors/{id}; secrets blanked; a storm throttled."""
import logging

from conftest import ADMIN
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import errorlog
from app.main import app

V = "/api/v1"


def _boom():
    raise RuntimeError("kaboom token=abc123 password: hunter2")


app.router.routes.insert(0, APIRoute("/_boom_for_tests", _boom, include_in_schema=False))   # (in front of the SPA's catch-all)


client = TestClient(app, headers=ADMIN, raise_server_exceptions=False)


def test_an_uncaught_error_answers_an_id_and_never_the_traceback():
    r = client.get("/_boom_for_tests")
    assert r.status_code == 500
    body = r.json()
    assert set(body) == {"detail", "error_id"} and len(body["error_id"]) == 8, body
    assert "Traceback" not in r.text and "kaboom" not in r.text and "_boom" not in r.text
    one = client.get(f"{V}/errors/{body['error_id']}")
    assert one.status_code == 200 and "RuntimeError" in one.json()["traceback"] and one.json()["path"] == "/_boom_for_tests"
    assert any(e["id"] == body["error_id"] for e in client.get(f"{V}/errors").json())
    assert all("traceback" not in e for e in client.get(f"{V}/errors").json()), "the list is without tracebacks"


def test_secrets_never_reach_the_record_or_the_log_text():
    r = client.get("/_boom_for_tests")
    e = client.get(f"{V}/errors/{r.json()['error_id']}").json()
    assert "abc123" not in e["traceback"] + e["message"] and "hunter2" not in e["traceback"] + e["message"], e
    out = errorlog.redact("Authorization: Bearer abc.def-1\nGET /x?token=xyz&x=1\nCookie: session=qq")
    assert not any(x in out for x in ("abc.def-1", "xyz", "qq")) and "x=1" in out, out
    out = errorlog.redact("Cookie: a=uno; b=dos\nlogin failed password: mi clave con espacios\nnext line stays")
    assert not any(x in out for x in ("uno", "dos", "clave", "espacios")) and "next line stays" in out, out


def test_only_an_admin_reads_the_errors():
    r = client.get("/_boom_for_tests")
    anon = TestClient(app, raise_server_exceptions=False)
    assert anon.get(f"{V}/errors/{r.json()['error_id']}").status_code in (401, 403)
    assert anon.get(f"{V}/errors").status_code in (401, 403)
    assert client.get(f"{V}/errors/00000000").status_code == 404


def test_a_storm_of_the_same_error_is_throttled():
    f = errorlog._Throttle()
    rec = lambda: logging.LogRecord("x", logging.ERROR, __file__, 1, "same thing %s", ("a",), None)
    assert sum(f.filter(rec()) for _ in range(40)) == errorlog.THROTTLE_MAX
    other = logging.LogRecord("x", logging.ERROR, __file__, 1, "another thing", (), None)
    assert f.filter(other), "a different message still gets through"
    g = errorlog._Throttle()   # a storm of DIFFERENT messages: the file takes a ceiling per minute, and the filter's memory has one too
    n = sum(g.filter(logging.LogRecord("x", logging.ERROR, __file__, 1, "m%d" % i, (), None)) for i in range(1000))
    assert n == errorlog.GLOBAL_PER_WINDOW and len(g.seen) <= 2001, (n, len(g.seen))
    assert len(errorlog._SafeFormatter("%(message)s").format(logging.LogRecord("x", logging.ERROR, __file__, 1, "y" * 50000, (), None))) <= errorlog.MAX_RECORD + 20


def test_the_file_keeps_only_the_configured_days(tmp_path, monkeypatch):
    monkeypatch.setattr(errorlog.settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(errorlog.settings, "error_log_keep_days", 2)
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        for h in list(root.handlers):
            if getattr(h, "_acm_error_log", False):
                root.removeHandler(h)
        path = errorlog.setup()
        h = next(h for h in root.handlers if getattr(h, "_acm_error_log", False))
        assert path == tmp_path / "logs" / "manager.log" and h.backupCount == 2
        logging.getLogger("acmanager.t").warning("hello %s", "password=s3cret")
        h.flush()
        txt = path.read_text()
        assert "hello" in txt and "s3cret" not in txt
    finally:
        for h in list(root.handlers):
            if getattr(h, "_acm_error_log", False):
                root.removeHandler(h); h.close()
        for h in before:
            if h not in root.handlers:
                root.addHandler(h)
