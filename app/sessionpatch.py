"""More than three sessions in one acServer cycle (practice, qualify, race, practice, race ...): the session sequence of a preset.

acServer reads three fixed sections ([PRACTICE] [QUALIFY] [RACE]) into `main.Sessions`, a Go slice it walks with index+1 modulo its length, and ACSP `SET_SESSION_INFO` can rename and resize a slot (name, type, laps,
time, wait) but never add one. So after the server starts, the slice is replaced by a longer one IN MEMORY (up to MAX_SESSIONS): `runtime.makeslice` is CALLED by hand from the thread that reaches
`main.broadcastSession` (the manager asks for it with RESTART_SESSION: same session, no index change), the sessions are copied, `main.Sessions` and `main.CurrentSession` repointed, and every slot is then defined with
`SET_SESSION_INFO` from the sequence. Nothing on disk changes, a restart of the server undoes it, and the manager does it again at every start, before anybody joins (a client learns the session list when it connects).
The layout facts (a Session is 15 words of 4 bytes, which words are static) come from decompiling the server and are described in acserver-re/patches/grow_sessions.py, where this code comes from.
The ptrace work runs in a PROCESS of its own (`python -m app.sessionpatch`, started by `install`): inside the manager, asyncio's thread that waits for the acServer child shares the thread group of the tracer and swallows the stop events of the server's main thread (seen on staging), and a helper that dies detaches everything by itself.
Only for the acServer build of `gridtimes.ACSERVER_SHA256`; on any other the sequence is not installed and the server runs its three sessions (it says so in the log).

A sequence step is {kind: practice|qualify|race, name, time_min, laps, wait_s}; the first three sections of the server's config are the originals the extra slots copy their fixed words from (by kind).
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import signal
import json
import struct
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("acm.sessionpatch")

SESSIONS, CURRENT, MAKESLICE, SESSION_TYPE, BROADCAST = 0x83E2EB8, 0x83E2734, 0x807F460, 0x82A5140, 0x8218090   # main.Sessions, main.CurrentSession, runtime.makeslice, main.Session's type, main.broadcastSession
CURIDX = 0x83F45E0   # main.CurrentSessionIndex (the same as app/gridtimes.py CURRENT_INDEX; not imported: this module must stay light, it is also run as a program)
WORDS, STRIDE = 15, 60
MAX_SESSIONS = 12   # also the ceiling of SetSessionIn.index (app/schemas/servers.py)
SERVER_OPTIONS = 0x83E3600   # main.ServerOptions: the server's global options. raceOverTime (+8) and resultScreenTime (+132) are int32 MILLISECONDS (RACE_OVER_TIME=60 reads 60000)
RACE_OVER_AT, RESULT_SCREEN_AT = SERVER_OPTIONS + 8, SERVER_OPTIONS + 132
MAX_WAIT_MS = 7_200_000   # a value read beyond this is not what we think it is: nothing is written
STATIC = (0, 1, 2, 3, 4, 5, 10, 11, 12)   # the words copied from the original slot: name, type, time, laps, wait, is_open. 6-9 are runtime (start / end time), 13 is the slot's own index
TYPES = {"practice": 1, "qualify": 2, "race": 3}
PT_ATTACH, PT_DETACH, PT_CONT, PT_GETREGS, PT_SETREGS = 16, 17, 7, 12, 13
WALL = 0x40000000
BENIGN = (signal.SIGURG, signal.SIGCHLD, signal.SIGALRM, signal.SIGPROF, signal.SIGWINCH)   # signals a Go thread gets in normal life (preemption, timers): passed on while we work. Anything else during our call (a SIGSEGV from a bad call) aborts and puts everything back
_NAMES = "r15 r14 r13 r12 rbp rbx r11 r10 r9 r8 rax rcx rdx rsi rdi orig_rax rip cs eflags rsp ss fs_base gs_base ds es fs gs".split()


class Regs(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in _NAMES]


libc = ctypes.CDLL(None, use_errno=True)
libc.ptrace.argtypes = [ctypes.c_long, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]
libc.ptrace.restype = ctypes.c_long


def layout_for(sequence: list[dict], kinds: list[int]) -> list[int]:
    """For each step, which ORIGINAL slot (index in the server's own sections) its fixed words come from: the one of its kind. `kinds`: the type of each original slot, in order."""
    out = []
    for step in sequence:
        t = TYPES[step["kind"]]
        if t not in kinds:
            raise ValueError(f"the sequence has a {step['kind']} but the server has no such section")
        out.append(kinds.index(t))
    return out


def _pt(req: int, tid: int, addr: int = 0, data: int = 0) -> int:
    r = libc.ptrace(req, tid, ctypes.c_void_p(addr), ctypes.c_void_p(data))
    if r == -1 and ctypes.get_errno():
        raise OSError(ctypes.get_errno(), f"ptrace({req}, {tid}): {os.strerror(ctypes.get_errno())}")
    return r


def _wait_stop(tid: int, timeout: float = 5.0) -> int | None:
    """Signal of the next stop of this thread; None on timeout."""
    end = time.time() + timeout
    while time.time() < end:
        p, st = os.waitpid(tid, WALL | os.WNOHANG)
        if p == tid:
            if os.WIFSTOPPED(st):
                return os.WSTOPSIG(st)
            raise RuntimeError(f"thread {tid} ended: status {st:#x}")
        time.sleep(0.001)
    return None


def _tgkill(pid: int, tid: int, sig: int) -> None:
    libc.syscall(234, pid, tid, sig)   # x86_64


def _stop(pid: int, tid: int, resend: bool = True) -> bool:
    """Make a RUNNING traced thread stop; True once it is in a ptrace-stop with its SIGSTOP consumed (a thread that has ended counts as stopped: there is nothing left to do). A SIGTRAP is ours and is never passed on:
    if the thread had just hit the breakpoint (rip right after it) it is moved back to its start and let go, so it runs the function for real and then meets the SIGSTOP."""
    try:
        if resend:   # (not again for a thread that was already asked: two pending SIGSTOPs would stop the whole process after the detach)
            _tgkill(pid, tid, int(signal.SIGSTOP))
        for _ in range(50):
            sig = _wait_stop(tid, 0.2)
            if sig == signal.SIGSTOP:
                return True
            if sig is None:
                continue
            if sig == signal.SIGTRAP:
                r = Regs()
                _pt(PT_GETREGS, tid, 0, ctypes.addressof(r))
                if r.rip == BROADCAST + 1:
                    r.rip = BROADCAST
                    _pt(PT_SETREGS, tid, 0, ctypes.addressof(r))
                _pt(PT_CONT, tid, 0, 0)
            else:
                _pt(PT_CONT, tid, 0, sig)
    except RuntimeError:   # the thread ended
        return True
    return False


def _attach_all(pid: int, attached: set[int], running: set[int]) -> None:
    """Attach to every thread there is; a thread that ends meanwhile is skipped and the list is read again until a pass finds nothing new (Go starts threads when it needs them: one that is not traced would die on the breakpoint)."""
    while True:
        new = {int(t) for t in os.listdir(f"/proc/{pid}/task")} - attached
        if not new:
            return
        for t in sorted(new):
            try:
                _pt(PT_ATTACH, t)
            except OSError as e:
                if e.errno == 3:   # ESRCH: gone already
                    attached.add(t)
                    continue
                raise
            attached.add(t)
            if _wait_stop(t) is None:
                running.add(t)   # its attach-stop has not come: it counts as running (it will be stopped, or detached, like any other)


def grow(pid: int, layout: list[int], timeout: float = 30.0, cancel: threading.Event | None = None, armed: threading.Event | None = None) -> str:
    """Replace main.Sessions of the RUNNING acServer `pid` by a slice of len(layout) sessions (slot i takes the fixed words of ORIGINAL slot layout[i]). Blocks until a thread reaches broadcastSession (something must
    make it: RESTART_SESSION; `cancel` set from outside gives up; `armed` is set when the breakpoint is in and every thread runs: only then may something ask for the session change). Every ptrace call is made from this thread (the tracer is a thread, not a process): run it with asyncio.to_thread, once. Raises RuntimeError with the reason when it cannot.
    Whatever happens the breakpoints are taken out, the thread that was moved gets its registers back and every traced thread is stopped and detached: a thread left attached would stop at its next signal and nobody would wake it."""
    mem = open(f"/proc/{pid}/mem", "r+b", 0)
    rd = lambda a, n: (mem.seek(a), mem.read(n))[1]
    wr = lambda a, b: (mem.seek(a), mem.write(b))
    arr0, ln, _cp = struct.unpack("<III", rd(SESSIONS, 12))
    if not 1 <= ln <= 3:
        raise RuntimeError(f"main.Sessions has {ln} entries: not an untouched server (already grown, or not this build)")
    if not ln < len(layout) <= MAX_SESSIONS or max(layout) >= ln:
        raise RuntimeError(f"layout of {len(layout)} sessions does not fit a server with {ln} originals")
    old = [list(struct.unpack(f"<{WORDS}I", rd(arr0 + i * STRIDE, STRIDE))) for i in range(ln)]
    attached: set[int] = set()
    running: set[int] = set()   # attached threads that are NOT in a ptrace-stop right now
    bp_orig = None              # the byte under the breakpoint at broadcastSession, while it is in
    ret_fix = None              # (address, byte) of the breakpoint at the return address of makeslice, while it is in
    moved = None                # (thread, its registers before we moved it) while it is somewhere it must not stay
    try:
        _attach_all(pid, attached, running)
        bp_orig = rd(BROADCAST, 1)
        wr(BROADCAST, b"\xcc")
        for t in sorted(attached - running):
            try:
                _pt(PT_CONT, t)
                running.add(t)
            except OSError:
                pass
        if armed:
            armed.set()
        hit, end = None, time.time() + timeout
        while hit is None and time.time() < end and not (cancel and cancel.is_set()):
            for t in list(running):
                p, st = os.waitpid(t, WALL | os.WNOHANG)
                if p != t or not os.WIFSTOPPED(st):
                    continue
                running.discard(t)
                sig = os.WSTOPSIG(st)
                if sig == signal.SIGTRAP:
                    r = Regs()
                    _pt(PT_GETREGS, t, 0, ctypes.addressof(r))
                    if r.rip == BROADCAST + 1:
                        hit = t
                        break
                _pt(PT_CONT, t, 0, 0 if sig in (signal.SIGTRAP, signal.SIGSTOP) else sig)   # not ours (Go's SIGURG preemption ...): pass it on
                running.add(t)
            time.sleep(0.0005)
        if hit is None:
            raise RuntimeError("cancelled" if cancel and cancel.is_set() else f"no session change within {timeout:.0f} s")
        r0 = Regs()
        _pt(PT_GETREGS, hit, 0, ctypes.addressof(r0))
        regs = Regs.from_buffer_copy(bytes(r0))
        r0.rip = BROADCAST   # where it goes back to: the entry of broadcastSession, as if it had not run yet
        saved = Regs.from_buffer_copy(bytes(r0))
        regs.rip = BROADCAST
        moved = (hit, saved)   # from now on it is stopped one byte INTO an instruction: whatever happens it must get these registers back before it is let go
        wr(BROADCAST, bp_orig)   # nobody else may trip over the breakpoint
        bp_orig = None
        for t in sorted(running):   # everybody else stops while the slice is swapped
            if _stop(pid, t):
                running.discard(t)
        if running:
            raise RuntimeError(f"threads {sorted(running)} would not stop")
        esp0 = r0.rsp & 0xFFFFFFFF
        ret = struct.unpack("<I", rd(esp0, 4))[0]
        cur_ptr, cur_idx = struct.unpack("<I", rd(CURRENT, 4))[0], struct.unpack("<i", rd(CURIDX, 4))[0]
        sp = (esp0 - 0x40) & ~0xF
        wr(sp, struct.pack("<IIII", ret, SESSION_TYPE, MAX_SESSIONS, MAX_SESSIONS) + b"\0" * 16)   # makeslice(type, len, cap) with the REAL return address: the stack still unwinds
        ret_fix = (ret, rd(ret, 1))
        wr(ret, b"\xcc")
        regs.rsp, regs.rip, regs.orig_rax = sp, MAKESLICE, 0xFFFFFFFFFFFFFFFF
        _pt(PT_SETREGS, hit, 0, ctypes.addressof(regs))
        _pt(PT_CONT, hit)
        running.add(hit)
        while True:
            sig = _wait_stop(hit, 10)
            if sig is None:
                raise RuntimeError("runtime.makeslice did not come back")
            running.discard(hit)
            if sig == signal.SIGTRAP:
                break
            if sig not in BENIGN:
                raise RuntimeError(f"the call to runtime.makeslice was met by signal {sig}")
            _pt(PT_CONT, hit, 0, sig)
            running.add(hit)
        wr(ret_fix[0], ret_fix[1])
        ret_fix = None
        new, nl, nc = struct.unpack("<III", rd(sp + 16, 12))
        if (nl, nc) != (MAX_SESSIONS, MAX_SESSIONS) or not new:
            raise RuntimeError(f"makeslice returned {new:#x} len {nl} cap {nc}")
        for i, src in enumerate(layout):
            w = list(old[i]) if i < ln else [0] * WORDS
            for k in STATIC:
                w[k] = old[src][k]
            w[13] = i
            wr(new + i * STRIDE, struct.pack(f"<{WORDS}I", *w))
        if cur_ptr:
            wr(CURRENT, struct.pack("<I", new + max(cur_idx, 0) * STRIDE))
        wr(SESSIONS, struct.pack("<III", new, len(layout), MAX_SESSIONS))
        _pt(PT_SETREGS, hit, 0, ctypes.addressof(saved))   # back to the entry of broadcastSession, breakpoints gone
        moved = None
        return f"grown to {len(layout)} sessions (current index {cur_idx})"
    finally:
        for fix in (lambda: bp_orig is not None and wr(BROADCAST, bp_orig), lambda: ret_fix is not None and wr(ret_fix[0], ret_fix[1])):
            try:
                fix()
            except OSError:
                pass
        if moved is not None:   # a failure while it was away: stop it where it is and give it back its registers
            try:
                if moved[0] in running and _stop(pid, moved[0]):
                    running.discard(moved[0])
                if moved[0] not in running:
                    _pt(PT_SETREGS, moved[0], 0, ctypes.addressof(moved[1]))
            except (OSError, RuntimeError):
                pass
        for t in sorted(attached):
            try:
                if t in running and not _stop(pid, t):
                    _stop(pid, t, resend=False)   # once more, waiting only
                _pt(PT_DETACH, t, 0, 0)
            except (OSError, RuntimeError):
                pass
        try:
            os.kill(pid, signal.SIGCONT)   # if a stop signal slipped through anyway, the process must not stay stopped
        except OSError:
            pass
        mem.close()


async def _run_helper(pid: int, layout: list[int], on_armed) -> str:
    """Run `grow` in a helper process: it prints ARMED once the breakpoint is in and every thread runs (then `on_armed()` asks for the session change), and one last line, OK ... or ERR ... . A cancel or a timeout
    SIGTERMs it: it puts everything back before it goes (killing it outright would not). Raises RuntimeError with the reason."""
    root = Path(__file__).resolve().parent.parent
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "app.sessionpatch", str(pid), json.dumps(layout), "30", cwd=root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        last = ""
        async with asyncio.timeout(45):
            while line := await proc.stdout.readline():
                last = line.decode().strip()
                if last == "ARMED":
                    on_armed()
            await proc.wait()
        if proc.returncode != 0 or not last.startswith("OK"):
            err = (await proc.stderr.read()).decode().strip().splitlines()
            raise RuntimeError(last.removeprefix("ERR ") if last.startswith("ERR") else (err[-1] if err else f"the helper ended with {proc.returncode}"))
        return last.removeprefix("OK ")
    finally:
        if proc.returncode is None:   # cancelled or timed out
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except TimeoutError:
                proc.kill()


async def install(server_id: int, sequence: list[dict], kinds: list[int], inst) -> str | None:
    """After the server `inst` started: grow its slice to the sequence and define every slot. Returns why not (it logs it too), None when done. Tied to THAT instance: if it stops or is replaced meanwhile, it gives up.
    ponytail: a driver who joins in the first second or so (a wake by joining) learns the old three-session list; the server plays the whole sequence anyway and he learns it when he joins again."""
    from app import gridtimes, supervisor   # late: supervisor owns the instance; gridtimes is heavy
    from app.live import acsp
    for _ in range(60):   # the plugin socket needs a moment to hear the server
        if supervisor.get(server_id) is not inst or not inst.running:
            return _fail(server_id, "the server was stopped or replaced first")
        if inst.acsp and inst.acsp.session:
            break
        await asyncio.sleep(0.5)
    else:
        return _fail(server_id, "the server did not come up")
    try:
        layout = layout_for(sequence, kinds)
        if not gridtimes.known_binary(inst.pid, inst.started_at):
            return _fail(server_id, "this acServer is not the build the session patch belongs to")
        if len(layout) > len(kinds):
            what = await _run_helper(inst.pid, layout, lambda: inst.acsp.send(acsp.encode_restart_session()))   # RESTART_SESSION: a thread reaches broadcastSession, the same session again
            log.info("server %s: %s", server_id, what)
        for i, st in enumerate(sequence):
            if supervisor.get(server_id) is not inst:
                return _fail(server_id, "the server was replaced while its sessions were being defined")
            inst.acsp.send(acsp.encode_set_session_info(i, st["name"], TYPES[st["kind"]], st["laps"], st["time_min"], st["wait_s"]))
            await asyncio.sleep(0.05)
    except (RuntimeError, OSError, ValueError, TimeoutError) as e:
        return _fail(server_id, str(e) or type(e).__name__)
    return None


def _fail(server_id: int, why: str) -> str:
    log.warning("server %s: the session sequence was NOT installed (the server runs its own three sessions): %s", server_id, why)
    return why


# --- the waits at the end of a session (per step of the sequence) ------------------------------------------------------------------------------------------------

def effective_waits(step: dict | None, original: tuple[int, int]) -> tuple[int, int]:
    """(raceOverTime, resultScreenTime) in ms for a session: the step's own `over_s` / `result_s`, else what the server was started with (`original`)."""
    over = original[0] if step is None or step.get("over_s") is None else int(step["over_s"]) * 1000
    result = original[1] if step is None or step.get("result_s") is None else int(step["result_s"]) * 1000
    return over, result


def read_waits(pid: int) -> tuple[int, int]:
    fd = os.open(f"/proc/{pid}/mem", os.O_RDONLY)
    try:
        return struct.unpack("<i", os.pread(fd, 4, RACE_OVER_AT))[0], struct.unpack("<i", os.pread(fd, 4, RESULT_SCREEN_AT))[0]
    finally:
        os.close(fd)


def write_waits(pid: int, over_ms: int, result_ms: int) -> None:
    """Set the two global waits of the server NOW (acServer reads them when a session ends). Refuses when the values there are not plausible, and reads them back."""
    fd = os.open(f"/proc/{pid}/mem", os.O_RDWR)
    try:
        now = struct.unpack("<ii", os.pread(fd, 4, RACE_OVER_AT) + os.pread(fd, 4, RESULT_SCREEN_AT))
        if not all(0 <= v <= MAX_WAIT_MS for v in now) or not all(0 <= v <= MAX_WAIT_MS for v in (over_ms, result_ms)):
            raise RuntimeError(f"wait times {now} / {(over_ms, result_ms)} are not plausible: not written")
        try:
            os.pwrite(fd, struct.pack("<i", over_ms), RACE_OVER_AT)
            os.pwrite(fd, struct.pack("<i", result_ms), RESULT_SCREEN_AT)
        except OSError:
            os.pwrite(fd, struct.pack("<i", now[0]), RACE_OVER_AT)   # the two go together: if the second failed, the first goes back
            raise
        back = struct.unpack("<ii", os.pread(fd, 4, RACE_OVER_AT) + os.pread(fd, 4, RESULT_SCREEN_AT))
        if back != (over_ms, result_ms):
            raise RuntimeError(f"the wait times read back {back}, not {(over_ms, result_ms)}")
    finally:
        os.close(fd)


def config_ms(value, default_s: int) -> int:
    """A wait of the server's config in ms; acServer's own default (`default_s`, seconds) when the key is missing or is not a number."""
    try:
        seconds = float(value)
        if not 0 <= seconds <= MAX_WAIT_MS / 1000:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        seconds = default_s
    return int(seconds * 1000)


def step_and_original(server_id: int, idx: int) -> tuple[dict | None, tuple[int, int]]:
    """(the sequence step the server plays at session index `idx` or None, the waits in ms the server's CONFIG gives: RACE_OVER_TIME and RESULT_SCREEN_TIME, whose defaults when the key is missing are 30 s and 10 s,
    measured on the real acServer). From the config and not from the memory of the process: after a manager restart the memory may hold the waits of an earlier step. Reads the database: call it off the event loop."""
    from sqlmodel import Session

    from app.db import engine
    from app.models import Server
    with Session(engine) as s:
        srv = s.get(Server, server_id)
        seq = ((srv.session if srv else None) or {}).get("sequence")
        cfg = ((srv.config if srv else None) or {}).get("SERVER") or {}
    original = (config_ms(cfg.get("RACE_OVER_TIME"), 30), config_ms(cfg.get("RESULT_SCREEN_TIME"), 10))
    return (seq[idx] if seq and 0 <= idx < len(seq) else None), original


def on_session(client, event: dict, is_new: bool) -> None:
    """ACSPClient calls it for `new_session` and for the description of the CURRENT session: the waits of the step that just began are written into the running server (the ones of the session before are not
    the ones of this one). Only servers with a sequence; a step that says nothing gets the waits of the server's config back."""
    idx = event.get("session_index")
    if not is_new and (idx != event.get("current_session_index") or client.waits_idx == idx):
        return
    client.waits_idx = idx
    try:
        asyncio.get_running_loop().create_task(_set_waits(client, idx))
    except RuntimeError:   # no loop (a test calling _apply directly)
        pass


async def _set_waits(client, idx: int) -> None:
    from app import gridtimes, supervisor   # late: heavy
    async with client.waits_lock:   # one at a time, in the order the sessions began: an old one that is slow cannot land on top of the new one
        try:
            step, original = await asyncio.to_thread(step_and_original, client.server_id, idx)
            inst = supervisor.get(client.server_id)
            if step is None:
                return
            if not inst or not inst.pid or not await asyncio.to_thread(gridtimes.known_binary, inst.pid, inst.started_at):
                raise RuntimeError("the server is not there yet, or it is not the known build")
            over, result = effective_waits(step, original)
            await asyncio.to_thread(write_waits, inst.pid, over, result)
            log.info("server %s: session %s waits %s s after the winner, %s s of results", client.server_id, idx, over // 1000, result // 1000)
        except (RuntimeError, OSError, ValueError, TypeError, OverflowError) as e:
            if client.waits_idx == idx:
                client.waits_idx = None   # not done: the next description of this session tries again
            log.warning("server %s: the waits of session %s were not set: %s", client.server_id, idx, e)


def main(argv: list[str]) -> int:
    """`python -m app.sessionpatch <pid> <layout json> [timeout]`: grow the server, print ARMED then OK/ERR. SIGTERM = give up cleanly."""
    pid, layout = int(argv[0]), json.loads(argv[1])
    timeout = float(argv[2]) if len(argv) > 2 else 30.0
    cancel, armed = threading.Event(), threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: cancel.set())
    watcher = threading.Thread(target=lambda: (armed.wait(), print("ARMED", flush=True)), daemon=True)
    watcher.start()
    try:
        print("OK " + grow(pid, layout, timeout, cancel, armed), flush=True)
        return 0
    except (RuntimeError, OSError) as e:
        print(f"ERR {e}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
