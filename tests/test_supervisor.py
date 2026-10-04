import asyncio
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
