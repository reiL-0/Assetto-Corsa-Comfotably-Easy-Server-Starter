"""Unpacks an uploaded archive in a disposable, limited child process (the defence against zip bombs and bad parsers).

The manager never opens an upload itself: `run_sandboxed` starts `python -m app.unpack` as a child with
- a systemd scope (when `ACM_LIMITS_SCOPE` is on) capping memory, CPU and task count and stopping it after `RuntimeMaxSec`, plus rlimits (CPU time, file size);
- a wall-clock timeout in the parent, which kills the child's whole process group;
- a real byte/file counter: the child writes member by member and stops the moment a limit is crossed, whatever the headers claimed;
- a free-disk check before it starts (the scratch dir is on the data disk) and one unpacking at a time (`SLOTS`): the others queue.
The child result is one JSON line. Only `.zip` is accepted (decision 2026-10-06: no `.rar`, whose unpacker is an external program with a history of bugs, and no loose files). Only the child ever touches the untrusted bytes. Stronger isolation (container, gVisor) can wrap the same command later.
"""

from __future__ import annotations

import json
import os
import resource
import shutil
import signal
import stat
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

from app import uploadguard
from app.config import settings

NOT_ZIP = "only .zip archives are accepted (not .rar, and no loose files): put the car or track folder inside one .zip"
SLOTS = threading.Semaphore(1)   # ponytail: one unpacking at a time (they are rare and heavy); raise if uploads queue up
CHUNK = 1 << 20
MIN_FREE_MARGIN = 1.2            # free disk needed = the byte limit x this


# ---- the child ------------------------------------------------------------------------------------------------------

def _extract_zip(archive: Path, dest: Path, max_total: int, max_files: int, max_file: int) -> tuple[int, int]:
    total = files = 0
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            name = uploadguard.safe_member(info.filename)
            if stat.S_ISLNK(info.external_attr >> 16):
                raise uploadguard.Rejected(f"symbolic link in the archive: {info.filename!r}")
            target = dest / name
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            files += 1
            if files > max_files:
                raise uploadguard.Rejected(f"too many files in the archive (over {max_files})")
            target.parent.mkdir(parents=True, exist_ok=True)
            one = 0
            with zf.open(info) as src, target.open("wb") as out:
                while chunk := src.read(CHUNK):
                    one += len(chunk)
                    total += len(chunk)
                    if one > max_file or total > max_total:   # counted as it is written: the headers may lie
                        raise uploadguard.Rejected("the archive unpacks to more than the allowed size")
                    out.write(chunk)
    return files, total


def child_main(argv: list[str]) -> int:
    archive, dest = Path(argv[0]), Path(argv[1])
    max_total, max_files, max_file, cpu_s = (int(x) for x in argv[2:6])
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 5))
    resource.setrlimit(resource.RLIMIT_FSIZE, (max_file + CHUNK, max_file + CHUNK))   # no single file past the limit, bsdtar included
    try:
        if archive.open("rb").read(4) != b"PK\x03\x04":
            raise uploadguard.Rejected(NOT_ZIP)
        files, total = _extract_zip(archive, dest, max_total, max_files, max_file)
        print(json.dumps({"ok": True, "files": files, "bytes": total}))
        return 0
    except (uploadguard.Rejected, zipfile.BadZipFile) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 3
    except OSError as e:   # a file past the size limit (EFBIG), a name that is both file and folder, a full disk…
        print(json.dumps({"ok": False, "error": f"cannot unpack the archive ({e.strerror or type(e).__name__})"}))
        return 3


# ---- the parent ------------------------------------------------------------------------------------------------------

def _command(archive: Path, dest: Path, timeout: int, max_total: int, max_files: int, max_file: int) -> list[str]:
    scope: list[str] = []
    if settings.limits_scope in ("user", "system"):   # same switch as the per-server limits (supervisor.limit_prefix)
        scope = ["systemd-run", "--scope", "--quiet", "--collect", "-p", f"MemoryMax={settings.unpack_mem_mb}M", "-p", "MemorySwapMax=0",
                 "-p", "CPUQuota=100%", "-p", "TasksMax=64", "-p", f"RuntimeMaxSec={timeout + 10}"] + (["--user"] if settings.limits_scope == "user" else [])
    return [*scope, sys.executable, "-m", "app.unpack", str(archive), str(dest), str(max_total), str(max_files), str(max_file), str(timeout)]


def run_sandboxed(archive: Path, dest: Path, *, timeout: int | None = None, max_total: int = uploadguard.MAX_TOTAL,
                  max_files: int = uploadguard.MAX_FILES, max_file: int = uploadguard.MAX_FILE) -> dict:
    """Unpacks `archive` into the (empty) `dest` in the limited child. Returns {files, bytes}; raises `uploadguard.Rejected` with a sentence, or
    `OSError` when the machine cannot take it (not enough free disk)."""
    timeout = timeout or settings.unpack_timeout
    free = shutil.disk_usage(dest).free
    if free < max_total * MIN_FREE_MARGIN:
        max_total = int(free / MIN_FREE_MARGIN)   # never promise more than the disk can hold
        if max_total < 256 * 1024**2:
            raise OSError("not enough free disk space to unpack this archive right now")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent), "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"}
    with SLOTS:   # the others wait here, one unpacking at a time
        p = subprocess.Popen(_command(archive, dest, timeout, max_total, max_files, max_file), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env=env, start_new_session=True)
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)   # the child and whatever it started
            p.communicate()
            raise uploadguard.Rejected(f"unpacking took longer than {timeout} s and was stopped") from None
    try:
        res = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        # killed by a limit (memory, cpu time, file size) before it could answer
        raise uploadguard.Rejected("the archive could not be unpacked within the allowed resources (it was stopped)") from None
    if not res.get("ok"):
        raise uploadguard.Rejected(res.get("error", "cannot unpack the archive"))
    return res


if __name__ == "__main__":
    sys.exit(child_main(sys.argv[1:]))
