# app/stewards — comisarios automáticos (fase 0: modo sombra)

**Purpose:** mirar los eventos ACSP de cada servidor y guardar como `Incident` lo que parece una infracción. Fase 0: **solo registra**; no manda chat, no sanciona, no expulsa. Diseño completo y fases siguientes: `OPR WP/todo/plugin-penalizaciones-diseno.md`.

## Files

### `detectors.py`
- `WALL_MIN_SPEED = 15.0`, `CONTACT_MIN_SPEED = 5.0`: velocidad mínima de impacto para contar un muro / un contacto. **Suposiciones sin verificar** (se asume km/h); se ajustan con los incidentes reales.
- `detect(event) -> dict | None`: función pura. Entra un evento ya parseado de `app/live/acsp.py`; sale `{kind, car_id, other_car_id, speed, value, world_pos}` o `None`.
  - `client_event` contra el entorno → `wall`; contra otro auto → `contact` (ambos coches avisan, ver dedup); ambos solo si `speed` llega al mínimo.
  - `lap_completed` con `cuts > 0` → `cuts` (`value` = cuántos cortes en esa vuelta).

- `BEHIND_DEG = 35`, `SAME_WAY_DEG = 70`, `MOVING = 3.0` (m/s): umbrales de la sugerencia de culpa (ver `at_fault`).
- `PAIR_S = 0.1`: dos muestras de posición son «el mismo instante» si se tomaron con menos de esto (acServer manda todos los autos en un mismo ciclo).
- `at_contact(trail_a, trail_b, point) -> (estado_a, estado_b, hora) | None`: **acServer avisa de un contacto 1,5–2,5 s tarde** (medido en una sesión real: el punto del choque queda ~50 m atrás de cada auto sobre su línea de avance), así que las posiciones actuales ya pasaron el choque. Busca en el historial `ACSPClient.trail` el par de muestras más cercanas entre sí y al punto de choque y devuelve el estado (`pos`, `velocity`) de cada auto en ese instante. `None` si falta el historial de alguno.
- `at_fault(a, b) -> (0 | 1 | None, motivo)`: con el estado de cada auto en el choque (`pos`, `velocity` en el plano x/z) culpa a quien **venía detrás y avanzando hacia el otro** en la misma dirección. Contacto lateral, de frente o con autos parados → `None` («lo decide el comisario»). Es una **sugerencia**: las posiciones son de ~5 Hz.

### `engine.py`
- `DEDUP_S = 3.0`, `_recent: dict[(server_id, kind, coches)] -> epoch`: acServer reporta un contacto desde los dos autos y un roce como ráfaga; se guarda un incidente por coche/pareja cada 3 s. `_recent` guarda cuándo se **admitió** la última vez y lo protege `_cv`; si el incidente no llega a escribirse (fallo de BD o modo `off`) la clave se libera, salvo que otra admisión más nueva la haya reemplazado.
- `MAX_QUEUED = 1000`: el callback UDP (event loop) **no espera a SQLite**. `on_event` / `record_limits` construyen el `Incident` con el estado en memoria y lo entregan por `_admit` (dedup + cola acotada + entrega en un paso, bajo `_cv`) a **un** hilo escritor (`_drain`, patrón de `metrics.enqueue`). Cola llena = se descarta el más nuevo, se cuenta (`dropped`) y se registra en el log (1, 10, 100, cada 1000). Un fallo de escritura se registra con traza, se cuenta (`failed`) y libera la clave de dedup. El escritor lee `Server.stewards` en la BD: si no es `shadow` descarta y cuenta `ignored`.
- `flush(timeout)` espera a que todo lo admitido esté escrito (pruebas y cierre: `lifespan` lo llama en un hilo y avisa en el log si quedan incidentes sin guardar); `stats()` → `{queued, written, failed, dropped, ignored}`.
- `on_event(client, event, now=None)`: lo llama `ACSPClient._apply` con **cada** evento. Sin detección sale sin más. Si hay: deduplica y entrega el `Incident` (en un contacto calcula además `fault_guid` y `evidence` con `at_fault` sobre el estado de los dos autos **en el momento del choque** (`at_contact`; la evidencia guarda `moment_ago_s`, cuánto antes del aviso fue); piloto y nombre salen de `client.cars`, sesión de `client.session`, reloj de `client.board.elapsed_ms`). Ya no escribe en SQLite desde el callback.

- `LIMITS_MIN_S = 0.5`: un script de piloto no puede reportar más seguido que esto por auto (es del cliente: podría inundar).
- `record_limits(client, car_id, name, report, now=None) -> bool`: entrega a la cola un incidente `limits` con lo que reporta el script de CSP del piloto (`report` = `{ms, wheels, speed, lap, spline, pos}`; `value` = ms fuera, `speed` = velocidad máxima fuera, `evidence.source = "client"`) si el auto `car_id` existe y su `driver_name` coincide con `name`; `True` = encolado, `False` = auto desconocido, ráfaga (`LIMITS_MIN_S`) o cola llena. Se llama desde el event loop (lee el estado vivo del cliente); el escritor lo descarta si el servidor no está en `shadow`. **Evidencia para el comisario, nunca sanción automática** (el cliente puede mentir).

### `api.py` (router con prefijo `/servers/{server_id}`; el guard de `api/v1` deja leer al comisario y escribir al admin)
- `GET /incidents?kind=&limit=` → `Incident` más recientes primero (límite 500).
- `POST /stewards/report` (ahora `async`: lee `cars`/`session` en el event loop; la comprobación del servidor va a un hilo) (`LimitsIn`: `car_id`, `name`, `ms`, `wheels`, `speed`, `lap`, `spline`, `pos`) → 204; 409 si el servidor no corre o ese auto no es ese piloto. Lo llama el **sitio** (`OPR WP/track_limits.py`) con el token del manager; la pide el script `opr_cuts.lua` del piloto.
- `PUT /stewards` `{mode: off|shadow}` → cambia `Server.stewards` y devuelve el `ServerOut`.

## Datos
- Tabla `incidents` (`app/models.py: Incident`): server_id, ts, session_type/name/ms, kind (`wall|contact|cuts|limits`: `limits` = ruedas fuera de pista, reportadas por el juego del piloto), car_id, driver/other guid y nombre, speed, value, world_pos, `fault_guid` (contacto: a quién señala la heurística, `None` si no hay culpa clara) y `evidence` (contacto: posición y velocidad de los dos autos y el motivo). «Piloto» y «con» de un contacto solo dicen quién reportó primero el evento, **no** quién tuvo la culpa. Solo se añade.
- `Server.stewards`: `off` (por defecto) | `shadow`. Se muestra también en `ServerOut`.

## Interactions
- **Entra:** `app/live/acsp.py` (`ACSPClient._apply`, import tardío para no hacer un ciclo) y `app/api/v1/__init__.py` (router).
- **Usa:** `app.live.acsp` (constantes `COLLISION_WITH_*`), `app.db.engine`, `app.models`, `app.servers` (`_get`, `_out`, `ServerOut`).
- **Sanciones automáticas (`auto.py`), separadas del motor de incidentes:** no usan `Incident`. `auto_penalties(sess, liga, server_id, filename, parsed)` lo llama `league._count_results` al contar por primera vez una Race de una liga con `Championship.auto_rules` (`{cuts_per_step, step_seconds, max_seconds}`; `None` = apagado). Suma los `Cuts` de las vueltas con tiempo de cada piloto que corre y crea un `Penalty` de tiempo (`created_by='auto'`, `auto_key='cuts'`, aviso a Discord). Una por (archivo, piloto, clave) —índice único de la migración 005—, así que contar el archivo en otra liga no la duplica (cuelga del archivo, no de la liga). Al retirarla un comisario queda `dismissed=1`: el resultado la ignora y no se vuelve a crear. Solo cortes; contactos y límites siguen siendo evidencia. No se reevalúan carreras ya contadas.
- **No hace aún:** hablar con el piloto, cola de comisarios para contactos.

## Flow
1. acServer manda un datagrama → `ACSPClient.datagram_received` → `parse` → `_apply`.
2. `_apply` actualiza tablero/coches y llama `engine.on_event`.
3. `detect` decide si es muro/contacto/cortes; `on_event` deduplica y encola el `Incident`; el hilo escritor comprueba el modo del servidor y lo guarda.
4. El comisario lo ve en `GET /servers/{id}/incidents`.
