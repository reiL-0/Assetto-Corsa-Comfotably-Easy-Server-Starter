"""The white flag of a timed race without NASCAR: shown only when ACSERVER itself has said the time is over (its own elapsed_ms, never extrapolated), once, from the state (the hub draws it from
`rc.flag`: it drops notices with that text), never over a caution, and only in a timed race."""
import pytest
from test_racecontrol import live  # noqa: F401  (the `live` fixture)

from app import racecontrol as rcmod
from app.live import acsp
from app.live import racecontrol as live_rc

WHITE = "BANDERA BLANCA: última vuelta"
T = 1_000_000.0


def _rc(sid, c):
    return live_rc.state_of(sid, True, c.session, create=True)


def _whites(sid, c):
    return [n["text"] for n in _rc(sid, c).snapshot()["notices"] if n["text"] == WHITE]


def _timed(sid, c, elapsed):
    """A timed race of 10 min that has run `elapsed` s by the manager's clock; nothing heard from acServer yet."""
    c.session = {"session_type": 3, "time_min": 10, "laps": 0}
    c.session_t0, c.winner_done, c.last_lap_shown, c.info_at, c.info_elapsed_ms, c.info_ask = T - elapsed, False, False, 0.0, 0, 0.0
    _rc(sid, c)


def _answer(c, at, elapsed_s):
    """acServer answers SESSION_INFO at `at` with its own clock (what acsp._apply does with the reply)."""
    c.info_at, c.info_elapsed_ms, c.session_t0 = at, int(elapsed_s * 1000), at - elapsed_s


def test_the_white_flag_waits_for_acservers_own_word_and_comes_once(live):   # noqa: F811
    sid, c, sent = live
    _timed(sid, c, 580)
    rcmod.plain_white_tick(T)
    assert sent == [] and _whites(sid, c) == [], "20 s before the end: nothing is asked, nothing is shown"
    c.session_t0 = T - 598   # the manager's clock says 2 s left: from here acServer is asked for ITS clock, once a second
    rcmod.plain_white_tick(T)
    assert sent == [acsp.encode_get_session_info(-1)] and _whites(sid, c) == [], "asked, no answer yet: no flag"
    rcmod.plain_white_tick(T + 0.5)
    assert len(sent) == 1, "not asked again within the second"
    _answer(c, T + 1, 599.0)   # acServer: 599 s, one second to go
    rcmod.plain_white_tick(T + 1.1)
    rcmod.plain_white_tick(T + 2.4)   # (1.4 s later the manager's own clock would say 600.4: it must not take that for acServer's word)
    assert _whites(sid, c) == [] and _rc(sid, c).flag == "green", "acServer has NOT said the time is over"
    _answer(c, T + 3, 600.4)
    rcmod.plain_white_tick(T + 3.1)
    assert _whites(sid, c) == [WHITE] and c.last_lap_shown and _rc(sid, c).flag == "white", "the flag is in the STATE: that is what the hub draws"
    rcmod.plain_white_tick(T + 4)
    rcmod.plain_white_tick(T + 9)
    assert _whites(sid, c) == [WHITE], "once"


def test_an_old_answer_does_not_show_the_flag(live):   # noqa: F811
    sid, c, sent = live
    _timed(sid, c, 605)   # the manager's clock is past the limit...
    _answer(c, T - 5, 605.0)
    c.info_ask = T
    rcmod.plain_white_tick(T + 0.1)
    assert _whites(sid, c) == [], "...but acServer's last word is 5 s old: it asks again and waits for a fresh one"
    _answer(c, T + 0.2, 606.0)
    rcmod.plain_white_tick(T + 0.3)
    assert _whites(sid, c) == [WHITE]


def test_a_new_session_forgets_what_acserver_said_in_the_last_one():
    from app.live import acsp as acsp_mod
    c = acsp_mod.ACSPClient(1)
    c.info_at, c.info_elapsed_ms, c.first_done_at, c.last_lap_shown = T, 700_000, T, True
    captured = (b"2\x04\x00\x00\x01\x08O\x00\x00\x00P\x00\x00\x00R\x00\x00\x00 \x00\x00\x00t\x00\x00\x00e\x00\x00\x00s\x00\x00\x00t\x00\x00\x00\x07magione\x00\x08Practice"
                b"\x01X\x02\x00\x00\x00\x00\x11\x16\x073_clear\x00\x00\x00\x00")
    c.datagram_received(captured, ("127.0.0.1", 0))   # a NEW_SESSION
    assert (c.info_at, c.info_elapsed_ms, c.first_done_at, c.last_lap_shown) == (0.0, 0, 0.0, False)


def test_a_caution_in_force_is_not_taken_off_by_the_white_flag(live):   # noqa: F811
    sid, c, sent = live
    _timed(sid, c, 605)
    _rc(sid, c).flag = "yellow"
    _answer(c, T, 605.0)
    rcmod.plain_white_tick(T + 0.1)
    assert _rc(sid, c).flag == "yellow" and _whites(sid, c) == [], "the yellow stays: the race is ending under it"
    assert c.last_lap_shown, "and it is not asked about again"


@pytest.mark.parametrize("session", [
    {"session_type": 3, "time_min": 0, "laps": 5},    # by laps: the leader's last lap, not a clock (plain_winner)
    {"session_type": 2, "time_min": 10, "laps": 0},   # qualifying
    {"session_type": 1, "time_min": 10, "laps": 0},   # practice
])
def test_only_a_timed_race_has_a_clock_white_flag(live, session):   # noqa: F811
    sid, c, sent = live
    c.session = session
    _rc(sid, c)
    c.session_t0, c.winner_done, c.last_lap_shown = T - 700, False, False
    _answer(c, T, 700.0)
    rcmod.plain_white_tick(T + 0.1)
    assert _whites(sid, c) == [] and sent == []


def test_a_nascar_race_and_a_finished_race_are_left_alone(live):   # noqa: F811
    sid, c, sent = live
    _timed(sid, c, 700)
    _answer(c, T, 700.0)
    c.winner_done = True
    rcmod.plain_white_tick(T + 0.1)
    assert _whites(sid, c) == [] and sent == [], "the winner is already known"
    c.winner_done = False
    _rc(sid, c).configure_nascar(30, None, None)
    rcmod.plain_white_tick(T + 0.1)
    assert _whites(sid, c) == [] and sent == [], "NASCAR has its own white flag"
