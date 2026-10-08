import asyncio
import json
import os
import time
from datetime import UTC, datetime, timedelta

from app import resourcelog, supervisor
from app.config import settings


class _Driver:
    def __init__(self, connected):
        self.connected = connected


class _Inst:
    running = True

    def __init__(self, pid, drivers):
        self.pid = pid
        self.acsp = type("A", (), {"board": type("B", (), {"drivers": drivers, "session": {"session_type": 3}})()})()


def _lines(tmp_path):
    f = next((tmp_path / "logs").glob("resources-*.jsonl"))
    return [json.loads(x) for x in f.read_text().splitlines()]


def test_each_cycle_writes_one_host_line_and_one_per_running_server(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(supervisor, "_instances", {7: _Inst(os.getpid(), [_Driver(True), _Driver(True), _Driver(False)])})
    s = resourcelog.Sampler()
    first = s.sample(0, now=1000.0)
    t_end = time.time() + 0.3
    while time.time() < t_end:   # burn CPU so this process shows a measurable share of a core
        sum(i * i for i in range(2000))
    second = s.sample(250, now=1000.0 + 0.3)
    host, srv = second[0], second[1]
    assert host["kind"] == "host" and host["late_ms"] == 250 and srv["kind"] == "server" and srv["server"] == 7
    assert srv["players"] == 2 and srv["session_type"] == 3 and srv["rss_mb"] > 0 and srv["threads"] >= 1
    assert first[1]["cpu_pct"] is None and srv["cpu_pct"] is not None and 20 < srv["cpu_pct"] <= 120      # the busy loop: roughly one core
    assert len(_lines(tmp_path)) == 4                                                                      # 2 cycles x (host + server): all in one file


def test_a_stopped_server_and_a_missing_process_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    stopped = _Inst(os.getpid(), [])
    stopped.running = False
    gone = _Inst(2_000_000_000, [])                                                                        # no such pid: nothing to read, nothing to crash
    monkeypatch.setattr(supervisor, "_instances", {1: stopped, 2: gone})
    lines = resourcelog.Sampler().sample()
    assert [x["kind"] for x in lines] == ["host", "server"] and lines[1]["server"] == 2 and lines[1]["cpu_pct"] is None and lines[1]["rss_mb"] == 0


def test_old_files_are_deleted_and_the_summary_reads_what_was_written(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "resource_log_keep_days", 3)
    d = resourcelog.log_dir()
    old = (datetime.now(UTC) - timedelta(days=10)).strftime("%Y-%m-%d")
    (d / f"resources-{old}.jsonl").write_text("{}\n")
    monkeypatch.setattr(supervisor, "_instances", {})
    s = resourcelog.Sampler()
    s.sample()
    assert not (d / f"resources-{old}.jsonl").exists()                                                     # past the retention
    now = datetime.now(UTC).isoformat(timespec="seconds")
    rows = [{"t": now, "kind": "host", "load1": 0.5, "steal_pct": 0.1, "psi_cpu": 0.2, "late_ms": 30},
            {"t": now, "kind": "host", "load1": 1.5, "steal_pct": 2.0, "psi_cpu": 3.0, "late_ms": 180},
            {"t": now, "kind": "server", "server": 1, "pid": 1, "cpu_pct": 20.0, "rss_mb": 50.0, "threads": 5, "players": 5, "session_type": 3},
            {"t": now, "kind": "server", "server": 1, "pid": 1, "cpu_pct": 40.0, "rss_mb": 60.0, "threads": 5, "players": 5, "session_type": 3}]
    (d / f"resources-{datetime.now(UTC):%Y-%m-%d}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    out = resourcelog.summarize(hours=1)
    assert "despertares tardíos (≥100 ms): 1" in out and "servidor 1" in out and "máx. 40.0%" in out
    assert "~6.00% de núcleo por piloto" in out                                                            # (20 + 40) % over (5 + 5) player-samples
    assert resourcelog.summarize(hours=1, directory=tmp_path / "empty").startswith("sin muestras")


def test_the_loop_samples_on_its_own_and_is_off_with_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(supervisor, "_instances", {})
    monkeypatch.setattr(settings, "resource_log_seconds", 0)
    asyncio.run(resourcelog.run_forever())                                                                 # 0 = off: returns at once, writes nothing
    assert not list((tmp_path / "logs").glob("*.jsonl")) if (tmp_path / "logs").exists() else True
    monkeypatch.setattr(settings, "resource_log_seconds", 0.05)

    async def go():
        t = asyncio.create_task(resourcelog.run_forever())
        await asyncio.sleep(0.4)
        t.cancel()
    asyncio.run(go())
    assert len([x for x in _lines(tmp_path) if x["kind"] == "host"]) >= 3
