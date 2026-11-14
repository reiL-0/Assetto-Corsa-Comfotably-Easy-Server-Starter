"""`cuts_off` / `drs_off`: a session can switch the track-limit cuts (per kind of session) and the DRS off, whatever the server, its preset or the league say."""
import pytest
from pydantic import ValidationError
from sqlmodel import Session

from app import league
from app.db import engine
from app.events import EventIn
from app.live import racecontrol as live_rc
from app.models import Server
from app.rulebook import DrsRule
from app.schemas.servers import SessionIn

BASE = {'name': 'S', 'track': 'monza', 'cars': ['ks_gt3'], 'race_laps': 5}
MINE_CUTS = {'cuts_per_step': 1, 'step_seconds': 5, 'max_seconds': 600, 'tolerance': 2, 'ladder': [0, 5]}


def _server(port, **kw):
    with Session(engine) as s:
        sv = Server(name=f"o{port}", base_port=port, **kw)
        s.add(sv); s.commit()
        return sv.id


def test_off_is_validated_and_never_contradicts_a_rule_of_its_own():
    assert SessionIn(**BASE).cuts_off == [] and SessionIn(**BASE).drs_off is False
    assert SessionIn(**BASE, cuts_off=['practice', 'practice', 'qualify']).cuts_off == ['practice', 'qualify']
    with pytest.raises(ValidationError):
        SessionIn(**BASE, cuts_off=['sprint'])
    SessionIn(**BASE, cuts_off=['practice'], cuts_rule=MINE_CUTS)   # (practice is informational: a rule of its own for the race is no contradiction)
    with pytest.raises(ValidationError):
        SessionIn(**BASE, cuts_off=['race'], cuts_rule=MINE_CUTS)
    with pytest.raises(ValidationError):
        SessionIn(**BASE, drs_off=True, drs_rule=DrsRule().model_dump())
    # an event keeps its rules next to the session: the same check there
    with pytest.raises(ValidationError):
        EventIn(title='e', session=SessionIn(**BASE, drs_off=True), drs_rule=DrsRule().model_dump())
    with pytest.raises(ValidationError):
        EventIn(title='e', session=SessionIn(**BASE, cuts_off=['race']), cuts_rule=MINE_CUTS)
    assert EventIn(title='e', session=SessionIn(**BASE, drs_off=True)).session.drs_off


def test_the_session_can_switch_the_drs_off_over_the_servers():
    league._EFFECTIVE.clear()
    rules = {'detectors': {'drs': DrsRule(start_laps_off=4).model_dump()}}
    on = league.effective_rules(_server(22000, rules=rules, session={}))
    off = league.effective_rules(_server(22001, rules=rules, session={'drs_off': True}))
    assert on['drs']['start_laps_off'] == 4 and off['drs'] is None
    assert league.effective_rules(_server(22002, session=None))['drs'] is None, 'a server that was never configured has no DRS to switch off'


@pytest.mark.parametrize('session_type, name, counted', [(1, 'practice', 0), (2, 'practice', 1), (3, 'practice', 1), (3, 'race', 0)])
def test_the_cuts_of_a_session_kind_that_is_off_are_neither_counted_nor_shown(monkeypatch, session_type, name, counted):
    from types import SimpleNamespace
    from app import supervisor
    league._EFFECTIVE.clear()   # (an empty cache, as right after an apply: the Instance still knows)
    sid = _server(22010 + session_type * 2 + (name == 'race'))
    monkeypatch.setitem(supervisor._instances, sid, SimpleNamespace(cuts_off=(name,)))
    rc = live_rc.RaceControl({'session_type': session_type})
    rc.server_id = sid
    rule = league.cut_rule_cached(sid)
    rc.add_cuts(3, 2, True, rule, '76561198000000003')
    assert rc.cuts_client.get(3, 0) == 2 * counted
    view = rc.cuts_view(rule)
    assert bool(view['cuts']) == bool(counted), view
    if not counted:
        assert view['cuts_rule'] is None


def test_an_applied_session_gives_its_instance_the_cuts_off(monkeypatch):
    from types import SimpleNamespace
    from app import supervisor
    from app.services import server_service
    import asyncio
    sid = _server(22030)
    inst = SimpleNamespace(running=True, acsp=None, nascar_config=None, rolling='off', cuts_off=())
    monkeypatch.setitem(supervisor._instances, sid, inst)
    monkeypatch.setattr(server_service, 'check_content', lambda body, cars: {car: [] for car in cars})
    with Session(engine) as sess:
        asyncio.run(server_service.apply(sess, sess.get(Server, sid), SessionIn(**BASE, cuts_off=['practice', 'race'], restart=False)))
    assert inst.cuts_off == ('practice', 'race')
    with Session(engine) as sess:
        asyncio.run(server_service.apply(sess, sess.get(Server, sid), SessionIn(**BASE, restart=False)))
    assert inst.cuts_off == (), 'the next apply replaces it'


def test_a_leagues_frozen_rulebook_carries_the_off_so_results_make_no_notices_or_penalties():
    import time
    from app.league import session_for
    from app.models import Championship, Event, LeagueMember
    from app.rulebook import CutRule, Detectors, Rulebook
    cut = CutRule(cuts_per_step=1, step_seconds=5, max_seconds=60, tolerance=2, ladder=[0, 5])
    rb = Rulebook(detectors=Detectors(cuts=cut, drs=DrsRule(start_laps_off=2))).model_dump()
    with Session(engine) as s:
        c = Championship(name='off', rulebook=rb)
        s.add(c); s.commit()
        s.add(LeagueMember(championship_id=c.id, guid='76561198000000021', name='A', car='ks_gt3')); s.commit()
        def snap(**extra):
            ev = Event(title='e', league_id=c.id, data={**BASE, **extra}); s.add(ev); s.commit()
            return session_for(s, ev, time.time(), notify=False).rulebook_snapshot.detectors
        both = snap()
        assert both.cuts is not None and both.drs is not None, 'inherit: the league decides'
        practice_only = snap(cuts_off=['practice'])
        assert practice_only.cuts is not None, 'practice off: the race still has the ladder'
        off = snap(cuts_off=['race'], drs_off=True)
        assert off.cuts is None and off.drs is None
        # the result is counted (ChampionshipEvent.rulebook_snapshot) under _event_rules: it must say the same, or cut_notices / cut_penalties / drs_penalties still punish
        from app.stewards.auto import _event_rules
        ev_off = Event(title='r', league_id=c.id, data={**BASE, 'cuts_off': ['race'], 'drs_off': True}); s.add(ev_off); s.commit()
        counted = _event_rules(c, ev_off).detectors
        assert counted.cuts is None and counted.drs is None
        ev_practice = Event(title='p', league_id=c.id, data={**BASE, 'cuts_off': ['practice']}); s.add(ev_practice); s.commit()
        assert _event_rules(c, ev_practice).detectors.cuts is not None and _event_rules(c, ev_practice).detectors.drs is not None
