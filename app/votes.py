"""Who started a vote (kick / restart / next session), read from acServer's log and told to the server's chat and to the stewards.

No penalty: the votes themselves cannot pass (server_service.BLOCK_VOTES). acServer v1.15 does not tell the ACSP plugin about a vote
(main.SetVote / NewVote / StepVote call no udp function), so the log line `Creating vote to ...` (main.SetVote, stdout) is the only source.
A driver is printed as «name [team]» (main.Driver.String), so the name is whatever comes before the last «[...]»."""
import logging
import re
import threading
import time

from sqlmodel import Session

from app import discord, metrics
from app.db import engine
from app.live import acsp
from app.models import Server

log = logging.getLogger("acmanager.votes")

SPACING = 10   # s between two notices for the same voter on the same server: with a 1 s vote (BLOCK_VOTES) one driver could open a vote a second, and the notice would be the spam
_told: dict[tuple, float] = {}

_WHO = r"(.+?) \[[^\]]*\]"
_SESSION = re.compile(rf"^Creating vote to (NEXT|RESTART) SESSION from {_WHO}$")
_KICK = re.compile(rf"^Creating vote to KICK USER {_WHO} from {_WHO}$")   # target, then voter: the order of the arguments of main.SetVote's Println (read from the binary, not yet seen in a real log)
_ANY = re.compile(r"^Creating vote\b")


def parse(line: str) -> dict | None:
    """{kind: next | restart | kick | unknown, voter, target} for a log line that opens a vote; None for any other line.
    A line that opens a vote but does not fit (a format that changed, `Creating vote for UNKNOWN from ...`) is `unknown` with the raw text: said, never dropped."""
    line = line.rstrip()
    if m := _SESSION.match(line):
        return {"kind": m[1].lower(), "voter": m[2], "target": None}
    if m := _KICK.match(line):
        return {"kind": "kick", "voter": m[2], "target": m[1]}
    if _ANY.match(line):
        return {"kind": "unknown", "voter": None, "target": None, "raw": line}
    return None


def _clean(text: str | None, limit: int = 40) -> str | None:
    """A name is typed by the driver (Steam): printable characters only, 40 at most."""
    return None if text is None else "".join(c for c in text if c.isprintable())[:limit]


def _md(text: str) -> str:
    """A name inside a Discord message: its markdown escaped and its `@` broken so it cannot ping a role or the whole channel (in the game's chat an `@` is harmless, and acsp encodes the zero-width space as «?»)."""
    return re.sub(r"([\\*_~`|>])", r"\\\1", text).replace("@", "@\u200b")


def _what(v: dict) -> str:
    return {"next": "pasar a la siguiente sesión", "restart": "reiniciar la sesión",
            "kick": f"expulsar a {v['target']}"}.get(v["kind"], "una votación que no se pudo leer")


def _identity(inst, name: str | None) -> str:
    """` (slot n, Steam id)` of the connected driver with that name, only when exactly one has it: two with the same name are not told apart by a name."""
    same = [d for d in inst.logboard.board.drivers if d.connected and _clean(d.name) == name] if name else []
    return f" (slot {same[0].car_id}{', Steam ' + same[0].guid if same[0].guid else ''})" if len(same) == 1 else ""


def _tell_stewards(server_id: int, text: str) -> None:
    """In its own thread: the server's name is a database read and the post is network, and none of it may hold the event loop that reads every server's log."""
    try:
        with Session(engine) as sess:
            srv = sess.get(Server, server_id)
        discord.alert(f"🗳️ **{_md(_clean(srv.name) if srv else f'Servidor #{server_id}')}**: {text}")
    except Exception:
        log.exception("vote notice to the stewards failed for server %s", server_id)


def on_log_line(inst, line: str) -> None:
    """Called for every NEW line of the server log (not for the history read again after the manager restarts: supervisor._tail)."""
    v = parse(line)
    if not v:
        return
    v = {k: x if k == "kind" else _clean(x, 200 if k == "raw" else 40) for k, x in v.items()}
    known = v["kind"] != "unknown"   # (not `v["voter"]`: a name of only unprintable characters is empty once cleaned)
    who = v["voter"] or "alguien"
    chat = f"{who} ha iniciado una votación para {_what(v)}." if known else f"Se ha iniciado una votación ({v['raw'][:80]})."

    def tell_chat():
        if inst.acsp:
            inst.acsp.send(acsp.encode_broadcast_chat(chat))

    def record():
        metrics.enqueue(inst.server_id, f"vote_{v['kind']}", name=(v["voter"] if known else v["raw"])[:300], car=v["target"])   # ponytail: one Activity row per vote (voter in `name`, kick target in `car`); a table of votes when something reads them

    def stewards():
        text = f"{_md(who)}{_identity(inst, v['voter'])} inició una votación para {_md(_what(v))}." if known else f"votación con formato desconocido: {_md(v['raw'])}"
        threading.Thread(target=_tell_stewards, args=(inst.server_id, text), daemon=True).start()

    key, now = (inst.server_id, v["voter"] if known else v["raw"]), time.monotonic()
    if len(_told) > 500:   # names are typed by drivers: the table must not grow with them
        _told.clear()
    spam = key in _told and now - _told[key] < SPACING
    if not spam:
        _told[key] = now
    # the record is kept for every vote; only the notices are spaced. One channel failing must not silence the other two, nor stop the log from being read (it also feeds the live board)
    for step in ((record,) if spam else (tell_chat, record, stewards)):
        try:
            step()
        except Exception:
            log.exception("vote notice (%s) failed for server %s", step.__name__, inst.server_id)
