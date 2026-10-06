"""Packet formats of the Assetto Corsa client protocol that the relay builds or has to recognise.

Layouts come from reading AssettoServer's source (AssettoServer.Shared/Network/Packets); they are NOT verified against a real client yet
(that is what the spike is for). TCP frames are a 2-byte little-endian length followed by the payload, whose first byte is the packet id.
"""

from __future__ import annotations

import struct

# ACServerProtocol ids
REQUEST_NEW_CONNECTION = 0x3D   # the client's handshake request
NEW_CAR_CONNECTION = 0x3E       # the server's handshake answer (HandshakeResponse); the first spike used 0x3D here by mistake, so nothing was edited or injected
CAR_LIST = 0x40
CAR_CONNECT = 0x4E          # first UDP datagram of a client: [id][session id]
WEATHER_UPDATE = 0x78       # vanilla weather: ambient, road, graphics name (UTF-32), wind
SUN_ANGLE_UPDATE = 0x54     # vanilla sun angle (a stock acServer sends it with the weather; the WeatherFX implementation of AssettoServer sends neither)
EXTENDED = 0xAB             # CSP's extension: [0xAB][sub id][...]

# CSPMessageTypeTcp / CSPMessageTypeUdp
TCP_CLIENT_MESSAGE = 0x03
UDP_WEATHER_UPDATE = 0x01
# CSPClientMessageType (from the enum's description: handshake messages are 0 and 1; the exact values are checked in the spike)
HANDSHAKE_IN = 0

WEATHER_TYPES = {  # CSP WeatherFX type ids (https://assettoserver.org/docs/misc/wfx-types/)
    "LightThunderstorm": 0, "Thunderstorm": 1, "HeavyThunderstorm": 2, "LightDrizzle": 3, "Drizzle": 4, "HeavyDrizzle": 5, "LightRain": 6,
    "Rain": 7, "HeavyRain": 8, "LightSnow": 9, "Snow": 10, "HeavySnow": 11, "LightSleet": 12, "Sleet": 13, "HeavySleet": 14, "Clear": 15,
    "FewClouds": 16, "ScatteredClouds": 17, "BrokenClouds": 18, "OvercastClouds": 19, "Fog": 20, "Mist": 21, "Smoke": 22, "Haze": 23,
}


def frame(payload: bytes) -> bytes:
    return struct.pack("<H", len(payload)) + payload


def split_frames(buf: bytearray) -> list[bytes]:
    """Take every complete TCP frame (payload only) off the front of `buf`; what is left is a partial frame."""
    out = []
    while len(buf) >= 2:
        n = struct.unpack_from("<H", buf)[0]
        if len(buf) < 2 + n:
            break
        out.append(bytes(buf[2:2 + n]))
        del buf[:2 + n]
    return out


def weather_update(*, unix: int, current: int, upcoming: int, transition: float, ambient: float, road: float, grip: float = 1.0,
                   wind_deg: float = 0.0, wind_kmh: float = 0.0, humidity: float = 0.5, pressure: float = 1013.0,
                   rain: float = 0.0, wetness: float = 0.0, water: float = 0.0) -> bytes:
    """CSP's UDP weather packet (CSPWeatherUpdate): the conditions CSP's WeatherFX and RainFX (including the physics) run on."""
    t = max(0, min(65535, round(transition * 65535)))
    return struct.pack("<BBQBBH10e", EXTENDED, UDP_WEATHER_UPDATE, unix, current, upcoming, t,
                       ambient, road, grip, wind_deg, wind_kmh, humidity, pressure, rain, wetness, water)


def csp_handshake_in(min_version: int, requires_weather_fx: bool) -> bytes:
    """What AssettoServer sends a CSP client over TCP so it applies a minimum CSP build and runs server-driven WeatherFX (without the length prefix)."""
    return struct.pack("<BBBHI?", EXTENDED, TCP_CLIENT_MESSAGE, 255, HANDSHAKE_IN, min_version, requires_weather_fx)


def parse_weather_update(data: bytes) -> dict:
    """The inverse of `weather_update`, for tests and for reading what was sent."""
    _, _, unix, cur, up, t, *halves = struct.unpack("<BBQBBH10e", data)
    keys = ("ambient", "road", "grip", "wind_deg", "wind_kmh", "humidity", "pressure", "rain", "wetness", "water")
    return {"unix": unix, "current": cur, "upcoming": up, "transition": t / 65535, **dict(zip(keys, halves))}
