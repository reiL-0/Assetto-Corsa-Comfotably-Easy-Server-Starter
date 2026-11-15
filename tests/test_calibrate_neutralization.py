"""ops/calibrate_neutralization.py: what the automatic VSC / FCY / safety car would have done in an audit already recorded."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("calibrate_neutralization", Path(__file__).resolve().parent.parent / "ops" / "calibrate_neutralization.py")
cal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cal)


def rows():
    def decision(kind, car, t, mono):
        return {"kind": "neutralization_decision", "server_id": 1, "ts": "2026-11-12T05:10:00+00:00", "monotonic": mono,
                "decision": {"kind": kind, "cars": [car], "events": {str(car): {"t": t, "kind": kind}}}}

    def cleared(car, mono):
        return {"kind": "spin_cleared", "server_id": 1, "ts": "2026-11-12T05:11:00+00:00", "monotonic": mono, "car": car, "reason": "resumed", "clear_s": 5}
    return [decision("spin", 1, 100.0, 100.0), cleared(1, 106.0),     # a stop of 6 s
            decision("spin", 2, 200.0, 200.0), cleared(2, 220.0),     # 20 s
            decision("spin", 3, 300.0, 300.0), cleared(3, 380.0),     # 80 s
            decision("slow_car", 4, 400.0, 400.0)]                    # a slow driver


def test_stops_are_counted_by_length_and_slow_cars_never_bring_a_flag():
    per = cal.calibrate(rows(), escalate_s=8, immobile_s=60)
    c = per[(1, "2026-11-12T05")]
    assert (c["local_stops"], c["vsc_fcy"], c["sc"], c["slow_car"]) == (3, 2, 1, 1)
    assert cal.calibrate(rows(), escalate_s=0, immobile_s=0)[(1, "2026-11-12T05")]["vsc_fcy"] == 0, "0 = off"
    assert cal.calibrate(rows(), escalate_s=5, immobile_s=15)[(1, "2026-11-12T05")]["vsc_fcy"] == 3


def test_the_cli_prints_the_totals(tmp_path, capsys):
    import json
    f = tmp_path / "events.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in rows()) + "\nnot json: a line cut at the rotation\n")
    assert cal.main([str(f)]) == 0
    assert "stops 3, VSC/FCY 2, SC 1, slow_car 1" in capsys.readouterr().out


def test_the_order_of_the_files_does_not_change_the_pairing(tmp_path):
    import json
    old = [{"kind": "neutralization_decision", "server_id": 1, "ts": "2026-11-07T05:00:00+00:00", "monotonic": 100.0, "decision": {"kind": "spin", "cars": [1], "events": {"1": {"t": 100.0, "kind": "spin"}}}}]   # never cleared
    new = rows()
    a, b = tmp_path / "events.jsonl", tmp_path / "events.jsonl.1"
    a.write_text("\n".join(json.dumps(r) for r in new))
    b.write_text("\n".join(json.dumps(r) for r in old))
    import io
    import contextlib
    outs = []
    for order in ([a, b], [b, a]):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cal.main([str(f) for f in order])
        outs.append(buf.getvalue())
    assert outs[0] == outs[1]
    assert "stops 3, VSC/FCY 2, SC 1" in outs[0], "the old stop that was never cleared is not paired with a later closing"
