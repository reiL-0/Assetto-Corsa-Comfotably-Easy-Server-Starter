"""Envio de telemetria en un hilo aparte del loop de render.

Diseno: un unico 'slot' con el ultimo sample. Lo viejo se descarta, nunca se
acumula una cola. El worker se despierta con un Event, manda el sample por
HTTP y vuelve a dormir. Nada bloqueante toca el hilo principal del juego.

Codigos del backend:
    204  aceptado
    409  guid no conectado   -> se sigue probando, sin alarmar (es lo normal
                                hasta que el leaderboard vea al piloto)
    429  rate-limit          -> pausa corta y reintento
"""
import http.client
import json
import threading
import time
from urllib.parse import urlsplit

try:
    import ac
except ImportError:
    ac = None

INGEST_PATH = "/api/v1/telemetry/ingest"


def _log(msg):
    if ac is not None:
        ac.log("OPR Telemetry: " + msg)


class Sender(object):
    def __init__(self, url, steam_id, timeout, debug=False):
        parts = urlsplit(url if "://" in url else "http://" + url)
        self._scheme = parts.scheme or "http"
        self._host = parts.hostname or "localhost"
        self._port = parts.port or (443 if self._scheme == "https" else 80)
        self._path = parts.path.rstrip("/") + INGEST_PATH
        self._steam_id = steam_id or ""
        self._timeout = timeout
        self._debug = debug

        self._lock = threading.Lock()
        self._latest = None
        self._wake = threading.Event()
        self._stop = False
        self._thread = None
        self._cooldown_until = 0.0

        # estado que lee el hilo principal para el recuadro
        self.state = "starting"
        self.sent = 0
        self.last_code = 0
        self.detail = ""

    # -- lo que llama el hilo principal ---------------------------------
    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="opr-telemetry-sender")
        self._thread.daemon = True
        self._thread.start()

    def submit(self, sample):
        with self._lock:
            self._latest = sample
        self._wake.set()

    def stop(self):
        self._stop = True
        self._wake.set()
        t = self._thread
        if t is not None:
            t.join(timeout=2.0)

    # -- worker --------------------------------------------------------
    def _run(self):
        _log("worker iniciado -> {0}://{1}:{2}{3}".format(
            self._scheme, self._host, self._port, self._path))
        while not self._stop:
            self._wake.wait(timeout=1.0)
            self._wake.clear()
            if self._stop:
                break
            with self._lock:
                sample = self._latest
                self._latest = None
            if sample is None:
                continue
            if time.monotonic() < self._cooldown_until:
                continue
            self._send(sample)

    def _connection(self):
        if self._scheme == "https":
            return http.client.HTTPSConnection(self._host, self._port, timeout=self._timeout)
        return http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)

    def _send(self, sample):
        body = json.dumps(dict(sample, steamId=self._steam_id)).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Connection": "close",
        }
        conn = None
        try:
            conn = self._connection()
            conn.request("POST", self._path, body=body, headers=headers)
            resp = conn.getresponse()
            code = resp.status
            resp.read()
        except Exception as exc:  # red caida, timeout, DNS, TLS...
            self.state = "no_net"
            self.detail = str(exc)[:80]
            if self._debug:
                _log("sin red: " + self.detail)
            return
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

        self.last_code = code
        if code == 204:
            self.state = "ok"
            self.sent += 1
        elif code == 409:
            self.state = "waiting"
        elif code == 429:
            self.state = "cooldown"
            self._cooldown_until = time.monotonic() + 2.0
        else:
            self.state = "http_err"
            self.detail = "HTTP {0}".format(code)
        if self._debug and code != 204:
            _log("respuesta {0}".format(code))
