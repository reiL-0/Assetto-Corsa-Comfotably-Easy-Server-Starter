"""Carga de configuracion para OPR Telemetry.

Lee `config.ini` junto al script. Si no existe, lo crea copiando
`config.ini.example` y devuelve valores por defecto (sin steam_id -> pausa).

Modulo con prefijo `opr_` a proposito: todas las apps de AC comparten
`sys.modules`, asi que un `config.py` "pelado" chocaria con el de otra app.
"""
import configparser
import os
import shutil


def _detect_steam_id():
    """SteamID64 de la cuenta logueada en Steam (Windows). '' si no se puede."""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam\ActiveProcess")
        account = winreg.QueryValueEx(key, "ActiveUser")[0]
        winreg.CloseKey(key)
        return str(76561197960265728 + account) if account else ""
    except Exception:
        return ""


class Config(object):
    def __init__(self):
        self.url = "http://localhost:8080"
        self.steam_id = ""
        self.send_interval_ms = 120
        self.timeout_seconds = 2.0
        self.debug = False
        self.path = ""


def load(app_dir):
    cfg = Config()
    cfg.path = os.path.join(app_dir, "config.ini")

    if not os.path.isfile(cfg.path):
        example = os.path.join(app_dir, "config.ini.example")
        if os.path.isfile(example):
            try:
                shutil.copyfile(example, cfg.path)
            except OSError:
                pass
        cfg.steam_id = _detect_steam_id()
        return cfg

    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(cfg.path)
    except configparser.Error:
        cfg.steam_id = _detect_steam_id()
        return cfg

    def get(section, option, default):
        try:
            return parser.get(section, option).strip()
        except (configparser.NoSectionError, configparser.NoOptionError):
            return default

    cfg.url = (get("backend", "url", cfg.url) or cfg.url).rstrip("/")
    cfg.steam_id = get("backend", "steam_id", "") or _detect_steam_id()

    try:
        cfg.send_interval_ms = int(float(get("telemetry", "send_interval_ms", "120")))
    except ValueError:
        pass
    try:
        cfg.timeout_seconds = float(get("telemetry", "timeout_seconds", "2.0"))
    except ValueError:
        pass
    cfg.debug = get("telemetry", "debug", "0").lower() in ("1", "true", "yes", "on")
    return cfg
