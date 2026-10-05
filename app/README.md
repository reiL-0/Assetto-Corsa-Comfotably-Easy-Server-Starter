# app — AC Server Manager (backend)

**Purpose:** API HTTP (FastAPI + SQLModel/SQLite) que crea, configura, arranca y vigila servidores de Assetto Corsa (`acServer` nativo
de Kunos), con control en vivo por el protocolo UDP ACSP. Cada módulo es un router `/api/v1/...` (se agrupan en `api/v1/__init__.py`);
no hay endpoints «solo para la UI». Subcarpetas: `live/` (ACSP, tiempos en vivo — su README), `api/` (router versionado), `admin/` (página HTML).

## Files

| Módulo | Qué es / lo no obvio |
|---|---|
| `main.py` | App FastAPI. `lifespan` abre la BD, re-engancha (`adopt`) los acServer que sobrevivieron a un reinicio, lanza los bucles de fondo (`schedule.run_forever`, `wake.run_forever`, parada por inactividad, purga de métricas) y los cierra. `GET /healthz`, `GET /admin/servers`. Monta la SPA (`web.mount_spa`) al final si `ACM_SERVE_UI`. |
| `config.py` | `Settings` (pydantic-settings, prefijo `ACM_`): `host`, `port`, `data_dir`, `db_path`, `serve_ui`, `cors_origins`, `acserver_cmd`, `port_range_start/end` (9600–9700, bloques de puertos por servidor), `log_lines`, `download_hosts`, `idle_stop_seconds` (0 = nunca), `discord_status_webhook`, `discord_webhook`. |
| `db.py` | Motor SQLite (WAL, `foreign_keys`), `init_db()` (crea tablas y `_add_missing_columns`: añade columnas nuevas a tablas existentes sin migraciones; cuidado con tipos como `AutoString`), `get_session`. |
| `models.py` | Tablas: `User`, `Token` (sesiones y tokens API en una sola tabla), `Server` (puertos, `wake`, `integrity`, `integrity_extras`, `welcome`…), `Event`, `Schedule`, `ContentSeal`, `Penalty`, `Activity`, `Championship`, `ChampionshipEvent`. |
| `auth.py` | Usuarios y roles (`admin` > `steward` > lectura), login por cookie o `Authorization: Bearer`; `guard(role)` es la dependencia que protege routers; hash de contraseñas y de tokens (solo se guarda el hash). |
| `servers.py` | CRUD de servidores, reparto de puertos (`_alloc_base_port`: TCP/UDP/HTTP/plugin), render de `server_cfg.ini` y `entry_list.ini`, `SessionIn`/`apply_session` (aplica una sesión completa: pista, autos, tiempos, opciones, inscritos, bienvenida; opcionalmente reinicia), `start_server`/`stop`, ajuste `wake`, y los comandos en vivo (`chat`, `kick`, `next_session`, `restart_session`, `admin`). Rutas steward: lecturas y moderación; escritura admin. |
| `supervisor.py` | Un proceso `acServer` por servidor, en su propia sesión y con la salida en `server.log`, para **sobrevivir a reinicios del manager**; `adopt()` lo reconoce por `server.pid`. `Instance` guarda cliente ACSP, buffer de logs y estado. |
| `wake.py` | Servidor apagado que «parece abierto»: `Waker` ocupa sus puertos, responde el ping UDP del lanzador (`0xC8`) y sirve `/INFO` y `/JSON|guid` con la misma forma que acServer; un intento de entrar por TCP desde una dirección que consultó el lobby (UA «Assetto Corsa Launcher», 15 min) lo enciende. `before_start` libera los puertos. |
| `timeline.py` | Reloj de sesión del servidor «apagado»: calcula en qué sesión y minuto estaría y, al arrancar de verdad, lo salta a esa posición (`resume`, vía ACSP `SET_SESSION_INFO` + `NEXT_SESSION`). La posición se lee **antes** de arrancar. |
| `schedule.py` | Arranques programados: recordatorios a Discord, inicio del evento a su hora, parada al terminar (`duration_min`), `open_window` (ventana en la que `wake` está permitido), rechazo 409 por solapes. `tick` cada pocos segundos. |
| `events.py` | Eventos guardados: una `SessionIn` completa con título; `run_event` la aplica y reinicia el servidor. `duplicate`. |
| `content.py` | Índice de autos y pistas instalados, checksums, `build_entry_list`, descarga/subida en zip, subida por partes (`/uploads`, `PUT` con `offset`, `complete`) y desde enlace (`/uploads/from-link`). |
| `download.py` | Descarga desde MediaFire, Google Drive o Dropbox directo al VPS (lista blanca `download_hosts`, límites de tamaño y redirecciones). |
| `integrity.py` | Sellos MD5 de lo que acServer comprueba (`surfaces.ini` del sistema, `surfaces.ini` y `models.ini` de la pista, `data.acd` de cada auto) + extras (archivos/carpetas del servidor). Modo por servidor `off/warn/require` (puerta antes de arrancar) y vigilancia de «checksum» en el log (`on_log_line`). |
| `results.py` | Lee `results/*.json` de acServer a clasificación normalizada; `apply_penalties` reordena con las sanciones. |
| `penalties.py` | Sanciones del comisariado, guardadas aparte del resultado (tipos: tiempo, posiciones, DSQ, parrilla, puntos); aviso a Discord. |
| `championship.py` | Campeonatos: puntos y carreras contadas; clasificación calculada al leer (con sanciones aplicadas). |
| `metrics.py` | Registro de actividad (`log`) y agregados (`summary`, `activity`, `now`); `purge` por antigüedad. |
| `discord.py` | Webhooks: estado del servidor (arrancó/paró/cayó), `announce` (anuncios de liga), `alert`, mensajes de sanción. |
| `telemetry.py` | `POST /telemetry/ingest`: recibe lo que manda la app dentro del juego (`clients/OPRTelemetry`) y lo mezcla con el mapa en vivo. Público (identifica por SteamID64). |
| `web.py` | `mount_spa`: sirve `app/static` (frontend compilado) con respaldo a `index.html`. |

## Interactions
- **Inbound:** el sitio web de la liga (`OPR WP/acm/manager_api.py`, token en servidor), la UI React (`web/`), `clients/`, `deploy.sh`.
- **Outbound:** procesos `acServer`, UDP ACSP, Discord, descargas externas; disco: `data/acmanager.db`, `data/content/{cars,tracks}`, `data/instances/<id>/` (cfg, results, `server.log`, `server.pid`).
- **Variables de entorno:** todas `ACM_*` (ver `config.py` y el README raíz).
- **Puertos:** bloque por servidor desde 9600 (juego TCP/UDP, HTTP = juego+1, plugin local).

## Flow — «Iniciar sesión» desde el panel
1. `POST /servers/{id}/apply` con `SessionIn` → `servers.apply_session` valida contenido (`_check_content`), escribe los INI y, si `restart`, detiene y arranca.
2. `start_server` → `integrity.gate` → `wake.before_start` (libera puertos) → `timeline.resume` (lee la posición) → `supervisor.start`.
3. `supervisor` crea el proceso, el cliente ACSP se conecta y `timeline` coloca la sesión donde toca.
4. Los resultados llegan a `results/` y se leen con `results.parse_result_file` + `penalties`.
