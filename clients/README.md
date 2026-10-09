# clients — programas que hablan con el manager

| Elemento | Qué es |
|---|---|
| `OPRTelemetry/` | App Python que corre **dentro** de Assetto Corsa y envía la telemetría del auto local al backend (README propio). |
| `probe.py` | Prueba el endpoint sin abrir AC: envía telemetría sintética (un auto en círculos) a `POST /api/v1/telemetry/ingest` usando el mismo `opr_sender.Sender`. `python clients/probe.py --url http://localhost:8080 --steam-id 7656… --hz 8`. |

## Fuente de cada cliente (qué es canónico)
- **`clients/OPRTelemetry/` (aquí) es la generación antigua (v0.1.0)** de la app dentro del juego: identifica por SteamID64 y manda a `POST /api/v1/telemetry/ingest` **de este manager**. Es la que habla con `app/telemetry.py` y `clients/probe.py`; no se mezcla con la otra.
- **La app que usan los pilotos hoy es `AC-Live-Telemetry`** (repo `reiL-0/AC-Live-Telemetry`, v0.5.0; clave automática, `POST /api/telemetry/ingest` del backend de telemetría `telemetria.*`). Ese repo es la **fuente canónica**; `ACLivemapTrackerAPP/apps/python/OPRTelemetry/` es una copia exacta (su `SOURCE.txt` fija el commit y los hashes, y su CI lo comprueba). Se actualiza copiando desde el repo canónico, nunca editando la copia.
- **Los tres corren en el Python 3.3.5 embebido de Assetto Corsa.** Aquí no hay un 3.3 para probarlo: `ops/check_py33.py` es el filtro automático (rechaza f-strings, async, `typing`, `subprocess.run`, etc.); NO prueba que funcione en 3.3, eso sigue siendo la prueba dentro del juego. Se ejecuta en `tests/test_ops_checks.py`.
