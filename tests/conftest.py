import os
import tempfile

# Fresh throwaway DB + data dir per test session, before app import.
os.environ["ACM_DATA_DIR"] = tempfile.mkdtemp(prefix="acm-test-")
os.environ["ACM_ACSERVER_CMD"] = ""

# import must follow the env setup above
from app.db import init_db

init_db()
