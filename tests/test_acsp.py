import asyncio
import struct

from app import acsp


def test_read_string_matches_hand_built_buffer():
    # "hi!" as ACSP encodes it: 1 length byte, then each char widened to 4 bytes.
    buf = bytes([3]) + b"h\x00\x00\x00i\x00\x00\x00!\x00\x00\x00"
    s, pos = acsp._read_string(buf, 0)
    assert s == "hi!"
    assert pos == len(buf)


def test_write_string_round_trips_through_read_string():
    encoded = acsp._write_string("driver 1")
    s, pos = acsp._read_string(encoded, 0)
    assert s == "driver 1"
    assert pos == len(encoded)


def test_parse_chat():
    buf = bytes([acsp.CHAT, 3]) + acsp._write_string("hello")
    event = acsp.parse(buf)
    assert event == {"type": "chat", "car_id": 3, "message": "hello"}


def test_parse_car_update():
    payload = struct.pack("<6f", 1.0, 2.0, 3.0, 0.1, 0.2, 0.3) + struct.pack(
        "<2Hf", 3, 6500, 0.42
    )
    buf = bytes([acsp.CAR_UPDATE, 7]) + payload
    event = acsp.parse(buf)
    assert event["type"] == "car_update"
    assert event["car_id"] == 7
    assert event["pos"] == [1.0, 2.0, 3.0]
    assert event["gear"] == 3
    assert round(event["spline_pos"], 2) == 0.42


def test_parse_lap_completed_with_leaderboard():
    body = bytes([9]) + struct.pack("<I", 91234) + bytes([1, 2])
    body += struct.pack("<BIHB", 9, 91234, 3, 0)
    body += struct.pack("<BIHB", 2, 95000, 2, 1)
    body += struct.pack("<f", 0.98)
    event = acsp.parse(bytes([acsp.LAP_COMPLETED]) + body)
    assert event["car_id"] == 9
    assert event["laptime_ms"] == 91234
    assert event["cuts"] == 1
    assert event["leaderboard"] == [
        {"car_id": 9, "laptime_ms": 91234, "laps": 3, "has_completed": False},
        {"car_id": 2, "laptime_ms": 95000, "laps": 2, "has_completed": True},
    ]
    assert round(event["grip_level"], 2) == 0.98


def test_encode_admin_and_kick_commands():
    assert acsp.encode_kick_user(5) == bytes([acsp.KICK_USER, 5])
    assert acsp.encode_next_session() == bytes([acsp.NEXT_SESSION])
    cmd = acsp.encode_admin_command("ballast 3 50")
    assert cmd[0] == acsp.ADMIN_COMMAND
    s, _ = acsp._read_string(cmd, 1)
    assert s == "ballast 3 50"


def _s(text: str) -> bytes:
    # acServer's 1-byte-per-char string (car model/skin, track, ...)
    return bytes([len(text)]) + text.encode()


def test_parse_real_new_session_from_acserver_1_15():
    # Captured from a live acServer v1.15 (Linux build) over the plugin socket.
    buf = (
        b"2\x04\x00\x00\x01\x08O\x00\x00\x00P\x00\x00\x00R\x00\x00\x00 \x00\x00\x00"
        b"t\x00\x00\x00e\x00\x00\x00s\x00\x00\x00t\x00\x00\x00\x07magione\x00\x08Practice"
        b"\x01X\x02\x00\x00\x00\x00\x11\x16\x073_clear\x00\x00\x00\x00"
    )
    e = acsp.parse(buf)
    assert (e["server_name"], e["track"], e["name"], e["time_min"]) == ("OPR test", "magione", "Practice", 600)
    assert (e["ambient_temp"], e["road_temp"], e["weather"]) == (17, 22, "3_clear")


def test_client_updates_state_from_datagrams():
    client = acsp.ACSPClient(1)

    chat = bytes([acsp.CHAT, 4]) + acsp._write_string("gg")
    client.datagram_received(chat, ("127.0.0.1", 0))
    assert client.events[-1] == {"type": "chat", "car_id": 4, "message": "gg"}

    conn = bytes([acsp.NEW_CONNECTION])
    conn += acsp._write_string("Driver")
    conn += acsp._write_string("guid-1")
    conn += bytes([4])
    conn += _s("car_model") + _s("skin")
    client.datagram_received(conn, ("127.0.0.1", 0))
    assert 4 in client.cars
    assert client.cars[4]["driver_name"] == "Driver"

    closed = bytes([acsp.CONNECTION_CLOSED])
    closed += acsp._write_string("Driver")
    closed += acsp._write_string("guid-1")
    closed += bytes([4])
    closed += _s("car_model") + _s("skin")
    client.datagram_received(closed, ("127.0.0.1", 0))
    assert 4 not in client.cars


def test_connect_sends_and_receives_over_real_socket():
    async def scenario():
        client = await acsp.connect(1, remote_port=19001, local_port=19000)
        try:
            loop = asyncio.get_running_loop()
            received = []
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _EchoProtocol(received),
                local_addr=("127.0.0.1", 19001),
                remote_addr=("127.0.0.1", 19000),
            )
            try:
                client.send(acsp.encode_broadcast_chat("hi all"))
                await asyncio.sleep(0.1)
                assert len(received) == 1
                assert received[0][0] == acsp.BROADCAST_CHAT
            finally:
                transport.close()
        finally:
            client.close()

    asyncio.run(scenario())


class _EchoProtocol(asyncio.DatagramProtocol):
    def __init__(self, sink: list[bytes]) -> None:
        self.sink = sink

    def datagram_received(self, data: bytes, addr) -> None:
        self.sink.append(data)
