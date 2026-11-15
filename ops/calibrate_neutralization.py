"""What the automatic VSC / FCY / safety car would have done in races already driven, from the race audit (data/race-audit/events.jsonl).

    python ops/calibrate_neutralization.py events.jsonl [events.jsonl.1 ...] [--escalate-s 8] [--immobile-s 60]

The audit keeps every `neutralization_decision` (a stopped car confirmed after `spin.stopped_s`) and the `spin_cleared` that closes it, so the length of every stop is known:
a stop of `escalate_s` or more would have brought the VSC / FCY, one of `immobile_s` or more the safety car. A slow car (`slow_car`) never brings a flag and is only counted.
It reads decisions, not telemetry: a continuous tape (app/live/fieldrec.py, ACM_FIELD_RECORD_SERVERS) can be replayed with ops/replay_field.py for the full picture, wrecks (damage) included.
Prints one line per session hour (server, UTC hour) and the totals. Nothing is written."""
import argparse
import json
import sys
from collections import Counter, defaultdict


def stops(rows):
    """[(server, hour, seconds the car was stopped)] of every confirmed stop that was closed, from the decisions and their `spin_cleared`.
    The decision comes `stopped_s` (5 s) into the stop and the clearing `clear_s` (5 s) after it ended: the two margins cancel, so decision -> clearing IS the length of the stop.
    A stop shorter than `stopped_s` is never confirmed, so it is not in the audit: the lengths here start at 5 s."""
    open_stops, out = {}, []
    for r in sorted((r for r in rows if r.get("kind") in ("neutralization_decision", "spin_cleared")), key=lambda r: r.get("ts", "")):   # (by time, so the order of the files does not matter)
        if r["kind"] == "neutralization_decision":
            if r["decision"]["kind"] == "spin":
                for car in r["decision"]["cars"]:
                    open_stops[(r["server_id"], str(car))] = (r["decision"]["events"][str(car)]["t"], r["ts"][:13])   # (a new stop replaces one that was never closed)
        else:
            begun = open_stops.pop((r["server_id"], str(r["car"])), None)   # (each closing ends one stop, once)
            if begun:
                out.append((r["server_id"], begun[1], r["monotonic"] - begun[0]))
    return out


def calibrate(rows, escalate_s=8.0, immobile_s=60.0):
    """{(server, hour): Counter(vsc_fcy, sc, local_stops, slow_car)} for the rows of an audit."""
    per = defaultdict(Counter)
    for server, hour, held in stops(rows):
        per[(server, hour)]["local_stops"] += 1
        if escalate_s and held >= escalate_s:
            per[(server, hour)]["vsc_fcy"] += 1
        if immobile_s and held >= immobile_s:
            per[(server, hour)]["sc"] += 1
    for r in rows:
        if r.get("kind") == "neutralization_decision" and r["decision"]["kind"] == "slow_car":
            per[(r["server_id"], r["ts"][:13])]["slow_car"] += len(r["decision"]["cars"])
    return per


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--escalate-s", type=float, default=8.0)
    ap.add_argument("--immobile-s", type=float, default=60.0)
    a = ap.parse_args(argv)
    rows = []
    for f in a.files:
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass   # (a line cut at the rotation)
    per = calibrate(rows, a.escalate_s, a.immobile_s)
    total = Counter()
    for (server, hour), c in sorted(per.items(), key=lambda kv: kv[0][1]):
        print(f"server {server}  {hour}h  stops {c['local_stops']:3d}  VSC/FCY {c['vsc_fcy']:3d}  SC {c['sc']:3d}  slow_car (reported only) {c['slow_car']:3d}")
        total.update(c)
    print(f"TOTAL ({len(per)} session hours, escalate_s={a.escalate_s:g}, immobile_s={a.immobile_s:g}): stops {total['local_stops']}, VSC/FCY {total['vsc_fcy']}, SC {total['sc']}, slow_car {total['slow_car']} (never a flag)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
