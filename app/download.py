"""Fetch a content archive from a share link (MediaFire, Google Drive, Dropbox) straight onto the server.

The VPS downloads at datacenter speed and the request never crosses Cloudflare's 100 MB cap, so a track that is
already in someone's Drive does not have to go through a slow uplink.

Admin-only, and still guarded against SSRF: https only, only known file hosts (`settings.download_hosts` adds more),
and every hop, redirects included, must resolve to a public address.
ponytail: the address is checked at resolve time, not pinned for the connection; with hosts limited to big providers
rebinding is not a realistic path. Pin the IP if the host list ever opens up.
"""

from __future__ import annotations

import base64
import http.cookiejar
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from app.config import settings

HOSTS = (
    "mediafire.com", "drive.google.com", "docs.google.com", "drive.usercontent.google.com", "googleusercontent.com",
    "dropbox.com", "dropboxusercontent.com",
)
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
ALLOW_PRIVATE = False  # tests flip this to talk to a local server
CHUNK = 1 << 20


def check_url(url: str) -> urllib.parse.SplitResult:
    """Raise ValueError unless `url` is an https link to an allowed host that resolves to public addresses."""
    u = urllib.parse.urlsplit(url.strip())
    if u.scheme != "https" and not (ALLOW_PRIVATE and u.scheme == "http"):
        raise ValueError("only https links are accepted")
    host = (u.hostname or "").lower()
    if not any(host == h or host.endswith("." + h) for h in (*HOSTS, *settings.download_hosts)):
        raise ValueError(f"{host or 'that host'} is not an allowed download host (MediaFire, Google Drive, Dropbox)")
    if not ALLOW_PRIVATE:
        try:
            addrs = {a[4][0] for a in socket.getaddrinfo(host, u.port or 443, type=socket.SOCK_STREAM)}
        except OSError as e:
            raise ValueError(f"cannot resolve {host}") from e
        if not addrs or not all(ipaddress.ip_address(a).is_global for a in addrs):
            raise ValueError(f"{host} does not resolve to a public address")
    return u


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)  # a share link may bounce anywhere; every hop is held to the same rules
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), _Redirects
    )


def _get(op, url: str, limit: int | None = None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    return op.open(req, timeout=30)


def drive_id(u: urllib.parse.SplitResult) -> str:
    if "/folders/" in u.path:
        raise ValueError("Drive folders are not supported: share a single .zip file")
    m = re.search(r"/file/d/([\w-]+)", u.path)
    fid = m.group(1) if m else (urllib.parse.parse_qs(u.query).get("id") or [""])[0]
    if not fid:
        raise ValueError("could not find the file id in that Google Drive link")
    return fid


def mediafire_direct(page: str) -> str:
    """The real file URL inside a MediaFire share page (it is either plain or base64-scrambled)."""
    m = re.search(r'data-scrambled-url="([^"]+)"', page)
    if m:
        return base64.b64decode(m.group(1)).decode()
    for pat in (r'id="downloadButton"[^>]*href="(https?://[^"]+)"', r'href="(https?://[^"]+)"[^>]*id="downloadButton"',
                r'https?://download\d*\.mediafire\.com/[^"\'\s<>]+'):
        m = re.search(pat, page)
        if m:
            return m.group(1) if m.groups() else m.group(0)
    raise ValueError("MediaFire page has no download link (is the file public?)")


def resolve(url: str, op: urllib.request.OpenerDirector) -> str:
    """Turn a share link into a URL that returns the file itself."""
    u = check_url(url)
    host = u.hostname or ""
    if host == "drive.google.com" or host == "docs.google.com":
        return "https://drive.usercontent.google.com/download?" + urllib.parse.urlencode(
            {"id": drive_id(u), "export": "download", "confirm": "t"})
    if host.endswith("dropbox.com") and not host.endswith("dropboxusercontent.com"):
        q = dict(urllib.parse.parse_qsl(u.query)) | {"dl": "1"}
        return urllib.parse.urlunsplit(u._replace(query=urllib.parse.urlencode(q)))
    if host.endswith("mediafire.com") and "/file/" in u.path:
        with _get(op, url) as r:
            page = r.read(2_000_000).decode("utf-8", "replace")
        direct = mediafire_direct(page)
        check_url(direct)
        return direct
    return url


def _drive_confirm(page: str) -> str | None:
    """Drive answers big files with a 'cannot scan for viruses' form; its hidden fields are the real request."""
    m = re.search(r'<form[^>]+action="([^"]+)"', page)
    if not m:
        return None
    fields = dict(re.findall(r'<input[^>]+name="([^"]+)"[^>]+value="([^"]*)"', page))
    return m.group(1).replace("&amp;", "&") + "?" + urllib.parse.urlencode(fields)


def fetch(url: str, dest: Path, max_bytes: int, progress: Callable[[int, int | None], None] = lambda d, t: None) -> None:
    """Download `url` (already resolved) to `dest`, refusing pages that are not files and anything over `max_bytes`."""
    op = opener()
    resp = _get(op, url)
    if "text/html" in resp.headers.get("Content-Type", ""):  # Drive's confirmation step, else a login/error page
        confirm = _drive_confirm(resp.read(2_000_000).decode("utf-8", "replace"))
        resp.close()
        if not confirm:
            raise ValueError("the link did not return a file (is it shared with 'anyone with the link'?)")
        check_url(confirm)
        resp = _get(op, confirm)
        if "text/html" in resp.headers.get("Content-Type", ""):
            raise ValueError("the link did not return a file (is it shared with 'anyone with the link'?)")
    total = int(resp.headers.get("Content-Length") or 0) or None
    if total and total > max_bytes:
        raise ValueError(f"file is {total >> 20} MB, over the {max_bytes >> 20} MB limit")
    done = 0
    with resp, dest.open("wb") as fh:
        while chunk := resp.read(CHUNK):
            done += len(chunk)
            if done > max_bytes:
                raise ValueError(f"file is over the {max_bytes >> 20} MB limit")
            fh.write(chunk)
            progress(done, total)
