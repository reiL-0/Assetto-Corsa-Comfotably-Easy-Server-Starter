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

### `cspcmd.py`, `cspweather.py`, `weatherplan.py` — clima dinámico de CSP por chat oculto
Lo que hace el plugin oficial `plugin-dynamic-conditions` de CSP (leído del código, **sin verificar aún con un cliente**): comandos de CSP como mensajes de chat ocultos que un plugin ACSP manda a un acServer sin modificar.
- `cspcmd.py`: `serialize/deserialize` (texto = `"\t\t\t\t$CSP0:"` + base64 sin `=` de `u16 LE tipo + estructura empaquetada`), `weather_set_v2` (tipo 1001, 32 B: hora simulada, tipo actual y siguiente, transición u16, `TimeToApply`, ambiente, asfalto, agarre en un byte sobre 0,6–1,0, humedad en un byte, viento °/km/h, presión hPa, lluvia, mojado, charcos; los reales en `Half`), `handshake_in` (tipo 0: build mínimo y «requiere WeatherFX»), `parse_weather_set_v2`.
- `weatherplan.py`: el plan de clima con la forma del editor de AC Server Manager. Modo `entries`: lista de climas (`Entry`: tipo WeatherFX, duración en minutos reales antes de pasar al siguiente —0 = hasta el fin de la sesión—, sesiones a las que pertenece, temperaturas base con variación —el asfalto se suma al ambiente—, viento en m/s con dirección y variación); dentro de una sesión se reproducen en orden desde su inicio, cada cambio un fundido suave (`transition_s`) que acaba cuando empieza el siguiente (`Timeline`); una sesión sin ningún clima asignado deja el clima vainilla del servidor. Modo `live`: el clima real de una latitud/longitud con **Open-Meteo** (sin clave; `fetch_live`: código WMO + nubosidad → tipo WeatherFX con `live_type`, temperatura, viento, humedad, presión), refrescado cada `refresh_min` y con fundido al cambiar de tipo. `Weather.step` devuelve las condiciones que se mandan: lluvia por tipo, mojado y charcos propios (suben con la lluvia y bajan al secarse; CSP calcula los suyos por tipo), agarre y humedad.
- `cspweather.py`: `WeatherDirector` (lo posee `supervisor.Instance`, arranca con el servidor): cada `update_s` (30 s por defecto, y un primer paso a los 2 s para que un cambio hecho en Control AC se vea ya; el `driving` del plan (`visual`: agarre 100 % y sin agua en la pista, la lluvia solo se ve) y el `sun_angle`, si lo trae, manda sobre el `SUN_ANGLE` del servidor para la hora que ven los clientes CSP; el plugin oficial usaba 1 min) pregunta a `Weather` por las condiciones de la sesión en curso (el reloj de sesión de `LiveBoard` dice cuál y cuánto lleva) y las difunde con `BROADCAST_CHAT` **solo si algo cambió** (tipo, transición, temperatura, viento, lluvia) o cada `KEEPALIVE` = 60 s; al evento `client_loaded` (`ACSPClient.on_client_loaded`) manda el último al coche que acaba de cargar con `SEND_CHAT`. La fecha simulada sigue `SUN_ANGLE` y `TIME_OF_DAY_MULT`. **Por qué poco y espaciado:** cada comando hace que cada cliente recalcule nubes y lluvia, y las transiciones son donde jugadores con PCs más flojos pierden fotogramas o ven errores de simulación (experiencia de otras ligas con el clima dinámico de ACSM): por eso máximo 8 climas por plan, fundidos de ≥ 20 s (90 s por defecto) y comandos espaciados. El plan está en `Server.weather_plan`: `PUT/DELETE /servers/{id}/weather_plan` (admin), `POST /servers/{id}/csp_weather` (steward: una condición suelta, para probar).

### `logboard.py` — `LogBoard`
El tablero en vivo leído del **log de acServer**, para cuando no hay socket ACSP. acServer imprime su clasificación tras cada vuelta (`SendLapCompletedMessage` + una línea `N) nombre BEST: … TOTAL: … Laps:n SesID:i HasFinished:b` por plaza), quién ocupa cada plaza (`Dispatching TCP message to <auto> (<plaza>) [<nombre> []]`, con el Steam ID en la línea `Looking for available slot … GUID` anterior) y la sesión (`SENDING session name/type/time/laps`, `NextSession`). `feed(línea)` (lo llama `supervisor.Instance._tail` con cada línea nueva) mantiene un `LiveBoard`, la misma estructura que rellena ACSP, así que `acsm.leaderboard` sirve el mismo JSON de cualquiera de las dos fuentes. Sabe menos que ACSP: sin posición en pista, velocidad punta ni última vuelta. Un bloque completo manda sobre quién está conectado.

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
- `tracktime.py`: la hora que muestra un cliente CSP es el `timestamp` del comando leído en la **zona horaria de la pista** (medido en vivo: 12:37 enviado se vio como 23:37 en una pista de Melbourne, UTC+11). `offset_seconds` calcula ese desfase (zona IANA del plan, o los `geotags` de la pista consultados en Open-Meteo con caché; sin dato: 0) y el director envía «hora que se quiere − desfase». El director se crea desde código `async` (los endpoints de plan de clima lo son): crearlo desde un hilo daba «no current event loop» (HTTP 500 y director perdido).
