"""Who started a vote: the log line parser, the notice, and that the log read again after an adopt() is not told twice."""
from collections import deque
from types import SimpleNamespace

from app import discord, metrics, supervisor, votes
from app.live import acsp
from app.live.board import Driver


def test_parse_the_three_kinds_and_keeps_what_it_cannot_read():
    assert votes.parse("Creating vote to NEXT SESSION from Alan Montes []") == {"kind": "next", "voter": "Alan Montes", "target": None}
    assert votes.parse("Creating vote to RESTART SESSION from Ana [Team Rojo]") == {"kind": "restart", "voter": "Ana", "target": None}
    assert votes.parse("Creating vote to KICK USER Bob [] from Alan Montes []") == {"kind": "kick", "voter": "Alan Montes", "target": "Bob"}
    assert votes.parse("Creating vote to KICK USER Bob [] from Alan from Spain []")["voter"] == "Alan from Spain"   # a name with « from » in it
    odd = votes.parse("Creating vote for UNKNOWN from Alan []")
    assert odd["kind"] == "unknown" and odd["raw"].startswith("Creating vote")   # said, never dropped
    assert votes.parse("NextSession") is None and votes.parse("DRIVER: Alan Montes []") is None


class _Inst:
    server_id = 7

    def __init__(self, drivers=(), with_acsp=True):
        self.sent = []
        self.acsp = SimpleNamespace(send=self.sent.append) if with_acsp else None
        self.logboard = SimpleNamespace(board=SimpleNamespace(drivers=list(drivers)))


class _Now:
    """threading.Thread that runs its target at once, so the test can read what the stewards' thread would post."""
    def __init__(self, target, args=(), daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


def _capture(monkeypatch):
    out = {"alerts": [], "votes": [], "events": []}
    monkeypatch.setattr(votes.threading, "Thread", _Now)
    monkeypatch.setattr(votes, "_told", {})
    monkeypatch.setattr(discord, "alert", out["alerts"].append)   # the server-status channel: a vote must never go there
    monkeypatch.setattr(discord, "notify_votes", out["votes"].append)
    monkeypatch.setattr(metrics, "enqueue", lambda sid, kind, **kw: out["events"].append((sid, kind, kw)))
    return out


def test_the_notice_names_the_voter_the_target_and_the_slot_only_when_it_is_unambiguous(monkeypatch):
    out = _capture(monkeypatch)
    alan = Driver(car_id=3, name="Alan Montes", guid="76561198000000001")
    inst = _Inst([alan, Driver(car_id=4, name="Bob", guid="76561198000000002")])
    votes.on_log_line(inst, "Creating vote to KICK USER Bob [] from Alan Montes []")
    assert inst.sent == [acsp.encode_broadcast_chat("Alan Montes ha iniciado una votación para expulsar a Bob.")]
    msg = out["votes"][0]
    assert "Steam 76561198000000001 inició" in msg and "expulsar a Steam 76561198000000002." in msg and "slot" not in msg
    assert "Alan" not in msg and "Bob" not in msg and not out["alerts"]   # Discord gets IDs, never a name a driver typed, and never the status channel
    assert out["events"] == [(7, "vote_kick", {"name": "Alan Montes", "car": "Bob"})]
    out["votes"].clear()
    votes._told.clear()   # (the same voter a second time would be spaced out)
    twin = _Inst([alan, Driver(car_id=5, name="Alan Montes", guid="9")])   # two drivers with that name: no identity is guessed
    votes.on_log_line(twin, "Creating vote to NEXT SESSION from Alan Montes []")
    assert "Steam ID no identificado" in out["votes"][0] and "siguiente sesión" in out["votes"][0]


def test_names_typed_by_a_driver_cannot_ping_or_format_the_stewards_channel(monkeypatch):
    out = _capture(monkeypatch)
    inst = _Inst()
    votes.on_log_line(inst, "Creating vote to KICK USER @everyone [] from **Mala\x00 \x1b[31mIdea** []")
    chat = inst.sent[0]
    u = lambda t: t.encode("utf-32-le")   # ACSP strings are UTF-32
    assert u("expulsar a @everyone") in chat and u("**Mala [31mIdea** ha") in chat   # in the game's chat an @ is harmless; control characters are gone
    assert u("\x1b") not in chat and u("\x00") not in chat
    assert "everyone" not in out["votes"][0] and "Mala" not in out["votes"][0]   # nothing a driver typed reaches Discord: no ping, no markdown, no link
    votes.on_log_line(inst, "Creating vote to NEXT SESSION from " + "N" * 500 + " []")
    assert len(inst.sent[1]) < 600   # a long name does not flood the chat (40 characters, 4 bytes each)


def test_a_failing_channel_does_not_silence_the_others_nor_stop_the_log(monkeypatch):
    out = _capture(monkeypatch)
    inst = _Inst()
    inst.acsp = SimpleNamespace(send=lambda b: 1 / 0)   # the chat raises: the record and the stewards' notice still happen
    votes.on_log_line(inst, "Creating vote to RESTART SESSION from Ana []")
    assert len(out["events"]) == 1 and len(out["votes"]) == 1
    inst = _Inst(with_acsp=False)
    inst.logboard = None   # the identity lookup raises inside the stewards' notice: logged and swallowed
    votes.on_log_line(inst, "Creating vote to RESTART SESSION from Ana []")
    votes.on_log_line(inst, "any other line")


def test_the_history_read_again_after_adopt_is_not_told_twice(tmp_path, monkeypatch):
    told = []
    monkeypatch.setattr(votes, "on_log_line", lambda inst, line: told.append(line))
    monkeypatch.setattr(supervisor.integrity, "on_log_line", lambda *a: None)
    log = tmp_path / "server.log"
    old = b"Creating vote to NEXT SESSION from Old []\nNextSession\n"
    log.write_bytes(old)
    fake = SimpleNamespace(log_path=log, _log_pos=0, _live_from=len(old), log=deque(), server_id=1, logboard=SimpleNamespace(feed=lambda line: None))
    supervisor.Instance._tail(fake)   # the first read after adopt(): all of it is history
    assert told == [] and fake.log and fake._log_pos == len(old)
    with open(log, "ab") as f:
        f.write(b"Creating vote to NEXT SESSION from New []\n")
    supervisor.Instance._tail(fake)   # what the server writes from now on is told
    assert told == ["Creating vote to NEXT SESSION from New []"]
    fresh = SimpleNamespace(log_path=log, _log_pos=0, _live_from=0, log=deque(), server_id=1, logboard=SimpleNamespace(feed=lambda line: None))
    told.clear()
    supervisor.Instance._tail(fresh)   # a process this manager started: the whole log is new
    assert len(told) == 3   # every line goes to the notifier, which keeps only the votes


def test_a_chunk_with_history_and_news_tells_only_the_news(tmp_path, monkeypatch):
    told, fed = [], []
    monkeypatch.setattr(votes, "on_log_line", lambda inst, line: told.append(line))
    monkeypatch.setattr(supervisor.integrity, "on_log_line", lambda *a: None)
    log = tmp_path / "server.log"
    old = b"Creating vote to NEXT SESSION from Old []\nNextSession\n"
    log.write_bytes(old + b"Creating vote to KICK USER A [] from B []\npartial")   # news written between adopt()'s stat and its first read, plus a line still being written
    fake = SimpleNamespace(log_path=log, _log_pos=0, _live_from=len(old) - 4, log=deque(), server_id=1, logboard=SimpleNamespace(feed=fed.append))
    supervisor.Instance._tail(fake)   # _live_from falls in the middle of «NextSession»: that whole line is history, the next one is news
    assert told == ["Creating vote to KICK USER A [] from B []"]
    assert fed == ["Creating vote to NEXT SESSION from Old []", "NextSession", "Creating vote to KICK USER A [] from B []"]   # the live board still reads every line, whole
    assert fake._log_pos == len(old) + len(b"Creating vote to KICK USER A [] from B []\n")   # the partial line waits for its end


def test_an_unreadable_vote_line_cannot_break_out_of_its_quotes_to_ping(monkeypatch):
    out = _capture(monkeypatch)
    votes.on_log_line(_Inst(), "Creating vote for UNKNOWN from `@everyone` []")
    msg = out["votes"][0]
    assert "everyone" not in msg and "`" not in msg and "formato" in msg   # no raw text at all: nothing to break out of


def test_a_voter_who_opens_a_vote_every_second_is_told_once_but_recorded_every_time(monkeypatch):
    out = _capture(monkeypatch)
    inst, clock = _Inst(), [100.0]
    monkeypatch.setattr(votes.time, "monotonic", lambda: clock[0])
    for _ in range(3):
        votes.on_log_line(inst, "Creating vote to NEXT SESSION from Troll []")
        clock[0] += 1
    assert len(inst.sent) == 1 and len(out["votes"]) == 1 and len(out["events"]) == 3
    votes.on_log_line(inst, "Creating vote to NEXT SESSION from Otro []")   # somebody else: told at once
    clock[0] += votes.SPACING
    votes.on_log_line(inst, "Creating vote to NEXT SESSION from Troll []")   # and the troll again once the spacing has passed
    assert len(inst.sent) == 3


def test_a_name_of_only_unprintable_characters_does_not_break_the_notice(monkeypatch):
    out = _capture(monkeypatch)
    inst = _Inst()
    votes.on_log_line(inst, "Creating vote to NEXT SESSION from \x01\x02 []")
    assert inst.sent and "Steam ID no identificado" in out["votes"][0] and len(out["events"]) == 1
