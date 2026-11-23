"""Errors of the manager, the way Odoo shows them: a text log that keeps only a couple of days, and an id the admin panel pops up so the error can be found.

* `<data_dir>/logs/manager.log`: WARNING and above of the whole process, rotated at midnight, the last `ACM_ERROR_LOG_KEEP_DAYS` (2) days kept, the rest deleted by the handler itself.
  One process writes it (uvicorn, one worker), so the rotation cannot race; with several workers use logrotate instead. A message that repeats more than `THROTTLE_MAX` times a minute is
  dropped (one error in a loop must not fill the disk), and tokens / passwords / cookies are blanked before anything is written.
* An exception nobody caught answers 500 `{detail, error_id}` and nothing else: the traceback NEVER travels in a response (public routes of the game's hub share this process). It is kept in the
  log and in the last `RING` errors in memory, and an admin reads it by id from `GET /api/v1/errors/{id}` (the panel asks for it when the popup's «Detalles» is opened).
"""

from __future__ import annotations

import logging
import re
import secrets
import time
import traceback
from collections import deque
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.auth import require
from app.config import settings

log = logging.getLogger("acmanager.errors")
THROTTLE_MAX, THROTTLE_WINDOW = 5, 60.0   # the same message more than 5 times in a minute is not written again that minute
RING = deque(maxlen=100)                  # the last errors with their traceback: {id, ts, method, path, type, message, traceback}
_BEARER = re.compile(r"(?i)(bearer\s+)[^\s&\"',;)]+")   # (first: «Authorization: Bearer <token>» must not stop at the word Bearer)
_WHOLE = re.compile(r"(?i)((?:password|passwd|secret|authorization|cookie|set-cookie)[\"']?\s*[=:]\s*[\"']?)[^\r\n]+")   # (to the end of the line: a value may hold spaces, `;`, `&`)
_SECRET = re.compile(r"(?i)((?:token|api[_-]?key|session)[\"']?\s*[=:]\s*[\"']?)[^\s&\"',;)]+")
MAX_RECORD, GLOBAL_PER_WINDOW = 8000, 200   # one record never writes more than 8 KB; the file takes at most 200 records a minute, whatever they say


def redact(text: str) -> str:
    """Blanks what must never reach a file: bearer tokens and `token=`, `password=`, `secret`, `cookie`... values."""
    return _SECRET.sub(r"\1***", _WHOLE.sub(r"\1***", _BEARER.sub(r"\1***", text)))


class _SafeFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = redact(super().format(record))
        return out if len(out) <= MAX_RECORD else out[:MAX_RECORD] + "\n...[cut]"


class _Throttle(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self.seen: dict[tuple, list[float]] = {}   # (logger, message template) -> [window start, count]
        self.total = [0.0, 0]   # [window start, records written in it]

    def filter(self, record: logging.LogRecord) -> bool:
        key, now = (record.name, str(record.msg)[:200]), time.monotonic()
        if now - self.total[0] > THROTTLE_WINDOW:
            self.total = [now, 0]
        self.total[1] += 1
        if self.total[1] > GLOBAL_PER_WINDOW:   # (a storm of DIFFERENT messages: the per-message count would not stop it)
            return False
        win = self.seen.get(key)
        if not win or now - win[0] > THROTTLE_WINDOW:
            if len(self.seen) > 500:
                self.seen = {k: v for k, v in self.seen.items() if now - v[0] <= THROTTLE_WINDOW}
                if len(self.seen) > 2000:   # (unique messages in one window: the memory of the filter has a ceiling too)
                    self.seen.clear()
            self.seen[key] = [now, 1]
            return True
        win[1] += 1
        return win[1] <= THROTTLE_MAX


def setup() -> Path | None:
    """Starts the file (never breaks the start: no writable folder = no file, the journal still has everything). Returns its path."""
    try:
        d = Path(settings.data_dir) / "logs"
        d.mkdir(parents=True, exist_ok=True)
        path = d / "manager.log"
        h = TimedRotatingFileHandler(path, when="midnight", backupCount=max(1, settings.error_log_keep_days), encoding="utf-8", delay=True)
    except OSError as e:
        logging.getLogger("acmanager").warning("no error log file: %s", e)
        return None
    h.setLevel(logging.WARNING)
    h.setFormatter(_SafeFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    h.addFilter(_Throttle())
    root = logging.getLogger()
    if not any(getattr(x, "_acm_error_log", False) for x in root.handlers):   # (once per process: a reload must not double every line)
        h._acm_error_log = True
        root.addHandler(h)
    return path


def record(exc: BaseException, method: str, path: str) -> str:
    """Logs an uncaught exception with its traceback, keeps it in the ring, and returns its id."""
    eid = secrets.token_hex(4)
    tb = redact("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    RING.append({"id": eid, "ts": time.time(), "method": method, "path": path[:200], "type": type(exc).__name__, "message": redact(str(exc))[:300], "traceback": tb})
    log.error("error %s: %s %s\n%s", eid, method, path[:200], tb)
    return eid


async def on_exception(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": "Internal server error", "error_id": record(exc, request.method, request.url.path)})


router = APIRouter(prefix="/errors", tags=["errors"], dependencies=[Depends(require("admin"))])


@router.get("")
def recent() -> list[dict]:
    """The last errors, newest first, without their traceback (ask for one by id)."""
    return [{k: v for k, v in e.items() if k != "traceback"} for e in reversed(tuple(RING))]


@router.get("/{error_id}")
def one(error_id: str) -> dict:
    for e in tuple(RING):   # (a copy: another request may add one meanwhile)
        if e["id"] == error_id:
            return e
    raise HTTPException(404, "that error is no longer in memory (the log file may still have it)")
