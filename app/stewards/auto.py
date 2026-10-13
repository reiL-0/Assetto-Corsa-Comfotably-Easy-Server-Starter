"""Automatic penalties from a counted Race's result file (no live incidents involved: acServer already writes `Cuts` for every lap).

`cut_seconds` is the rule; `auto_penalties` writes the Penalty rows (kind time, created_by 'auto', auto_key 'cuts') when the league counts the file. One row per
(file, driver, key) -- the unique index of migration 005 -- and a steward's withdrawal leaves the row marked `dismissed`, so it is never created again. Only
cuts are automatic: contacts and track limits stay evidence for the stewards (see README.md).
"""

from __future__ import annotations

from sqlmodel import Session, select

from app import discord
from app.models import Championship, Penalty
from app.results import non_racing_for


def cut_seconds(cuts: int, rules: dict) -> int:
    """Seconds for `cuts` cuts under a league's rules: 0 until `cuts_per_step` is reached, then `step_seconds` per full step, capped at `max_seconds`."""
    return min(rules["max_seconds"], cuts // rules["cuts_per_step"] * rules["step_seconds"])


def auto_penalties(sess: Session, c: Championship, server_id: int, filename: str, parsed: dict) -> list[str]:
    """Add the cut penalties of this Race result under the league's `auto_rules`; returns the Discord messages, one per penalty created. Does not commit nor announce: the caller counts the file in the same transaction and announces once it is saved."""
    if parsed["type"] != "Race" or not c.auto_rules:
        return []
    cuts: dict[str, int] = {}
    for lap in parsed["laps"]:
        if lap["driver_guid"] and (lap["lap_time_ms"] or 0) > 0:   # a lap with no time is an unfinished one
            cuts[lap["driver_guid"]] = cuts.get(lap["driver_guid"], 0) + (lap["cuts"] or 0)
    racing = {e["driver_guid"] for e in parsed["classification"]} - non_racing_for(sess, server_id, filename)
    messages: list[str] = []
    for guid, n in cuts.items():
        seconds = cut_seconds(n, c.auto_rules)
        if not seconds or guid not in racing:
            continue
        if sess.exec(select(Penalty.id).where(Penalty.server_id == server_id, Penalty.filename == filename, Penalty.driver_guid == guid, Penalty.auto_key == "cuts")).first():
            continue   # already made (or made and withdrawn)
        reason = f"Cortes de pista: {n} (automática)"
        sess.add(Penalty(server_id=server_id, filename=filename, driver_guid=guid, kind="time", value=seconds * 1000, reason=reason, created_by="auto", auto_key="cuts"))
        name = next((e["driver_name"] for e in parsed["classification"] if e["driver_guid"] == guid), guid)
        messages.append(discord.penalty_message(c.name, parsed, name, "time", seconds, reason))
    return messages
