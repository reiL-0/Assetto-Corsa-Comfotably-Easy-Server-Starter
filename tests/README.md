# tests — pytest

**Purpose:** pruebas del backend (`make test` o `.venv/bin/python -m pytest -q -p no:warnings`; `deploy.sh` las corre antes de desplegar).
`conftest.py` fija un directorio de datos temporal (`ACM_DATA_DIR`), `ACM_ACSERVER_CMD=""` (nunca arranca un acServer real), amplía el rango de puertos y crea un admin `root` con token Bearer compartido por los clientes de prueba.

| Archivo | Cubre |
|---|---|
| `test_auth.py` | login, roles, tokens |
| `test_servers.py`, `test_options.py`, `test_session.py` | CRUD, render de INI, opciones de sesión, `apply`, bienvenida, carrera por tiempo |
| `test_supervisor.py` | arranque/parada/re-enganche de procesos |
| `test_acsp.py`, `test_live.py` | codificación/decodificación ACSP, estado en vivo |
| `test_wake.py`, `test_timeline.py` | fachada del lobby, ping UDP, reloj de sesión |
| `test_schedule.py`, `test_events.py` | programación, solapes, ventana, eventos guardados |
| `test_integrity.py` | sellos, modos, vigilancia de checksums |
| `test_content.py`, `test_download.py` | índice, subidas por partes, descarga por enlace |
| `test_results.py`, `test_penalties.py`, `test_championship.py` | clasificación, sanciones, puntos |
| `test_metrics.py`, `test_telemetry.py`, `test_db.py`, `test_health.py` | métricas, ingesta, migración de columnas, salud |

Los tests no deben bloquear el bucle de eventos ni dejar sockets abiertos (los de red usan puertos efímeros).
