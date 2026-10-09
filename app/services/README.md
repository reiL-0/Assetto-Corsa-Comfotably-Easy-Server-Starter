# app/services — logic of the manager that is not HTTP

**Purpose:** what the routes call. Handlers authenticate, validate, call a service and translate its result; services know nothing of FastAPI.

- `ini_generator.py` (pure): acServer's `server_cfg.ini` / `entry_list.ini` text from stored data and explicit ports (`ports`, `render_ini`, `server_cfg`, `entry_list`). `app.servers` keeps `_ports`, `_render_ini`, `render_server_cfg`, `render_entry_list` as thin wrappers that pass the manager's settings.

**Interactions:** called by `app/servers.py` (`_write_instance`, the `.ini` routes), `app/wake.py` (`_ports`). Tests: `tests/test_ini_generator.py`.
