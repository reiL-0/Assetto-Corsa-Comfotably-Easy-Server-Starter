# clients/OPRTelemetry — app dentro del juego

**Purpose:** app de Assetto Corsa (Python 3.3 del juego) que lee el auto del jugador local y lo envía cada ~120 ms a `POST /api/v1/telemetry/ingest`.
Se instala copiando la carpeta a `assettocorsa/apps/python/` y activándola en el juego. Copia `config.ini.example` → `config.ini` (si falta, se crea sola; sin `steam_id` queda en pausa).

| Archivo | Qué hace |
|---|---|
| `OPRTelemetry.py` | Punto de entrada de la app (`acMain`, `acUpdate`): arma la muestra y la deja en el *slot* del emisor; nunca bloquea el hilo de render. |
| `opr_telemetry.py` | Lee pedales, marcha, rpm, velocidad, posición, dirección con el módulo `ac`; devuelve el dict exacto que espera el backend o `None` si no hay auto. |
| `opr_mmap.py` | Lee rumbo, cabeceo y balanceo (radianes) de la memoria compartida `acpmf_physics` (el módulo `ac` no los da). |
| `opr_sender.py` | `Sender`: un hilo aparte con un único slot (se descarta lo viejo, no hay cola). Códigos: 204 aceptado; otros se reintentan o se descartan. |
| `opr_config.py` | Carga `config.ini` (`url` del backend, `steam_id`, `send_interval_ms`). |
| `config.ini.example`, `manifest.ini` | Plantilla de configuración y metadatos de la app (nombre, versión). |

Los módulos llevan prefijo `opr_` porque todas las apps de AC comparten `sys.modules` y un `config.py` chocaría con el de otra app.
**Interactions:** → `app/telemetry.py` (ingesta) → mapa en vivo del sitio (`OPR WP/telemetry_api.py`).
