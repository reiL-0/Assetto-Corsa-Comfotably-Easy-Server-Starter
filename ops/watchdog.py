#!/usr/bin/env python3
"""Watchdog: tells the league's Discord when the manager, the backup or the disk go wrong (and when they recover).

    python3 watchdog.py [--selftest]        (run by acm-watchdog.timer every 2 minutes, as root, env from /etc/acm.env)

It lives OUTSIDE the manager on purpose: a manager that is down cannot report itself. Stdlib only.

Checks (each is name -> error text or None):
  manager  GET $WATCHDOG_MANAGER/healthz must answer 200            (default http://127.0.0.1:8080)
  backup   $WATCHDOG_BACKUP_STATUS (opr-backup's status.json): last run ok, off-site ok, not older than 36 h
  disk     the partition of $WATCHDOG_DISK_PATH under $WATCHDOG_DISK_PCT % full  (default / and 85)
  extra    $WATCHDOG_URLS = "name=url,name=url": each must answer 200 (e.g. the site, later the CDMX tunnel's far end)

A check must fail FAILS_NEEDED runs in a row before it is announced (no noise from a restart), is repeated every REPEAT_H hours
while it stays broken, and a «recovered» line is sent when it passes again. State: $WATCHDOG_STATE (JSON).
Webhook: $ACM_DISCORD_STATUS_WEBHOOK (the one the manager already uses for «server started/stopped»); empty = print only.
"""
import json
import os
import shutil
import sys
import time
import urllib.request
from pathlib import Path

FAILS_NEEDED = 2
REPEAT_H = 6
BACKUP_MAX_AGE_H = 36


def get(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return None if r.status == 200 else f"HTTP {r.status}"
    except Exception as e:  # noqa: BLE001 - whatever it is, the thing is not answering
        return str(getattr(e, "reason", e))[:120] or type(e).__name__


def check_backup(path, now):
    try:
        s = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return "no se puede leer el estado del respaldo"
    if not s.get("ok"):
        return f"el último respaldo falló: {s.get('error') or 'sin detalle'}"[:200]
    if now - s.get("last_run", 0) > BACKUP_MAX_AGE_H * 3600:
        return f"hace más de {BACKUP_MAX_AGE_H} h que no corre el respaldo"
    return None


def check_disk(path, limit):
    u = shutil.disk_usage(path)
    pct = 100 * u.used / u.total
    return f"disco al {pct:.0f} % ({u.free // 2**30} GB libres)" if pct >= limit else None


def run_checks(env, now):
    out = {"manager": get(env.get("WATCHDOG_MANAGER", "http://127.0.0.1:8080").rstrip("/") + "/healthz"),
           "backup": check_backup(env.get("WATCHDOG_BACKUP_STATUS", "/var/lib/opr-backup/status.json"), now),
           "disk": check_disk(env.get("WATCHDOG_DISK_PATH", "/"), int(env.get("WATCHDOG_DISK_PCT", "85")))}
    for pair in filter(None, (env.get("WATCHDOG_URLS") or "").split(",")):
        name, _, url = pair.partition("=")
        out[name.strip()] = get(url.strip())
    return out


def step(state, results, now):
    """state {name: {fails, since, last}} + this run's results -> (messages, new state). Pure, so the selftest can drive it."""
    msgs, new = [], {}
    for name, err in results.items():
        st = state.get(name, {"fails": 0, "since": None, "last": None})
        if err:
            st["fails"] += 1
            st["since"] = st["since"] or now
            if st["fails"] >= FAILS_NEEDED and (st["last"] is None or now - st["last"] >= REPEAT_H * 3600):
                st["last"] = now
                msgs.append(f"⚠️ **{name}**: {err}" + (f" (desde hace {int((now - st['since']) / 60)} min)" if now - st["since"] >= 120 else ""))
            new[name] = st
        elif st["last"] is not None:
            msgs.append(f"✅ **{name}** se recuperó")
    return msgs, new


def post(webhook, text):
    if not webhook:
        print(text)
        return
    req = urllib.request.Request(webhook, json.dumps({"content": text[:1900]}).encode(),
                                 {"Content-Type": "application/json", "User-Agent": "opr-watchdog"})
    urllib.request.urlopen(req, timeout=10).read()


def main(env=os.environ):
    sp = Path(env.get("WATCHDOG_STATE", "/var/lib/acm-watchdog/state.json"))
    try:
        state = json.loads(sp.read_text())
    except (OSError, ValueError):
        state = {}
    now = time.time()
    msgs, new = step(state, run_checks(env, now), now)
    if msgs:
        try:
            post(env.get("ACM_DISCORD_STATUS_WEBHOOK", ""), "\n".join(msgs))
        except Exception as e:  # noqa: BLE001 - a failed post must be retried next run, not forgotten: do not save «alerted»
            print(f"discord: {e}", file=sys.stderr)
            return 1
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(new))
    return 0


def selftest():
    t = 1_000_000.0
    bad, ok = {"manager": "HTTP 502", "disk": None}, {"manager": None, "disk": None}
    m, s = step({}, bad, t)
    assert m == [] and s["manager"]["fails"] == 1, "one blip is not announced"
    m, s = step(s, bad, t + 120)
    assert len(m) == 1 and "manager" in m[0] and "HTTP 502" in m[0], m
    m, s = step(s, bad, t + 240)
    assert m == [], "no repeat inside REPEAT_H"
    m, s = step(s, bad, t + 120 + REPEAT_H * 3600)
    assert len(m) == 1 and "min)" in m[0], "repeats while it stays broken"
    m, s = step(s, ok, t + 9e5)
    assert m == ["✅ **manager** se recuperó"] and s == {}, m
    m, s = step(s, ok, t + 9e5 + 120)
    assert m == [], "a quiet system says nothing"
    m, s = step({}, {"x": "boom"}, t)
    m, s = step(s, {"x": None}, t + 60)
    assert m == [], "recovering before it was ever announced says nothing"
    now = time.time()
    with __import__("tempfile").TemporaryDirectory() as d:
        f = Path(d) / "s.json"
        f.write_text(json.dumps({"ok": True, "last_run": now - 3600}))
        assert check_backup(f, now) is None
        f.write_text(json.dumps({"ok": True, "last_run": now - 40 * 3600}))
        assert "36 h" in check_backup(f, now)
        f.write_text(json.dumps({"ok": False, "error": "rclone"}))
        assert "rclone" in check_backup(f, now) and check_backup(Path(d) / "none", now)
        assert check_disk(d, 101) is None and "disco" in check_disk(d, 0)
    assert get("http://127.0.0.1:1/") is not None
    print("selftest ok")


if __name__ == "__main__":
    selftest() if sys.argv[1:2] == ["--selftest"] else sys.exit(main())
