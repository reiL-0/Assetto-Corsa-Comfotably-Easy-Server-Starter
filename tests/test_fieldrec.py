"""Input tapes exercise the production judges, not a replay implementation of them."""
import json
import queue
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.config import settings
from app.live import fieldrec, raceaudit
from app.live import racecontrol as control
from app.live.field import Field
from app.rulebook import Neutralization
from ops.replay_field import read_events, replay, sweep_rules


def record_scenario(monkeypatch, feed):
    events = []
    clock = [0.0]
    monkeypatch.setenv("ACM_FIELD_RECORD_SERVERS", "42")
    monkeypatch.setattr(raceaudit, "enqueue", lambda event: events.append(event))
    simulated = SimpleNamespace(time=lambda: 1000 + clock[0], monotonic=lambda: clock[0])
    monkeypatch.setattr(control, "time", simulated)
    rc = control.RaceControl({"session_type": 3})
    rc.server_id = 42
    rc.neutralization_rule = Neutralization(mode="shadow").model_dump()
    rc.audit = Mock()
    field = Field()
    feed(rc, field, clock)
    # Roundtrip through JSON exactly as on disk (including integer map keys).
    return json.loads(json.dumps(events)), rc


def pileup(rc, field, clock):
    for tick in range(17):  # six cars; three brake together and stay stopped for eight seconds
        t = tick / 2
        clock[0] = t
        for car in range(6):
            field.update(car, .1 + car * .01, now=t)
        speeds = {car: 0 if car < 3 and t >= .5 else 180 for car in range(6)}
        rc.incident_step(speeds, field, 1000 + t)


def test_pileup_exact_decision_and_determinism_and_stricter_threshold(monkeypatch):
    events, rc = record_scenario(monkeypatch, pileup)
    expected = [call.kwargs for call in rc.audit.call_args_list if call.args == ("neutralization_decision",)]
    assert len(events) == 17  # nested clear ticks and flags are not duplicated
    first = replay(events)
    assert first == replay(events)
    assert first["summary"]["complete"]
    assert first["summary"]["by_kind"] == {"pileup": 1}
    assert first["decisions"] == [{"kind": "pileup", "ts": 1005.5, "monotonic": 5.5,
                                    "server_id": 42, "cars": [0, 1, 2], "evidence": expected[0]}]
    assert replay(events, {"neutralization": {"spin": {"stopped_s": 10}}})["decisions"] == []


def test_queue_full_drops_without_throwing(monkeypatch):
    assert raceaudit.flush()
    pending = queue.Queue(maxsize=1)
    pending.put({})
    monkeypatch.setattr(raceaudit, "_pending", pending)
    monkeypatch.setenv("ACM_FIELD_RECORD_SERVERS", "42")
    rc = control.RaceControl({"session_type": 3})
    rc.server_id = 42
    rc.audit = Mock()
    before = fieldrec.stats()["dropped"]
    rc.incident_step({}, Field(), 1000)
    assert fieldrec.stats()["dropped"] == before + 1


def test_disabled_and_server_selection(monkeypatch):
    put = Mock()
    monkeypatch.setattr(raceaudit, "enqueue", put)
    monkeypatch.delenv("ACM_FIELD_RECORD_SERVERS", raising=False)
    rc = control.RaceControl({"session_type": 3})
    rc.audit = Mock()
    rc.server_id = 42
    rc.incident_step({}, Field(), 1000)
    put.assert_not_called()
    monkeypatch.setenv("ACM_FIELD_RECORD_SERVERS", "1, 42")
    rc.incident_step({}, Field(), 1000)
    assert put.call_count == 1
    monkeypatch.setenv("ACM_FIELD_RECORD_SERVERS", "*")
    assert fieldrec.enabled(99)


def test_pace_judge_and_auto_green_use_real_methods(monkeypatch):
    def feed(rc, field, clock):
        rc.neutralization_rule.update(green_after_laps=1, green_clear_s=1)
        rc.set_flag("fcy", 100, grace_s=3, now=1000, field=field)
        for tick in range(21):
            t = tick / 2
            clock[0] = t
            field.update(0, .1, now=t)
            field.progress[0] = 1 + t / 10
            field.completed_laps[0] = 1 if t < 7 else int(t - 5)
            samples = field.samples()
            rc.incident_step({0: 150}, field, 1000 + t)
            rc.pace_tick(samples, {0: 150}, t, 1000 + t)
            if t in (6, 7, 8, 9):
                rc.green_if_due(0, field)
    events, rc = record_scenario(monkeypatch, feed)
    result = replay(events)
    assert result == replay(events)
    assert result["summary"]["by_kind"] == {"auto_green_decision": 1, "pace": 1}
    assert rc.pacejudge.log[-1]["car"] == 0
    assert replay(events, {"detectors": {"pace": {"sustained_s": 10}}})["summary"]["by_kind"] == {"auto_green_decision": 1}
    assert replay(events) == result  # scoped calibration restores the real constants


def test_disconnect_epoch_pit_and_event_roundtrip(monkeypatch):
    def feed(rc, field, clock):
        field.update(0, .1, now=0)
        rc.incident_step({0: 180}, field, 1000)
        rc.report_pit(0, True, 1000)
        field.drop(0)
        clock[0] = .5
        rc.incident_step({}, field, 1000.5)
        field.update(0, .2, now=.5)
        rc.incident_step({0: 0}, field, 1000.5)
    events, _ = record_scenario(monkeypatch, feed)
    assert events[-1]["args"]["field"]["$field"]["0"]["$sample"]["generation"] == 1
    assert replay(events)["decisions"] == []
    assert replay(events[1:])["summary"]["complete"] is False


def test_storage_failure_observable(monkeypatch):
    assert raceaudit.flush()
    monkeypatch.setenv("ACM_FIELD_RECORD_SERVERS", "42")
    monkeypatch.setattr(raceaudit._Handler, "handle", Mock(side_effect=OSError("disk failed")))
    rc = control.RaceControl({"session_type": 3})
    rc.server_id = 42
    rc.audit = Mock()
    before = fieldrec.stats()["errors"]
    rc.incident_step({}, Field(), 1000)
    assert raceaudit.flush()
    assert fieldrec.stats()["errors"] == before + 1


def test_files_sweep_and_allowlist(tmp_path, monkeypatch):
    events, _ = record_scenario(monkeypatch, pileup)
    path = tmp_path / "field.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events))
    variants = list(sweep_rules({}, ["spin.stopped_s=5,10"]))
    assert [replay(read_events([path]), rules)["summary"]["decisions"] for _, rules in variants] == [1, 0]
    events[0]["kind"] = "__getattribute__"
    path.write_text(json.dumps(events[0]))
    with pytest.raises(ValueError, match="unsupported"):
        list(read_events([path]))


def test_give_back_excursion_contact_and_due_replay(monkeypatch):
    def feed(rc, field, clock):
        rc.gb_rule = {"window_s": 10, "seconds": 5}
        for tick in range(45):
            t = tick / 2
            clock[0] = t
            b = 5.2 + .004 * t
            a = b - .02 + .03 * min(1, max(0, (t - 5) / 2))
            if t == 7.5:
                rc.give_back_report(2, 1000 + t, 2000, 5, .2)
            rc.give_back_tick({1: b, 2: a}, {1: 120, 2: 120}, 1000 + t)
    events, _ = record_scenario(monkeypatch, feed)
    result = replay(events)
    assert result == replay(events)
    assert result["summary"]["by_kind"] == {"give_back_open": 1, "give_back_due": 1}
    assert [d["cars"] for d in result["decisions"]] == [[2], [2]]


def test_real_writer_persists_only_detector_data(monkeypatch):
    assert raceaudit.flush()
    monkeypatch.setenv("ACM_FIELD_RECORD_SERVERS", "42")
    rc = control.RaceControl({"session_type": 3, "name": "private session"})
    rc.server_id = 42
    rc.audit = Mock()
    rc.set_pace_car(5, guid="private driver guid")
    field = Field()
    field.update(0, .1)
    rc.incident_step({0: 180}, field, rc.clock.time())
    assert raceaudit.flush()
    path = next((Path(settings.data_dir) / "race-audit" / "field").glob("server-42-*.jsonl"))
    data = path.read_text()
    assert "private" not in data
    result = replay(read_events([path]))
    assert result["summary"]["inputs"] == 2
    assert path.stat().st_mode & 0o777 == 0o600


def test_shared_shutdown_drains_field_tape(monkeypatch, tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("isolated_field_audit", raceaudit.__file__)
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    monkeypatch.setattr(audit, "settings", SimpleNamespace(data_dir=tmp_path))
    for n in range(10):
        assert audit.enqueue({"format": fieldrec.FORMAT, "seq": n})
    assert audit.close(2)
    path = next((tmp_path / "race-audit" / "field").glob("*.jsonl"))
    assert [json.loads(line)["seq"] for line in path.read_text().splitlines()] == list(range(10))
    before = audit.stats()["dropped"]
    assert audit.enqueue({"format": fieldrec.FORMAT}) is False
    assert audit.stats()["dropped"] == before + 1


def test_full_queue_does_no_encode(monkeypatch):
    assert raceaudit.flush()
    pending = queue.Queue(maxsize=1)
    pending.put({})
    monkeypatch.setattr(raceaudit, '_pending', pending)
    encode = Mock(side_effect=AssertionError('must not encode'))
    monkeypatch.setattr(fieldrec, 'encode', encode)
    monkeypatch.setenv('ACM_FIELD_RECORD_SERVERS', '42')
    rc = control.RaceControl({'session_type': 3})
    rc.server_id, rc.audit = 42, Mock()
    rc.incident_step({}, Field(), 1000)
    encode.assert_not_called()


def test_encode_has_finite_budget():
    value = {'text': 'x' * 1_000_000, 'items': list(range(100000)), 'deep': [[[[[[[[[[[]]]]]]]]]]]}
    result = json.dumps(fieldrec.encode(value))
    assert len(result) < 128 * 1024
    assert 'truncated' in result


def test_all_public_entries_are_classified_and_wrapped():
    import inspect
    public = {name for name, method in inspect.getmembers(control.RaceControl, inspect.isfunction) if not name.startswith('_')}
    assert public == fieldrec.METHODS | fieldrec.EXCLUDED.keys()
    for name in fieldrec.METHODS:
        assert getattr(control.RaceControl, name).__dict__.get('_fieldrec_captured'), name
    assert 'give_back_due_now' in fieldrec.METHODS


@pytest.mark.parametrize('compressed', [False, True])
def test_truncated_tape_preserves_complete_events(tmp_path, monkeypatch, compressed):
    import gzip
    events, _ = record_scenario(monkeypatch, pileup)
    payload = (json.dumps(events[0]) + '\n' + json.dumps(events[1])[:100]).encode()
    path = tmp_path / ('tape.jsonl.gz' if compressed else 'tape.jsonl')
    path.write_bytes(gzip.compress(payload)[:-5] if compressed else payload)
    with pytest.warns(UserWarning, match='discard'):
        assert list(read_events([path])) == events[:1]


def test_forgive_remaps_global_obligation_ids(monkeypatch):
    import itertools

    from app.live import giveback
    monkeypatch.setattr(giveback, '_IDS', itertools.count(701))
    def feed(rc, field, clock):
        rc.gb_rule = {'window_s': 10, 'seconds': 5}
        for tick in range(45):
            t = tick / 2
            clock[0] = t
            b = 5.2 + .004 * t
            a = b - .02 + .03 * min(1, max(0, (t - 5) / 2))
            if t == 7.5:
                rc.give_back_report(2, 1000 + t, 2000, 5, .2)
            rc.give_back_tick({1: b, 2: a}, {1: 120, 2: 120}, 1000 + t)
            if t == 9:
                ob = next(iter(rc.giveback._open.values()))
                assert ob['id'] == 701
                assert rc.give_back_forgive(ob['id'])
    events, _ = record_scenario(monkeypatch, feed)
    assert replay(events)['summary']['by_kind'] == {'give_back_open': 1}


def test_manual_due_and_rollback_remap_ids_between_sessions(monkeypatch):
    import itertools

    from app.live import giveback
    monkeypatch.setattr(giveback, '_IDS', itertools.count(901))
    def feed(rc, field, clock):
        rc.gb_rule = {'window_s': 10, 'seconds': 5}
        for tick in range(45):
            t = tick / 2
            clock[0] = t
            b = 5.2 + .004 * t
            a = b - .02 + .03 * min(1, max(0, (t - 5) / 2))
            if t == 7.5:
                for car in (2, 4):
                    rc.give_back_report(car, 1000 + t, 2000, 5, .2)
            rc.give_back_tick({1: b, 2: a, 3: b + 1, 4: a + 1}, {c: 120 for c in range(1, 5)}, 1000 + t)
            if t == 9:
                obs = {ob['car']: ob['id'] for ob in rc.giveback._open.values()}
                assert rc.give_back_forgive(obs[2])
                ob = rc.give_back_due_now(obs[4])
                assert ob
                rc.give_back_restore(ob)
    first, _ = record_scenario(monkeypatch, feed)
    second, _ = record_scenario(monkeypatch, feed)
    result = replay(first + second)
    assert result['summary']['complete']
    assert result['summary']['sessions'] == 2
    assert result['summary']['by_kind'] == {'give_back_open': 4, 'give_back_due': 2}
    assert [d['cars'] for d in result['decisions'] if d['kind'] == 'give_back_due'] == [[4], [4]]


def test_truncated_gzip_footer_preserves_all_complete_lines(tmp_path, monkeypatch):
    import gzip
    events, _ = record_scenario(monkeypatch, pileup)
    path = tmp_path / 'complete.jsonl.gz'
    path.write_bytes(gzip.compress(('\n'.join(json.dumps(e) for e in events) + '\n').encode())[:-6])
    discarded = []
    with pytest.warns(UserWarning, match='truncated gzip'):
        assert list(read_events([path], discarded)) == events
    assert discarded == [{'file': str(path), 'discarded': 1, 'reason': 'truncated gzip stream'}]


def test_cli_sweep_counts_discarded_tail_per_run(tmp_path, monkeypatch, capsys):
    from ops.replay_field import main
    events, _ = record_scenario(monkeypatch, pileup)
    path = tmp_path / 'tail.jsonl'
    path.write_text(json.dumps(events[0]) + '\n{"format":')
    monkeypatch.setattr('sys.argv', ['replay_field', str(path), '--sweep', 'spin.stopped_s=5,10'])
    with pytest.warns(UserWarning):
        main()
    results = json.loads(capsys.readouterr().out)['sweep']
    assert all(len(r['summary']['discarded']) == 1 and not r['summary']['complete'] for r in results)


def test_armed_nascar_has_explicit_error(monkeypatch):
    events, _ = record_scenario(monkeypatch, pileup)
    events[0]['nascar'] = True
    with pytest.raises(ValueError, match='Armed NASCAR.*sessions without.*disarm'):
        replay(events)


def test_a_vsc_never_sanctions_for_speed_only_the_fcy_does_when_there_is_no_reference_lap():
    def run(flag, mode="delta"):
        from app.live.field import Field
        field, rc = Field(), control.RaceControl(session_ref={"session_type": 3})
        rc.server_id = 9
        rc.set_flag(flag, 60, grace_s=3, now=1000, field=field, mode=mode)
        for tick in range(30):   # a car at 200 km/h under a 60 km/h flag, with no reference lap
            t = tick / 2
            field.update(0, .1, now=t)
            field.progress[0] = 1 + t / 10
            rc.pace_tick(field.samples(), {0: 200}, t, 1000 + t)
        return list(rc.violations)
    assert run("vsc") == [] and run("vsc", "fixed") == [], "a VSC is judged by delta only: nothing for the speed"
    assert run("fcy") != [], "the FCY has no reference laps: its speed is its delta"
