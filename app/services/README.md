# app/services — logic of the manager that is not HTTP

**Purpose:** what the routes call. Handlers authenticate, validate, call a service and translate its result; services know nothing of FastAPI.

- `ini_generator.py` (pure): acServer's `server_cfg.ini` / `entry_list.ini` text from stored data and explicit ports (`ports`, `render_ini`, `server_cfg`, `entry_list`). `app.servers` keeps `_ports`, `_render_ini`, `render_server_cfg`, `render_entry_list` as thin wrappers that pass the manager's settings.

- `server_service.py`: lifecycle of a managed server, no HTTP: `apply` (save a session; with `restart`, stop + start as one step), `start`, `stop`, `stop_instance` (automatic stop, only if the instance is still the registered one), `adopt_running` (boot), `write_instance`, `ports`, and the per-server `server_lock`. Refuses with `ServerError(status, detail)`. The lock order is always `server_lock` -> `supervisor`; callers already holding it (the scheduler, deciding `loaded` and applying as one step) pass `held=True` (not reentrant). The DB session is the caller's.

**Interactions:** `app/servers.py` (routes = adapters: `apply_to_server`, `start_server`, `stop_server` translate `ServerError` into `HTTPException`, same status/detail; `_write_instance`, the `.ini` routes), `app/schedule.py` (tick / wake call the service directly), `app/wake.py` (`ports`), `app/events.py` (via `servers.apply_to_server`). Tests: `tests/test_ini_generator.py`.
