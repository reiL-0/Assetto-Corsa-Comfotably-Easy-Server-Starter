import io
import shutil
import zipfile

from conftest import ADMIN
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import catalog, content
from app.db import engine
from app.main import app
from app.models import ContentHolder

api = TestClient(app, headers=ADMIN)
V = "/api/v1"


def _zip(name: str, data: bytes = b"physics-v1") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{name}/data.acd", data)
        zf.writestr(f"{name}/ui/ui_car.json", f'{{"name": "{name}"}}')
        zf.writestr(f"{name}/{name}.kn5", b"M" * 2000)
    return buf.getvalue()


def _up(name: str, data: bytes = b"physics-v1", pack: bool = True):
    return api.post(f"{V}/content/cars?pack={'true' if pack else 'false'}", files={"file": (f"{name}.zip", _zip(name, data), "application/zip")})


def _item(name: str) -> dict:
    return next(i for i in api.get(f"{V}/catalog").json() if i["name"] == name)


def test_digest_depends_on_content_not_on_order(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for d, order in ((a, ("x", "y")), (b, ("y", "x"))):
        d.mkdir()
        for n in order:
            (d / n).write_bytes(n.encode() * 10)
    assert catalog.digest_dir(a)[0] == catalog.digest_dir(b)[0]
    (b / "x").write_bytes(b"changed")
    assert catalog.digest_dir(a)[0] != catalog.digest_dir(b)[0]


def test_an_upload_is_cataloged_once_and_a_new_version_supersedes_the_old_holder():
    assert _up("cat_one").status_code == 201
    assert _up("cat_one").status_code == 201                                  # the same content again: still one blob and one holder
    it = _item("cat_one")
    assert len(it["holders"]) == 1 and it["holders"][0]["status"] == "active" and it["holders"][0]["holder"] == "league" and it["files"] == 2   # pack: no kn5
    assert _up("cat_one", b"physics-v2").status_code == 201                   # a newer version under the same name
    holders = [h["status"] for i in api.get(f"{V}/catalog").json() if i["name"] == "cat_one" for h in i["holders"]]
    assert sorted(holders) == ["active", "superseded"]


def test_a_claim_against_one_holder_keeps_the_files_for_the_others():
    _up("cat_two")
    it = _item("cat_two")
    car_dir = content._cars_dir() / "cat_two"
    # holder B proves it has the same content without uploading it: it answers a challenge from ITS copy
    other = car_dir.parent.parent / "b_copy" / "cat_two"
    shutil.copytree(car_dir, other)
    ch = api.post(f"{V}/catalog/challenge", json={"hash": it["hash"]}).json()
    ans = catalog.answer_for(other, ch["nonce"], ch["ranges"])
    assert api.post(f"{V}/catalog/prove", json={"nonce": ch["nonce"], "answer": ans, "holder": "tenant_b", "license_attested": True}).status_code == 201
    holders = {h["holder"]: h for h in _item("cat_two")["holders"]}
    assert set(holders) == {"league", "tenant_b"} and holders["tenant_b"]["status"] == "active"

    r = api.post(f"{V}/catalog/holders/{holders['league']['id']}/revoke", json={"note": "claim: ripped"}).json()
    assert r["status"] == "revoked" and r["purged"] is False and car_dir.is_dir()      # B still holds it: the folder stays
    r = api.post(f"{V}/catalog/holders/{holders['tenant_b']['id']}/revoke", json={}).json()
    assert r["purged"] is True and not car_dir.exists()                                  # nobody active left: deleted from disk
    assert api.post(f"{V}/catalog/holders/{holders['league']['id']}/restore", json={"note": "licence shown"}).json()["status"] == "active"
    assert "revoke" in [e["action"] for e in api.get(f"{V}/catalog/events").json()]      # everything is logged


def test_a_blocked_file_is_removed_for_everybody_and_refused_when_uploaded_again():
    _up("cat_three")
    h = _item("cat_three")["hash"]
    car_dir = content._cars_dir() / "cat_three"
    assert api.post(f"{V}/catalog/{h}/block", json={"reason": "claim against the file"}).json() == {"blocked": True, "purged": True}
    assert not car_dir.exists() and _item("cat_three")["holders"][0]["status"] == "disputed"
    r = _up("cat_three")
    assert r.status_code == 403 and "rights claim" in r.text
    holder_id = _item("cat_three")["holders"][0]["id"]
    assert api.post(f"{V}/catalog/holders/{holder_id}/restore", json={}).status_code == 409   # blocked: unblock first
    assert api.delete(f"{V}/catalog/{h}/block").status_code == 204
    assert _up("cat_three").status_code == 403                                              # the holder is still «disputed» until restored
    assert api.post(f"{V}/catalog/holders/{holder_id}/restore", json={}).json()["status"] == "active"


def test_wrong_or_reused_proofs_and_bad_inputs_are_refused():
    _up("cat_four")
    h = _item("cat_four")["hash"]
    ch = api.post(f"{V}/catalog/challenge", json={"hash": h}).json()
    base = {"nonce": ch["nonce"], "holder": "x", "license_attested": True}
    assert api.post(f"{V}/catalog/prove", json={**base, "answer": "0" * 64}).status_code == 403
    assert api.post(f"{V}/catalog/prove", json={**base, "answer": "0" * 64}).status_code == 410            # a challenge is used up by one try
    ch = api.post(f"{V}/catalog/challenge", json={"hash": h}).json()
    ans = catalog.answer_for(content._cars_dir() / "cat_four", ch["nonce"], ch["ranges"])
    assert api.post(f"{V}/catalog/prove", json={"nonce": ch["nonce"], "answer": ans, "holder": "x", "license_attested": False}).status_code == 400
    assert api.post(f"{V}/catalog/challenge", json={"hash": "f" * 64}).status_code == 404
    assert api.put(f"{V}/catalog/{h}/source", json={"url": "javascript:alert(1)"}).status_code == 422
    assert api.put(f"{V}/catalog/{h}/source", json={"url": "https://www.overtake.gg/downloads/x.1/"}).json()["source_url"].startswith("https://")


def test_scan_registers_content_that_was_installed_before_the_catalog():
    d = content._cars_dir() / "cat_old"
    (d / "ui").mkdir(parents=True)
    (d / "data.acd").write_bytes(b"old")
    (d / "ui" / "ui_car.json").write_text('{"name": "old"}')
    r = api.post(f"{V}/catalog/scan?limit=500").json()
    assert r["remaining"] == 0 and _item("cat_old")["holders"][0]["holder"] == "league"
    with Session(engine) as s:
        assert s.exec(select(ContentHolder).where(ContentHolder.holder == "league")).first()


def test_the_official_pages_of_a_sessions_content_go_to_discord_and_the_event_pages():
    from app import announcement
    _up("dl_car")
    h = _item("dl_car")["hash"]
    assert api.put(f"{V}/catalog/{h}/source", json={"url": "https://www.overtake.gg/downloads/dl-car.1/"}).status_code == 200
    _up("dl_nolink")
    sess = {"track": "dl_track", "cars": ["dl_car", "dl_nolink"]}
    assert api.get(f"{V}/catalog/downloads?track=dl_track&cars=dl_car,dl_nolink").json() == [{"kind": "car", "name": "dl_car", "url": "https://www.overtake.gg/downloads/dl-car.1/"}]
    v = announcement.variables("T", "S", sess, 1.0, {"yes": 0, "maybe": 0, "no": 0})
    assert "⬇️ **DESCARGAS**" in v["descargas"] and "<https://www.overtake.gg/downloads/dl-car.1/>" in v["descargas"] and "dl_nolink" not in v["descargas"]
    assert announcement.variables("T", "S", {"track": "x", "cars": ["nothing"]}, 1.0, {"yes": 0, "maybe": 0, "no": 0})["descargas"] == ""
    assert "{descargas}" in announcement.DEFAULT["content"]
