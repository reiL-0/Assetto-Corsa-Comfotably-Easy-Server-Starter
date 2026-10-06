import asyncio
import struct

from app.relay import protocol as p
from app.relay import http as relay_http
from app.relay.tcp import TcpRelay, rewrite_track, rewrite_udp_port
from app.relay.udp import UdpRelay
from app.relay.weather import Conditions, Plan


def test_weather_packet_round_trip_and_framing():
    b = p.weather_update(unix=7, current=15, upcoming=7, transition=0.5, ambient=20, road=22, rain=0.5, wetness=0.25)
    assert b[:2] == b"\xab\x01" and len(b) == 34
    d = p.parse_weather_update(b)
    assert (d["unix"], d["current"], d["upcoming"], d["rain"], d["wetness"]) == (7, 15, 7, 0.5, 0.25) and abs(d["transition"] - 0.5) < 1e-4
    buf = bytearray(p.frame(b"abc") + p.frame(b"de")[:3])
    assert p.split_frames(buf) == [b"abc"] and bytes(buf) == p.frame(b"de")[:3]   # a partial frame stays for the next read
    assert p.csp_handshake_in(3898, True) == b"\xab\x03\xff\x00\x00" + struct.pack("<I", 3898) + b"\x01"


def test_plan_blends_into_the_next_step_and_wetness_builds_then_dries():
    plan = Plan([(0, 15), (100, 7), (400, 15)], transition_s=20)
    assert plan.at(10) == (15, 15, 0.0) and plan.at(90)[:2] == (15, 7) and abs(plan.at(90)[2] - 0.5) < 1e-9
    assert plan.at(150) == (7, 7, 0.0) and plan.at(500) == (15, 15, 0.0)
    c = Conditions(plan)
    states = [dict(c.step(1.0)) for _ in range(400)]
    peak = max(x["wetness"] for x in states)
    assert max(x["rain"] for x in states) > 0.5 and 0.3 < peak <= 1.0 and states[10]["rain"] == 0   # dry at first, it rained, the track got wet
    for _ in range(1500):
        c.step(1.0)
    assert c.state["rain"] == 0 and c.wetness < peak                                              # and it dried afterwards


def test_udp_relay_forwards_both_ways_and_injects_only_after_the_handshake():
    async def scenario():
        loop = asyncio.get_running_loop()
        got = []

        class Server(asyncio.DatagramProtocol):   # a stand-in for acServer: answers every datagram
            def connection_made(self, t):
                self.t = t

            def datagram_received(self, data, addr):
                got.append((data, addr))
                self.t.sendto(b"srv:" + data, addr)
        srv, _ = await loop.create_datagram_endpoint(Server, local_addr=("127.0.0.1", 0))
        relay = UdpRelay(srv.get_extra_info("sockname")[1], lobby_http_port=9681)
        pub, _ = await loop.create_datagram_endpoint(lambda: relay, local_addr=("127.0.0.1", 0))
        pub_addr = pub.get_extra_info("sockname")

        class Client(asyncio.DatagramProtocol):
            def __init__(self):
                self.rx = []

            def datagram_received(self, data, addr):
                self.rx.append((data, addr))
        a, b = Client(), Client()
        ta, _ = await loop.create_datagram_endpoint(lambda: a, remote_addr=pub_addr)
        tb, _ = await loop.create_datagram_endpoint(lambda: b, remote_addr=pub_addr)
        ta.sendto(b"\x4e\x01")     # A connects (CAR_CONNECT)
        tb.sendto(b"\xc8")         # B only pinged the lobby
        await asyncio.sleep(0.3)
        assert a.rx[0][0] == b"srv:\x4e\x01" and a.rx[0][1] == pub_addr                                 # replies come from the relay's address
        assert b.rx[0][0] == b"\xc8" + struct.pack("<H", 9681) and all(d != b"\xc8" for d, _ in got)    # the lobby ping is answered by the relay with its own HTTP port
        assert len({addr for _, addr in got}) == 1                                                      # acServer sees one endpoint per client that reached it
        assert relay.inject(b"WEATHER") == 1
        await asyncio.sleep(0.2)
        assert (b"WEATHER", pub_addr) in a.rx and all(d != b"WEATHER" for d, _ in b.rx)
        for t in (ta, tb, srv, pub):
            t.close()
    asyncio.run(scenario())


def test_tcp_relay_rewrites_the_udp_port_and_adds_the_csp_handshake():
    name = "Srv"
    hs = bytes([p.NEW_CAR_CONNECTION, len(name)]) + b"".join(c.encode("utf-32-le") for c in name) + struct.pack("<H", 9691) + b"rest"
    assert struct.unpack_from("<H", rewrite_udp_port(hs, 9680), 2 + 4 * 3)[0] == 9680 and rewrite_udp_port(hs, 9680).endswith(b"rest")

    async def scenario():
        async def server(r, w):   # acServer stand-in: answers a request with the handshake, then the car list
            await r.read(1)
            w.write(p.frame(hs) + p.frame(bytes([p.CAR_LIST, 1])) + p.frame(bytes([p.WEATHER_UPDATE, 9])) + p.frame(bytes([p.WEATHER_UPDATE, 8])))
            await w.drain()
            await asyncio.sleep(0.2)
            w.close()
        srv = await asyncio.start_server(server, "127.0.0.1", 0)
        relay = TcpRelay(srv.sockets[0].getsockname()[1], 9680, inject_after="weather", min_csp=3898)   # (the optional handshake injection is still supported)
        front = await asyncio.start_server(relay.handle, "127.0.0.1", 0)
        r, w = await asyncio.open_connection("127.0.0.1", front.sockets[0].getsockname()[1])
        w.write(b"x")
        await asyncio.sleep(0.4)
        buf = bytearray(await r.read(4096))
        frames = p.split_frames(buf)
        assert [f[0] for f in frames] == [p.NEW_CAR_CONNECTION, p.CAR_LIST, p.WEATHER_UPDATE, p.EXTENDED, p.WEATHER_UPDATE]   # once, right after the first vanilla weather frame
        assert struct.unpack_from("<H", frames[0], 2 + 4 * 3)[0] == 9680 and frames[3] == p.csp_handshake_in(3898, True)
        w.close()
        srv.close()
        front.close()
    asyncio.run(scenario())


def test_track_gets_the_csp_build_in_front_and_details_announce_weatherfx():
    name = "Srv"
    hs = bytes([p.NEW_CAR_CONNECTION, len(name)]) + b"".join(c.encode("utf-32-le") for c in name) + struct.pack("<H", 9751) + bytes([18]) + bytes([5]) + b"imola" + b"rest"
    out = rewrite_track(hs, 2744)
    pos = 1 + 1 + 4 * 3 + 2 + 1
    assert out[pos] == len(b"csp/2744/../imola") and out[pos + 1:pos + 1 + out[pos]] == b"csp/2744/../imola" and out.endswith(b"rest")
    assert rewrite_track(out, 2744) == out and rewrite_track(hs, 0) == hs                          # not twice, not without a build
    d = relay_http.build_details({"track": "imola-gp", "cport": 9681, "name": "x"}, 2744, {"current": 7, "ambient": 14, "road": 12, "grip": 0.9})
    assert "WEATHERFX_V1" in d["features"] and d["track"] == "csp/2744/../imola-gp" and d["trackBase"] == "csp/2744/../imola"
    assert (d["ambientTemperature"], d["grip"], d["wrappedPort"]) == (14, 90, 9681)
