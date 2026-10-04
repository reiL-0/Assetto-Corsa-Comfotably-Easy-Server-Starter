import asyncio
import os
import shlex
import sys

from app import supervisor
from app.config import settings
from app.live import acsp


def test_start_capture_stop(tmp_path, monkeypatch):
    script = tmp_path / "fake_acserver.py"
    script.write_text(
        "import time\nwhile True:\n    print('tick', flush=True)\n    time.sleep(0.1)\n"
    )
    cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    monkeypatch.setattr(settings, "acserver_cmd", cmd)

    async def scenario():
        inst = await supervisor.start(99, tmp_path)
        try:
            await asyncio.sleep(0.4)
            assert inst.running
            assert any("tick" in line for line in inst.log)
        finally:
            await supervisor.stop(99)
        assert not inst.running

    asyncio.run(scenario())


def test_idle_instance_is_stopped_but_one_with_cars_is_not(tmp_path, monkeypatch):
    script = tmp_path / "fake_acserver.py"
    script.write_text("import time\ntime.sleep(60)\n")
    monkeypatch.setattr(
        settings, "acserver_cmd", f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    )
    monkeypatch.setattr(settings, "idle_stop_seconds", 1)
    monkeypatch.setattr(supervisor, "IDLE_POLL", 0.1)

    async def scenario():
        busy = await supervisor.start(97, tmp_path)
        idle = await supervisor.start(98, tmp_path)
        idle.acsp = acsp.ACSPClient(0)
        busy.acsp = acsp.ACSPClient(0)
        busy.acsp.cars[0] = {"car_id": 0}
        try:
            await asyncio.sleep(1.6)
            assert not idle.running
            assert busy.running
        finally:
            await supervisor.stop(97)
            await supervisor.stop(98)

    asyncio.run(scenario())


def test_a_running_server_survives_the_manager_and_is_adopted(tmp_path, monkeypatch):
    """The manager 'restarts' (registry lost) while the server keeps running; the new manager takes it back, tails its log
    and can stop it."""
    script = tmp_path / "fake_acserver.py"
    script.write_text("import time\nwhile True:\n    print('tick', flush=True)\n    time.sleep(0.1)\n")
    monkeypatch.setattr(settings, "acserver_cmd", f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}")

    async def scenario():
        first = await supervisor.start(95, tmp_path)
        pid = first.pid
        await asyncio.sleep(0.4)
        assert (tmp_path / "server.pid").exists()
        # --- the manager dies: all its in-memory state and tasks go, the server process does not
        first._reader.cancel()
        supervisor._instances.clear()
        assert supervisor._alive(pid)

        again = await supervisor.adopt(95, tmp_path)
        assert again is not None and again.pid == pid and again.running and supervisor.get(95) is again
        before = len(again.log)
        await asyncio.sleep(0.6)
        assert len(again.log) > before, "the log is followed again"
        await again.stop()
        assert not supervisor._alive(pid) and not (tmp_path / "server.pid").exists()
        first.proc._transport.close()  # let the event loop drop the now-finished child

    asyncio.run(scenario())


def test_adopt_ignores_stale_and_foreign_pid_files(tmp_path, monkeypatch):
    script = tmp_path / "fake_acserver.py"
    script.write_text("import time\ntime.sleep(30)\n")
    monkeypatch.setattr(settings, "acserver_cmd", f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}")

    async def scenario():
        assert await supervisor.adopt(94, tmp_path) is None                      # no pid file
        (tmp_path / "server.pid").write_text('{"pid": 999999}')
        assert await supervisor.adopt(94, tmp_path) is None and not (tmp_path / "server.pid").exists()   # dead pid: file cleaned
        (tmp_path / "server.pid").write_text('{"pid": %d}' % os.getpid())        # alive, but it is this test, not an acServer
        assert await supervisor.adopt(94, tmp_path) is None

    asyncio.run(scenario())

