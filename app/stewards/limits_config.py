"""Per-server immutable settings. Workers load; live callbacks only peek.

A generation prevents a read started before PUT from publishing after it.
PUT publishes its committed snapshot immediately, including for UDP callbacks.
State is process-local, like the supervisor and RaceControl.
"""
import logging
import threading

from sqlalchemy import update
from sqlmodel import Session

from app.db import engine
from app.models import Setting
from app.stewards.limits_filter import TrackLimitsConfig

log = logging.getLogger(__name__)

_lock = threading.Lock()
_write_lock = threading.Lock()  # request workers only; serializes commit/publication
_cache: dict[int, TrackLimitsConfig] = {}
_generation: dict[int, int] = {}
DEFAULT = TrackLimitsConfig()


def peek(server_id: int) -> TrackLimitsConfig:
    with _lock:
        return _cache.get(server_id, DEFAULT)


def load(server_id: int) -> TrackLimitsConfig:
    while True:
        with _lock:
            if server_id in _cache:
                return _cache[server_id]
            generation = _generation.get(server_id, 0)
        with Session(engine) as sess:
            try:
                row = sess.get(Setting, f"track_limits:{server_id}")
                # Retired keys remain readable; PUT still forbids unknown fields.
                value = row.value if row else None
                if isinstance(value, dict):
                    value = {k: v for k, v in value.items() if k in TrackLimitsConfig.model_fields}
                cfg = TrackLimitsConfig.model_validate(value) if row else DEFAULT
            except ValueError as exc:
                # Validation/JSON errors can embed the entire stored value.
                log.warning("Invalid track-limit config for server %s; using defaults", server_id,
                            exc_info=(ValueError, ValueError("Invalid stored configuration"),
                                      exc.__traceback__))
                cfg = DEFAULT
        with _lock:
            if generation == _generation.get(server_id, 0):
                return _cache.setdefault(server_id, cfg)
            # A concurrent PUT won: retry against its published snapshot.


def invalidate(server_id: int, snapshot: TrackLimitsConfig) -> None:
    with _lock:
        _generation[server_id] = _generation.get(server_id, 0) + 1
        _cache[server_id] = snapshot


def save(sess: Session, server_id: int, cfg: TrackLimitsConfig) -> None:
    with _write_lock:
        key = f"track_limits:{server_id}"
        # Update without decoding the old JSON, which may itself be corrupt.
        result = sess.execute(update(Setting).where(Setting.key == key).values(value=cfg.model_dump()))
        if not result.rowcount:
            sess.add(Setting(key=key, value=cfg.model_dump()))
        sess.commit()
        invalidate(server_id, cfg)
