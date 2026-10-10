"""Watchdog regressions: no network or real subprocesses."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def watchdog():
    spec = importlib.util.spec_from_file_location(
        "watchdog_under_test", Path(__file__).resolve().parents[1] / "ops/watchdog.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def saved(path):
    data = json.loads(path.read_text())
    data.pop("_seq", None)   # the save counter is not part of the checks' state
    return data


def test_checks_are_isolated(watchdog, monkeypatch):
    monkeypatch.setattr(watchdog, "get", lambda url: "HTTP 502" if "healthz" in url else None)
    monkeypatch.setattr(watchdog, "check_backup", lambda *args: "backup failed")

    def inaccessible(*args):
        raise PermissionError("denied")

    monkeypatch.setattr(watchdog, "check_disk", inaccessible)
    results = watchdog.run_checks({"WATCHDOG_URLS": "site=http://example.test"}, 100)
    assert results == {"manager": "HTTP 502", "backup": "backup failed",
                       "disk": "no se pudo comprobar disk: PermissionError: denied", "site": None}
    monkeypatch.setattr(watchdog, "check_backup", inaccessible)
    assert "PermissionError" in watchdog.run_checks({}, 100)["backup"]


@pytest.mark.parametrize("last_run", [None, "100", [], {}, True, float("nan"), float("inf")])
def test_invalid_backup_timestamp(watchdog, tmp_path, last_run):
    path = tmp_path / "backup.json"
    path.write_text(json.dumps({"ok": True, "last_run": last_run}))
    assert "last_run" in watchdog.check_backup(path, 100)


def test_backup_contract(watchdog, tmp_path):
    path = tmp_path / "backup.json"
    status = {"ok": True, "last_run": 100}
    path.write_text(json.dumps(status))
    assert watchdog.check_backup(path, 101) is None
    status["offsite"] = {"ok": False, "error": "Drive unavailable"}
    path.write_text(json.dumps(status))
    assert watchdog.check_backup(path, 101) == "la subida fuera del VPS falló: Drive unavailable"
    status["offsite"]["ok"] = True
    path.write_text(json.dumps(status))
    assert watchdog.check_backup(path, 101) is None


@pytest.mark.parametrize("contents", ["{", "[]", "null", '{"disk": null}',
                                         '{"disk": {"fails": "bad"}}',
                                         '{"disk": {"fails": 1}}', "{}"])
def test_old_or_corrupt_state_does_not_block_alert(watchdog, monkeypatch, tmp_path, contents):
    path = tmp_path / "state.json"
    path.write_text(contents)
    monkeypatch.setattr(watchdog, "run_checks", lambda *args: {"disk": "full"})
    monkeypatch.setattr(watchdog.time, "time", lambda: 1000)
    messages = []
    monkeypatch.setattr(watchdog, "post", lambda hook, msg: messages.append(msg))
    env = {"WATCHDOG_STATE": str(path)}
    assert watchdog.main(env) == 0
    assert messages == []
    assert watchdog.main(env) == 0
    assert len(messages) == 1


def test_atomic_write_failure_preserves_old_file(watchdog, monkeypatch, tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"old": "intact"}')

    def fail_replace(source, target):
        assert Path(source).parent == path.parent
        assert json.loads(Path(source).read_text()) == {"new": "complete"}
        raise OSError("disk error")

    monkeypatch.setattr(watchdog.os, "replace", fail_replace)
    with pytest.raises(OSError):
        watchdog.save_state(path, {"new": "complete"})
    assert path.read_text() == '{"old": "intact"}'
    assert list(tmp_path.iterdir()) == [path]


def test_discord_failure_is_retried_with_fresh_data_never_a_stale_alert(watchdog, monkeypatch, tmp_path):
    path = tmp_path / "state.json"
    env = {"WATCHDOG_STATE": str(path), "ACM_DISCORD_STATUS_WEBHOOK": "https://example.test"}
    results = {"disk": "full"}
    monkeypatch.setattr(watchdog, "run_checks", lambda *args: dict(results))
    monkeypatch.setattr(watchdog.time, "time", lambda: 1000)
    requests = []

    def unavailable(request, timeout):
        requests.append(request)
        raise OSError("Discord down")

    monkeypatch.setattr(watchdog.urllib.request, "urlopen", unavailable)
    assert watchdog.main(env) == 0            # first failing run: debounce, nothing sent
    assert watchdog.main(env) == 1            # second: alert attempted, Discord down
    assert watchdog.main(env) == 1            # third: attempted again (the alert was NOT recorded as sent)
    assert len(requests) == 2
    assert json.loads(path.read_text())["disk"]["last"] is None
    results["disk"] = None                    # the problem is gone while Discord is still down
    assert watchdog.main(env) == 0            # nothing stale is sent: the check is re-read, it passes, and no «recovered» for an alert that never went out
    assert len(requests) == 2
    assert saved(path) == {}


def test_full_disk_does_not_resend_the_alert_every_run(watchdog, monkeypatch, tmp_path):
    main_file, fallback = tmp_path / "state.json", tmp_path / "run-state.json"
    env = {"WATCHDOG_STATE": str(main_file), "WATCHDOG_STATE_FALLBACK": str(fallback)}
    monkeypatch.setattr(watchdog, "run_checks", lambda *args: {"disk": "full"})
    monkeypatch.setattr(watchdog.time, "time", lambda: 1000)
    sent = []
    monkeypatch.setattr(watchdog, "post", lambda hook, message: sent.append(message))
    real_save = watchdog.save_state
    def disk_full(path, state):
        if path == main_file:
            raise OSError("No space left on device")
        real_save(path, state)
    monkeypatch.setattr(watchdog, "save_state", disk_full)
    assert watchdog.main(env) == 0 and not sent          # first failing run: debounce
    assert watchdog.main(env) == 0 and len(sent) == 1    # second: alert goes out; the main file cannot be written, the tmpfs one is
    assert fallback.exists() and not main_file.exists()
    assert watchdog.main(env) == 0 and len(sent) == 1    # NOT sent again (before: every run)
    monkeypatch.setattr(watchdog, "save_state", real_save)   # the disk has room again
    assert watchdog.main(env) == 0 and len(sent) == 1
    assert main_file.exists() and not fallback.exists()  # the main file is the truth again


def test_state_choice_follows_the_save_counter_not_the_clock(watchdog, tmp_path):
    import os as _os
    main_file, fallback = tmp_path / "state.json", tmp_path / "run-state.json"
    newer, older = {"disk": {"fails": 2, "since": 5, "last": 7}}, {"old": {"fails": 9, "since": 1, "last": 1}}
    # the fallback holds the LATER save but its mtime is OLDER (clock went back): it still wins
    main_file.write_text(json.dumps({"_seq": 3, **older}))
    fallback.write_text(json.dumps({"_seq": 4, **newer}))
    _os.utime(fallback, (1, 1))
    assert watchdog.load_state(main_file, fallback) == (newer, 4)
    # a fallback that could not be deleted (lower seq, NEWER mtime) never beats the main file
    main_file.write_text(json.dumps({"_seq": 5, **newer}))
    fallback.write_text(json.dumps({"_seq": 4, **older}))
    _os.utime(fallback, (9999999999, 9999999999))
    assert watchdog.load_state(main_file, fallback) == (newer, 5)
    # a state written before `_seq` existed counts as 0; a tie goes to the main file; garbage is ignored
    main_file.write_text(json.dumps(newer)); fallback.write_text("not json")
    assert watchdog.load_state(main_file, fallback) == (newer, 0)
    main_file.write_text(json.dumps({"_seq": "x", **newer}))
    assert watchdog.load_state(main_file, fallback) == ({}, 0)


def test_stale_fallback_that_could_not_be_deleted_is_not_used_after_the_disk_recovers(watchdog, monkeypatch, tmp_path):
    main_file, fallback = tmp_path / "state.json", tmp_path / "run-state.json"
    env = {"WATCHDOG_STATE": str(main_file), "WATCHDOG_STATE_FALLBACK": str(fallback)}
    fallback.write_text(json.dumps({"_seq": 1, "disk": {"fails": 5, "since": 1, "last": 1}}))
    monkeypatch.setattr(watchdog, "run_checks", lambda *args: {"disk": None})
    monkeypatch.setattr(watchdog.Path, "unlink", lambda self, *a, **k: (_ for _ in ()).throw(OSError("busy")))   # the fallback cannot be removed
    assert watchdog.main(env) == 0            # reads the fallback (seq 1), recovered -> main saved with seq 2
    assert json.loads(main_file.read_text())["_seq"] == 2 and fallback.exists()
    assert watchdog.load_state(main_file, fallback)[1] == 2   # the main file wins even though the fallback is still there


def test_save_failure_still_reports_failure(watchdog, monkeypatch, tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"disk": {"fails": 1, "since": 100, "last": None}}))
    monkeypatch.setattr(watchdog, "run_checks", lambda *args: {"disk": "full"})

    def fail(*args):
        raise OSError("read only")

    sent = []
    monkeypatch.setattr(watchdog, "save_state", fail)   # neither the disk nor the tmpfs fallback can be written
    monkeypatch.setattr(watchdog, "post", lambda hook, message: sent.append(message))
    assert watchdog.main({"WATCHDOG_STATE": str(path), "WATCHDOG_STATE_FALLBACK": str(tmp_path / "fb.json")}) == 1
    assert sent, "the alert goes out even if the state cannot be saved anywhere (it may repeat next run)"


def test_debounce_repeat_recovery_and_purity(watchdog):
    state = {}
    messages, state = watchdog.step(state, {"disk": "full"}, 1000)
    assert messages == []
    previous = copy.deepcopy(state)
    messages, state = watchdog.step(state, {"disk": "full"}, 1120)
    assert len(messages) == 1
    assert previous == {"disk": {"fails": 1, "since": 1000, "last": None}}
    # The input object is never mutated.
    snapshot = copy.deepcopy(state)
    messages, later = watchdog.step(state, {"disk": "full"}, 1240)
    assert state == snapshot and messages == []
    messages, later = watchdog.step(later, {"disk": "full"}, 1120 + 6 * 3600)
    assert len(messages) == 1
    messages, later = watchdog.step(later, {"disk": None}, 30000)
    assert messages == ["✅ **disk** se recuperó"] and later == {}
    assert watchdog.step(later, {"disk": None}, 30120) == ([], {})


def test_http_double(watchdog, monkeypatch):
    class Response:
        status = 502

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(watchdog.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    assert watchdog.get("http://example.test") == "HTTP 502"


def test_recovery_delivery_is_retried(watchdog, monkeypatch, tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"disk": {"fails": 2, "since": 100, "last": 220}}))
    monkeypatch.setattr(watchdog, "run_checks", lambda *args: {"disk": None})
    sent = []

    def fail(hook, message):
        sent.append(message)
        raise OSError("down")

    monkeypatch.setattr(watchdog, "post", fail)
    env = {"WATCHDOG_STATE": str(path)}
    assert watchdog.main(env) == 1
    monkeypatch.setattr(watchdog, "post", lambda hook, message: sent.append(message))
    assert watchdog.main(env) == 0
    assert sent == ["✅ **disk** se recuperó"] * 2
    assert saved(path) == {}


def test_selftest_without_network(watchdog, monkeypatch):
    monkeypatch.setattr(watchdog, "get", lambda *args: "connection refused")
    watchdog.selftest()
