"""acServer as another Unix user (hosting for others): the command that switches user and drops capabilities, the environment it gets, and `adopt` for a process whose
directory the kernel hides from us. What needs two real users (a different owner, a CAP_SETUID that must not survive) was checked in a container; see docs/quality/BASELINE.md."""

import asyncio
import json
import os
import shlex
import sys
import time

import pytest
from pydantic import ValidationError

from app import supervisor
from app.config import settings


def _use(script, tmp_path, monkeypatch, body):
    script.write_text(body)
    monkeypatch.setattr(settings, "acserver_cmd", f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}")


SLEEP = "import time\ntime.sleep(60)\n"


def test_spawn_prefix_switches_user_and_leaves_the_child_with_no_capabilities(monkeypatch):
    monkeypatch.setattr(settings, "acserver_uid", None)
    assert supervisor.spawn_prefix() == []
    monkeypatch.setattr(settings, "acserver_uid", 2000)
    monkeypatch.setattr(supervisor.shutil, "which", lambda name: "/usr/bin/setpriv")
    p = supervisor.spawn_prefix()
    assert p[0] == "setpriv" and "--reuid=2000" in p and "--regid=2000" in p and p[-1] == "--"
    for needed in ("--clear-groups", "--inh-caps=-all", "--ambient-caps=-all", "--no-new-privs"):   # the first two: without them the child keeps the manager's CAP_SETUID
        assert needed in p
    monkeypatch.setattr(supervisor.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="setpriv"):
        supervisor.spawn_prefix()


@pytest.mark.parametrize("uid", [0, -1, os.geteuid()])
def test_root_and_the_managers_own_user_are_refused_as_the_acserver_user(uid):
    with pytest.raises(ValidationError):
        type(settings)(acserver_uid=uid)


def _no_cap_kill(monkeypatch):
    def refused(pid, sig):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(supervisor.os, "kill", refused)
    monkeypatch.setattr(supervisor.os, "stat", lambda p: type("S", (), {"st_uid": 2000})())
    monkeypatch.setattr(settings, "acserver_uid", 2000)


def test_without_cap_kill_a_signal_to_a_server_of_another_user_is_sent_by_that_user_with_no_secrets(monkeypatch):
    """The manager holds CAP_SETUID, not CAP_KILL: kill(2) on another user's process is refused to it (reproduced in a container: stop() raised PermissionError and left the server running)."""
    seen = []

    class Done:
        async def wait(self):
            return 0

    async def fake_exec(*argv, **kw):
        seen.append((argv, kw))
        return Done()

    async def scenario():
        monkeypatch.setenv("ACM_FAKE_SECRET", "hunter2")
        monkeypatch.setattr(supervisor.asyncio, "create_subprocess_exec", fake_exec)
        _no_cap_kill(monkeypatch)
        await supervisor._signal(4242, 15)
        argv, kw = seen[0]
        assert argv[:2] == ("setpriv", "--reuid=2000") and argv[-3:] == ("kill", "-15", "4242")
        assert "--inh-caps=-all" in argv and "--ambient-caps=-all" in argv
        assert "ACM_FAKE_SECRET" not in kw["env"] and "PATH" in kw["env"], "the helper runs as the client's user: it must not carry the manager's secrets"
        monkeypatch.setattr(settings, "acserver_uid", 3000)   # a pid of a user that is neither the manager's nor the configured one (a reused pid): never become that user
        with pytest.raises(PermissionError):
            await supervisor._signal(4242, 15)

    asyncio.run(scenario())


def test_a_frozen_helper_does_not_make_stop_wait_for_ever(monkeypatch):
    class Frozen:
        async def wait(self):
            await asyncio.sleep(60)

    async def fake_exec(*argv, **kw):
        return Frozen()

    async def scenario():
        monkeypatch.setattr(supervisor.asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(supervisor, "HELPER_TIMEOUT", 0.2)
        _no_cap_kill(monkeypatch)
        with pytest.raises(TimeoutError, match="CAP_KILL"):
            await supervisor._signal(4242, 15)

    asyncio.run(scenario())


def test_a_signal_to_a_server_of_the_managers_own_user_is_a_plain_kill(tmp_path, monkeypatch):
    _use(tmp_path / "fake.py", tmp_path, monkeypatch, SLEEP)

    async def scenario():
        inst = await supervisor.start(89, tmp_path)
        await supervisor._signal(inst.pid, 15)
        for _ in range(50):
            if not inst.running:
                break
            await asyncio.sleep(0.1)
        assert not inst.running
        with pytest.raises(ProcessLookupError):
            await supervisor._signal(inst.pid, 15)   # gone: like os.kill

    asyncio.run(scenario())


def test_acserver_does_not_inherit_the_managers_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("ACM_FAKE_SECRET", "hunter2")
    monkeypatch.setenv("OPR_FAKE_SECRET", "hunter3")
    monkeypatch.setenv("KEEP_ME", "yes")
    seen = tmp_path / "env.json"
    _use(tmp_path / "fake.py", tmp_path, monkeypatch,
         f"import json, os, time\nopen({str(seen)!r}, 'w').write(json.dumps(dict(os.environ)))\ntime.sleep(60)\n")

    async def scenario():
        await supervisor.start(93, tmp_path)
        try:
            for _ in range(60):
                if seen.exists() and seen.read_text():
                    break
                await asyncio.sleep(0.1)
        finally:
            await supervisor.stop(93)

    asyncio.run(scenario())
    env = json.loads(seen.read_text())
    assert not [k for k in env if k.startswith(("ACM_", "OPR_"))], "the manager's settings (and their secrets) must not reach acServer"
    assert env.get("KEEP_ME") == "yes" and "PATH" in env and "XDG_RUNTIME_DIR" in env


def test_adopt_of_a_process_whose_directory_is_hidden_goes_by_its_start_time(tmp_path, monkeypatch):
    """Another user's process: readlink(/proc/<pid>/cwd) is refused to us. Simulated by refusing it."""
    _use(tmp_path / "fake.py", tmp_path, monkeypatch, SLEEP)

    def refused(path):
        raise PermissionError(13, "Permission denied", path)

    async def scenario():
        first = await supervisor.start(92, tmp_path)
        started_at = first.started_at
        first._reader.cancel()
        supervisor._instances.clear()
        try:
            with monkeypatch.context() as m:
                m.setattr(supervisor.os, "readlink", refused)
                (tmp_path / "server.pid").write_text(json.dumps({"pid": first.pid, "started_at": time.time() - 3600}))
                assert await supervisor.adopt(92, tmp_path) is None, "a start time an hour off is another process"
                (tmp_path / "server.pid").write_text(json.dumps({"pid": first.pid}))
                assert await supervisor.adopt(92, tmp_path) is None, "no start time recorded: not enough to be sure"
                (tmp_path / "server.pid").write_text(json.dumps({"pid": first.pid, "started_at": "soon"}))
                assert await supervisor.adopt(92, tmp_path) is None, "a corrupt start time must not break the boot"
                (tmp_path / "server.pid").write_text(json.dumps({"pid": first.pid, "started_at": started_at}))
                again = await supervisor.adopt(92, tmp_path)
                assert again is not None and again.pid == first.pid and again.running
            await again.stop()
        finally:
            if supervisor._alive(first.pid):
                os.kill(first.pid, 9)
            first.proc._transport.close()

    asyncio.run(scenario())


def test_adopt_refuses_a_process_of_another_owner_and_takes_back_one_started_before_the_switch(tmp_path, monkeypatch):
    _use(tmp_path / "fake.py", tmp_path, monkeypatch, SLEEP)

    async def scenario():
        first = await supervisor.start(91, tmp_path)
        first._reader.cancel()
        supervisor._instances.clear()
        record = (tmp_path / "server.pid").read_text()
        try:
            with monkeypatch.context() as m:   # the manager "is" someone else now, and no acserver_uid names the owner of the process
                m.setattr(supervisor.os, "geteuid", lambda: os.getuid() + 1)
                m.setattr(settings, "acserver_uid", None)
                assert await supervisor.adopt(91, tmp_path) is None
            (tmp_path / "server.pid").write_text(record)
            with monkeypatch.context() as m:   # acserver_uid switched on while this server was already running as the manager's user: still ours, or it would be orphaned
                m.setattr(settings, "acserver_uid", os.getuid() + 1)
                again = await supervisor.adopt(91, tmp_path)
                assert again is not None and again.pid == first.pid
            await again.stop()
        finally:
            if supervisor._alive(first.pid):
                os.kill(first.pid, 9)
            first.proc._transport.close()

    asyncio.run(scenario())


def test_adopt_does_not_take_this_very_process_for_an_acserver(tmp_path, monkeypatch):
    """Same binary name as the acServer command (python), alive, our own user: only the directory or the start time tell them apart."""
    _use(tmp_path / "fake.py", tmp_path, monkeypatch, SLEEP)

    async def scenario():
        (tmp_path / "server.pid").write_text(json.dumps({"pid": os.getpid(), "started_at": time.time()}))
        assert await supervisor.adopt(90, tmp_path) is None
        (tmp_path / "server.pid").write_text(json.dumps({"pid": os.getpid(), "started_at": time.time() - 3600}))   # (a record from an hour ago: another process than the one that has this pid now)
        with monkeypatch.context() as m:
            m.setattr(supervisor.os, "readlink", lambda p: (_ for _ in ()).throw(PermissionError(13, "no", p)))
            assert await supervisor.adopt(90, tmp_path) is None

    asyncio.run(scenario())


def test_the_pid_file_carries_the_process_own_start_time_and_adopt_prefers_it(tmp_path, monkeypatch):
    """`started_at` is taken when the spawn returns (an event loop stalled for seconds right there would move it); the record also holds the start time the kernel gives the process."""
    _use(tmp_path / "fake.py", tmp_path, monkeypatch, SLEEP)

    def refused(path):
        raise PermissionError(13, "Permission denied", path)

    async def scenario():
        first = await supervisor.start(88, tmp_path)
        record = json.loads((tmp_path / "server.pid").read_text())
        assert abs(record["proc_started"] - record["started_at"]) < supervisor.START_TOLERANCE
        first._reader.cancel()
        supervisor._instances.clear()
        try:
            with monkeypatch.context() as m:
                m.setattr(supervisor.os, "readlink", refused)
                record["started_at"] += 600   # the manager's clock note is off by ten minutes: the process' own time still says it is this one
                (tmp_path / "server.pid").write_text(json.dumps(record))
                again = await supervisor.adopt(88, tmp_path)
                assert again is not None and again.pid == first.pid
                record["proc_started"] += 600   # and when that one is off, it is not
                (tmp_path / "server.pid").write_text(json.dumps(record))
                assert await supervisor.adopt(88, tmp_path) is None
            await again.stop()
        finally:
            if supervisor._alive(first.pid):
                os.kill(first.pid, 9)
            first.proc._transport.close()

    asyncio.run(scenario())
