"""A plain log file of what the machine and every game server use: the data to size hosting (how many players fit per core, is the VPS stalling).

Written every `ACM_RESOURCE_LOG_SECONDS` (30 by default, 0 = off) as JSON lines in `<data_dir>/logs/resources-YYYY-MM-DD.jsonl`. It lives **outside the web app**:
no table, no endpoint, nothing in the pages; read it over ssh or with `python -m app.resourcelog summary [--hours 12]`. Files older than
`ACM_RESOURCE_LOG_KEEP_DAYS` (30) are deleted.

Each cycle writes one `host` line and one `server` line per running acServer:
- `server`: `server`, `pid`, `cpu_pct` (of ONE core since the previous sample; null on the first), `rss_mb`, `threads`, `players` (connected), `session_type`.
- `host`: `load1`, `steal_pct` (CPU the hypervisor took since the last sample), `psi_cpu` (share of time something waited for a CPU, last 10 s) and `late_ms`:
  how late this loop woke up. A VM that is stalled wakes late, so a burst of large `late_ms` next to a «server CPU overload» warning is the signature of jitter
  from the host rather than of a busy server.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config import settings

CLK = os.sysconf("SC_CLK_TCK")
LATE_MS = 100   # a wake-up later than this counts as a stall in the summary (acServer itself warns above 100 ms)


def log_dir() -> Path:
    d = Path(settings.data_dir) / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _proc_ticks(pid: int) -> int | None:
    try:
        f = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(f[11]) + int(f[12])   # utime + stime
    except (OSError, IndexError, ValueError):
        return None


def _proc_status(pid: int) -> tuple[float, int]:
    rss, threads = 0.0, 0
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1]) / 1024
            elif line.startswith("Threads:"):
                threads = int(line.split()[1])
    except (OSError, ValueError):
        pass
    return round(rss, 1), threads


def _host_cpu() -> tuple[int, int]:
    """(total, steal) jiffies since boot."""
    try:
        f = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
        return sum(f), f[7] if len(f) > 7 else 0
    except (OSError, ValueError, IndexError):
        return 0, 0


def _psi_cpu() -> float | None:
    try:
        return float(Path("/proc/pressure/cpu").read_text().split()[1].split("=")[1])
    except (OSError, ValueError, IndexError):
        return None


class Sampler:
    def __init__(self) -> None:
        self.prev: dict[int, tuple[int, float]] = {}   # pid -> (ticks, wall clock)
        self.prev_host: tuple[int, int] | None = None
        self.day = ""

    def sample(self, late_ms: float = 0.0, now: float | None = None) -> list[dict]:
        """One cycle: the lines are returned and appended to today's file."""
        from app import supervisor   # lazy: supervisor pulls in a lot
        now = time.monotonic() if now is None else now
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        total, steal = _host_cpu()
        steal_pct = None
        if self.prev_host and total > self.prev_host[0]:
            steal_pct = round((steal - self.prev_host[1]) * 100 / (total - self.prev_host[0]), 2)
        self.prev_host = (total, steal)
        lines = [{"t": stamp, "kind": "host", "load1": os.getloadavg()[0], "steal_pct": steal_pct, "psi_cpu": _psi_cpu(), "late_ms": round(late_ms)}]
        for sid, inst in list(supervisor._instances.items()):
            if not getattr(inst, "running", False) or not inst.pid:
                continue
            ticks, cpu = _proc_ticks(inst.pid), None
            if ticks is not None and inst.pid in self.prev and now > self.prev[inst.pid][1]:
                cpu = round((ticks - self.prev[inst.pid][0]) / CLK * 100 / (now - self.prev[inst.pid][1]), 1)
            if ticks is not None:
                self.prev[inst.pid] = (ticks, now)
            rss, threads = _proc_status(inst.pid)
            board = inst.acsp.board if inst.acsp else None
            lines.append({"t": stamp, "kind": "server", "server": sid, "pid": inst.pid, "cpu_pct": cpu, "rss_mb": rss, "threads": threads,
                          "players": sum(1 for d in board.drivers if d.connected) if board else None,
                          "session_type": (board.session or {}).get("session_type") if board else None})
        self._write(lines)
        return lines

    def _write(self, lines: list[dict]) -> None:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        d = log_dir()
        if today != self.day:   # a new day: drop the files that are past the retention
            self.day = today
            cutoff = (datetime.now(UTC) - timedelta(days=settings.resource_log_keep_days)).strftime("%Y-%m-%d")
            for f in d.glob("resources-*.jsonl"):
                if f.stem.removeprefix("resources-") < cutoff:
                    f.unlink(missing_ok=True)
        with (d / f"resources-{today}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(x, separators=(",", ":")) + "\n" for x in lines))


async def run_forever() -> None:
    period = settings.resource_log_seconds
    if period <= 0:
        return
    s, loop = Sampler(), asyncio.get_running_loop()
    while True:
        t0 = loop.time()
        await asyncio.sleep(period)
        s.sample(max(0.0, (loop.time() - t0 - period) * 1000))


# --- summary (python -m app.resourcelog summary --hours 12) --------------------------------------------------------------

def _pct(vals: list[float], p: float) -> float:
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(len(vals) * p))] if vals else 0.0


def summarize(hours: float = 12.0, directory: Path | None = None) -> str:
    since = datetime.now(UTC) - timedelta(hours=hours)
    rows: list[dict] = []
    for f in sorted((directory or log_dir()).glob("resources-*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if datetime.fromisoformat(r["t"]) >= since:
                rows.append(r)
    host = [r for r in rows if r["kind"] == "host"]
    if not host:
        return f"sin muestras en las últimas {hours:g} h"
    out = [f"== {len(host)} muestras del equipo en las últimas {hours:g} h (desde {host[0]['t']})"]
    steal = [r["steal_pct"] for r in host if r["steal_pct"] is not None]
    late = [r for r in host if r["late_ms"] >= LATE_MS]
    out.append(f"   carga media máx. {max(r['load1'] for r in host):.2f} · steal medio {sum(steal) / len(steal) if steal else 0:.2f}% (máx. {max(steal) if steal else 0:.2f}%) · "
               f"presión de CPU máx. {max((r['psi_cpu'] or 0) for r in host):.2f}")
    out.append(f"   despertares tardíos (≥{LATE_MS} ms): {len(late)}" + ("  -> " + ", ".join(f"{r['t'][11:19]} +{r['late_ms']}ms" for r in late[:8]) if late else ""))
    for sid in sorted({r["server"] for r in rows if r["kind"] == "server"}):
        s = [r for r in rows if r["kind"] == "server" and r["server"] == sid]
        cpu = [r["cpu_pct"] for r in s if r["cpu_pct"] is not None]
        busy = [r for r in s if (r["players"] or 0) > 0 and r["cpu_pct"] is not None]
        per = (sum(r["cpu_pct"] for r in busy) / sum(r["players"] for r in busy)) if busy else 0
        out.append(f"== servidor {sid}: {len(s)} muestras · CPU media {sum(cpu) / len(cpu) if cpu else 0:.1f}% · p95 {_pct(cpu, .95):.1f}% · máx. {max(cpu) if cpu else 0:.1f}% de un núcleo · "
                   f"RAM máx. {max(r['rss_mb'] for r in s):.0f} MB · pilotos máx. {max((r['players'] or 0) for r in s)} · con gente: ~{per:.2f}% de núcleo por piloto")
    return "\n".join(out)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] != ["summary"]:
        sys.exit("uso: python -m app.resourcelog summary [--hours N]")
    print(summarize(float(args[args.index("--hours") + 1]) if "--hours" in args else 12.0))
