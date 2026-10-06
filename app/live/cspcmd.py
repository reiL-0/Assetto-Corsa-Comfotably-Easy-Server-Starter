"""Custom Shaders Patch commands that an ACSP plugin can send to CSP clients, as hidden chat messages (what CM's «dynamic conditions» plugin does).

Format (read from ac-custom-shaders-patch/plugin-dynamic-conditions, NOT yet verified against a client in this project): the chat text is
`"\\t\\t\\t\\t$CSP0:" + base64(u16 LE message type + packed little-endian struct)` without the `=` padding. The tabs push it out of sight in the original
game; CSP clients read it. Sent with `ACSP SEND_CHAT` (one car) or `BROADCAST_CHAT` (everybody), so it works with an unmodified acServer.
"""

from __future__ import annotations

import base64
import struct

PREFIX = "\t\t\t\t$CSP0:"
HANDSHAKE_IN, HANDSHAKE_OUT, WEATHER_SET_V2 = 0, 1, 1001


def serialize(msg_type: int, payload: bytes) -> str:
    return PREFIX + base64.b64encode(struct.pack("<H", msg_type) + payload).decode().rstrip("=")


def deserialize(text: str) -> tuple[int, bytes]:
    b64 = text.split("$CSP0:", 1)[1]
    raw = base64.b64decode(b64 + "=" * (-len(b64) % 4))
    return struct.unpack_from("<H", raw)[0], raw[2:]


def handshake_in(min_build: int, requires_weather_fx: bool) -> str:
    """Server -> client: the CSP build the server asks for and whether it needs WeatherFX (an unfit client is told so and leaves)."""
    return serialize(HANDSHAKE_IN, struct.pack("<I?", min_build, requires_weather_fx))


def weather_set_v2(*, timestamp: int, current: int, upcoming: int, transition: float, time_to_apply: float, ambient: float, road: float,
                   grip: float = 1.0, humidity: float = 0.5, wind_deg: float = 0.0, wind_kmh: float = 0.0, pressure: float = 1013.0,
                   rain: float = 0.0, wetness: float = 0.0, water: float = 0.0) -> str:
    """`CommandWeatherSetV2` (type 1001): the conditions CSP's WeatherFX and RainFX (including the physics) run on. `timestamp` = the simulated date (unix s);
    `transition` 0..1 blends `current` into `upcoming`; `time_to_apply` = seconds CSP takes to reach these values (use the broadcast period);
    `grip` is sent as one byte over 0.6..1.0, `humidity` over 0..1; pressure in hPa, wind in km/h, rain values 0..1."""
    grip_enc = round(max(0.0, min(1.0, (grip - 0.6) / 0.4)) * 255)
    return serialize(WEATHER_SET_V2, struct.pack("<QBBHeeeBBeeeeee", timestamp, current, upcoming, max(0, min(65535, round(transition * 65535))),
                                                 time_to_apply, ambient, road, grip_enc, round(max(0.0, min(1.0, humidity)) * 255),
                                                 wind_deg, wind_kmh, pressure, rain, wetness, water))


def parse_weather_set_v2(payload: bytes) -> dict:
    ts, cur, up, tr, tta, amb, road, grip, hum, wdeg, wkmh, pres, rain, wet, water = struct.unpack("<QBBHeeeBBeeeeee", payload)
    return {"timestamp": ts, "current": cur, "upcoming": up, "transition": tr / 65535, "time_to_apply": tta, "ambient": amb, "road": road,
            "grip": 0.6 + grip / 255 * 0.4, "humidity": hum / 255, "wind_deg": wdeg, "wind_kmh": wkmh, "pressure": pres, "rain": rain, "wetness": wet, "water": water}
