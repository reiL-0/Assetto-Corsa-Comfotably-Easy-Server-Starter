"""The lobby page: `/INFO` and `/JSON|…` answered from acServer's internal HTTP port with the ports of the relay in place of its own."""

from __future__ import annotations

import asyncio
import json
import urllib.request


def _fetch(port: int, path: str) -> bytes:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as r:
            return r.read()
    except OSError:
        return b""


def patch_info(raw: bytes, udp_port: int, tcp_port: int, http_port: int) -> bytes:
    try:
        info = json.loads(raw)
    except ValueError:
        return raw
    info.update(port=udp_port, tport=tcp_port, cport=http_port)
    return json.dumps(info, ensure_ascii=False, separators=(",", ":")).encode()


FEATURES = ["WEATHERFX_V1", "SPECTATING_AWARE", "LOWER_CLIENTS_SENDING_RATE", "EMOJI", "CLIENT_MESSAGES", "CLIENT_UDP_MESSAGES"]   # as a real AssettoServer 0.0.54 announces


def csp_track(track: str, min_csp: int) -> str:
    return f"csp/{min_csp}/../{track}" if min_csp and track and not track.startswith("csp/") else track


def build_details(info: dict, min_csp: int, state: dict | None = None) -> dict:
    """`/api/details` (what Content Manager and CSP ask a Content-Manager-style server for): the lobby info plus what a real AssettoServer adds
    (captured): `features` (WEATHERFX_V1 is how CSP is told the server drives the weather), the track with the minimum CSP build in front, `trackBase`
    (the same without the layout), `poweredBy`, and the current weather. `state` = `Conditions.state` of the relay's weather plan."""
    track_id, _, layout = str(info.get("track", "")).partition("-")
    s = state or {}
    return {**info, "features": FEATURES, "track": csp_track(info.get("track", ""), min_csp), "trackBase": csp_track(track_id, min_csp),
            "poweredBy": "OPR relay (acServer)", "currentWeatherId": f"type={s.get('current', 15)}",
            "ambientTemperature": s.get("ambient", 20), "roadTemperature": s.get("road", 22), "grip": round(100 * s.get("grip", 1.0)),
            "windSpeed": 0, "windDirection": 0, "wrappedPort": info.get("cport"), "extra": True}


def handler(upstream_http: int, udp_port: int, tcp_port: int, http_port: int, min_csp: int = 0, conditions=None):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = (await asyncio.wait_for(reader.readline(), 5)).decode(errors="replace").split()
            while (await asyncio.wait_for(reader.readline(), 5)) not in (b"\r\n", b"\n", b""):
                pass
            path = line[1] if len(line) > 1 else "/"
            if path.startswith("/api/details"):
                raw = await asyncio.to_thread(_fetch, upstream_http, "/INFO")
                info = json.loads(patch_info(raw, udp_port, tcp_port, http_port) or b"{}")
                cond = conditions() if conditions else None
                data = json.dumps(build_details(info, min_csp, cond.state if cond else None), ensure_ascii=False, separators=(",", ":")).encode()
            else:
                data = await asyncio.to_thread(_fetch, upstream_http, path)
                if path.startswith("/INFO"):
                    data = patch_info(data, udp_port, tcp_port, http_port)
                    info = json.loads(data or b"{}")
                    info["track"] = csp_track(info.get("track", ""), min_csp)   # the lobby listing carries the CSP build too
                    data = json.dumps(info, ensure_ascii=False, separators=(",", ":")).encode()
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Length: {len(data)}\r\nContent-Type: {'application/json; charset=utf-8' if path.startswith('/api/details') else 'text/plain; charset=utf-8'}\r\nConnection: close\r\n\r\n".encode() + data)
            await writer.drain()
        except (asyncio.TimeoutError, OSError, IndexError):
            pass
        finally:
            writer.close()
    return handle
