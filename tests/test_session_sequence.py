"""A session sequence (practice, qualify, race, intermediate practice, race ...): validated, understood by the session clock, and really played by an acServer grown in memory (app/sessionpatch.py)."""

import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from app import sessionpatch, timeline
from app.live import acsp
from app.schemas.servers import SessionIn

SEQ = [
    {"kind": "practice", "name": "Practice", "time_min": 5, "laps": 0, "wait_s": 0},
    {"kind": "qualify", "name": "Qualify", "time_min": 3, "laps": 0, "wait_s": 0},
    {"kind": "race", "name": "Race 1", "time_min": 0, "laps": 8, "wait_s": 30},
    {"kind": "practice", "name": "Intermedia", "time_min": 3, "laps": 0, "wait_s": 0},
    {"kind": "race", "name": "Race 2", "time_min": 0, "laps": 8, "wait_s": 30},
]
BASE = {"name": "S", "track": "spa", "cars": ["gt3"], "practice_min": 5, "qualify_min": 3, "race_laps": 8}


def test_layout_takes_the_fixed_words_from_the_original_slot_of_the_same_kind():
    assert sessionpatch.layout_for(SEQ, [1, 2, 3]) == [0, 1, 2, 0, 2]
    assert sessionpatch.layout_for(SEQ[1:3] + SEQ[4:], [2, 3]) == [0, 1, 1]   # a server with no practice section
    with pytest.raises(ValueError):
        sessionpatch.layout_for(SEQ, [2, 3])   # a practice step but no practice section


def test_the_preset_accepts_a_sequence_and_refuses_the_ones_that_cannot_be_played():
    assert len(SessionIn(**BASE, sequence=SEQ).sequence) == 5
    with pytest.raises(ValidationError):
        SessionIn(**BASE, sequence=SEQ[:3])                                          # fewer than four: nothing to patch
    with pytest.raises(ValidationError):
        SessionIn(**{**BASE, "practice_min": None}, sequence=SEQ)                    # a practice step without a practice section
    with pytest.raises(ValidationError):
        SessionIn(**BASE, sequence=[s for s in SEQ if s["kind"] != "race"] * 2)      # no race at all
    with pytest.raises(ValidationError):
        SessionIn(**BASE, sequence=SEQ, reversed_grid=3)                              # acServer's own inversion clashes with the state machine
    with pytest.raises(ValidationError):
        SessionIn(**BASE, sequence=[*SEQ[:4], {**SEQ[4], "laps": 0, "time_min": 0}])  # a race with no length
    with pytest.raises(ValidationError):
        SessionIn(**BASE, sequence=[{**SEQ[0], "laps": 3}, *SEQ[1:]])                 # a timed practice with laps
    assert SessionIn(**BASE, grid_from_race=2).grid_from_race == 2 and SessionIn(**BASE).grid_from_race is None


def test_the_clock_of_a_stopped_server_walks_the_whole_sequence():
    segs = timeline.segments({}, 20, SEQ)
    assert [(s.index, s.type, s.name) for s in segs] == [(0, 1, "Practice"), (1, 2, "Qualify"), (2, 3, "Race 1"), (3, 1, "Intermedia"), (4, 3, "Race 2")]
    start = 1_000_000.0
    after_race_1 = start + 5 * 60 + 3 * 60 + (20 * 60 + 30) + 60   # one minute into the intermediate
    pos = timeline.position({"SERVER": {"LOOP_MODE": 1}}, 0, start, after_race_1, 20, SEQ)
    assert pos["index"] == 3 and round(pos["elapsed_s"]) == 60 and round(pos["remaining_s"]) == 120


SIM = Path(os.environ.get("ACM_TEST_ACSERVER_SIM", Path.home() / "Documents/acserver-re/sim"))


@pytest.mark.skipif(not (SIM / "acServer").exists(), reason="needs the local acServer simulator (ACM_TEST_ACSERVER_SIM): the real binary the patch belongs to")
def test_the_real_acserver_grows_to_the_sequence_and_plays_every_session(tmp_path):
    log = open(tmp_path / "srv.log", "w")
    srv = subprocess.Popen(["./acServer"], cwd=SIM, stdout=log, stderr=subprocess.STDOUT)   # our child: ptrace is allowed under Yama scope 1 too
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 19602))
    send = lambda b: s.sendto(b, ("127.0.0.1", 19601))
    try:
        time.sleep(3)
        threading.Timer(1.0, lambda: send(acsp.encode_restart_session())).start()
        assert sessionpatch.grow(srv.pid, sessionpatch.layout_for(SEQ, [1, 2, 3]), 20).startswith("grown to 5 sessions")
        for i, st in enumerate(SEQ):
            send(acsp.encode_set_session_info(i, st["name"], sessionpatch.TYPES[st["kind"]], st["laps"], st["time_min"], st["wait_s"]))
            time.sleep(0.1)
        send(acsp.encode_set_session_info(5, "out", 1, 0, 1, 0))   # past the end: acServer says out of bounds and carries on
        for _ in range(len(SEQ) + 1):
            send(acsp.encode_next_session())
            time.sleep(0.6)
        assert srv.poll() is None   # still alive
    finally:
        srv.terminate()
        srv.wait(5)
        log.close()
    text = (tmp_path / "srv.log").read_text(errors="replace")
    played = re.findall(r"SENDING session name : (.*)\nSENDING session index : (\d+)\nSENDING session type : (\d+)\nSENDING session time : (\d+)", text)
    seen = {(name.strip(), int(index), int(typ), int(minutes)) for name, index, typ, minutes in played}
    for i, st in enumerate(SEQ):
        assert (st["name"], i, sessionpatch.TYPES[st["kind"]], st["time_min"]) in seen, f"session {i} ({st['name']}) was never played as defined"
    assert "ACSP_SET_SESSION_INFO out of bounds: 5" in text


# --- install(): grow, then define every slot ---------------------------------------------------------------------------------------------------------------------

def _fake_instance(sent):
    from types import SimpleNamespace
    acsp_client = SimpleNamespace(session={"session_type": 1}, send=lambda b: sent.append(b))
    return SimpleNamespace(running=True, acsp=acsp_client, pid=4242, started_at=1.0)


def test_install_restarts_the_session_to_trip_the_patch_then_defines_every_slot(monkeypatch):
    import asyncio

    from app import gridtimes, supervisor
    sent, grown = [], []
    inst = _fake_instance(sent)
    monkeypatch.setattr(supervisor, "get", lambda sid: inst)
    monkeypatch.setattr(gridtimes, "known_binary", lambda pid, started: True)

    def fake_grow(pid, layout, timeout=30.0, cancel=None):
        time.sleep(0.6)   # the manager asks for a session change 0.3 s after it starts waiting
        grown.append((pid, layout))
        return "grown"

    monkeypatch.setattr(sessionpatch, "grow", fake_grow)
    assert asyncio.run(sessionpatch.install(9401, SEQ, [1, 2, 3], inst)) is None
    assert grown == [(4242, [0, 1, 2, 0, 2])]
    assert sent[0] == acsp.encode_restart_session()   # RESTART_SESSION (the same session again), not NEXT_SESSION
    assert sent[1:] == [acsp.encode_set_session_info(i, s["name"], sessionpatch.TYPES[s["kind"]], s["laps"], s["time_min"], s["wait_s"]) for i, s in enumerate(SEQ)]


def test_install_leaves_the_server_alone_on_a_build_it_does_not_know(monkeypatch):
    import asyncio

    from app import gridtimes, supervisor
    sent = []
    inst = _fake_instance(sent)
    monkeypatch.setattr(supervisor, "get", lambda sid: inst)
    monkeypatch.setattr(gridtimes, "known_binary", lambda pid, started: False)
    monkeypatch.setattr(sessionpatch, "grow", lambda *a, **k: pytest.fail("must not touch an unknown binary"))
    assert "not the build" in asyncio.run(sessionpatch.install(9402, SEQ, [1, 2, 3], inst))
    assert sent == []


def test_install_gives_up_when_the_server_is_replaced_meanwhile(monkeypatch):
    import asyncio

    from app import supervisor
    sent = []
    inst = _fake_instance(sent)
    monkeypatch.setattr(supervisor, "get", lambda sid: object())   # another instance holds the server now
    assert "replaced" in asyncio.run(sessionpatch.install(9403, SEQ, [1, 2, 3], inst))
    assert sent == []


@pytest.mark.skipif(not (SIM / "acServer").exists(), reason="needs the local acServer simulator (ACM_TEST_ACSERVER_SIM)")
def test_a_failed_patch_leaves_the_real_acserver_running_and_it_can_be_grown_afterwards(tmp_path):
    import struct
    srv = subprocess.Popen(["./acServer"], cwd=SIM, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 19602))
    send = lambda b: s.sendto(b, ("127.0.0.1", 19601))
    state = lambda: Path(f"/proc/{srv.pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    def index():
        with open(f"/proc/{srv.pid}/mem", "rb", 0) as f:
            f.seek(sessionpatch.CURIDX)
            return struct.unpack("<i", f.read(4))[0]
    try:
        time.sleep(3)
        with pytest.raises(RuntimeError, match="no session change"):
            sessionpatch.grow(srv.pid, [0, 1, 2, 0, 2], timeout=2)   # nothing asks for a session change: it gives up
        assert state() in ("S", "R"), f"the server was left stopped or traced ({state()})"
        assert srv.poll() is None
        before = index()
        send(acsp.encode_next_session())   # the server still answers its plugin, and no breakpoint is left in
        time.sleep(1.0)
        assert index() == (before + 1) % 3 and srv.poll() is None
        threading.Timer(0.5, lambda: send(acsp.encode_restart_session())).start()
        assert sessionpatch.grow(srv.pid, [0, 1, 2, 0, 2], 20).startswith("grown to 5")   # and it can still be grown
        assert state() in ("S", "R") and srv.poll() is None
    finally:
        srv.terminate()
        srv.wait(5)


@pytest.mark.skipif(not (SIM / "acServer").exists(), reason="needs the local acServer simulator (ACM_TEST_ACSERVER_SIM)")
def test_a_cancelled_patch_puts_everything_back(tmp_path):
    srv = subprocess.Popen(["./acServer"], cwd=SIM, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cancel = threading.Event()
    try:
        time.sleep(3)
        threading.Timer(0.8, cancel.set).start()
        with pytest.raises(RuntimeError, match="cancelled"):
            sessionpatch.grow(srv.pid, [0, 1, 2, 0, 2], 30, cancel)
        time.sleep(0.3)
        state = Path(f"/proc/{srv.pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        assert state in ("S", "R") and srv.poll() is None
    finally:
        srv.terminate()
        srv.wait(5)
