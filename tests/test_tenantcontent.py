import io
import os
import time
import zipfile

from conftest import ADMIN
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import catalog, servers, tenantcontent
from app.config import settings
from app.db import engine
from app.main import app
from app.models import Plan

root = TestClient(app, headers=ADMIN)
V = "/api/v1"


def _tenant(name: str, **plan) -> TestClient:
    p = root.post(f"{V}/plans", json={"name": f"p-{name}", "max_servers": 3, "slots": 8, **plan}).json()
    t = root.post(f"{V}/tenants", json={"name": name, "plan_id": p["id"]}).json()
    root.post(f"{V}/tenants/{t['id']}/users", json={"username": f"u-{name}", "password": "password-123", "role": "admin"})
    c = TestClient(app)
    c.post(f"{V}/auth/login", json={"username": f"u-{name}", "password": "password-123"})
    c.tenant_id, c.plan_id = t["id"], p["id"]
    return c


def _zip(name: str, data: bytes = b"physics") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{name}/data.acd", data)
        zf.writestr(f"{name}/ui/ui_car.json", f'{{"name": "{name}"}}')
        zf.writestr(f"{name}/{name}.kn5", b"M" * 3000)
    return buf.getvalue()


def _up(c: TestClient, name: str, data: bytes = b"physics", kind: str = "car"):
    return c.post(f"{V}/tenant/content/{kind}", files={"file": (f"{name}.zip", _zip(name, data), "application/zip")})


def test_a_customer_upload_is_cut_to_the_server_pack_and_kept_once_in_the_blob_store():
    a = _tenant("tc-a")
    r = _up(a, "tc_car_one")
    assert r.status_code == 201, r.text
    item = r.json()
    d = catalog.blob_path(item["hash"])
    assert (d / "data.acd").is_file() and not (d / "tc_car_one.kn5").exists() and item["files"] == 2     # pack: no 3D model
    mine = a.get(f"{V}/tenant/content").json()
    assert [i["name"] for i in mine["items"]] == ["tc_car_one"] and mine["used_bytes"] == item["size"] and mine["quota_bytes"] is None
    assert root.get(f"{V}/tenant/content").status_code == 400                        # not a customer account


def test_the_same_content_from_another_customer_is_deduplicated_and_removal_is_per_holder():
    a, b = _tenant("tc-dd-a"), _tenant("tc-dd-b")
    ha = _up(a, "tc_shared").json()["hash"]
    hb = _up(b, "tc_shared").json()["hash"]
    assert ha == hb and catalog.blob_path(ha).is_dir()                                # one copy for both
    same_bytes_other_folder = io.BytesIO()                                              # identical files under another folder name
    with zipfile.ZipFile(same_bytes_other_folder, "w") as zf:
        zf.writestr("tc_renamed/data.acd", b"physics")
        zf.writestr("tc_renamed/ui/ui_car.json", '{"name": "tc_shared"}')
    r = _tenant("tc-dd-c").post(f"{V}/tenant/content/car", files={"file": ("r.zip", same_bytes_other_folder.getvalue(), "application/zip")})
    assert r.status_code == 409 and "already known as" in r.text and "tc_shared" in r.text
    assert a.delete(f"{V}/tenant/content/{ha}").status_code == 204
    assert catalog.blob_path(ha).is_dir() and [i["name"] for i in b.get(f"{V}/tenant/content").json()["items"]] == ["tc_shared"]   # B still holds it
    assert a.get(f"{V}/tenant/content").json()["items"] == []
    assert b.delete(f"{V}/tenant/content/{ha}").status_code == 204 and not catalog.blob_path(ha).exists()     # last holder: deleted from disk
    assert a.delete(f"{V}/tenant/content/{ha}").status_code == 404


def test_the_storage_quota_of_the_plan_is_enforced():
    a = _tenant("tc-quota")
    with Session(engine) as s:
        p = s.get(Plan, a.plan_id)
        p.disk_mb = 1
        s.add(p)
        s.commit()
    big = os.urandom(1_500_000)                                                         # does not compress: over 1 MB once packed
    r = _up(a, "tc_big", big)
    assert r.status_code == 413 and "quota" in r.text
    assert _up(a, "tc_small", b"tiny").status_code == 201


def test_a_server_sees_only_what_its_customer_holds():
    a, b = _tenant("tc-iso-a"), _tenant("tc-iso-b")
    _up(a, "tc_a_only")
    _up(b, "tc_b_only")
    sa = a.post(f"{V}/servers", json={"name": "SA"}).json()["id"]
    sb = b.post(f"{V}/servers", json={"name": "SB"}).json()["id"]
    with Session(engine) as sess:
        inst_a = servers._write_instance(sess.get(servers.Server, sa))
        inst_b = servers._write_instance(sess.get(servers.Server, sb))
    assert sorted(p.name for p in (inst_a / "content" / "cars").iterdir()) == ["tc_a_only"]
    assert sorted(p.name for p in (inst_b / "content" / "cars").iterdir()) == ["tc_b_only"]
    assert (inst_a / "content" / "cars" / "tc_a_only").is_symlink() and (inst_a / "content" / "cars" / "tc_a_only" / "data.acd").is_file()
    assert not (inst_a / "content" / "cars" / "tc_b_only").exists()


def test_a_server_that_asks_for_content_its_customer_does_not_hold_does_not_start(monkeypatch):
    monkeypatch.setattr(settings, "acserver_cmd", "/bin/true")
    a = _tenant("tc-miss")
    _up(a, "tc_have")
    sid = a.post(f"{V}/servers", json={"name": "M", "config": {"SERVER": {"CARS": "tc_have;tc_not_mine", "TRACK": "imola"}}, "entry_list": []}).json()["id"]
    r = a.post(f"{V}/servers/{sid}/start")
    assert r.status_code == 409 and "tc_not_mine" in r.text and "imola" in r.text and "tc_have" not in r.text


def test_chunked_upload_and_nobody_elses_upload_id():
    a, b = _tenant("tc-chunk-a"), _tenant("tc-chunk-b")
    uid = a.post(f"{V}/tenant/content/uploads/start", json={"kind": "car"}).json()["id"]
    data, off = _zip("tc_chunked"), 0
    for i in range(0, len(data), 700):
        part = data[i:i + 700]
        assert a.put(f"{V}/tenant/content/uploads/{uid}?offset={off}", content=part).status_code == 200
        off += len(part)
    assert b.get(f"{V}/tenant/content/uploads/{uid}").status_code == 404
    assert b.put(f"{V}/tenant/content/uploads/{uid}?offset={off}", content=b"x").status_code == 404
    assert a.post(f"{V}/tenant/content/uploads/{uid}/complete").status_code == 202
    for _ in range(100):
        st = a.get(f"{V}/tenant/content/uploads/{uid}").json()
        if st["state"] in ("done", "error"):
            break
        time.sleep(0.1)
    assert st["state"] == "done" and st["result"]["name"] == "tc_chunked", st
