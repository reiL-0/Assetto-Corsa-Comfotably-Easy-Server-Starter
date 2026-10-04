# app/live — live server state and the ACSM-compatible view

**Purpose:** turn the acServer's ACSP UDP plugin feed into (a) the live timing table and (b) the slice of AC Server
Manager (ACSM) that the league site reads, so the site, its HUD and its telemetry validation work with no ACSM and
no stracker. Everything here belongs to one running acServer instance.

## Files

### `acsp.py` — ACSP protocol + per-instance client
- **Parsers** `parse(buf) -> event dict` and `_read_*`: one dict per datagram (`new_session`, `session_info`,
  `new_connection`, `connection_closed`, `car_update`, `car_info`, `lap_completed`, `client_event`, `chat`, …).
  Strings are UTF-32 (`_read_string`: names, chat, server name) or 1 byte/char (`_read_sstring`: track, session, car
  model/skin, weather) — verified against acServer v1.15 packets.
- **Encoders** `encode_*`: chat, kick, next/restart session, admin command, `encode_get_session_info`,
  `encode_realtime_pos_interval`.
- **`ACSPClient`** (asyncio datagram protocol, one per running server):
  - `events` (ring buffer), `n_events` (cursor for the `/live` websocket), `cars` (connected cars), `session`,
    `telemetry` (in-game app samples), `board` (`LiveBoard`, below).
  - `hello()`: acServer only sends positions when asked and misses our startup (the socket binds just after the
    process spawns), so for up to 30 s it asks for the session and for `POS_INTERVAL_MS` (200 ms) positions.
  - `connect(server_id, remote_port, local_port)` binds our side and starts `hello()`; `close()` cancels it.

### `board.py` — `LiveBoard`, `Driver`
- **`Driver`**: car_id, name, guid, model, skin, connected, best/last/total lap ms, laps, top_kmh, pos, spline.
- **`LiveBoard.apply(event)`**: `new_session` resets the table (only connected drivers carry over);
  `new_connection`/`connection_closed` flip `connected` (leavers stay listed until the next session);
  `car_update` sets position/spline and top speed; `lap_completed` sets last lap, adds it to the total and takes
  every car's best lap and lap count from the server's own `leaderboard` (sentinels ≥ 2^31 are "no lap").
- **`LiveBoard.leaderboard()`**: ACSM's `leaderboard.json` subset — session (`Track`, `TrackConfig`, `Name`, `Type`,
  `Time`, `Laps`, `ElapsedMilliseconds`, temps) and `ConnectedDrivers` / `DisconnectedDrivers` with `CarInfo`,
  `Cars{model: BestLap, LastLap, NumLaps, TotalLapTime, TopSpeedBestLap}` (times in **ns**), `LastPos{X,Y,Z}`,
  `NormalisedSplinePos`. In a race `Position` = by laps, then total time. Not available from ACSP and left empty:
  `Ping`, `IsInPits`, `BestLapSplits`, `Split`, `TeamName`, `DriverInitials`.

### `acsm.py` — public read routes (`router`, prefix `/servers/{id}/acsm`)
- `GET /api/live-timings/leaderboard.json` → `LiveBoard.leaderboard()`; 409 when the server is not running.
- `GET /content/tracks/{track}[/{config}]/map.png` and `.../data/map.ini` → files from the acServer's own
  `content/tracks` (via `app.content._tracks_dir`). Names must match `[\w.-]+`, so nothing outside that folder is reachable.
- Public on purpose: the manager listens on localhost and the site's proxy sends no token.

## Interactions
- **Fed by:** `supervisor.start` → `acsp.connect`; `ACSPClient._apply` calls `board.apply` for every event.
- **Read by:** the league site (`servers.json` `acsmUrl` = `http://127.0.0.1:8080/api/v1/servers/<id>/acsm`, `internal: true`)
  for `/api/leaderboard`, `/api/live-map` (+ `/api/live-map/image` relay) and the telemetry backend's connection check.
- **Content:** tracks uploaded with `POST /content/tracks` (zip/rar) or `POST /content/tracks/import` (file in the
  inbox dir) land in the acServer's `content/tracks`, so a server can run them and this module serves their maps.
- **Map files** come from the pack itself: `<track>[/<layout>]/map.png` + `data/map.ini`. `ui/…/outline.png` alone is
  NOT enough (arbitrary rotation/stretch; tested: ~20 % of the racing line lands on it).

## Flow (a lap on the map)
1. Server starts → `connect()` → `hello()` requests positions → `car_update` ~5/s per car → `Driver.pos/spline`.
2. Site polls `leaderboard.json` → `LastPos` per car; `map.ini` gives the world→pixel transform, `map.png` the image.
3. Car crosses the line → `lap_completed` → best/last/laps updated → standings change.
