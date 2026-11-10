"""Manual cuts stay live; running events exclusively own their rule."""
import json
import re
import time

import pytest
from pydantic import ValidationError
from sqlmodel import Session

from app import league
from app.db import engine
from app.live import racecontrol as rc
from app.models import Championship, Event, Schedule, Server
from app.rulebook import CutRule, Detectors, Rulebook
from app.schemas.servers import SessionIn


@pytest.mark.parametrize('ladder', [None, [0, 5]])
def test_manual_rule_and_event_precedence(monkeypatch, ladder):
    monkeypatch.setattr(league, "_EFFECTIVE", {})
    cut = CutRule(tolerance=2, cuts_per_step=1, step_seconds=5, max_seconds=60, ladder=ladder)
    body = SessionIn(name='manual', track='spa', cars=['car'], cuts_rule=cut)
    assert body.model_dump()['cuts_rule'] == cut.model_dump()
    assert SessionIn(name='old', track='spa', cars=['car']).cuts_rule is None
    with pytest.raises(ValidationError):
        SessionIn(name='bad', track='spa', cars=['car'], cuts_rule={**cut.model_dump(), 'tolerance': -1})
    with Session(engine) as s:
        server = Server(name='manual cuts', base_port=19000 + (1000 if ladder else 0), session=body.model_dump())
        s.add(server)
        s.commit()
        sid = server.id
        expected = dict(max=2, per=1, step=5, cap=60)
        if ladder is not None:
            expected['ladder'] = ladder
        manual = league.cut_rule(sid)
        assert manual == expected and manual.live_only
        assert league.cut_rule_cached(sid) is manual
        championship = Championship(name='cuts precedence', rulebook=Rulebook(detectors=Detectors(cuts=cut)).model_dump())
        s.add(championship)
        s.commit()
        ev = Event(title='running', league_id=championship.id, data={})
        s.add(ev)
        s.commit()
        s.add(Schedule(server_id=sid, event_id=ev.id, start_at=time.time(), state='running'))
        s.commit()
        league.invalidate_rule_cache(sid)
        event_rule = league.cut_rule(sid)
        assert event_rule == {**expected, 'cap': 10}
        assert not getattr(event_rule, 'live_only', False)
        championship.rulebook = Rulebook(detectors=Detectors(cuts=None)).model_dump()
        s.add(championship)
        s.commit()
        league.invalidate_rule_cache(sid)
        assert league.cut_rule(sid) == expected   # a league that says nothing about cuts does not switch the server's own rule off...
        assert league.cut_rule(sid).live_only      # ...but it is live only: nothing from it goes to the league's results
        ev.data = {'cuts_rule': None}
        s.add(ev)
        s.commit()
        league.invalidate_rule_cache(sid)
        assert league.cut_rule(sid) == expected
        assert league.cut_rule(sid).live_only
        server2 = Server(name='no cuts', base_port=19100 + (1000 if ladder else 0))
        s.add(server2)
        s.commit()
        assert league.cut_rule(server2.id) == league.DEFAULT_CUT_RULE
        assert league.cut_rule(server2.id).live_only


@pytest.mark.parametrize('manual', [True, False])
def test_live_dt_and_result_queue(monkeypatch, manual):
    monkeypatch.setattr(rc, '_cut_dues_pending', {})
    monkeypatch.setattr(rc, '_cut_served', set())
    rule = dict(max=2, per=1, step=5, cap=60, ladder=[0, 5])
    if manual:
        rule = league._ManualCutRule(rule)
    state = rc.RaceControl()
    state.server_id = 123
    state.session_ref = {'session_type': 3}
    state.add_cuts(3, 6, True, rule, '76561190000000001')
    assert state.cuts_client[3] == 6
    assert len(state.dt_open) == 1 and len(state.penalties) == 1
    assert len(rc.pending_cut_dues()) == (0 if manual else 2)
    encoded = json.dumps(state.cuts_view(rule), separators=(',', ':'))
    assert re.search(r'"cuts_rule":\{"cap":60,"max":2,"per":1,"step":5\}', encoded)

    state.report_pit(3, True, now=state.clock.time() + 1)
    assert len(rc.pending_cut_served()) == (0 if manual else 1)


def test_old_event_clients_keep_inheritance():
    from app.events import EventIn, create_event, update_event

    body = EventIn(title='old preset', session=SessionIn(name='old', track='spa', cars=['car']))
    with Session(engine) as s:
        created = create_event(body, s)
        assert 'cuts_rule' not in s.get(Event, created.id).data
        update_event(created.id, body, s)
        assert 'cuts_rule' not in s.get(Event, created.id).data
        explicit = body.model_copy(update={'cuts_rule': None})
        disabled = create_event(explicit, s)
        assert s.get(Event, disabled.id).data["cuts_rule"] is None
        update_event(disabled.id, body.model_copy(update={"inherit_cuts_rule": True}), s)
        assert "cuts_rule" not in s.get(Event, disabled.id).data


@pytest.mark.parametrize("session_rule", [None, CutRule(tolerance=9, cuts_per_step=1, step_seconds=5, max_seconds=60)])
def test_session_rule_never_selects_event_rule(session_rule):
    from app.events import EventIn, create_event, update_event

    base = dict(title='calendar inheritance', session=dict(name='calendar', track='spa', cars=['car'], cuts_rule=session_rule))
    custom = CutRule(tolerance=2, cuts_per_step=1, step_seconds=7, max_seconds=60)
    with Session(engine) as s:
        inherited = create_event(EventIn(**base), s)
        assert 'cuts_rule' not in s.get(Event, inherited.id).data
        update_event(inherited.id, EventIn(**base), s)
        assert 'cuts_rule' not in s.get(Event, inherited.id).data
        for rule in (custom, None):
            saved = create_event(EventIn(**base, cuts_rule=rule), s)
            expected = rule.model_dump() if rule else None
            assert s.get(Event, saved.id).data['cuts_rule'] == expected
            update_event(saved.id, EventIn(**base), s)
            assert s.get(Event, saved.id).data['cuts_rule'] == expected
            update_event(saved.id, EventIn(**base, inherit_cuts_rule=True), s)
            assert 'cuts_rule' not in s.get(Event, saved.id).data


def test_applied_event_rule_stays_out_of_manual_server_session():
    custom = CutRule(tolerance=3, cuts_per_step=1, step_seconds=7, max_seconds=60)
    with Session(engine) as s:
        event = Event(title='custom cuts', data=dict(name='event', track='spa', cars=['car'], cuts_rule=custom.model_dump()))
        s.add(event)
        s.commit()
        applied = league.session_for(s, event, 0)
        server = Server(name='applied event', base_port=23000, session=applied.model_dump(mode='json'))
        s.add(server)
        s.commit()
        s.refresh(server)
        assert server.session.get('cuts_rule') is None
        assert server.session['rulebook_snapshot']['detectors']['cuts'] == custom.model_dump()


def test_cold_default_counts_live_without_dues(monkeypatch):
    monkeypatch.setattr(league, "_EFFECTIVE", {})
    monkeypatch.setattr(rc, '_cut_dues_pending', {})
    rule = league.cut_rule_cached(999992)
    assert rule == league.DEFAULT_CUT_RULE and rule.live_only
    state = rc.RaceControl({'session_type': 3})
    state.server_id = 999992
    state.add_cuts(1, 3, True, rule, 'driver')
    assert state.cuts_view(rule)['cuts'] == {'1': 3}
    assert state.penalties and state.dt_open and not rc.pending_cut_dues()
    assert state.cuts_view(rule)['cuts_rule'] == dict(cap=600, max=2, per=1, step=5)
