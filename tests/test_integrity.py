import time
from datetime import UTC, datetime

import pytest
from conftest import ADMIN
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import discord, integrity
from app.db import engine
from app.main import app
from app.models import Activity, Server
from test_events import _client_with_role

V = "/api/v1"
api = TestClient(app, headers=ADMIN)


def _content(tag: str):
    """A car and a track on disk, as acServer wants them, plus the system file."""
    root = integrity.root()
    car, track = f"ic{tag}", f"it{tag}"
    (root / "content" / "cars" / car).mkdir(parents=True, exist_ok=True)
    (root / "content" / "cars" / car / "data.acd").write_bytes(b"physics-v1")
    (root / "content" / "tracks" / track / "data").mkdir(parents=True, exist_ok=True)
    (root / "content" / "tracks" / track / "data" / "surfaces.ini").write_text("surfaces")
    (root / "content" / "tracks" / track / "models.ini").write_text("models")
    (root / "system" / "data").mkdir(parents=True, exist_ok=True)
    (root / "system" / "data" / "surfaces.ini").write_text("system")
    return car, track, root


def _server(car, track, mode="warn", extras=False):
    sid = api.post(f"{V}/servers", json={"name": "Integrity", "config": {"SERVER": {"CARS": car, "TRACK": track, "CONFIG_TRACK": ""}}}).json()["id"]
    assert api.put(f"{V}/integrity/servers/{sid}", json={"mode": mode, "extras": extras}).json() == {"mode": mode, "extras": extras}
    return sid


def _status(sid):
    return {r["key"].split(":")[0]: r["status"] for r in api.get(f"{V}/integrity/check", params={"server_id": sid}).json()["results"]}


def test_seal_then_detect_a_changed_missing_or_unsealed_file():
    car, track, root = _content("a")
    sid = _server(car, track)
    assert set(_status(sid).values()) == {"unsealed"}
    assert api.post(f"{V}/integrity/seal", json={"server_id": sid}).json()["sealed"] == sorted(["system", f"track:{track}:", f"car:{car}"])
    assert set(_status(sid).values()) == {"ok"}
    (root / "content" / "cars" / car / "data.acd").write_bytes(b"physics-TAMPERED")
    st = api.get(f"{V}/integrity/check", params={"server_id": sid}).json()
    assert {r["key"]: r["status"] for r in st["results"]}[f"car:{car}"] == "changed" and st["problems"][0].startswith(f"Auto {car}: changed")
    (root / "content" / "tracks" / track / "models.ini").unlink()
    assert _status(sid)["track"] == "missing"
    assert api.post(f"{V}/integrity/seal", json={"server_id": sid}).status_code == 400, "a missing file cannot be sealed"
    (root / "content" / "tracks" / track / "models.ini").write_text("models")
    api.post(f"{V}/integrity/seal", json={"server_id": sid})            # approving the new data.acd is the admin's deliberate act
    assert set(_status(sid).values()) == {"ok"}
    # the hashes are MD5, the ones acServer prints in its log
    import hashlib
    seals = {x["key"]: x for x in api.get(f"{V}/integrity/seals").json()}
    assert seals[f"car:{car}"]["files"][f"content/cars/{car}/data.acd"] == hashlib.md5(b"physics-TAMPERED").hexdigest()


def test_the_gate_by_mode():
    car, track, root = _content("b")
    sid = _server(car, track, mode="require")
    with Session(engine) as s:
        srv = s.get(Server, sid)
        with pytest.raises(HTTPException) as e:
            integrity.gate(s, srv)                                       # nothing sealed yet: «require» refuses
        assert e.value.status_code == 409 and "unsealed" in e.value.detail
    api.post(f"{V}/integrity/seal", json={"server_id": sid})
    with Session(engine) as s:
        integrity.gate(s, s.get(Server, sid))                            # sealed and unchanged: starts
    (root / "content" / "cars" / car / "data.acd").write_bytes(b"x")
    said = []
    import app.integrity as mod
    mod.discord.alert = said.append
    try:
        with Session(engine) as s:
            with pytest.raises(HTTPException):
                integrity.gate(s, s.get(Server, sid))                    # changed: refused
            api.put(f"{V}/integrity/servers/{sid}", json={"mode": "warn", "extras": False})
            integrity.gate(s, s.get(Server, sid))                        # «warn»: starts, and says so
            assert len(said) == 1 and "distinto al sellado" in said[0]
            api.put(f"{V}/integrity/servers/{sid}", json={"mode": "off", "extras": False})
            s.expire_all()
            integrity.gate(s, s.get(Server, sid))
            assert len(said) == 1
    finally:
        mod.discord.alert = discord.alert.__wrapped__ if hasattr(discord.alert, "__wrapped__") else mod.discord.alert


def test_extras_are_sealed_only_when_asked_and_checked_when_the_server_wants_them():
    car, track, root = _content("c")
    (root / "plugins" / "rp").mkdir(parents=True, exist_ok=True)
    (root / "plugins" / "rp" / "a.txt").write_text("v1")
    sid = _server(car, track, mode="require", extras=True)
    api.post(f"{V}/integrity/seal", json={"server_id": sid})
    with Session(engine) as s:
        integrity.gate(s, s.get(Server, sid))                            # no extras sealed: nothing to verify
    assert api.post(f"{V}/integrity/seal", json={"server_id": sid, "extras": ["plugins/rp"]}).status_code == 200
    (root / "plugins" / "rp" / "a.txt").write_text("v2")
    with Session(engine) as s, pytest.raises(HTTPException) as e:
        integrity.gate(s, s.get(Server, sid))
    assert "Extra plugins/rp: changed" in e.value.detail
    api.put(f"{V}/integrity/servers/{sid}", json={"mode": "require", "extras": False})   # plugins left out of this server's check
    with Session(engine) as s:
        s.expire_all()
        integrity.gate(s, s.get(Server, sid))
    for bad in ("../etc", "/etc/passwd", "content/../../x"):
        assert api.post(f"{V}/integrity/seal", json={"server_id": sid, "extras": [bad]}).status_code in (400, 404, 422)
    assert api.delete(f"{V}/integrity/seal/extra:plugins/rp").status_code == 204


def test_checksum_failures_in_the_server_log_are_recorded():
    assert integrity.is_failure("Car checksum  lotus_exos_125 false")
    assert integrity.is_failure("checksum mismatch. Kicked reiL")
    assert integrity.is_failure("ERROR: Car checksum failed")
    for fine in ("Car checksum  lotus_exos_125 true", "Checksums received from  reiL []", "Sending 3 checksum requests",
                 "CHECKSUM: system/data/surfaces.ini=41949b9f7045cad2af3f5eb951d170a9", "NEW PICKUP CONNECTION from  1.2.3.4:5"):
        assert not integrity.is_failure(fine), fine
    sid = _server(*_content("d")[:2])
    integrity.on_log_line(sid, "Car checksum  lotus false")
    with Session(engine) as s:
        rows = [a for a in s.query(Activity).filter(Activity.kind == "checksum_fail", Activity.server_id == sid)]
    assert len(rows) == 1 and "lotus" in rows[0].name
    assert [f["line"] for f in api.get(f"{V}/integrity/failures").json() if f["server_id"] == sid] == ["Car checksum  lotus false"]


def test_only_admins_seal_and_stewards_read():
    car, track, _ = _content("e")
    sid = _server(car, track)
    steward, driver = _client_with_role("steward"), _client_with_role("driver")
    assert steward.get(f"{V}/integrity/check", params={"server_id": sid}).status_code == 200
    assert steward.post(f"{V}/integrity/seal", json={"server_id": sid}).status_code == 403
    assert driver.get(f"{V}/integrity/check", params={"server_id": sid}).status_code == 403
    assert TestClient(app).get(f"{V}/integrity/seals").status_code == 401


def test_installed_content_is_sealed_without_asking(tmp_path):
    import zipfile

    from app import content, integrity
    from app.db import engine
    from sqlmodel import Session

    def pack(name, files):
        z = tmp_path / f"{name}.zip"
        with zipfile.ZipFile(z, "w") as zf:
            for f, text in files.items():
                zf.writestr(f"{name}/{f}", text)
        return z
    content._extract(pack("autocar", {"data.acd": "v1"}), content._cars_dir())
    content._extract(pack("autotrack", {"data/surfaces.ini": "s", "models.ini": "m"}), content._tracks_dir())
    with Session(engine) as sess:
        seals = integrity._seals(sess)
    assert "car:autocar" in seals and "track:autotrack:" in seals
    first = seals["car:autocar"].files
    content._extract(pack("autocar", {"data.acd": "v2"}), content._cars_dir())      # a reinstall is the new reference
    with Session(engine) as sess:
        assert integrity._seals(sess)["car:autocar"].files != first
    (content._cars_dir() / "handmade").mkdir()
    (content._cars_dir() / "handmade" / "data.acd").write_text("h")
    assert integrity.seal_new() >= 1                                                 # boot picks up what arrived by hand
    with Session(engine) as sess:
        assert "car:handmade" in integrity._seals(sess)
