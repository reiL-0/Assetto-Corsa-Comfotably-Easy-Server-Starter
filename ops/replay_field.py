"""Replay the manager's pure race judges without starting the application."""
import argparse
import gzip
import itertools
import json
import math
import sys
import warnings as tape_warnings
import zlib
from collections import Counter
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.live import giveback
from app.live import racecontrol as control
from app.live.field import Field, Sample
from app.live.fieldrec import FORMAT, METHODS
from app.live.raceaudit import TRUNCATED
from app.rulebook import Detectors, Neutralization

DECISIONS = {"neutralization_decision", "auto_green_decision", "pace_judge", "no_pass", "give_back", "nascar_incident"}


def decode(value):
    if isinstance(value, list):
        return [decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    if "$sample" in value:
        return Sample(**value["$sample"])
    if "$field" in value:
        samples = decode(value["$field"])
        field = Field()
        field.session = value.get("session", 0)
        for car, sample in samples.items():
            for key in ("progress", "epoch", "generation", "stamp", "since"):
                getattr(field, key)[car] = getattr(sample, key)
            if sample.completed_laps is not None:
                field.completed_laps[car] = sample.completed_laps
        return field
    return {int(k) if isinstance(k, str) and k.lstrip("-").isdigit() else k: decode(v)
            for k, v in value.items()}


def merge(base, overlay):
    result = deepcopy(base)
    for key, value in overlay.items():
        result[key] = merge(result.get(key, {}), value) if isinstance(value, dict) else value
    return result


def read_events(paths, discarded=None):
    """Keep complete records before crash tails; warn/count discarded fragments.

    Invalid complete records remain errors. Plain JSON without final newline is
    valid; only malformed unterminated tails and incomplete gzip streams recover.
    """
    discarded = discarded if discarded is not None else []
    def discard(file, reason):
        entry = {"file": str(file), "discarded": 1, "reason": reason}
        discarded.append(entry)
        tape_warnings.warn(f"{file}: discarded {reason}", UserWarning)
    for name in paths:
        path = Path(name)
        files = sorted(path.glob("*.jsonl*"), key=lambda p: (p.stat().st_mtime_ns, p.name)) if path.is_dir() else [path]
        for file in files:
            # Small binary reads preserve completed lines even when gzip's text
            # buffering would otherwise lose them on a missing footer.
            opener = gzip.open if file.suffix == ".gz" else open
            tail, number = b"", 0
            def parse(line):
                event = json.loads(line)
                if not isinstance(event, dict) or event.get("format") != FORMAT or event.get("kind") not in METHODS:
                    raise ValueError("unsupported field tape event")
                return event
            with opener(file, "rb") as stream:
                try:
                    while True:
                        chunk = stream.read1(65536)
                        if not chunk:
                            break
                        tail += chunk
                        while b"\n" in tail:
                            line, tail = tail.split(b"\n", 1)
                            number += 1
                            try:
                                yield parse(line)
                            except (ValueError, TypeError) as exc:
                                raise ValueError(f"{file}:{number}: {exc}") from exc
                except (EOFError, zlib.error):
                    discard(file, "truncated gzip stream")
                if tail:
                    try:
                        json.loads(tail)
                    except (ValueError, UnicodeError):
                        discard(file, "incomplete final line")
                    else:
                        yield parse(tail)


def is_truncated(value):
    if isinstance(value, str):
        return TRUNCATED in value
    if isinstance(value, dict):
        return any(is_truncated(k) or is_truncated(v) for k, v in value.items())
    if isinstance(value, list):
        return any(is_truncated(v) for v in value)
    return False


def replay(events, rules=None, server_id=None):
    """Run in one thread. Scoped clock/counters are restored even on malformed input."""
    rules = rules or {}
    if not isinstance(rules, dict):
        raise TypeError("rules must be a JSON object")
    if set(rules) - {"neutralization", "detectors"}:
        raise ValueError("rules accepts neutralization and detectors")
    states, sequences, decisions, warnings = {}, {}, [], []
    id_maps = {}
    clock = [0.0, 0.0]
    selected = 0
    current_args = [{}]
    detector_rules = deepcopy(rules.get("detectors", {}))
    if not isinstance(detector_rules, dict):
        raise TypeError("detectors must be a JSON object")
    pace = detector_rules.pop("pace", {})
    if not isinstance(pace, dict):
        raise TypeError("detectors.pace must be a JSON object")
    with ExitStack() as stack:
        for key, value in pace.items():
            if key not in ("sustained_s", "max_gap_s", "fresh_s") or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("detectors.pace accepts positive sustained_s, max_gap_s, fresh_s")
            stack.enter_context(patch.object(control.pace_rules, key.upper(), value))
        if detector_rules:
            Detectors.model_validate(detector_rules)
        simulated = SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[1])
        for name in ("_penalty_ids", "_vsc_ids", "_notice_ids"):
            stack.enter_context(patch.object(control, name, itertools.count(1)))
        stack.enter_context(patch.object(giveback, "_IDS", itertools.count(1)))
        for event in events:
            if server_id is not None and event["server_id"] != server_id:
                continue
            if event.get("nascar"):
                raise ValueError("Armed NASCAR is unsupported: this replay only supports sessions without the NASCAR engine; disarm it before recording (NASCAR replay is not implemented)")
            selected += 1
            if is_truncated(event):
                warnings.append({"server_id": event["server_id"], "session": event["session"], "seq": event["seq"], "reason": "truncated_input"})
                continue
            clock[:] = event["ts"], event["monotonic"]
            key = (event["server_id"], event["session"])
            seq = event["seq"]
            if seq != sequences.get(key, 0) + 1:
                warnings.append({"server_id": key[0], "session": key[1], "expected_seq": sequences.get(key, 0) + 1, "seq": seq})
            sequences[key] = seq
            if key not in states:
                if event["flag"] != "green":
                    warnings.append({"server_id": key[0], "session": key[1], "reason": "recording_started_under_caution"})
                rc = control.RaceControl({"session_type": event["session_type"]}, clock=simulated)
                rc.server_id, rc._fieldrec_replay = key[0], True
                states[key] = rc

                def audit(kind, _rc=rc, **fields):
                    if kind not in DECISIONS:
                        return
                    if kind == "give_back":
                        for op, obligation, *detail in fields["events"]:
                            decisions.append({"kind": "give_back_" + op, "ts": clock[0],
                                              "monotonic": clock[1], "server_id": _rc.server_id,
                                              "cars": [obligation["car"]],
                                              "evidence": deepcopy({"obligation": obligation, "detail": detail, "rule": fields["rule"]})})
                        return
                    evidence = fields.get("decision", fields)
                    cars = evidence.get("cars", [evidence["car"]] if "car" in evidence else [])
                    if kind == "auto_green_decision" and "car" in current_args[0]:
                        cars = [current_args[0]["car"]]
                    decisions.append({"kind": evidence.get("kind", kind), "ts": clock[0],
                                      "monotonic": clock[1], "server_id": _rc.server_id,
                                      "cars": cars,
                                      "evidence": deepcopy(fields)})
                rc.audit = audit
            rc = states[key]
            context = deepcopy(event["context"])
            rule = merge(context.pop("neutralization_rule"), rules.get("neutralization", {}))
            extras = {k: rule.pop(k) for k in ("category", "rulebook_revision") if k in rule}
            rc.neutralization_rule = {**Neutralization.model_validate(rule).model_dump(), **extras}
            if "give_back" in detector_rules:
                detectors = Detectors.model_validate(detector_rules)
                gb = detectors.give_back
                context["gb_rule"] = gb.model_dump() if gb.enabled else None
            for name, value in context.items():
                if name in ("neutralization_excluded", "auto_off"):
                    value = set(value)
                setattr(rc, name, value)
            if event["kind"] not in METHODS:
                raise ValueError("unsupported field tape method")
            ids = id_maps.setdefault(key, {})
            for original in event.get("obligations", []):
                for ob in (rc.giveback._open.values() if rc.giveback else ()):
                    if (ob["car"], ob["t0"]) == (original["car"], original["t0"]):
                        ids[original["id"]] = ob["id"]
            arguments = decode(event["args"])
            original_id = arguments.get("ob_id", arguments.get("ob", {}).get("id"))
            if original_id is not None:
                if original_id not in ids:
                    warnings.append({"server_id": key[0], "session": key[1], "reason": "unmapped_obligation", "id": original_id})
                    continue  # never forgive/sanction an unrelated obligation
                if "ob_id" in arguments:
                    arguments["ob_id"] = ids[original_id]
                else:
                    arguments["ob"]["id"] = ids[original_id]
            current_args[0] = arguments
            getattr(rc, event["kind"])(**arguments)
    return {"summary": {"inputs": selected, "sessions": len(states), "decisions": len(decisions),
                        "by_kind": dict(sorted(Counter(d["kind"] for d in decisions).items())),
                        "complete": not warnings, "gaps": warnings}, "decisions": decisions}


def sweep_rules(rules, specs):
    paths, choices = [], []
    for spec in specs:
        path, values = spec.split("=", 1)
        if not path.startswith(("neutralization.", "detectors.")):
            path = "neutralization." + path
        paths.append(path.split("."))
        choices.append([json.loads(v) for v in values.split(",")])
    for values in itertools.product(*choices):
        variant, parameters = deepcopy(rules), {}
        for path, value in zip(paths, values):
            target = variant
            for part in path[:-1]:
                target = target.setdefault(part, {})
            target[path[-1]] = value
            parameters[".".join(path)] = value
        yield parameters, variant


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recordings", nargs="+", help="JSONL[.gz] files in order, or rotation directory")
    parser.add_argument("--rules", default="{}", help="JSON object or path to JSON file")
    parser.add_argument("--server-id", type=int)
    parser.add_argument("--sweep", action="append", default=[], help="repeat for a Cartesian parameter grid")
    args = parser.parse_args()
    try:
        def run(rules):
            discarded = []
            result = replay(read_events(args.recordings, discarded), rules, args.server_id)
            result["summary"]["discarded"] = discarded
            if discarded:
                result["summary"]["complete"] = False
            return result

        rules = json.loads(args.rules if args.rules.lstrip().startswith("{") else Path(args.rules).read_text())
        if args.sweep:
            # Re-open the tape for each variant: memory stays bounded by judges + output.
            output = {"sweep": [{"parameters": parameters, **run(variant)}
                                for parameters, variant in sweep_rules(rules, args.sweep)]}
        else:
            output = run(rules)
        print(json.dumps(output, sort_keys=True, allow_nan=False))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(2, f"replay: {exc}\n")


if __name__ == "__main__":
    main()
