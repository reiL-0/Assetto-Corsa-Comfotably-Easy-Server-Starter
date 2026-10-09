"""app/services/ini_generator is pure: same text as the old render_* in servers.py, with explicit inputs."""
from app.services import ini_generator as ini


def test_ports_block_and_internal_http():
    assert ini.ports(9600, 9600, 9700) == {"tcp": 9600, "udp": 9600, "http": 9601, "plugin": 9602, "plugin_local": 9603, "http_internal": 9700}
    assert ini.ports(9604, 9600, 9700)["http_internal"] == 9701      # one internal port per 4-port block


def test_server_cfg_merges_ports_user_values_win_and_welcome_toggles():
    p = ini.ports(9600, 9600, 9700)
    text = ini.server_cfg({"SERVER": {"NAME": "x", "TCP_PORT": 1234, "PASSWORD": True}}, p, has_welcome=True)
    assert "TCP_PORT=1234" in text and "UDP_PORT=9600" in text           # a value the user set is kept, the rest is filled in
    assert "HTTP_PORT=9700" in text and "UDP_PLUGIN_ADDRESS=127.0.0.1:9603" in text and "PASSWORD=1" in text and "WELCOME_MESSAGE=cfg/welcome.txt" in text
    assert "WELCOME_MESSAGE" not in ini.server_cfg({"SERVER": {"WELCOME_MESSAGE": "old"}}, p, has_welcome=False)


def test_entry_list_numbers_the_cars():
    assert ini.entry_list([{"MODEL": "a"}, {"MODEL": "b", "SPECTATOR_MODE": False}]) == "[CAR_0]\nMODEL=a\n\n[CAR_1]\nMODEL=b\nSPECTATOR_MODE=0\n\n"
