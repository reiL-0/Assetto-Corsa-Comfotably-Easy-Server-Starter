# app/api/v1 — router `/api/v1`

**Purpose:** `__init__.py` crea `api_router` (prefijo `/api/v1`), expone `GET /version` y **monta cada router con su nivel de acceso**:

| Router | Acceso |
|---|---|
| `auth` | login/logout/me, usuarios (admin), tokens propios |
| `servers` + `steward` | lectura y moderación: steward; escritura/configuración: admin (la config trae `ADMIN_PASSWORD`, por eso no es de lectura libre) |
| `metrics` | lectura steward |
| `penalties` | los stewards escriben sanciones (guarda propia) |
| `events`, `schedule`, `integrity` | steward lee, admin escribe |
| `telemetry` | **público** (la app del juego no tiene login; manda su SteamID64) |
| `acsm` (`app/live/acsm.py`) | lecturas públicas para el sitio de la liga, compatibles con ACSM; solo desde localhost |
| `content`, `championship` | cualquier usuario autenticado (`guard()`), escrituras con rol |

Para añadir un módulo: crear su `router` en `app/` e incluirlo aquí con la dependencia de acceso que corresponda. Documentar el módulo en `app/README.md`.
