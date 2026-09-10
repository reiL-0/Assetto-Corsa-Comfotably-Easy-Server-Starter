import asyncio
import shlex
import sys

from app import supervisor
from app.config import settings


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
