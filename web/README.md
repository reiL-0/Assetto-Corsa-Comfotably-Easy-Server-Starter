# web — frontend React del manager

**Purpose:** SPA (Vite + React + TypeScript + Mantine + TanStack Query) del manager. Hoy es un andamio («Phase 0»): muestra el estado de `/healthz`.
La operación real se hace desde el panel del sitio de la liga (`OPR WP/admin/control.html`). Es un cliente más de la API.

| Archivo | Qué es |
|---|---|
| `package.json`, `package-lock.json` | Dependencias y scripts (`dev`, `build`). |
| `vite.config.ts` | Servidor de desarrollo con proxy de `/api` y `/healthz` al puerto 8080; la compilación sale a `../app/static`. |
| `tsconfig.json`, `index.html` | Configuración de TypeScript y documento base. |
| `src/` | Código (README propio). |

Uso: `make web` (compila a `app/static`, lo sirve el backend con `ACM_SERVE_UI=true`) · `make dev-web` (recarga en caliente).
