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
    # every session also counts acServer's default 10 s of results, and a race 30 s more for the others after the winner
    one_minute_into_the_intermediate = start + (5 * 60 + 10) + (3 * 60 + 10) + (20 * 60 + 30 + 30 + 10) + 60
    pos = timeline.position({"SERVER": {"LOOP_MODE": 1}}, 0, start, one_minute_into_the_intermediate, 20, SEQ)
    assert pos["index"] == 3 and round(pos["elapsed_s"]) == 60 and round(pos["remaining_s"]) == 3 * 60 + 10 - 60


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

    async def fake_helper(pid, layout, on_armed):
        await asyncio.sleep(0.2)   # getting every thread under ptrace takes a while: nothing may be asked before it says «armed»
        assert sent == [], "the session change was asked for before the breakpoint was in"
        on_armed()
        await asyncio.sleep(0.1)
        grown.append((pid, layout))
        return "grown to 5 sessions"

    monkeypatch.setattr(sessionpatch, "_run_helper", fake_helper)
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
    monkeypatch.setattr(sessionpatch, "_run_helper", lambda *a, **k: pytest.fail("must not touch an unknown binary"))
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


@pytest.mark.skipif(not (SIM / "acServer").exists(), reason="needs the local acServer simulator (ACM_TEST_ACSERVER_SIM)")
def test_a_patch_that_fails_halfway_gives_the_thread_back_unharmed(tmp_path, monkeypatch):
    """What crashed the staging server: the thread that hit the breakpoint was let go one byte into an instruction. Here the call to makeslice is aimed at an address that faults: the patch must abort, the thread
    must go back to the start of broadcastSession (the restart then completes) and the server must still be the three-session server it was."""
    import struct
    srv = subprocess.Popen(["./acServer"], cwd=SIM, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 19602))
    armed = threading.Event()
    def length():
        with open(f"/proc/{srv.pid}/mem", "rb", 0) as f:
            f.seek(sessionpatch.SESSIONS)
            return struct.unpack("<III", f.read(12))[1]
    try:
        time.sleep(3)
        monkeypatch.setattr(sessionpatch, "MAKESLICE", 0x1000)   # nothing is mapped there
        job = threading.Thread(target=lambda: s.sendto(acsp.encode_restart_session(), ("127.0.0.1", 19601)) if armed.wait(10) else None)
        job.start()
        with pytest.raises(RuntimeError, match="signal"):
            sessionpatch.grow(srv.pid, [0, 1, 2, 0, 2], 20, None, armed)
        job.join()
        time.sleep(1.0)
        assert srv.poll() is None, "the server died"
        assert length() == 3
        s.sendto(acsp.encode_next_session(), ("127.0.0.1", 19601))   # still answers its plugin
        time.sleep(1.0)
        assert srv.poll() is None
    finally:
        srv.terminate()
        out = srv.communicate(timeout=5)[0].decode(errors="replace")
    assert "panic" not in out and "SIGSEGV" not in out, out[-600:]


def test_the_helper_process_reports_why_it_could_not_do_it():
    import asyncio
    with pytest.raises(RuntimeError, match="No such file|ERR|Errno"):
        asyncio.run(sessionpatch._run_helper(2_999_999, [0, 1, 2, 0, 2], lambda: None))   # no such process


@pytest.mark.skipif(not (SIM / "acServer").exists(), reason="needs the local acServer simulator (ACM_TEST_ACSERVER_SIM)")
def test_the_helper_process_grows_the_real_acserver(tmp_path):
    """The real path of `install`: a separate process does the ptrace. Needs ptrace_scope 0 (the VPS) or the tracer to be the server's parent: where neither is true the helper is refused and the test says so."""
    import asyncio
    srv = subprocess.Popen(["./acServer"], cwd=SIM, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 19602))
    try:
        time.sleep(3)
        try:
            what = asyncio.run(sessionpatch._run_helper(srv.pid, [0, 1, 2, 0, 2], lambda: s.sendto(acsp.encode_restart_session(), ("127.0.0.1", 19601))))
        except RuntimeError as e:
            if "not permitted" in str(e).lower() or "permission denied" in str(e).lower():
                pytest.skip(f"ptrace between sibling processes is refused here (Yama): {e}")
            raise
        assert what.startswith("grown to 5 sessions") and srv.poll() is None
    finally:
        srv.terminate()
        srv.wait(5)


# --- the waits at the end of a session (over_s, result_s) ------------------------------------------------------------------------------------------------------------

def test_the_waits_of_a_step_are_validated():
    ok = {**SEQ[2], "over_s": 90, "result_s": 20}
    assert len(SessionIn(**BASE, sequence=[*SEQ[:2], ok, *SEQ[3:]]).sequence) == 5
    assert SessionIn(**BASE, sequence=[{**SEQ[0], "result_s": 45}, *SEQ[1:]]).sequence[0].result_s == 45   # the results screen exists after every kind
    with pytest.raises(ValidationError):
        SessionIn(**BASE, sequence=[{**SEQ[0], "over_s": 30}, *SEQ[1:]])   # only a race waits for the others after the winner
    with pytest.raises(ValidationError):
        SessionIn(**BASE, sequence=[*SEQ[:2], {**SEQ[2], "over_s": 99999}, *SEQ[3:]])
    with pytest.raises(ValidationError):
        SessionIn(**BASE, sequence=[*SEQ[:2], {**SEQ[2], "result_s": 601}, *SEQ[3:]])


def test_a_step_that_says_nothing_gets_what_the_server_started_with():
    assert sessionpatch.effective_waits(None, (60_000, 30_000)) == (60_000, 30_000)
    assert sessionpatch.effective_waits({"over_s": None, "result_s": None}, (60_000, 30_000)) == (60_000, 30_000)
    assert sessionpatch.effective_waits({"over_s": 90, "result_s": None}, (60_000, 30_000)) == (90_000, 30_000)
    assert sessionpatch.effective_waits({"over_s": 0, "result_s": 5}, (60_000, 30_000)) == (0, 5_000)   # 0 is a value, not «nothing»


def test_the_clock_counts_the_waits_a_step_asks_for():
    steps = [*SEQ[:2], {**SEQ[2], "over_s": 60, "result_s": 30}, *SEQ[3:]]
    segs = timeline.segments({}, 20, steps)
    assert segs[2].secs == 20 * 60 + 30 + 60 + 30                      # race: est + wait + its own over_s + its own result_s
    assert segs[0].secs == 5 * 60 + 10                                  # no word from the step: acServer's default 10 s of results
    assert timeline.segments({"SERVER": {"RESULT_SCREEN_TIME": 25}}, 20, steps)[0].secs == 5 * 60 + 25   # ... or the server's own
    assert timeline.segments({"SERVER": {"RESULT_SCREEN_TIME": "junk"}}, 20, steps)[0].secs == 5 * 60   # a config that is not a number counts for nothing, it does not break the clock


def test_each_new_session_writes_its_own_waits_and_a_step_without_them_gives_the_original_back(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from app import gridtimes, supervisor
    written, steps = [], {0: {"kind": "practice"}, 2: {"kind": "race", "over_s": 90, "result_s": 20}, 3: {"kind": "practice", "result_s": 5}}
    monkeypatch.setattr(sessionpatch, "step_and_original", lambda sid, idx: (steps.get(idx), (60_000, 30_000)))
    monkeypatch.setattr(sessionpatch, "write_waits", lambda pid, o, r: written.append((o, r)))
    monkeypatch.setattr(supervisor, "get", lambda sid: SimpleNamespace(pid=4242, started_at=1.0))
    monkeypatch.setattr(gridtimes, "known_binary", lambda pid, started: True)
    import asyncio as _a
    client = SimpleNamespace(server_id=9501, waits_idx=None, waits_lock=_a.Lock())

    async def go():
        for idx in (0, 2, 3, 4):
            sessionpatch.on_session(client, {"session_index": idx, "current_session_index": idx}, True)
            await asyncio.sleep(0.05)
        sessionpatch.on_session(client, {"session_index": 4, "current_session_index": 4}, False)   # the same session described again: nothing
        sessionpatch.on_session(client, {"session_index": 1, "current_session_index": 4}, False)   # another session described: nothing
        await asyncio.sleep(0.05)

    asyncio.run(go())
    # 0: the step says nothing -> what the server's config gives; 2: its own; 3: only the results; 4: no such step -> nothing written (it keeps what 3 left)
    assert written == [(60_000, 30_000), (90_000, 20_000), (60_000, 5_000)]


def test_the_original_waits_come_from_the_config_with_acservers_own_defaults():
    from sqlmodel import Session as DbSession

    from app.db import engine
    from app.models import Server
    from fastapi.testclient import TestClient

    from app.main import app
    from conftest import ADMIN
    sid = TestClient(app, headers=ADMIN).post("/api/v1/servers", json={"name": "Waits"}).json()["id"]
    with DbSession(engine) as db:
        srv = db.get(Server, sid)
        srv.config = {"SERVER": {"RACE_OVER_TIME": 600}}   # RESULT_SCREEN_TIME missing: acServer's 10 s
        srv.session = {"sequence": [{"kind": "race", "name": "R", "laps": 2, "over_s": 90}]}
        db.add(srv)
        db.commit()
    step, original = sessionpatch.step_and_original(sid, 0)
    assert step["over_s"] == 90 and original == (600_000, 10_000)
    assert sessionpatch.step_and_original(sid, 5)[0] is None


@pytest.mark.skipif(not (SIM / "acServer").exists(), reason="needs the local acServer simulator (ACM_TEST_ACSERVER_SIM)")
def test_the_real_acserver_takes_the_waits_and_refuses_what_is_not_plausible():
    srv = subprocess.Popen(["./acServer"], cwd=SIM, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(3)
        assert sessionpatch.read_waits(srv.pid) == (60_000, 30_000)   # RACE_OVER_TIME=60 and RESULT_SCREEN_TIME=30 of the sim's config, in ms
        sessionpatch.write_waits(srv.pid, 90_000, 5_000)
        assert sessionpatch.read_waits(srv.pid) == (90_000, 5_000)
        with pytest.raises(RuntimeError, match="plausible"):
            sessionpatch.write_waits(srv.pid, 10**9, 1)
        assert sessionpatch.read_waits(srv.pid) == (90_000, 5_000)   # untouched by the refusal
        sessionpatch.write_waits(srv.pid, 60_000, 30_000)
        assert srv.poll() is None
    finally:
        srv.terminate()
        srv.wait(5)


def test_config_waits_that_are_not_numbers_fall_back_to_acservers_defaults():
    assert sessionpatch.config_ms(None, 30) == 30_000 and sessionpatch.config_ms("junk", 10) == 10_000 and sessionpatch.config_ms(float("inf"), 10) == 10_000
    assert sessionpatch.config_ms("180", 30) == 180_000 and sessionpatch.config_ms(0, 30) == 0 and sessionpatch.config_ms(-5, 30) == 30_000


def test_a_failed_step_is_tried_again_by_the_next_description_of_the_session(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from app import gridtimes, supervisor
    calls = []
    monkeypatch.setattr(sessionpatch, "step_and_original", lambda sid, idx: ({"kind": "practice", "result_s": 5}, (60_000, 30_000)))
    monkeypatch.setattr(supervisor, "get", lambda sid: None if not calls else SimpleNamespace(pid=1, started_at=1.0))   # the server is not there on the first try
    monkeypatch.setattr(gridtimes, "known_binary", lambda pid, started: True)
    monkeypatch.setattr(sessionpatch, "write_waits", lambda pid, o, r: calls.append((o, r)))
    client = SimpleNamespace(server_id=9502, waits_idx=None, waits_lock=asyncio.Lock())
    info = {"session_index": 1, "current_session_index": 1}

    async def go():
        sessionpatch.on_session(client, info, False)
        await asyncio.sleep(0.05)
        assert client.waits_idx is None and calls == []   # it failed and said so: nothing remembered as done
        calls.append("tried")
        sessionpatch.on_session(client, info, False)
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert calls == ["tried", (60_000, 5_000)]


def test_the_template_with_no_qualify_validates_and_the_first_race_needs_no_policy():
    """Practice, race 1, intermediate, race 2 inverted: no qualify section is needed, race 1 takes the practice's own grid (acServer sorts by best lap) and only race 2 asks for the policy."""
    seq = [
        {"kind": "practice", "name": "Práctica", "time_min": 10},
        {"kind": "race", "name": "Carrera 1", "laps": 8, "wait_s": 30},
        {"kind": "practice", "name": "Intermedia", "time_min": 3},
        {"kind": "race", "name": "Carrera 2", "laps": 8, "wait_s": 30},
    ]
    s = SessionIn(name="S", track="spa", cars=["gt3"], practice_min=10, race_laps=8, sequence=seq, grid_from_race=6)
    assert [st.kind for st in s.sequence] == ["practice", "race", "practice", "race"] and s.grid_from_race == 6
    assert sessionpatch.layout_for([st.model_dump() for st in s.sequence], [1, 3]) == [0, 1, 0, 1]   # a server with a practice and a race section only
    with pytest.raises(ValidationError):
        SessionIn(name="S", track="spa", cars=["gt3"], race_laps=8, sequence=seq)   # but a practice step needs its section


def test_the_template_practice_qualify_practice_race_validates():
    """The second practice is there to arrange the grid: the race starts as the qualifying is classified, with the stewards' grid penalties."""
    seq = [
        {"kind": "practice", "name": "Práctica", "time_min": 10},
        {"kind": "qualify", "name": "Clasificación", "time_min": 8},
        {"kind": "practice", "name": "Ajuste de parrilla", "time_min": 4},
        {"kind": "race", "name": "Carrera", "laps": 10, "wait_s": 30},
    ]
    s = SessionIn(name="S", track="spa", cars=["gt3"], practice_min=10, qualify_min=8, race_laps=10, sequence=seq, grid_from_qualify=True)
    assert s.grid_from_qualify is True and sessionpatch.layout_for([st.model_dump() for st in s.sequence], [1, 2, 3]) == [0, 1, 0, 2]
    assert SessionIn(name="S", track="spa", cars=["gt3"], practice_min=10, qualify_min=8, race_laps=10, sequence=seq).grid_from_qualify is False
