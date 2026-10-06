import shutil
import subprocess

import pytest
from conftest import ADMIN
from fastapi.testclient import TestClient

from app import edge, servers
from app.main import app

api = TestClient(app, headers=ADMIN)
V = "/api/v1"


class S:
    def __init__(self, base):
        self.base_port = base


def test_only_game_and_public_http_ports_are_forwarded_never_the_local_ones():
    p = edge.game_ports([S(9600), S(9604)], servers._ports)
    assert p["udp"] == [9600, 9604] and p["tcp"] == [9600, 9601, 9604, 9605]      # base (tcp+udp) and base+1 (the manager's public HTTP)
    assert 9602 not in p["tcp"] and 9603 not in p["tcp"]                           # plugin ports are local


def test_the_ruleset_keeps_the_players_ip_drops_the_rest_and_caps_floods():
    r = edge.nft_ruleset({"udp": [9600], "tcp": [9600, 9601]}, public_iface="eth0", wg_iface="wg0", backend_ip="10.8.0.2")
    assert "dnat to 10.8.0.2" in r and "masquerade" not in r and "snat" not in r.lower().replace("dnat", "")   # no source NAT: the real IP arrives
    assert "policy drop" in r and "limit rate over 600/second" in r and "elements = { 9600 }" in r and "elements = { 9600, 9601 }" in r


def test_wireguard_configs_start_the_tunnel_from_the_game_machine_and_route_replies_back():
    b = edge.wg_backend(edge_ip="10.8.0.1", backend_ip="10.8.0.2", edge_endpoint="203.0.113.5:51820")
    assert "Endpoint = 203.0.113.5:51820" in b and "PersistentKeepalive = 25" in b and "Table = off" in b
    assert "ip rule add from 10.8.0.2 table 100" in b and "ListenPort" not in b          # outbound only: nothing listens at home
    e = edge.wg_edge(edge_ip="10.8.0.1", backend_ip="10.8.0.2", listen_port=51820)
    assert "ListenPort = 51820" in e and "AllowedIPs = 10.8.0.2/32" in e and "<EDGE_PRIVATE_KEY>" in e      # keys are placeholders


def test_the_endpoint_renders_from_the_servers_and_validates_its_inputs():
    sid = api.post(f"{V}/servers", json={"name": "Edge"}).json()
    ports = servers._ports(sid["base_port"])
    r = api.get(f"{V}/edge/rules?kind=nft")
    assert r.status_code == 200 and str(ports["udp"]) in r.text and str(ports["http"]) in r.text
    assert api.get(f"{V}/edge/rules?kind=wg-backend&edge_endpoint=198.51.100.7:51820").text.count("198.51.100.7:51820") == 1
    assert api.get(f"{V}/edge/rules?kind=nft&backend_ip=not-an-ip").status_code == 422
    assert api.get(f"{V}/edge/rules?kind=nft&public_iface=eth0;rm").status_code == 422        # no way to inject into the text
    assert api.get(f"{V}/edge/rules?kind=other").status_code == 422


@pytest.mark.skipif(not (shutil.which("nft") and shutil.which("unshare")), reason="needs nft and unshare")
def test_nftables_accepts_the_generated_ruleset(tmp_path):
    f = tmp_path / "opr_edge.nft"
    f.write_text(edge.nft_ruleset({"udp": [9600, 9604], "tcp": [9600, 9601, 9604, 9605]}, public_iface="eth0", wg_iface="wg0", backend_ip="10.8.0.2"))
    r = subprocess.run(["unshare", "-Urn", "nft", "-c", "-f", str(f)], capture_output=True, text=True)    # dry run in a throwaway network namespace
    if "Operation not permitted" in r.stderr or "Cannot" in r.stderr and "namespace" in r.stderr:
        pytest.skip("unprivileged user namespaces are not available here")
    assert r.returncode == 0, r.stderr
