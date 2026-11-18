"""More than three sessions in one acServer cycle (practice, qualify, race, practice, race ...): the session sequence of a preset.

acServer reads three fixed sections ([PRACTICE] [QUALIFY] [RACE]) into `main.Sessions`, a Go slice it walks with index+1 modulo its length, and ACSP `SET_SESSION_INFO` can rename and resize a slot (name, type, laps,
time, wait) but never add one. So after the server starts, the slice is replaced by a longer one IN MEMORY (up to MAX_SESSIONS): `runtime.makeslice` is CALLED by hand from the thread that reaches
`main.broadcastSession` (the manager asks for it with RESTART_SESSION: same session, no index change), the sessions are copied, `main.Sessions` and `main.CurrentSession` repointed, and every slot is then defined with
`SET_SESSION_INFO` from the sequence. Nothing on disk changes, a restart of the server undoes it, and the manager does it again at every start, before anybody joins (a client learns the session list when it connects).
The layout facts (a Session is 15 words of 4 bytes, which words are static) come from decompiling the server and are described in acserver-re/patches/grow_sessions.py, where this code comes from.
Only for the acServer build of `gridtimes.ACSERVER_SHA256`; on any other the sequence is not installed and the server runs its three sessions (it says so in the log).

A sequence step is {kind: practice|qualify|race, name, time_min, laps, wait_s}; the first three sections of the server's config are the originals the extra slots copy their fixed words from (by kind).
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import signal
import struct
import threading
import time

from app import gridtimes

log = logging.getLogger("acm.sessionpatch")

SESSIONS, CURRENT, MAKESLICE, SESSION_TYPE, BROADCAST = 0x83E2EB8, 0x83E2734, 0x807F460, 0x82A5140, 0x8218090   # main.Sessions, main.CurrentSession, runtime.makeslice, main.Session's type, main.broadcastSession
CURIDX = gridtimes.CURRENT_INDEX
WORDS, STRIDE = 15, 60
MAX_SESSIONS = 12   # also the ceiling of SetSessionIn.index (app/schemas/servers.py)
STATIC = (0, 1, 2, 3, 4, 5, 10, 11, 12)   # the words copied from the original slot: name, type, time, laps, wait, is_open. 6-9 are runtime (start / end time), 13 is the slot's own index
TYPES = {"practice": 1, "qualify": 2, "race": 3}
PT_ATTACH, PT_DETACH, PT_CONT, PT_GETREGS, PT_SETREGS = 16, 17, 7, 12, 13
WALL = 0x40000000
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


def _stop(pid: int, tid: int) -> bool:
    """Make a RUNNING traced thread stop; True once it is in a ptrace-stop with its SIGSTOP consumed (a thread that has ended counts as stopped: there is nothing left to do). A SIGTRAP is ours and is never passed on:
    if the thread had just hit the breakpoint (rip right after it) it is moved back to its start and let go, so it runs the function for real and then meets the SIGSTOP."""
    try:
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


def grow(pid: int, layout: list[int], timeout: float = 30.0, cancel: threading.Event | None = None) -> str:
    """Replace main.Sessions of the RUNNING acServer `pid` by a slice of len(layout) sessions (slot i takes the fixed words of ORIGINAL slot layout[i]). Blocks until a thread reaches broadcastSession (something must
    make it: RESTART_SESSION; `cancel` set from outside gives up). Every ptrace call is made from this thread (the tracer is a thread, not a process): run it with asyncio.to_thread, once. Raises RuntimeError with the reason when it cannot.
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
        wr(BROADCAST, bp_orig)   # first thing: nobody else may trip over the breakpoint
        bp_orig = None
        for t in sorted(running):   # everybody else stops while the slice is swapped
            if _stop(pid, t):
                running.discard(t)
        if running:
            raise RuntimeError(f"threads {sorted(running)} would not stop")
        r0 = Regs()
        _pt(PT_GETREGS, hit, 0, ctypes.addressof(r0))
        regs = Regs.from_buffer_copy(bytes(r0))
        r0.rip = BROADCAST   # where it goes back to: the entry of broadcastSession, as if it had not run yet
        saved = Regs.from_buffer_copy(bytes(r0))
        regs.rip = BROADCAST
        esp0 = r0.rsp & 0xFFFFFFFF
        ret = struct.unpack("<I", rd(esp0, 4))[0]
        cur_ptr, cur_idx = struct.unpack("<I", rd(CURRENT, 4))[0], struct.unpack("<i", rd(CURIDX, 4))[0]
        sp = (esp0 - 0x40) & ~0xF
        wr(sp, struct.pack("<IIII", ret, SESSION_TYPE, MAX_SESSIONS, MAX_SESSIONS) + b"\0" * 16)   # makeslice(type, len, cap) with the REAL return address: the stack still unwinds
        ret_fix = (ret, rd(ret, 1))
        wr(ret, b"\xcc")
        regs.rsp, regs.rip, regs.orig_rax = sp, MAKESLICE, 0xFFFFFFFFFFFFFFFF
        moved = (hit, saved)
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
                    _stop(pid, t)   # once more
                _pt(PT_DETACH, t, 0, 0)
            except (OSError, RuntimeError):
                pass
        mem.close()


async def install(server_id: int, sequence: list[dict], kinds: list[int], inst) -> str | None:
    """After the server `inst` started: grow its slice to the sequence and define every slot. Returns why not (it logs it too), None when done. Tied to THAT instance: if it stops or is replaced meanwhile, it gives up.
    ponytail: a driver who joins in the first second or so (a wake by joining) learns the old three-session list; the server plays the whole sequence anyway and he learns it when he joins again."""
    from app import supervisor   # late: supervisor owns the instance
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
            loop = asyncio.get_running_loop()
            trigger = loop.call_later(0.3, lambda: inst.acsp and inst.acsp.send(acsp.encode_restart_session()))   # makes a thread reach broadcastSession; the same session again
            cancel = threading.Event()
            try:
                what = await asyncio.to_thread(grow, inst.pid, layout, 30.0, cancel)
            except asyncio.CancelledError:
                cancel.set()   # the thread cannot be interrupted: it is told to give up and put everything back
                raise
            finally:
                trigger.cancel()
            log.info("server %s: %s", server_id, what)
        for i, st in enumerate(sequence):
            if supervisor.get(server_id) is not inst:
                return _fail(server_id, "the server was replaced while its sessions were being defined")
            inst.acsp.send(acsp.encode_set_session_info(i, st["name"], TYPES[st["kind"]], st["laps"], st["time_min"], st["wait_s"]))
            await asyncio.sleep(0.05)
    except (RuntimeError, OSError, ValueError) as e:
        return _fail(server_id, str(e))
    return None


def _fail(server_id: int, why: str) -> str:
    log.warning("server %s: the session sequence was NOT installed (the server runs its own three sessions): %s", server_id, why)
    return why
