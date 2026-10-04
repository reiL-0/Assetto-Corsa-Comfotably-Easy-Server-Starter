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

### Content integrity (`app/integrity.py`)

What acServer verifies on every driver that joins (it logs the values at start: `CHECKSUM: …`, `ACD CHECKSUM:`): the MD5 of `system/data/surfaces.ini`,
the track's `data/surfaces.ini`, `models.ini` (`models_<layout>.ini`) and `data/drs_zones.ini`, and each car's `data.acd`; a mismatch kicks the driver.
It does not check models (kn5), skins, apps, CSP or other plugins, and has no setting to add files, so those cannot be enforced from the server.
The manager keeps the reference honest: `POST /integrity/seal {server_id | cars, track, config, extras}` (admin) stores the current MD5s as approved
(`ContentSeal`; extras = any file/folder under the server directory), `GET /integrity/check?server_id=` compares (`ok` / `changed` / `missing` /
`unsealed`), `DELETE /integrity/seal/{key}`, `GET /integrity/seals`, `GET /integrity/failures`. Per server (`Server.integrity`, `PUT /integrity/servers/{id}
{mode, extras}`, not part of any session): `off`; `warn` (default; a changed or missing file is reported on the status channel and the server starts);
`require` (it does not start unless everything is sealed and unchanged: 409). The gate runs in `start_server`, so the panel, schedules and wake all go
through it. Every server log line goes through `integrity.on_log_line`: a checksum failure reported by acServer is recorded (`checksum_fail`) and announced.

### Deploying

`./deploy.sh` deploys the committed `app/` to the VPS (refuses with uncommitted changes): runs the test suite locally, imports the staged package with the production venv, saves what runs to `/opt/acm/releases` (newest 5), `rsync`s it over `/opt/acm/app/app` (keeping `static/`), restarts `acm` and waits for `/healthz`; if it does not answer within 20 s the saved copy is put back. `/opt/acm/app/DEPLOYED` holds the revision running. A restart is safe for running games (see below). If `pyproject.toml` changed, new dependencies have to be installed in the server venv by hand (the script warns).

### Servers outlive the manager

`app/supervisor.py` starts each acServer in its own session with its output in `data/instances/<id>/server.log` (the previous run is kept as `server.log.1`) and its pid in `server.pid`. A manager restart therefore does not drop anyone: at boot (`servers.adopt_running`) every server whose pid file points at a live acServer in that instance directory is taken back: log followed again, ACSP socket re-bound, and `GET_CAR_INFO` sent for every slot so the people already on it reappear (`car_info` fills the board without counting a join). Uptime is kept; the event logged is `server_adopted`, not a start. A stale pid file (process gone, or pid reused by something else) is deleted. If the process disappears while adopted it counts as a crash (exit code unknown). The systemd unit must have `KillMode=process`, otherwise systemd kills the servers on `restart`/`stop`:

    # /etc/systemd/system/acm.service.d/killmode.conf
    [Service]
    KillMode=process

### Penalty announcements

Adding a penalty (`POST .../results/{file}/penalties`) posts the decision to `ACM_DISCORD_WEBHOOK` (server, session and track, driver, effect, the steward's reason; the steward's name is not shown), and removing one posts that it was withdrawn (`app/discord.py` `penalty_message`). A refused request posts nothing.

### Scheduled starts

`app/schedule.py`: `POST /schedules {event_id, server_id, start_at (unix s), reminders: [60, 10], duration_min?}` (steward reads, admin writes),
`GET /schedules`, `DELETE /schedules/{id}`. A task started in the app lifespan ticks every 20 s: the nearest due reminder is
posted to `ACM_DISCORD_WEBHOOK` (older missed ones are marked sent, not posted); at `start_at` the saved event is loaded onto the
server and it restarts (whoever is connected is dropped) unless it is already on it (`loaded`, see below). Overdue by more than 10 min
(manager was down) -> `missed`, not run; an apply error -> `failed` with the reason, also posted.

`info` (free text, ≤ 600 chars) is appended to its Discord messages; `silent_past` marks the reminders already due at creation as sent (the caller, the site's calendar, announced the event itself). A schedule that overlaps another pending/running one on the same server is refused with 409; events queued back to back are fine.

With `duration_min` the schedule is `running` until `start_at + duration`: 5 min before the end the in-game chat says so; at the end
the server is stopped (`server_stop` with reason `event_end`, Discord notice) and the schedule is `done`. Without it the schedule is
`done` once started and only the idle stop (`ACM_IDLE_STOP_SECONDS`) ends the session.

### Stopped but open: wake on connect (`app/wake.py`)

A stopped server can still look open and empty in the lobby, and a player trying to join starts it. Per server, `PUT /servers/{id}/wake {mode}`
(`Server.wake`, shown in Control AC): `off`; `window` (default: only inside an event's window, from 1 h before `start_at` until its end, or 3 h
after the start when there is no duration); `always` (any time, the server starts as it was left). While a stopped server is allowed to wake, the
manager holds its ports:
- **HTTP port (game port + 1):** answers like acServer with nobody on: `/INFO` from `data/instances/<id>/info.json` (a copy of the real answer the supervisor
  saves every minute and just before a stop; built from the config if it never ran) with `clients` 0 and the first session in full; `/JSON|<guid>` with the entry
  list's cars and skins; anything else 200 empty. Looking at the lobby wakes nothing.
- **Game port, only for Assetto Corsa:** the game or Content Manager first asks the lobby (`/INFO`, `/JSON|...`, user agent «Assetto Corsa Launcher»); a TCP connection to the
  game port wakes the server only from an address that did so in the last 15 min (it is reset, not closed, so no TIME_WAIT keeps acServer from binding the port). Anything
  else (a port scan, a browser, `curl`) is ignored. UDP never wakes anything: its one legitimate packet is the game's ping `0xC8`, answered like acServer does (`0xC8` + the HTTP
  port, 2 bytes little endian, e.g. `c8 81 25` for 9601) so Content Manager shows the stopped server as reachable and Join is clickable. A wake closes all the listeners, calls
  `schedule.wake` and leaves the ports to acServer. The first connection gets no answer; the player retries a few seconds later. With an event window the event is loaded as a start
  would (`loaded = true`, so the real start does not restart it and kick the early arrivals); if it is already loaded (idle stop or crash mid-event) or the mode is `always`, the
  server is simply started.
Outside the allowed times nothing listens, so a port scan cannot start anything; at most 6 wakes an hour and 30 s between two. Each wake logs a `wake` activity row.

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
| `POST/GET/PATCH/DELETE /championships[/{id}]` | championship CRUD (PATCH replaces name + points table; standings follow on the next read) |
| `POST /championships/{id}/events` | count a race result (validates the file exists) |
| `GET /championships/{id}/events` | events counted so far |
| `DELETE /championships/{id}/events/{event_id}` | stop counting a race |
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

## Sessions, saved events and content

All under `/api/v1` (steward reads, admin writes; events carry passwords).

| Endpoint | Purpose |
|----------|---------|
| `POST /servers/{id}/apply` | Build `server_cfg.ini` + entry list from a form (`SessionIn`: name, passwords, track + layout, cars or an explicit `entries` list, practice/qualify/race, `reversed_grid`, `loop`, `locked`, `pickup`), check the content is installed and loadable, optionally restart |
| `GET/POST /events`, `GET/PUT/DELETE /events/{id}` | Saved events (presets): a `SessionIn` under a title and notes |
| `POST /events/{id}/duplicate` | Copy |
| `POST /events/{id}/run {server_id, restart}` | Apply the event to a server, re-checking the content installed *now* |
| `GET /servers/{id}/results/{file}/parsed` | Classification with the stewards' penalties applied (`original_position`, `time_penalty_ms`, `penalties`, `disqualified`) and `grid` (driver order for the next race); `?raw=true` is exactly what acServer wrote |
| `GET/POST /servers/{id}/results/{file}/penalties`, `DELETE .../{penalty_id}` | Stewards' decisions (steward role, not just admin). `kind`: `time` (value = seconds added to the race time), `position` (places lost), `dsq`, `grid` (places lost on the next grid only), `points` (championship points taken); a `reason` is required; the driver must be in that result. The result file is never edited |
| `GET /content/tracks`, `GET /content/cars` | Installed content, with `usable` (acServer can load it) and, for tracks, `base` + layouts |
| `POST /content/uploads {kind}` → `PUT /content/uploads/{id}?offset=N` (parts) → `POST .../complete` | Archive in parts under the proxy's request cap, resumable by offset |
| `POST /content/uploads/from-link {kind, url}` | The server downloads a MediaFire / Google Drive / Dropbox link (known hosts only) |
| `GET /content/uploads/{id}` | Progress: `downloading` / `uploading` / `extracting` / `done` / `error` |

`entries` (`EntryIn`): one slot each with `model`, `skin`, `driver_name`, `team`, `guid` (SteamID64; several joined by `;` share a
car), `ballast` (kg), `restrictor` (%) and `spectator`. A slot with a `guid` is reserved for that driver; `locked` lets only those
Steam IDs in (`LOCKED_ENTRY_LIST`, rejected if nobody has one) and `pickup` lets everyone else take a free slot. With no
qualifying session the race grid follows the entry order, so the panel can order the table by a previous result's classification
(`GET /servers/{id}/results/{file}/parsed`) and invert the first N before applying. Fixed setups are not handled.

`options` (`OptionsIn`) carries the rest of `server_cfg.ini`: `sun_angle` (time of day, 0 = 13:00, 16° per hour), clock speed, ABS/TC
(0 off, 1 factory, 2 forced), stability / auto-clutch / tyre blankets / virtual mirror, damage / fuel / tyre-wear %, wheels allowed
out, legal tyres, max ballast, start rule, contacts per km, race-over / results-screen / qualify-wait times, pit window, vote quorums
and duration, ban mode, client send rate, a list of `weather` blocks (graphics name, ambient, road *above* ambient, wind) and the
`dynamic_track` grip section. Names are the INI keys in lower case. A field left out keeps what the server has now; `weather` replaces
all `[WEATHER_n]` blocks when sent. Every range is validated (422 otherwise).

Championship standings use the penalised classification: a disqualified driver scores nothing, `points` penalties are subtracted
(shown as `penalty_points`), and a time penalty can change who gets the points. With time penalties the order is laps first, then
total time (a driver who did not finish with `TotalTime` 0 would sort first among equals; review those by hand).

A weekend (practice → qualify → race, looping) already runs natively in one acServer session list; `reversed_grid`
maps to `REVERSED_GRID_RACE_POSITIONS`. Grid carry-over between *separate* runs uses the entry order (see above).

## Metrics (activity log)

`app/metrics.py` keeps an append-only `activity` table so the site's admin panel has history (it starts the day it is switched on and
is purged after 120 days). Recorded: `join` / `leave` / `lap` / `session` from ACSP, `online` (players on track, one sample a minute
per running server), `server_start` / `server_stop` (with reason `manual` or `idle`) / `server_crash` (the process ended by itself,
`value` = exit code), `import_ok` / `import_error` (content uploads and links) and `http_5xx` (any 5xx of this API).
`GET /api/v1/metrics/activity?days=14&tz=-360&hours=48` (steward) returns per-day peak players, player-minutes, laps, distinct drivers,
sessions, crashes and errors, a 10-minute online series, top tracks / cars / drivers and the latest incidents; `tz` is minutes east of
UTC and decides where a day ends.

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
| `ACM_DISCORD_STATUS_WEBHOOK` | _(empty)_ | Discord webhook that gets a post when a server starts, stops (manual / idle) or crashes (`app/discord.py`, hooked into `metrics.log`) |
| `ACM_DISCORD_WEBHOOK` | _(empty)_ | Discord webhook for league announcements: reminders and start/failure notices of scheduled starts (`app/schedule.py`) |

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
