import asyncio
import time

from conftest import ADMIN
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import timeline, wake
from app.db import engine
from app.live import acsp
from app.main import app
from app.models import Server

client = TestClient(app, headers=ADMIN)
M = 60
CFG = {"SERVER": {"LOOP_MODE": 1}, "PRACTICE": {"NAME": "Practice", "TIME": 15}, "QUALIFY": {"NAME": "Qualify", "TIME": 15},
       "RACE": {"NAME": "Race", "LAPS": 5, "WAIT_TIME": 60}}


def test_segments_and_position_follow_the_clock():
    segs = timeline.segments(CFG)
    assert [(s.index, s.type, s.secs) for s in segs] == [(0, 1, 15 * M), (1, 2, 15 * M), (2, 3, timeline.RACE_LAPS_EST_MIN * M + 60)]
    pos = lambda minutes, cfg=CFG, anchor=0: timeline.position(cfg, anchor, 1000.0, 1000.0 + minutes * M)   # noqa: E731
    assert pos(10) == {"index": 0, "remaining_s": 5 * M, "elapsed_s": 10 * M}
    assert pos(15)["index"] == 1 and pos(15)["remaining_s"] == 15 * M                      # practice over: qualifying, whoever is there
    assert pos(25)["index"] == 1 and pos(25)["remaining_s"] == 5 * M
    assert pos(30)["index"] == 2 and pos(30)["remaining_s"] == 21 * M
    assert pos(51)["index"] == 0 and pos(51)["remaining_s"] == 15 * M                       # looped
    assert pos(51 + 3 * 51)["index"] == 0 and pos(51 + 51 + 20)["index"] == 1               # many cycles: still right
    noloop = {**CFG, "SERVER": {"LOOP_MODE": 0}}
    assert pos(50, noloop)["index"] == 2 and pos(52, noloop) is None                        # the cycle ended
    assert pos(5, anchor=1)["index"] == 1 and pos(5, anchor=1)["remaining_s"] == 10 * M    # anchored in qualifying
    assert timeline.position(CFG, None, None, 5.0) is None and timeline.position({"SERVER": {}}, 0, 1.0, 5.0) is None


def _server(cfg):
    sid = client.post("/api/v1/servers", json={"name": "Clock", "config": cfg}).json()["id"]
    return sid


def test_the_lobby_shows_the_session_clock_while_the_server_is_off():
    sid = _server(CFG)
    with Session(engine) as s:
        srv = s.get(Server, sid)
        assert wake.facade_info(srv)["session"] == 0                                         # never ran: the first session in full
        srv.anchor_index, srv.anchor_at = 0, time.time() - 20 * M                           # practice began 20 minutes ago
        s.add(srv)
        s.commit()
        s.refresh(srv)
        info = wake.facade_info(srv)
    assert info["session"] == 1 and abs(info["timeleft"] - 10 * M) <= 2 and info["clients"] == 0


def test_session_events_move_the_anchor_but_a_description_of_another_session_does_not():
    sid = _server(CFG)
    timeline.on_session_event(sid, {"type": "new_session", "session_index": 1, "current_session_index": 1, "elapsed_ms": 0})
    with Session(engine) as s:
        a = s.get(Server, sid)
        assert a.anchor_index == 1 and abs(a.anchor_at - time.time()) < 3
    timeline.on_session_event(sid, {"type": "session_info", "session_index": 2, "current_session_index": 1, "elapsed_ms": 99000})   # asked about the race
    timeline.on_session_event(sid, {"type": "session_info", "session_index": 1, "current_session_index": 1, "elapsed_ms": 120000})   # the running one
    with Session(engine) as s:
        assert abs(s.get(Server, sid).anchor_at - (time.time() - 120)) < 3


class _Transport:
    def __init__(self):
        self.sent = []

    def sendto(self, data):
        self.sent.append(data)

    def close(self):
        pass


class _Inst:
    def __init__(self, sid, current):
        self.acsp = acsp.ACSPClient(sid)
        self.acsp.transport = _Transport()
        self.acsp.session = {"current_session_index": current}


def _planned(sid):
    with Session(engine) as s:
        return timeline.server_position(s.get(Server, sid))


def _anchor(sid, index, minutes_ago):
    with Session(engine) as s:
        srv = s.get(Server, sid)
        srv.anchor_index, srv.anchor_at = index, time.time() - minutes_ago * M
        s.add(srv)
        s.commit()


def test_a_start_moves_the_real_server_to_where_the_clock_is(monkeypatch):
    sid = _server(CFG)
    segs = timeline.segments(CFG)
    inst = _Inst(sid, 0)
    monkeypatch.setattr(timeline.supervisor, "get", lambda _id: inst)
    _anchor(sid, 0, 20)                                        # practice began 20 min ago: qualifying, 10 min left
    got = asyncio.run(timeline.resume(sid, _planned(sid), time.time(), settle=0))
    assert got["index"] == 1 and abs(got["remaining_s"] - 10 * M) < 3
    assert inst.acsp.transport.sent == [timeline.definition(segs[1], 10), acsp.encode_next_session()]
    # ...and the original length comes back as soon as the next session starts, not before
    inst.acsp.transport.sent.clear()
    inst.acsp._apply({"type": "new_session", "session_index": 1, "current_session_index": 1, "elapsed_ms": 0, "name": "Qualify", "track": "t", "session_type": 2})
    assert inst.acsp.transport.sent == []
    inst.acsp._apply({"type": "new_session", "session_index": 2, "current_session_index": 2, "elapsed_ms": 0, "name": "Race", "track": "t", "session_type": 3})
    assert inst.acsp.transport.sent == [timeline.definition(segs[1])] and inst.acsp.restore_when_session_changes is None
    assert timeline.definition(segs[1]) != timeline.definition(segs[1], 10)


def test_the_real_servers_own_first_session_does_not_hide_where_the_clock_was(monkeypatch):
    """What went wrong the first time: acServer starts «Practice» at once, that re-anchored the clock to now, and the start found nothing to catch up."""
    sid = _server(CFG)
    inst = _Inst(sid, 0)
    monkeypatch.setattr(timeline.supervisor, "get", lambda _id: inst)
    _anchor(sid, 0, 12.3)                                            # practice began 12 min 18 s ago: 2 min 42 s left
    planned, at = _planned(sid), time.time()                        # (read by start_server before spawning)
    timeline.on_session_event(sid, {"type": "new_session", "session_index": 0, "current_session_index": 0, "elapsed_ms": 0})   # the fresh practice
    assert abs(_planned(sid)["remaining_s"] - 15 * M) < 3          # the clock now says «just started»...
    got = asyncio.run(timeline.resume(sid, planned, at, settle=0))
    assert got["index"] == 0 and abs(got["remaining_s"] - 162) < 3  # ...but the start used the position read before
    assert inst.acsp.transport.sent == [timeline.definition(timeline.segments(CFG)[0], 3)]   # practice shortened to 3 min (rounded), no jump needed
    inst.acsp.transport.sent.clear()
    inst.acsp.session = {"current_session_index": 0, "elapsed_ms": 50000}                    # the fresh practice has been running 50 s already:
    asyncio.run(timeline.resume(sid, planned, at, settle=0))                                  # (162 + 50) / 60 -> 4 min total, so about 162 s are left
    assert inst.acsp.transport.sent == [timeline.definition(timeline.segments(CFG)[0], 4)]


def test_resume_edge_cases(monkeypatch):
    sid = _server(CFG)
    inst = _Inst(sid, 0)
    monkeypatch.setattr(timeline.supervisor, "get", lambda _id: inst)
    run = lambda: asyncio.run(timeline.resume(sid, _planned(sid), time.time(), settle=0))   # noqa: E731
    _anchor(sid, 0, 0.1)
    assert run() is None and inst.acsp.transport.sent == []                                           # just started: nothing to move
    _anchor(sid, 0, 14.8)                                                                             # 12 s of practice left: the next, in full
    got = run()
    assert got == {"index": 1, "remaining_s": None} and inst.acsp.transport.sent == [acsp.encode_next_session()]
    inst.acsp.transport.sent.clear()
    inst.acsp.session = {"current_session_index": 0}
    _anchor(sid, 0, 31 + 5)                                                                           # into the second race (laps: no length to change)
    got = run()
    assert got["index"] == 2 and inst.acsp.transport.sent == [acsp.encode_next_session()] * 2
    assert asyncio.run(timeline.resume(sid, None, time.time(), settle=0)) is None                     # no position: starts as it is
