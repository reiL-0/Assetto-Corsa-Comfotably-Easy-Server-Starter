"""One lock for everything that changes a league or archives it (app/championship.py, app/league.py, app/archive.py). «Is it archived?» and the write that follows
must not be separated by an archive in between, or a change could land on a league whose results were just deleted. The manager is ONE process (like the
supervisor and the other in-memory state: a restart is the only thing that ends it), so a process lock is enough; a second process would need a database-level claim
for every writer. Archiving holds it while it builds the snapshot (file reads) and commits: league edits wait for that, nothing else does."""
import threading
from functools import wraps

lock = threading.RLock()


def serialized(fn):
    """Run the whole handler with the league lock held."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        with lock:
            return fn(*args, **kwargs)
    return wrapper
