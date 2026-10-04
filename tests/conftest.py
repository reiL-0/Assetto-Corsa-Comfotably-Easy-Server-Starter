import os
import tempfile

# Fresh throwaway DB + data dir per test session, before app import.
os.environ["ACM_DATA_DIR"] = tempfile.mkdtemp(prefix="acm-test-")
os.environ["ACM_ACSERVER_CMD"] = ""
os.environ["ACM_PORT_RANGE_END"] = "12000"  # every test makes its own server; the default pool holds only 25

# import must follow the env setup above
from app.db import init_db

init_db()

# Auth bootstrap: a real admin + Bearer token, shared by every test client.
import secrets
from datetime import UTC, datetime

from sqlmodel import Session

from app.auth import _sha
from app.db import engine
from app.models import Token, User

_RAW = secrets.token_urlsafe(16)
with Session(engine) as _s:
    _admin = User(username="root", role="admin")
    _s.add(_admin)
    _s.commit()
    _s.add(Token(user_id=_admin.id, token_hash=_sha(_RAW), name="tests", created_at=datetime.now(UTC)))
    _s.commit()

ADMIN = {"Authorization": f"Bearer {_RAW}"}
