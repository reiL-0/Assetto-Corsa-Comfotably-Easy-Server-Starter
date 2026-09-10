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
  supervisor.py      spawn/stop acServer processes, stdout ring buffer
  api/v1/            versioned public API — the only surface clients use
  web.py             serves app/static SPA with index.html fallback
  static/            built frontend (gitignored; `make web` populates it)
web/                 React frontend source
tests/               pytest
data/instances/<id>/ per-server working dir (cfg/, results/) written on start
```

Planned modules (later phases): `app/acsp/` (UDP protocol + client),
`app/servers/` (acServer process supervision), `app/content/`, `app/results/`,
`app/championship/`, `app/auth/` (cookie session + Bearer API tokens),
`app/scheduler/`.

## Prerequisites

- Python 3.12+ — installed (3.14).
- Node.js + npm — **not installed**. On Arch: `sudo pacman -S nodejs npm`
  (only to build the React UI; the backend runs headless without it).

## First run

```sh
make install     # creates .venv, installs backend + dev deps
make run         # backend on http://127.0.0.1:8080  (autoreload)
```

- <http://127.0.0.1:8080/healthz> → `{"status":"ok"}`
- <http://127.0.0.1:8080/api/docs> → interactive API docs
- <http://127.0.0.1:8080/> → built UI, or an inline placeholder until `make web` is run

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
| `ACM_ACSERVER_CMD` | `""`                       | argv for the AC dedicated server; empty = start disabled |
| `ACM_PORT_RANGE_START` / `ACM_PORT_RANGE_END` | `9600` / `9700` | pool for per-server port blocks (4 apart) |
| `ACM_LOG_LINES`    | `500`                      | per-instance stdout ring buffer size |

## Roadmap

0. **Scaffold** — FastAPI app, SQLite + models, versioned API skeleton, SPA shell ✔
1. **Server CRUD + config rendering + process lifecycle** ← *here* (start/stop, INI
   generation, port allocation done; readiness parsing + auto-restart deferred)
2. ACSP client: live timing, chat, live map, admin actions
3. Content indexer + checksums + entry-list builder + file transfer endpoints
4. Results parser + championship engine
5. Auth (cookie session + Bearer API tokens), RBAC, live stewarding
6. Scheduler, multi-server, optional plugin chaining
