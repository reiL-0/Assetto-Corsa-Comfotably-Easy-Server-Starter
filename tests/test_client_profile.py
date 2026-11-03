"""ACM_PROFILE=client (hosting for others): no script of the league's website is ever sent, the chat words of the automatic telemetry token start with HL (HLHELLO, HLCLAIM:)
and Discord gets a neutral user agent. The league's own profile (the default) keeps OPRHELLO, OPRCLAIM: and OPR-AC-Manager."""
import asyncio

import pytest
from pydantic import ValidationError

from app import csp, discord
from app.config import settings
from app.live import acsp


def test_the_league_profile_is_what_it_always_was():
    assert settings.profile == "opr"
    assert (settings.claim_word("HELLO"), settings.claim_word("CLAIM:"), settings.claim_word("PROBE")) == ("OPRHELLO", "OPRCLAIM:", "OPRPROBE")
    assert settings.agent() == "OPR-AC-Manager"
    assert "oppenpaddockracing.site" in settings.csp_hub_url   # (the league's scripts are the league's own business)


def test_the_client_profile_sends_no_script_of_the_league_and_speaks_with_its_own_words():
    c = type(settings)(profile="client", csp_hub=True, csp_trainer_servers="*", csp_claim_servers="*", csp_probe_servers="1")   # whatever was asked, the profile wins
    assert (c.csp_hub, c.csp_trainer_servers, c.csp_probe_servers) == (False, "", "")   # (the claim exchange is answered only with ACM_CLAIM_SECRET: its own switch)
    assert (c.claim_word("HELLO"), c.claim_word("CLAIM:"), c.claim_word("PROBE")) == ("HLHELLO", "HLCLAIM:", "HLPROBE")
    assert c.agent() == "HotLapHosting-Manager"
    mine = type(settings)(profile="client", claim_prefix="GR", user_agent="Grid-Manager")
    assert (mine.claim_word("HELLO"), mine.agent()) == ("GRHELLO", "Grid-Manager")
    with pytest.raises(ValidationError, match="ACM_PROFILE=client"):
        type(settings)(claim_prefix="XY")   # the league's script says OPRHELLO: another word would never be answered, and nothing would say why
    assert type(settings)(claim_prefix="OPR").claim_word("HELLO") == "OPRHELLO"


@pytest.mark.parametrize("bad", ["hl", "H", "ABCDEFGHI", "HL1", "H L", "HL:"])
def test_the_chat_prefix_is_two_to_eight_capital_letters(bad):
    with pytest.raises(ValidationError):
        type(settings)(claim_prefix=bad)


def test_no_server_of_a_client_gets_a_script_of_the_league(monkeypatch):
    """What csp.extra_for puts in a server's options. The test suite switches the hub off for everyone (tests/conftest.py), so the control comes first: with the league's
    profile and its scripts on, the three addresses of the league's website ARE there; with the client profile none is."""
    monkeypatch.setattr(settings, "profile", "opr")
    for k, v in (("csp_hub", True), ("csp_trainer_servers", "*"), ("csp_probe_servers", "1")):
        monkeypatch.setattr(settings, k, v)
    league = csp.extra_for(1, "", "", None)
    assert all(name in league for name in ("opr_hub.lua", "opr_trainer.lua", "opr_rc_probe.lua")), league
    client = type(settings)(profile="client")
    for k in ("profile", "csp_hub", "csp_probe_servers", "csp_trainer_servers", "csp_claim_servers"):
        monkeypatch.setattr(settings, k, getattr(client, k))
    assert csp.extra_for(1, "", "", None) == "", "a client's server is sent a script of the league (its drivers would load it from, and report to, the league's website)"
    assert csp.extra_for(1, '[SCRIPT_1]\nSCRIPT = "https://example.test/own.lua"\n', "", None).count("SCRIPT") == 2   # what the client's admin put there stays


def test_the_token_exchange_speaks_with_the_clients_words_and_ignores_the_leagues(monkeypatch):
    posted, sent = [], []
    monkeypatch.setattr(acsp, "_claim_post", lambda guid, code: posted.append((guid, code)) or True)
    monkeypatch.setattr(settings, "profile", "client")
    monkeypatch.setattr(settings, "csp_claim_servers", "2")
    monkeypatch.setattr(settings, "claim_secret", "s3cret")
    client = acsp.ACSPClient(2)
    client.send = sent.append

    def say(text, car_id=3):
        async def go():
            client.cars[car_id] = {"car_id": car_id, "driver_guid": "76561198000000001"}
            client.datagram_received(bytes([acsp.CHAT, car_id]) + acsp._write_string(text), ("127.0.0.1", 0))
            await asyncio.gather(*list(client._claim_tasks))
        asyncio.run(go())

    say("OPRHELLO")
    assert posted == [] and sent == [], "a client's server answered the league's word"
    say("\t\t\t\t$HLHELLO")
    assert len(posted) == 1 and len(sent) == 1
    code = posted[0][1]
    assert ("HLCLAIM:" + code).encode("utf-32-le") in sent[0], "the private answer carries the client's word"
    assert b"O\x00\x00\x00P\x00\x00\x00R\x00\x00\x00" not in sent[0], "nothing of the league in what a driver is sent"


def test_discord_is_told_a_neutral_agent(monkeypatch):
    seen = []

    class R:
        def read(self):
            return b""

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(discord.urllib.request, "urlopen", lambda req, timeout=8: seen.append(req) or R())
    monkeypatch.setattr(settings, "discord_status_webhook", "https://discord.test/hook")
    discord._send("hola")
    discord._api("GET", "/users/@me")
    assert [r.get_header("User-agent") for r in seen] == ["OPR-AC-Manager", "OPR-AC-Manager"]
    monkeypatch.setattr(settings, "profile", "client")
    seen.clear()
    discord._send("hola")
    discord._api("GET", "/users/@me")
    assert [r.get_header("User-agent") for r in seen] == ["HotLapHosting-Manager", "HotLapHosting-Manager"]


def test_a_clients_trainer_comes_only_from_its_own_site(monkeypatch):
    """Phase 2: hl_trainer.lua of the client's own website is sent (to every server, switchable per server like the hub); the league's address, or anybody else's, never is."""
    own = "https://grid.example.test/static/csp/hl_trainer.lua"
    c = type(settings)(profile="client", csp_hub=True, csp_hub_url="https://grid.example.test/static/csp/hl_hub.lua", csp_trainer_url=own, csp_probe_servers="1")
    assert c.csp_trainer_servers == "*" and c.csp_probe_servers == ""
    for k in ("profile", "csp_hub", "csp_hub_url", "csp_trainer_url", "csp_trainer_servers", "csp_probe_servers", "csp_hub_required", "csp_min_build"):
        monkeypatch.setattr(settings, k, getattr(c, k))
    out = csp.extra_for(1, "", "", None)
    assert own in out and "hl_hub.lua" in out and "oppenpaddock" not in out and "probe" not in out, out
    assert own not in csp.extra_for(1, "", "trainer", None), "the checkbox of the server does not switch the trainer off"
    for bad in ("https://www.oppenpaddockracing.site/static/csp/opr_trainer.lua", "https://evil.test/static/csp/opr_trainer.lua", "https://grid.example.test/x/hl_trainer.lua", "http://grid.example.test/static/csp/hl_trainer.lua"):
        assert type(settings)(profile="client", csp_trainer_url=bad).csp_trainer_servers == "", bad
