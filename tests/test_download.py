import http.server
import io
import threading
import time
import urllib.parse
import zipfile

import pytest
from conftest import ADMIN
from fastapi.testclient import TestClient

from app import content, download
from app.config import settings
from app.main import app

V = "/api/v1"


def _zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for k, v in entries.items():
            zf.writestr(k, v)
    return buf.getvalue()


ZIP = _zip({"linktrack/data/surfaces.ini": b"x" * 3000, "linktrack/map.png": b"png"})


class _Files(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        port = self.server.server_address[1]
        if self.path == "/ok.zip":
            self._send(200, ZIP, "application/zip")
        elif self.path == "/redir":
            self.send_response(302)
            self.send_header("Location", "/ok.zip")
            self.end_headers()
        elif self.path == "/bounce":
            self.send_response(302)
            self.send_header("Location", "https://evil.example/x.zip")
            self.end_headers()
        elif self.path == "/page":
            self._send(200, b"<html>please log in</html>", "text/html")
        elif self.path == "/drive":  # Drive's "cannot scan for viruses" step: a form whose hidden fields are the request
            form = (f'<form id="download-form" action="http://127.0.0.1:{port}/confirmed" method="get">'
                    '<input type="hidden" name="id" value="F1"><input type="hidden" name="uuid" value="u-1"></form>')
            self._send(200, form.encode(), "text/html")
        elif self.path.startswith("/confirmed"):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            ok = q.get("uuid") == ["u-1"] and q.get("id") == ["F1"]
            self._send(200 if ok else 403, ZIP if ok else b"no", "application/zip")
        else:
            self._send(404, b"", "text/plain")

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def files(monkeypatch):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Files)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(download, "ALLOW_PRIVATE", True)  # the fake host is on loopback
    monkeypatch.setattr(settings, "download_hosts", ["127.0.0.1"])
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_only_known_hosts_over_https_and_public_addresses(monkeypatch):
    for bad in ("http://drive.google.com/file/d/x", "https://evil.example/a.zip", "https://169.254.169.254/latest",
                "https://localhost/a", "ftp://mediafire.com/a", "file:///etc/passwd", "https://mediafire.com.evil.example/a"):
        with pytest.raises(ValueError):
            download.check_url(bad)
    monkeypatch.setattr(download.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("10.0.0.5", 443))])
    with pytest.raises(ValueError, match="public address"):  # an allowed name pointing inside the network
        download.check_url("https://drive.google.com/file/d/x")
    monkeypatch.setattr(download.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("142.250.1.1", 443))])
    assert download.check_url("https://drive.google.com/file/d/x").hostname == "drive.google.com"


def test_share_links_become_direct_downloads(monkeypatch):
    monkeypatch.setattr(download, "ALLOW_PRIVATE", True)  # skip DNS; the host rules still apply
    op = download.opener()
    d = download.resolve("https://drive.google.com/file/d/ABC_def-1/view?usp=sharing", op)
    assert d.startswith("https://drive.usercontent.google.com/download?") and "id=ABC_def-1" in d and "confirm=t" in d
    assert "id=XYZ" in download.resolve("https://drive.google.com/open?id=XYZ", op)
    with pytest.raises(ValueError, match="folders"):
        download.resolve("https://drive.google.com/drive/folders/abc", op)
    assert "dl=1" in download.resolve("https://www.dropbox.com/s/abc/f.zip?dl=0", op)
    plain = "https://download123.mediafire.com/abc/file.zip"
    assert download.mediafire_direct(f'<a id="downloadButton" class="x" href="{plain}">Download</a>') == plain
    assert download.mediafire_direct(f'<a class="x" href="{plain}" id="downloadButton">') == plain
    import base64
    scrambled = base64.b64encode(plain.encode()).decode()
    assert download.mediafire_direct(f'<a data-scrambled-url="{scrambled}" id="downloadButton">') == plain
    with pytest.raises(ValueError, match="no download link"):
        download.mediafire_direct("<html>file removed</html>")


def test_fetch_follows_redirects_and_the_drive_confirmation(files, tmp_path):
    for path in ("/ok.zip", "/redir", "/drive"):
        dest = tmp_path / "f.zip"
        seen: list[tuple[int, int | None]] = []
        download.fetch(files + path, dest, 10**7, lambda d, t, log=seen: log.append((d, t)))
        assert dest.read_bytes() == ZIP and seen[-1] == (len(ZIP), len(ZIP))


def test_fetch_refuses_pages_big_files_and_redirects_off_the_list(files, tmp_path):
    with pytest.raises(ValueError, match="not return a file"):
        download.fetch(files + "/page", tmp_path / "f", 10**7)
    with pytest.raises(ValueError, match="limit"):
        download.fetch(files + "/ok.zip", tmp_path / "f", 100)
    with pytest.raises(ValueError, match="not an allowed download host"):  # a link that bounces to another host
        download.fetch(files + "/bounce", tmp_path / "f", 10**7)


def _wait(api, uid):
    for _ in range(200):
        st = api.get(f"{V}/content/uploads/{uid}").json()
        if st["state"] in ("done", "error"):
            return st
        time.sleep(0.05)
    raise AssertionError("never finished")


def test_import_from_link_end_to_end(files):
    api = TestClient(app, headers=ADMIN)
    uid = api.post(f"{V}/content/uploads/from-link", json={"kind": "track", "url": files + "/redir"}).json()["id"]
    st = _wait(api, uid)
    assert st["state"] == "done" and st["result"] == "linktrack" and st["total"] == len(ZIP)
    assert (content._tracks_dir() / "linktrack" / "data" / "surfaces.ini").is_file()
    assert not any(content._scratch().glob("upload-*"))
    bad = api.post(f"{V}/content/uploads/from-link", json={"kind": "car", "url": files + "/page"}).json()["id"]
    st = _wait(api, bad)
    assert st["state"] == "error" and "download failed" in st["error"]


def test_import_from_link_validates_and_needs_admin(monkeypatch):
    monkeypatch.setattr(download, "ALLOW_PRIVATE", False)
    api = TestClient(app, headers=ADMIN)
    r = api.post(f"{V}/content/uploads/from-link", json={"kind": "track", "url": "https://evil.example/a.zip"})
    assert r.status_code == 400 and "allowed download host" in r.json()["detail"]
    assert api.post(f"{V}/content/uploads/from-link", json={"kind": "skin", "url": "https://x.mediafire.com/a"}).status_code == 422
    anon = TestClient(app).post(f"{V}/content/uploads/from-link", json={"kind": "track", "url": "https://mediafire.com/a"})
    assert anon.status_code == 401
