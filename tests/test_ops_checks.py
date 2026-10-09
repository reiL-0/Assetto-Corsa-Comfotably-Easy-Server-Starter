"""The repo-level checks of ops/ are themselves tested: they must pass here and must refuse what they exist to refuse (plan T5.2)."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(*args):
    return subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True, cwd=ROOT, timeout=60)


def test_agent_files_are_in_this_checkout():
    r = run("ops/check_agent_files.py")
    assert r.returncode == 0, r.stdout + r.stderr


def test_in_game_client_is_python_33_safe():
    r = run("ops/check_py33.py", "clients")
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_python_33_filter_refuses_what_33_cannot_take(tmp_path):
    (tmp_path / "bad.py").write_text('import typing\nx = f"{1}"\nimport subprocess\nsubprocess.run([])\nd = {**{}}\nasync def g():\n    await g()\n')
    r = run("ops/check_py33.py", tmp_path)
    assert r.returncode != 0
    for what in ("f-string", "typing", "subprocess.run", "dict unpacking", "async/await"):
        assert what in r.stderr + r.stdout, what
    (tmp_path / "bad.py").write_text("x = 1\n")
    assert run("ops/check_py33.py", tmp_path).returncode == 0
