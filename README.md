# AC Server Manager

Manager for **Assetto Corsa** dedicated servers, built only on the native Kunos
tooling: the `acServer` binary + INI config, the ACSP UDP plugin protocol for
live control/telemetry, and the JSON files in `results/`.

No `acServer` patching, no Content Manager dependency, no third-party server
binary (e.g. AssettoServer).

## API-first

**The product is the HTTP API.** The bundled React UI is just one client of it
and has no privileged or UI-only endpoints. Two ways to run it:

- **Full:** manager on localhost serving its own UI.
- **Headless:** `ACM_SERVE_UI=false`, host your own frontend on any domain, and
  have it drive the manager through the API — commands (`start`, `stop`,
  `next session`, `kick`, `broadcast`, ballast…) and file transfer
  (`server_cfg.ini`, `entry_list.ini`, content, skins, results, logs).

Everything is under a versioned prefix `/api/v1/...`. FastAPI generates the
schema: interactive docs at `/api/docs`, spec at `/api/openapi.json` — generate
a client from that.

## Stack

| Layer     | Choice |
|-----------|--------|
| Backend   | Python 3.12+, FastAPI + Uvicorn, `logging` (stdlib) |
| DB        | SQLite via SQLModel (SQLAlchemy 2 + Pydantic); WAL + foreign keys on |
| Config    | pydantic-settings, `ACM_*` env vars / `.env` |
| Frontend  | React 18 + Vite + TypeScript + Mantine + TanStack Query (optional client) |

## Layout

```
app/
  main.py            FastAPI app + lifespan; CORS, /healthz, routers, optional SPA
  config.py          env-driven settings
  db.py              SQLite engine, pragmas, init_db, SessionDep
  models.py          SQLModel tables (User, Server)
  servers.py         server CRUD + server_cfg.ini/entry_list.ini rendering + start/stop
  supervisor.py      spawn/stop acServer processes, stdout ring buffer, ACSP client lifecycle
  acsp.py            ACSP UDP plugin protocol: parse live events, encode admin commands
  content.py         car/track indexer, checksums, entry-list builder, content/skin zip transfer
  results.py         parses acServer session result JSON into a normalized classification
  championship.py    championship CRUD + points standings computed from counted race results
  api/v1/            versioned public API — the only surface clients use
  admin/servers.html standalone server-browser page (no build step), served at /admin/servers
  web.py             serves app/static SPA with index.html fallback
  static/            built frontend (gitignored; `make web` populates it)
web/                 React frontend source
tests/               pytest
data/content/{cars,tracks}/  installed content, indexed by content.py
data/instances/<id>/ per-server working dir (cfg/, results/) written on start
```

Planned modules (later phases): `app/scheduler/`.

### In-game telemetry (OPR Telemetry app)

`clients/OPRTelemetry/` is the AC in-game Python app (moved here from the
`ACLivemapTrackerAPP` repo). It reads the driver's **own** car (pedals, gear,
rpm, steer, heading, position) and POSTs it ~8 Hz to
`POST /api/v1/telemetry/ingest` (copy `clients/OPRTelemetry/` to `<AC>/apps/python/`). The sample is merged into the
live map: `GET /servers/{id}/cars` returns it under `telemetry` (dropped after 2 s
without updates) and `WS /live` streams `{"type":"telemetry","car_id",...}` events.

**No login, no token.** The app sends its own SteamID64 (auto-detected from the
Steam registry on Windows, or `steam_id` in `config.ini`) in the body; the manager
matches it against the ACSP `driver_guid` of the cars connected right now. Nothing
to provision per driver.

Replies: `204` ok · `409` driver not connected to a running server · `429` faster
than 20 Hz · `422` malformed body. Trade-off: the endpoint is public, so anyone who
knows a connected driver's SteamID64 could feed that car fake pedals/steer. It is
cosmetic live-map data only; lap times, splits and race position still come solely
from ACSP / results. `clients/` is excluded from ruff: it runs on AC's embedded
Python 3.3.
`python clients/probe.py --url ... --steam-id ...` sends synthetic samples without AC.

### Auth + RBAC

`app/auth.py`. One `tokens` table backs both **cookie sessions** (`POST
/auth/login`, 30 days, HttpOnly + SameSite=Lax) and **Bearer API tokens**
(`POST /auth/tokens`, no expiry, plaintext shown once, only the SHA-256 is
stored). Passwords are scrypt. Roles ascend `driver < steward < admin`.

| Who | Can |
|-----|-----|
| driver | read content + championships |
| steward | + read servers (config/logs/live/results) and moderate: chat, kick, next/restart session, admin command |
| admin | + every write (servers, content, championships, users) |

Fail-safe default (`guard()` in `auth.py`): every non-GET route is admin-only
until it is deliberately moved to the `steward` router. `/healthz`,
`/api/v1/version` and `/auth/login` are public.

**Bootstrap:** while no users exist, `POST /api/v1/users` is open and the
first user becomes admin:

```sh
curl -X POST localhost:8080/api/v1/users -H 'content-type: application/json' \
  -d '{"username":"admin","password":"change-me-please"}'
```

A remote (cross-origin) frontend should use a Bearer token, not the cookie.
The `/live` websocket accepts either (browsers can only send the cookie).

### ACSP (live timing / chat / live map / admin)

Each server gets a 4-port block (`base`..`base+3`): `tcp`/`udp`, `http`,
`plugin` (acServer's own `UDP_PLUGIN_LOCAL_PORT`), `plugin_local` (the
manager's side of the socket, written into `UDP_PLUGIN_ADDRESS`). On
`start`, `supervisor.start()` opens a UDP endpoint (`acsp.connect`) wired to
that block alongside the acServer process, and tears it down on `stop`.

`ACSPClient` (in `app/live/acsp.py`; see `app/live/README.md`) parses inbound datagrams (session info, car
connect/disconnect, car position updates, lap completed, chat, client
events) into dicts, keeping a ring buffer of raw events plus a live
`session` snapshot and `cars` map. It also encodes outbound commands (chat,
kick, next/restart session, and the generic `ADMIN_COMMAND` string used for
ballast/restrictor changes).

API surface, all under `/api/v1/servers/{id}/`:

| Endpoint | Purpose |
|----------|---------|
| `GET /session` | latest session info snapshot |
| `GET /cars` | connected cars + last known position/telemetry |
| `WS /live` | streams new ACSP events as JSON frames (polls every 200ms) |
| `POST /chat` | `{message, car_id?}` — broadcast or whisper |
| `POST /kick/{car_id}` | kick a driver |
| `POST /next_session` / `POST /restart_session` | session control |
| `POST /admin` | `{command}` — raw console admin command (e.g. `ballast 3 50`) |

All of these 409 if the server isn't running or the plugin socket hasn't
connected yet.

### Content (indexer, checksums, entry-list builder, file transfer)

Installed content lives under `data/content/{cars,tracks}/`, laid out the
same way the game itself expects it (`<car>/ui/ui_car.json`,
`<car>/data.acd`, `<car>/skins/<skin>/`; `<track>/ui/ui_track.json` or
`<track>/ui/<layout>/ui_track.json` for multi-layout tracks). `content.py`
indexes that tree, computes the SHA1s acServer itself checks for integrity
(`data.acd` for cars; `surfaces.ini` + `models[_<layout>].ini` for tracks),
and validates `{car, skin}` rows into `entry_list.ini`-ready dicts.

API surface under `/api/v1/content/`:

| Endpoint | Purpose |
|----------|---------|
| `GET /cars`, `GET /tracks` | indexed content with UI metadata + skins/layouts |
| `GET /cars/{car}/checksum`, `GET /tracks/{track}/checksum` | integrity hashes |
| `POST /cars`, `POST /tracks` | upload content as a zip (top-level folder = its name) |
| `GET /cars/{car}.zip`, `GET /tracks/{track}.zip` | download content as a zip |
| `POST /cars/{car}/skins`, `GET /cars/{car}/skins/{skin}.zip` | skin upload/download |
| `POST /entry_list` | `[{car, skin}]` -> validated `entry_list.ini` rows |

Uploaded zips are checked against zip-slip (`..` / absolute paths) before
extraction. Per-server file transfer lives on the servers router:
`PUT /servers/{id}/server_cfg.ini` and `PUT /servers/{id}/entry_list.ini`
accept a raw INI body and parse it back into the stored config, and
`GET /servers/{id}/results[/​{filename}]` lists/downloads session result
JSON files acServer writes under that instance's `results/` dir.

### Results + championship

`results.py` parses one of those result JSON files into a normalized shape:
session type/track, a `classification` (acServer's own finishing order,
annotated with gap-to-leader — total time for races, best lap otherwise),
and the raw `laps` list. `GET /servers/{id}/results/{filename}/parsed`
exposes it directly.

A `Championship` (`app/championship.py`) just points at a set of already
written race result files — no re-simulated standings, no stored totals.
Adding an event (`POST /championships/{id}/events`, `{server_id, filename}`)
records which result counts; `GET /championships/{id}/standings` re-reads
every counted `Race` result on each call and sums points by `DriverGuid`
using the championship's `points_system` (default top-10 F1-style
`[25,18,15,12,10,8,6,4,2,1]`), ranking ties by win count.

| Endpoint | Purpose |
|----------|---------|
| `POST/GET/DELETE /championships[/{id}]` | championship CRUD |
| `POST /championships/{id}/events` | count a race result (validates the file exists) |
| `GET /championships/{id}/events` | events counted so far |
| `GET /championships/{id}/standings` | computed points table |

Only `Race`-type sessions score; add a per-event flag if a league wants
qualifying points too (see the `ponytail:` note in `championship.py`).

## Prerequisites

- Python 3.12+ — installed (3.14).
- Node.js + npm — installed (needed only to build the React UI; the backend
  runs headless without it).

## First run

```sh
./start.sh              # same as: ./start.sh start
./start.sh stop
./start.sh restart
./start.sh status
```

`start.sh` reads `start.conf` (`BUILD_UI=true|false`) to decide whether to
build and serve the bundled UI or run headless; it checks for Python 3.12+
always, and for Node/npm only when `BUILD_UI=true`. `start`/`restart` run
`make install` (+ `make web` if `BUILD_UI=true`), then launch uvicorn
detached in its own process group, tracked via `.start.pid`, logging to
`data/server.log`. `stop` signals that process group so the `--reload`
worker dies with it. Or drive the Makefile targets yourself (foreground,
no PID tracking):

```sh
make install     # creates .venv, installs backend + dev deps
make run         # backend on http://127.0.0.1:8080  (autoreload)
```

- <http://127.0.0.1:8080/healthz> → `{"status":"ok"}`
- <http://127.0.0.1:8080/api/docs> → interactive API docs
- <http://127.0.0.1:8080/> → built UI, or an inline placeholder until `make web` is run
- <http://127.0.0.1:8080/admin/servers> → standalone server-browser admin page
  (`app/admin/servers.html`, plain HTML/JS + Tailwind CDN, no build step —
  create/edit/delete servers, start/stop, live status and player count via
  `fetch()` straight against `/api/v1/servers`)

`data/acmanager.db` is created and its tables built on first start.

Build the frontend:

```sh
make web         # cd web && npm install && npm run build  -> app/static/
```

`app/static/` is fully gitignored — the built UI is a build artifact, not
committed. A fresh clone runs headless and serves the inline placeholder until
`make web`.

### Frontend dev loop

```sh
make run       # terminal 1: backend
make dev-web   # terminal 2: Vite dev server, proxies /api + /healthz to :8080
```

## Tests

```sh
make test        # pytest
make lint        # ruff
```

## Config (env vars)

| Var                | Default                    | Meaning |
|--------------------|----------------------------|---------|
| `ACM_HOST`         | `127.0.0.1`                | HTTP bind host (`0.0.0.0` to expose) |
| `ACM_PORT`         | `8080`                     | HTTP port |
| `ACM_DATA_DIR`     | `data`                     | base dir for db, instance dirs, logs |
| `ACM_DB_PATH`      | `<data_dir>/acmanager.db`  | SQLite file path |
| `ACM_SERVE_UI`     | `true`                     | serve the bundled UI; `false` = pure API |
| `ACM_CORS_ORIGINS` | `[]`                       | JSON list of allowed cross-origin sites |
| `ACM_ACSERVER_CMD` | `""`                       | argv for the AC dedicated server; empty = start disabled. Its directory must hold `content/` and `system/` (symlinked into each instance) |
| `ACM_PORT_RANGE_START` / `ACM_PORT_RANGE_END` | `9600` / `9700` | pool for per-server port blocks (4 apart) |
| `ACM_LOG_LINES`    | `500`                      | per-instance stdout ring buffer size |
| (content dir) | | with `ACM_ACSERVER_CMD` set, `content/` **is the acServer's own** (uploads land where the server reads them); otherwise `<data_dir>/content`. Big archives: copy to `<data_dir>/inbox/` and `POST /content/tracks/import {"file": "x.rar"}` (Cloudflare caps uploads at 100 MB). `.rar` needs `bsdtar` (`apt install libarchive-tools`). |
| `ACM_IDLE_STOP_SECONDS` | `0`                   | stop an instance after N s with no connected cars (0 = never); restart via `POST /servers/{id}/start` |

## Roadmap

0. **Scaffold** — FastAPI app, SQLite + models, versioned API skeleton, SPA shell ✔
1. **Server CRUD + config rendering + process lifecycle** ✔ (start/stop, INI
   generation, port allocation; readiness parsing + auto-restart deferred)
2. **ACSP client: live timing, chat, live map, admin actions** ✔ (UDP
   protocol parse/encode, session+cars snapshot, live WS feed, chat/kick/
   next/restart/admin-command endpoints; per-server realtime-pos interval
   tuning + reconnect-on-restart deferred)
3. **Content indexer + checksums + entry-list builder + file transfer
   endpoints** ✔ (car/track indexing, SHA1 checksums, entry-list builder, zip
   upload/download for content+skins, raw INI upload, results
   listing/download; a real content library to test against is next)
4. **Results parser + championship engine** (result JSON parsing
   with classification + gaps, championship CRUD, points standings from
   counted Race results done; qualifying/practice points and drop-weeks
   deferred)
5. Auth (cookie session + Bearer API tokens) ✔, RBAC ✔ ← *here*; live
   stewarding (incident log, penalties) still to do
6. Scheduler, multi-server, optional plugin chaining
