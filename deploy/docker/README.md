# Docker para el PC de CDMX (borrador sin construir)

**Estado:** escrito el 2026-10-06 sin poder construirlo (el equipo de desarrollo no tenía el daemon de Docker ni el plugin `compose`). Hay que probarlo en el PC de CDMX
antes de fiarse. Los archivos: `docker-compose.yml`, `Dockerfile.manager`, `web.Dockerfile.example`, `.env.example`.

## Qué incluye
- **web** (el sitio de la liga, gunicorn de **un** worker) y **cloudflared** (túnel hacia Cloudflare): `docker compose up -d web cloudflared`.
- **manager** (opcional, perfil `manager`).
Todo con `network_mode: host`: la web habla con el manager por `127.0.0.1:8080` y el túnel con la web por `127.0.0.1:5100`; no se publica ningún puerto.

## Por qué el manager es opcional (importante)
- En el servidor actual el manager corre con systemd con `KillMode=process`: **reiniciar el manager no detiene los acServer** (los recupera con `adopt`). En un
  contenedor, reiniciarlo **mata todos los procesos** del contenedor, incluidos los acServer y las partidas en curso.
- Dentro de un contenedor no hay `systemd-run`: los **límites de CPU/RAM por servidor** (`ACM_LIMITS_SCOPE`) no funcionan; solo se puede limitar el contenedor entero.
- Hasta tener un contenedor por servidor (plan en `docker-plan.md`), lo recomendado es **manager nativo con systemd** y en Docker solo la web y el túnel.

## Red y seguridad
- Los puertos de juego (9600–9699) los abre el manager en el host; llegan por el túnel WireGuard del nodo de borde (`GET /edge/rules`), con el router cerrado.
- Secretos en archivos fuera del repo (`web.env`, `manager.env`, `cloudflared.token`), permisos 600. Fijar versiones de las imágenes antes de producción.
- La web guarda su base de datos en `${DATA_ROOT}/web`: incluirla en las copias (`ops/backup.py` de la web).

## Pendiente
Probar la construcción y el arranque; comprobar el `healthcheck` (el healthcheck usa `/api/servers`, como el despliegue actual de la web (puerto 5100)); decidir dónde corre el WireGuard de casa (en el host, no en un contenedor);
copias de seguridad de los volúmenes; un contenedor por servidor de juego (aislado, sin root).
