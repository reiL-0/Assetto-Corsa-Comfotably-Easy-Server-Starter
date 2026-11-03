"""The connection pool must not be smaller than the thread pool that borrows from it (anyio's default limiter is 40):
with fewer connections than threads a burst of FileResponse requests (skin logos) deadlocks the pool for good."""
from app.db import engine

REQUEST_THREADS = 40   # anyio's default


def test_pool_covers_every_request_thread():
    assert engine.pool.size() + engine.pool._max_overflow >= REQUEST_THREADS
