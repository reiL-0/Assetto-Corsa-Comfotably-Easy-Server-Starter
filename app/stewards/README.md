# app/stewards — comisarios automáticos (fase 0: modo sombra)

**Purpose:** mirar los eventos ACSP de cada servidor y guardar como `Incident` lo que parece una infracción. Fase 0: **solo registra**; no manda chat, no sanciona, no expulsa. Diseño completo y fases siguientes: `OPR WP/todo/plugin-penalizaciones-diseno.md`.

## Files

### `detectors.py`
- `WALL_MIN_SPEED = 15.0`, `CONTACT_MIN_SPEED = 5.0`: velocidad mínima de impacto para contar un muro / un contacto. **Suposiciones sin verificar** (se asume km/h); se ajustan con los incidentes reales.
- `detect(event) -> dict | None`: función pura. Entra un evento ya parseado de `app/live/acsp.py`; sale `{kind, car_id, other_car_id, speed, value, world_pos}` o `None`.
  - `client_event` contra el entorno → `wall`; contra otro auto → `contact` (ambos coches avisan, ver dedup); ambos solo si `speed` llega al mínimo.
  - `lap_completed` con `cuts > 0` → `cuts` (`value` = cuántos cortes en esa vuelta).

- `BEHIND_DEG = 35`, `SAME_WAY_DEG = 70`, `MOVING = 3.0` (m/s): umbrales de la sugerencia de culpa (ver `at_fault`).
- `at_fault(a, b) -> (0 | 1 | None, motivo)`: con el último `car_update` de cada auto (`pos`, `velocity` en el plano x/z) culpa a quien **venía detrás y avanzando hacia el otro** en la misma dirección. Contacto lateral, de frente o con autos parados → `None` («lo decide el comisario»). Es una **sugerencia**: las posiciones son de ~5 Hz.

### `engine.py`
- `DEDUP_S = 1.0`, `_recent: dict[(server_id, kind, coches)] -> epoch`: acServer reporta un contacto desde los dos autos y un roce como ráfaga; se guarda un incidente por coche/pareja por segundo. `cuts` no se deduplica (llega una vez por vuelta). El contacto usa la pareja ordenada, así A→B y B→A son la misma clave.
- `on_event(client, event, now=None)`: lo llama `ACSPClient._apply` con **cada** evento. Sin detección sale sin tocar la BD. Si hay: lee `Server.stewards`; solo con `"shadow"` escribe un `Incident` (en un contacto calcula además `fault_guid` y `evidence` con `at_fault`; piloto y nombre salen de `client.cars`, sesión de `client.session`, reloj de `client.board.elapsed_ms`). Escribe síncrono en SQLite, como `bans.is_banned`.

### `api.py` (router con prefijo `/servers/{server_id}`; el guard de `api/v1` deja leer al comisario y escribir al admin)
- `GET /incidents?kind=&limit=` → `Incident` más recientes primero (límite 500).
- `PUT /stewards` `{mode: off|shadow}` → cambia `Server.stewards` y devuelve el `ServerOut`.

## Datos
- Tabla `incidents` (`app/models.py: Incident`): server_id, ts, session_type/name/ms, kind (`wall|contact|cuts`), car_id, driver/other guid y nombre, speed, value, world_pos, `fault_guid` (contacto: a quién señala la heurística, `None` si no hay culpa clara) y `evidence` (contacto: posición y velocidad de los dos autos y el motivo). «Piloto» y «con» de un contacto solo dicen quién reportó primero el evento, **no** quién tuvo la culpa. Solo se añade.
- `Server.stewards`: `off` (por defecto) | `shadow`. Se muestra también en `ServerOut`.

## Interactions
- **Entra:** `app/live/acsp.py` (`ACSPClient._apply`, import tardío para no hacer un ciclo) y `app/api/v1/__init__.py` (router).
- **Usa:** `app.live.acsp` (constantes `COLLISION_WITH_*`), `app.db.engine`, `app.models`, `app.servers` (`_get`, `_out`, `ServerOut`).
- **No hace aún:** hablar con el piloto, crear `Penalty`, avisar a Discord, cola de comisarios (fases 1+).

## Flow
1. acServer manda un datagrama → `ACSPClient.datagram_received` → `parse` → `_apply`.
2. `_apply` actualiza tablero/coches y llama `engine.on_event`.
3. `detect` decide si es muro/contacto/cortes; `on_event` deduplica, comprueba el modo del servidor y guarda el `Incident`.
4. El comisario lo ve en `GET /servers/{id}/incidents`.
