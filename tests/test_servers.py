from conftest import ADMIN
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app, headers=ADMIN)


def test_crud_and_ini_render():
    r = client.post(
        "/api/v1/servers",
        json={
            "name": "GT3 Practice",
            "config": {
                "SERVER": {"NAME": "OPRL GT3", "MAX_CLIENTS": 24, "PICKUP_MODE_ENABLED": True},
                "PRACTICE": {"NAME": "Practice", "TIME": 60},
            },
            "entry_list": [
                {"MODEL": "ks_ferrari_488_gt3", "SKIN": "red"},
                {"MODEL": "ks_porsche_991_gt3_r", "SKIN": "blue"},
            ],
        },
    )
    assert r.status_code == 201, r.text
    s = r.json()
    sid = s["id"]
    assert s["base_port"] >= 9600
    assert s["ports"]["tcp"] == s["base_port"]
    assert s["ports"]["http"] == s["base_port"] + 1

    cfg = client.get(f"/api/v1/servers/{sid}/server_cfg.ini").text
    assert "[SERVER]" in cfg
    assert "NAME=OPRL GT3" in cfg
    assert f"TCP_PORT={s['base_port']}" in cfg
    assert "PICKUP_MODE_ENABLED=1" in cfg  # bool -> 1

    el = client.get(f"/api/v1/servers/{sid}/entry_list.ini").text
    assert "[CAR_0]" in el and "[CAR_1]" in el
    assert "MODEL=ks_ferrari_488_gt3" in el

    # distinct port block for the next server
    other = client.post("/api/v1/servers", json={"name": "B"}).json()
    assert other["base_port"] != s["base_port"]
    assert other["base_port"] % 4 == s["base_port"] % 4

    assert client.delete(f"/api/v1/servers/{sid}").status_code == 204
    assert client.get(f"/api/v1/servers/{sid}").status_code == 404


def test_start_requires_binary():
    sid = client.post("/api/v1/servers", json={"name": "NoBin"}).json()["id"]
    assert client.post(f"/api/v1/servers/{sid}/start").status_code == 400


def test_upload_raw_ini_and_list_results():
    sid = client.post("/api/v1/servers", json={"name": "RawIni"}).json()["id"]

    cfg_text = "[SERVER]\nNAME=Uploaded\nMAX_CLIENTS=10\n\n"
    r = client.put(f"/api/v1/servers/{sid}/server_cfg.ini", content=cfg_text)
    assert r.status_code == 200, r.text
    assert r.json()["config"]["SERVER"]["NAME"] == "Uploaded"

    el_text = "[CAR_0]\nMODEL=car_a\nSKIN=blue\n\n[CAR_1]\nMODEL=car_b\nSKIN=red\n\n"
    r = client.put(f"/api/v1/servers/{sid}/entry_list.ini", content=el_text)
    assert r.status_code == 200, r.text
    assert r.json()["entry_list"] == [
        {"MODEL": "car_a", "SKIN": "blue"},
        {"MODEL": "car_b", "SKIN": "red"},
    ]

    assert client.get(f"/api/v1/servers/{sid}/results").json() == []
    assert client.get(f"/api/v1/servers/{sid}/results/nope.json").status_code == 404


def test_simultaneous_creates_get_distinct_port_blocks():
    from concurrent.futures import ThreadPoolExecutor
    body = {"name": "race-ports", "config": {}, "entry_list": []}
    with ThreadPoolExecutor(8) as ex:
        rs = list(ex.map(lambda _: client.post("/api/v1/servers", json=body), range(8)))
    assert all(r.status_code == 201 for r in rs)
    ports = [r.json()["ports"]["tcp"] for r in rs]
    assert len(set(ports)) == len(ports)
