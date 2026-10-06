from conftest import ADMIN
from fastapi.testclient import TestClient

from app import announcement, requirements, wake
from app.main import app
from app.models import Server

api = TestClient(app, headers=ADMIN)
V = "/api/v1"


def test_requirements_show_in_the_description_and_the_announcement_without_touching_the_track(monkeypatch):
    assert api.get(f"{V}/requirements").json() == {"csp_build": 0, "items": []}
    assert requirements.lines() == []
    bad = [{"csp_build": 3898, "items": [{"name": "x", "url": "ftp://x"}]}, {"csp_build": -1, "items": []},
           {"items": [{"name": "a", "url": "https://x.test/a"}, {"name": "b", "url": "https://x.test/a"}]}]
    assert all(api.put(f"{V}/requirements", json=b).status_code == 422 for b in bad)
    ok = {"csp_build": 3898, "items": [{"name": "Helicorsa v7b", "url": "https://drive.google.com/file/d/1/view"}]}
    assert api.put(f"{V}/requirements", json=ok).json() == ok
    try:
        sid = api.post(f"{V}/servers", json={"name": "Req", "config": {"SERVER": {"TRACK": "spa", "NAME": "Req"}}}).json()["id"]
        assert "TRACK=spa\n" in api.get(f"{V}/servers/{sid}/server_cfg.ini").text                # the Linux acServer cannot take csp/<build>/../<track>
        srv = Server(id=sid, name="Req", base_port=9700, config={"SERVER": {"TRACK": "spa"}}, entry_list=[], welcome="Bienvenidos")
        d = wake.details(srv, None)["description"]
        assert d.startswith("Bienvenidos") and "Custom Shaders Patch build 3898" in d and "Helicorsa v7b: https://drive.google.com/file/d/1/view" in d
        text = announcement.variables("T", "S", {"track": "spa", "cars": ["a"]}, 1.0, {"yes": 0, "maybe": 0, "no": 0})["requisitos"]
        assert text.startswith("📦 **REQUISITOS**") and "Helicorsa v7b" in text
    finally:
        api.put(f"{V}/requirements", json={"csp_build": 0, "items": []})
    assert announcement.variables("T", "S", {"track": "spa", "cars": ["a"]}, 1.0, {"yes": 0, "maybe": 0, "no": 0})["requisitos"] == ""


def test_csp_weather_names_are_accepted():
    opts = {"weather": [{"graphics": "7_heavy_clouds_type=7"}, {"graphics": "3_clear_type=15_time=0_mult=0"}]}
    sid = api.post(f"{V}/servers", json={"name": "Wx"}).json()["id"]
    r = api.post(f"{V}/servers/{sid}/apply", json={"name": "w", "track": "spa", "cars": ["a"], "restart": False, "options": opts})
    assert r.status_code != 422, r.text
