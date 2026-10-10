# Línea de hosting para terceros — archivada (2026-10-10)

Cinco ramas de 2026-10-06 (negocio de alquilar servidores a clientes) que nunca se fusionaron. Se conservan como **tags** (`git show archive/<nombre>`) y se borraron las ramas. Análisis de Gemini + Codex el 2026-10-10. Decisión: **no fusionar**; cuando haya fecha de lanzamiento, **rehacerla por etapas sobre el `main` de ese día**, no rebasar estas.

| Tag | Qué es | Notas |
|---|---|---|
| `archive/hosting-binaries` | registro de versiones oficiales de acServer por hash; versión por servidor | independiente de las demás |
| `archive/edge-rules` | generador de reglas nftables/WireGuard del nodo de borde (no las aplica) | independiente |
| `archive/hosting-tenants` | tenants, planes y tokens por servidor; lista blanca de rutas | incluye un commit duplicado de `upload-kind-redirect` |
| `archive/hosting-tenant-content` | contenido por cliente (copia única por hash, cuota, `content/` compuesto) | **incluye** `hosting-tenants` |
| `archive/docker-compose` | borrador de Docker para el PC de CDMX | nunca se construyó |

## Por qué no se fusionan (bloqueos)
- **Sin migraciones.** `main` usa migraciones numeradas (`app/db.py`, hoy 001/002) y `create_all` no añade columnas a tablas existentes: en producción darían `no such column`. Harían falta `_m003+` (binaries: `servers.acserver_binary_id` y `AcBinary`; tenants: `users.tenant_id`, `tokens.server_id`, `servers.tenant_id`, planes/tenants; tenant-content: `content_blobs.store`, backfill `shared`; `NULL` = servidor propio de OPR).
- **Conflictos con `main`.** `app/servers.py` se refactorizó a `app/services/server_service.py` (`server_lock`, puertos atómicos); `content.py`/`catalog.py` ganaron `source_url/source_official` y escaneo en segundo plano; también `models.py`, `api/v1/__init__.py`, README. Aceptar el `servers.py` viejo desharía protecciones.
- **Seguridad (tenants).** `/auth/tokens` está en la lista blanca y emite un token con `server_id=None`: un token limitado obtiene acceso a todos los servidores de su tenant. Lista blanca `tenancy.py:28` demasiado amplia (cualquier subruta futura).
- **tenant-content.** Cuota y deduplicación no atómicas; la cuota física se evade con versiones `superseded`; borrar y re-subir deja contenido inexistente; `/apply` no funciona con contenido solo del cliente; la revocación no recompone instancias existentes; subidas abandonadas sin caducidad.
- **binaries.** Comprueba que el ejecutable exista, no que coincida con el hash registrado.
- **edge.** `established,related` se acepta antes del limitador UDP (se lo salta); admite IPv6 y puertos 0/99999.
- **docker.** El wheel no incluye subpaquetes; `COPY . .` sin `.dockerignore` (riesgo de colar `.env`/bases); permisos de volúmenes (UID 995); Python 3.12 sin validar.

## Si se retoma
Rama de integración desde `main` → binaries (opcional) → tenants → tenant-content; edge en paralelo. Escribir las migraciones, mover la lógica al servicio, arreglar lo anterior, y probar en un PC aparte con una base copiada (OPR sin tenant, migración desde 001/002, API/scheduler/wake, dos clientes, tokens y subidas concurrentes). Los detalles están en el log de la sesión del monitor multiagente (`multi-agent-mcp/logs/debate-20261010-*-manual.md`).
