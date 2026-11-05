"""Best-effort, process-local race audit. Producers never perform file/network I/O.

One bounded FIFO and one lazy daemon writer preserve enqueue order. Queue overflow
and persistence failures are observable; accepted does not mean persisted.
"""
import json
import logging
import math
import os
import queue
import re
import threading
import time
from collections import deque
from datetime import UTC, datetime
from itertools import islice
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import settings

log = logging.getLogger("acmanager.raceaudit")
_pending = queue.Queue(maxsize=4096)
_lock = threading.Lock()
_worker = None
_closed = False
_stop_sent = False
_STOP = object()
_counts = {"queued": 0, "written": 0, "errors": 0, "dropped": 0}
MAX_BYTES = 10 * 1024 * 1024
BACKUPS = 10

FIELD_TOTAL_BYTES = 200 * 1024 * 1024
FIELD_FILE_BYTES = 20 * 1024 * 1024


class _FieldWriter:
    """Writer-thread owned files; one active session per server, closed-only eviction."""
    def __init__(self):
        self.active = {}
        self.directory = Path(settings.data_dir) / "race-audit" / "field"

    def close(self):
        for _, _, handler in self.active.values():
            handler.close()
        self.active.clear()

    def retain(self, incoming=0):
        files = list(self.directory.glob("*.jsonl*"))
        total = sum(p.stat().st_size for p in files)
        opened = {Path(h.baseFilename) for _, _, h in self.active.values()}
        for path in sorted(files, key=lambda p: (p.stat().st_mtime_ns, p.name)):
            if total + incoming <= FIELD_TOTAL_BYTES:
                break
            if path.resolve() not in opened:
                size = path.stat().st_size
                path.unlink()
                total -= size
        if total + incoming > FIELD_TOTAL_BYTES:
            raise OSError("field retention full of active files")

    def write(self, event):
        sid, session = event.get("server_id", 0), event.get("session", "unknown")
        line = json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        size = len((line + "\n").encode("utf-8"))
        if size > FIELD_FILE_BYTES:
            raise ValueError("field event exceeds file cap")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        previous = self.active.get(sid)
        if previous and (previous[0] != session or Path(previous[2].baseFilename).stat().st_size + size > FIELD_FILE_BYTES):
            previous[2].close()
            del self.active[sid]
        if sid not in self.active:
            safe = lambda v: re.sub(r"[^a-zA-Z0-9_-]", "_", str(v))[:80]
            # Timestamp is the start of this recorded session; UUID disambiguates restarts.
            stem = f"server-{safe(sid)}-session-{safe(session)}"
            part = 0
            while (self.directory / f"{stem}-{part:06d}.jsonl").exists():
                part += 1
            path = self.directory / f"{stem}-{part:06d}.jsonl"
            handler = _Handler(path, maxBytes=0, backupCount=0)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self.active[sid] = (session, event.get("ts"), handler)
        self.retain(size)
        self.active[sid][2].handle(logging.LogRecord(log.name, logging.INFO, "", 0, line, (), None))


class _Handler(RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8")

    def handleError(self, record):
        raise OSError("audit write failed")


def _path():
    return Path(settings.data_dir) / "race-audit" / "events.jsonl"


def thresholds(*modules):
    """Capture the numeric constants actually used by the pure judges."""
    return {module.__name__.rsplit(".", 1)[-1]: {key: value for key, value in vars(module).items()
            if key.isupper() and not key.startswith("_") and isinstance(value, (int, float))}
            for module in modules}


def stats():
    with _lock:
        return {**_counts, "pending": _pending.qsize()}


def _write_loop():
    handler = None
    field_handler = None
    while True:
        event = _pending.get()
        if event is _STOP:
            try:
                if handler is not None:
                    handler.close()
                if field_handler is not None:
                    field_handler.close()
            except Exception:  # noqa: BLE001 - still terminate if closing storage fails
                with _lock:
                    _counts["errors"] += 1
            finally:
                _pending.task_done()
            return
        try:
            if event.get("format") == "acm-field-v1":
                if field_handler is None:
                    field_handler = _FieldWriter()
                field_handler.write(event)
                with _lock:
                    _counts["written"] += 1
                continue
            log.info("race audit server=%s kind=%s", event["server_id"], event["kind"])
            if handler is None:
                path = _path()
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                path.parent.chmod(0o700)
                handler = _Handler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS)
                handler.setFormatter(logging.Formatter("%(message)s"))
            line = json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            handler.handle(logging.LogRecord(log.name, logging.INFO, "", 0, line, (), None))
            with _lock:
                _counts["written"] += 1
        except Exception:  # noqa: BLE001 - audit persistence must never stop race control
            with _lock:
                _counts["errors"] += 1
            if field_handler:
                try:
                    field_handler.close()
                except Exception:  # noqa: BLE001 - failed storage cleanup is observable
                    with _lock:
                        _counts["errors"] += 1
                field_handler = None
            if handler:
                try:
                    handler.close()
                except Exception:  # noqa: BLE001 - close may fail after the write failed
                    with _lock:
                        _counts["errors"] += 1
            handler = None
        finally:
            _pending.task_done()


# Global traversal/text budgets keep producer work and serialized size bounded,
# including wide, deeply nested or cyclic evidence. Serialization stays in writer.
TRUNCATED = "[truncated]"


def _snapshot(value, budget=None, depth=0):
    if budget is None:
        budget = [256, 8192]  # nodes and total characters (including mapping keys)
    if budget[0] <= 0:
        return TRUNCATED
    budget[0] -= 1
    if isinstance(value, str):
        size = min(512, budget[1])
        budget[1] -= min(len(value), size)
        return value if len(value) <= size else value[:size] + TRUNCATED
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if value.bit_length() <= 64 else TRUNCATED
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite audit number")
        return value
    if isinstance(value, (dict, list, tuple)):
        if depth >= 6:
            return TRUNCATED
        result = {} if isinstance(value, dict) else []
        items = value.items() if isinstance(value, dict) else value
        count = 0
        for item in islice(items, 32):
            if budget[0] <= 1:
                break
            if isinstance(value, dict):
                key, child = item
                if not isinstance(key, (str, int, float, bool, type(None))):
                    raise TypeError("invalid audit key")
                key = _snapshot(key, budget, depth + 1)
                result[key] = _snapshot(child, budget, depth + 1)
            else:
                result.append(_snapshot(item, budget, depth + 1))
            count += 1
        if count < len(value):
            if isinstance(result, dict):
                result[TRUNCATED] = len(value) - count
            else:
                result.append({TRUNCATED: len(value) - count})
        return result
    raise TypeError("invalid audit evidence")


def record(server_id, kind, **fields):
    """Enqueue one JSON event, never raise. Only pass already-calculated evidence."""
    try:
        with _lock:
            if _closed or _pending.full():
                _counts["dropped"] += 1
                return
        event = _snapshot(fields)
        event.update(ts=datetime.now(UTC).isoformat(), monotonic=time.monotonic(),
                     server_id=_snapshot(server_id), kind=_snapshot(kind))
        event.setdefault("session", None)
        enqueue(event)
    except Exception:  # noqa: BLE001 - malformed evidence must not affect race control
        with _lock:
            _counts["errors"] += 1


def enqueue(event):
    """Admit an owned, already bounded snapshot; shared by audit and field tape."""
    global _worker
    try:
        with _lock:
            if _closed or _pending.full():
                _counts["dropped"] += 1
                return False
            if _worker is None or not _worker.is_alive():
                _worker = threading.Thread(target=_write_loop, name="race-audit", daemon=True)
                _worker.start()
            _pending.put_nowait(event)
            _counts["queued"] += 1
            return True
    except queue.Full:
        with _lock:
            _counts["dropped"] += 1
    except Exception:  # noqa: BLE001 - admission must never affect telemetry
        with _lock:
            _counts["errors"] += 1
    return False


def flush(timeout=8):
    """Wait outside telemetry for accepted events, bounded even on slow storage."""
    deadline = time.monotonic() + timeout
    with _pending.all_tasks_done:
        while _pending.unfinished_tasks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _pending.all_tasks_done.wait(remaining)
    return True


def close(timeout=8):
    """Stop admission, drain accepted events and join within one timeout budget.

    False means storage is still busy; a later close can finish the shutdown.
    """
    global _closed, _stop_sent
    deadline = time.monotonic() + max(0, timeout)
    with _lock:
        _closed = True
        worker = _worker
    if worker is None:
        return True
    if not flush(max(0, deadline - time.monotonic())):
        return False
    with _lock:
        if not _stop_sent:
            _pending.put_nowait(_STOP)  # drained and admission is closed
            _stop_sent = True
    worker.join(max(0, deadline - time.monotonic()))
    return not worker.is_alive()


def read(server_id, since=None, limit=100):
    """Scan newest retained files first, only on request threads."""
    limit = max(1, min(limit, 1000))
    found = []
    path = _path()
    for p in [path, *(Path(str(path) + f".{i}") for i in range(1, BACKUPS + 1))]:
        recent = deque(maxlen=limit - len(found))
        try:
            with p.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                        if event["server_id"] == server_id and (since is None or event["ts"] >= since):
                            recent.append(event)
                    except (ValueError, KeyError):
                        continue
        except FileNotFoundError:
            continue
        except OSError as exc:
            return {"events": found, "stats": stats(), "read_error": type(exc).__name__}
        found = list(recent) + found
        if len(found) >= limit:
            break
    return {"events": found, "stats": stats()}
