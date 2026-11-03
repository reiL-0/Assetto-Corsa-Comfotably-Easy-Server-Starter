"""CSP is mandatory on a hosted client's servers (the owner's decision): the neutral hub is sent from the CLIENT's own site, marked REQUIRED (CSP closes the game of a driver who cannot load it), and the
server's TRACK carries the minimum CSP build (`csp/<build>/../<track>`); the manager keeps the clean track name everywhere it comes back from the acServer (session info, result files, info.json).
The league's own manager (profile opr) is exactly as it was: no REQUIRED, no minimum, its own URL."""
import json
import struct

import pytest

from app import csp
from app.config import settings
from app.live import acsp
from app.results import parse_result_file
from app.services import ini_generator as ini

OWN_HUB = "https://gridracing.example.test/static/csp/hl_hub.lua"


def client_settings(**kw):
    return type(settings)(profile="client", csp_hub=True, csp_hub_url=OWN_HUB, **kw)


def use(monkeypatch, s):
    for k in ("profile", "csp_hub", "csp_hub_url", "csp_hub_required", "csp_min_build", "csp_hub_version", "csp_trainer_servers", "csp_trainer_url", "csp_probe_servers", "csp_claim_servers"):
        monkeypatch.setattr(settings, k, getattr(s, k))


def test_the_client_profile_defaults_to_required_and_build_3898_with_its_own_hub():
    c = client_settings()
    assert c.csp_hub is True and c.csp_hub_required is True and c.csp_min_build == 3898 and c.csp_hub_url == OWN_HUB
    assert (c.csp_trainer_servers, c.csp_probe_servers) == ("", ""), "the league's trainer or probe was switched on (the trainer needs the client's own address: tests/test_client_profile.py)"
    assert client_settings(csp_min_build=0).csp_min_build == 0 and client_settings(csp_min_build=4300).csp_min_build == 4300 and client_settings(csp_hub_required=False).csp_hub_required is False


@pytest.mark.parametrize("url", [
    "https://www.oppenpaddockracing.site/static/csp/opr_hub.lua", "https://WWW.OPPENPADDOCKRACING.SITE/static/csp/hl_hub.lua", "https://oppenpaddockracing.site/static/csp/hl_hub.lua",
    "http://gridracing.example.test/static/csp/hl_hub.lua", "https://gridracing.example.test/static/csp/opr_hub.lua", "https://gridracing.example.test/hl_hub.lua", "https://gridracing.example.test/static/csp/hl_hub.lua?x=1",
    "https://gridracing.example.test/static/csp/hl_hub.lua/../opr_hub.lua", "https://x y.test/static/csp/hl_hub.lua", "//gridracing.example.test/static/csp/hl_hub.lua", ""])
def test_a_client_never_gets_a_hub_that_is_not_its_own_script(url):
    c = type(settings)(profile="client", csp_hub=True, csp_hub_url=url) if url else type(settings)(profile="client", csp_hub=True)
    assert c.csp_hub is False, ("a client's drivers would load", url)


def test_the_league_is_exactly_as_it_was():
    league = type(settings)(csp_hub=True)   # (the test suite switches the hub off for everyone: tests/conftest.py)
    assert league.csp_hub is True and league.csp_hub_required is False and league.csp_min_build == 0 and "oppenpaddockracing" in league.csp_hub_url


def test_the_hub_goes_out_required_with_the_servers_id(monkeypatch):
    use(monkeypatch, client_settings(csp_hub_version="7"))
    extra = csp.extra_for(2, "", "", None)
    assert f'SCRIPT = "{OWN_HUB}?v=7"' in extra and "SERVER = 2" in extra and "REQUIRED = 1" in extra, extra
    assert "oppenpaddockracing" not in extra and extra.count("[SCRIPT_") == 1, "something else was sent"
    assert csp.extra_for(2, "", "hub", None) == "", "a hub switched off for one server still went out"


def test_the_league_hub_is_not_required(monkeypatch):
    use(monkeypatch, type(settings)(profile="opr", csp_hub=True))
    extra = csp.extra_for(2, "", "", None)
    assert "REQUIRED" not in extra and "oppenpaddockracing.site/static/csp/opr_hub.lua" in extra


def test_the_track_carries_the_minimum_build_only_where_the_hub_is_sent(monkeypatch):
    use(monkeypatch, client_settings())
    assert csp.min_build_for("") == 3898 and csp.min_build_for("hub") == 0, "a server with no hub was made CSP-only"
    p = ini.ports(9600, 9600, 9700)
    cfg = {"SERVER": {"NAME": "x", "TRACK": "spa", "CONFIG_TRACK": "gp"}}
    text = ini.server_cfg(cfg, p, has_welcome=False, csp_min_build=3898)
    assert "TRACK=csp/3898/../spa" in text and "CONFIG_TRACK=gp" in text, text
    assert cfg["SERVER"]["TRACK"] == "spa", "the stored configuration was changed: only the written file may carry the prefix"
    again = ini.server_cfg({"SERVER": {"TRACK": "csp/3898/../spa"}}, p, has_welcome=False, csp_min_build=3898)
    assert again.count("csp/") == 1, "the prefix was added twice"
    plain = ini.server_cfg(cfg, p, has_welcome=False)
    assert "TRACK=spa" in plain and "csp/" not in plain, "the league's file changed"


@pytest.mark.parametrize("raw,clean", [("csp/3898/../spa", "spa"), ("csp/3898/../B/../ks_monza", "ks_monza"), ("spa", "spa"), ("", ""), (None, None), ("ks_nordschleife", "ks_nordschleife"), ("csp/12/../x_y-z", "x_y-z"),
                                       ("csp/pista", "csp/pista"), ("csp/3898/../grupo/pista", "csp/3898/../grupo/pista"), ("csp/x/../spa", "csp/x/../spa"), ("csp//../spa", "csp//../spa")])
def test_the_clean_track_name(raw, clean):
    assert csp.clean_track(raw) == clean


def session_info(track):
    def s(text):
        b = text.encode()
        return bytes([len(b)]) + b
    buf = struct.pack("<4B", 4, 0, 0, 3) + acsp._write_string("Server") + s(track) + s("") + s("Race") + bytes([3]) + struct.pack("<3H", 20, 0, 60) + bytes([24, 30]) + s("sunny") + struct.pack("<i", 0)
    return buf


def test_what_the_acserver_reports_is_cleaned_wherever_it_comes_back(tmp_path):
    info, _ = acsp._read_session_info(session_info("csp/3898/../spa"), 0)
    assert info["track"] == "spa", "the live board would look for a track that does not exist"
    assert acsp._read_session_info(session_info("spa"), 0)[0]["track"] == "spa"
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"Type": "Race", "TrackName": "csp/3898/../spa", "TrackConfig": "gp", "Name": "Race", "Result": [], "Laps": []}))
    assert parse_result_file(p)["track"] == "spa", "the result would be filed under a track that does not exist"


def test_both_places_that_write_the_servers_file_carry_the_minimum_build(monkeypatch):
    from conftest import ADMIN
    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from app import servers
    from app.db import engine
    from app.main import app
    from app.models import Server
    from app.services import server_service
    use(monkeypatch, client_settings())
    sid = TestClient(app, headers=ADMIN).post("/api/v1/servers", json={"name": "CspServer"}).json()["id"]
    with Session(engine) as s:
        srv = s.get(Server, sid)
        srv.config = {**srv.config, "SERVER": {**srv.config.get("SERVER", {}), "TRACK": "spa", "CONFIG_TRACK": "gp"}}
        shown = servers.render_server_cfg(srv)                 # what the panel shows
        written = (server_service.write_instance(srv) / "cfg" / "server_cfg.ini").read_text()   # what acServer reads
        no_hub = srv
        no_hub.csp_off = "hub"
        plain = servers.render_server_cfg(no_hub)
    assert "TRACK=csp/3898/../spa" in shown and "TRACK=csp/3898/../spa" in written, (shown, written)
    assert "TRACK=spa" in plain and "csp/" not in plain, "a server with no hub was made CSP-only"
