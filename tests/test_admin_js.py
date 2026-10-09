"""The admin pages' JavaScript has no browser here: its async rules (plan T5.1) are run by node against a fake DOM. Skipped without node."""
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).parent / "js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_servers_page_async_rules():
    r = subprocess.run(["node", str(JS / "check_servers_page.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "servers page ok" in r.stdout, r.stdout + r.stderr
