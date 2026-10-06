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


def handler(upstream_http: int, udp_port: int, tcp_port: int, http_port: int):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = (await asyncio.wait_for(reader.readline(), 5)).decode(errors="replace").split()
            while (await asyncio.wait_for(reader.readline(), 5)) not in (b"\r\n", b"\n", b""):
                pass
            path = line[1] if len(line) > 1 else "/"
            data = await asyncio.to_thread(_fetch, upstream_http, path)
            if path.startswith("/INFO"):
                data = patch_info(data, udp_port, tcp_port, http_port)
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Length: {len(data)}\r\nContent-Type: text/plain; charset=utf-8\r\nConnection: close\r\n\r\n".encode() + data)
            await writer.drain()
        except (asyncio.TimeoutError, OSError, IndexError):
            pass
        finally:
            writer.close()
    return handle
