# app/admin — página de servidores

**Purpose:** una sola página HTML sin build (`servers.html`, Tailwind/Material Symbols por CDN) con el navegador de servidores del manager.
La sirve `main.admin_servers` en `GET /admin/servers`; usa la API `/api/v1` con la sesión del usuario.

| Archivo | Qué hace |
|---|---|
| `servers.html` | Tarjetas de servidores con estado, jugadores y acciones de arranque/parada. Tema oscuro propio (no comparte tokens con el sitio de la liga). |

**Interactions:** `app/main.py` (ruta) · `app/servers.py` (datos). El panel real de operación es `OPR WP/admin/control.html`.
