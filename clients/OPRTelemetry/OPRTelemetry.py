"""OPR Telemetry - app in-game de Assetto Corsa.

Lee la telemetria del auto del jugador local (acelerador, freno, embrague,
marcha, rpm, posicion, rotacion, velocidad, angulo de direccion) y la envia
cada ~120 ms al backend de Open Paddock Racing League por HTTP POST.

El envio corre en un hilo aparte (opr_sender.Sender). `acUpdate` solo arma el
sample y lo deja en un slot; si el backend no responde el dato se descarta y
se reintenta en el proximo tick - el juego nunca se traba.

Entradas del juego: solo `ac.getCarState(0, ...)` - el auto propio. El piloto se
identifica por su SteamID64 (se detecta solo desde Steam, o config.ini).
"""
import os
import sys
import time
import traceback

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import ac  # noqa: E402

import opr_config  # noqa: E402
import opr_telemetry  # noqa: E402
from opr_sender import Sender  # noqa: E402

APP_NAME = "OPR Telemetry"

_app_window = None
_label = None
_sender = None
_cfg = None
_interval_s = 0.12
_last_submit = 0.0
_error_logged = False

_STATE_TEXT = {
    "starting":  "iniciando...",
    "no_steam":  "no se detecto SteamID (ponelo en config.ini)",
    "ok":        "OK - enviados: {sent}",
    "waiting":   "esperando (auto no conectado aun)",
    "no_net":    "sin conexion al backend",
    "http_err":  "error {code}",
    "cooldown":  "rate-limit, reintentando...",
}


def acMain(ac_version):
    global _app_window, _label, _sender, _cfg, _interval_s

    _app_window = ac.newApp(APP_NAME)
    ac.setSize(_app_window, 280, 90)
    ac.setTitle(_app_window, APP_NAME)
    ac.drawBorder(_app_window, 0)

    _label = ac.addLabel(_app_window, "")
    ac.setPosition(_label, 12, 34)
    ac.setFontSize(_label, 14)

    try:
        _cfg = opr_config.load(APP_DIR)
    except Exception:
        ac.log("OPR Telemetry: error leyendo config.ini\n" + traceback.format_exc())
        _cfg = opr_config.Config()

    _interval_s = _cfg.send_interval_ms / 1000.0
    _sender = Sender(_cfg.url, _cfg.steam_id, _cfg.timeout_seconds, _cfg.debug)

    if not _cfg.steam_id:
        _sender.state = "no_steam"
        ac.log("OPR Telemetry: sin SteamID - la app queda en pausa")
    else:
        _sender.start()
        ac.log("OPR Telemetry: enviando a {0}".format(_cfg.url))

    return APP_NAME


def acUpdate(delta_t):
    global _last_submit, _error_logged
    try:
        _render()

        if _sender is None or _sender.state in ("no_steam",):
            return

        now = time.monotonic()
        if now - _last_submit < _interval_s:
            return
        _last_submit = now

        sample = opr_telemetry.read(0)
        if sample is not None:
            _sender.submit(sample)
    except Exception:
        if not _error_logged:
            _error_logged = True
            ac.log("OPR Telemetry: excepcion en acUpdate\n" + traceback.format_exc())


def _render():
    if _label is None or _sender is None:
        return
    st = _sender
    text = _STATE_TEXT.get(st.state, st.state).format(sent=st.sent, code=st.last_code)
    if st.detail and st.state in ("no_net", "http_err"):
        text += "\n" + st.detail
    ac.setText(_label, APP_NAME + "\n" + text)


def acShutdown():
    try:
        if _sender is not None:
            _sender.stop()
        opr_telemetry.shutdown()
    except Exception:
        pass
